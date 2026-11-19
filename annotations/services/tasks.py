"""Cloud Tasks pipeline for note translation (D3).

``enqueue`` fires a Cloud Tasks HTTP task at
``POST {APP_BASE_URL}/api/v1/tasks/translate-note/`` carrying
``{note_id, source_hash}`` and an OIDC token minted for the
tasks-invoker service account. The worker endpoint verifies that
token and calls :func:`run_translation_task`.

Local dev (``not IS_GCP_ENVIRONMENT``) runs the worker inline
inside ``enqueue`` — a zero-infra fallback, no emulator needed.
"""
import json
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from annotations.models import Note, NoteTranslation

from .translation_service import (
    NoteTooLongError,
    TranslationError,
    hash_note_text,
    translate_note_text,
)

logger = logging.getLogger(__name__)

TASK_URL_PATH = '/api/v1/tasks/translate-note/'

# Outcomes of run_translation_task (also mapped to HTTP codes by
# the worker view).
OUTCOME_TRANSLATED = 'translated'
OUTCOME_NOOP = 'noop'
OUTCOME_TOO_LONG = 'too_long'


def note_translation_languages(note_text, note=None):
    """Languages that need pending rows for a write to *note*.

    Always the configured targets — the worker upserts the
    ``source`` row once detection runs (the detected language is
    unknowable before the run).
    """
    return list(getattr(settings, 'NOTE_TARGET_LANGUAGES', []))


def upsert_pending_rows(note, languages, source_hash):
    """(Re)mark *languages* ``pending`` for the run keyed by
    *source_hash*.

    ``update_or_create`` so an edit re-pends previously
    ``ready``/``failed`` rows; the old ``text`` is kept — serving
    is status-gated, and the worker rewrites it on completion.
    """
    for language in languages:
        NoteTranslation.objects.update_or_create(
            note=note,
            language=language,
            defaults={
                'status': NoteTranslation.STATUS_PENDING,
                'source_hash': source_hash,
                'error': '',
            },
        )


def enqueue(note_id, source_hash):
    """Enqueue a translation run for *note_id*.

    On GCP this issues a Cloud Task; locally it runs the worker
    inline so note writes translate with zero infrastructure.
    """
    if getattr(settings, 'IS_GCP_ENVIRONMENT', False):
        _enqueue_cloud_task(note_id, source_hash)
    else:
        run_translation_task(note_id, source_hash)


def _client_kwargs():
    """REST transport locally — the dev proxy strips ALPN."""
    if getattr(settings, 'IS_GCP_ENVIRONMENT', False):
        return {}
    return {'transport': 'rest'}


def _queue_path(client):
    project = getattr(settings, 'GOOGLE_CLOUD_PROJECT', None) or \
        'bible-research-489314'
    location = getattr(
        settings, 'TASK_QUEUE_LOCATION', 'europe-west3'
    )
    queue = getattr(settings, 'TASK_QUEUE_ID', 'note-translations')
    return client.queue_path(project, location, queue)


def _enqueue_cloud_task(note_id, source_hash):
    """POST the worker URL via Cloud Tasks with a tasks-invoker
    OIDC token (queue config: max 5 attempts, exponential
    backoff, 5 concurrent dispatches)."""
    from google.cloud import tasks_v2

    base_url = getattr(settings, 'APP_BASE_URL', '').rstrip('/')
    url = base_url + TASK_URL_PATH
    client = tasks_v2.CloudTasksClient(**_client_kwargs())
    task = {
        'http_request': {
            'http_method': tasks_v2.HttpMethod.POST,
            'url': url,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({
                'note_id': note_id,
                'source_hash': source_hash,
            }).encode('utf-8'),
            'oidc_token': {
                'service_account_email': getattr(
                    settings, 'TASKS_INVOKER_SA_EMAIL', ''
                ),
                'audience': url,
            },
        },
    }
    client.create_task(
        request={'parent': _queue_path(client), 'task': task}
    )
    logger.info(
        'enqueued translate-note task for note %s', note_id
    )


def verify_task_oidc(request):
    """Validate the Cloud Tasks OIDC token on *request*.

    Returns the decoded claims dict on success, ``None`` on any
    failure — the endpoint accepts ONLY a Bearer token minted for
    ``TASKS_INVOKER_SA_EMAIL`` (the auth boundary for the
    otherwise AllowAny task route).
    """
    authorization = request.META.get('HTTP_AUTHORIZATION', '')
    if not authorization.startswith('Bearer '):
        return None
    token = authorization[len('Bearer '):].strip()
    if not token:
        return None
    base_url = getattr(settings, 'APP_BASE_URL', '').rstrip('/')
    audience = base_url + TASK_URL_PATH
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as g_requests

        claims = id_token.verify_oauth2_token(
            token, g_requests.Request(), audience=audience,
        )
    except Exception:
        logger.info('translate-note task OIDC verify failed')
        return None
    expected = getattr(settings, 'TASKS_INVOKER_SA_EMAIL', '')
    if not expected or claims.get('email') != expected:
        logger.warning(
            'translate-note task OIDC email mismatch: %s',
            claims.get('email'),
        )
        return None
    return claims


def _mark_failed(note, languages, source_hash, error):
    """Write ``failed`` rows for *languages* (cap breach / final
    failure) so the UI can surface a retry state."""
    for language in languages:
        NoteTranslation.objects.update_or_create(
            note=note,
            language=language,
            defaults={
                'status': NoteTranslation.STATUS_FAILED,
                'source_hash': source_hash,
                'error': error,
            },
        )


def run_translation_task(note_id, source_hash):
    """Execute one translation run for *note_id* (D4).

    Returns one of ``OUTCOME_*``:

    - ``noop`` — the note vanished or its current text no longer
      matches *source_hash* (a newer edit superseded this run;
      last writer wins).
    - ``too_long`` — the note exceeds the per-note char cap;
      rows are marked ``failed``/``too_long``, never retried.
    - ``translated`` — rows were written.

    Raises ``TranslationError`` for retryable upstream failures —
    the worker view maps it to a non-2xx so Cloud Tasks retries.
    """
    note = Note.objects.filter(pk=note_id).first()
    if note is None:
        return OUTCOME_NOOP
    note_text = note.note_text
    if hash_note_text(note_text) != source_hash:
        return OUTCOME_NOOP

    targets = note_translation_languages(note_text, note)
    if not targets:
        return OUTCOME_NOOP

    # Expensive part runs OUTSIDE the write transaction — holding
    # the note lock across ~seconds of API calls invites stalls.
    detected = None
    translations = {}
    failures = {}
    for target in targets:
        if detected is not None and target == detected:
            continue
        try:
            text, det = translate_note_text(
                note_text, target, source_language=detected,
            )
        except NoteTooLongError:
            with transaction.atomic():
                locked = Note.objects.select_for_update().get(
                    pk=note_id
                )
                if hash_note_text(locked.note_text) != source_hash:
                    return OUTCOME_NOOP
                _mark_failed(
                    locked, targets, source_hash, 'too_long',
                )
            return OUTCOME_TOO_LONG
        except TranslationError:
            logger.exception(
                'translate-note failed for note %s into %s',
                note_id, target,
            )
            failures[target] = 'translate_failed'
            continue
        if detected is None:
            # First call ran without source_language_code —
            # detection comes back free (D6).
            detected = det
            if det is None and text == note_text:
                # Empty/undetectable text skipped the API call —
                # no point storing a 'ready' row for it.
                continue
        if target != detected:
            # A target coinciding with the detected source gets
            # the 'source' row below, not a stored translation.
            translations[target] = text

    engine = getattr(settings, 'NOTE_TRANSLATION_ENGINE', '')

    with transaction.atomic():
        # Re-check under the row lock: a run superseded while the
        # API calls were in flight must abort instead of
        # overwriting fresher rows (D4).
        locked = Note.objects.select_for_update().get(pk=note_id)
        if hash_note_text(locked.note_text) != source_hash:
            return OUTCOME_NOOP
        if detected:
            locked.source_language = detected
            locked.save(update_fields=['source_language'])
            # Uniform language slot for the detected source —
            # text stays EMPTY, the original is served from
            # Note.note_text (D2). Upserted even when the source
            # is not a configured target.
            NoteTranslation.objects.update_or_create(
                note=locked,
                language=detected,
                defaults={
                    'text': '',
                    'status': NoteTranslation.STATUS_SOURCE,
                    'source_hash': source_hash,
                    'engine': '',
                    'error': '',
                    'author_verified': False,
                    'verified_at': None,
                },
            )
        for language, text in translations.items():
            # Every fresh write resets verification — new machine
            # output is unreviewed by definition (D9).
            NoteTranslation.objects.update_or_create(
                note=locked,
                language=language,
                defaults={
                    'text': text,
                    'status': NoteTranslation.STATUS_READY,
                    'source_hash': source_hash,
                    'engine': engine,
                    'error': '',
                    'author_verified': False,
                    'verified_at': None,
                },
            )
        for language, error in failures.items():
            NoteTranslation.objects.update_or_create(
                note=locked,
                language=language,
                defaults={
                    'status': NoteTranslation.STATUS_FAILED,
                    'source_hash': source_hash,
                    'engine': '',
                    'error': error,
                },
            )

    if failures:
        # Non-2xx from the view → Cloud Tasks retries the whole
        # run; the hash check keeps retries idempotent.
        raise TranslationError(
            f'translation failed for {sorted(failures)}'
        )
    return OUTCOME_TRANSLATED


def retranslate_failed(note):
    """Re-mark a note's ``failed`` rows ``pending`` and re-enqueue.

    Returns the number of rows re-pended (0 → nothing enqueued —
    entries in other states stay untouched).
    """
    source_hash = hash_note_text(note.note_text)
    requeued = note.translations.filter(
        status=NoteTranslation.STATUS_FAILED,
    ).update(
        status=NoteTranslation.STATUS_PENDING,
        source_hash=source_hash,
        error='',
    )
    if requeued:
        enqueue(note.pk, source_hash)
    return requeued
