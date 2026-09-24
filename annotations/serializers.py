from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import serializers
from bible.models import Verse
from bible.services.dbt.client import get_default_dbt_client
from bible.services.esv.client import get_default_esv_client
from bible.services.esv.registry import is_esv_fileset
from bible.utils.bible_books import get_dbt_book_id
from .models import (
    Note,
    NoteVerse,
    Tag,
    Comment,
    Image,
    ReadingPosition
)
User = get_user_model()


class CurrentAuthenticatedUserDefault:
    """Custom default callable for authenticated user field.
    Returns the current user if authenticated, otherwise the guest user.
    """
    requires_context = True

    def __call__(self, serializer_field):
        request = serializer_field.context.get('request')
        if (request and hasattr(request, 'user') and
                request.user.is_authenticated):
            return request.user
        # Return guest user instead of None
        try:
            return User.objects.get(username='guest')
        except User.DoesNotExist:
            # If guest user doesn't exist, return None
            # The create method will handle this case
            return None


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
        default=CurrentAuthenticatedUserDefault()
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
                # For unauthenticated users, only show guest user tags
                try:
                    guest_user = User.objects.get(username='guest')
                    self.fields['parent_tag'].queryset = Tag.objects.filter(
                        user=guest_user
                    )
                except User.DoesNotExist:
                    self.fields['parent_tag'].queryset = Tag.objects.none()

    def create(self, validated_data):
        # If user is None, try to use guest user
        if 'user' in validated_data and validated_data['user'] is None:
            try:
                validated_data['user'] = User.objects.get(username='guest')
            except User.DoesNotExist:
                # If guest user doesn't exist, remove user field
                # to let the model use its default
                validated_data.pop('user')
        return super().create(validated_data)


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
        default=CurrentAuthenticatedUserDefault()
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
                # For unauthenticated users, only show guest user tags
                try:
                    guest_user = User.objects.get(username='guest')
                    self.fields['tag'].queryset = Tag.objects.filter(
                        user=guest_user
                    )
                except User.DoesNotExist:
                    self.fields['tag'].queryset = Tag.objects.none()

    class Meta:
        model = Note
        fields = [
            'id', 'user', 'note_text', 'public', 'created_at',
            'updated_at', 'tag', 'tag_position', 'verse_references'
        ]

    def create(self, validated_data):
        """
        Overrides the default create method to handle nested verse_references.
        """
        # Pop out the 'verse_references' as it's not a direct field on the Note
        verse_references_data = validated_data.pop('verse_references', [])
        # If user is None, try to use guest user
        if 'user' in validated_data and validated_data['user'] is None:
            try:
                validated_data['user'] = User.objects.get(username='guest')
            except User.DoesNotExist:
                # If guest user doesn't exist, remove user field
                validated_data.pop('user')
        # Create the Note instance using the remaining validated data
        note = Note.objects.create(**validated_data)

        # Handle the many-to-many relationship with verses via NoteVerse
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
                    # Create a NoteVerse instance to link the note and verse
                    note_verse_instances.append(
                        NoteVerse(note=note, verse=verse_instance)
                    )
                except Verse.DoesNotExist:
                    error_msg = (
                        f"Verse not found for '{verse_ref_data['book']} "
                        f"{verse_ref_data['chapter']}:"
                        f"{verse_ref_data['verse']}'. "
                        f"Please ensure the verse exists in your database."
                    )
                    raise serializers.ValidationError(error_msg)
                except Exception as e:
                    raise serializers.ValidationError(
                        f"Error processing verse reference: {e}"
                    )

            # Bulk create the intermediary NoteVerse instances for efficiency
            NoteVerse.objects.bulk_create(note_verse_instances)

        return note

    def to_representation(self, instance):
        """
        Overrides the default representation for GET requests to
        include nested tag and verse data with content from
        Bible provider, plus section headings.
        """
        representation = super().to_representation(instance)
        verses = list(instance.verses.all().order_by('verse'))
        if not verses:
            return representation

        dbt_book_id = verses[0].dbt_book_id
        chapter = verses[0].chapter
        book_name = verses[0].book

        verse_numbers = [v.verse for v in verses]
        first_verse_num = verse_numbers[0]
        last_verse_num = verse_numbers[-1]

        # Get fileset_id from context, default to DBT
        fileset_id = self.context.get('fileset_id', 'ENGESV')

        verses_with_text = []
        headings = []

        try:
            # Use ESV client if fileset is ENGESV_API
            if is_esv_fileset(fileset_id):
                esv_client = get_default_esv_client()
                parsed = esv_client.get_chapter_with_headings(
                    dbt_book_id, chapter
                )

                # Extract verses in range
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
                # this note's verses
                headings = [
                    {
                        'before_verse': h.get('before_verse'),
                        'text': h.get('text', '')
                    }
                    for h in parsed.get('headings', [])
                    if h.get('before_verse') in verse_numbers
                ]
            else:
                # Use DBT client for other filesets
                kwargs = {
                    "verse_start": first_verse_num,
                    "verse_end": last_verse_num
                }
                dbt_client = get_default_dbt_client()
                verse_text = dbt_client.get_verses(
                    dbt_book_id, chapter, **kwargs
                )

                for verse in verses:
                    matching_verse = next(
                        (
                            v for v in verse_text['data']
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

                # Get headings from DBT response if available
                if 'headings' in verse_text:
                    headings = [
                        {
                            'before_verse': h.get('before_verse'),
                            'text': h.get('text', '')
                        }
                        for h in verse_text.get('headings', [])
                        if h.get('before_verse') in verse_numbers
                    ]
        except Exception as e:
            print(f"Error fetching verses/headings: {e}")
            # Fallback: return verses without text
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
                try:
                    guest_user = User.objects.get(
                        username='guest'
                    )
                    self.fields['tag_id'].queryset = (
                        Tag.objects.filter(user=guest_user)
                    )
                except User.DoesNotExist:
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
        request = self.context.get('request')

        if request and request.user.is_authenticated:
            user = request.user
        else:
            try:
                user = User.objects.get(username='guest')
            except User.DoesNotExist:
                user = None

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

    - On write (POST/PATCH): accepts 'content' and optionally
      'parent_comment' (its PK). 'user' and 'note' are injected
      by the view.
    - On read (GET): exposes author info via 'author' and omits
      internal 'user' FK so clients receive a clean response.
      The 'replies' field is populated by build_comment_tree and
      is not part of the model schema.

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
            'id', 'author', 'note_id', 'timestamp', 'images',
        ]

    def validate_parent_comment(self, value):
        """Ensure a reply targets a comment on the same note."""
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

    Deleted comments are represented with redacted content so that
    reply threads remain structurally intact in the response.

    Returns a list of root-level comment dicts, each containing a
    'replies' list that may itself contain further nested comment
    dicts.
    """
    comment_map = {}

    for comment in queryset:
        data = CommentSerializer(comment).data
        if comment.is_deleted:
            data['content'] = '[deleted]'
            data['images'] = []
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
        """Validate book name against known Bible books."""
        if get_dbt_book_id(value) is None:
            raise serializers.ValidationError(
                f"Invalid book name: {value}"
            )
        return value

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
