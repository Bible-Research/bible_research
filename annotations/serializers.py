import logging
import math

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from rest_framework import serializers
from bible.models import Verse
from bible.services.providers import provider_for
from bible.utils.bible_books import (
    get_book_name_from_id,
    get_dbt_book_id,
)
from bible.utils.provider_errors import (
    capture_internal_exception,
    capture_provider_exception,
    provider_error_fields,
)
from .models import (
    Note,
    NoteVerse,
    Tag,
    Comment,
    Image,
    ReadingPosition
)
User = get_user_model()

logger = logging.getLogger(__name__)


def subtree_tag_ids(tag, user=None):
    """Return the ids of *tag* plus all of its descendant tags
    via parent_tag chains.

    Pass *user* to restrict the descent to a single owner's
    subtree — cross-owner parent links (only creatable via
    admin/shell, never the API) are then treated as a boundary
    instead of being swallowed into the result.
    """
    tag_ids = {tag.id}
    frontier = [tag.id]
    owner_q = Q()
    if user is not None:
        owner_q = Q(user=user)
    while frontier:
        children = set(
            Tag.objects.filter(
                owner_q,
                parent_tag_id__in=frontier,
            ).values_list('id', flat=True)
        )
        frontier = list(children - tag_ids)
        tag_ids |= children
    return tag_ids


def dedupe_verse_refs(verse_refs):
    """Drop duplicate ``{book, chapter, verse}`` refs — book is
    matched case-insensitively — preserving first-seen order.

    Duplicate refs would otherwise write duplicate NoteVerse
    links (guarded by the ``unique_note_verse_link`` constraint
    once its migration lands).
    """
    seen = set()
    deduped = []
    for ref in verse_refs:
        key = (
            ref['book'].lower(),
            ref['chapter'],
            ref['verse'],
        )
        if key not in seen:
            seen.add(key)
            deduped.append(ref)
    return deduped


def resolve_verse_refs(verse_refs):
    """Batch-fetch every Verse referenced by *verse_refs*.

    Returns a dict keyed by ``(book.lower(), chapter, verse)``.
    Raises ValidationError listing any refs that do not resolve.
    Book lookup is case-insensitive; when legacy duplicate Verse
    rows exist the last fetch wins rather than raising
    MultipleObjectsReturned.
    """
    unique_keys = set()
    for ref in verse_refs:
        unique_keys.add((
            ref['book'].lower(),
            ref['chapter'],
            ref['verse'],
        ))
    if not unique_keys:
        return {}

    query = Q()
    for book, chapter, verse in unique_keys:
        query |= Q(
            book__iexact=book,
            chapter=chapter,
            verse=verse,
        )
    fetched = Verse.objects.filter(query)
    verse_map = {
        (v.book.lower(), v.chapter, v.verse): v
        for v in fetched
    }
    missing = unique_keys - set(verse_map.keys())
    if missing:
        missing_str = ', '.join(
            f"{b} {c}:{v}"
            for b, c, v in sorted(missing)
        )
        raise serializers.ValidationError(
            f"Verse not found: {missing_str}. "
            "Please ensure the verses exist "
            "in your database."
        )
    return verse_map


class TagSerializer(serializers.ModelSerializer):
    """Serializes Tag model data for API input and output.
    Includes the 'parent_tag' as its primary key for relationships.
    """
    parent_tag = serializers.PrimaryKeyRelatedField(
        # Safe default: when __init__ cannot scope the queryset
        # to the request user it stays empty — never 'all tags'.
        queryset=Tag.objects.none(),
        allow_null=True,
        required=False,
        default=None
    )
    user = serializers.HiddenField(
        default=serializers.CurrentUserDefault()
    )
    # CharField trims surrounding whitespace by default
    # (trim_whitespace=True), so '  Name  ' stores as 'Name'.
    name = serializers.CharField(max_length=100)
    # Annotated by TagViewSet.get_queryset (Count over 'notes');
    # read-only — the value never comes from request data. When
    # a Tag is serialized without the annotation (e.g. nested
    # inside a note payload), the key is simply omitted.
    note_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Tag
        fields = [
            'id', 'user', 'name', 'parent_tag', 'public',
            'note_count', 'created_at', 'updated_at',
        ]
        # 'read_only_fields' makes sure 'id', 'created_at', 'updated_at' are
        # automatically handled by Django and not expected in user input.
        read_only_fields = ['id', 'created_at', 'updated_at']
        extra_kwargs = {
            'parent_tag': {'default': None}
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get('request')

        # Filter parent_tag queryset based on user authentication
        if request and hasattr(request, 'user'):
            if request.user.is_authenticated:
                # For authenticated users, only show their own tags
                self.fields['parent_tag'].queryset = Tag.objects.filter(
                    user=request.user
                )
            else:
                # Anonymous callers can only read; writes are
                # rejected by the view's permissions before
                # validation, so the queryset stays empty.
                self.fields['parent_tag'].queryset = (
                    Tag.objects.none()
                )

    def validate_parent_tag(self, value):
        """Reject cycles: a tag cannot be its own parent, nor be
        moved underneath one of its own descendants.

        The scoped queryset already rejects foreign tags; this
        catches the self/descendant cases that pass the lookup —
        PATCH {parent_tag: <self>} used to silently corrupt the
        hierarchy.
        """
        if value is None or self.instance is None:
            return value
        if value.id == self.instance.id:
            raise serializers.ValidationError(
                "A tag cannot be its own parent."
            )
        if value.id in subtree_tag_ids(
            self.instance, user=self.instance.user
        ):
            raise serializers.ValidationError(
                "A tag cannot be moved under its own descendant."
            )
        return value

    def validate(self, attrs):
        """Reject duplicate top-level tag names per user.

        ``unique_together (user, name, parent_tag)`` cannot cover
        NULL parents (UNIQUE treats NULLs as distinct), so the
        application enforces the top-level (user, name) rule ahead
        of the model's conditional ``UniqueConstraint``
        (``unique_top_level_tag_name_per_user``).
        """
        instance = getattr(self, 'instance', None)
        name = attrs.get(
            'name', instance.name if instance else None
        )
        parent = attrs.get(
            'parent_tag',
            instance.parent_tag if instance else None,
        )
        user = attrs.get(
            'user', instance.user if instance else None
        )
        if parent is None and name is not None:
            dupes = Tag.objects.filter(
                user=user,
                name=name,
                parent_tag__isnull=True,
            )
            if instance is not None:
                dupes = dupes.exclude(pk=instance.pk)
            if dupes.exists():
                raise serializers.ValidationError({
                    'name': (
                        'You already have a top-level tag '
                        'with this name.'
                    )
                })
        return attrs


class NoteVerseReferenceSerializer(serializers.Serializer):
    """Serializer to accept verse details for linking to a Note.
    This is for WRITE operations (e.g., when creating a Note).
    """
    book = serializers.CharField(
        max_length=50,
        help_text="The book name (e.g., 'John')."
    )
    chapter = serializers.IntegerField(
        help_text="The chapter number (e.g., 3)."
    )
    verse = serializers.IntegerField(
        help_text="The verse number (e.g., 16)."
    )


class LinkedNotesRequestSerializer(serializers.Serializer):
    """Validates POST /api/v1/notes/linked/ request bodies."""
    verse_references = serializers.ListField(
        child=NoteVerseReferenceSerializer(),
        min_length=1,
        max_length=500,
        help_text=(
            "Verses to match against; a note is linked when "
            "it references at least one of them."
        ),
    )


class NoteSerializer(serializers.ModelSerializer):
    """Serializes Note model data.
    - For POST/PUT: Accepts tag UUID and a list of verse
      references (book, chapter, verse).
    - For GET: Displays full tag object and full verse objects.
    """
    tag = serializers.PrimaryKeyRelatedField(
        queryset=Tag.objects.all(),
        allow_null=True,
        required=False,
        help_text="UUID of the primary Tag associated with this note."
    )
    user = serializers.HiddenField(
        default=serializers.CurrentUserDefault()
    )
    verse_references = NoteVerseReferenceSerializer(
        many=True,
        write_only=True,
        required=False,
        help_text=(
            "List of objects, each with 'book', 'chapter', and 'verse' "
            "to link to the note."
        )
    )
    is_owner = serializers.SerializerMethodField(
        read_only=True,
        help_text=(
            "True only when the request user is authenticated and "
            "owns this note. Always false for anonymous readers."
        ),
    )
    # Declared so an omitted 'public' key is absent from
    # validated_data — create() then inherits the tag's flag.
    public = serializers.BooleanField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get('request')

        # Filter tag queryset based on user authentication
        if request and hasattr(request, 'user'):
            if request.user.is_authenticated:
                # For authenticated users, only show their own tags
                self.fields['tag'].queryset = Tag.objects.filter(
                    user=request.user
                )
            else:
                # Anonymous callers can only read; writes are
                # rejected by the view's permissions before
                # validation, so the queryset stays empty.
                self.fields['tag'].queryset = Tag.objects.none()

    class Meta:
        model = Note
        fields = [
            'id', 'user', 'note_text', 'public', 'created_at',
            'updated_at', 'tag', 'tag_position', 'verse_references',
            'is_owner'
        ]
        # tag_position is managed exclusively by reorder/move —
        # a client-supplied value on write can collide with the
        # unique_tag_position_per_tag constraint and 500.
        read_only_fields = [
            'id', 'created_at', 'updated_at', 'tag_position',
        ]

    def validate_verse_references(self, value):
        """Silently dedupe repeated verse references — linking a
        note to the same verse twice is meaningless and would
        violate the NoteVerse uniqueness constraint."""
        return dedupe_verse_refs(value)

    def get_is_owner(self, obj):
        """True only when the request user owns this note."""
        request = self.context.get('request')
        return bool(
            request
            and request.user.is_authenticated
            and obj.user_id == request.user.id
        )

    def create(self, validated_data):
        """
        Overrides the default create method to handle nested verse_references.
        """
        # Pop out the 'verse_references' as it's not a direct field on the Note
        verse_references_data = validated_data.pop('verse_references', [])
        # Resolve all references before any write so a missing
        # verse fails without leaving an orphan Note row behind.
        verse_map = resolve_verse_refs(verse_references_data)
        # An omitted 'public' flag inherits the tag's publicity —
        # an explicit false is honored (per-note opt-out). The
        # default applies at creation only; update() never
        # re-derives it.
        if 'public' not in validated_data:
            tag = validated_data.get('tag')
            validated_data['public'] = bool(tag and tag.public)
        # Create the Note row and its NoteVerse links in one
        # transaction so a failed write rolls the Note back.
        with transaction.atomic():
            # Create the Note instance with the remaining validated data
            note = Note.objects.create(**validated_data)

            # Handle the many-to-many relationship via NoteVerse
            if verse_references_data:
                NoteVerse.objects.bulk_create([
                    NoteVerse(
                        note=note,
                        verse=verse_map[(
                            ref['book'].lower(),
                            ref['chapter'],
                            ref['verse'],
                        )],
                    )
                    for ref in verse_references_data
                ])

        return note

    def update(self, instance, validated_data):
        """
        Overrides the default update method so a note's verse links
        can be replaced via 'verse_references'. When the field is
        present in the payload, every existing NoteVerse link is
        replaced by the given references; omitting the field leaves
        the links untouched.
        """
        verse_references_data = validated_data.pop(
            'verse_references', None
        )

        # Resolve all references before any write so a missing
        # verse fails without touching the note or its links.
        verse_map = None
        if verse_references_data is not None:
            verse_map = resolve_verse_refs(verse_references_data)

        note = super().update(instance, validated_data)

        if verse_map is not None:
            with transaction.atomic():
                note.verses.clear()
                NoteVerse.objects.bulk_create([
                    NoteVerse(
                        note=note,
                        verse=verse_map[(
                            ref['book'].lower(),
                            ref['chapter'],
                            ref['verse'],
                        )],
                    )
                    for ref in verse_references_data
                ])

        return note

    def _chapter_payload(self, fileset_id, dbt_book_id, chapter):
        """Fetch (or reuse) the chapter's provider payload.

        Results are memoized on the shared serializer context
        keyed by ``(fileset_id, book_id, chapter)`` — DRF
        serializes each note instance through a fresh
        ``to_representation`` call, so the batching cache has to
        live in ``context`` where a ``many=True`` list can share
        it. Failures are cached too: every note on a missing
        chapter degrades with the same error fields.
        """
        cache = self.context.setdefault('chapter_cache', {})
        key = (fileset_id, dbt_book_id, chapter)
        if key not in cache:
            try:
                cache[key] = provider_for(fileset_id).get_chapter(
                    dbt_book_id, chapter
                )
            except Exception as exc:  # noqa: BLE001
                cache[key] = exc
        result = cache[key]
        if isinstance(result, Exception):
            raise result
        return result

    def to_representation(self, instance):
        """
        Overrides the default representation for GET requests to
        include nested tag and verse data with content from
        Bible provider, plus section headings.

        Set 'include_verse_text' to False in the serializer
        context to skip the blocking upstream provider call;
        verses then carry only book/chapter/verse coordinates
        (empty 'text') and 'headings' is empty.
        """
        representation = super().to_representation(instance)
        # Sort in Python so a prefetch_related('verses') cache is
        # reused; .order_by() on the related manager would issue
        # a fresh query per note.
        verses = sorted(
            instance.verses.all(), key=lambda v: v.verse
        )
        if not verses:
            # Emit the same keys as the verse-linked path so the
            # response shape never depends on verse linkage
            # (clients used to have to sniff for absent
            # ``verses``/``headings`` and a raw ``tag`` PK).
            representation['verses'] = []
            representation['headings'] = []
            if instance.tag:
                representation['tag'] = TagSerializer(
                    instance.tag
                ).data
            return representation

        dbt_book_id = verses[0].dbt_book_id
        chapter = verses[0].chapter
        book_name = verses[0].book

        verse_numbers = [v.verse for v in verses]

        # Get fileset_id from context, default to DBT
        fileset_id = self.context.get('fileset_id', 'ENGESV')

        verses_with_text = []
        headings = []

        include_verse_text = self.context.get(
            'include_verse_text', True
        )

        try:
            if not include_verse_text:
                # Coordinates only — e.g. POST
                # /api/v1/notes/linked/ does not display
                # verse text, so skip the provider call.
                for verse in verses:
                    verses_with_text.append({
                        'book': book_name,
                        'chapter': chapter,
                        'verse': verse.verse,
                        'text': ''
                    })
            else:
                # Provider dispatch lives in the facade; the
                # whole chapter is fetched once per
                # (fileset, book, chapter) and memoized on the
                # serializer context, so a page of notes on the
                # same passage costs one upstream call.
                parsed = self._chapter_payload(
                    fileset_id, dbt_book_id, chapter
                )
                for verse in verses:
                    matching_verse = next(
                        (
                            v for v in parsed['verses']
                            if v['verse_start'] == verse.verse
                        ),
                        None
                    )
                    text = (
                        matching_verse['verse_text']
                        if matching_verse else ''
                    )
                    verse_data = {
                        'book': book_name,
                        'chapter': chapter,
                        'verse': verse.verse,
                        'text': text
                    }
                    verses_with_text.append(verse_data)

                # Filter headings to only those relevant to
                # this note's verses (SWORD modules carry no
                # headings metadata — ``headings`` is None).
                headings = [
                    {
                        'before_verse': h.get('before_verse'),
                        'text': h.get('text', '')
                    }
                    for h in (parsed.get('headings') or [])
                    if h.get('before_verse') in verse_numbers
                ]
                # Relay provider-mandated metadata — e.g.
                # API.Bible's FUMS block, which the frontend
                # must report per license terms.
                if parsed.get('meta'):
                    representation['meta'] = parsed['meta']
        except Exception as e:
            # Catch-all also covers response-shaping bugs; they
            # surface as provider_error rather than crashing GETs.
            logger.exception(f"Error fetching verses/headings: {e}")
            # Surface the provider failure to clients while
            # keeping the verse references for display.
            fields = provider_error_fields(e)
            # Report upstream outages to Sentry (no-op for
            # not_found and when Sentry is not configured).
            capture_provider_exception(e, fields['error_code'])
            representation.update(fields)
            for verse in verses:
                verses_with_text.append({
                    'book': book_name,
                    'chapter': chapter,
                    'verse': verse.verse,
                    'text': ''
                })

        representation['verses'] = verses_with_text
        representation['headings'] = headings

        # Include full tag object if a tag exists
        if instance.tag:
            representation['tag'] = TagSerializer(instance.tag).data

        return representation


class BulkNoteItemSerializer(serializers.Serializer):
    """Represents a single note in a bulk creation request."""
    note_text = serializers.CharField(
        required=False,
        allow_blank=True,
        default='',
    )
    # No default: an omitted flag stays absent from
    # validated_data so create() can inherit tag.public.
    public = serializers.BooleanField(
        required=False,
    )
    verse_references = NoteVerseReferenceSerializer(
        many=True,
        required=False,
        default=list,
    )

    def validate_verse_references(self, value):
        """Dedupe repeated refs — double-linking one verse would
        violate the NoteVerse uniqueness constraint."""
        return dedupe_verse_refs(value)


class BulkNoteCreateSerializer(serializers.Serializer):
    """
    Serializer for bulk note creation under a single tag.
    POST /api/v1/notes/bulk/
    """
    tag_id = serializers.PrimaryKeyRelatedField(
        queryset=Tag.objects.all(),
        help_text="UUID of the tag to assign to all notes.",
    )
    notes = BulkNoteItemSerializer(
        many=True,
        # Match the /notes/reorder/ batch cap — an unbounded list
        # would let one request write thousands of rows.
        max_length=100,
        help_text="List of notes to create (max 100).",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            if request.user.is_authenticated:
                self.fields['tag_id'].queryset = (
                    Tag.objects.filter(user=request.user)
                )
            else:
                # Anonymous callers can only read; writes are
                # rejected by the view's permissions before
                # validation, so the queryset stays empty.
                self.fields['tag_id'].queryset = (
                    Tag.objects.none()
                )

    def validate_notes(self, value):
        if not value:
            raise serializers.ValidationError(
                "At least one note is required."
            )
        return value

    def create(self, validated_data):
        """
        Bulk-creates all notes and their verse links.
        Called inside transaction.atomic() by the view.
        """
        tag = validated_data['tag_id']
        notes_data = validated_data['notes']
        # The view requires IsAuthenticated, so a real user is
        # always present here.
        user = self.context['request'].user

        verse_map = resolve_verse_refs(
            ref
            for nd in notes_data
            for ref in nd.get('verse_references', [])
        )

        note_instances = [
            Note(
                note_text=nd.get('note_text', ''),
                # Omitted flag inherits the tag's publicity;
                # an explicit false is honored.
                public=nd.get('public', tag.public),
                tag=tag,
                user=user,
            )
            for nd in notes_data
        ]
        Note.objects.bulk_create(note_instances)

        nv_instances = []
        for note_obj, nd in zip(
            note_instances, notes_data
        ):
            for ref in nd.get('verse_references', []):
                key = (
                    ref['book'].lower(),
                    ref['chapter'],
                    ref['verse'],
                )
                nv_instances.append(
                    NoteVerse(
                        note=note_obj,
                        verse=verse_map[key],
                    )
                )
        if nv_instances:
            NoteVerse.objects.bulk_create(nv_instances)

        return note_instances


class PositionField(serializers.Field):
    """Reorder position field: coerces int/decimal-string input
    to float and rejects non-finite values.

    ``float()`` on a huge int literal raises OverflowError and
    'nan'/'inf' parse successfully — both must surface as a
    400, not a 500 or a silently stored NaN.
    """
    default_error_messages = {
        'invalid': 'A valid number is required.',
        'non_finite': 'Position must be a finite number.',
    }

    def to_internal_value(self, data):
        if isinstance(data, bool) or isinstance(
            data, (dict, list, tuple)
        ):
            self.fail('invalid')
        try:
            value = float(data)
        except (TypeError, ValueError, OverflowError):
            self.fail('invalid')
        if not math.isfinite(value):
            self.fail('non_finite')
        return value

    def to_representation(self, value):
        return float(value)


class NoteReorderUpdateSerializer(serializers.Serializer):
    """One ``{note_id, position}`` pair in a reorder request."""
    note_id = serializers.CharField(allow_blank=False)
    position = PositionField()


class NoteReorderSerializer(serializers.Serializer):
    """Validates POST /api/v1/notes/reorder/ bodies.

    The view converts serializer errors into the annotations
    ``{'error': 'validation_error', 'message'}`` contract.
    """
    tag_id = serializers.CharField(allow_blank=False)
    updates = NoteReorderUpdateSerializer(
        many=True,
        allow_empty=False,
        max_length=100,
    )

    def validate(self, attrs):
        seen_ids = set()
        positions = []
        for update in attrs['updates']:
            note_id = update['note_id']
            if note_id in seen_ids:
                raise serializers.ValidationError(
                    f"Duplicate note_id in request: {note_id}"
                )
            seen_ids.add(note_id)
            positions.append(update['position'])
        if len(positions) != len(set(positions)):
            raise serializers.ValidationError(
                'Duplicate positions in request.'
            )
        return attrs


class NoteMoveSerializer(serializers.Serializer):
    """Validates POST /api/v1/notes/move/ bodies."""
    tag_id = serializers.CharField(allow_blank=False)
    note_id = serializers.CharField(allow_blank=False)
    target_note_id = serializers.CharField(allow_blank=False)
    placement = serializers.ChoiceField(
        choices=('after', 'before')
    )

    def validate(self, attrs):
        if attrs['note_id'] == attrs['target_note_id']:
            raise serializers.ValidationError(
                'note_id and target_note_id must differ.'
            )
        return attrs


class CommentAuthorSerializer(serializers.ModelSerializer):
    """Minimal read-only representation of the comment author."""

    class Meta:
        model = User
        fields = ['id', 'username']
        read_only_fields = ['id', 'username']


class ImageSerializer(serializers.ModelSerializer):
    """
    Read/write serializer for Image attachments.

    ``signed_url`` is a computed short-lived GET URL served to clients
    so they never read directly from a public bucket. Generation
    requires ADC on GCP; when signing fails (local/test environments
    without credentials) ``signed_url`` is ``None`` — never the raw
    ``gs://`` URI, which browsers cannot fetch.
    """

    signed_url = serializers.SerializerMethodField()

    class Meta:
        model = Image
        fields = [
            'id',
            'storage_url',
            'signed_url',
            'content_type',
            'size_bytes',
            'comment',
            'note',
            'uploaded_by',
            'created_at',
        ]
        # 'comment'/'note' are set by the viewset from the nested
        # route — letting clients write them would allow silent
        # re-parenting of an existing image.
        read_only_fields = [
            'id',
            'storage_url',
            'signed_url',
            'content_type',
            'size_bytes',
            'comment',
            'note',
            'uploaded_by',
            'created_at',
        ]

    def get_signed_url(self, obj):
        try:
            from annotations.services.image_storage import (
                signed_image_url,
            )
            return signed_image_url(obj.id, obj.storage_url)
        except Exception as e:
            # Signing runs on our credentials against our
            # bucket — a failure is our infrastructure breaking
            # (or a code bug), not a routine upstream blip, so
            # it reports at error level like a genuine 500. The
            # field still degrades to None for the client.
            capture_internal_exception(e)
            return None


class CommentSerializer(serializers.ModelSerializer):
    """
    Serializes a single Comment for both read and write operations.

    - On write (POST/PATCH): accepts 'content' and, on create only,
      optionally 'parent_comment' (its PK). 'user' and 'note' are
      injected by the view. 'is_deleted' is read-only — deletion
      happens exclusively via DELETE, so PATCH cannot un-delete.
    - On read (GET): exposes author info via 'author' and omits
      internal 'user' FK so clients receive a clean response.
      The 'replies' field is populated by build_comment_tree and
      is not part of the model schema.
    - Soft-deleted comments are redacted on every read path
      (to_representation): 'content' becomes '[deleted]' and
      'images' becomes []. Clients must render a tombstone for
      is_deleted=True; the row is never removed so reply threads
      stay intact.

    N+1 prevention: this serializer is intentionally flat.
    The view fetches all comments in one query and delegates
    tree assembly to build_comment_tree().
    """

    author = CommentAuthorSerializer(
        source='user',
        read_only=True
    )
    images = ImageSerializer(many=True, read_only=True)
    parent_comment = serializers.PrimaryKeyRelatedField(
        queryset=Comment.objects.none(),
        allow_null=True,
        required=False,
        default=None
    )
    content = serializers.CharField(
        # Bound the payload — a comment should never carry an
        # unbounded blob of text.
        max_length=10000
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scope the parent_comment lookup to the note named in
        # the URL: a comment pk from another note fails PKRF
        # validation instead of resolving, so the field cannot
        # be used to probe whether a comment exists elsewhere.
        note_pk = self.context.get('note_pk')
        if note_pk is not None:
            self.fields['parent_comment'].queryset = (
                Comment.objects.filter(note_id=note_pk)
            )

    class Meta:
        model = Comment
        fields = [
            'id',
            'author',
            'note_id',
            'parent_comment',
            'content',
            'timestamp',
            'is_deleted',
            'images',
        ]
        read_only_fields = [
            'id', 'author', 'note_id', 'timestamp', 'is_deleted',
            'images',
        ]

    def to_representation(self, instance):
        """Serialize; redact content/images of deleted comments."""
        data = super().to_representation(instance)
        if instance.is_deleted:
            data['content'] = '[deleted]'
            data['images'] = []
        return data

    def validate_parent_comment(self, value):
        """
        Ensure a reply targets a comment on the same note.

        parent_comment is create-only: once a comment exists its
        position in the thread is immutable (no re-parenting).
        """
        if self.instance is not None:
            raise serializers.ValidationError(
                "parent_comment cannot be changed."
            )
        if value is None:
            return value
        if value.is_deleted:
            # Replies to a tombstone are rejected — the reply
            # would hang off a '[deleted]' stub whose author can
            # no longer see or moderate it.
            raise serializers.ValidationError(
                "Cannot reply to a deleted comment."
            )
        # Defense-in-depth: the field queryset is already scoped
        # to the URL's note in __init__; keep the check so a
        # serializer built without context still enforces it.
        note_pk = self.context.get('note_pk')
        if note_pk and str(value.note_id) != str(note_pk):
            raise serializers.ValidationError(
                "parent_comment must belong to the same note."
            )
        return value


def build_comment_tree(queryset):
    """
    Assemble a flat Comment queryset into a nested tree structure.

    All comments are fetched in a single DB query by the caller.
    This function performs O(n) work in Python to build the tree,
    avoiding any additional database round-trips.

    Deleted comments are represented with redacted content
    (handled by CommentSerializer.to_representation) so that reply
    threads remain structurally intact in the response.

    Returns a list of root-level comment dicts, each containing a
    'replies' list that may itself contain further nested comment
    dicts.
    """
    comment_map = {}

    for comment in queryset:
        data = CommentSerializer(comment).data
        data['replies'] = []
        comment_map[comment.id] = data

    roots = []
    for comment in queryset:
        node = comment_map[comment.id]
        if comment.parent_comment_id:
            parent = comment_map.get(comment.parent_comment_id)
            if parent is not None:
                parent['replies'].append(node)
        else:
            roots.append(node)

    return roots


class ReadingPositionSerializer(serializers.ModelSerializer):
    """Serializes ReadingPosition model for API operations."""

    # Chapters/verses are 1-indexed — 0 or negative values are
    # meaningless coordinates, so reject them with 400 instead
    # of storing them. Both stay required on POST/PUT (the
    # model's default=1 would otherwise auto-optional them);
    # PATCH is unaffected — partial=True skips required checks.
    chapter = serializers.IntegerField(min_value=1)
    verse = serializers.IntegerField(min_value=1)

    class Meta:
        model = ReadingPosition
        fields = [
            'id',
            'book',
            'chapter',
            'verse',
            'last_accessed'
        ]
        read_only_fields = ['id', 'last_accessed']

    def validate_book(self, value):
        """Validate *and canonicalize* the book name.

        The lookup is tolerant (aliases, case, typos) but the row
        must store one canonical title-cased name — the
        ``(user, book)`` uniqueness and the ``?book=``/``bulk``
        lookups are exact-match, so storing the raw variant
        ('john') would fork a second position for the same
        logical book.
        """
        book_id = get_dbt_book_id(value)
        if book_id is None:
            raise serializers.ValidationError(
                f"Invalid book name: {value}"
            )
        return get_book_name_from_id(book_id)

    def update(self, instance, validated_data):
        """Reject attempts to change ``book`` on an existing row.

        ``book`` is the upsert key: letting it change under PATCH
        would either fork the position or collide with another
        row's ``(user, book)`` pair (a 500). Re-seating a
        position goes through delete + create instead.
        """
        new_book = validated_data.get('book')
        if new_book is not None and new_book != instance.book:
            raise serializers.ValidationError({
                'book': (
                    'book cannot be changed after creation; '
                    'delete the position and create a new one.'
                )
            })
        return super().update(instance, validated_data)

    def create(self, validated_data):
        """Create or update reading position (upsert behavior).

        Sets ``self.created_row`` so the view can answer 201 on
        a fresh row vs 200 on an update.
        """
        user = self.context['request'].user
        book = validated_data['book']

        position, created = ReadingPosition.objects.update_or_create(
            user=user,
            book=book,
            defaults={
                'chapter': validated_data.get('chapter', 1),
                'verse': validated_data.get('verse', 1)
            }
        )
        self.created_row = created
        return position
