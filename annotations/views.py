import logging
import math

from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework.decorators import action
from drf_spectacular.types import OpenApiTypes
from rest_framework import viewsets, status as drf_status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from django.conf import settings
from django.db import models, transaction, IntegrityError
from django.db.utils import OperationalError
from django.db.models import Count, Q, F, Min, Prefetch
from django.shortcuts import get_object_or_404

from bible.models import Verse
from bible.utils.bible_books import (
    get_book_name_from_id,
    get_dbt_book_id,
)
from bible_research.permissions import GuestReadOnly

from .models import (
    Tag,
    Note,
    Comment,
    Image,
    ReadingPosition,
    generate_image_id
)
from .pagination import NotesPagination
from .serializers import (
    TagSerializer,
    NoteSerializer,
    NoteMoveSerializer,
    NoteReorderSerializer,
    LinkedNotesRequestSerializer,
    BulkNoteCreateSerializer,
    CommentSerializer,
    ImageSerializer,
    ReadingPositionSerializer,
    build_comment_tree,
    subtree_tag_ids,
)
from .services.image_storage import upload_original, delete_original

logger = logging.getLogger(__name__)


NOTE_IDS_CAP = 200

# `tag_id` sentinel meaning "notes with no tag". Real tag ids
# are `TAG…` strings, so this can never collide with one.
UNTAGGED_TAG_ID = 'untagged'

# Below this float gap between neighbouring tag positions the
# midpoint may no longer be representable; /notes/move/ then
# rebalances the whole tag instead of bisecting.
MOVE_GAP_EPSILON = 1e-9


def get_accessible_notes_qs(user):
    """
    Return a Note queryset scoped to what *user* may see:
    - Authenticated → own notes ∪ public notes
    - Anonymous     → public notes only
    """
    if user.is_authenticated:
        return Note.objects.filter(
            Q(user=user) | Q(public=True)
        )
    return Note.objects.filter(public=True)


def _canonical_note_order(queryset):
    """Order *queryset* by the canonical 'custom' ordering:
    tag_position ASC (nulls last), then created_at DESC."""
    return queryset.order_by(
        F('tag_position').asc(nulls_last=True),
        '-created_at',
    )


def _flatten_errors(errors):
    """Flatten a serializer ``errors`` structure into a single
    human-readable message string."""
    if isinstance(errors, dict):
        parts = []
        for field, msgs in errors.items():
            text = _flatten_errors(msgs)
            if field == 'non_field_errors':
                parts.append(text)
            else:
                parts.append(f'{field}: {text}')
        return '; '.join(parts)
    if isinstance(errors, (list, tuple)):
        return '; '.join(
            _flatten_errors(item) for item in errors
        )
    return str(errors)


def _validation_error(message):
    """Return a 400 in the annotations contract shape —
    ``{'error': 'validation_error', 'message'}`` — which the
    frontend branches on. Never embed ``str(exc)`` in it."""
    return Response(
        {
            'error': 'validation_error',
            'message': message,
        },
        status=drf_status.HTTP_400_BAD_REQUEST,
    )


def _serializer_error(errors):
    """Return a 400 in the annotations contract shape with a
    serializer's ``errors`` flattened into ``message``."""
    return _validation_error(_flatten_errors(errors))


def _position_conflict_response():
    """Return the 409 'position_conflict' body emitted by
    reorder/move when unique_tag_position_per_tag trips."""
    return Response(
        {
            'error': 'position_conflict',
            'message': (
                'Position conflict: the computed position is '
                'already occupied by another note in this tag. '
                'Please refresh and try again.'
            ),
            'constraint': 'unique_tag_position_per_tag',
        },
        status=drf_status.HTTP_409_CONFLICT,
    )


def _db_error_response():
    """Return the generic 409 body for non-position
    IntegrityError/OperationalError (e.g. deadlock) failures."""
    return Response(
        {
            'error': 'database_error',
            'message': (
                'A database constraint was violated. '
                'Please try again.'
            ),
        },
        status=drf_status.HTTP_409_CONFLICT,
    )


def _park_notes_for_renumber(notes, tag_id, extra_floor=None):
    """
    Assign every note a temporary tag_position parked below
    all positions currently used in the tag — including real
    negative values left behind by repeated 'before first'
    moves — and below the 1..N range a renumber writes next,
    so unique_tag_position_per_tag cannot trip mid-update.

    *extra_floor* is the lowest final position the caller will
    write in phase two (e.g. reorder's minimum requested
    position); temps park below it too so client-requested
    finals can never land inside the temp range.

    Temps step down one representable float at a time via
    ``math.nextafter`` — ``base - idx - 1`` silently collapses
    to ``base`` once |base| reaches ~2**52, making the
    "temporary" positions non-unique and tripping the
    constraint as a misleading 409.
    """
    lowest = Note.objects.filter(tag_id=tag_id).aggregate(
        lowest=Min('tag_position')
    )['lowest']
    base = min(0, lowest) if lowest is not None else 0
    if extra_floor is not None:
        base = min(base, extra_floor)
    if not math.isfinite(base):
        # A NaN/inf tag_position already in the DB (Postgres
        # persists them) must not poison the temp range.
        base = 0.0
    pos = base
    for note in notes:
        pos = math.nextafter(pos, -math.inf)
        note.tag_position = pos


def _is_tag_position_conflict(exc):
    """
    Return True if *exc* is an IntegrityError raised by the
    unique_tag_position_per_tag constraint.

    Postgres reports the constraint by name; SQLite instead
    lists the constrained columns
    ("UNIQUE constraint failed: annotations_note.tag_id,
    annotations_note.tag_position"), so match both forms.
    """
    error_msg = str(exc).lower()
    if 'unique_tag_position_per_tag' in error_msg:
        return True
    return (
        'unique constraint failed' in error_msg
        and 'tag_id' in error_msg
        and 'tag_position' in error_msg
    )


class TagViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows tags to be created, viewed, updated, or deleted.

    Authenticated users see and manage their own tags. Anonymous
    users can retrieve a tag only when it carries at least one
    public note (share pages, issue #9).
    """
    serializer_class = TagSerializer

    def get_permissions(self):
        # Reads are open: get_queryset scopes anonymous
        # requests to tags carrying at least one public note.
        # Writes always require authentication and are denied
        # for the legacy read-only 'guest' account.
        if self.action in ('list', 'retrieve'):
            return [AllowAny()]
        return [IsAuthenticated(), GuestReadOnly()]

    def get_queryset(self):
        """
        Returns the queryset of tags that the current user
        has access to.
        Authenticated users see their own tags.
        Anonymous users may retrieve only tags that have at
        least one public note; every other anonymous action
        sees an empty queryset.
        """
        user = self.request.user
        logger.info(
            f"TagViewSet.get_queryset called for user: "
            f"{user.username if user.is_authenticated else 'Anonymous'}"
        )

        # For authenticated users, return their own tags
        if user.is_authenticated:
            return Tag.objects.filter(
                user=user
            ).order_by('name')

        # Anonymous retrieve exposes a tag only when at least
        # one of its notes is public, so share pages can render
        # the tag name without leaking private-only tags.
        if self.action == 'retrieve':
            return Tag.objects.filter(
                notes__public=True
            ).distinct()
        return Tag.objects.none()

    @staticmethod
    def _duplicate_tag_error(exc):
        """Map a name-uniqueness IntegrityError to a 400.

        The serializer validates duplicates application-side;
        this only fires when a concurrent request wins the race
        between the check and the INSERT (or when the
        ``unique_top_level_tag_name_per_user`` constraint exists
        at the DB level).
        """
        error_msg = str(exc).lower()
        is_name_conflict = (
            'unique_top_level_tag_name_per_user' in error_msg
            or 'annotations_tag_user_id_name' in error_msg
            or (
                'unique constraint failed' in error_msg
                and 'user_id' in error_msg
                and 'name' in error_msg
            )
        )
        if is_name_conflict:
            raise ValidationError({
                'name': (
                    'You already have a tag with this name.'
                )
            })
        raise exc

    def perform_create(self, serializer):
        """
        Assigns the current authenticated user as the creator
        of the tag when a new tag is created.
        """
        user = self.request.user
        tag_name = serializer.validated_data.get('name', 'N/A')

        # IsAuthenticated (get_permissions) guarantees a real
        # user here — anonymous callers can no longer reach
        # create.
        try:
            serializer.save(user=user)
        except IntegrityError as exc:
            self._duplicate_tag_error(exc)
        logger.info(
            f"Created tag '{tag_name}' "
            f"for user {user.username}"
        )

    def perform_update(self, serializer):
        """
        Ensures that a user can only update their own tags.
        Raises PermissionDenied if a user attempts to update
        another user's tag — the queryset scoping normally
        404s first; this is the explicit ownership barrier in
        case scoping ever widens.
        """
        instance = serializer.instance
        user = self.request.user
        if not user.is_authenticated:
            raise PermissionDenied("Authentication required.")
        if instance.user != user:
            logger.warning(
                f"Permission denied: User {user.username} "
                f"attempted to update tag ID: {instance.id} "
                f"owned by "
                f"{instance.user.username if instance.user else 'nobody'}"
            )
            raise PermissionDenied(
                "You do not have permission "
                "to update this tag."
            )
        try:
            serializer.save()
        except IntegrityError as exc:
            self._duplicate_tag_error(exc)
        logger.info(
            f"Updated tag '{instance.name}' (ID: {instance.id}) "
            f"by user: {user.username}"
        )

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name='delete_notes',
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                description=(
                    'Pass delete_notes=true to delete the tag '
                    'together with every note under it and its '
                    'descendant tags. Without it, deleting a tag '
                    'that still has notes fails with 409 '
                    'Conflict.'
                ),
                required=False,
            ),
        ],
    )
    def destroy(self, request, *args, **kwargs):
        """
        DELETE /api/v1/tags/<pk>/[?delete_notes=true]

        Deleting a tag cascades to its notes — and to any
        descendant tags and their notes. To prevent silent data
        loss the delete is rejected with 409 Conflict while the
        tag subtree still has notes, unless the caller
        explicitly opts in with ?delete_notes=true.
        """
        instance = self.get_object()
        delete_notes = (
            request.query_params.get('delete_notes', '').lower()
            == 'true'
        )
        with transaction.atomic():
            # Lock the tag row: Postgres FK checks take
            # FOR KEY SHARE on the referenced parent, so a
            # concurrent note INSERT into this subtree waits for
            # the count-then-delete below instead of slipping
            # past the 409 guard.
            instance = Tag.objects.select_for_update().get(
                pk=instance.pk
            )
            note_count = Note.objects.filter(
                tag_id__in=subtree_tag_ids(
                    instance, user=instance.user
                )
            ).count()
            if note_count and not delete_notes:
                logger.warning(
                    f"Refusing to delete tag '{instance.name}' "
                    f"(ID: {instance.id}): {note_count} note(s) "
                    f"still attached"
                )
                return Response(
                    {
                        'error': 'tag_has_notes',
                        'message': (
                            f'This tag still has {note_count} '
                            f'note(s). Delete or move the notes '
                            f'first, or retry with '
                            f'?delete_notes=true to delete the '
                            f'tag together with its notes.'
                        ),
                        'note_count': note_count,
                    },
                    status=drf_status.HTTP_409_CONFLICT,
                )
            self.perform_destroy(instance)
        return Response(status=drf_status.HTTP_204_NO_CONTENT)

    def perform_destroy(self, instance):
        """
        Ensures that a user can only delete their own tags.
        Raises PermissionDenied if a user attempts to delete
        another user's tag — mirrors NoteViewSet.
        """
        user = self.request.user
        if not user.is_authenticated:
            raise PermissionDenied("Authentication required.")
        if instance.user != user:
            logger.warning(
                f"Permission denied: User {user.username} "
                f"attempted to delete tag ID: {instance.id} "
                f"owned by "
                f"{instance.user.username if instance.user else 'nobody'}"
            )
            raise PermissionDenied(
                "You do not have permission "
                "to delete this tag."
            )
        logger.info(
            f"Deleting tag '{instance.name}' (ID: {instance.id}) "
            f"by user: {user.username}"
        )
        # Detach cross-owner children before the CASCADE — the
        # API scopes parent_tag to the owner, so such links are
        # only creatable via admin/shell; without this guard the
        # cascade would destroy a stranger's tag subtree.
        Tag.objects.filter(parent_tag=instance).exclude(
            user=instance.user
        ).update(parent_tag=instance.parent_tag)
        instance.delete()
        logger.debug(
            f"Tag '{instance.name}' "
            f"successfully deleted"
        )


class NoteViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows notes to be created, viewed, updated, or deleted.
    Authenticated users see and manage their own notes.
    Unauthenticated users can view notes marked as public.

    Additional filtering:
    - GET /api/v1/notes/?tag_id={tag_id} - List notes filtered by tag ID
    - GET /api/v1/notes/?public=true - List both public and private notes
    - GET /api/v1/notes/?ordering={ordering} - Sort notes
    - GET /api/v1/notes/?page={page} - Paginate results
    - GET /api/v1/notes/?page_size={size} - Set page size
    - GET /api/v1/notes/?fileset_id={fileset_id} - Bible translation
    - POST /api/v1/notes/linked/ - Notes sharing given verses
    - GET /api/v1/notes/order-index/?tag_id= - Compact order index
    - POST /api/v1/notes/move/ - Move one note relative to another
    """
    serializer_class = NoteSerializer
    pagination_class = NotesPagination

    def get_permissions(self):
        # Reads are open: get_queryset scopes anonymous
        # requests to public notes only. Writes and the
        # other actions always require authentication
        # (issue #47) and are denied for the legacy
        # read-only 'guest' account.
        if self.action in ('list', 'retrieve', 'order_index'):
            return [AllowAny()]
        return [IsAuthenticated(), GuestReadOnly()]

    def get_serializer_context(self):
        """Add fileset_id from query params to serializer context."""
        context = super().get_serializer_context()
        fileset_id = self.request.query_params.get('fileset_id')
        if fileset_id:
            context['fileset_id'] = fileset_id
        return context

    def perform_create(self, serializer):
        """
        Assigns the current authenticated user as the creator
        of the note when a new note is created.
        """
        user = self.request.user
        if user.is_authenticated:
            note_text = serializer.validated_data.get(
                'note_text',
                'N/A'
            )[:50]
            serializer.save(user=user)
            logger.info(
                f"Created note for user {user.username}: "
                f"{note_text}..."
            )
        else:
            logger.warning(
                "Unauthenticated user attempted "
                "to create a note"
            )
            raise PermissionDenied(
                "Authentication required to create a note."
            )

    def perform_update(self, serializer):
        """
        Ensures that a user can only update their own notes.
        Raises PermissionDenied if a user attempts to update
        another user's note.
        """
        instance = serializer.instance
        user = self.request.user

        logger.info(
            f"Attempting to update note ID: {instance.id} "
            f"by user: "
            f"{user.username if user.is_authenticated else 'Anonymous'}"
        )

        if not user.is_authenticated:
            raise PermissionDenied("Authentication required.")
        if instance.user != user:
            logger.warning(
                f"Permission denied: User {user.username} "
                f"attempted to update note ID: {instance.id} "
                f"owned by {instance.user.username}"
            )
            raise PermissionDenied(
                "You do not have permission "
                "to update this note."
            )
        serializer.save()
        logger.debug(
            f"Note ID: {instance.id} "
            f"successfully updated"
        )

    def perform_destroy(self, instance):
        """
        Ensures that a user can only delete their own notes.
        Raises PermissionDenied if a user attempts to delete
        another user's note.
        """
        user = self.request.user

        logger.info(
            f"Attempting to delete note ID: {instance.id} "
            f"by user: "
            f"{user.username if user.is_authenticated else 'Anonymous'}"
        )

        if not user.is_authenticated:
            raise PermissionDenied("Authentication required.")
        if instance.user != user:
            logger.warning(
                f"Permission denied: User {user.username} "
                f"attempted to delete note ID: {instance.id} "
                f"owned by {instance.user.username}"
            )
            raise PermissionDenied(
                "You do not have permission "
                "to delete this note."
            )
        instance.delete()
        logger.debug(
            f"Note ID: {instance.id} "
            f"successfully deleted"
        )

    def _apply_ordering(self, queryset, ordering_param):
        """
        Apply ordering to queryset based on ordering parameter.

        Supported orderings:
        - custom: tag_position ASC, created_at DESC (default)
        - -custom: tag_position DESC, created_at ASC
        - created: created_at ASC
        - -created: created_at DESC
        - verse: first verse ASC
        - -verse: first verse DESC
        """
        from bible.utils.bible_books import BOOK_ORDER_MAP
        from django.db.models import Min, Case, When, IntegerField

        if ordering_param in ['verse', '-verse']:
            # Annotate with first verse info
            queryset = queryset.annotate(
                first_book=Min('verses__book'),
                first_chapter=Min('verses__chapter'),
                first_verse=Min('verses__verse'),
                book_order=Case(
                    *[
                        When(first_book=book, then=order)
                        for book, order in BOOK_ORDER_MAP.items()
                    ],
                    default=999,
                    output_field=IntegerField()
                )
            )

            if ordering_param == 'verse':
                return queryset.order_by(
                    'book_order',
                    'first_chapter',
                    'first_verse'
                )
            else:  # -verse
                return queryset.order_by(
                    '-book_order',
                    '-first_chapter',
                    '-first_verse'
                )

        elif ordering_param == 'custom':
            return _canonical_note_order(queryset)

        elif ordering_param == '-custom':
            return queryset.order_by(
                F('tag_position').desc(nulls_first=True),
                'created_at'
            )

        elif ordering_param == 'created':
            return queryset.order_by('created_at')

        elif ordering_param == '-created':
            return queryset.order_by('-created_at')

        else:
            # Default to custom ordering
            return _canonical_note_order(queryset)

    def get_queryset(self):
        """
        Returns the queryset of notes that the current user
        has access to with pagination and ordering support.

        - Authenticated users see their own private notes
          by default
        - Unauthenticated users see only public notes

        Available query parameters:
        - GET /api/v1/notes/ - List user's own notes
        - GET /api/v1/notes/?public=true - List public notes
          and user's notes
        - GET /api/v1/notes/<pk>/ - Retrieve a specific note
          by its ID
        - GET /api/v1/notes/?tag_id=<tag_id> - Filter by
          tag ID; `tag_id=untagged` lists notes with no tag
        - GET /api/v1/notes/?ordering=<ordering> - Sort order
          (custom, -custom, created, -created, verse, -verse)
        - GET /api/v1/notes/?page=<page> - Page number
        - GET /api/v1/notes/?page_size=<size> - Items per page

        Special cases:
        - When requesting a specific note by ID:
          Users see the note if it's public or their own
        - When filtering by tag_id:
          Users see all public notes with that tag and
          their own notes
        """
        user = self.request.user
        note_pk = self.kwargs.get('pk', None)
        tag_id = self.request.query_params.get('tag_id', None)
        ordering_param = self.request.query_params.get(
            'ordering', 'custom'
        )

        logger.info(
            f"NoteViewSet.get_queryset called for user: "
            f"{user.username if user.is_authenticated else 'Anonymous'} "
            f"| note_pk: {note_pk} | tag_id: {tag_id} "
            f"| ordering: {ordering_param}"
        )

        if note_pk or tag_id:
            # If specific note or tag is requested,
            # search it in public notes
            queryset = get_accessible_notes_qs(user)
            logger.debug(
                "Using get_accessible_notes_qs due to "
                "note_pk or tag_id filter"
            )
        else:
            # Otherwise evaluate if user requested public notes
            public_param = self.request.query_params.get(
                'public',
                ''
            ).lower()
            public = public_param == 'true'
            logger.debug(
                f"Public parameter: {public_param}, "
                f"public={public}"
            )

            if user.is_authenticated:
                user_notes = models.Q(user=user)
                if public:
                    queryset = Note.objects.filter(
                        user_notes | models.Q(public=True)
                    )
                    logger.debug(
                        f"Authenticated user {user.username}: "
                        f"Fetching own notes + public notes"
                    )
                else:
                    queryset = Note.objects.filter(user_notes)
                    logger.debug(
                        f"Authenticated user {user.username}: "
                        f"Fetching only own notes"
                    )
            else:
                # Anonymous callers always see public notes
                # only; the ?public= flag must not widen the
                # scope to private notes (issue #47).
                queryset = get_accessible_notes_qs(user)
                logger.debug(
                    "Unauthenticated user: "
                    "Fetching public notes only"
                )

        if tag_id == UNTAGGED_TAG_ID:
            queryset = queryset.filter(tag__isnull=True)
            logger.debug("Filtering untagged notes")
        elif tag_id:
            queryset = queryset.filter(tag_id=tag_id)
            logger.debug(f"Filtering by tag_id: {tag_id}")

        if note_pk:
            queryset = queryset.filter(id=note_pk)
            logger.debug(f"Filtering by note_pk: {note_pk}")

        # Apply ordering based on parameter
        final_queryset = self._apply_ordering(
            queryset, ordering_param
        )
        # NoteSerializer expands ``tag`` and iterates ``verses``
        # per note — eager-load both relations so a page of
        # notes costs two queries, not N+1.
        final_queryset = final_queryset.select_related(
            'tag'
        ).prefetch_related('verses')
        logger.info(
            f"Returning notes with ordering: {ordering_param}"
        )
        return final_queryset

    @action(
        detail=False, methods=['post'], url_path='bulk'
    )
    def bulk_create_notes(self, request):
        """
        Bulk-create notes under a single tag.
        POST /api/v1/notes/bulk/
        """
        if not request.user.is_authenticated:
            raise PermissionDenied('Authentication required.')
        serializer = BulkNoteCreateSerializer(
            data=request.data,
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            notes = serializer.save()
        output = NoteSerializer(
            notes,
            many=True,
            context=self.get_serializer_context(),
        )
        return Response(
            output.data,
            status=drf_status.HTTP_201_CREATED,
        )

    @extend_schema(
        request=LinkedNotesRequestSerializer,
        parameters=[
            OpenApiParameter(
                name='fileset_id',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description=(
                    'Bible translation fileset. Accepted for '
                    'parity with other note endpoints; linked '
                    'results are not enriched with verse text.'
                ),
                required=False,
            ),
        ],
        responses={
            200: {
                'type': 'object',
                'properties': {
                    'count': {'type': 'integer'},
                    'results': {
                        'type': 'array',
                        'items': {'type': 'object'},
                    },
                },
            },
        },
    )
    @action(detail=False, methods=['post'], url_path='linked')
    def linked(self, request):
        """
        List the requesting user's notes linked to any of the
        given verses.

        POST /api/v1/notes/linked/
        {
          "verse_references": [
            {"book": "John", "chapter": 3, "verse": 16}
          ]
        }

        A note counts as linked when it references at least one
        requested verse. Returns {"count": N, "results": [...]}
        where each result carries verse coordinates only — no
        upstream provider call is made per note.
        """
        serializer = LinkedNotesRequestSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        user = request.user
        # Defense-in-depth: anonymous requests are rejected
        # earlier by this viewset's IsAuthenticated permission
        # (and the device middleware auto-provisions a user for
        # almost every request).
        if not user.is_authenticated:
            return Response({'count': 0, 'results': []})

        query = Q()
        for ref in serializer.validated_data['verse_references']:
            query |= Q(
                verses__book__iexact=ref['book'],
                verses__chapter=ref['chapter'],
                verses__verse=ref['verse'],
            )
        notes = (
            Note.objects
            .filter(user=user)
            .filter(query)
            .select_related('tag')
            .prefetch_related('verses')
            .distinct()
            .order_by('-created_at')
        )
        context = self.get_serializer_context()
        # The linked-notes UI only needs verse coordinates;
        # skipping text enrichment avoids one blocking upstream
        # Bible-provider call per note.
        context['include_verse_text'] = False
        output = NoteSerializer(
            notes,
            many=True,
            context=context,
        )
        return Response(
            {'count': len(output.data), 'results': output.data}
        )

    @action(detail=False, methods=['post'], url_path='reorder')
    def reorder(self, request):
        """
        Reorders notes within a tag (up to 100 notes per request).

        Accepts:
        {
          "tag_id": "TAG123",
          "updates": [
            {"note_id": "NOT456", "position": 50.0},
            {"note_id": "NOT789", "position": 51.0}
          ]
        }

        Uses a two-phase update to avoid constraint violations
        when swapping positions:
        1. Move all notes to temporary negative positions
        2. Move them to final positions

        The database enforces unique positions per tag via
        the 'unique_tag_position_per_tag' constraint.
        """
        serializer = NoteReorderSerializer(data=request.data)
        if not serializer.is_valid():
            return _serializer_error(serializer.errors)
        tag_id = serializer.validated_data['tag_id']
        updates = serializer.validated_data['updates']
        position_map = {
            update['note_id']: update['position']
            for update in updates
        }
        note_ids = list(position_map)
        positions = list(position_map.values())

        user = request.user
        # Defense-in-depth (mirrors move/): filtering on an
        # AnonymousUser would raise TypeError -> 500.
        if not user.is_authenticated:
            raise PermissionDenied('Authentication required.')

        try:
            with transaction.atomic():
                # Fetch and lock notes
                notes = list(
                    Note.objects.select_for_update().filter(
                        id__in=note_ids,
                        user=user,
                        tag_id=tag_id
                    )
                )

                if len(notes) != len(note_ids):
                    found = {n.id for n in notes}
                    missing = set(note_ids) - found
                    return _validation_error(
                        f'Notes not found or do not '
                        f'belong to tag {tag_id}: '
                        f'{missing}'
                    )

                # Two-phase update to avoid constraint violations
                # during swaps:
                # Phase 1: Move to temporary negative positions,
                # parked below any real negatives already in
                # the tag and below the lowest requested final
                # position, so client-requested finals cannot
                # land inside the temp range and trip the
                # constraint as a misleading 409.
                _park_notes_for_renumber(
                    notes, tag_id, extra_floor=min(positions)
                )

                Note.objects.bulk_update(notes, ['tag_position'])

                # Phase 2: Move to final positions
                for note in notes:
                    note.tag_position = position_map[note.id]

                Note.objects.bulk_update(notes, ['tag_position'])

            return Response(status=drf_status.HTTP_204_NO_CONTENT)

        except IntegrityError as e:
            # Database rejected due to unique constraint
            # violation (unique_tag_position_per_tag)
            if _is_tag_position_conflict(e):
                return _position_conflict_response()
            logger.error(
                f'Unexpected IntegrityError in reorder: {e}'
            )
            return _db_error_response()
        except OperationalError:
            # e.g. a deadlock aborting the transaction — retryable.
            logger.error('OperationalError (deadlock?) in reorder')
            return _db_error_response()

    @extend_schema(
        parameters=[
            OpenApiParameter(
                name='tag_id',
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description=(
                    'Tag whose accessible notes to list as a '
                    'compact order index.'
                ),
                required=True,
            ),
        ],
    )
    @action(
        detail=False, methods=['get'], url_path='order-index'
    )
    def order_index(self, request):
        """
        Compact, unpaginated index of every accessible note in a
        tag, in canonical custom order (tag_position ASC, nulls
        last, created_at DESC — same as ordering=custom).

        GET /api/v1/notes/order-index/?tag_id=TAG123

        Each row carries the note id, tag_position and passage
        label coordinates: book/chapter of the note's "first"
        verse and the min/max verse within that book+chapter
        block. Mirroring NoteSerializer, verses are ordered by
        verse NUMBER only, so the first verse is the smallest
        verse number across all books/chapters. Notes without
        verses return null for all four label fields. Built via
        values/prefetch — no NoteSerializer, so no upstream
        provider call per note.
        """
        tag_id = request.query_params.get('tag_id')
        if not tag_id:
            return _validation_error(
                'tag_id query parameter is required.'
            )

        notes_qs = get_accessible_notes_qs(request.user)
        if tag_id == UNTAGGED_TAG_ID:
            # Same sentinel semantics as GET /notes/ and
            # /comments/counts/: 'untagged' resolves to notes
            # with no tag — filtering the literal string would
            # always return [].
            notes_qs = notes_qs.filter(tag__isnull=True)
        else:
            notes_qs = notes_qs.filter(tag_id=tag_id)

        notes = (
            _canonical_note_order(
                notes_qs
                # Avoid loading heavy note_text rows at scale; the
                # verse prefetch runs as a separate query and is
                # unaffected by .only().
                .only('id', 'tag_position', 'created_at')
            )
            .prefetch_related(
                Prefetch(
                    'verses',
                    queryset=Verse.objects.order_by('verse'),
                )
            )
        )
        entries = []
        for note in notes:
            verses = list(note.verses.all())
            book = chapter = verse_start = verse_end = None
            if verses:
                book = verses[0].book
                chapter = verses[0].chapter
                block = [
                    v.verse for v in verses
                    if v.book == book and v.chapter == chapter
                ]
                verse_start = min(block)
                verse_end = max(block)
            entries.append({
                'id': note.id,
                'tag_position': note.tag_position,
                'book': book,
                'chapter': chapter,
                'verse_start': verse_start,
                'verse_end': verse_end,
            })
        return Response(entries)

    @action(detail=False, methods=['post'], url_path='move')
    def move(self, request):
        """
        Move a single note next to another note within a tag.

        POST /api/v1/notes/move/
        {
          "tag_id": "TAG123",
          "note_id": "NOT456",
          "target_note_id": "NOT789",
          "placement": "after"   # or "before"
        }

        placement is interpreted in canonical ascending order
        (tag_position ASC): 'after' the last note appends at
        last+1, 'before' the first note prepends at first-1, and
        between two notes the new position is the float midpoint.
        When the float gap is exhausted, all of the tag's notes
        are renumbered 1..N in current order and the moved note
        takes its slot — one transaction, with the same two-phase
        temp-position trick as reorder to satisfy
        unique_tag_position_per_tag.

        Returns 200 {"note_id", "tag_position", "rebalanced"}.
        """
        serializer = NoteMoveSerializer(data=request.data)
        if not serializer.is_valid():
            return _serializer_error(serializer.errors)
        data = serializer.validated_data
        tag_id = data['tag_id']
        note_id = data['note_id']
        target_note_id = data['target_note_id']
        placement = data['placement']

        if not request.user.is_authenticated:
            raise PermissionDenied('Authentication required.')

        user = request.user

        def _err(message):
            return _validation_error(message)

        try:
            with transaction.atomic():
                # Lock the tag's ordered notes; placement is
                # computed against canonical ascending order.
                # Scope note: order-index (like GET /notes/)
                # also lists other users' PUBLIC notes in the
                # tag, but move only sees/locks the requester's
                # own — a foreign public note anchor 400s, and
                # foreign positions in a shared tag can 409.
                notes = list(
                    _canonical_note_order(
                        Note.objects.select_for_update()
                        .filter(user=user, tag_id=tag_id)
                    )
                )
                by_id = {n.id: n for n in notes}
                note = by_id.get(note_id)
                target = by_id.get(target_note_id)
                missing = [
                    nid
                    for nid, found in (
                        (note_id, note),
                        (target_note_id, target),
                    )
                    if found is None
                ]
                if missing:
                    return _err(
                        f'Notes not found or do not belong to '
                        f'tag {tag_id}: {missing}'
                    )

                # Extract the moved note, then find the neighbours
                # bracketing the target slot.
                ordered = [n for n in notes if n.id != note_id]
                target_idx = next(
                    i for i, n in enumerate(ordered)
                    if n.id == target_note_id
                )
                if placement == 'after':
                    low_idx = target_idx
                    high_idx = target_idx + 1
                else:
                    low_idx = target_idx - 1
                    high_idx = target_idx
                low = ordered[low_idx] if low_idx >= 0 else None
                high = (
                    ordered[high_idx]
                    if high_idx < len(ordered)
                    else None
                )
                low_pos = low.tag_position if low else None
                high_pos = high.tag_position if high else None

                new_pos = None
                rebalanced = False
                if low_pos is not None and high_pos is not None:
                    gap = high_pos - low_pos
                    midpoint = low_pos + gap / 2.0
                    if (
                        gap > MOVE_GAP_EPSILON
                        and low_pos < midpoint < high_pos
                    ):
                        new_pos = midpoint
                    else:
                        # Float exhaustion: no representable
                        # midpoint left between the neighbours.
                        rebalanced = True
                elif high is None:
                    # 'after' the last note.
                    if low_pos is not None:
                        new_pos = low_pos + 1.0
                    else:
                        rebalanced = True
                else:
                    # 'before' the first note.
                    if high_pos is not None:
                        new_pos = high_pos - 1.0
                    else:
                        rebalanced = True

                # No position is client-supplied here, but a
                # non-finite value already stored in the DB
                # (Postgres persists NaN) makes the midpoint /
                # last+1 / first-1 arithmetic above meaningless;
                # renumber 1..N instead of writing it back.
                if new_pos is not None and not math.isfinite(
                    new_pos
                ):
                    rebalanced = True

                if rebalanced:
                    # Insert the moved note at the slot bracketed
                    # by low/high, then renumber 1..N.
                    final_order = (
                        ordered[:high_idx]
                        + [note]
                        + ordered[high_idx:]
                    )
                    # Phase 1: park every tag note on a temporary
                    # position below all existing values — 'before
                    # first' moves leave real negatives (0, -1,
                    # -2, ...) that would collide with naive
                    # -(idx+1) temps — and below the final 1..N
                    # range.
                    _park_notes_for_renumber(notes, tag_id)
                    Note.objects.bulk_update(
                        notes, ['tag_position']
                    )
                    # Phase 2: write the final sequential slots.
                    for idx, n in enumerate(final_order):
                        n.tag_position = float(idx + 1)
                    Note.objects.bulk_update(
                        final_order, ['tag_position']
                    )
                    new_pos = note.tag_position
                else:
                    note.tag_position = new_pos
                    note.save(update_fields=['tag_position'])

            return Response(
                {
                    'note_id': note.id,
                    'tag_position': new_pos,
                    'rebalanced': rebalanced,
                },
                status=drf_status.HTTP_200_OK,
            )

        except IntegrityError as e:
            # Database rejected due to unique constraint
            # violation (unique_tag_position_per_tag)
            if _is_tag_position_conflict(e):
                return _position_conflict_response()
            logger.error(
                f'Unexpected IntegrityError in move: {e}'
            )
            return _db_error_response()
        except OperationalError:
            # e.g. a deadlock aborting the transaction — retryable.
            logger.error('OperationalError (deadlock?) in move')
            return _db_error_response()


class CommentViewSet(viewsets.ModelViewSet):
    """
    API endpoint for threaded comments on a Note.

    URL pattern (nested under a note):
      GET    /api/v1/notes/{note_pk}/comments/
          Returns the full comment tree for the note.
          Root-level comments are returned with nested
          'replies' lists. Deleted comments appear as
          '[deleted]' to preserve thread structure.

      POST   /api/v1/notes/{note_pk}/comments/
          Create a top-level or reply comment.
          Pass 'parent_comment' PK to create a reply.

      GET    /api/v1/notes/{note_pk}/comments/{pk}/
          Retrieve a single comment (flat). Deleted
          comments are redacted the same way as in the
          tree ('[deleted]', images []).

      PATCH  /api/v1/notes/{note_pk}/comments/{pk}/
          Update a comment's content. 'is_deleted' is
          read-only and 'parent_comment' is create-only,
          so PATCH can neither un-delete nor re-parent.

      DELETE /api/v1/notes/{note_pk}/comments/{pk}/
          Soft-delete: sets is_deleted=True and clears
          content. Thread structure is preserved.

    Access: the parent note must be visible to the requester
    (own notes or public notes — the same scope as
    get_accessible_notes_qs). Comments on inaccessible notes
    return 404 so the note's existence is not leaked.
    Any authenticated user may comment on a public note;
    edits and deletes remain author-only.

    N+1 prevention:
      get_queryset() issues one DB query with
      select_related('user') for all list and tree
      operations. build_comment_tree() assembles the
      hierarchy entirely in Python.
    """

    serializer_class = CommentSerializer
    http_method_names = [
        'get', 'post', 'patch', 'delete', 'head', 'options',
    ]

    def get_permissions(self):
        # Reads are open: visibility is enforced by
        # get_accessible_notes_qs (anon may see public notes).
        # Writes always require authentication and are denied
        # for the legacy read-only 'guest' account.
        if self.action in ('list', 'retrieve'):
            return [AllowAny()]
        return [IsAuthenticated(), GuestReadOnly()]

    def get_serializer_context(self):
        """Expose the URL's note_pk so CommentSerializer can
        scope parent_comment lookups to this note."""
        context = super().get_serializer_context()
        note_pk = self.kwargs.get('note_pk')
        if note_pk is not None:
            context['note_pk'] = note_pk
        return context

    def _get_note(self):
        """
        Return the parent Note when the requester may see it,
        else raise 404 — inaccessible notes are indistinguishable
        from nonexistent ones.
        """
        return get_object_or_404(
            get_accessible_notes_qs(self.request.user),
            pk=self.kwargs['note_pk'],
        )

    def get_queryset(self):
        """
        Return all comments for the note in one query.
        select_related('user') prevents per-comment author
        lookups during serialization. Comments are also
        restricted to notes the requester may access, so
        detail routes 404 on other users' private notes.
        """
        note_pk = self.kwargs['note_pk']
        queryset = Comment.objects.filter(note_id=note_pk)
        if self.action != 'list':
            # list() resolves the note through _get_note() — the
            # authoritative existence + accessibility check —
            # before calling this, so embedding the
            # accessible-notes subquery here too would only
            # duplicate that work. Detail routes still need it.
            queryset = queryset.filter(
                note__in=get_accessible_notes_qs(
                    self.request.user
                ),
            )
        return (
            queryset
            .select_related('user')
            .prefetch_related('images')
            .order_by('timestamp')
        )

    def list(self, request, *args, **kwargs):
        """Return comments as a nested tree (no N+1)."""
        # 404 when the note does not exist or is a private
        # note owned by someone else — never leak existence.
        self._get_note()
        queryset = self.get_queryset()
        tree = build_comment_tree(queryset)
        logger.info(
            f"Returning comment tree for note "
            f"{self.kwargs.get('note_pk')} "
            f"({len(queryset)} total comments)"
        )
        return Response(tree)

    def create(self, request, *args, **kwargs):
        """404 inaccessible notes before serializer validation —
        otherwise a 400-vs-404 split on parent_comment would
        leak which comment ids exist on a private note."""
        self._get_note()
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """Inject request user and parent note on create."""
        note = self._get_note()
        user = self.request.user
        logger.info(
            f"User {user.username} creating comment "
            f"on note {note.id}"
        )
        serializer.save(user=user, note=note)

    def perform_update(self, serializer):
        """Allow only the comment author to update.

        Deleted comments are tombstones — PATCH on one is
        rejected outright so a stub can never be quietly
        rewritten after removal.
        """
        instance = serializer.instance
        user = self.request.user
        if instance.user != user:
            raise PermissionDenied(
                "You do not have permission "
                "to edit this comment."
            )
        if instance.is_deleted:
            raise ValidationError(
                "This comment has been deleted and "
                "cannot be edited."
            )
        serializer.save()
        logger.info(
            f"User {user.username} updated "
            f"comment {instance.id}"
        )

    def perform_destroy(self, instance):
        """
        Soft-delete: mark as deleted and redact content.
        The row is retained so child replies remain intact.

        The comment author OR the owner of the parent note may
        delete — the note owner moderates discussion on their
        own note (mirrors ImageDestroyView's note-owner rule).
        """
        user = self.request.user
        if (
            instance.user != user
            and instance.note.user != user
        ):
            raise PermissionDenied(
                "You do not have permission "
                "to delete this comment."
            )
        instance.is_deleted = True
        instance.content = ''
        instance.save(update_fields=['is_deleted', 'content'])
        logger.info(
            f"User {user.username} soft-deleted "
            f"comment {instance.id}"
        )


@extend_schema(
    parameters=[
        OpenApiParameter(
            name='tag_id',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description=(
                'Return counts for every accessible note '
                'carrying this tag. Mutually exclusive '
                'with note_ids.'
            ),
            required=False,
        ),
        OpenApiParameter(
            name='note_ids',
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description=(
                'Comma-separated note PKs (max 200). '
                'Mutually exclusive with tag_id.'
            ),
            required=False,
        ),
        OpenApiParameter(
            name='include_deleted',
            type=OpenApiTypes.BOOL,
            location=OpenApiParameter.QUERY,
            description=(
                'When true, soft-deleted comments are '
                'included in the count. Default false.'
            ),
            required=False,
        ),
    ],
    responses={
        200: {
            'type': 'object',
            'properties': {
                'counts': {
                    'type': 'object',
                    'additionalProperties': {'type': 'integer'},
                    'example': {
                        'NOT0123': 5,
                        'NOT0456': 0,
                    },
                },
            },
        }
    },
)
class CommentCountView(APIView):
    """
    GET /api/v1/comments/counts/

    Returns the number of (non-deleted) comments per Note.
    Exactly one of tag_id or note_ids must be supplied.
    `tag_id=untagged` counts comments on notes with no tag.
    """

    # Anonymous callers must keep working: share pages fetch
    # comment counts without a token. Counts stay scoped to
    # public notes via get_accessible_notes_qs below.
    permission_classes = [AllowAny]

    def get(self, request):
        params = request.query_params
        tag_id = params.get('tag_id')
        note_ids_raw = params.get('note_ids')
        include_deleted = (
            params.get('include_deleted', 'false').lower()
            == 'true'
        )

        has_tag = bool(tag_id)
        has_ids = bool(note_ids_raw)

        if has_tag and has_ids:
            raise ValidationError(
                'Provide either tag_id or note_ids, not both.'
            )
        if not has_tag and not has_ids:
            raise ValidationError(
                'One of tag_id or note_ids is required.'
            )

        note_ids = []
        if has_ids:
            raw_parts = [
                p.strip() for p in note_ids_raw.split(',')
                if p.strip()
            ]
            if len(raw_parts) > NOTE_IDS_CAP:
                raise ValidationError(
                    f'note_ids may contain at most '
                    f'{NOTE_IDS_CAP} IDs.'
                )
            note_ids = raw_parts

        notes_qs = get_accessible_notes_qs(request.user)

        if has_tag:
            if tag_id == UNTAGGED_TAG_ID:
                notes_qs = notes_qs.filter(tag__isnull=True)
            else:
                notes_qs = notes_qs.filter(tag_id=tag_id)
        else:
            notes_qs = notes_qs.filter(id__in=note_ids)

        count_filter = (
            Q()
            if include_deleted
            else Q(comments__is_deleted=False)
        )
        rows = (
            notes_qs
            .annotate(
                comment_count=Count(
                    'comments',
                    filter=count_filter,
                ),
            )
            .values_list('id', 'comment_count')
        )

        counts = {note_id: cnt for note_id, cnt in rows}
        logger.info(
            f"CommentCountView returning counts for "
            f"{len(counts)} notes"
        )
        return Response({'counts': counts})


class _BaseImageViewSet(viewsets.GenericViewSet):
    """Shared machinery for image attachments on notes/comments.

    Both parents expose the same route shape —
    ``GET/POST …/images/`` — with the same upload flow:
    parent resolution (404 for inaccessible/nonexistent),
    an uploader-identity check, a per-parent image cap enforced
    cheaply and again under a row lock, GCS upload, then a
    rollback-deleting row insert. Subclasses supply only the
    parent-specific bits via the class attributes and
    ``_get_parent``/``_check_upload_allowed``.
    """

    serializer_class = ImageSerializer
    parser_classes = [MultiPartParser]

    # Subclass hooks:
    parent_model = None       # ORM class row-locked on upload
    parent_lookup = None      # Image FK name: 'note'/'comment'
    cap_setting = None        # settings attr holding the cap
    parent_label = 'parent'   # word used in cap error messages

    def get_permissions(self):
        # Reads are open: visibility is enforced by
        # get_accessible_notes_qs (anon may see public notes).
        # Writes always require authentication and are denied
        # for the legacy read-only 'guest' account.
        if self.action in ('list', 'retrieve'):
            return [AllowAny()]
        return [IsAuthenticated(), GuestReadOnly()]

    def _get_parent(self):
        """Return the parent object, or raise 404 when the
        requester may not see it (no existence oracle)."""
        raise NotImplementedError

    def _check_upload_allowed(self, parent, user):
        """Raise PermissionDenied/ValidationError when *user*
        may not attach an image to *parent*."""
        raise NotImplementedError

    def _cap_error(self, max_images):
        return ValidationError({
            'file': (
                f'Maximum {max_images} images '
                f'per {self.parent_label}.'
            )
        })

    def _images_response(self, request, parent):
        images = Image.objects.filter(
            **{self.parent_lookup: parent}
        )
        serializer = ImageSerializer(
            images, many=True, context={'request': request}
        )
        return Response(serializer.data)

    def list(self, request, **kwargs):
        parent = self._get_parent()
        return self._images_response(request, parent)

    def create(self, request, **kwargs):
        parent = self._get_parent()
        user = request.user
        self._check_upload_allowed(parent, user)
        file_obj = request.FILES.get('file')
        if not file_obj:
            raise ValidationError({'file': 'No file provided.'})
        max_images = getattr(settings, self.cap_setting)
        lookup = {self.parent_lookup: parent}
        # Cheap pre-check before the expensive GCS round-trip.
        # The authoritative check happens under a row lock below.
        if Image.objects.filter(**lookup).count() >= max_images:
            raise self._cap_error(max_images)

        image_id = generate_image_id()
        gs_uri, size_bytes, content_type = upload_original(
            image_id, file_obj
        )
        try:
            with transaction.atomic():
                # Lock the parent row so concurrent uploads cannot
                # both pass the cap check.
                locked = self.parent_model.objects\
                    .select_for_update().get(pk=parent.pk)
                locked_lookup = {self.parent_lookup: locked}
                if Image.objects.filter(
                    **locked_lookup
                ).count() >= max_images:
                    raise self._cap_error(max_images)
                image = Image.objects.create(
                    id=image_id,
                    uploaded_by=user,
                    storage_url=gs_uri,
                    size_bytes=size_bytes,
                    content_type=content_type,
                    **locked_lookup,
                )
        except Exception:
            delete_original(image_id, gs_uri)
            raise
        logger.info(
            "User %s uploaded image %s to %s %s",
            user.username, image.id,
            self.parent_label, parent.id,
        )
        serializer = ImageSerializer(
            image, context={'request': request}
        )
        return Response(serializer.data, status=201)


class NoteImageViewSet(_BaseImageViewSet):
    """
    Image attachments scoped to a Note.

      GET  /api/v1/notes/{note_pk}/images/
      POST /api/v1/notes/{note_pk}/images/

    Upload: multipart/form-data with a single 'file' field.
    Requester must be the note's owner.
    Read: same accessibility scope as get_accessible_notes_qs.
    """

    parent_model = Note
    parent_lookup = 'note'
    cap_setting = 'IMAGE_MAX_PER_NOTE'
    parent_label = 'note'

    def _get_parent(self):
        """Return the parent Note when the requester may see it,
        else raise 404 — inaccessible notes are indistinguishable
        from nonexistent ones (no existence oracle)."""
        return get_object_or_404(
            get_accessible_notes_qs(self.request.user),
            pk=self.kwargs['note_pk'],
        )

    def _check_upload_allowed(self, note, user):
        if not user.is_authenticated or note.user != user:
            raise PermissionDenied(
                "Only the note's owner may upload images."
            )


class CommentImageViewSet(_BaseImageViewSet):
    """
    Image attachments scoped to a Comment.

      GET  /api/v1/notes/{note_pk}/comments/{comment_pk}/images/
      POST /api/v1/notes/{note_pk}/comments/{comment_pk}/images/

    Upload: multipart/form-data with a single 'file' field.
    Requester must be the comment's author.
    """

    parent_model = Comment
    parent_lookup = 'comment'
    cap_setting = 'IMAGE_MAX_PER_COMMENT'
    parent_label = 'comment'

    def _get_parent(self):
        """Return the parent Comment when the requester may see
        its note, else raise 404 — comments on inaccessible
        (private foreign) notes are indistinguishable from
        nonexistent ones (no existence oracle)."""
        return get_object_or_404(
            Comment.objects.filter(
                note__in=get_accessible_notes_qs(
                    self.request.user
                ),
            ),
            pk=self.kwargs['comment_pk'],
            note_id=self.kwargs['note_pk'],
        )

    def _check_upload_allowed(self, comment, user):
        if not user.is_authenticated or comment.user != user:
            raise PermissionDenied(
                "Only the comment's author may upload images."
            )
        if comment.is_deleted:
            # A tombstone must not accrete attachments — the
            # serializer redacts them anyway, so serving a new
            # upload would violate the redaction contract.
            raise ValidationError(
                {'file': (
                    'Cannot attach images to a '
                    'deleted comment.'
                )}
            )

    def list(self, request, note_pk=None, comment_pk=None):
        # 404 when the note/comment does not exist or lives on a
        # private note owned by someone else.
        comment = self._get_parent()
        if comment.is_deleted:
            # CommentSerializer redacts deleted comments to
            # ``images: []`` — the nested route must honor the
            # same tombstone contract instead of serving the
            # signed URLs the redaction was meant to hide.
            return Response([])
        return self._images_response(request, comment)


class ImageDestroyView(APIView):
    """
    DELETE /api/v1/images/{image_pk}/

    Only the uploader, the comment author, or the owner of the
    parent note may delete. For comment-attached images the
    parent note is reached through image.comment.note.
    Hard-delete: DB row removed and GCS object deleted
    (best-effort — the post_delete receiver on Image calls
    delete_original for every removal path).

    Images the requester may not delete return 404 —
    indistinguishable from nonexistent — so the endpoint leaks
    no existence oracle.
    """

    permission_classes = [IsAuthenticated, GuestReadOnly]

    def delete(self, request, image_pk):
        user = request.user
        # One query resolves the row AND the delete permission:
        # comment-attached images have note=NULL (XOR FK), so the
        # parent note is reached through comment.note.
        image = get_object_or_404(
            Image.objects.select_related(
                'note', 'comment', 'comment__note'
            ).filter(
                Q(uploaded_by=user)
                | Q(note__user=user)
                | Q(comment__user=user)
                | Q(comment__note__user=user)
            ),
            pk=image_pk,
        )

        image_id = image.id
        image.delete()
        logger.info(
            "User %s deleted image %s",
            user.username, image_id,
        )
        return Response(status=drf_status.HTTP_204_NO_CONTENT)


class ReadingPositionViewSet(viewsets.ModelViewSet):
    """
    API endpoint for reading position tracking.

    GET /api/v1/reading-positions/
    GET /api/v1/reading-positions/?book=John
    POST /api/v1/reading-positions/
    POST /api/v1/reading-positions/bulk/
    """
    serializer_class = ReadingPositionSerializer

    def get_permissions(self):
        # DEFAULT_PERMISSION_CLASSES already apply
        # IsAuthenticated + GuestReadOnly. ``bulk`` is a READ
        # implemented as POST (the book list belongs in a body,
        # not a query string), so it exempts only GuestReadOnly
        # — a legacy guest token may read positions the same
        # way GET ?book= already allows.
        if self.action == 'bulk':
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_queryset(self):
        """Filter positions to current user only."""
        user = self.request.user
        queryset = ReadingPosition.objects.filter(user=user)

        book = self.request.query_params.get('book')
        if book:
            book_id = get_dbt_book_id(book)
            if book_id is not None:
                # Canonicalize so stored names match the
                # tolerant lookup used by create.
                book = get_book_name_from_id(book_id)
            queryset = queryset.filter(book=book)

        return queryset

    def perform_create(self, serializer):
        """Create or update the position row (upsert).

        The serializer's create() runs the
        ``update_or_create``; it sets ``_row_created`` on itself
        so ``create`` can pick 201 vs 200.
        """
        serializer.save()
        logger.info(
            "User %s upserted reading position for '%s'",
            self.request.user.username,
            serializer.validated_data.get('book'),
        )

    def create(self, request, *args, **kwargs):
        """Upsert: 201 when a row is created, 200 on update."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(
            self.get_serializer(serializer.instance).data,
            status=(
                drf_status.HTTP_201_CREATED
                if serializer.created_row
                else drf_status.HTTP_200_OK
            ),
        )

    @extend_schema(
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'books': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': (
                            'Book names (max 100); resolved '
                            'with the same tolerant lookup as '
                            'POST create (aliases, case, typos).'
                        ),
                    },
                },
                'required': ['books'],
            },
        },
        responses={
            200: {
                'type': 'object',
                'additionalProperties': {
                    'type': 'object',
                    'nullable': True,
                    'properties': {
                        'chapter': {'type': 'integer'},
                        'verse': {'type': 'integer'},
                    },
                },
            },
        },
    )
    @action(detail=False, methods=['post'])
    def bulk(self, request):
        """Bulk-fetch positions for multiple books.

        ``{"books": ["John", ...]}`` → ``{"John": {"chapter": N,
        "verse": N} | null, ...}``. Book names go through the
        same canonical lookup as create; unresolvable names are
        rejected with 400.
        """
        books = request.data.get('books', [])
        if not isinstance(books, list):
            return _validation_error(
                'books must be an array.'
            )
        if len(books) > 100:
            return _validation_error(
                f'books may contain at most 100 entries. '
                f'Received {len(books)}.'
            )

        # Resolve each requested name to the canonical stored
        # form; the response map keys on the name the client
        # actually sent so existing callers keep working.
        resolved = []
        for raw in books:
            if not isinstance(raw, str) or not raw.strip():
                return _validation_error(
                    'books entries must be non-empty strings.'
                )
            book_id = get_dbt_book_id(raw)
            if book_id is None:
                return _validation_error(
                    f'Invalid book name: {raw}'
                )
            resolved.append((raw, get_book_name_from_id(book_id)))

        positions = ReadingPosition.objects.filter(
            user=request.user,
            book__in={canonical for _, canonical in resolved},
        )
        by_book = {pos.book: pos for pos in positions}

        result = {raw: None for raw, _ in resolved}
        for raw, canonical in resolved:
            pos = by_book.get(canonical)
            if pos is not None:
                result[raw] = {
                    'chapter': pos.chapter,
                    'verse': pos.verse,
                }

        logger.info(
            "User %s bulk-fetched %d reading positions",
            request.user.username,
            len(resolved),
        )
        return Response(result)
