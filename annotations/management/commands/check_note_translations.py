"""Monthly coverage audit for note translations.

Scans every note and reports misses against the configured
``NOTE_TARGET_LANGUAGES`` — a miss is a target language that has
no NoteTranslation row in a terminal-good state (``ready`` or the
``source`` marker when it equals the note's source language).
Pending rows older than a grace window also count as misses so a
stuck queue surfaces here.

On a clean run the command logs a summary and exits. When misses
exist it emits a single ``sentry_sdk.capture_message`` (level
error) so the audit shows up in Sentry once, not once per note.
Scheduled monthly via the ``run_all_jobs`` JOBS table
(``last_day_of_month``) and the Cloud Scheduler trigger in
terraform.
"""
import logging
from datetime import timedelta

import sentry_sdk
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from annotations.models import Note, NoteTranslation

logger = logging.getLogger(__name__)

# A pending row older than this is considered stuck, not
# in-flight (Cloud Tasks retries exhaust within an hour).
PENDING_GRACE = timedelta(hours=6)


class Command(BaseCommand):
    help = 'Audit note-translation coverage; report to Sentry.'

    def handle(self, *args, **options):
        targets = list(
            getattr(settings, 'NOTE_TARGET_LANGUAGES', [])
        )
        if not targets:
            self.stdout.write(
                'NOTE_TARGET_LANGUAGES is empty — pipeline '
                'disabled, nothing to audit.'
            )
            return

        cutoff = timezone.now() - PENDING_GRACE
        misses = {}  # lang -> count
        checked = 0
        for note in Note.objects.iterator():
            checked += 1
            rows = {
                t.language: t
                for t in note.translations.all()
            }
            for lang in targets:
                row = rows.get(lang)
                if row is None:
                    # Target equal to the detected source is
                    # satisfied by the note itself even before
                    # its 'source' marker row lands.
                    if lang != (note.source_language or ''):
                        misses[lang] = misses.get(lang, 0) + 1
                    continue
                if row.status in ('ready', 'source'):
                    continue
                if (
                    row.status == 'pending'
                    and row.updated_at >= cutoff
                ):
                    continue  # still in-flight
                misses[lang] = misses.get(lang, 0) + 1

        summary = (
            f'checked={checked} '
            + ' '.join(
                f'{lang}={n}' for lang, n in sorted(misses.items())
            )
        )
        if not misses:
            self.stdout.write(
                f'note translation coverage clean ({summary})'
            )
            return
        msg = (
            'note translation coverage misses: ' + summary
        )
        logger.error(msg)
        sentry_sdk.capture_message(msg, level='error')
        self.stdout.write(self.style.ERROR(msg))
