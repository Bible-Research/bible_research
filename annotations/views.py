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
from django.contrib.auth import get_user_model
from django.db import models, transaction, IntegrityError
from django.db.models import Count, Q, F, Min, Prefetch
from django.shortcuts import get_object_or_404

from bible.models import Verse
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
    LinkedNotesRequestSerializer,
    BulkNoteCreateSerializer,
    CommentSerializer,
    ImageSerializer,
    ReadingPositionSerializer,
    build_comment_tree,
)
from .services.image_storage import upload_original, delete_original

User = get_user_model()
logger = logging.getLogger(__name__)


NOTE_IDS_CAP = 200

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
    """
    lowest = Note.objects.filter(tag_id=tag_id).aggregate(
        lowest=Min('tag_position')
    )['lowest']
    base = min(0, lowest) if lowest is not None else 0
    if extra_floor is not None:
        base = min(base, extra_floor)
    for idx, note in enumerate(notes):
        note.tag_position = base - idx - 1


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

    Users can only see and manage their own tags.
    """
    serializer_class = TagSerializer

    def get_queryset(self):
        """
        Returns the queryset of tags that the current user
        has access to.
        Authenticated users see their own tags.
        Unauthenticated users see the guest user's tags.
        """
        user = self.request.user
        logger.info(
            f"TagViewSet.get_queryset called for user: "
            f"{user.username if user.is_authenticated else 'Anonymous'}"
        )

        # For authenticated users, return their own tags
        if user.is_authenticated:
            queryset = Tag.objects.filter(
                user=user
            ).order_by('name')
            logger.debug(
                f"Returning {queryset.count()} tags "
                f"for user {user.username}"
            )
            return queryset

        # For unauthenticated users, return guest user's tags
        try:
            guest_user = User.objects.get(username='guest')
            queryset = Tag.objects.filter(
                user=guest_user
            ).order_by('name')
            logger.debug(
                f"Returning {queryset.count()} tags "
                f"for guest user"
            )
            return queryset
        except User.DoesNotExist:
            logger.warning(
                "Guest user does not exist, "
                "returning empty queryset"
            )
            return Tag.objects.none()

    def perform_create(self, serializer):
        """
        Assigns the current authenticated user as the creator
        of the tag when a new tag is created.
        """
        user = self.request.user
        tag_name = serializer.validated_data.get('name', 'N/A')

        if user.is_authenticated:
            logger.info(
                f"Creating tag '{tag_name}' "
                f"for user {user.username}"
            )
            serializer.save(user=user)
        else:
            # For unauthenticated users, use guest user
            try:
                guest_user = User.objects.get(username='guest')
                logger.info(
                    f"Creating tag '{tag_name}' "
                    f"for guest user"
                )
                serializer.save(user=guest_user)
            except User.DoesNotExist:
                logger.error(
                    f"Guest user does not exist, "
                    f"creating tag '{tag_name}' "
                    f"without user assignment"
                )
                serializer.save()

    def perform_update(self, serializer):
        """
        Ensures that a user can only update their own tags.
        """
        instance = serializer.instance
        user = self.request.user
        logger.info(
            f"Updating tag '{instance.name}' (ID: {instance.id}) "
            f"by user: "
            f"{user.username if user.is_authenticated else 'Anonymous'}"
        )
        serializer.save()

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
        note_count = Note.objects.filter(
            tag_id__in=self._subtree_tag_ids(instance)
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
                        f'?delete_notes=true to delete the tag '
                        f'together with its notes.'
                    ),
                    'note_count': note_count,
                },
                status=drf_status.HTTP_409_CONFLICT,
            )
        self.perform_destroy(instance)
        return Response(status=drf_status.HTTP_204_NO_CONTENT)

    @staticmethod
    def _subtree_tag_ids(tag):
        """
        Return the ids of *tag* plus all of its descendant tags
        via parent_tag chains, so the blast radius of a cascade
        delete can be measured before it happens.
        """
        tag_ids = {tag.id}
        frontier = [tag.id]
        while frontier:
            children = set(
                Tag.objects.filter(
                    parent_tag_id__in=frontier
                ).values_list('id', flat=True)
            )
            frontier = list(children - tag_ids)
            tag_ids |= children
        return tag_ids

    def perform_destroy(self, instance):
        """
        Ensures that a user can only delete their own tags.
        """
        user = self.request.user
        logger.info(
            f"Deleting tag '{instance.name}' (ID: {instance.id}) "
            f"by user: "
            f"{user.username if user.is_authenticated else 'Anonymous'}"
        )
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
        # other actions always require authentication —
        # explicitly, not just via DeviceAndCountryMiddleware
        # auto-provisioning (issue #47) — and are denied for
        # the shared read-only 'guest' account.
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
                'text',
                'N/A'
            )[:50]
            logger.info(
                f"Creating note for user {user.username}: "
                f"{note_text}..."
            )
            serializer.save(user=user)
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
        logger.debug(
            f"Note ID: {instance.id} "
            f"successfully updated"
        )
        serializer.save()

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
        logger.debug(
            f"Note ID: {instance.id} "
            f"successfully deleted"
        )
        instance.delete()

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
            return queryset.order_by(
                F('tag_position').asc(nulls_last=True),
                '-created_at'
            )

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
            return queryset.order_by(
                F('tag_position').asc(nulls_last=True),
                '-created_at'
            )

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
          tag ID
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

        if tag_id:
            queryset = queryset.filter(tag_id=tag_id)
            logger.debug(f"Filtering by tag_id: {tag_id}")

        if note_pk:
            queryset = queryset.filter(id=note_pk)
            logger.debug(f"Filtering by note_pk: {note_pk}")

        # Apply ordering based on parameter
        final_queryset = self._apply_ordering(
            queryset, ordering_param
        )
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
        # Non-dict JSON bodies (array/scalar) get no fields
        # and fall through to the usual 400s.
        data = (
            request.data if isinstance(request.data, dict) else {}
        )
        tag_id = data.get('tag_id')
        updates = data.get('updates', [])

        if not tag_id:
            return Response(
                {
                    'error': 'validation_error',
                    'message': 'tag_id is required.',
                },
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        if not isinstance(updates, list) or not updates:
            return Response(
                {
                    'error': 'validation_error',
                    'message': 'updates array is required.',
                },
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        # Enforce maximum of 100 notes per reorder request
        if len(updates) > 100:
            return Response(
                {
                    'error': 'validation_error',
                    'message': (
                        f'Cannot reorder more than 100 notes '
                        f'at once. Received {len(updates)} notes.'
                    ),
                },
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        # Validate update structure
        note_ids = []
        position_map = {}

        for idx, update in enumerate(updates):
            if not isinstance(update, dict):
                return Response(
                    {
                        'error': 'validation_error',
                        'message': (
                            f'Update at index {idx} must be an '
                            f'object with note_id and position.'
                        ),
                    },
                    status=drf_status.HTTP_400_BAD_REQUEST,
                )

            note_id = update.get('note_id')
            position = update.get('position')

            if not isinstance(note_id, str) or not note_id:
                return Response(
                    {
                        'error': 'validation_error',
                        'message': (
                            f'Update at index {idx} needs a '
                            f'non-empty string note_id.'
                        ),
                    },
                    status=drf_status.HTTP_400_BAD_REQUEST,
                )

            if position is None:
                return Response(
                    {
                        'error': 'validation_error',
                        'message': (
                            f'Update at index {idx} is missing '
                            f'position.'
                        ),
                    },
                    status=drf_status.HTTP_400_BAD_REQUEST,
                )

            # Check for duplicate note_ids in request
            if note_id in position_map:
                return Response(
                    {
                        'error': 'validation_error',
                        'message': (
                            f'Duplicate note_id in request: '
                            f'{note_id}'
                        ),
                    },
                    status=drf_status.HTTP_400_BAD_REQUEST,
                )

            try:
                position_map[note_id] = float(position)
            except (TypeError, ValueError, OverflowError):
                return Response(
                    {
                        'error': 'validation_error',
                        'message': (
                            f'Update at index {idx} has a '
                            f'non-numeric position.'
                        ),
                    },
                    status=drf_status.HTTP_400_BAD_REQUEST,
                )
            # 'nan'/'inf' strings and JSON NaN/Infinity literals
            # survive float(); Postgres happily persists NaN and
            # SQLite turns it into a misleading IntegrityError.
            if not math.isfinite(position_map[note_id]):
                return Response(
                    {
                        'error': 'validation_error',
                        'message': (
                            f'Update at index {idx} has a '
                            f'non-finite position.'
                        ),
                    },
                    status=drf_status.HTTP_400_BAD_REQUEST,
                )

            note_ids.append(note_id)

        # Check for duplicate positions in request
        positions = list(position_map.values())
        if len(positions) != len(set(positions)):
            return Response(
                {
                    'error': 'validation_error',
                    'message': 'Duplicate positions in request.',
                },
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

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
                    return Response(
                        {
                            'error': 'validation_error',
                            'message': (
                                f'Notes not found or do not '
                                f'belong to tag {tag_id}: '
                                f'{missing}'
                            ),
                        },
                        status=drf_status.HTTP_400_BAD_REQUEST,
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
                return Response(
                    {
                        'error': 'position_conflict',
                        'message': (
                            'Position conflict: one or more '
                            'positions are already occupied by '
                            'other notes in this tag. Please '
                            'refresh and try again.'
                        ),
                        'constraint': 'unique_tag_position_per_tag',
                    },
                    status=drf_status.HTTP_409_CONFLICT,
                )
            else:
                # Generic integrity error
                logger.error(
                    f'Unexpected IntegrityError in reorder: {e}'
                )
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
            return Response(
                {
                    'error': 'validation_error',
                    'message': 'tag_id query parameter is required.',
                },
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        notes = (
            get_accessible_notes_qs(request.user)
            .filter(tag_id=tag_id)
            # Avoid loading heavy note_text rows at scale; the
            # verse prefetch runs as a separate query and is
            # unaffected by .only().
            .only('id', 'tag_position', 'created_at')
            .order_by(
                F('tag_position').asc(nulls_last=True),
                '-created_at',
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
        def _err(message):
            return Response(
                {
                    'error': 'validation_error',
                    'message': message,
                },
                status=drf_status.HTTP_400_BAD_REQUEST,
            )

        # Non-dict JSON bodies (array/scalar) get no fields
        # and fall through to the usual 400s.
        data = (
            request.data if isinstance(request.data, dict) else {}
        )
        tag_id = data.get('tag_id')
        note_id = data.get('note_id')
        target_note_id = data.get('target_note_id')
        placement = data.get('placement')

        if not tag_id:
            return _err('tag_id is required.')
        if not note_id:
            return _err('note_id is required.')
        if not target_note_id:
            return _err('target_note_id is required.')
        # Unhashable/non-string ids would raise TypeError on the
        # by_id lookup below instead of failing validation.
        if not isinstance(note_id, str):
            return _err('note_id must be a string.')
        if not isinstance(target_note_id, str):
            return _err('target_note_id must be a string.')
        if placement not in ('after', 'before'):
            return _err("placement must be 'after' or 'before'.")
        if note_id == target_note_id:
            return _err('note_id and target_note_id must differ.')
        if not request.user.is_authenticated:
            raise PermissionDenied('Authentication required.')

        user = request.user

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
                    Note.objects.select_for_update()
                    .filter(user=user, tag_id=tag_id)
                    .order_by(
                        F('tag_position').asc(nulls_last=True),
                        '-created_at',
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
                return Response(
                    {
                        'error': 'position_conflict',
                        'message': (
                            'Position conflict: the computed '
                            'position is already occupied by '
                            'another note in this tag. Please '
                            'refresh and try again.'
                        ),
                        'constraint': 'unique_tag_position_per_tag',
                    },
                    status=drf_status.HTTP_409_CONFLICT,
                )
            else:
                logger.error(
                    f'Unexpected IntegrityError in move: {e}'
                )
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
        # for the shared read-only 'guest' account.
        if self.action in ('list', 'retrieve'):
            return [AllowAny()]
        return [IsAuthenticated(), GuestReadOnly()]

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
        return (
            Comment.objects
            .filter(
                note_id=note_pk,
                note__in=get_accessible_notes_qs(
                    self.request.user
                ),
            )
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
        """Allow only the comment author to update."""
        instance = serializer.instance
        user = self.request.user
        if instance.user != user:
            raise PermissionDenied(
                "You do not have permission "
                "to edit this comment."
            )
        logger.info(
            f"User {user.username} updating "
            f"comment {instance.id}"
        )
        serializer.save()

    def perform_destroy(self, instance):
        """
        Soft-delete: mark as deleted and redact content.
        The row is retained so child replies remain intact.
        """
        user = self.request.user
        if instance.user != user:
            raise PermissionDenied(
                "You do not have permission "
                "to delete this comment."
            )
        logger.info(
            f"User {user.username} soft-deleting "
            f"comment {instance.id}"
        )
        instance.is_deleted = True
        instance.content = ''
        instance.save(update_fields=['is_deleted', 'content'])


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
    """

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


class NoteImageViewSet(viewsets.GenericViewSet):
    """
    Image attachments scoped to a Note.

      GET  /api/v1/notes/{note_pk}/images/
      POST /api/v1/notes/{note_pk}/images/

    Upload: multipart/form-data with a single 'file' field.
    Requester must be the note's owner.
    Read: same accessibility scope as get_accessible_notes_qs.
    """

    serializer_class = ImageSerializer
    parser_classes = [MultiPartParser]

    def get_permissions(self):
        # Reads are open: visibility is enforced by
        # get_accessible_notes_qs (anon may see public notes).
        # Writes always require authentication and are denied
        # for the shared read-only 'guest' account.
        if self.action in ('list', 'retrieve'):
            return [AllowAny()]
        return [IsAuthenticated(), GuestReadOnly()]

    def _get_note(self):
        return get_object_or_404(
            Note, pk=self.kwargs['note_pk']
        )

    def list(self, request, note_pk=None):
        note = self._get_note()
        accessible = get_accessible_notes_qs(request.user)
        if not accessible.filter(pk=note.pk).exists():
            raise PermissionDenied(
                "You do not have access to this note."
            )
        images = Image.objects.filter(note=note)
        serializer = ImageSerializer(
            images, many=True, context={'request': request}
        )
        return Response(serializer.data)

    def create(self, request, note_pk=None):
        note = self._get_note()
        user = request.user
        if not user.is_authenticated or note.user != user:
            raise PermissionDenied(
                "Only the note's owner may upload images."
            )
        file_obj = request.FILES.get('file')
        if not file_obj:
            raise ValidationError({'file': 'No file provided.'})
        max_images = getattr(
            settings, 'IMAGE_MAX_PER_NOTE', 10
        )
        # Cheap pre-check before the expensive GCS round-trip.
        # The authoritative check happens under a row lock below.
        if Image.objects.filter(
            note=note
        ).count() >= max_images:
            raise ValidationError(
                {'file': (
                    f'Maximum {max_images} images per note.'
                )}
            )

        image_id = generate_image_id()
        gs_uri, size_bytes, content_type = upload_original(
            image_id, file_obj
        )
        try:
            with transaction.atomic():
                # Lock the parent row so concurrent uploads cannot
                # both pass the cap check.
                locked_note = Note.objects.select_for_update().get(
                    pk=note.pk
                )
                if Image.objects.filter(
                    note=locked_note
                ).count() >= max_images:
                    raise ValidationError(
                        {'file': (
                            f'Maximum {max_images} images per note.'
                        )}
                    )
                image = Image.objects.create(
                    id=image_id,
                    note=locked_note,
                    uploaded_by=user,
                    storage_url=gs_uri,
                    size_bytes=size_bytes,
                    content_type=content_type,
                )
        except Exception:
            delete_original(image_id, gs_uri)
            raise
        logger.info(
            "User %s uploaded image %s to note %s",
            user.username, image.id, note.id,
        )
        serializer = ImageSerializer(
            image, context={'request': request}
        )
        return Response(serializer.data, status=201)


class CommentImageViewSet(viewsets.GenericViewSet):
    """
    Image attachments scoped to a Comment.

      GET  /api/v1/notes/{note_pk}/comments/{comment_pk}/images/
      POST /api/v1/notes/{note_pk}/comments/{comment_pk}/images/

    Upload: multipart/form-data with a single 'file' field.
    Requester must be the comment's author.
    """

    serializer_class = ImageSerializer
    parser_classes = [MultiPartParser]

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [AllowAny()]
        return [IsAuthenticated(), GuestReadOnly()]

    def _get_comment(self):
        return get_object_or_404(
            Comment,
            pk=self.kwargs['comment_pk'],
            note_id=self.kwargs['note_pk'],
        )

    def list(self, request, note_pk=None, comment_pk=None):
        comment = self._get_comment()
        accessible = get_accessible_notes_qs(request.user)
        if not accessible.filter(
            pk=comment.note_id
        ).exists():
            raise PermissionDenied(
                "You do not have access to this comment."
            )
        images = Image.objects.filter(comment=comment)
        serializer = ImageSerializer(
            images, many=True, context={'request': request}
        )
        return Response(serializer.data)

    def create(
        self, request, note_pk=None, comment_pk=None
    ):
        comment = self._get_comment()
        user = request.user
        if not user.is_authenticated or comment.user != user:
            raise PermissionDenied(
                "Only the comment's author may upload images."
            )
        file_obj = request.FILES.get('file')
        if not file_obj:
            raise ValidationError({'file': 'No file provided.'})
        max_images = getattr(
            settings, 'IMAGE_MAX_PER_COMMENT', 5
        )
        if Image.objects.filter(
            comment=comment
        ).count() >= max_images:
            raise ValidationError(
                {'file': (
                    f'Maximum {max_images} images per comment.'
                )}
            )

        image_id = generate_image_id()
        gs_uri, size_bytes, content_type = upload_original(
            image_id, file_obj
        )
        try:
            with transaction.atomic():
                locked_comment = (
                    Comment.objects.select_for_update().get(
                        pk=comment.pk
                    )
                )
                if Image.objects.filter(
                    comment=locked_comment
                ).count() >= max_images:
                    raise ValidationError(
                        {'file': (
                            f'Maximum {max_images} images '
                            f'per comment.'
                        )}
                    )
                image = Image.objects.create(
                    id=image_id,
                    comment=locked_comment,
                    uploaded_by=user,
                    storage_url=gs_uri,
                    size_bytes=size_bytes,
                    content_type=content_type,
                )
        except Exception:
            delete_original(image_id, gs_uri)
            raise
        logger.info(
            "User %s uploaded image %s to comment %s",
            user.username, image.id, comment.id,
        )
        serializer = ImageSerializer(
            image, context={'request': request}
        )
        return Response(serializer.data, status=201)


class ImageDestroyView(APIView):
    """
    DELETE /api/v1/images/{image_pk}/

    Only the uploader, the comment author, or the owner of the
    parent note may delete. For comment-attached images the
    parent note is reached through image.comment.note.
    Hard-delete: DB row removed and GCS object deleted
    (best-effort).
    """

    permission_classes = [IsAuthenticated, GuestReadOnly]

    def delete(self, request, image_pk):
        image = get_object_or_404(Image, pk=image_pk)
        user = request.user

        # Comment-attached images have note=NULL (XOR FK), so
        # resolve the parent note through the comment.
        parent_note = image.note
        if parent_note is None and image.comment is not None:
            parent_note = image.comment.note

        is_uploader = image.uploaded_by == user
        is_note_owner = (
            parent_note is not None
            and parent_note.user == user
        )
        is_comment_author = (
            image.comment is not None
            and image.comment.user == user
        )

        if not (is_uploader or is_note_owner or is_comment_author):
            raise PermissionDenied(
                "You do not have permission to delete "
                "this image."
            )

        storage_url = image.storage_url
        image_id = image.id
        image.delete()
        delete_original(image_id, storage_url)
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
    permission_classes = [IsAuthenticated, GuestReadOnly]

    def get_queryset(self):
        """Filter positions to current user only."""
        user = self.request.user
        queryset = ReadingPosition.objects.filter(user=user)

        book = self.request.query_params.get('book')
        if book:
            queryset = queryset.filter(book=book)

        return queryset

    def create(self, request, *args, **kwargs):
        """Upsert: create or update reading position."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        position = serializer.save()

        return Response(
            self.get_serializer(position).data,
            status=drf_status.HTTP_200_OK
        )

    @action(detail=False, methods=['post'])
    def bulk(self, request):
        """Bulk fetch positions for multiple books."""
        books = request.data.get('books', [])
        if not isinstance(books, list):
            return Response(
                {'error': 'books must be an array'},
                status=drf_status.HTTP_400_BAD_REQUEST
            )

        positions = ReadingPosition.objects.filter(
            user=request.user,
            book__in=books
        )

        result = {book: None for book in books}
        for pos in positions:
            result[pos.book] = {
                'chapter': pos.chapter,
                'verse': pos.verse
            }

        return Response(result)
