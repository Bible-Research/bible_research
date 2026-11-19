"""Tests for the note-translation pipeline: model, service,
worker endpoint, enqueue hooks, API contract, verify/retranslate,
management commands."""
import json
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import Note, NoteTranslation
from .services import tasks as note_tasks
from .services import translation_service as ts

User = get_user_model()

TIPDOC = json.dumps({
    "type": "doc",
    "content": [
        {
            "type": "paragraph",
            "content": [
                {
                    "type": "text",
                    "text": "See @JHN.3.16 and @JHN.3:16-17",
                    "marks": [{"type": "bold"}],
                },
                {
                    "type": "scriptureRef",
                    "attrs": {"ref": "JHN.3.16"},
                },
                {
                    "type": "mention",
                    "attrs": {"id": "x", "label": "m"},
                },
                {"type": "text", "text": " after."},
            ],
        }
    ],
})

PLAIN = "A plain legacy note about @JHN.3.16."


class _FakeTranslation:
    def __init__(self, text, detected='lv'):
        self.translated_text = text
        self.detected_language_code = detected


class _FakeResponse:
    def __init__(self, contents, detected='lv'):
        self.translations = [
            _FakeTranslation(f'[{detected}-out] {c}', detected)
            for c in contents
        ]


class FakeTranslateClient:
    """Mimics TranslationServiceClient.translate_text."""

    def __init__(self, detected='lv', fail_targets=()):
        self.detected = detected
        self.fail_targets = set(fail_targets)
        self.calls = []

    def translate_text(self, request=None, **kwargs):
        self.calls.append(request)
        target = request['target_language_code']
        if target in self.fail_targets:
            raise RuntimeError('upstream exploded')
        contents = request['contents']
        detected = request.get('source_language_code') or \
            self.detected
        return _FakeResponse(contents, detected)


def _patch_client(client):
    return patch.object(ts, '_client', return_value=client)


def _note(user, text=TIPDOC, **kwargs):
    return Note.objects.create(
        user=user, note_text=text, **kwargs
    )


class TranslationServiceTest(TestCase):
    """Task 2.1: structural TipDoc translation + masking."""

    def setUp(self):
        self.client_obj = FakeTranslateClient(detected='lv')

    def test_tiptap_structure_and_tokens_round_trip(self):
        with _patch_client(self.client_obj):
            out, detected = ts.translate_note_text(
                TIPDOC, 'en', source_language=None,
            )
        self.assertEqual(detected, 'lv')
        doc = json.loads(out)
        para = doc['content'][0]
        # Non-text atoms untouched; marks preserved.
        self.assertEqual(
            para['content'][1],
            {"type": "scriptureRef", "attrs": {"ref": "JHN.3.16"}},
        )
        self.assertEqual(para['content'][2]['type'], 'mention')
        first = para['content'][0]
        self.assertEqual(first['marks'], [{"type": "bold"}])
        # Tokens restored verbatim inside the translated text.
        self.assertIn('@JHN.3.16', first['text'])
        self.assertIn('@JHN.3:16-17', first['text'])
        self.assertIn('[lv-out]', first['text'])
        # ONE translate call for the whole doc batch.
        self.assertEqual(len(self.client_obj.calls), 1)
        self.assertEqual(
            self.client_obj.calls[0]['target_language_code'], 'en'
        )

    def test_plain_text_fallback(self):
        with _patch_client(self.client_obj):
            out, _ = ts.translate_note_text(
                PLAIN, 'en', source_language='lv',
            )
        self.assertIn('@JHN.3.16', out)
        self.assertIn('[lv-out]', out)

    @override_settings(NOTE_TRANSLATE_MAX_CHARS=10)
    def test_char_cap_raises(self):
        with _patch_client(self.client_obj):
            with self.assertRaises(ts.NoteTooLongError):
                ts.translate_note_text(TIPDOC, 'en')

    @override_settings(NOTE_TRANSLATE_MAX_CHARS=10)
    def test_plain_char_cap_raises(self):
        with _patch_client(self.client_obj):
            with self.assertRaises(ts.NoteTooLongError):
                ts.translate_note_text(PLAIN, 'en')

    def test_empty_text_skips_api(self):
        out, det = ts.translate_note_text('   ', 'en')
        self.assertEqual(out, '   ')
        self.assertIsNone(det)
        self.assertEqual(self.client_obj.calls, [])


class UsageCounterTest(TestCase):
    """Task 2.5: monthly usage counter increments."""

    def test_usage_counter_incremented(self):
        with _patch_client(FakeTranslateClient()), patch.object(
            ts.gcs, 'increment_monthly_usage', return_value=42,
        ) as inc:
            ts.translate_note_text(PLAIN, 'en', 'lv')
        billed = len(ts.mask_tokens(PLAIN)[0])
        inc.assert_called_once_with('note-translate', billed)


class _BaseCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='alice', password='pw',
        )
        self.other = User.objects.create_user(
            username='bob', password='pw',
        )
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def _create_note_via_api(self, text=TIPDOC):
        """POST a note; ``tag`` is required by the write
        serializer."""
        from .models import Tag
        tag = Tag.objects.create(user=self.user, name='t')
        return self.api.post('/api/v1/notes/', {
            'note_text': text,
            'tag': tag.id,
        }, format='json')


class EnqueueHooksTest(_BaseCase):
    """Task 2.4: perform_create/update/bulk enqueue behavior."""

    def setUp(self):
        super().setUp()
        # views imported enqueue by name — patch it there.
        self.view_patcher = patch(
            'annotations.views.enqueue'
        )
        self.view_enqueue = self.view_patcher.start()
        self.addCleanup(self.view_patcher.stop)

    def test_create_enqueues(self):
        with self.captureOnCommitCallbacks(execute=True):
            resp = self._create_note_via_api()
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(self.view_enqueue.call_count, 1)
        note = Note.objects.get()
        # Pending rows exist for every configured target.
        langs = set(
            NoteTranslation.objects.values_list(
                'language', flat=True,
            )
        )
        self.assertEqual(langs, {'en', 'lv'})
        self.assertTrue(
            all(
                t.status == 'pending'
                for t in NoteTranslation.objects.all()
            )
        )
        self.assertEqual(note.translations.count(), 2)

    def test_tag_only_patch_enqueues_nothing(self):
        note = _note(self.user)
        resp = self.api.patch(
            f'/api/v1/notes/{note.id}/', {'public': True},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.view_enqueue.call_count, 0)

    def test_text_patch_enqueues(self):
        note = _note(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.api.patch(
                f'/api/v1/notes/{note.id}/',
                {'note_text': 'New text'},
                format='json',
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.view_enqueue.call_count, 1)

    def test_identical_text_patch_enqueues_nothing(self):
        note = _note(self.user, text=PLAIN)
        resp = self.api.patch(
            f'/api/v1/notes/{note.id}/',
            {'note_text': PLAIN},
            format='json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.view_enqueue.call_count, 0)

    def test_bulk_create_enqueues_per_note(self):
        from .models import Tag
        tag = Tag.objects.create(user=self.user, name='t')
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.api.post('/api/v1/notes/bulk/', {
                'tag_id': tag.id,
                'notes': [
                    {'note_text': 'one'},
                    {'note_text': 'two'},
                ],
            }, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(self.view_enqueue.call_count, 2)


class LocalInlineWorkerTest(_BaseCase):
    """Task 2.2: local dev runs the worker inline — no GCP."""

    def test_create_translates_inline(self):
        client_obj = FakeTranslateClient(detected='lv')
        with _patch_client(client_obj):
            with self.captureOnCommitCallbacks(execute=True):
                resp = self._create_note_via_api()
        self.assertEqual(resp.status_code, 201)
        note = Note.objects.get()
        note.refresh_from_db()
        self.assertEqual(note.source_language, 'lv')
        rows = {
            t.language: t for t in note.translations.all()
        }
        self.assertEqual(rows['lv'].status, 'source')
        self.assertEqual(rows['lv'].text, '')
        self.assertEqual(rows['en'].status, 'ready')
        self.assertIn('[lv-out]', rows['en'].text)


class WorkerEndpointTest(_BaseCase):
    """Task 2.3: OIDC auth boundary + stale-hash no-op."""

    URL = '/api/v1/tasks/translate-note/'

    def _post(self, body=None, headers=None):
        return self.client.post(
            self.URL,
            data=body or {
                'note_id': 'NOTX', 'source_hash': 'h',
            },
            format='json',
            **(headers or {}),
        )

    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_missing_oidc_401(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 401)

    def test_bad_oidc_401(self):
        with patch(
            'google.oauth2.id_token.verify_oauth2_token',
            side_effect=ValueError('bad token'),
        ):
            resp = self._post(headers={
                'HTTP_AUTHORIZATION': 'Bearer junk',
            })
        self.assertEqual(resp.status_code, 401)

    def test_wrong_email_401(self):
        with patch(
            'google.oauth2.id_token.verify_oauth2_token',
            return_value={'email': 'intruder@x.iam'},
        ):
            resp = self._post(headers={
                'HTTP_AUTHORIZATION': 'Bearer ok',
            })
        self.assertEqual(resp.status_code, 401)

    def _claims(self):
        return patch(
            'google.oauth2.id_token.verify_oauth2_token',
            return_value={
                'email': 'tasks-invoker@bible-research-489314.'
                         'iam.gserviceaccount.com',
            },
        )

    @override_settings(
        TASKS_INVOKER_SA_EMAIL=(
            'tasks-invoker@bible-research-489314.'
            'iam.gserviceaccount.com'
        ),
    )
    def test_stale_hash_noop_204(self):
        note = _note(self.user, text='fresh text')
        stale = '0' * 64
        with self._claims():
            resp = self.client.post(self.URL, {
                'note_id': note.id,
                'source_hash': stale,
            }, format='json', HTTP_AUTHORIZATION='Bearer ok')
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(note.translations.count(), 0)

    @override_settings(
        TASKS_INVOKER_SA_EMAIL=(
            'tasks-invoker@bible-research-489314.'
            'iam.gserviceaccount.com'
        ),
    )
    def test_successful_run_writes_rows(self):
        note = _note(self.user, text=TIPDOC)
        source_hash = ts.hash_note_text(TIPDOC)
        client_obj = FakeTranslateClient(detected='lv')
        with self._claims(), _patch_client(client_obj):
            resp = self.client.post(self.URL, {
                'note_id': note.id,
                'source_hash': source_hash,
            }, format='json', HTTP_AUTHORIZATION='Bearer ok')
        self.assertEqual(resp.status_code, 200)
        note.refresh_from_db()
        self.assertEqual(note.source_language, 'lv')
        rows = {t.language: t for t in note.translations.all()}
        self.assertEqual(rows['lv'].status, 'source')
        self.assertEqual(rows['lv'].text, '')
        self.assertEqual(rows['en'].status, 'ready')
        self.assertEqual(rows['en'].engine, 'google-nmt')

    @override_settings(
        TASKS_INVOKER_SA_EMAIL=(
            'tasks-invoker@bible-research-489314.'
            'iam.gserviceaccount.com'
        ),
    )
    def test_failure_marks_failed_and_500s(self):
        note = _note(self.user, text=TIPDOC)
        source_hash = ts.hash_note_text(TIPDOC)
        client_obj = FakeTranslateClient(
            detected='lv', fail_targets={'en'},
        )
        with self._claims(), _patch_client(client_obj):
            resp = self.client.post(self.URL, {
                'note_id': note.id,
                'source_hash': source_hash,
            }, format='json', HTTP_AUTHORIZATION='Bearer ok')
        self.assertEqual(resp.status_code, 500)
        rows = {t.language: t for t in note.translations.all()}
        self.assertEqual(rows['en'].status, 'failed')
        self.assertEqual(rows['en'].error, 'translate_failed')

    @override_settings(
        TASKS_INVOKER_SA_EMAIL=(
            'tasks-invoker@bible-research-489314.'
            'iam.gserviceaccount.com'
        ),
        NOTE_TRANSLATE_MAX_CHARS=10,
    )
    def test_too_long_marks_failed_no_retry(self):
        note = _note(self.user, text=TIPDOC)
        source_hash = ts.hash_note_text(TIPDOC)
        with self._claims(), _patch_client(FakeTranslateClient()):
            resp = self.client.post(self.URL, {
                'note_id': note.id,
                'source_hash': source_hash,
            }, format='json', HTTP_AUTHORIZATION='Bearer ok')
        self.assertEqual(resp.status_code, 204)
        rows = {t.language: t for t in note.translations.all()}
        self.assertTrue(
            all(t.status == 'failed' for t in rows.values())
        )
        self.assertTrue(
            all(t.error == 'too_long' for t in rows.values())
        )


class LangServingTest(_BaseCase):
    """Task 3.1: ?lang= swap on detail + list + fallback."""

    def setUp(self):
        super().setUp()
        self.note = _note(
            self.user, text=TIPDOC, source_language='lv',
        )
        self.note.refresh_from_db()
        h = ts.hash_note_text(TIPDOC)
        NoteTranslation.objects.create(
            note=self.note, language='lv',
            status='source', text='', source_hash=h,
        )
        NoteTranslation.objects.create(
            note=self.note, language='en',
            status='ready', text='{"type":"doc","content":[]}',
            source_hash=h, engine='google-nmt',
        )

    def test_detail_lang_serves_translation(self):
        resp = self.api.get(
            f'/api/v1/notes/{self.note.id}/?lang=en'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.data['note_text'],
            '{"type":"doc","content":[]}',
        )
        self.assertEqual(resp.data['served_language'], 'en')

    def test_detail_source_lang_serves_original(self):
        resp = self.api.get(
            f'/api/v1/notes/{self.note.id}/?lang=lv'
        )
        self.assertEqual(resp.data['note_text'], TIPDOC)
        self.assertEqual(resp.data['served_language'], 'lv')

    def test_detail_missing_lang_falls_back(self):
        resp = self.api.get(
            f'/api/v1/notes/{self.note.id}/?lang=de'
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['note_text'], TIPDOC)
        self.assertEqual(resp.data['served_language'], 'lv')
        self.assertEqual(len(resp.data['translations']), 2)

    def test_list_lang_swaps_page(self):
        other = _note(
            self.user, text='plain second note',
            source_language='en',
        )
        resp = self.api.get('/api/v1/notes/?lang=en')
        results = {
            n['id']: n for n in resp.data['results']
        }
        self.assertEqual(
            results[self.note.id]['note_text'],
            '{"type":"doc","content":[]}',
        )
        # No 'en' ready row for the second note — but it IS
        # 'en' source → original text.
        self.assertEqual(
            results[other.id]['note_text'], 'plain second note'
        )

    def test_metadata_in_payload(self):
        resp = self.api.get(f'/api/v1/notes/{self.note.id}/')
        self.assertEqual(resp.data['source_language'], 'lv')
        meta = {
            t['language']: t for t in resp.data['translations']
        }
        self.assertEqual(meta['en']['status'], 'ready')
        self.assertEqual(meta['en']['engine'], 'google-nmt')
        self.assertFalse(meta['en']['verified'])


class VerifyEndpointTest(_BaseCase):
    """Task 3.2: owner-only verify endpoint contract."""

    def setUp(self):
        super().setUp()
        self.note = _note(self.user, text=TIPDOC)
        h = ts.hash_note_text(TIPDOC)
        self.ready = NoteTranslation.objects.create(
            note=self.note, language='en', status='ready',
            text='x', source_hash=h, engine='google-nmt',
        )
        self.url = (
            f'/api/v1/notes/{self.note.id}'
            '/translations/en/verify/'
        )

    def test_owner_sets_verified(self):
        resp = self.api.post(self.url)
        self.assertEqual(resp.status_code, 200)
        self.ready.refresh_from_db()
        self.assertTrue(self.ready.author_verified)
        self.assertIsNotNone(self.ready.verified_at)

    def test_owner_clears_verified(self):
        self.api.post(self.url)
        resp = self.api.delete(self.url)
        self.assertEqual(resp.status_code, 200)
        self.ready.refresh_from_db()
        self.assertFalse(self.ready.author_verified)
        self.assertIsNone(self.ready.verified_at)

    def test_non_owner_404(self):
        api = APIClient()
        api.force_authenticate(user=self.other)
        self.assertEqual(api.post(self.url).status_code, 404)
        self.assertEqual(api.delete(self.url).status_code, 404)

    def test_missing_row_404(self):
        resp = self.api.post(
            f'/api/v1/notes/{self.note.id}'
            '/translations/de/verify/'
        )
        self.assertEqual(resp.status_code, 404)

    def test_pending_row_409(self):
        NoteTranslation.objects.filter(pk=self.ready.pk).update(
            status='pending',
        )
        resp = self.api.post(self.url)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            resp.data['error_code'], 'translation_not_ready',
        )

    def test_source_row_409(self):
        NoteTranslation.objects.filter(pk=self.ready.pk).update(
            status='source', language='lv',
        )
        resp = self.api.post(
            f'/api/v1/notes/{self.note.id}'
            '/translations/lv/verify/'
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            resp.data['error_code'], 'translation_not_ready',
        )


class RetranslateEndpointTest(_BaseCase):
    """Task 3.3: owner retry of failed rows."""

    def setUp(self):
        super().setUp()
        self.note = _note(self.user, text=TIPDOC)
        h = ts.hash_note_text(TIPDOC)
        self.failed = NoteTranslation.objects.create(
            note=self.note, language='en', status='failed',
            error='x', source_hash=h,
        )
        NoteTranslation.objects.create(
            note=self.note, language='lv', status='source',
            source_hash=h,
        )
        self.url = f'/api/v1/notes/{self.note.id}/retranslate/'

    def test_failed_rows_repend_and_enqueue(self):
        with patch(
            'annotations.services.tasks.enqueue'
        ) as enq:
            resp = self.api.post(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['requeued'], 1)
        self.failed.refresh_from_db()
        self.assertEqual(self.failed.status, 'pending')
        enq.assert_called_once()
        # The 'source' row stays untouched.
        self.assertEqual(
            self.note.translations.get(language='lv').status,
            'source',
        )

    def test_non_owner_404(self):
        api = APIClient()
        api.force_authenticate(user=self.other)
        self.assertEqual(api.post(self.url).status_code, 404)


class TranslateNotesCommandTest(_BaseCase):
    """Task 4.4: translate_notes --user scoping."""

    def test_user_scope_only(self):
        from django.core.management import call_command
        mine = _note(self.user, text='mine')
        theirs = _note(self.other, text='theirs')
        client_obj = FakeTranslateClient(detected='lv')
        with _patch_client(client_obj):
            call_command(
                'translate_notes', '--user', 'alice',
            )
        self.assertTrue(mine.translations.exists())
        self.assertFalse(theirs.translations.exists())


class CheckNoteTranslationsTest(_BaseCase):
    """Task 4.5: coverage audit Sentry behavior."""

    def _covered_note(self, user):
        note = _note(user, text=TIPDOC, source_language='lv')
        h = ts.hash_note_text(TIPDOC)
        NoteTranslation.objects.create(
            note=note, language='lv', status='source',
            source_hash=h,
        )
        NoteTranslation.objects.create(
            note=note, language='en', status='ready',
            source_hash=h, text='t',
        )
        return note

    def test_clean_run_no_sentry(self):
        self._covered_note(self.user)
        from django.core.management import call_command
        with patch('sentry_sdk.capture_message') as cap:
            call_command('check_note_translations')
        cap.assert_not_called()

    def test_misses_emit_sentry(self):
        # A bare note authored in 'en' with no rows misses only
        # the 'lv' target — 'en' is satisfied by the source.
        _note(self.user, text='uncovered', source_language='en')
        self._covered_note(self.user)
        from django.core.management import call_command
        with patch('sentry_sdk.capture_message') as cap:
            call_command('check_note_translations')
        self.assertEqual(cap.call_count, 1)
        args, kwargs = cap.call_args
        self.assertIn('lv=1', args[0])
        self.assertEqual(kwargs.get('level'), 'error')
