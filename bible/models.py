import uuid
from django.db import models


def generate_verse_id():
    return f"VER{str(uuid.uuid4()).upper().replace('-', '')[:15]}"


class Verse(models.Model):
    id = models.CharField(
        max_length=18,
        default=generate_verse_id,
        primary_key=True,
        editable=False,
        help_text="Unique identifier for the tag."
    )
    book = models.CharField(max_length=50)
    chapter = models.IntegerField()
    verse = models.IntegerField()
    dbt_book_id = models.CharField(max_length=50)

    class Meta:
        # One row per (book, chapter, verse) — note verse links
        # resolve verses with a case-insensitive book lookup and
        # silently rely on there being exactly one row.
        # Requires a human-run `makemigrations`.
        constraints = [
            models.UniqueConstraint(
                fields=['book', 'chapter', 'verse'],
                name='unique_book_chapter_verse',
            )
        ]

    def __str__(self):
        return f'{self.book} {self.chapter}:{self.verse}'


class FeatureFlag(models.Model):
    """A runtime-toggleable feature switch editable in admin.

    Kill-switch semantics: the flag-check helper
    (``bible.services.feature_flags.is_feature_enabled``)
    treats a missing row as enabled, so a feature ships on and
    ops disables it without a redeploy. Rows are read through a
    short-TTL Django cache entry busted on admin saves.
    """
    key = models.CharField(max_length=64, unique=True)
    enabled = models.BooleanField(default=True)
    description = models.CharField(
        max_length=255, blank=True, default=''
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        state = 'enabled' if self.enabled else 'disabled'
        return f'{self.key} ({state})'
