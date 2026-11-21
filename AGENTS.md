# AGENTS.md — bible_research (Django REST API)

Guidance for AI agents working in this repository. Read this before
editing; trust the code over any doc that disagrees with it.
Cross-repo contracts are spec'd in the workspace-level `openspec/`
(see workspace AGENTS.md).

## What this is

Django 6.1 + Django REST Framework API serving a Bible reading/study
app (`reactive-bible` frontend). Deployed on Google App Engine at
`https://bible-research-489314.ey.r.appspot.com`; PostgreSQL in prod.
Local dev's `config.yaml` also points at prod via a read-only user;
tests run on SQLite.

## Layout

- `bible/` — passage text, audio, search, translations, timestamps.
- `annotations/` — notes, tags, comments, images, reading
  positions, and the note-translation pipeline
  (`services/translation_service.py` TipTap→Cloud Translation,
  `services/tasks.py` Cloud Tasks enqueue + inline local worker,
  `views_tasks.py` OIDC-protected worker endpoint,
  `Note.source_language` + `NoteTranslation` model).
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
`translation_listing`, `get_structure`).
`get_passage_body()` in the same module is
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
live statement (audio fileset ids read the audio bible's entry;
an unconfigured `API_BIBLE_KEY` degrades to the registry
`license`), other ids proxy DBT. Structure routing
(`/bible/structure/`)
goes through `bible/services/structure.py`: DBT filesets read
their own bible via `GET /bibles/{id}/book?verify_content=true`,
SWORD reads the local module + canonical-language names,
`ENGKJV` uses the bundled English table, and unanswerable
providers fall back to the canonical bible for the fileset's
`language_iso` (`eng`→`ENGESV`, `lvs`→`LAVNLI`,
`rus`→`RUSSYN`, default `ENGESV`); rows are filtered to the
66-book USFM canon. A failed SWORD canonical-name overlay
degrades instead of failing: the module's English names ship
with `degraded: true` and `no-store` — never cached anywhere
(same posture as `bible/translations/` degraded bodies). Note
enrichment batches by (fileset, book, chapter) through a
serializer-context cache — one upstream call per chapter per
page, not per note.

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

Every provider surface is gated to the 66-book canon
(`CANONICAL_BOOK_IDS` in `bible/utils/bible_books.py`, derived
from `_BIBLE_BOOKS`): `bible/search/` intersects the `books`
param with the canon — an empty intersection returns a
provider-shaped empty payload (paginated or grouped) with no
upstream call and no error, i.e. a coverage miss like
`book_not_in_fileset`, not a 400. `DbtProvider.search` and
`fetch_dbt_matches` always send `books=` upstream — the
request filter or the full sorted 66-id string
(`CANONICAL_BOOKS_PARAM`) — so DBT-reported totals count
canonical matches only; normalized rows are additionally
post-filtered through `filter_canonical_verses` on the DBT
and API.Bible paths (ESV/SWORD are canon-by-construction —
`get_dbt_book_id` refs and `_BIBLE_BOOKS`-mapped module
books). API.Bible has no upstream book filter, so on a
hypothetical deuterocanon bible its `total` could overcount
the rows actually returned — registered bibles are NIV-only
today. `get_passage_body` answers a non-canonical `book` id
as `book_not_in_fileset` before provider dispatch (the public
`passage=` path can't produce one — names resolve through
`get_dbt_book_id`).

Known fileset ids: `ENGKJV` (KJV), `ENGESV_API` (ESV), `LVSGLU8`
(Latvian Glück text), `LVSGLU8C1DA` (Latvian Glück generated audio,
verse-synthesis mode), `LVSGLU8C2DA` (same audio, chapter-synthesis
mode via SSML marks — see `audio_mode_for_fileset` in
`sword/registry.py`; artifacts live under GCS `audio/` vs
`audio_chapter/`), `ENGNIV_API` (API.Bible NIV text),
`ENGNIVC1DA` (NIV audio). `*DA`-suffixed ids are audio filesets.

`bible/services/dbt/timestamps_registry.py`
(`GENERATED_TIMESTAMPS_FILESETS`) lists DBT audio filesets with
no DBT timestamps (Latvian `LATLVR*`/`LATBSLP*`) whose verse
timestamps we generate via Cloud STT
(`generate_audio_timestamps`, `bible/services/google_stt/`).
`DbtProvider.get_timestamps` falls back to the
`timestamps_generated/` GCS artifact when DBT answers empty or
fails; `-opus16` ids strip the suffix to the base fileset.
Worklist source is `DBTClient.get_fileset_files` —
`GET /bibles/filesets/{fileset_id}` returns the fileset's real
per-chapter coverage with signed `path` + `duration` (unlike
`/bibles/{bible_id}/book`, which keys on the bible id and unions
sibling filesets; a `*DA` fileset id 404s there). The worklist
skips rows outside the 66-book canon — a deuterocanonical
fileset can't burn STT spend on chapters the SWORD reference
lacks, and `--book TOB` now yields an empty pending list.

`DBTClient` methods that hit `/bibles/{id}/book`
(`get_books`/`get_book`/`get_chapters`) use raw `session.get`
like `get_bibles`/`get_fileset_files` — the vendored OpenAPI
client's `v4_bible_books` deserialization is broken (its
`V4BibleBooksData` model references phantom types `Id`, `Name`,
`Chapters`… that were never generated) and it returns model
objects, not the dicts callers index into. Don't route new
book-list calls through `bibles_api`.

API.Bible text responses carry a `meta` block with FUMS fields the
frontend must report (license requirement). The API.Bible client
needs `API_BIBLE_KEY` (settings/`config.yaml`); the registry and
translations listing work without it.

Book-name handling lives in `bible/utils/bible_books.py`
(`get_dbt_book_id`, `get_book_name_from_id`, `get_testament`). There
is no `__init__.py` in `bible/utils/` — it is a namespace package.
`ReadingPositionSerializer.validate_book` validates names through
`get_dbt_book_id`.

`Verse.book` stores the seed file's spelling —
`esv_bible_verses.json` keys SNG as `Song of Songs` while the
canonical name clients send is `Song of Solomon`. Matching
stored rows on the client-supplied name must go through
`dbt_book_id` (`resolve_verse_refs`, `POST /notes/linked/`,
`?ordering=verse` book-order mapping); serializers and
`order-index` report the canonical title via
`get_book_name_from_id` so API responses always carry
`Song of Solomon`.

## Endpoints

`bible/urls.py`:
- `GET /api/v1/bible/?passage=&fileset_id=[&response_format=audio]`
- `GET /api/v1/bible/translations/?language_iso=`
- `GET /api/v1/bible/timestamps/?fileset_id=&book=&chapter=`
- `GET /api/v1/bible/copyright/?bible_id=`
- `GET /api/v1/bible/structure/?fileset_id=`
- `GET /api/v1/bible/search/?query=&fileset_id=&limit=&page=`
- `GET /api/v1/bible/languages/?search=&code=&page=&limit=`

`annotations/urls.py` (DRF router):
- `tags/`, `notes/` (supports `notes/reorder/` bulk update,
  `notes/order-index/`, `notes/move/`, `?lang=` language
  serving on detail+list,
  `notes/<id>/translations/<lang>/verify/` POST|DELETE
  owner-only, `notes/<id>/retranslate/` POST owner-only)
- `tasks/translate-note/` — Cloud Tasks worker endpoint,
  AllowAny + explicit OIDC verification, never user tokens
- `reading-positions/`
- `notes/<note_pk>/comments/`, `notes/<note_pk>/images/`,
  `notes/<note_pk>/comments/<comment_pk>/images/`
- `comments/counts/`, `images/<image_pk>/`

Docs: `/api/v1/docs/` (Swagger), `/api/v1/schema/`, `/api/v1/redoc/`.
Token login: `POST /api/token/`.

## Vercel edge caching

The frontend proxies `/api/*` through Vercel's CDN, which
caches rewrite responses where the API emits
`Vercel-CDN-Cache-Control` (stripped before the browser —
never relaxes the client-facing `Cache-Control` posture).
`bible/utils/edge_cache.py`'s `VercelEdgeCacheMixin` marks
only the anonymous-identical catalog endpoints:
`bible/translations/`, `bible/languages/` and
`bible/structure/` (`max-age=86400,
stale-while-revalidate=604800`) and `bible/copyright/`
(`max-age=604800, stale-while-revalidate=86400`). Never mark: `bible/` verse
and audio payloads (licensed content must not sit in
third-party caches), `bible/search/` (verse snippets),
`bible/timestamps/` (regenerated monthly — staleness risk),
and every user-scoped `annotations/`/`users/` endpoint. The
mixin skips non-2xx responses and `no-store`/`private`
bodies, so errors and degraded payloads are never marked.
See DEVELOPER_GUIDE "Vercel edge caching".

## Error contract

Provider failures surface as `error` + `error_code` fields built by
`bible/utils/provider_errors.py`: `rate_limited` → HTTP 429,
`not_found` (missing passage, upstream 404, or
`PassageNotFoundError`) → HTTP 404, `book_not_in_fileset`
(every provider's "fileset lacks this book/chapter" coverage
miss on the passage endpoint) → HTTP 404, `search_disabled`
(SWORD search rejected because the `sword_search` FeatureFlag
row is disabled — a runtime kill switch toggled via Django
admin, read-through-cached, missing row = enabled) → HTTP 403,
anything else →
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
DSN, no traffic. The DSN is an entry inside the shared
`env-file` GCP secret (env var `SENTRY_DSN` or config.yaml
locally). What each stream means:

- `DjangoIntegration` captures unhandled 500s at level error —
  real bugs.
- `capture_internal_exception` (in
  `bible/utils/provider_errors.py`) reports caught server-side
  failures at level error, tagged `error_code:internal_error` —
  GCS signing/counter/deletion failures, unexpected DB
  `IntegrityError`/`OperationalError` (deadlocks) in reorder/move,
  SWORD data corruption, timestamp-artifact read failures, and
  Cloud Run job failures (also tagged `job:<label>`;
  `run_all_jobs` adds an aggregate `capture_message` and
  `sentry_sdk.flush(timeout=5)` before exit). Real bugs —
  agent-fixable.
- `capture_provider_exception` reports upstream outages at level
  warning, tagged `error_code` in {`rate_limited`,
  `provider_error`} — the `_CAPTURED_ERROR_CODES` allowlist, so
  `not_found`/`book_not_in_fileset` are never sent. Trend-only;
  NOT auto-fix candidates. Classification:
  `is_upstream_failure` treats exceptions carrying an upstream
  status plus `requests`/`urllib3`/DBT-`ApiException`/
  `google.api_core` families as upstream; anything else caught
  at the provider boundary is re-routed to error-level
  `internal_error` (a `TypeError` in response shaping is our
  bug, not an upstream blip).
- Triage tags: `fileset_id`, `provider`, `endpoint`,
  `job:<label>`; fingerprints group on `{{ default }}` +
  `error_code` (+`fileset_id` when known) so one bad fileset
  can't spawn unbounded issue groups. `SentryUserMiddleware`
  sets scope user `{'id': pk}` (pk only —
  `send_default_pii=False` stays).
- `LoggingIntegration(level=INFO, event_level=None)` — INFO+
  log records are breadcrumbs only, never events; DEBUG is
  dropped entirely. `ignore_errors` skips `KeyboardInterrupt`;
  `sentry_before_send` (`bible_research/utils.py`) drops
  `DisallowedHost` scanner noise and strips `request.data` on
  `/api/token` and `/api/v1/users/` paths (password-bearing
  bodies).
- Performance: `traces_sampler` → 0 for `/_ah/` + docs/schema
  routes, ~0.3 for `/api/`, 0.1 elsewhere;
  `profiles_sample_rate=0.1`; explicit spans wrap provider
  calls, GCS reads/signing, and SWORD module scans.

The agent-fix queue for this project is `is:unresolved
level:error environment:production` — warning-level provider
events are excluded by the level filter. Events carry
`environment:production` on GCP and `development` elsewhere,
with `release` = `SENTRY_RELEASE` else `GAE_VERSION`, so the
environment filter skips dev noise and `resolvedInNextRelease`
resolves an issue at the next GAE deploy.

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
  them. `manage.py dbshell` needs a `psql` client — installed at
  `/opt/homebrew/opt/postgresql@16/bin/psql` but off agent PATH;
  prefer `manage.py shell` or a `psycopg2` script built on
  `settings.DATABASES`.
- `db_backups/local_db_backup.sh` (prod dump — DEVELOPER_GUIDE
  "Database Backups") needs `pg_dump` matching the server major:
  Aiven runs PG16 but PATH's `pg_dump` is v14 and aborts. Run with
  `export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"` first.
- For a writable local database, repoint `config.yaml`'s `default`
  to SQLite (`ENGINE: django.db.backends.sqlite3`,
  `NAME: db.sqlite3` — a file already exists in the repo root).
- Staging database `stagingdb` lives on the same Aiven
  instance. `staging_rw` owns it (read/write + `CREATEDB`);
  its password is a deploy secret, never committed.
  `ai_agent` has `CONNECT` plus `SELECT` on
  `staging_rw`-created tables via default privileges, so
  read-only agent investigation works there exactly like on
  `defaultdb`. For a writable remote dev DB, repoint
  `config.yaml`'s `default` at `stagingdb`/`staging_rw`
  (password from the staging deploy's `DATABASE_STAGING_URL`
  — ask the user).
- `settings.py` picks the DB URL per environment:
  Vercel previews (staging backend) require
  `DATABASE_STAGING_URL` → `stagingdb`, never `defaultdb`;
  GCP and Vercel production use `DATABASE_URL`; local dev
  falls back through `DATABASE_LOCAL_URL` →
  `DATABASE_READ_URL` (read-only user) → `DATABASE_URL`, so
  reaching prod from a dev shell is a deliberate opt-in.
  `DB_SSL_CERT` is the same Aiven CA PEM in every
  environment. See DEVELOPER_GUIDE "Environment Variables".
- `stagingdb` was seeded from `defaultdb` (2026-11-16): prod
  schema via `pg_dump --schema-only`, all `bible_verse`
  rows, `django_migrations`/content types/auth permissions,
  and the `Tester5`/`Daniels` accounts with their tokens,
  tags, notes, comments and reading positions — prod ids
  and password hashes preserved, so the same logins work
  on staging. Other prod users were NOT copied; reseed by
  re-running the same pg_dump/COPY procedure.

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

## Conventions / rules

Universal rules — follow exactly:

1. **Max 79 chars per line, every file type.** Verify:
   `awk 'length > 79 {print FILENAME":"NR}' <file>`. Fix
   violations you introduce plus long lines in files already
   being modified. Exempt: formatter output (e.g. Prettier at
   printWidth 80 may emit 80-char lines) and vendored upstream
   skill files under `.agents/skills/` and `.devin/skills/`
   (`openspec-*`, `vercel-react-best-practices/`) — refresh
   tools own them.
2. **Commit format** `Type: Capitalized message` —
   `Feat:`/`Fix:`/`Docs:`/`Refactor:`/`Test:`/`Chore:`.
3. **Commit on feature branches only, never `main`.**
   Name branches `<type>/<slug>` with the commit types
   (`feat/`, `fix/`, `docs/`, `refactor/`, `test/`, `chore/`).
   Stage only files changed this session, by path — never
   `git add -A`, `git add .` or `git commit -a`; confirm the
   staged set with `git status` first; leave the user's own
   dirty files alone; never commit plan/scratch markdown.
4. **Dangerous commands — never run** the destructive/deploy
   commands listed under "Repo-specific rules" below; write
   the change and tell the user the command to run instead.
5. **Scoped verification** — run only the tests/checks for
   what you changed, not the whole suite.
6. **PR bodies go to a temp file** + `gh pr create
   --body-file`, never inline `--body`; delete the temp file
   after. See `.devin/skills/create-pr/`.
7. **Every PR body includes a UI verification / QC section** —
   concrete steps (pages, clicks, expected result) exercising
   the change end-to-end through the UI. Omit only when
   genuinely unreachable from the UI — then give an
   equivalent (curl command, log line, file to inspect).
8. **GitHub remote targeting**: when `origin` is the user's
   fork (an `upstream` remote exists), ALL GitHub operations
   — issues, PRs, comments, releases — target the fork via
   `--repo <owner>/<fork>`; never upstream unless asked.
   Otherwise `origin` is the target.
9. **No secrets in code.** Secrets live in gitignored local
   files / env vars / GCP Secret Manager. Never create new
   Secret Manager secrets — append KEY=VALUE lines to the
   shared `env-file` secret (each extra secret adds cost).
10. **Update docs after functionality changes** — this file
    and the repo's other contract docs (README /
    DEVELOPER_GUIDE / docs/*) — in the same change.
11. **`docs/plans/` is frozen** — existing files stay as
    history with their status headers (implemented / partial /
    in progress / not implemented / reverted); nothing new is
    added there or anywhere else — all new planning goes
    through OpenSpec (`openspec-propose` for changes,
    `openspec-explore`/change artifacts for investigations).
12. **This file is self-updating.** Missing tool → tell the
    user what to install instead of grinding through failing
    alternatives. Discovered flow/local quirk → write it into
    the relevant AGENTS.md and commit that doc update
    alongside the work that discovered it.


### Repo-specific rules

- **Dangerous commands — never run**: `makemigrations`,
  `migrate`, `sqlmigrate` (workspace rule — even if a task
  seems to need it, e.g. tests failing on missing tables;
  tell the user the needed command); `gcloud app deploy` and
  other mutating `gcloud` calls; `terraform apply`. Deploys
  are CI's job — see the workspace AGENTS.md "Deployments"
  section for the manual-deploy contract.
- **Local terraform hangs without the Netskope CA bundle** —
  `terraform plan`/`apply` stalls with zero output (dead IPv6
  TCP) unless `SSL_CERT_FILE`,
  `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH`, `CURL_CA_BUNDLE` and
  `REQUESTS_CA_BUNDLE` point at the combined bundle
  (`~/.config/devin/combined-ca.pem`). Also set
  `TF_VAR_alert_email_address` (same value as the
  `ALERT_EMAIL_ADDRESS` repo variable) or plan proposes
  destroying the monitoring channel/alert policies.
- **Commits/PRs target `Bible-Research/bible_research`**
  (`origin`) — the only remote.
- **Test logins**: `testuser`, `guest` — create via
  `python scripts/create_test_user.py` if a local DB lacks
  them (pytest's in-memory DB makes its own). The shared UI
  test account is `Tester5` / `password123` (workspace
  AGENTS.md); `Tester5` and `Daniels` also exist on
  `stagingdb` with their prod data (see "Databases").
- **OpenSpec lives at the workspace root** — never run
  `openspec init` in this repo; cross-repo contracts are
  spec'd there (workspace AGENTS.md, "OpenSpec").
- **All implementation work happens in a git worktree**
  (`worktrees/<slug>` inside this repo — gitignored; cut
  from `origin/main`, needs its own venv + `config.yaml`
  symlink) — never the main checkout. Once the PR's checks
  are green, remove the worktree + local branch right away —
  don't wait for the merge (the PR lives on the remote
  branch). Full lifecycle: workspace `../AGENTS.md`
  "Repo-specific rules".

## Housekeeping

- CORS is narrowed to known browser consumers
  (`CORS_ALLOWED_ORIGINS` + `CORS_ALLOWED_ORIGIN_REGEXES` in
  `settings.py`): the `bibleresearch.vercel.app` prod domain,
  the `staging-bibleresearch.vercel.app` staging alias,
  `*-industr-analysers-projects.vercel.app` PR previews,
  Capacitor WebView origins (`capacitor://`/`localhost`), and
  `http://localhost:<port>` dev servers. Token auth means no
  cookies/CSRF, so this bounds page embedding, not access —
  non-browser callers are unaffected. A new browser consumer
  must be added to the list. Transport hardening:
  X-Forwarded-Proto trust is unconditional; Secure
  session/CSRF cookies turn on for deployed envs (GCP or
  Vercel); http→https redirect stays GCP-gated on
  `IS_GCP_ENVIRONMENT`;
  `ALLOWED_HOSTS` is `.appspot.com` + `.vercel.app` (the
  staging backend's hostnames) + localhost.
- Vercel staging backend: the `bible-research-api` Vercel
  project deploys the `staging` branch to
  `staging-bibleresearchapi.vercel.app` —
  `vercel.json`'s `git.deploymentEnabled` allows `staging`
  only, so `main` and feature-branch pushes never build (prod
  stays GAE-only). Vercel's Django preset auto-detects
  `manage.py` → `WSGI_APPLICATION` and runs collectstatic
  itself; `scripts/vercel_install.sh` is the Install
  Command, pinning `uv pip install` to `.python-version`
  (the preset's installer ignores it and resolves wheels
  for its newest CPython, which the pinned runtime cannot
  import);
  `scripts/vercel_build.sh` additionally applies migrations
  (refuses non-`stagingdb` `DATABASE_STAGING_URL`s) so
  `stagingdb`'s schema tracks `staging`.
  `vercel.json`'s `outputDirectory` is `staticfiles`
  (`STATIC_ROOT`'s parent) — required because a custom
  Build Command leaves Vercel expecting the `public`
  static-output dir; serving `staticfiles/` puts collected
  assets on `/static/` via the CDN. Runtime config
  arrives via the project's `DOTENV` env var (Preview scope —
  required, or settings raise `FileNotFoundError`) with
  `DATABASE_STAGING_URL` → `stagingdb`, never `defaultdb`
  (see "Databases"). Serverless
  carries no GCP credentials, so GCS-backed features
  (generated audio, image attachments) are unavailable there.
- All implementation work happens in a git worktree
  (`worktrees/<slug>` inside this repo — gitignored) — never
  the main checkout; remove the worktree + branch once the PR
  merges green. Full workflow: workspace `../AGENTS.md`
  "Repo-specific rules".
- `terraform/scheduler.tf`: the *monthly* Google TTS Cloud
  Scheduler stays intentionally commented out — do NOT enable it.
  The `daily-edge-audio` job IS enabled: it triggers `all-jobs`
  daily with a `jobs:run` container-args override of `--only
  generate_edge_audio`, so the monthly `dom:3` entry cannot
  piggyback on the trigger. The `eom-stt-timestamps` job IS also
  enabled: it fires a plain `jobs:run` on days 28-31 and the
  `last_day_of_month` predicate gates it to the real last day —
  a future `daily` JOBS entry would ALSO run on 28-31 via this
  trigger (review JOBS when adding entries).
- `terraform/all_jobs.tf`: the single `all-jobs` Cloud Run Job
  replaced the per-task `audio-generator` job (it reuses that SA and
  image repo, both still named `audio-generator`). Sub-jobs are
  listed in the `JOBS` table of
  `bible/management/commands/run_all_jobs.py` —
  `generate_chapter_audio` (`dom:3`, unscheduled),
  `generate_audio_timestamps` (`last_day_of_month`, started by
  the `eom-stt-timestamps` scheduler — spends the month's STT
  free minutes before they reset on the 1st; NOTE: output is
  currently LOW quality — GLU8 is only a proxy reference text
  since DBT has no LVR/NLI text, so artifacts land at
  ~0.09-0.28 alignment scores / `low_confidence`; revisit when
  a matching Latvian text is licensable — see
  DEVELOPER_GUIDE "Generated Timestamps"),
  `generate_gemini_audio` (`manual`), `generate_edge_audio`
  (`manual`; started only by the daily scheduler's `--only`
  override — the Everita voice) and `repair_audio_files`
  (`manual`; manifest-driven artifact repair — see below).
  Generated artifacts land under
  `audio_gemini/` and `audio_edge/` in the audio bucket (GCS
  modes `"gemini"`/`"edge"` in `bible/services/storage/gcs.py`)
  with day-keyed lock objects under `state/`; no fileset serves
  them yet.
- `bible/management/commands/audit_audio_files.py` — read-only
  GCS audio health check (frame-stream + sidecar validation in
  `bible/services/audio_audit.py`). `--metadata-only` lists
  pairs and checks sidecar size agreement without downloading
  MP3s (the free-tier-friendly mode); exits non-zero on
  suspects. `--deep` adds content checks (clipping, silence,
  loudness outliers) via the dev-only `miniaudio` decoder in
  `bible/services/audio_deep.py` — local use only, downloads
  the whole candidate set (GCS egress cost), never wire into
  `run_all_jobs`/terraform. `--report PATH` writes the JSON
  report to a local file or `gs://` URI — the manifest
  `repair_audio_files` consumes. See DEVELOPER_GUIDE
  "Audio Artifact Audit".
- `bible/management/commands/repair_audio_files.py` —
  manifest-driven repair of audit suspects (parsing/routing
  in `bible/services/audio_repair.py`, ledger + URI helpers
  in `storage/gcs.py`). Dry-run by default; `--apply`
  regenerates each suspect chapter pair in place via the
  Google TTS `Synthesizer` (`audio/` → verse, `audio_chapter/`
  → chapter; `audio_gemini/`/`audio_edge/` report
  `unsupported_engine`), billed against the shared
  `state/usage/{FILESET}/{YYYY-MM}.json` monthly char
  counter, and records repaired pairs under
  `state/repair/<base>.json` for idempotent re-runs
  (`--force` overrides). Default manifest:
  `gs://{AUDIO_BUCKET_NAME}/manifests/audio-repair.json`;
  registered as a `manual` JOBS entry — no scheduler
  wiring.
- `bible/management/commands/generate_gemini_audio.py` and
  `generate_edge_audio.py`: local mode (default) writes
  `tts-out/` + `gemini-tts-state.json`; `--upload-gcs`
  (auto-on when `CLOUD_RUN_JOB` is set) switches state +
  artifacts to GCS. Gemini needs `GEMINI_API_KEY` in
  `config.yaml` locally or the `env-file` secret on GCP —
  normalized to `settings.GEMINI_API_KEYS`. The Edge command
  is keyless (unofficial Microsoft endpoint, default voice
  `lv-LV-EveritaNeural`, exact per-verse timestamps).
- GCP secrets: never create a new Secret Manager secret — new
  credentials go in as appended KEY=VALUE lines on the shared
  `env-file` secret (each extra secret adds cost).
- Dependencies: `requirements.txt` pins the runtime stack to
  the versions installed in `venv/` (verify with `pip freeze`);
  `requirements-dev.txt` adds pytest/pytest-django/debugpy.
- Local pip/requests hit an SSL-intercept proxy: pip needs
  `--trusted-host pypi.org --trusted-host
  files.pythonhosted.org`; one-off `requests` scripts need
  `verify=False`.
- `bible/services/dbt/dbt_client/` is a slimmed vendored
  OpenAPI client imported via a `sys.path` append in
  `dbt/client.py` — not a pip package; don't add it to
  requirements or remove the path patch.
- Python egress (pip/requests/aiohttp/grpc) fails behind this
  machine's TLS-intercepting proxy unless pointed at the
  combined CA bundle — build/env details live in the global
  machine-environment notes (`~/.config/devin/AGENTS.md`).
  gRPC additionally needs REST transport
  (`transport="rest"`) — the proxy strips ALPN. See
  `tts-out/voice-comparison/README.md` for a worked example.
- If docs and code disagree, the code wins — fix the doc.
