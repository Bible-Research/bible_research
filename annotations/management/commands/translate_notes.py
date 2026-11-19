"""Backfill translations for existing notes.

Runs the same worker pipeline used by the Cloud Tasks endpoint —
one inline pass per note. Intended for the initial migration of
the note corpus and for manual repairs.

Usage:
    python manage.py translate_notes            # all notes
    python manage.py translate_notes --user alice
    python manage.py translate_notes --note-id NOTxxxxxxxx --dry-run
"""
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from annotations.models import Note
from annotations.services import tasks as note_tasks

logger = logging.getLogger(__name__)
User = get_user_model()


class Command(BaseCommand):
    help = 'Backfill/refresh note translations via the worker.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--user', metavar='USERNAME',
            help='Limit to notes owned by this user.',
        )
        parser.add_argument(
            '--note-id', metavar='ID',
            help='Limit to a single note id.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='List matching notes without translating.',
        )

    def handle(self, *args, **options):
        qs = Note.objects.all().order_by('created_at')
        if options['user']:
            try:
                user = User.objects.get(username=options['user'])
            except User.DoesNotExist:
                raise CommandError(
                    f"no user named {options['user']!r}"
                )
            qs = qs.filter(user=user)
        if options['note_id']:
            qs = qs.filter(pk=options['note_id'])
            if not qs.exists():
                raise CommandError(
                    f"no note {options['note_id']!r}"
                )

        targets = getattr(settings, 'NOTE_TARGET_LANGUAGES', [])
        total = qs.count()
        self.stdout.write(
            f'{total} note(s); target languages: '
            f'{", ".join(targets) or "(none — kill switch on)"}'
        )
        if options['dry_run']:
            for note in qs.iterator():
                self.stdout.write(f'  {note.id}')
            return

        done = 0
        for note in qs.iterator():
            done += 1
            self.stdout.write(
                f'[{done}/{total}] {note.id}', ending=''
            )
            result = note_tasks.run_translation_task(
                str(note.id),
                note_tasks.hash_note_text(note.note_text),
            )
            self.stdout.write(f' -> {result}')
        self.stdout.write(
            self.style.SUCCESS(f'Processed {total} note(s).')
        )
