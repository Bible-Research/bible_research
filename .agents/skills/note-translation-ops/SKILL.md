---
name: note-translation-ops
description: >
  Ops runbook for the note-translation pipeline — how the
  automatic enqueue path works, how to run partial or full
  backfills locally against prod (DOTENV + CA bundle recipe),
  how to trigger the monthly coverage audit manually, how to
  retry failed rows, and the kill switch. Use when asked to
  backfill, audit, repair, or monitor note translations.
---

# Note-translation ops runbook

Pipeline: note create/edit → `transaction.on_commit` upserts
`pending` `NoteTranslation` rows for `NOTE_TARGET_LANGUAGES`
→ Cloud Tasks POSTs `POST /api/v1/tasks/translate-note/` (OIDC
verified against `TASKS_INVOKER_SA_EMAIL`) → worker detects
`source_language`, calls Cloud Translation, upserts `ready`
rows. Local dev (`not IS_GCP_ENVIRONMENT`) runs the worker
inline — no GCP infra needed. Every write resets
`author_verified`; `source_hash` makes re-runs idempotent.

## Manual backfill — `translate_notes`

Filters compose: `--user USERNAME`, `--tag TAG_ID`,
`--note-id NOTE…`, `--dry-run` (list only). Bare invocation =
all notes. Rows already `ready` for every target are skipped
(the worker no-ops on a matching `source_hash`), so re-runs
are cheap and safe.

### Running it against prod from a laptop

The command writes `NoteTranslation` rows, so it needs the
app's writable DB credentials — pull them from the shared
`env-file` secret (the same mechanism `deploy.yml` uses for
migrations):

```bash
cd bible_research
python3.12 -m venv venv              # NOT 3.14 — see gotchas
venv/bin/pip install -r requirements.txt
export DOTENV="$(gcloud secrets versions access latest \
  --secret=env-file --project=bible-research-489314)"
export SSL_CERT_FILE="$HOME/.config/devin/combined-ca.pem"
export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
export CURL_CA_BUNDLE="$SSL_CERT_FILE"
export GRPC_DEFAULT_SSL_ROOTS_FILE_PATH="$SSL_CERT_FILE"

venv/bin/python manage.py translate_notes --dry-run       # count
venv/bin/python manage.py translate_notes --tag TAGxxxxx  # run
```

- `DOTENV` injects `DATABASE_URL` (prod `defaultdb`, writable
  app user), `DB_SSL_CERT`, `NOTE_TARGET_LANGUAGES`,
  `TASKS_INVOKER_SA_EMAIL`. Real env vars win over DOTENV
  values — ensure no `DATABASE_LOCAL_URL`/`DATABASE_READ_URL`
  is set in your shell, or the local-resolution chain
  silently redirects the "against prod" run elsewhere.
- Cloud Translation auth = your user ADC
  (`gcloud auth application-default login` once). The appspot
  SA's `cloudtranslate.user` role is not needed locally — your
  account's permissions apply.
- Every note does one Translate call per target; watch the
  `note-translate billed N chars into <lang>` log lines.

### Sizing a partial backfill

`--user` is the only corpus-shaped filter besides `--tag`;
there is no `--limit`. Check distribution first (read-only
`ai_agent` works):

```sql
SELECT u.username, COUNT(*), SUM(LENGTH(n.note_text))
FROM annotations_note n JOIN auth_user u ON u.id = n.user_id
GROUP BY u.username ORDER BY 2 DESC;
```

Cost: Cloud Translation ≈ $20 / 1M chars with a 500k
chars/month free tier. Usage accrues in the monthly counter
(`fileset_id='note-translate'` via
`increment_monthly_usage`).

## Coverage audit — `check_note_translations`

Verifies every note has a `ready`/`source` row per configured
target with a matching `source_hash`; emits a Sentry
`capture_message` (ERROR) with per-language/status counts on
misses. Scheduled automatically: `note-translations-audit`
Cloud Scheduler → `all-jobs` Cloud Run Job on days 28-31, the
`last_day_of_month` JOBS predicate gates to the real last day.

Manual trigger options:

```bash
# In GCP (runs through run_all_jobs --only):
gcloud run jobs execute all-jobs \
  --region=europe-west3 --project=bible-research-489314 \
  --args="--only,check_note_translations"

# Locally (same env recipe as the backfill):
venv/bin/python manage.py check_note_translations
```

## Retrying failures

- User-facing: owner `POST /api/v1/notes/<id>/retranslate/`
  re-enqueues `failed` rows only (frontend retry button).
- Ops: re-run `translate_notes` scoped the same way — only
  non-`ready` rows do work.

## Kill switch and config

All settings resolve via the `_audio_settings_source` chain
(`DOTENV`/`DOTENV_PATH` payload > local `.env` >
legacy `config.yaml`):

- `NOTE_TARGET_LANGUAGES` (default `["en","lv"]`) — empty =
  pipeline off. Prod value lives in `env-file` as `en,lv`.
- `TASKS_INVOKER_SA_EMAIL` — MUST be set in `env-file`; empty
  breaks OIDC verify (all worker calls 401).
- `TASK_QUEUE_ID`/`TASK_QUEUE_LOCATION`/`APP_BASE_URL`/
  `NOTE_TRANSLATION_ENGINE`/`NOTE_TRANSLATE_MAX_CHARS` —
  defaults match prod; only override for a different
  environment.

## Gotchas learned running this in prod (2026-11)

- **Python 3.14 breaks on the Netskope CA**: its strict
  X509 checks reject the proxy root (no keyUsage ext) —
  `SSLCertVerificationError ... key usage extension` on
  `oauth2.googleapis.com`. Use python3.12 (also matches
  `app.yaml` python312).
- **Stale CA bundle**: rebuild the combined bundle if TLS
  still fails —
  `cat $(venv/bin/python -c 'import certifi;print(certifi.where())') \
   "/Library/Application Support/Netskope/STAgent/data/nscacert.pem" \
   > /tmp/combined-ca.pem` — and point the five env vars at
  the fresh file.
- **Local terraform needs the same CA vars +**
  `TF_VAR_alert_email_address` (else plan proposes destroying
  the monitoring channel — see `terraform/` note in
  AGENTS.md).
- **`terraform apply` is CI's job** — the `Terraform` workflow
  applies on `main` push; run it locally only when CI's
  deployer SA can't (IAM bootstrap cases) and with the user's
  explicit go-ahead.
- The `en` row on each note is `status='source'` with EMPTY
  `text` — a metadata anchor, not a bug; the original is
  served from `note_text`.
