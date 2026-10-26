import uuid
import time
from django.db import models
from django.db.models import UniqueConstraint, Q
from django.db.models.signals import post_delete
from django.dispatch import receiver
from bible.models import Verse
from django.contrib.auth import get_user_model

User = get_user_model()


def generate_tag_id():
    return f"TAG{str(uuid.uuid4()).upper().replace('-', '')[:15]}"


def get_unix_timestamp():
    """Returns current Unix timestamp as a float."""
    return time.time()


class Tag(models.Model):
    """
    Represents a user-defined tag for organizing content.
    Tags can be hierarchical (e.g., 'Love' as parent of 'Reckless love').
    """

    id = models.CharField(
        max_length=18,
        default=generate_tag_id,
        primary_key=True,
        editable=False,
        help_text="Unique identifier for the tag."
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='tags',
        null=True,
        blank=True,
        help_text="The user who created this tag."
    )
    name = models.CharField(
        max_length=100,
        # Users can have tags with the same name, but not for the same user.
        unique=False,
        help_text="The name of the tag (e.g., 'Love', 'Grace')."
    )
    parent_tag = models.ForeignKey(
        # Refers to the Tag model itself, allowing for hierarchical tags.
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,  # The parent_tag is optional.
        related_name='children',
        help_text="The parent tag in a hierarchical structure (optional)."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Tag"
        verbose_name_plural = "Tags"

        # Ensures that a user cannot have two tags with the same
        # name and parent.
        unique_together = ('user', 'name', 'parent_tag')
        # unique_together cannot cover parent_tag=None (UNIQUE
        # treats NULLs as distinct), so top-level name uniqueness
        # needs this conditional constraint. TagSerializer also
        # validates it application-side for a clean 400.
        # Requires a human-run `makemigrations`.
        constraints = [
            UniqueConstraint(
                fields=['user', 'name'],
                condition=Q(parent_tag__isnull=True),
                name='unique_top_level_tag_name_per_user',
            )
        ]
        ordering = ['name']

    def __str__(self):
        """
        String representation of the Tag.
        """
        username = self.user.username if self.user else 'guest'
        if self.parent_tag:
            return f"{username}'s Tag: {self.parent_tag.name} > {self.name}"
        return f"{username}'s Tag: {self.name}"


def generate_note_id():
    return f"NOT{str(uuid.uuid4()).upper().replace('-', '')[:15]}"


class Note(models.Model):
    """
    Represents a user's personal note or commentary.
    Notes can be associated with a primary tag.
    """
    id = models.CharField(
        max_length=18,
        default=generate_note_id,
        primary_key=True,
        editable=False,
        help_text="Unique identifier for the note."
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='notes',
        null=True,
        blank=True,
        help_text="The user who created this note."
    )

    tag = models.ForeignKey(
        Tag,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        # Allows you to get notes from a Tag object (e.g., my_tag.notes.all())
        related_name='notes',
        help_text="A primary tag associated with this note (optional)."
    )

    note_text = models.TextField(
        blank=True, help_text="The actual content of the note."
    )

    public = models.BooleanField(
        default=False,
        help_text="If True, this note is accessible to unauthenticated users."
    )

    tag_position = models.FloatField(
        default=get_unix_timestamp,
        help_text=(
            "User-defined display order within a tag group. "
            "Defaults to Unix timestamp at creation."
        ),
    )

    # Many-to-many relationship with Verse model, through the NoteVerse
    # intermediary table. This explicit 'through' model is necessary because
    # you have 'note_verses' table in your schema, which implies additional
    # data (like 'id' for the link).
    verses = models.ManyToManyField(
        Verse,
        through='NoteVerse',
        # Allows you to get notes from a Verse object
        # (e.g., my_verse.notes.all())
        related_name='notes',
        help_text="The Bible verses associated with this note."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Note"
        verbose_name_plural = "Notes"
        # Order by most recent notes first
        ordering = ['-created_at']
        constraints = [
            UniqueConstraint(
                fields=['tag', 'tag_position'],
                name='unique_tag_position_per_tag',
                condition=Q(tag__isnull=False),
            )
        ]

    def __str__(self):
        # Display the first 50 characters of the note text
        truncated_text = self.note_text[:50]
        ellipsis = '...' if len(self.note_text) > 50 else ''
        id_display = (
            self.id[:8]
            if isinstance(self.id, str)
            else self.id.hex[:8]
        )
        return f"Note (ID: {id_display}): {truncated_text}{ellipsis}"


def generate_comment_id():
    return f"CMNT_{str(uuid.uuid4()).upper().replace('-', '')[:13]}"


class Comment(models.Model):
    """
    Represents a user comment on a Note, supporting infinite nesting
    via a self-referencing parent_comment foreign key.

    Soft-deletion (is_deleted=True) preserves reply threads when a
    parent comment is removed.

    Indexing strategy:
      - note_id: enables fast lookups of all comments for a given note.
      - parent_comment_id: enables fast lookups of direct replies to
        any comment.
    Both ForeignKey fields carry Django's default db_index=True;
    Meta.indexes makes the intent explicit for documentation and
    allows future composite-index extensions.
    """

    id = models.CharField(
        max_length=18,
        default=generate_comment_id,
        primary_key=True,
        editable=False,
        help_text="Unique identifier for the comment."
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='comments',
        help_text="The user who authored this comment."
    )
    note = models.ForeignKey(
        Note,
        on_delete=models.CASCADE,
        related_name='comments',
        help_text="The root note this comment belongs to."
    )
    parent_comment = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='replies',
        help_text=(
            "Parent comment for nested replies. "
            "Null for top-level comments."
        )
    )
    content = models.TextField(
        help_text="The text content of the comment."
    )
    timestamp = models.DateTimeField(
        auto_now_add=True,
        help_text="When the comment was created."
    )
    is_deleted = models.BooleanField(
        default=False,
        help_text=(
            "Soft-deletion flag. Deleted comments are hidden "
            "but retained so reply threads remain unbroken."
        )
    )

    class Meta:
        verbose_name = "Comment"
        verbose_name_plural = "Comments"
        ordering = ['timestamp']
        indexes = [
            models.Index(
                fields=['note_id'],
                name='comment_note_id_idx'
            ),
            models.Index(
                fields=['parent_comment_id'],
                name='comment_parent_id_idx'
            ),
        ]

    def __str__(self):
        truncated = self.content[:50]
        ellipsis = '...' if len(self.content) > 50 else ''
        return (
            f"Comment (ID: {self.id[:8]}): "
            f"{truncated}{ellipsis}"
        )


def generate_image_id():
    return f"IMG_{str(uuid.uuid4()).upper().replace('-', '')[:13]}"


class Image(models.Model):
    """
    Generic image attachment. Belongs to *either* a Note or a Comment
    (XOR — enforced by a CheckConstraint). The file itself lives in
    GCS; only the storage URL is persisted here.

    Indexing strategy:
      - comment_id and note_id each carry Django's default
        db_index=True, made explicit in Meta.indexes for documentation
        and so future composite indexes have a clear home.
    """

    id = models.CharField(
        max_length=18,
        default=generate_image_id,
        primary_key=True,
        editable=False,
        help_text="Unique identifier for the image (IMG_…).",
    )
    storage_url = models.CharField(
        max_length=1024,
        help_text=(
            "Canonical GCS URI of the original full-resolution file "
            "(e.g. gs://<bucket>/originals/<image_id>/<filename>)."
        ),
    )
    content_type = models.CharField(
        max_length=64,
        blank=True,
        help_text=(
            "MIME type captured at upload time (e.g. image/jpeg)."
        ),
    )
    size_bytes = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="File size in bytes, captured at upload time.",
    )
    comment = models.ForeignKey(
        Comment,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="images",
        help_text="Comment this image is attached to (nullable).",
    )
    note = models.ForeignKey(
        Note,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="images",
        help_text="Note this image is attached to (nullable).",
    )
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_images",
        help_text="User who uploaded the image.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Image"
        verbose_name_plural = "Images"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["comment_id"],
                name="image_comment_id_idx",
            ),
            models.Index(
                fields=["note_id"],
                name="image_note_id_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                name="image_exactly_one_parent",
                condition=(
                    Q(comment__isnull=False, note__isnull=True)
                    | Q(comment__isnull=True, note__isnull=False)
                ),
            ),
        ]

    def __str__(self):
        parent = (
            f"comment {self.comment_id}"
            if self.comment_id
            else f"note {self.note_id}"
        )
        return f"Image (ID: {self.id[:8]}) on {parent}"


def generate_note_verse_id():
    return f"NVE{str(uuid.uuid4()).upper().replace('-', '')[:15]}"


class NoteVerse(models.Model):
    """
    Intermediary model for many-to-many relationship between Note and Verse.
    This explicit model corresponds to your 'note_verses' table.
    """
    id = models.CharField(
        max_length=18,
        default=generate_note_verse_id,
        primary_key=True,
        editable=False,
        help_text="Unique identifier for note-verse link."
    )

    note = models.ForeignKey(
        Note,
        # If a note is deleted, its links to verses are also deleted.
        on_delete=models.CASCADE,
        help_text="The note associated with the verse."
    )

    verse = models.ForeignKey(
        Verse,
        on_delete=models.PROTECT,  # Prevent deleting Verses that have Notes
        help_text="The verse associated with the note."
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Note-Verse Link"
        verbose_name_plural = "Note-Verse Links"
        # Ensures that a specific note is linked to a specific
        # verse only once. Serializers also dedupe input
        # application-side for a clean 400.
        # Requires a human-run `makemigrations`.
        constraints = [
            UniqueConstraint(
                fields=['note', 'verse'],
                name='unique_note_verse_link',
            )
        ]
        # ordering = ['note', 'verse']

    def __str__(self):
        if isinstance(self.note.id, str):
            note_id_display = self.note.id[:8]
        else:
            note_id_display = self.note.id.hex[:8]
        return (
            f"Link: Note {note_id_display} to Verse {self.verse.id}"
        )


def generate_reading_position_id():
    return f"RDP{str(uuid.uuid4()).upper().replace('-', '')[:15]}"


class ReadingPosition(models.Model):
    """
    Tracks the last reading position for each book per user.
    Automatically syncs across all devices.
    """
    id = models.CharField(
        max_length=18,
        primary_key=True,
        editable=False,
        default=generate_reading_position_id,
        help_text="Unique identifier for reading position."
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='reading_positions',
        help_text="The user who owns this reading position."
    )

    book = models.CharField(
        max_length=50,
        help_text=(
            "Book name (e.g., 'Genesis', 'John', '1 Chronicles')"
        )
    )

    chapter = models.IntegerField(
        default=1,
        help_text="Chapter number (1-based)"
    )

    verse = models.IntegerField(
        default=1,
        help_text="First verse in view (for scroll restoration)"
    )

    last_accessed = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'reading_positions'
        verbose_name = "Reading Position"
        verbose_name_plural = "Reading Positions"
        unique_together = [['user', 'book']]
        ordering = ['-last_accessed']
        indexes = [
            models.Index(
                fields=['user', 'book'],
                name='reading_pos_user_book_idx'
            ),
            models.Index(
                fields=['user', '-last_accessed'],
                name='reading_pos_user_time_idx'
            ),
        ]

    def __str__(self):
        username = self.user.username if self.user else 'unknown'
        return f"{username} - {self.book} {self.chapter}:{self.verse}"


@receiver(post_delete, sender=Image)
def delete_image_blob(sender, instance, **kwargs):
    """Delete the image's GCS object whenever its row goes away.

    Row deletion happens through more paths than
    ``DELETE /images/<pk>/`` — note/tag/user cascades remove
    Image rows via FK CASCADE and used to orphan the blobs in
    the bucket forever. Firing on ``post_delete`` catches every
    removal path. Failures are logged-and-tolerated inside
    ``delete_original`` (best-effort; a missing blob is fine).
    """
    # Local import: image_storage pulls in google-cloud-storage,
    # which must not load during model import.
    from .services.image_storage import delete_original

    delete_original(instance.id, instance.storage_url)
