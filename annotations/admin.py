from django.contrib import admin
from .models import (
    Tag, Note, NoteVerse, Comment, Image,
    NoteShare, TagShare, SavedNote,
)


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    """
    Admin configuration for the Tag model.
    """
    list_display = (
        'name',
        'user',
        'parent_tag',
        'created_at',
        'updated_at',
    )
    search_fields = ('name', 'user__username')
    list_filter = ('user', 'created_at', 'updated_at', 'parent_tag')
    # Use a raw input for parent_tag for better UX with many tags
    raw_id_fields = ('user', 'parent_tag',)

    # Optionally, you can add fields or fieldsets to customize the edit form
    fieldsets = (
        (None, {
            'fields': ('name', 'user', 'parent_tag',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),  # Makes this section collapsible
        }),
    )
    # These fields should not be editable by admin users
    readonly_fields = ('created_at', 'updated_at',)


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
    """
    Admin configuration for the Note model.
    """
    list_display = (
        'id',
        'user',
        'note_text',
        'public',
        'created_at',
        'updated_at',
    )
    search_fields = ('note_text', 'user__username')
    list_filter = ('public', 'user', 'created_at', 'updated_at')
    raw_id_fields = ('user',)

    fieldsets = (
        (None, {
            'fields': ('user', 'note_text', 'public', 'tag')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )
    readonly_fields = ('created_at', 'updated_at',)


@admin.register(NoteVerse)
class NoteVerseAdmin(admin.ModelAdmin):
    """
    Admin configuration for the NoteVerse model.
    """
    list_display = ('id', 'note', 'verse', 'created_at')
    search_fields = ('note__content', 'verse__text')
    list_filter = ('created_at',)
    raw_id_fields = ('note', 'verse')

    readonly_fields = ('created_at',)


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    """
    Admin configuration for the Comment model.
    Surfaces soft-deleted comments so they remain auditable.
    """
    list_display = (
        'id',
        'user',
        'note',
        'parent_comment',
        'is_deleted',
        'timestamp',
    )
    search_fields = ('content', 'user__username', 'note__id')
    list_filter = ('is_deleted', 'timestamp')
    raw_id_fields = ('user', 'note', 'parent_comment')

    fieldsets = (
        (None, {
            'fields': (
                'user', 'note', 'parent_comment', 'content',
            )
        }),
        ('Status', {
            'fields': ('is_deleted',),
        }),
        ('Timestamps', {
            'fields': ('timestamp',),
            'classes': ('collapse',),
        }),
    )
    readonly_fields = ('id', 'timestamp')


@admin.register(Image)
class ImageAdmin(admin.ModelAdmin):
    """
    Minimal admin for Image — used for debugging and audit.
    """
    list_display = (
        'id',
        'created_at',
        'uploaded_by',
        'note',
        'comment',
        'storage_url',
        'size_bytes',
    )
    search_fields = (
        'id', 'uploaded_by__username', 'storage_url',
    )
    list_filter = ('created_at',)
    raw_id_fields = ('uploaded_by', 'note', 'comment')
    readonly_fields = ('id', 'created_at')


@admin.register(NoteShare)
class NoteShareAdmin(admin.ModelAdmin):
    """Read grant on a single note — audit-friendly listing."""
    list_display = ('id', 'note', 'owner', 'shared_with', 'created_at')
    search_fields = (
        'note__id', 'owner__username', 'shared_with__username'
    )
    raw_id_fields = ('note', 'owner', 'shared_with')
    readonly_fields = ('created_at',)


@admin.register(TagShare)
class TagShareAdmin(admin.ModelAdmin):
    """Read grant on a tag (optionally its whole subtree)."""
    list_display = (
        'id', 'tag', 'owner', 'shared_with',
        'include_descendants', 'created_at',
    )
    search_fields = (
        'tag__name', 'owner__username', 'shared_with__username'
    )
    list_filter = ('include_descendants',)
    raw_id_fields = ('tag', 'owner', 'shared_with')
    readonly_fields = ('created_at',)


@admin.register(SavedNote)
class SavedNoteAdmin(admin.ModelAdmin):
    """Bookmark pointer from a user to a non-owned note."""
    list_display = ('id', 'user', 'note', 'created_at')
    search_fields = ('user__username', 'note__id')
    raw_id_fields = ('user', 'note')
    readonly_fields = ('created_at',)
