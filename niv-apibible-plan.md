# Plan: NIV via API.Bible (non-commercial Starter plan)

Goal: add the New International Version (NIV) text (and audio, if the
account includes it) via API.Bible as a third selectable provider,
mirroring the existing `services/esv/` pattern. All code must work
without a real API key — the key arrives later and is read from
settings at runtime. Tests mock all HTTP.

## Current state (2026-10-13)

- PR 1 (backend provider): implemented on branch
  `feat/apibible-niv` in `bible_research` (local + `origin`),
  commits `5bb8fd1` (feat), `8179f00` + `320ac27` (review fixes).
  NOT merged — `main` has only a stale `__pycache__` under
  `bible/services/apibible/`; the real package
  (`registry.py`, `client.py`, `__init__.py`) exists only on the
  branch.
- PR 2 (frontend): implemented on branch
  `feat/apibible-niv-frontend` in `reactive-bible` — `fums.ts` +
  tests (FUMS reporting), `cacheManager.ts` presigned-URL
  `Expires` epoch parsing + tests, `api.tsx` (~531 insertions
  across 6 files). NOT merged.
- Both PRs still need to be opened and merged — target `origin`
  (the user's fork), base `main`, per workspace rules.
- PR 3 (GCS chapter cache): not started.
- `API_BIBLE_KEY`: still not provisioned — no key on `main`. The
  branch already reads it from env / GCP Secret Manager /
  `config.yaml` (`config_template.yaml` documents it), but a real
  key must still be added to local `config.yaml`, Secret Manager,
  and the GAE env for prod.

## Manual prerequisite (user, not agents)

- Sign up at api.bible, Starter plan ($0, strictly non-commercial).
- Express-license NIV (up to 3 copyrighted Bibles free).
  Text bible id `78a9f6124f344018-01` ("New International Version
  2011"); NIV audio edition `5dd1a42b02d2dee6-01` ("The Listener's
  Bible") — VERIFY audio is enabled on Starter before relying on it.
- Provide `API_BIBLE_KEY` later → `config.yaml` + GAE env.

## API.Bible facts driving the design

- Auth: `api-key` header (in header, NOT query param — no credential
  leak risk, but keep `provider_error_fields` discipline anyway).
- Base: `https://api.scripture.api.bible/v1`
- Chapter ids: `{USFM_BOOK}.{chapter}` (e.g. `JHN.3`) — same USFM
  codes as `bible/utils/bible_books.py::_BIBLE_BOOKS`; no mapping.
- Text: `GET /bibles/{bibleId}/chapters/{chapterId}` with
  `content-type=json&include-verse-numbers=true&include-verse-spans=
  true&include-titles=true`. JSON `data.content` is structured verse
  spans — small parser needed (simpler than ESV's regex parser).
  Response `meta` carries `fums`/`fumsId`/`fumsJsInclude` — MUST be
  reported client-side (FUMS, required by their terms for web apps).
- Audio: separate service — `GET /audio-bibles` (list),
  `GET /audio-bibles/{audioBibleId}/books/{bookId}/chapters`,
  `GET /audio-bibles/{audioBibleId}/chapters/{chapterId}` returns
  `resourceUrl` (presigned MP3, EXPIRES — like ESV's CDN redirect)
  and optional `timecodes` (verse→timestamp mapping, feeds the
  existing timestamps endpoint).
- Search: `GET /bibles/{bibleId}/search?query=&limit=&offset=`
  (offset-based pagination, NOT page-based).
- Copyright: bible object carries `copyrightStatement`.
- Limits: 5,000 calls/month hard cap on Starter; caching allowed if
  refreshed ≤30 days and ≤500 consecutive verses (frontend's LRU
  500-verse verse cache already complies).
- NEVER synthesize audio for copyrighted text via Google TTS —
  derivative-work violation. NIV audio comes from API.Bible or not
  at all.

## PR 1 — backend provider (`bible_research` repo)

**Status: implemented on branch `feat/apibible-niv`, unmerged.**

New package `bible/services/apibible/`:

- `registry.py` — `APIBIBLE_TRANSLATIONS` keyed by synthetic fileset
  id `ENGNIV_API`; entry fields: `name`, `abbr`, `language`,
  `language_iso`, `bible_id` (text), `audio_bible_id`,
  `audio_fileset_id` (`ENGNIVC1DA`), `license`. Helpers mirroring
  `esv/registry.py`: `canonical_apibible_fileset_id`,
  `is_apibible_fileset`, `get_apibible_meta`,
  `get_apibible_translation_listing()` emitting `text_plain` +
  `audio` fileset entries. Listing must work with NO key configured.
- `client.py` — `ApiBibleClient` mirroring `esv/client.py`:
  `requests.Session` with `api-key` header; lazy singleton
  `get_default_apibible_client()`; `ValueError` at construction if
  `settings.API_BIBLE_KEY` missing (like `ESVClient`).
  - `get_chapter(book_id, chapter)` → returns
    `{verses: [{verse_start, verse_text}], headings: [...], fums}`
    where `fums` passes through the response `meta` block.
  - `get_chapter_audio_url(book_id, chapter)` → presigned
    `resourceUrl`; also expose `get_chapter_audio(book_id, chapter)`
    returning `{audio_url, timecodes}` for the timestamps endpoint.
  - `search(query, offset, limit)`.
  - `get_bible()` → copyright statement.
  - Empty `data`/missing chapter → `PassageNotFoundError`.
- `bible/serializers.py` — `BiblePassageSerializer.to_representation`:
  `is_apibible_fileset` branches for text (verses + headings +
  `meta.fums` passthrough) and audio (`audio_url`), BEFORE the DBT
  fallback, same style as the ESV branch.
- `bible/views.py` — `BibleSearchView._apibible_search` mapping
  `page`/`limit` → `offset = (page-1)*limit`, normalizing results to
  the existing verse shape; `CopyrightView` branch returning the
  bible's `copyrightStatement`; `AudioTimestampView` branch mapping
  `timecodes` to `{verse_start, timestamp}`, 404 when absent.
- `bible/services/translation_service.py` — append
  `get_apibible_translation_listing()` (filtered by language_iso
  like the SWORD/ESV entries).
- `config_template.yaml` + `bible_research/settings.py` — read
  `API_BIBLE_KEY` from config/env. Do NOT modify the user's real
  `config.yaml`.
- Tests: `bible/tests/test_apibible_client.py` — mock `requests`,
  cover verse/heading parsing, audio URL, search, error mapping
  (401/403 → provider_error, 404 → not_found, 429 → rate_limited).
  Follow existing test conventions in `bible/tests/`.

## PR 2 — frontend (`reactive-bible` repo)

**Status: implemented on branch `feat/apibible-niv-frontend`,
unmerged.**

- No routing changes: `ENGNIV_API`/`ENGNIVC1DA` flow through the
  generic fileset path like `ENGESV_API`.
- FUMS: when a passage response carries `meta.fums`, lazy-load
  `https://cdn.scripture.api.bible/fums/fumsv2.min.js` once and call
  `_BAPI.t(fumsId)` per displayed chapter. Keep it failure-silent.
- Audio URL expiry: `resourceUrl` is presigned — verify
  `cacheAudioUrl` expiry parsing handles it; if not, pass expiry
  through and use it for cache TTL.
- i18n/types: extend `Translation`/fileset types only if the listing
  shape requires it; aim for zero changes there.

## PR 3 — optional, defer unless requested

GCS-backed chapter cache (TTL ≤30 days, keyed
`{bible_id}/{book}/{chapter}`) reusing `services/storage/gcs` to
stretch the 5k/month quota. No DB tables (migrations are forbidden).

## Hard rules for implementers

- 79-char max line length, every file type. Verify:
  `awk 'length > 79 {print FILENAME":"NR}' <file>`.
- Commit format `Type: Capitalized message`. Feature branches only,
  never `main`. Stage only files you edited — no `git add -A`.
- Never run `migrate`/`makemigrations`/`sqlmigrate`.
- Never commit this plan file or other scratch markdown.
- `bible_research` PRs → `origin` (`Bible-Research/bible_research`),
  base `main`. `reactive-bible` PRs → `origin`
  (`Bible-Research/reactive-bible`), base `main`; NEVER `upstream`
  or `emmanz1995` remotes.
- PR creation: body to a temp file + `gh pr create --body-file`
  (see `.devin/workflows/create-pr.md`); include a UI-verification
  section with concrete manual test steps; delete the body file
  after.
- Do NOT touch the dirty `.github/workflows/dependabot-automerge.yml`
  in `bible_research` — that's the user's own uncommitted edit.
- Backend tests: `source venv/bin/activate && python -m pytest
  bible/tests/test_apibible_client.py -q` (SQLite, no config needed).
- Frontend checks: `npm run lint`, `npm test -- --run`,
  `npm run build` (tsc).
- Update `bible_research/DEVELOPER_GUIDE.md` (provider routing
  section) and root `AGENTS.md` known-fileset list.
- No real API key exists during implementation — never call the live
  api.scripture.api.bible host in tests or code paths exercised by
  tests.
