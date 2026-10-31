# Bible Research API - Developer Guide

## Table of Contents
1. [Project Overview](#project-overview)
2. [Architecture](#architecture)
3. [Getting Started](#getting-started)
4. [Core Functionalities](#core-functionalities)
5. [API Endpoints](#api-endpoints)
6. [Database Schema](#database-schema)
7. [External Services](#external-services)
8. [Authentication & Authorization](#authentication--authorization)
9. [Development Workflow](#development-workflow)
10. [Testing](#testing)
11. [Deployment](#deployment)
12. [Contributing](#contributing)
13. [Additional Resources](#additional-resources)

---

## Project Overview

The Bible Research API is a Django REST Framework backend that provides:
- Bible verse retrieval in text and audio formats
- User annotation system with notes and tags
- Hierarchical tag organization
- Integration with the Digital Bible Platform (DBT) API
- Support for multiple Bible translations

This repository serves as the **backend API only**. A separate React 
frontend repository will consume this API.

---

## Architecture

### Technology Stack
- **Framework**: Django 6.1
- **API**: Django REST Framework
- **Database**: PostgreSQL (production) / SQLite (development)
- **External APIs**: DBT API v4, ESV API (api.esv.org),
  API.Bible (api.scripture.api.bible), local SWORD modules
- **Authentication**: Token-based (DRF TokenAuthentication
  plus a Bearer alias)

### Provider routing

Every request that reaches a Bible provider is dispatched by a
single facade — `bible/services/providers.py` — instead of each
view/serializer re-implementing the
`is_esv → is_apibible → is_sword → else DBT` chain. Callers ask
`provider_for(fileset_id)` for a provider object and invoke its
uniform capability surface:

- `get_chapter(book_id, chapter)` — normalized chapter payload
  (`verses`/`headings`/`meta`) used by note enrichment;
- `get_passage(book_id, chapter, response_format, verse_start,
  verse_end)` — the `/api/v1/bible/` response payload
  (`get_passage_body` in the same module wraps it with the
  response shaping + `error`/`error_code` mapping);
- `get_audio`, `get_timestamps`, `search`, `search_grouped`,
  `get_copyright`, `translation_listing`.

Coverage misses raise `PassageNotFoundError` (subclassed by
`TimestampsNotGeneratedError` for the "audio exists but its
timestamp sidecar does not" case) and surface as the stable
`book_not_in_fileset`/`not_found` codes; every other failure is
classified by `bible/utils/provider_errors.py` — raw exception
text never reaches a response body. Serializers shape data
only: `BiblePassageSerializer` is the request-validation
boundary and delegates to `get_passage_body`, and
`NoteSerializer` fetches each `(fileset_id, book, chapter)`
once per page through a context-shared cache instead of one
provider call per note.

Non-DBT copyright ids resolve through the same facade: ESV and
SWORD filesets (and abbrs like `GLU8`) answer from registry
license metadata, API.Bible filesets fetch the live statement
from the matching text/audio bible, and everything else
proxies DBT.

### Caching

The app uses a process-local Django cache (`CACHES` → locmem;
fine on the single-instance App Engine F1 — swap for
Memcached/Redis if a second instance is ever added). It backs
DRF throttling and these provider-data caches:

- `GET /api/v1/bible/translations/` — the whole response is
  cached per `language_iso` for 6 h and carries
  `Cache-Control: public, max-age=21600`. A DBT outage returns
  the registry-only list with `"degraded": true`, which is
  never cached (`Cache-Control: no-store`). Error responses
  are not cached either.
- `GET /api/v1/bible/copyright/` — cached per
  `(provider, bible_id)` for 24 h (copyright text is static)
  with `Cache-Control: public, max-age=86400`.
- `read_timestamps_json` (GCS `_timestamps.json` sidecars) —
  cached per `(fileset, voice, book, chapter, mode)` for 24 h;
  the artifact is nominally immutable (the generator only
  writes chapters that lack one). On overwrite the writer
  deletes the cache entry in its own process only — the
  generator runs as a Cloud Run job, separate from the App
  Engine serving process, so a re-generated chapter can serve
  stale data until the TTL expires or the service restarts.
  The timestamps endpoint and the passage audio path share this
  cache. Misses are never cached, so a chapter becomes
  available as soon as its artifact is generated.
- Signed audio URLs (`gcs.signed_audio_url`) live in a bounded
  in-process LRU (2048 entries, evicts oldest), expiring 60 s
  before the URL's real TTL.
- Chapter text (`bible/services/chapter_cache.py`) — every
  text-capable provider except API.Bible comes back from
  `provider_for` wrapped in `_CachingProvider`, which serves
  `get_chapter`/`get_passage(format='text')` from a
  process-wide LRU keyed by `(canonical_fileset_id, book_id,
  chapter)` (registry aliases like `GLU8`/`LVSGLU8C1DA` share
  the `LVSGLU8` entry). Entries live 24 h; `PassageNotFoundError`
  coverage misses are negative-cached for 5 min so fileset-
  fallback probing cannot re-penalize upstream. Verse-range
  hints are never sent upstream on a miss — a narrowed payload
  must not fill the whole-chapter entry — and audio payloads
  are never cached (their URLs expire). Size bound: 500
  chapters (~3 MB). Per-license safety: public-domain filesets
  (`ENGKJV`, `LVSGLU8`) are bounded only by that LRU, while
  copyrighted translations share a 500-verse budget per work —
  `ENGESV` (DBT) and `ENGESV_API` draw from the same `esv`
  budget; `ApiBibleProvider` is excluded entirely because FUMS
  `meta` tokens must be reported per display. One behavior
  delta the wrapper introduces: a text request
  (`response_format` unset/`'text'`) against a DBT *audio*
  (`*DA`) fileset now 404s `book_not_in_fileset` — the
  composed path serves only verse text, where the old
  `DbtProvider.get_passage` returned an audio payload for any
  format. The frontend always sends `response_format=audio`
  for audio filesets, so nothing user-facing changes.

All locmem cache keys carry a `v1:` namespace prefix (the
in-process LRUs above are module-level `OrderedDict`s, not
Django cache entries).

### Project Structure
```
bible_research/
├── bible/                  # Bible data and retrieval
│   ├── models.py          # Verse model
│   ├── views.py           # Bible passage endpoints
│   ├── serializers.py     # Request-validation boundary only
│   ├── services/          # Provider clients and facade
│   │   ├── providers.py   # fileset_id routing facade
│   │   ├── chapter_cache.py  # in-process chapter text LRU
│   │   ├── dbt/          # DBT API client
│   │   ├── esv/          # ESV API client + registry
│   │   ├── apibible/     # API.Bible client + registry
│   │   ├── sword/        # local SWORD module client + registry
│   │   ├── google_tts/   # generated-audio helpers
│   │   └── storage/      # GCS artifact helpers
│   └── utils/            # Helper functions (book mappings,
│                         # provider error normalization)
├── annotations/           # User notes and tags
│   ├── models.py         # Tag, Note, NoteVerse models
│   ├── views.py          # CRUD endpoints for tags/notes
│   └── serializers.py    # Annotation serialization
├── users/                # User management (minimal)
├── scripts/              # Utility scripts
│   ├── create_test_user.py
│   └── add_verse.py
└── bible_research/       # Django project settings
    ├── settings.py
    ├── urls.py
    └── authentication.py
```

---

## Getting Started

### Prerequisites
- Python 3.12
- PostgreSQL (for production) or SQLite (for development)
- DBT API key (from [Bible Brain][bible-brain])

### Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd bible_research
   ```

2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements-dev.txt
   ```
   `requirements-dev.txt` pulls in the pinned
   `requirements.txt` runtime stack plus dev-only tools
   (pytest, pytest-django, debugpy). Production installs
   `requirements.txt` alone.

4. **Configure the application**
   ```bash
   cp config_template.yaml config.yaml
   ```
   
   Edit `config.yaml` with your settings:
   ```yaml
   DATABASES:
     default:
       ENGINE: django.db.backends.postgresql
       NAME: bibledb
       USER: db_user
       PASSWORD: your_password
       HOST: localhost
       PORT: 5432
   
   DEBUG: true
   SECRET_KEY: 'your-secret-key'
   DBT_KEY: 'your-dbt-api-key'
   ```

5. **Run migrations**
   ```bash
   python manage.py migrate
   ```

6. **Create test users**
   ```bash
   # Create both guest and test users
   python scripts/create_test_user.py
   
   # Or create individually
   python scripts/create_test_user.py guest
   python scripts/create_test_user.py test
   ```

7. **Import Bible verses** (optional)
   ```bash
   python manage.py import_esv_verses
   ```

8. **Run the development server**
   ```bash
   python manage.py runserver
   ```

### Test Users

The project uses two special accounts:

1. **Guest User** (legacy, locked)
   - Username: `guest`
   - Email: `guest@example.com`
   - Purpose: Owns rows created before device
     auto-provisioning was removed; not used for new requests
   - Locked: unusable password — cannot log in or mint a
     `/api/token/` token
   - Read-only: API writes (POST/PUT/PATCH/DELETE)
     authenticated as `guest` (via a previously issued token)
     return 403

2. **Test User** (for development)
   - Username: `testuser`
   - Email: `testuser@example.com`
   - Password: `password123`
   - Has staff privileges

---

## Core Functionalities

### 1. Bible Passage Retrieval

**Purpose**: Fetch Bible verses in text or audio format from the DBT API

**Key Features**:
- Support for multiple Bible translations
- Text and audio format responses
- Chapter-level retrieval
- Automatic book name normalization — `get_dbt_book_id` accepts
  exact names plus common misspellings/alternate titles (e.g.
  "Isiah", "Song of Songs") and falls back to fuzzy matching for
  minor typos

**Implementation**:
- `BiblePassageView` (APIView)
- `BiblePassageSerializer` handles DBT API integration
- `DBTClient` wraps the DBT API

### 2. User Notes System

**Purpose**: Allow users to create personal notes linked to Bible verses

**Key Features**:
- Notes can be linked to multiple verses
- Notes can be tagged for organization
- Public/private visibility control
- Automatic verse text retrieval from DBT API

**Implementation**:
- `Note` model with many-to-many relationship to `Verse`
- `NoteVerse` intermediary model
- `NoteViewSet` provides full CRUD operations

### 3. Hierarchical Tag System

**Purpose**: Organize notes with parent-child tag relationships

**Key Features**:
- Unlimited nesting levels
- User-specific tags
- Automatic uniqueness constraints
- Tag filtering for notes

**Implementation**:
- `Tag` model with self-referential foreign key
- Unique constraint per user/parent combination
- `TagViewSet` for CRUD operations

### 4. Permission System

**Access Control**:
- **Authenticated users**: See their own tags and notes, plus
  every public note
- **Unauthenticated users**: See public notes only; `list` on
  tags returns `[]`, and a tag can be retrieved only when it
  carries at least one public note
- **Legacy `guest` account**: Read-only — reads allowed, writes
  rejected with 403 (`GuestReadOnly` in
  `bible_research/permissions.py`). `guest` is reachable only
  via a token issued before the account was locked.
- **Note visibility**: Controlled by `public` boolean field
- **No auto-provisioning**: `DeviceAndCountryMiddleware` has
  been removed — anonymous requests are real `AnonymousUser`s
  everywhere and every write endpoint requires authentication
  (anonymous writes return 401). Any read endpoint that must
  stay open to anonymous callers opts out of the
  `IsAuthenticated` default itself — e.g. the `bible` views and
  `GET /api/v1/comments/counts/` carry
  `permission_classes = [AllowAny]`.

**Throttling**: a global `AnonRateThrottle` caps anonymous
traffic at 300 requests/minute per client IP; authenticated
requests are exempt except where a view opts into a scope.
`POST /api/token/` (5/min per username), `POST
/api/v1/users/register/` (10/min per client), and
`GET /api/v1/bible/search/` (30/min, applied to anonymous and
authenticated callers alike) carry tighter scoped rates —
grouped search fans out to ~30 upstream provider calls per
request. Throttle counters use the per-process locmem cache,
so on GAE the effective limits scale roughly with running
instances and reset on every restart/deploy.

---

## API Endpoints

### Interactive API Documentation

The API includes auto-generated interactive documentation:

- **Swagger UI**: `http://localhost:8000/api/v1/docs/`
- **ReDoc**: `http://localhost:8000/api/v1/redoc/`
- **OpenAPI Schema**: `http://localhost:8000/api/v1/schema/`

These provide a complete, interactive reference for all endpoints
with request/response examples and the ability to test API calls
directly from your browser.

### Base URL
```
http://localhost:8000/api/v1/
```

### Authentication
```
POST /api/token/
```
**Request Body**:
```json
{
  "username": "testuser",
  "password": "password123"
}
```
**Response**:
```json
{
  "token": "9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b"
}
```
**Notes**:
- Username matching is case-insensitive
- Accounts with `is_active=False` are rejected with HTTP 400
- If two usernames differ only by case (e.g. `Alice`/`alice`),
  the exact-case match is preferred; otherwise the first match
  is used
- Invalid credentials return HTTP 400 with a `non_field_errors`
  list
- Throttled at 5 requests/minute per username (per client IP
  when no `username` is supplied); excess requests return
  HTTP 429
- Throttle counters use the default per-process locmem cache
  (no shared `CACHES` backend is configured), so on GAE the
  effective limit is roughly rate × running instances and
  counters reset on every restart/deploy

### User Management

#### Register New User
```
POST /api/v1/users/register/
```
**Request Body**:
```json
{
  "username": "newuser",
  "email": "user@example.com",
  "password": "securepass123",
  "password_confirm": "securepass123"
}
```
**Response**:
```json
{
  "user": {
    "id": 3,
    "username": "newuser",
    "email": "user@example.com",
    "date_joined": "2026-03-01T07:11:10Z",
    "account_kind": "registered"
  },
  "token": "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0",
  "message": "User registered successfully"
}
```
**Notes**:
- Email is optional
- Usernames may not contain whitespace (HTTP 400), and
  uniqueness is case-insensitive — login matches usernames
  with `iexact`, so `Alice`/`alice` cannot coexist
- `account_kind` is `guest` for the legacy read-only
  account, `registered` otherwise — clients should branch on
  it instead of string-matching `username == 'guest'`
- Password must pass Django's `AUTH_PASSWORD_VALIDATORS`:
  at least 8 characters, not entirely numeric, not a common
  password, and not too similar to the username/email.
  Violations return HTTP 400 with a `password` error list
- Returns authentication token automatically
- No authentication required for this endpoint
- Throttled at 10 requests/minute per client IP; excess
  requests return HTTP 429
- Throttle counters use the default per-process locmem cache
  (no shared `CACHES` backend is configured), so on GAE the
  effective limit is roughly rate × running instances and
  counters reset on every restart/deploy

#### Get Current User
```
GET /api/v1/users/me/
Authorization: Token <your-token>
```
**Response**:
```json
{
  "id": 3,
  "username": "newuser",
  "email": "user@example.com",
  "date_joined": "2026-03-01T07:11:10Z",
  "account_kind": "registered"
}
```

### Bible Passages

#### Get Bible Passage (Text)
```
GET /api/v1/bible/?passage=John+3&fileset_id=ENGESV
GET /api/v1/bible/?passage=John+3:16&fileset_id=ENGESV
GET /api/v1/bible/?passage=John+3:16-18&fileset_id=ENGESV
```
**Response**:
```json
{
  "book": "JHN",
  "book_name": "John",
  "chapter": 3,
  "format": "text",
  "verses": [
    {
      "verse": 1,
      "text": "Now there was a man of the Pharisees..."
    }
  ]
}
```

**Provider error response** (upstream Bible provider failed —
e.g. rate-limited): `verses` is omitted and the payload carries
`error`/`error_code` describing the failure. HTTP status is `404`
when `error_code` is `not_found` (the passage does not exist
upstream), `429` when `error_code` is `rate_limited`, otherwise
`502` (`provider_error`).
```json
{
  "book": "JHN",
  "book_name": "John",
  "chapter": 3,
  "error": "Bible provider rate limit exceeded (HTTP 429)",
  "error_code": "rate_limited"
}
```

The other provider-backed endpoints — `/api/v1/bible/timestamps/`,
`/api/v1/bible/copyright/`, `/api/v1/bible/search/`, and
`/api/v1/bible/translations/` — surface upstream failures with the
same `error`/`error_code` fields and the same HTTP status mapping
(`429`/`404`/`502`). Timestamp 404s for missing coverage ("not
yet generated" GCS artifacts, empty API.Bible timecodes) carry
`error_code: "not_found"`. Raw exception text is never embedded
in `error` because it can contain request URLs with credentials.

**Client error response** (invalid input): returns `400` with an
`error` message. `passage` accepts a book name plus a chapter
token, optionally with a verse selector (`John 3`,
`John 3:16`, `John 3:16-18,20`). Messages: `Passage parameter
is required. Example: ?passage=John+3`, `Passage parameter is
too long (max 100 characters).`, `Invalid passage format. Use
'Book Chapter' (e.g., 'John 3')`, `Unknown book: <name>`,
`Chapter must be a valid number` (non-numeric), `Chapter must
be a positive number` (zero or negative), `Verse selectors
must be positive numbers with Start <= End`,
`Verse numbers must be between 1 and 200`, `Too many verse
segments (max 25)`.

#### Get Bible Passage (Audio)
```
GET /api/v1/bible/?passage=John+3&response_format=audio&
    fileset_id=ENGESVN2DA
```
**Response**:
```json
{
  "book": "JHN",
  "book_name": "John",
  "chapter": 3,
  "audio_url": "https://...",
  "duration_seconds": 180,
  "file_size_bytes": 2048000,
  "format": "audio"
}
```

**Query Parameters**:
- `passage` (required): Book and chapter
  (e.g., "John 3", "2 Chronicles 14"), optionally with a verse
  selector appended to the chapter: "John 3:16" (single verse),
  "John 3:16-18" (range), or "John 3:16-18,20" (comma-separated
  mix). With a selector, `verses` contains only the requested
  verse numbers for `response_format=text`; audio responses are
  always chapter-granular and ignore the selector. A selector
  outside the chapter's verses returns `404` with
  `error_code` `book_not_in_fileset`. The parameter is bounded: at most
  100 characters total, 25 verse segments, and verse numbers
  1–200 — larger values return `400`.
- `response_format`: "text" or "audio" (default: "text")
- `fileset_id`: DBT fileset ID for the specific translation and
  format (default: "ENGESV")
  - For text: Use translation codes like "ENGESV", "ENGKJV",
    "ENGESV_API" (ESV API), "LVSGLU8" (SWORD), "ENGNIV_API"
    (API.Bible NIV)
  - For audio: Use audio fileset codes like "ENGESVN2DA",
    "LVSGLU8C1DA", "ENGNIVC1DA"

API.Bible text responses additionally carry a `meta` object with
the upstream FUMS fields (`fums`, `fumsId`, `fumsJsInclude`,
`fumsNoScript`) — clients must report them per API.Bible's
license terms. Audio responses return API.Bible's presigned
`resourceUrl`, which expires (typically within minutes); treat
it like any other short-lived signed URL.

**Text-vs-audio capability matrix** — providers disagree on
what a mismatched `response_format` means, and the endpoint
relays each provider's answer rather than normalizing it:

- `text` on an audio fileset:
  - DBT (`*DA` id) → `format:"audio"` payload — the audio
    fileset has no text rows to return.
  - SWORD (`LVSGLU8C1DA`/`C2DA`), API.Bible (`ENGNIVC1DA`) →
    chapter text (the audio id canonicalizes to the provider's
    text resource).
  - ESV has no separate audio fileset id — `ENGESV_API`
    serves both formats, so the mismatch cannot arise.
- `audio` on a text fileset:
  - DBT → 404 `book_not_in_fileset`.
  - ESV, SWORD, API.Bible → chapter audio URL (the same id's
    audio resource: ESV API audio, the generated TTS sibling,
    or the paired API.Bible audio bible).

Callers should always branch on the returned `format` field,
not on the requested `response_format`, and treat 404
`book_not_in_fileset` as "try another fileset".

**Error contract** — when the requested book/chapter is not
covered by the fileset, the endpoint returns HTTP 404 with a
stable error code (for both text and audio requests) so clients
can fall back to another fileset:

```json
{
  "book": "JHN",
  "book_name": "John",
  "chapter": 3,
  "error": "Book JHN chapter 3 is not available in fileset
           ENGNASN_ET",
  "error_code": "book_not_in_fileset"
}
```

This covers a DBT 404 (or empty `data`) from
`v4_bible_filesets_show_chapter`, SWORD misses (a book/chapter
outside the module, or generated audio not yet uploaded),
ESV API 404s for both text and audio requests, and API.Bible
misses (upstream 404s, empty chapters, or missing audio
resources reported as `PassageNotFoundError`). Every provider's
"fileset lacks this book/chapter" outcome is normalized to the
single `book_not_in_fileset` code the frontend's fileset
fallback keys on.

Other provider failures surface as `error` + `error_code` with a
real HTTP status (built by `bible/utils/provider_errors.py`):
`rate_limited` → 429, `not_found` → 404, anything else →
`provider_error` → 502. Raw exception text is never embedded in
`error` — upstream exception messages can contain request URLs
with credentials (DBT's `?key=` query param); only the exception
class name is surfaced. The React app branches on `error_code`,
so these fields stay stable.

**Client-input 400s** on all `bible/` endpoints carry the same
contract — `{"error", "error_code"}` built by
`bible/utils/error_responses.py`: `missing_parameter` when a
required query param is absent, `invalid_parameter` when one is
malformed. `fileset_id` (passage/search/timestamps), `bible_id`
(copyright), `language_iso` (translations) and `book`
(timestamps) are charset/length-validated at the view boundary
before provider routing, so garbage ids can never reach the DBT
branch or an upstream URL path.

#### List Available Translations
```
GET /api/v1/bible/translations/
```
**Response**:
```json
{
  "results": [
    {
      "abbr": "ESV",
      "name": "English Standard Version",
      "language": "English",
      "language_iso": "eng",
      "filesets": [
        {
          "id": "ENGESV",
          "type": "text_plain",
          "size": "C"
        },
        {
          "id": "ENGESVN2DA",
          "type": "audio_drama",
          "size": "NT"
        }
      ],
      "text_options": [
        {
          "id": "ESV:text:1",
          "kind": "text",
          "by_testament": {
            "OT": {"text": "ENGESVO_ET"},
            "NT": {"text": "ENGESVN_ET"}
          },
          "coverage_label": "Full Bible",
          "partial": {"OT": false, "NT": false}
        }
      ],
      "audio_options": [
        {
          "id": "ESV:audio_drama:1",
          "kind": "audio_drama",
          "by_testament": {
            "NT": {
              "mp3": "ENGESVN2DA",
              "opus16": "ENGESVN2DA-opus16"
            }
          },
          "coverage_label": "New Testament only",
          "partial": {"OT": false, "NT": false}
        }
      ]
    }
  ]
}
```

**Degraded mode** — the response merges the live DBT `/bibles`
list with bundled registry translations (ESV API, API.Bible NIV,
SWORD), deduplicated by `abbr`. If the DBT fetch fails the
request still returns `200` with the registry-only list and
`"degraded": true` (`Cache-Control: no-store`); any other
failure keeps the `502`/`provider_error` contract. Successful
responses are cached per `language_iso` for 6 h (see
*Caching*); the DBT listing itself is truncated at 500 rows.

**Normalized options** — each translation also carries
`text_options` (at most one entry, `kind: "text"`) and
`audio_options` (one per listening experience; `kind` is
`"audio"`, `"audio_drama"`, or `"generated"` for SWORD TTS).
Grouping rules (see `bible/services/fileset_groups.py`):

- `-opus16` ids pair with their `mp3` base id under a shared
  `by_testament` codec map.
- Audio filesets group by `(type, digit)` from the trailing
  `[CONPS]\dDA` suffix: `1` = plain reading, `2` = dramatized.
- Ids that do not match (SWORD `LVSGLU8C1DA`, `ENGESV_API`)
  become singleton options.
- Coverage comes from `size` (`C` → OT+NT; `NT`/`OT` substrings;
  `P` marks partial), not the id letter.
- `filesets` remains in the response unchanged for backwards
  compatibility.

**Query Parameters**:
- `language_iso` (optional): Filter by ISO language code
  (e.g., "eng", "lvs"). Must match `^[a-zA-Z]{2,3}$` —
  anything else returns `400` with
  `error_code: "invalid_parameter"`.

**Example**:
```
GET /api/v1/bible/translations/?language_iso=eng
```

#### List Languages
```
GET /api/v1/bible/languages/
```
Proxies the DBT `/languages` endpoint — every language that has
Bible content in DBT (~2,600). Used by the frontend's "Other"
language picker before fetching `/bible/translations/` for the
chosen `iso`.

**Query Parameters**:
- `search` (optional): Case-insensitive substring match on the
  language name (e.g., "russ" matches Russian and Buryat)
- `code` (optional): Exact ISO 639-3 code lookup (e.g., "rus")
- `page` (optional): Result page number
- `limit` (optional): Max results per page (DBT clamps to ~150)

**Response** — `meta.pagination` carries the whitelisted fields
of DBT's pagination object (`{}` when absent):
```json
{
  "results": [
    {
      "iso": "rus",
      "name": "Russian",
      "autonym": "Русский",
      "bibles": 6,
      "filesets": 18
    }
  ],
  "meta": {
    "pagination": {
      "total": 1,
      "count": 1,
      "per_page": 25,
      "current_page": 1,
      "total_pages": 1
    }
  }
}
```

**Example**:
```
GET /api/v1/bible/languages/?search=russ&page=1&limit=25
```

#### Search the Bible
```
GET /api/v1/bible/search/?query=love&fileset_id=ENGESV
```

**Query Parameters**:
- `query` (required): Word or phrase to search for. Leading
  and trailing whitespace is stripped; a query that is empty
  after stripping returns `400`, as does a query over 200
  characters
- `fileset_id` (required): DBT or SWORD fileset ID; use
  `ENGESV_API` for ESV API search or `ENGNIV_API` for API.Bible
  (NIV) search — `page`/`limit` are mapped onto API.Bible's
  `offset`-based pagination
- `limit`: Max results per page (default 15)
- `page`: Result page number (default 1)
- `sort_by`: Sort field (DBT only — forwarded on every paged
  and grouped upstream call)
- `books`: Comma-separated USFM book IDs (e.g. `JHN,ROM`);
  honored for DBT and SWORD only — the ESV API has no book
  filter
- `group_by`: Set to `book` to return every match grouped by
  book instead of a paginated list

**Response** (default paginated mode):
```json
{
  "data": {
    "verses": [
      {
        "book_id": "JHN",
        "chapter": 3,
        "verse_start": 16,
        "verse_text": "For God so loved the world..."
      }
    ],
    "meta": {
      "pagination": {
        "total": 770,
        "count": 15,
        "per_page": 15,
        "current_page": 1,
        "total_pages": 52
      }
    }
  }
}
```
`meta.pagination` is always present for every provider — fields
a provider omits fall back to locally-known values.
`chapter`/`verse_start` are always real integers. For SWORD
filesets the local scan stops once enough matches fill the
requested page; `meta.truncated: true` then marks
`total`/`total_pages` as lower bounds — keep paging while it
is set.

**Grouped mode** (`group_by=book`) fetches all matches and
returns them bucketed per book in canonical order. Verses
within each group are sorted by chapter and verse:
```json
{
  "data": {
    "groups": [
      {
        "book_id": "GEN",
        "count": 12,
        "verses": [
          {
            "book_id": "GEN",
            "chapter": 22,
            "verse_start": 2,
            "verse_text": "..."
          }
        ]
      }
    ],
    "meta": {
      "total": 770,
      "truncated": false
    }
  }
}
```
`meta.total` is the provider-reported match count (number
scanned for SWORD). `meta.truncated` is true when a safety cap
cut results off (5000 results for DBT/API.Bible/SWORD, 30 pages
for ESV).

### Tags

#### List Tags
```
GET /api/v1/tags/
Authorization: Token <your-token>
```

Anonymous callers get `200` with an empty array (`[]`).

Every tag payload carries:

```json
{
  "id": "TAG123456789ABCDE",
  "name": "Love",
  "parent_tag": null,
  "public": false,
  "note_count": 7,
  "created_at": "…",
  "updated_at": "…"
}
```

- `public` — boolean, writable on create and PATCH. A plain
  PATCH flips only the flag; it does NOT change any existing
  notes (the cascade lives in `set-public`, below).
- `note_count` — read-only integer, notes attached DIRECTLY
  to the tag (subtree totals are aggregated client-side);
  tag creates report `0`. Anonymous retrieves count public
  notes only, so a shared tag's badge cannot leak private
  notes. A tag nested inside a note payload carries no
  count and omits the key.

#### Get Tag by ID
```
GET /api/v1/tags/{tag_id}/
```

Works without authentication only for tags that carry at least
one public note (share pages need the tag name); every other tag
returns `404` to anonymous callers. Authenticated users can only
retrieve their own tags.

#### Create Tag
```
POST /api/v1/tags/
Authorization: Token <your-token>
Content-Type: application/json

{
  "name": "Love",
  "parent_tag": null
}
```

#### Create Child Tag
```
POST /api/v1/tags/
Authorization: Token <your-token>
Content-Type: application/json

{
  "name": "Agape Love",
  "parent_tag": "TAG123456789ABCDE"
}
```

`parent_tag` must be one of the caller's own tags — a foreign
or nonexistent tag id returns `400` (PKRF validation).
Names are trimmed of surrounding whitespace, and a top-level
tag name must be unique per user: creating or renaming to a
duplicate returns `400` on the `name` field (backed by the
`unique_top_level_tag_name_per_user` conditional constraint).
The same name under different parents remains allowed.

#### Update Tag
```
PUT /api/v1/tags/{tag_id}/
Authorization: Token <your-token>
Content-Type: application/json

{
  "name": "Divine Love"
}
```

Re-parenting is validated against cycles: a tag cannot be its
own parent nor be moved underneath one of its own descendants
(both return `400`). `parent_tag` may be set to another own
tag id or `null` (move to root); a same-named sibling under
the target parent (or a same-named top-level tag when moving
to root) returns `400`.

#### Set Tag Publicity (cascade)
```
POST /api/v1/tags/{tag_id}/set-public/
Authorization: Token <your-token>
Content-Type: application/json

{
  "public": true
}
```

Flips the tag's `public` flag AND every note DIRECTLY under
the tag to the same flag, atomically — descendant tags and
their notes are untouched. Toggling back to private is
symmetric (every note under the tag goes private again).
`notes_updated` reports the number of note rows MATCHED by
the update — an idempotent repeat therefore returns the same
count again rather than 0:

```json
{
  "tag": { "id": "TAG…", "public": true, "note_count": 12, … },
  "notes_updated": 12
}
```

Missing or non-boolean `public` returns `400`
`{"error": "validation_error", "message": "…"}`; foreign or
nonexistent tags return `404`. The action is idempotent.

#### Delete Tag
```
DELETE /api/v1/tags/{tag_id}/
Authorization: Token <your-token>
```

Deleting a tag cascades to every note under it — and to any
descendant tags and their notes. To prevent silent data loss,
the delete is rejected with `409 Conflict` while the tag
subtree still has notes:

```json
{
  "error": "tag_has_notes",
  "message": "This tag still has 2 note(s). ...",
  "note_count": 2
}
```

To intentionally delete a tag together with all of its notes,
pass the explicit opt-in flag:

```
DELETE /api/v1/tags/{tag_id}/?delete_notes=true
Authorization: Token <your-token>
```

### Notes

#### List Notes
```
GET /api/v1/notes/
Authorization: Token <your-token>
```

#### List Public Notes
```
GET /api/v1/notes/?public=true
```

#### Filter Notes by Tag
```
GET /api/v1/notes/?tag_id=TAG123456789ABCDE
Authorization: Token <your-token>
```

Anonymous callers (no `Authorization` header) may read
public notes; all other note operations require
authentication (401/403 otherwise). There is no device
auto-provisioning anymore — anonymous requests arrive as a
real `AnonymousUser` on every endpoint, and anonymous
`POST`/`PUT`/`PATCH`/`DELETE` are rejected with 401.
Note payloads include a read-only `is_owner` boolean: `true`
only when the request user is authenticated and owns the
note, always `false` for anonymous readers — the frontend
uses it to decide whether a shared note is editable.

**Query Parameters**:
- `tag_id` (required): Tag ID to filter notes. The sentinel
  `tag_id=untagged` lists notes with no tag instead.
- `include_descendants` (optional, default `false`): with a
  real `tag_id`, merges in notes from every descendant tag —
  `count` and pagination cover the merged set, and the
  `custom`/`-custom` orderings fall back to `-created_at`/
  `created_at` (tag_position is per-tag and meaningless
  across tags). Ignored for `tag_id=untagged` and when
  `tag_id` is absent; a nonexistent tag id returns an empty
  list. `notes/order-index/`, `notes/reorder/`, and
  `notes/move/` stay strictly per-tag.
- `fileset_id` (optional): Bible translation used to enrich
  each note's `verses[].text` — same provider routing as the
  passage endpoint (`ENGESV_API` → ESV API, SWORD ids like
  `LVSGLU8` → local module, `ENGNIV_API`/`NIV` → API.Bible,
  anything else → DBT) via `provider_for(fileset_id)`.
  Enrichment is batched: the whole chapter is fetched once per
  `(fileset_id, book, chapter)` per page through a serializer
  context cache, so a page of notes on one passage costs one
  provider call — and chapter payloads come from the
  process-wide chapter cache (see Caching above) unless the
  fileset is API.Bible-backed. Defaults to `ENGESV` (DBT).
  SWORD translations return no `headings`.
- `ordering` (optional): Sort order
  - `custom` (default): Custom order ascending
  - `-custom`: Custom order descending
  - `created`: Oldest first
  - `-created`: Newest first
  - `verse`: Bible order ascending
  - `-verse`: Bible order descending
- `page` (optional): Page number (default: 1)
- `page_size` (optional): Items per page (default: 25, max: 100)

**Paginated Response**:
```json
{
  "count": 150,
  "next": "http://api/notes/?tag_id=TAG123&page=2",
  "previous": null,
  "results": [
    {
      "id": "NOT123...",
      "note_text": "...",
      "verses": [...],
      "tag_position": 1,
      "created_at": "2025-12-28T03:51:55Z"
    }
  ]
}
```

When the upstream Bible provider fails while resolving verse
text for a note (e.g. rate-limited), the note keeps its
`verses` references with empty `text` and additionally carries
`error`/`error_code` (`rate_limited`, `not_found`, or
`provider_error`) describing the failure. A successful-but-
empty provider response (no verse rows returned) is reported
the same way as a missing passage: `error_code` is
`not_found`.

Notes with no verse links keep the same response shape:
`verses` is `[]`, `headings` is `[]`, and `tag` is still the
expanded tag object when set — clients never have to sniff
for absent keys or a raw tag PK.

**Examples**:
```
# Get first page with custom ordering (default)
GET /api/v1/notes/?tag_id=TAG123

# Get second page with 50 items per page
GET /api/v1/notes/?tag_id=TAG123&page=2&page_size=50

# Sort by newest first
GET /api/v1/notes/?tag_id=TAG123&ordering=-created

# Sort by Bible verse order
GET /api/v1/notes/?tag_id=TAG123&ordering=verse
```

#### Create Note
```
POST /api/v1/notes/
Authorization: Token <your-token>
Content-Type: application/json

{
  "note_text": "This verse speaks about God's love...",
  "public": false,
  "tag": "TAG123456789ABCDE",
  "verse_references": [
    {
      "book": "John",
      "chapter": 3,
      "verse": 16
    }
  ]
}
```

**Response**:
```json
{
  "id": "NOT123456789ABCDE",
  "user": 1,
  "note_text": "This verse speaks about God's love...",
  "public": false,
  "tag": {
    "id": "TAG123456789ABCDE",
    "name": "Love",
    "parent_tag": null
  },
  "verses": [
    {
      "book": "John",
      "chapter": 3,
      "verse": 16,
      "text": "For God so loved the world..."
    }
  ],
  "is_owner": true,
  "created_at": "2025-12-28T03:51:55Z",
  "updated_at": "2025-12-28T03:51:55Z"
}
```

Note creation is atomic: if any `verse_references` entry fails to
resolve, the request returns `400` and no note or verse links are
persisted. Repeated entries in `verse_references` are silently
deduplicated before linking (book match is case-insensitive), and
`tag_position` is read-only on this endpoint — ordering is
managed exclusively by `notes/reorder/` and `notes/move/`.

`public` is optional on create: when OMITTED the note inherits
its tag's `public` flag (an untagged note defaults to `false`).
An explicit `public: false` under a public tag is honored —
per-note opt-out stays possible. On PATCH the flag is an
explicit write: the tag default never applies and moving a
note between tags does not flip it.

Bulk creation goes through `POST /api/v1/notes/bulk/` with
`{"tag_id": "TAG...", "notes": [...]}` — up to 100 notes per
request (`400` beyond that), returning `201` with the created
notes. Per-item `public` is optional the same way: an omitted
flag inherits `tag.public` instead of landing `false`.

#### Get Note by ID
```
GET /api/v1/notes/{note_id}/
Authorization: Token <your-token>
```

#### Update Note
```
PUT /api/v1/notes/{note_id}/
PATCH /api/v1/notes/{note_id}/
Authorization: Token <your-token>
Content-Type: application/json

{
  "note_text": "Updated note text...",
  "public": true,
  "verse_references": [
    {
      "book": "John",
      "chapter": 3,
      "verse": 17
    }
  ]
}
```

`verse_references` is optional: when present it replaces all of the
note's verse links (send `[]` to clear them); when omitted the
existing links are left untouched. Unknown verses return 400 and
leave the note unchanged.

#### Delete Note
```
DELETE /api/v1/notes/{note_id}/
Authorization: Token <your-token>
```

#### List Linked Notes
```
POST /api/v1/notes/linked/?fileset_id=ENGKJV
Authorization: Token <your-token>
Content-Type: application/json

{
  "verse_references": [
    {
      "book": "John",
      "chapter": 3,
      "verse": 16
    }
  ]
}
```

Returns the requesting user's notes that reference at least one
of the given verses (1–500 references per request; book matching
is case-insensitive). Used by the note creation form to show
existing notes on the selected verses.

`fileset_id` is an optional query parameter selecting the Bible
translation, as on the other note endpoints. Each note's
`verses` entries carry coordinates only — this endpoint never
enriches verse text from the upstream Bible provider, so
`text` is always empty and `headings` is always `[]`.

**Response**:
```json
{
  "count": 1,
  "results": [
    {
      "id": "NOT123...",
      "note_text": "...",
      "verses": [...],
      "tag": {...},
      "created_at": "2025-12-28T03:51:55Z"
    }
  ]
}
```

#### Note Order Index
```
GET /api/v1/notes/order-index/?tag_id=TAG123456789ABCDE
Authorization: Token <your-token>
```

Returns every accessible note in the tag — unpaginated — as a
compact row in canonical custom order (`tag_position`
ascending, nulls last, `-created_at` tiebreak — identical to
`ordering=custom` on `GET /notes/`). Built for UIs that need
every note's rank + passage at once (e.g. a "move note"
dropdown listing 1-based order IDs), without paging through
full note payloads. Anonymous callers see only public notes.
`tag_id=untagged` resolves the sentinel to notes with no tag,
matching `GET /notes/` and `/comments/counts/` semantics.

**Response**:
```json
[
  {
    "id": "NOT123...",
    "tag_position": 1788499374.59,
    "book": "John",
    "chapter": 3,
    "verse_start": 16,
    "verse_end": 18
  }
]
```

`book`/`chapter`/`verse_start`/`verse_end` describe the note's
first verse block for display as `Book chapter:start[-end]`.
Mirroring `NoteSerializer`, verses are ordered by verse number
only — for a note spanning multiple books/chapters the "first"
verse is the smallest verse number, and `verse_start`/
`verse_end` are the min/max verse within that verse's
book+chapter. All four fields are `null` when the note has no
verses. The endpoint never calls the upstream Bible provider.

**Errors**:
- `400 {"error": "validation_error", "message": ...}` when
  `tag_id` is missing.

#### Move Note
```
POST /api/v1/notes/move/
Authorization: Token <your-token>
Content-Type: application/json

{
  "tag_id": "TAG123",
  "note_id": "NOT456",
  "target_note_id": "NOT789",
  "placement": "after"
}
```

Moves `note_id` immediately after or before `target_note_id`
(`placement`: `"after"` | `"before"`), interpreted in canonical
ascending order (`tag_position` ASC). Clients displaying
descending custom order must translate a visual "move after"
into the canonical placement themselves.

The new position is the float midpoint of the target's
neighbours — `last + 1` when moving after the last note,
`first - 1` before the first. When the float gap is exhausted,
all of the tag's notes are renumbered `1..N` in their current
order inside a single `transaction.atomic()` +
`select_for_update()` block (reusing the two-phase
temp-negative-position write that `notes/reorder/` uses to
satisfy `unique_tag_position_per_tag`), and the moved note
takes its slot. Notes are scoped to `request.user`, as in
`notes/reorder/`.

Scope caveat: `order-index` (like `GET /notes/`) also lists
other users' *public* notes in the tag, but `move` only sees
and reorders the requester's own notes. In a shared tag, a
`note_id`/`target_note_id` naming a foreign public note
returns `400`, and positions held by foreign notes can still
surface as a `409 position_conflict`.

**Response** `200 OK`:
```json
{
  "note_id": "NOT456",
  "tag_position": 5.5,
  "rebalanced": false
}
```

**Errors**:
- `400 {"error": "validation_error", "message": ...}` — missing
  or invalid `tag_id`/`note_id`/`target_note_id`/`placement`,
  `note_id` equal to `target_note_id`, or a note that does not
  exist or does not belong to the requester/`tag_id`.
- `409 {"error": "position_conflict", "constraint":
  "unique_tag_position_per_tag", ...}` — position collision,
  same contract as `notes/reorder/`.
- `401` for anonymous callers — all writes require
  authentication.
- `403` when the requester is authenticated as the legacy
  `guest` account — `guest` is read-only, so all writes
  (including `notes/move/`) are rejected.

### Comments

Comments are threaded under a note (`parent_comment` enables
replies; `is_deleted` soft-deletes while keeping the thread
intact).

```
GET    /api/v1/notes/{note_pk}/comments/
POST   /api/v1/notes/{note_pk}/comments/
GET    /api/v1/notes/{note_pk}/comments/{comment_pk}/
PATCH  /api/v1/notes/{note_pk}/comments/{comment_pk}/
DELETE /api/v1/notes/{note_pk}/comments/{comment_pk}/
GET    /api/v1/comments/counts/?tag_id= | note_ids=
```

`GET` list returns the comment tree (root comments with nested
`replies`) rather than a flat list. `POST` accepts `content`
and optional `parent_comment` to create a reply; the parent
must belong to the same note (`400` otherwise). `DELETE`
soft-deletes and returns `204`.

**Access**: a note's comments follow the same visibility
scope as the note itself (`get_accessible_notes_qs`) — a
requester sees comments only on their own notes or on public
notes. Any operation against a note outside that scope
returns `404`, identical to a nonexistent note, so private
note existence is not leaked. Any authenticated user may
comment on a public note; updating a comment is restricted
to its author (`403` otherwise), while deleting is allowed
for the author **or the note's owner** (the note owner
moderates discussion on their own note, mirroring
`DELETE /images/{id}/`). `content` is capped at 10000
characters. Reads require no token (anonymous callers see
public-note comments); writes require authentication.

Soft-delete contract: `DELETE` sets `is_deleted=True` and
clears `content`, but the row is retained so reply threads
stay intact. On every read path (tree **and** detail GET) a
deleted comment is serialized as a tombstone — `content` is
`"[deleted]"` and `images` is `[]`. Clients should render an
`[deleted]` placeholder when `is_deleted` is true; the API
never returns `404` for a deleted comment.

PATCH may only change `content`: `is_deleted` is read-only
(deletion happens exclusively via `DELETE` and cannot be
undone), and `parent_comment` is create-only — sending it on
update returns `400`. Tombstones are immutable: `PATCH` on a
deleted comment returns `400`, and `POST` with a
`parent_comment` pointing at a deleted comment also returns
`400` — a reply cannot hang off a `[deleted]` stub.

### Note & Comment Images
```
GET  /api/v1/notes/{note_id}/images/
POST /api/v1/notes/{note_id}/images/
GET  /api/v1/notes/{note_id}/comments/{comment_id}/images/
POST /api/v1/notes/{note_id}/comments/{comment_id}/images/
DELETE /api/v1/images/{image_id}/
```

Multipart upload with a single `file` field. Note images:
only the note's owner may upload; reads follow the note's
accessibility scope. Comment images: only the comment's
author may upload. On both nested routes the parent note or
comment is resolved through `get_accessible_notes_qs`, so an
inaccessible (private foreign) parent returns `404` —
identical to a nonexistent one — instead of a 403 that would
leak its existence. `DELETE /images/{id}/` returns `404`
unless the requester may delete it — uploader, parent note's
owner, or parent comment's author — so the endpoint leaks no
existence oracle.

Upload validation: the declared `Content-Type` must match the
actual image bytes (Pillow format check — `415` on mismatch),
and images tripping the decompression-bomb limit return `413`.
Uploading to a soft-deleted comment is rejected (`400`).

Every `Image` row deletion — including note/tag/user cascade
deletes — removes its GCS object via a `post_delete`
receiver, so blobs are never orphaned.

### Reading Positions
```
GET    /api/v1/reading-positions/
POST   /api/v1/reading-positions/
POST   /api/v1/reading-positions/bulk/
PATCH  /api/v1/reading-positions/{id}/
DELETE /api/v1/reading-positions/{id}/
Authorization: Token <your-token>
```

Tracks the caller's last-read location per book. `POST`
upserts on `(user, book)`: the `book` name is validated
through the book-name lookup and stored canonicalized
(title case — `john`, `JOHN`, and `John` all resolve to the
`John` row), so case variants can never fork duplicate
positions. `book` is immutable afterwards — `PATCH` with a
different book returns `400`; re-seat a position via
delete + create instead. The upsert returns `201` when a
row is created and `200` when an existing row is updated.

`POST /reading-positions/bulk/` is a read implemented as
POST (the book list belongs in a body): `{"books": ["John",
"Genesis"]}` → `{"John": {"chapter": 3, "verse": 16},
"Genesis": null, ...}` — keys echo the names the client
sent, values are null for books with no stored position.
Names go through the same tolerant lookup as create
(unresolvable names return `400`); the array is capped at
100 entries. Because it is a read, the legacy `guest`
account may call it — matching `GET ?book=` — while every
other write still returns `403` for guest and `401`
anonymously.

---

## Database Schema

### Verse Model
```python
class Verse(models.Model):
    id = CharField(max_length=18, primary_key=True)  # VER + 15 chars
    book = CharField(max_length=50)
    chapter = IntegerField()
    verse = IntegerField()
    dbt_book_id = CharField(max_length=50)
```

### Tag Model
```python
class Tag(models.Model):
    id = CharField(max_length=18, primary_key=True)  # TAG + 15 chars
    user = ForeignKey(User, on_delete=CASCADE)
    name = CharField(max_length=100)
    parent_tag = ForeignKey('self', null=True, blank=True)
    created_at = DateTimeField(auto_now_add=True)
    updated_at = DateTimeField(auto_now=True)
```

**Constraints**:
- Unique combination of (user, name, parent_tag) for child tags
- `unique_top_level_tag_name_per_user`: conditional
  `UniqueConstraint(user, name)` where `parent_tag IS NULL` —
  `unique_together` cannot cover NULL parents. Declared in the
  model and enforced application-side by `TagSerializer`;
  **migration not yet generated** (`python manage.py
  makemigrations` is a required human step).

### Note Model
```python
class Note(models.Model):
    id = CharField(max_length=18, primary_key=True)  # NOT + 15 chars
    user = ForeignKey(User, on_delete=CASCADE)
    tag = ForeignKey(Tag, null=True, blank=True)
    note_text = TextField(blank=True)
    public = BooleanField(default=False)
    verses = ManyToManyField(Verse, through='NoteVerse')
    created_at = DateTimeField(auto_now_add=True)
    updated_at = DateTimeField(auto_now=True)
```

### NoteVerse Model
```python
class NoteVerse(models.Model):
    id = CharField(max_length=18, primary_key=True)  # NVE + 15 chars
    note = ForeignKey(Note, on_delete=CASCADE)
    verse = ForeignKey(Verse, on_delete=PROTECT)
    created_at = DateTimeField(auto_now_add=True)
```

**Constraints** (both declared, pending `makemigrations`):
- `unique_note_verse_link` on NoteVerse: one link per
  (note, verse).
- `unique_book_chapter_verse` on Verse: one row per
  (book, chapter, verse).

---

## External Services

### Digital Bible Platform (DBT) API

**Base URL**: `https://b4.dbt.io/api`

**Authentication**: API key passed as query parameter

**Key Endpoints Used**:
- `GET /bibles` - List available Bibles
- `GET /bibles/{bible_id}` - Get Bible details
- `GET /bibles/{bible_id}/books` - Get books in a Bible
- `GET /bibles/{bible_id}/{book_id}/{chapter}` - Get verses

**Client Implementation**: `bible/services/dbt/client.py`

**Usage Example**:
```python
from bible.services.dbt.client import DBTClient

client = DBTClient()
verses = client.get_verses(
    book='JHN',
    chapter='3',
    bible_id='ENGESV',
    verse_start=16,
    verse_end=17
)
```

**Translation Codes**:
- `ENGESV` - English Standard Version (text)
- `ENGESVN2DA` - ESV Audio Drama
- `ENGESVN1DA` - ESV Audio
- `ENGKJV` - King James Version
- And many more...

### API.Bible (NIV)

**Base URL**: `https://api.scripture.api.bible/v1`

**Authentication**: `api-key` header (never a query param);
read from `settings.API_BIBLE_KEY` (`config.yaml` locally,
Secret Manager / env in prod). `ApiBibleClient` raises
`ValueError` at construction when the key is unset — the NIV
filesets degrade to `provider_error` responses until a key is
configured.

**Endpoints Used**:
- `GET /bibles/{bible_id}/chapters/{BOOK}.{n}` - chapter text
  (`content-type=json`, verse numbers/spans + titles)
- `GET /bibles/{bible_id}/search` - full-text search
  (`offset`-based pagination)
- `GET /bibles/{bible_id}` - bible object (`copyrightStatement`)
- `GET /audio-bibles/{audio_bible_id}/chapters/{BOOK}.{n}` -
  presigned `resourceUrl` + optional verse `timecodes`

**Client Implementation**: `bible/services/apibible/client.py`
(registry: `bible/services/apibible/registry.py`)

**Fileset IDs** (synthetic, registered server-side):
- `ENGNIV_API` - New International Version (text)
- `ENGNIVC1DA` - NIV audio ("The Listener's Bible");
  canonicalizes to `ENGNIV_API`

Passage responses pass the upstream `meta` (FUMS) block
through so the frontend can report chapter views per
API.Bible's license terms.

### Generated Audio (Cloud TTS + GCS)

The Latvian Glück (`LVSGLU8`) audio is generated by us, not served
by DBT: the `all-jobs` Cloud Run Job (`terraform/all_jobs.tf`,
running `manage.py run_all_jobs` → `generate_chapter_audio`)
synthesizes chapters via Google
Cloud TTS and writes MP3 + timestamps JSON into the audio bucket.

**Generation modes** (`generate_chapter_audio --mode`):

- `verse` (default) — one TTS call per verse, MP3 chunks
  concatenated, timestamps derived from cumulative mutagen
  durations. Artifacts under
  `audio/{fileset}/{voice}/{BOOK}/{CHAPTER}_audio.mp3` (+ `_timestamps.json`).
- `chapter` — one TTS call per chapter (segmented under a
  ~4500-byte SSML input budget) with `<mark name="vN"/>` SSML
  marks; verse timestamps come from the API's `timepoints`
  (`enable_time_pointing=[SSML_MARK]`), offset by prior segment
  durations. Artifacts under
  `audio_chapter/{fileset}/{voice}/...`.
- `both` — a verse pass then a chapter pass over the same
  worklist. The Cloud Run Job runs with `--mode both`.

**Fileset ids** (see `bible/services/sword/registry.py`):

- `LVSGLU8C1DA` — verse-mode artifacts (existing objects stay
  valid; unchanged paths).
- `LVSGLU8C2DA` — chapter-mode artifacts.

`BiblePassageSerializer` (audio branch) and `AudioTimestampView`
resolve the mode from the fileset id via
`audio_mode_for_fileset()` — no query params, response shapes are
unchanged. The translations listing emits both audio filesets.

**Run state** (per month, JSON objects in the same bucket):

- Locks are per mode:
  `state/lock/{fileset}/{mode}/{YYYY-MM}.json` — a completed
  verse-mode run does not block a chapter-mode run. Locks written
  before per-mode paths existed
  (`state/lock/{fileset}/{YYYY-MM}.json`, no mode segment) are
  orphaned and ignored by new runs.
- If every pending chapter fails, the lock is left `running`
  (the run reports `reason=all_failed`) rather than marked
  completed with zero artifacts; it goes stale after
  `LOCK_STALE_HOURS` so a later run retries.
- Usage is shared:
  `state/usage/{fileset}/{YYYY-MM}.json` — the
  `MONTHLY_TTS_CHAR_LIMIT` cap is a per-fileset spend ceiling
  across both modes (chapter-mode SSML markup chars are billed
  too).

**Config env vars**: `GOOGLE_TTS_LANGUAGE_CODE`,
`GOOGLE_TTS_VOICE_NAME`, `GOOGLE_TTS_SAMPLE_RATE_HERTZ`,
`AUDIO_BUCKET_NAME`, `AUDIO_SIGNED_URL_TTL_SECONDS`,
`MONTHLY_TTS_CHAR_LIMIT`, `LOCK_STALE_HOURS`.

**Master job** (`run_all_jobs`): the `all-jobs` Cloud Run Job
(`terraform/all_jobs.tf`) runs `manage.py run_all_jobs`, which
executes the entries in the `JOBS` table of
`bible/management/commands/run_all_jobs.py` sequentially — each
entry is a `(label, argv, schedule)` tuple gated by a UTC
schedule predicate (`daily`, `weekly:<dow>`, `dom:N` /
`dom:odd`, `last_day_of_month`, `manual`). A failing entry is
logged and skipped, and the run exits non-zero naming the
failures. `--only LABEL` runs one entry ignoring its schedule,
`--force` runs all entries, `--dry-run` prints what would run.
It currently lists `generate_chapter_audio` on `dom:3` (the
monthly audio pass, which has no scheduler trigger),
`generate_audio_timestamps` on `dom:10` (the monthly STT
timestamps pass), plus `generate_gemini_audio` and
`generate_edge_audio` on `manual`
(the Edge entry is started daily by the `daily-edge-audio`
Cloud Scheduler job via a `jobs:run` container-args override
of `--only generate_edge_audio` — see
`terraform/scheduler.tf`; `manual` keeps an unfiltered trigger
from starting either). `all-jobs` replaced the standalone
`audio-generator` Cloud Run Job; it reuses that job's runtime
service account (still named `audio-generator`) and its
`audio-generator` Artifact Registry image repo. The deploy
workflow pins the Job to the
freshly built image via `gcloud run jobs update all-jobs`.

### Generated Timestamps (Cloud STT)

Some DBT audio filesets — the Latvian 1953 LVR (`LATLVRN1DA`,
`LATLVRP1DA`) and LAVNLI OT partials (`LATBSLP1DA`,
`LATBSLP2DA`) — have no DBT verse timestamps, so
`generate_audio_timestamps` produces them with Cloud
Speech-to-Text V1 (the only tier with a 60 min/month free
allowance) and writes `timestamps_generated/` artifacts into
the audio bucket. The `/api/v1/bible/timestamps/` contract is
unchanged: `DbtProvider.get_timestamps` falls back to the
generated artifact when DBT answers empty (or fails) for a
fileset registered in
`bible/services/dbt/timestamps_registry.py`
(`GENERATED_TIMESTAMPS_FILESETS`, `-opus16` suffix stripped —
opus/webm variants are the same recordings). Missing or
unreadable artifacts still answer `{"data": []}` (an artifact
read failure degrades like a miss, never a 502).

**Worklist**: `GET /bibles/filesets/{fileset_id}` — the
fileset's real chapter coverage (one row per chapter file,
carrying the signed `path` MP3 URL and `duration`) — minus
`timestamps_generated/` artifacts already present. A chapter
where DBT suddenly serves timestamps is skipped (never spend
STT budget where coverage exists); a *failed* DBT check means
"proceed to generate".

**Per chapter**: download the DBT MP3 → stage to `stt_staging/`
→ `long_running_recognize` (MP3 encoding inferred, no
`sample_rate_hertz`) → align word offsets to the reference
text (`LVSGLU8` SWORD text via `difflib` in
`google_stt/aligner.py`; verses without a matched word
interpolate between anchored neighbours; < 2 anchors fails)
→ upload the artifact (`meta.source: stt_v1`,
`alignment_score`, `low_confidence` below
`MIN_ALIGNMENT_SCORE`/per-fileset override) → delete staging.

**Budget**: global `state/stt-usage/{YYYY-MM}.json` seconds
counter (STT bills per account, in 1-second increments once
the LRO completes — even on empty results; a poll timeout is
also metered, since the LRO may complete server-side — the
client best-effort `cancel()`s it first), capped by
`MONTHLY_STT_SECONDS_LIMIT` (3300s ≈ 55 min, headroom under
the 60 min free tier). Per-fileset monthly lock at
`state/stt-lock/{fileset}/{YYYY-MM}.json`.

**Config env vars**: `MONTHLY_STT_SECONDS_LIMIT`,
`STT_LANGUAGE_CODE` (`lv-LV`), `STT_MODEL` (`default` — the
only long-form V1 model lv-LV supports), plus the shared
`AUDIO_BUCKET_NAME` and `LOCK_STALE_HOURS`. Requires
`speech.googleapis.com` and `roles/speech.client` on the
`audio-generator` SA (both in terraform).

### Generated Audio (Gemini API free tier)

`manage.py generate_gemini_audio` synthesizes the same LVSGLU8
worklist through the **Gemini API** `generateContent` endpoint
(`gemini-2.5-flash-preview-tts`), which has a free tier — unlike
Cloud TTS. It has two modes:

- **Local** (default): run on each boot (e.g. via launchd);
  writes MP3 + timestamps under `tts-out/` and resumes via the
  gitignored `gemini-tts-state.json`.
- **GCS** (`--upload-gcs`, auto-enabled when the `CLOUD_RUN_JOB`
  env var is set): the mode used by the `all-jobs` sub-job —
  artifacts upload to the audio bucket under
  `audio_gemini/{fileset}/{voice}/{BOOK}/{CHAPTER}_audio.mp3`
  (+ `_timestamps.json`), a distinct prefix from `audio/` and
  `audio_chapter/` so nothing can collide. Nothing serves these
  objects over the API yet — a future audio fileset id can map
  to GCS mode `"gemini"` via `audio_mode_for_fileset()`.

**GCS run state** (same bucket, all JSON):

- Completion comes from the artifact listing itself — a chapter
  with both objects counts as done.
- The daily request counter lives in
  `state/gemini/{fileset}/{voice}/{YYYY-MM-DD}.json` (Pacific
  day) so a same-day job retry resumes the spend.
- A **day-keyed run lock**
  `state/lock/{fileset}/gemini/{YYYY-MM-DD}.json` serializes
  same-day executions; a finished run marks it `completed` so
  reruns exit early (`already_completed`), and an all-failed run
  is left `running` rather than recorded as success.

- **Auth**: `GEMINI_API_KEY` in `config.yaml` (gitignored) or in
  the GCP `env-file` secret (the `all-jobs` container injects it
  via `DOTENV`) — a single string or a list of keys (one per
  free-tier GCP project; a .env value is comma-separated),
  normalized to `settings.GEMINI_API_KEYS`. Keys travel
  in the `x-goog-api-key` header, never in URLs or logs.
- **Segments**: each chapter splits into consecutive-verse
  segments under `--max-segment-chars` (default 3000; ~5 chars/sec
  at reverent pace keeps output under the ~655 s/16,384-token cap).
  On truncation — detected via `usageMetadata` output tokens near
  the cap or an implausible chars/sec, never via `finishReason`
  (it stays `STOP` even when truncated) — the segment halves and
  retries, bounded by a per-chapter retry budget.
- **Pacing/quota**: `--sleep-seconds` (default 21, ~3 RPM) between
  calls; `--max-requests` (default 14) per day. HTTP 429 rotates to
  the next key; all keys exhausted → stop for the day (a clean
  exit 0 — `run_all_jobs` never reports the sub-job failed).
- **Resume** (local mode): `gemini-tts-state.json` (gitignored,
  repo root) tracks completed chapters + a request counter that
  resets at midnight Pacific. Chapters already on disk count as
  done even if the state file is lost.
- **Output** (local mode): `tts-out/LVSGLU8/<BOOK>/<CH>.mp3` plus
  `<CH>_timestamps.json` — char-proportional verse-timestamp
  estimates with exact anchors at each synthesized segment
  boundary (Gemini TTS has no SSML-mark timepoints). MP3 encoding
  uses `lameenc` (no ffmpeg).
- **Scheduling**: `manual` only — the Gemini entry is no
  longer the daily trigger's target (Edge is, see below). It
  can still be run on demand via `gcloud run jobs execute
  all-jobs --overrides` with `--only generate_gemini_audio`.

### Generated Audio (Edge read-aloud TTS — Everita)

`manage.py generate_edge_audio` synthesizes the same LVSGLU8
worklist through the **Microsoft Edge read-aloud endpoint**
(`edge-tts` package, `lv-LV-EveritaNeural` by default — the
production voice chosen from the local voice comparison). It
is free and keyless (the same endpoint Edge's browser
read-aloud uses), so there is no API key, char budget, or
daily quota — runs are bounded by `--max-chapters` (default
60) and `--max-minutes` (default 60) instead, keeping each
invocation inside the Job's 5400 s timeout. Same dual modes
as the Gemini command (`--upload-gcs` / `CLOUD_RUN_JOB`
auto-detect; artifacts under `audio_edge/`, GCS mode
`"edge"`), minus the request counter — completion comes from
the object listing alone. The day-keyed run lock lives at
`state/lock/{fileset}/edge/{YYYY-MM-DD}.json`.

Differences from the Gemini pipeline:

- **Per-verse synthesis**: one request per verse (each verse
  is its own `edge_tts.Communicate`), MP3 frames
  byte-concatenated (48 kbps CBR — the only output format the
  endpoint honors). Verse timestamps are therefore EXACT
  cumulative stream durations, not estimates.
- **Output**: MP3 48 kbps CBR from the endpoint directly —
  no `lameenc` re-encode.
- **Pacing**: `--sleep-seconds` (default 0.5) between verse
  calls; retries (3 attempts, 2 s backoff) absorb the
  endpoint's occasional `NoAudioReceived`/WSS drops.
- **Scheduling**: `terraform/scheduler.tf` `daily-edge-audio`
  fires `all-jobs` daily at 09:00 UTC with container args
  `--only generate_edge_audio`; the unscheduled monthly
  `dom:3` Google TTS entry can never piggyback (it needs a
  plain run on day 3, which no trigger performs).

---

## Authentication & Authorization

### Token Authentication

**Obtain Token**:
```bash
curl -X POST http://localhost:8000/api/token/ \
  -H "Content-Type: application/json" \
  -d '{"username": "testuser", "password": "password123"}'
```

**Use Token**:
```bash
curl http://localhost:8000/api/v1/notes/ \
  -H "Authorization: Token 9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b"
```

### Permission Levels

1. **Unauthenticated Users**:
   - Can access Bible passages
   - Can view public notes
   - Can retrieve a tag that carries at least one public note
   - Can read public-note comment counts
     (`GET /api/v1/comments/counts/`)
   - Cannot create/modify content — every write returns 401

2. **Legacy Guest User** (`guest`):
   - Locked account that owns rows created before device
     auto-provisioning was removed; reachable only via a
     previously issued token
   - Can read guest-owned tags/notes/comments and public
     content
   - Cannot write — every POST/PUT/PATCH/DELETE returns 403
     (`GuestReadOnly` permission)

3. **Authenticated Users**:
   - Full access to Bible passages
   - Can create/read/update/delete their own tags
   - Can create/read/update/delete their own notes
   - Can view public notes from other users
   - Cannot modify other users' content

4. **Staff Users**:
   - Access to Django admin panel
   - Same API permissions as regular users

### Anonymous Behavior

When no authenticated user is available:
- Tags: `list` returns `[]`; `retrieve` works only for tags
  carrying at least one public note
- Notes: Only public notes are visible. Note reads (`GET
  /api/v1/notes/`, `GET /api/v1/notes/{id}/`,
  `GET /api/v1/notes/order-index/`) are open to anonymous
  callers and scoped to public notes. All note writes
  (`POST`/`PUT`/`PATCH`/`DELETE`, `reorder/`, `move/`,
  `bulk/`, `linked/`) require authentication and return
  401/403 for anonymous callers.
- Creation: Rejected — anonymous writes return 401. Writes
  authenticated as the legacy `guest` account (via an old
  token) return 403 (`GuestReadOnly`)

---

## Development Workflow

### Code Style

**Python Line Length**: Maximum 79 characters (PEP 8)

**Example**:
```python
# Good
response_format = request.query_params.get(
    'response_format', 'text'
)

# Bad - line too long
response_format = request.query_params.get('response_format', 'text')
```

### Adding a New Bible Translation

1. Find the translation code in DBT API
2. Add mapping in `bible/utils/bible_books.py` if needed
3. Test with the Bible passage endpoint

### Adding a New Model Field

1. Update the model in `models.py`
2. Create migration: `python manage.py makemigrations`
3. Apply migration: `python manage.py migrate`
4. Update serializer in `serializers.py`
5. Update API documentation

### Database Migrations

```bash
# Create migrations
python manage.py makemigrations

# View SQL for migration
python manage.py sqlmigrate annotations 0001

# Apply migrations
python manage.py migrate

# Rollback migration
python manage.py migrate annotations 0001
```

### Logging

Logs are stored in `logs/` directory (local development only —
cloud filesystems are read-only, so production logs go to the
console):
- `debug.log` - `django`, `bible`, `annotations`, and `users`
  records at INFO and above
- `error.log` - ERROR-level records from the same loggers

**View logs**:
```bash
tail -f logs/debug.log
```

### Monitoring (Sentry)

Error monitoring via Sentry is wired but opt-in: when
`SENTRY_DSN` is empty the SDK stays uninitialized and nothing is
sent (local dev and CI are silent by default).

- **Local**: set `SENTRY_DSN` in `config.yaml` (see
  `config_template.yaml`) to the `bible-research-api` project's
  Client Key DSN.
- **Production (GAE)**: create the `SENTRY_DSN` secret in GCP
  Secret Manager (Terraform reads its metadata via
  `terraform/secrets.tf`); the env var of the same name also
  works. `GAE_VERSION` is used as the Sentry release;
  environment is `production` on GCP, `development` otherwise.
- Uncaught exceptions (HTTP 500s) are captured automatically.
  Provider failures surfaced through the `error`/`error_code`
  contract are additionally captured at `warning` level with an
  `error_code` tag (`rate_limited`/`provider_error` only —
  `not_found` and `book_not_in_fileset` are never sent). Log
  records INFO+ are breadcrumbs only, not events.
- `send_default_pii=False`; `before_send` drops request bodies
  on `/api/token/` and `/api/v1/users/` (they contain
  passwords).
- `sentry-trace`/`baggage` are allowed CORS headers so the
  frontend can link its traces to backend requests.

---

## Testing

The pytest suite runs against the real
`bible_research.settings` module — `conftest.py` imports it and
overrides only the test-required deltas (SQLite in-memory
database, dummy provider keys, generous throttle rates), so
tests exercise the production `REST_FRAMEWORK`
authentication/permission stack and `MIDDLEWARE` chain.
`config.yaml` is not required: under pytest, `settings.py`
treats a missing config file as empty instead of raising.

```bash
python -m pytest -q                # full suite
python -m pytest annotations/ -q   # one app
```

### Test User Setup

```bash
# Create test users
python scripts/create_test_user.py

# Verify users
python manage.py shell
>>> from django.contrib.auth import get_user_model
>>> User = get_user_model()
>>> User.objects.all()
```

### Manual API Testing

**Using Postman**:
Import `bible-research.postman_collection.json`

**Using cURL**:
```bash
# Get token
TOKEN=$(curl -s -X POST http://localhost:8000/api/token/ \
  -H "Content-Type: application/json" \
  -d '{"username":"testuser","password":"password123"}' \
  | python -c "import sys, json; print(json.load(sys.stdin)['token'])")

# Create tag
curl -X POST http://localhost:8000/api/v1/tags/ \
  -H "Authorization: Token $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Grace"}'

# List notes
curl http://localhost:8000/api/v1/notes/ \
  -H "Authorization: Token $TOKEN"
```

---

## Deployment

### Environment Variables

For production, use environment variables instead of `config.yaml`:

```bash
export SECRET_KEY='your-secret-key'
export DEBUG=False
export DBT_KEY='your-dbt-api-key'
export DATABASE_URL='postgresql://user:pass@host:5432/dbname'
```

### Static Files

```bash
python manage.py collectstatic --noinput
```

### Database Backups

Backups are automated by `db_backups/local_db_backup.sh`, which
reads the connection details from `config.yaml`
(`DATABASES.default`) and runs `pg_dump` over TCP. No local
PostgreSQL server or role is needed — only the client tools.

**Ubuntu/Debian setup** (first time only):

```bash
sudo apt update
sudo apt install postgresql-client
```

**Run a backup** (from the repo root — the script uses relative
paths):

```bash
bash db_backups/local_db_backup.sh
```

This writes a timestamped custom-format dump to
`db_backups/backup_<YYYYMMDD_HHMMSS>.dump`.

**Restore**: the dumps are custom format, so use `pg_restore`
(not `psql`). The configured DB user is read-only, so restores
target a local PostgreSQL instance:

```bash
# Install and start a local server (first time only)
sudo apt install postgresql
sudo systemctl enable --now postgresql

# Create a role matching your OS user and a target database
sudo -u postgres createuser "$USER"
sudo -u postgres createdb -O "$USER" bibledb

# Restore a dump into it
pg_restore -d bibledb --clean --if-exists \
  db_backups/backup_20251228_055249.dump
```

To run the app against the restored copy, repoint `config.yaml`'s
`DATABASES.default` at the local database.

### Production Checklist

- [ ] Set `DEBUG=False`
- [ ] Use strong `SECRET_KEY`
- [ ] Configure PostgreSQL with SSL
- [ ] Set up CORS for frontend domain
- [ ] Enable HTTPS
- [ ] Configure allowed hosts
- [ ] Set up database backups
- [ ] Configure logging to external service
- [ ] Create `SENTRY_DSN` in Secret Manager (enables Sentry
      monitoring — see "Monitoring (Sentry)")
- [ ] Review security settings

---

## Contributing

### Workflow

1. Create a feature branch
2. Make changes following code style guidelines
3. Write/update tests
4. Update documentation
5. Submit pull request

### Commit Messages

Follow conventional commits with **UPPERCASE type and capitalized 
message**:

**Format**: `Type: Capitalized message description`

**Examples**:
- ✅ `Feat: Add verse highlighting feature`
- ✅ `Fix: Correct tag deletion cascade behavior`
- ✅ `Docs: Update API endpoint documentation`
- ✅ `Refactor: Simplify note serializer logic`
- ❌ `feat: add feature` (lowercase - incorrect)
- ❌ `Feat: add feature` (message not capitalized - incorrect)

**Commit Types**:
- **Feat**: New feature or functionality
- **Fix**: Bug fix or error correction
- **Docs**: Documentation changes only
- **Refactor**: Code restructuring without changing behavior
- **Test**: Adding or updating tests
- **Chore**: Maintenance tasks (dependencies, config, etc.)
- **Style**: Code formatting, whitespace, etc.
- **Perf**: Performance improvements

---

## Additional Resources

- [Django Documentation](https://docs.djangoproject.com/)
- [Django REST Framework](https://www.django-rest-framework.org/)
- [DBT API Documentation][bible-brain]
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)

---

## Contact & Support

For questions or issues:
1. Check this documentation
2. Review existing issues in the repository
3. Create a new issue with detailed information

---

**Last Updated**: 2026-10-28

[bible-brain]: https://www.faithcomesbyhearing.com/bible-brain/api-reference
