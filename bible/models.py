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
