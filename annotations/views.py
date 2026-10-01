import logging

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
from django.db.models import Count, Q, F
from django.shortcuts import get_object_or_404

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
    """
    serializer_class = NoteSerializer
    pagination_class = NotesPagination
    # permission_classes = [permissions.IsAuthenticated]

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

        if user.is_authenticated and instance.user != user:
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

        if (user.is_authenticated and
                instance.user != user):
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
                queryset = Note.objects.filter(public=public)
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
        requested verse. Returns {"count": N, "results": [...]};
        anonymous requests get an empty result set.
        """
        serializer = LinkedNotesRequestSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)

        user = request.user
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
            .distinct()
            .order_by('-created_at')
        )
        output = NoteSerializer(
            notes,
            many=True,
            context=self.get_serializer_context(),
        )
        return Response(
            {'count': notes.count(), 'results': output.data}
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
        tag_id = request.data.get('tag_id')
        updates = request.data.get('updates', [])

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
            note_id = update.get('note_id')
            position = update.get('position')
            
            if not note_id or position is None:
                return Response(
                    {
                        'error': 'validation_error',
                        'message': (
                            f'Update at index {idx} is missing '
                            f'note_id or position.'
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
            
            note_ids.append(note_id)
            position_map[note_id] = float(position)
        
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
                # Phase 1: Move to temporary negative positions
                for idx, note in enumerate(notes):
                    note.tag_position = -(idx + 1)
                
                Note.objects.bulk_update(notes, ['tag_position'])
                
                # Phase 2: Move to final positions
                for note in notes:
                    note.tag_position = position_map[note.id]
                
                Note.objects.bulk_update(notes, ['tag_position'])
            
            return Response(status=drf_status.HTTP_204_NO_CONTENT)
        
        except IntegrityError as e:
            # Database rejected due to unique constraint
            # violation (unique_tag_position_per_tag)
            error_msg = str(e).lower()
            
            if 'unique_tag_position_per_tag' in error_msg:
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
          Retrieve a single comment (flat).

      PATCH  /api/v1/notes/{note_pk}/comments/{pk}/
          Update a comment's content.

      DELETE /api/v1/notes/{note_pk}/comments/{pk}/
          Soft-delete: sets is_deleted=True and clears
          content. Thread structure is preserved.

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

    def _get_note(self):
        """Return the parent Note or raise 404."""
        return get_object_or_404(
            Note, pk=self.kwargs['note_pk']
        )

    def get_queryset(self):
        """
        Return all comments for the note in one query.
        select_related('user') prevents per-comment author
        lookups during serialization.
        """
        note_pk = self.kwargs['note_pk']
        return (
            Comment.objects
            .filter(note_id=note_pk)
            .select_related('user')
            .prefetch_related('images')
            .order_by('timestamp')
        )

    def list(self, request, *args, **kwargs):
        """Return comments as a nested tree (no N+1)."""
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
        # Writes always require authentication.
        if self.action in ('list', 'retrieve'):
            return [AllowAny()]
        return [IsAuthenticated()]

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
        return [IsAuthenticated()]

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

    Only the uploader or the parent owner may delete.
    Hard-delete: DB row removed and GCS object deleted
    (best-effort).
    """

    permission_classes = [IsAuthenticated]

    def delete(self, request, image_pk):
        image = get_object_or_404(Image, pk=image_pk)
        user = request.user

        is_uploader = image.uploaded_by == user
        is_note_owner = (
            image.note is not None
            and image.note.user == user
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
    permission_classes = [IsAuthenticated]

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
