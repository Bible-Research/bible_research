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
- **Framework**: Django 4.2.6
- **API**: Django REST Framework
- **Database**: PostgreSQL (production) / SQLite (development)
- **External API**: Digital Bible Platform (DBT) API v4
- **Authentication**: Token-based (DRF TokenAuthentication)

### Project Structure
```
bible_research/
├── bible/                  # Bible data and retrieval
│   ├── models.py          # Verse model
│   ├── views.py           # Bible passage endpoints
│   ├── serializers.py     # Bible data serialization
│   ├── services/          # External API clients
│   │   └── dbt/          # DBT API client
│   └── utils/            # Helper functions (book mappings)
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
- Python 3.8+
- PostgreSQL (for production) or SQLite (for development)
- DBT API key (from [Bible Brain][dbt-api])

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
   pip install -r requirements.txt
   ```

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

1. **Guest User** (for anonymous access)
   - Username: `guest`
   - Email: `guest@example.com`
   - Purpose: Default user for unauthenticated requests

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
- Automatic book name normalization

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
- **Authenticated users**: See their own tags and notes
- **Unauthenticated users**: See guest user's tags and public notes
- **Note visibility**: Controlled by `public` boolean field

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
    "date_joined": "2026-03-01T07:11:10Z"
  },
  "token": "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0",
  "message": "User registered successfully"
}
```
**Notes**:
- Email is optional
- Password must be at least 8 characters
- Returns authentication token automatically
- No authentication required for this endpoint

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
  "date_joined": "2026-03-01T07:11:10Z"
}
```

### Bible Passages

#### Get Bible Passage (Text)
```
GET /api/v1/bible/?passage=John+3&fileset_id=ENGESV
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
  (e.g., "John 3", "2 Chronicles 14")
- `response_format`: "text" or "audio" (default: "text")
- `fileset_id`: DBT fileset ID for the specific translation and
  format (default: "ENGESV")
  - For text: Use translation codes like "ENGESV", "ENGKJV"
  - For audio: Use audio fileset codes like "ENGESVN2DA"

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
          "type": "text_plain"
        },
        {
          "id": "ENGESVN2DA",
          "type": "audio_drama"
        }
      ]
    }
  ]
}
```

**Query Parameters**:
- `language_iso` (optional): Filter by ISO language code
  (e.g., "eng", "lvs")

**Example**:
```
GET /api/v1/bible/translations/?language_iso=eng
```

### Tags

#### List Tags
```
GET /api/v1/tags/
Authorization: Token <your-token>
```

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

#### Update Tag
```
PUT /api/v1/tags/{tag_id}/
Authorization: Token <your-token>
Content-Type: application/json

{
  "name": "Divine Love"
}
```

#### Delete Tag
```
DELETE /api/v1/tags/{tag_id}/
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

**Query Parameters**:
- `tag_id` (required): Tag ID to filter notes
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
`provider_error`) describing the failure.

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
  "created_at": "2025-12-28T03:51:55Z",
  "updated_at": "2025-12-28T03:51:55Z"
}
```

#### Get Note by ID
```
GET /api/v1/notes/{note_id}/
Authorization: Token <your-token>
```

#### Update Note
```
PUT /api/v1/notes/{note_id}/
Authorization: Token <your-token>
Content-Type: application/json

{
  "note_text": "Updated note text...",
  "public": true
}
```

#### Delete Note
```
DELETE /api/v1/notes/{note_id}/
Authorization: Token <your-token>
```

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
   - Can view guest user's tags
   - Cannot create/modify content

2. **Authenticated Users**:
   - Full access to Bible passages
   - Can create/read/update/delete their own tags
   - Can create/read/update/delete their own notes
   - Can view public notes from other users
   - Cannot modify other users' content

3. **Staff Users**:
   - Access to Django admin panel
   - Same API permissions as regular users

### Guest User Behavior

When no authenticated user is available:
- Tags: Guest user's tags are shown
- Notes: Only public notes are visible
- Creation: New content is assigned to guest user

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

Logs are stored in `logs/` directory (development only):
- `django.log` - General Django logs
- `bible.log` - Bible app logs
- `annotations.log` - Annotations app logs

**View logs**:
```bash
tail -f logs/django.log
```

---

## Testing

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

```bash
# Backup
pg_dump bibledb > db_backups/backup_$(date +%Y%m%d).sql

# Restore
psql bibledb < db_backups/backup_20251228.sql
```

### Vercel Deployment

The project includes Vercel configuration:
- `vercel.json` - Vercel settings
- `build_files.sh` - Build script

### Production Checklist

- [ ] Set `DEBUG=False`
- [ ] Use strong `SECRET_KEY`
- [ ] Configure PostgreSQL with SSL
- [ ] Set up CORS for frontend domain
- [ ] Enable HTTPS
- [ ] Configure allowed hosts
- [ ] Set up database backups
- [ ] Configure logging to external service
- [ ] Set up monitoring (e.g., Sentry)
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
- [DBT API Documentation][dbt-api]
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)

[dbt-api]: https://www.faithcomesbyhearing.com/bible-brain/api-reference

---

## Contact & Support

For questions or issues:
1. Check this documentation
2. Review existing issues in the repository
3. Create a new issue with detailed information

---

**Last Updated**: 2026-03-01
