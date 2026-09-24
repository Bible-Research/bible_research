# AGENTS.md — bible_research (Django REST API)

Guidance for AI agents working in this repository. Read this before
editing; trust the code over any doc that disagrees with it.

## What this is

Django 5.2 + Django REST Framework API serving a Bible reading/study
app (`reactive-bible` frontend). Deployed on Google App Engine at
`https://bible-research-489314.ey.r.appspot.com`; PostgreSQL in prod,
SQLite for dev/tests.

## Layout

- `bible/` — passage text, audio, search, translations, timestamps.
- `annotations/` — notes, tags, comments, images, reading positions.
- `users/` — auth/token endpoints.
- `bible_research/` — settings, urls, middleware, authentication.
- `terraform/` — GCP infra (Cloud Run job, storage; see note below).
- `config.yaml` — local secrets (gitignored). Tests do NOT need it.

All API routes live under `/api/v1/` (see `bible_research/urls.py`).

## Provider routing (core business logic)

Every passage/audio request carries a `fileset_id` that selects the
provider. Routing happens in `bible/services/translation_service.py`:

1. ESV filesets (`is_esv_fileset` in `bible/services/esv/registry.py`)
   → ESV API via `bible/services/esv/client.py`.
2. SWORD filesets (`bible/services/sword/registry.py`) → local SWORD
   modules via pysword (`bible/services/sword/client.py`).
3. Everything else → DBT API v4 via `bible/services/dbt/client.py`.

Known fileset ids: `ENGKJV` (KJV), `ENGESV_API` (ESV), `LVSGLU8`
(Latvian Glück text), `LVSGLU8C1DA` (Latvian Glück generated audio).
`*DA`-suffixed ids are audio filesets.

Book-name handling lives in `bible/utils/bible_books.py`
(`get_dbt_book_id`, `get_book_name_from_id`, `get_testament`). There
is no `__init__.py` in `bible/utils/` — it is a namespace package.
`ReadingPositionSerializer.validate_book` validates names through
`get_dbt_book_id`.

## Endpoints

`bible/urls.py`:
- `GET /api/v1/bible/?passage=&fileset_id=[&response_format=audio]`
- `GET /api/v1/bible/translations/?language_iso=`
- `GET /api/v1/bible/timestamps/?fileset_id=&book=&chapter=`
- `GET /api/v1/bible/copyright/?bible_id=`
- `GET /api/v1/bible/search/?query=&fileset_id=&limit=&page=`

`annotations/urls.py` (DRF router):
- `tags/`, `notes/` (supports `notes/reorder/` bulk update)
- `reading-positions/`
- `notes/<note_pk>/comments/`, `notes/<note_pk>/images/`,
  `notes/<note_pk>/comments/<comment_pk>/images/`
- `comments/counts/`, `images/<image_pk>/`

Docs: `/api/v1/docs/` (Swagger), `/api/v1/schema/`, `/api/v1/redoc/`.
Token login: `POST /api/token/`.

## Error contract

Provider failures surface as `error` + `error_code` fields built by
`bible/utils/provider_errors.py`: `rate_limited` → HTTP 429,
anything else → `provider_error` → HTTP 502 (see
`bible/views.py`). Note serialization degrades the same way —
verse refs kept, `text` empty. Keep field names stable; the React
app branches on `error_code`. Never embed `str(exc)` in `error` —
exception text can contain request URLs with credentials.

## Auth quirks

- `bible_research/authentication.py`: DRF `TokenAuthentication`
  (`Authorization: Token …`) plus `BearerTokenAuthentication`
  (`Authorization: Bearer …`) and a CSRF-exempt session auth.
- `DeviceAndCountryMiddleware` auto-provisions/authenticates users
  from device + language headers and falls back to a `guest` user —
  anonymous requests are not necessarily unauthenticated.
- Serializers scope tag/note querysets to `request.user`, else to the
  `guest` user.

## Models / IDs

Prefixed string PKs: `TAG…`, `CMNT_…`, `IMG_…`, etc.
`ReadingPosition` upserts per user+book+chapter. Images attach to
notes and comments via nested routes. Positions use float
`tag_position`/`position` fields — reordering writes fractional
values; uniqueness collisions are handled in `annotations/views.py`.

## Testing

```bash
source venv/bin/activate
python -m pytest annotations/tests.py -q   # or a single class
```

conftest.py configures Django settings for pytest; tests run on
SQLite in-memory — no `config.yaml`, no DB setup needed.
`bible/services/dbt/dbt_integration_test.py` hits the live DBT API —
skip unless you have a real `DBT_KEY`.

Known pre-existing failures on `main` (not regressions):
`test_note_serializer`, `test_single_query_assertion`,
`test_position_already_occupied`.

## Housekeeping

- Max line length is 79 chars — every file type, enforced.
- NEVER run `migrate`/`makemigrations`/`sqlmigrate` — workspace rule.
- Commit format: `Type: Capitalized message`; never commit to `main`;
  stage only files you changed.
- `terraform/scheduler.tf`: the monthly TTS Cloud Scheduler is
  intentionally commented out — do NOT enable it.
- Stale docs: `DEVELOPER_GUIDE.md` still references Django 4.2.6 and
  older structure; this file is the source of truth for agents.
