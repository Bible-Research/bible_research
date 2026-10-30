# AGENTS.md — bible_research (Django REST API)

Guidance for AI agents working in this repository. Read this before
editing; trust the code over any doc that disagrees with it.

## What this is

Django 6.1 + Django REST Framework API serving a Bible reading/study
app (`reactive-bible` frontend). Deployed on Google App Engine at
`https://bible-research-489314.ey.r.appspot.com`; PostgreSQL in prod.
Local dev's `config.yaml` also points at prod via a read-only user;
tests run on SQLite.

## Layout

- `bible/` — passage text, audio, search, translations, timestamps.
- `annotations/` — notes, tags, comments, images, reading positions.
- `users/` — auth/token endpoints.
- `bible_research/` — settings, urls, authentication.
- `terraform/` — GCP infra (the `all-jobs` Cloud Run job, storage;
  see note below).
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

`provider_for` wraps ESV/SWORD/DBT providers in
`_CachingProvider`: whole-chapter text is served from the
process-wide LRU in `bible/services/chapter_cache.py` (24h TTL,
500-entry bound, 5-min negative caching of
`PassageNotFoundError`, per-translation verse budgets for
licensor caps — `ENGESV`/`ENGESV_API` share `esv`, public-domain
`ENGKJV`/`LVSGLU8` are exempt). Verse-range hints are never
forwarded upstream on a miss (a narrowed fetch must not fill
the chapter entry); `ApiBibleProvider` is returned unwrapped
(FUMS `meta` tokens must be reported per display). Returned
payloads are shared — never mutate them.

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

## Sentry event streams

Project `bible-research-api` (org `bible-research`, de.sentry.io);
init lives in `bible_research/settings.py` and is DSN-gated — no
DSN, no traffic. What each stream means:

- `DjangoIntegration` captures unhandled 500s at level error —
  real bugs.
- `capture_internal_exception` (in
  `bible/utils/provider_errors.py`) reports caught server bugs at
  level error, tagged `error_code:internal_error` — real bugs,
  agent-fixable.
- `capture_provider_exception` reports upstream outages at level
  warning, tagged `error_code` in {`rate_limited`,
  `provider_error`} — the `_CAPTURED_ERROR_CODES` allowlist, so
  `not_found`/`book_not_in_fileset` are never sent. Trend-only;
  NOT auto-fix candidates.
- `LoggingIntegration(level=INFO, event_level=None)` — INFO+
  log records are breadcrumbs only, never events; DEBUG is
  dropped entirely.
- `sentry_before_send` (`bible_research/utils.py`) strips
  `request.data` on `/api/token` and `/api/v1/users/` paths
  (password-bearing bodies).

The agent-fix queue for this project is `is:unresolved
level:error environment:production` — warning-level provider
events are excluded by the level filter. Events carry
`environment:production` on GCP and `development` elsewhere,
with `release` = `GAE_VERSION`, so the environment filter
skips dev noise and `resolvedInNextRelease` resolves an issue
at the next GAE deploy.

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

Pending human-run `makemigrations` (Meta declared, no migration
file yet): `unique_top_level_tag_name_per_user` on Tag
(`(user, name)` where `parent_tag IS NULL`),
`unique_note_verse_link` on NoteVerse (`note, verse`),
`unique_book_chapter_verse` on Verse (`book, chapter, verse`).
Serializers enforce all three application-side.

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

conftest.py imports the real `bible_research.settings` and
overrides only the test deltas; tests run on SQLite in-memory —
no `config.yaml`, no DB setup needed, and they never touch the
production database. `manage.py test` also stays off prod:
`settings.py` overrides `DATABASES` to local SQLite
(`db.sqlite3`) whenever `'test'` is in `sys.argv`.

Test user accounts: `testuser`, `guest` — create via
`python scripts/create_test_user.py` if a local DB lacks them
(pytest's in-memory DB creates what it needs per test).

Known pre-existing failure on `main` (not a regression):
`test_single_query_assertion` — `@expectedFailure`.

Known pinned prod bugs (FIXME-marked tests). Each pins current
buggy behavior pending a product decision — these are latent
code bugs, not test failures: none outstanding. Recently
resolved:

- ~~`annotations/test_reading_positions.py`
  (`test_case_variant_books_create_separate_rows`)~~ — book
  names canonicalize through `get_dbt_book_id` +
  `get_book_name_from_id` before storage; `book` is immutable
  after create (serializer `update()` → 400).
- ~~`annotations/test_tag_isolation.py`
  (`test_duplicate_name_same_user`)~~ — `TagSerializer.validate`
  enforces top-level (user, name) uniqueness → 400, backed by
  the `unique_top_level_tag_name_per_user` conditional
  constraint (pending its migration).
- ~~`annotations/test_note_isolation.py`
  (`test_untagged_order_index`)~~ — order-index resolves the
  `untagged` sentinel to `tag__isnull=True`, matching list and
  `/comments/counts/` semantics.
- ~~`annotations/test_images.py`
  (`test_note_images_list_private_foreign_403`)~~ — resolved:
  `NoteImageViewSet._get_note` and `CommentImageViewSet.
  _get_comment` now resolve parents through
  `get_accessible_notes_qs`, so inaccessible and nonexistent
  parents both 404 (the test asserts 404 as
  `test_note_images_list_private_foreign_404`).
- ~~`annotations/test_images.py`
  (`test_cascade_delete_orphans_gcs_blob`)~~ — a `post_delete`
  receiver on Image calls `delete_original`, so cascade
  deletes (note/tag/user) no longer orphan GCS blobs.

## Housekeeping

- Max line length is 79 chars — every file type, enforced.
- NEVER run `migrate`/`makemigrations`/`sqlmigrate` — workspace rule.
- Commit format: `Type: Capitalized message`; never commit to `main`;
  stage only files you changed.
- `terraform/scheduler.tf`: the *monthly* Google TTS Cloud
  Scheduler stays intentionally commented out — do NOT enable it.
  The `daily-edge-audio` job IS enabled: it triggers `all-jobs`
  daily with a `jobs:run` container-args override of `--only
  generate_edge_audio`, so the monthly `dom:3` entry cannot
  piggyback on the trigger.
- `terraform/all_jobs.tf`: the single `all-jobs` Cloud Run Job
  replaced the per-task `audio-generator` job (it reuses that SA and
  image repo, both still named `audio-generator`). Sub-jobs are
  listed in the `JOBS` table of
  `bible/management/commands/run_all_jobs.py` —
  `generate_chapter_audio` (`dom:3`, unscheduled),
  `generate_gemini_audio` (`manual`) and `generate_edge_audio`
  (`manual`; started only by the daily scheduler's `--only`
  override — the Everita voice). Generated artifacts land under
  `audio_gemini/` and `audio_edge/` in the audio bucket (GCS
  modes `"gemini"`/`"edge"` in `bible/services/storage/gcs.py`)
  with day-keyed lock objects under `state/`; no fileset serves
  them yet.
- `bible/management/commands/generate_gemini_audio.py` and
  `generate_edge_audio.py`: local mode (default) writes
  `tts-out/` + `gemini-tts-state.json`; `--upload-gcs`
  (auto-on when `CLOUD_RUN_JOB` is set) switches state +
  artifacts to GCS. Gemini needs `GEMINI_API_KEY` in
  `config.yaml` locally or the `env-file` secret on GCP —
  normalized to `settings.GEMINI_API_KEYS`. The Edge command
  is keyless (unofficial Microsoft endpoint, default voice
  `lv-LV-EveritaNeural`, exact per-verse timestamps).
- Dependencies: `requirements.txt` pins the runtime stack to
  the versions installed in `venv/` (verify with `pip freeze`);
  `requirements-dev.txt` adds pytest/pytest-django/debugpy.
- `bible/services/dbt/dbt_client/` is a slimmed vendored
  OpenAPI client imported via a `sys.path` append in
  `dbt/client.py` — not a pip package; don't add it to
  requirements or remove the path patch.
- This machine sits behind a Netskope TLS-intercepting proxy
  (`ca.gocardless.goskope.com`): pip/requests/aiohttp/grpc all
  fail with "self signed certificate in certificate chain"
  unless pointed at a combined CA bundle. Build one:
  `cat $(venv/bin/python -c 'import certifi;print(certifi.where())') \
    "/Library/Application Support/Netskope/STAgent/data/nscacert.pem" \
    > /tmp/combined-ca.pem`
  (optionally append `security find-certificate -a -c gocardless
  -p`), then export `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` /
  `PIP_CERT` / `CURL_CA_BUNDLE` /
  `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` to it. gRPC also needs
  REST transport (`transport="rest"`) — the proxy strips ALPN.
  See `tts-out/voice-comparison/README.md` for a worked example.
- If docs and code disagree, the code wins — fix the doc.
