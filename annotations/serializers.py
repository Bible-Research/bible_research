import logging

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
from bible.utils.provider_errors import provider_error_fields
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


class TagSerializer(serializers.ModelSerializer):
    """Serializes Tag model data for API input and output.
    Includes the 'parent_tag' as its primary key for relationships.
    """
    parent_tag = serializers.PrimaryKeyRelatedField(
        queryset=Tag.objects.all(),
        allow_null=True,
        required=False,
        default=None
    )
    user = serializers.HiddenField(
        default=serializers.CurrentUserDefault()
    )
    name = serializers.CharField(max_length=100)

    class Meta:
        model = Tag
        fields = [
            'id', 'user', 'name', 'parent_tag',
            'created_at', 'updated_at',
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
        # Create the Note row and its NoteVerse links in one
        # transaction so a failed verse resolution rolls the Note
        # back instead of leaving an orphan row behind.
        with transaction.atomic():
            # Create the Note instance with the remaining validated data
            note = Note.objects.create(**validated_data)

            # Handle the many-to-many relationship via NoteVerse
            if verse_references_data:
                note_verse_instances = []
                for verse_ref_data in verse_references_data:
                    try:
                        # Attempt to retrieve the Verse instance using
                        # case-insensitive book match
                        verse_instance = Verse.objects.get(
                            book__iexact=verse_ref_data['book'],
                            chapter=verse_ref_data['chapter'],
                            verse=verse_ref_data['verse']
                        )
                        # Create a NoteVerse link between note and verse
                        note_verse_instances.append(
                            NoteVerse(note=note, verse=verse_instance)
                        )
                    except Verse.DoesNotExist:
                        error_msg = (
                            f"Verse not found for "
                            f"'{verse_ref_data['book']} "
                            f"{verse_ref_data['chapter']}:"
                            f"{verse_ref_data['verse']}'. "
                            f"Please ensure the verse exists in "
                            f"your database."
                        )
                        raise serializers.ValidationError(error_msg)
                    except Exception as e:
                        raise serializers.ValidationError(
                            f"Error processing verse reference: {e}"
                        )

                # Bulk-create the NoteVerse link rows for efficiency
                NoteVerse.objects.bulk_create(note_verse_instances)

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
        verse_instances = None
        if verse_references_data is not None:
            verse_instances = []
            for verse_ref_data in verse_references_data:
                try:
                    verse_instances.append(Verse.objects.get(
                        book__iexact=verse_ref_data['book'],
                        chapter=verse_ref_data['chapter'],
                        verse=verse_ref_data['verse']
                    ))
                except Verse.DoesNotExist:
                    error_msg = (
                        f"Verse not found for "
                        f"'{verse_ref_data['book']} "
                        f"{verse_ref_data['chapter']}:"
                        f"{verse_ref_data['verse']}'. "
                        f"Please ensure the verse exists "
                        f"in your database."
                    )
                    raise serializers.ValidationError(error_msg)
                except Exception as e:
                    raise serializers.ValidationError(
                        f"Error processing verse reference: {e}"
                    )

        note = super().update(instance, validated_data)

        if verse_instances is not None:
            with transaction.atomic():
                note.verses.clear()
                NoteVerse.objects.bulk_create([
                    NoteVerse(note=note, verse=verse_instance)
                    for verse_instance in verse_instances
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
            representation.update(provider_error_fields(e))
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
    public = serializers.BooleanField(
        required=False,
        default=False,
    )
    verse_references = NoteVerseReferenceSerializer(
        many=True,
        required=False,
        default=list,
    )


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
        help_text="List of notes to create.",
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

    def _resolve_verses(self, notes_data):
        """
        Batch-fetches all Verse instances referenced across
        all notes. Returns a dict keyed by
        (book_lower, chapter, verse).
        Raises ValidationError for any verse not found.
        """
        unique_keys = set()
        for note_data in notes_data:
            for ref in note_data.get('verse_references', []):
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
                f"Verses not found: {missing_str}. "
                "Please ensure the verses exist "
                "in your database."
            )
        return verse_map

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

        verse_map = self._resolve_verses(notes_data)

        note_instances = [
            Note(
                note_text=nd.get('note_text', ''),
                public=nd.get('public', False),
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
    requires ADC on GCP; in local/test environments it falls back to
    ``storage_url``.
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
        read_only_fields = [
            'id',
            'storage_url',
            'signed_url',
            'content_type',
            'size_bytes',
            'uploaded_by',
            'created_at',
        ]

    def get_signed_url(self, obj):
        try:
            from annotations.services.image_storage import (
                signed_image_url,
            )
            return signed_image_url(obj.id, obj.storage_url)
        except Exception:
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
        queryset=Comment.objects.all(),
        allow_null=True,
        required=False,
        default=None
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
        request = self.context.get('request')
        note_pk = (
            self.context.get('note_pk') or
            (request and request.parser_context and
             request.parser_context.get('kwargs', {})
             .get('note_pk'))
        )
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
        """Create or update reading position (upsert behavior)."""
        user = self.context['request'].user
        book = validated_data['book']

        position, created = ReadingPosition.objects.update_or_create(
            user=user,
            book=book,
            defaults={
                'chapter': validated_data['chapter'],
                'verse': validated_data.get('verse', 1)
            }
        )
        return position
