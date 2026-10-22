# AGENTS.md — bible_research (Django REST API)

Guidance for AI agents working in this repository. Read this before
editing; trust the code over any doc that disagrees with it.

## What this is

Django 5.2 + Django REST Framework API serving a Bible reading/study
app (`reactive-bible` frontend). Deployed on Google App Engine at
`https://bible-research-489314.ey.r.appspot.com`; PostgreSQL in prod.
Local dev's `config.yaml` also points at prod via a read-only user;
tests run on SQLite.

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
provider. All routing goes through the facade in
`bible/services/providers.py` — `provider_for(fileset_id)` returns
the provider object and callers use its uniform surface
(`get_chapter`, `get_passage`, `get_audio`, `get_timestamps`,
`search`, `search_grouped`, `get_copyright`,
`translation_listing`). `get_passage_body()` in the same module is
the `/api/v1/bible/` fetch+shape path the view calls; the
serializer only validates input. Dispatch order:

1. ESV filesets (`is_esv_fileset` in `bible/services/esv/registry.py`)
   → ESV API via `bible/services/esv/client.py`.
2. API.Bible filesets (`bible/services/apibible/registry.py`) →
   api.scripture.api.bible via `bible/services/apibible/client.py`.
3. SWORD filesets (`bible/services/sword/registry.py`) → local SWORD
   modules via pysword (`bible/services/sword/client.py`).
4. Everything else → DBT API v4 via `bible/services/dbt/client.py`.

Copyright routing uses the same facade: ESV/SWORD ids (and abbrs)
answer from registry `license` metadata, API.Bible ids fetch the
live statement (audio fileset ids read the audio bible's entry),
other ids proxy DBT. Note enrichment batches by
(fileset, book, chapter) through a serializer-context cache —
one upstream call per chapter per page, not per note.

Known fileset ids: `ENGKJV` (KJV), `ENGESV_API` (ESV), `LVSGLU8`
(Latvian Glück text), `LVSGLU8C1DA` (Latvian Glück generated audio,
verse-synthesis mode), `LVSGLU8C2DA` (same audio, chapter-synthesis
mode via SSML marks — see `audio_mode_for_fileset` in
`sword/registry.py`; artifacts live under GCS `audio/` vs
`audio_chapter/`), `ENGNIV_API` (API.Bible NIV text),
`ENGNIVC1DA` (NIV audio). `*DA`-suffixed ids are audio filesets.

API.Bible text responses carry a `meta` block with FUMS fields the
frontend must report (license requirement). The API.Bible client
needs `API_BIBLE_KEY` (settings/`config.yaml`); the registry and
translations listing work without it.

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
- `GET /api/v1/bible/languages/?search=&code=&page=&limit=`

`annotations/urls.py` (DRF router):
- `tags/`, `notes/` (supports `notes/reorder/` bulk update,
  `notes/order-index/`, `notes/move/`)
- `reading-positions/`
- `notes/<note_pk>/comments/`, `notes/<note_pk>/images/`,
  `notes/<note_pk>/comments/<comment_pk>/images/`
- `comments/counts/`, `images/<image_pk>/`

Docs: `/api/v1/docs/` (Swagger), `/api/v1/schema/`, `/api/v1/redoc/`.
Token login: `POST /api/token/`.

## Error contract

Provider failures surface as `error` + `error_code` fields built by
`bible/utils/provider_errors.py`: `rate_limited` → HTTP 429,
`not_found` (missing passage, upstream 404, or
`PassageNotFoundError`) → HTTP 404, `book_not_in_fileset`
(every provider's "fileset lacks this book/chapter" coverage
miss on the passage endpoint) → HTTP 404, anything else →
`provider_error` → HTTP 502 (see `bible/views.py`). Note
serialization degrades the same way — verse refs kept, `text`
empty. Client-input 400/404s in `bible/views.py` are built by
`bible/utils/error_responses.py` (`bad_request`/`not_found`,
codes `missing_parameter`/`invalid_parameter`), and routing ids
(`fileset_id`, `bible_id`, `language_iso`, `book`) are
charset-validated at the view boundary via
`bible/utils/request_params.py`. Keep field names stable; the
React app branches on `error_code`. Never embed `str(exc)` in
`error` — exception text can contain request URLs with
credentials.

## Auth quirks

- Token auth only: `bible_research/authentication.py` holds DRF
  `TokenAuthentication` (`Authorization: Token …`) plus
  `BearerTokenAuthentication` (`Authorization: Bearer …`). The
  DRF API no longer accepts cookie sessions (the old CSRF-exempt
  session auth was deleted); Django session auth still exists for
  `/admin` only.
- No auto-provisioning: anonymous requests arrive as a real
  `AnonymousUser` — `DeviceAndCountryMiddleware` and its
  header-derived per-device `User` rows are gone. Every write
  endpoint requires `IsAuthenticated` explicitly, so anonymous
  writes return 401 and `/me/` 401s for anonymous callers.
- The legacy `guest` account is read-only:
  `bible_research/permissions.py` `GuestReadOnly` (wired into
  `DEFAULT_PERMISSION_CLASSES` plus the image/reading-position
  views that override permissions) rejects its writes with 403.
  It exists only to own rows created before provisioning was
  removed and is reachable only via a previously issued token.
- Serializers scope tag/note querysets to `request.user`;
  anonymous callers get `.none()`. `GET
  /api/v1/comments/counts/` carries `permission_classes =
  [AllowAny]` so share pages can count public-note comments.

## Databases

- Local `config.yaml` `DATABASES.default` connects to the
  **production** Aiven PostgreSQL (`defaultdb`) as `ai_agent`, a
  **read-only** user — `manage.py runserver` and `manage.py shell`
  operate on live production data.
- Read-only investigation is expected and encouraged: ORM reads work
  as usual, and raw SQL via:

  ```python
  from django.db import connection
  with connection.cursor() as cur:
      cur.execute("SELECT ...")
  ```

- Writes and DDL fail with `InsufficientPrivilege` — never attempt
  them. `manage.py dbshell` needs a local `psql` client (not
  installed); prefer `manage.py shell` or a `psycopg2` script built
  on `settings.DATABASES`.
- For a writable local database, repoint `config.yaml`'s `default`
  to SQLite (`ENGINE: django.db.backends.sqlite3`,
  `NAME: db.sqlite3` — a file already exists in the repo root).

## Models / IDs

Prefixed string PKs: `TAG…`, `CMNT_…`, `IMG_…`, etc.
`ReadingPosition` upserts per user+book+chapter. Images attach to
notes and comments via nested routes. Positions use float
`tag_position`/`position` fields — reordering writes fractional
values; uniqueness collisions are handled in `annotations/views.py`.

## Code style & patterns

- Imports ordered: stdlib → Django → third-party → local.
- Docstrings: triple-quoted with Args/Returns on public methods.
- ViewSets for model CRUD; APIView for custom endpoints
  (`BiblePassageView`).
- Serializers split write-only input fields
  (`PrimaryKeyRelatedField`) from read-only `*_detail` fields;
  `to_representation` adds computed data (e.g. verse text).
- FKs: `CASCADE` for owned children, `PROTECT` for rows that
  must not disappear (verses); M2M uses explicit `through`
  models (`NoteVerse`).
- Validate input in serializer `validate_*` methods; enforce
  ownership in `perform_update`/`perform_destroy` via
  `PermissionDenied`; `perform_create` assigns the request user
  only — anonymous writes 401 via `IsAuthenticated`.
- Never: modify applied migrations, raw SQL in app code (use the
  ORM — ad-hoc read-only investigation is the exception),
  circular imports (use `get_user_model()`), secrets in code
  (`config.yaml` is gitignored).

## Testing

```bash
source venv/bin/activate
python -m pytest annotations/tests.py -q   # or a single class
```

conftest.py configures Django settings for pytest; tests run on
SQLite in-memory — no `config.yaml`, no DB setup needed, and they
never touch the production database. `manage.py test` also stays
off prod: `settings.py` overrides `DATABASES` to local SQLite
(`db.sqlite3`) whenever `'test'` is in `sys.argv`.
`bible/services/dbt/dbt_integration_test.py` hits the live DBT API —
skip unless you have a real `DBT_KEY`.

Test user accounts: `testuser`, `guest` — create via
`python scripts/create_test_user.py` if a local DB lacks them
(pytest's in-memory DB creates what it needs per test).

Known pre-existing failure on `main` (not a regression):
`test_single_query_assertion` — `@expectedFailure`.

Known pinned prod bugs (FIXME-marked tests). Each pins current
buggy behavior pending a product decision — these are latent
code bugs, not test failures:

- `annotations/test_reading_positions.py:179`
  (`test_case_variant_books_create_separate_rows`) —
  case-variant book names (`John` vs `john`) create duplicate
  `ReadingPosition` rows (serializers.py:907 + case-sensitive
  `unique_together`, models.py:460).
- `annotations/test_tag_isolation.py:131`
  (`test_duplicate_name_same_user`) — duplicate tag name with
  NULL `parent_tag` silently creates a second row with 201
  (`unique_together` at models.py:65 doesn't cover NULL
  parent; no clean 400).
- `annotations/test_note_isolation.py:231`
  (`test_untagged_order_index`) — order-index endpoint filters
  `tag_id` literally and never resolves the `untagged`
  sentinel, returning an empty list for untagged notes
  (views.py:1019 vs :590-592).
- ~~`annotations/test_images.py`
  (`test_note_images_list_private_foreign_403`)~~ — resolved:
  `NoteImageViewSet._get_note` and `CommentImageViewSet.
  _get_comment` now resolve parents through
  `get_accessible_notes_qs`, so inaccessible and nonexistent
  parents both 404 (the test asserts 404 as
  `test_note_images_list_private_foreign_404`).
- `annotations/test_images.py:1033`
  (`test_cascade_delete_orphans_gcs_blob`) — row-level CASCADE
  delete of a note/comment leaves orphaned GCS blobs; no
  `post_delete` receiver calls `delete_original`
  (models.py:303-318).

## Housekeeping

- Max line length is 79 chars — every file type, enforced.
- NEVER run `migrate`/`makemigrations`/`sqlmigrate` — workspace rule.
- Commit format: `Type: Capitalized message`; never commit to `main`;
  stage only files you changed.
- `terraform/scheduler.tf`: the monthly TTS Cloud Scheduler is
  intentionally commented out — do NOT enable it.
- Stale docs: `DEVELOPER_GUIDE.md` still references Django 4.2.6 and
  older structure; this file is the source of truth for agents.
