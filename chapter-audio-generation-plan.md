# Plan: Per-Chapter Audio Generation Mode + Dual Audio API Paths

Status: plan only — not implemented. Repo: `bible_research/` (backend),
with a small read-side contract change consumed by `reactive-bible`.

## Background — how it works today

`python manage.py generate_chapter_audio --fileset-id LVSGLU8`
(Cloud Run Job `audio-generator`, `terraform/audio_generator.tf`):

1. Worklist = `(book_id, chapter)` pairs from the SWORD module
   (`SwordClient.list_chapters`).
2. Per-chapter run lock + monthly char budget tracked as JSON objects
   in the audio bucket (`state/lock/…`, `state/usage/…`).
3. `Synthesizer.run()` synthesizes **each verse separately** via
   Cloud TTS, measures every verse MP3's duration with mutagen,
   concatenates the MP3 byte-chunks into one chapter MP3, and builds
   the timestamp payload `{verse_start, timestamp}` from cumulative
   durations.
4. `gcs.upload_chapter_artifacts` writes:
   `audio/{fileset}/{voice}/{BOOK}/{CHAPTER}_timestamps.json` then
   `…_audio.mp3`.

Read side:

- `GET /api/v1/bible/?…&response_format=audio&fileset_id=LVSGLU8C1DA`
  → `BiblePassageSerializer` → `gcs.chapter_audio_exists` +
  `gcs.signed_audio_url` + `gcs.read_timestamps_json` (for
  `duration_seconds` / `file_size_bytes`).
- `GET /api/v1/bible/timestamps/?fileset_id=&book=&chapter=` →
  `AudioTimestampView` → `gcs.read_timestamps_json` →
  `{data: [{verse_start, timestamp}]}`.

So a "chapter MP3" already exists, but it is stitched from N
per-verse TTS calls. The new mode does **one synthesis call per
chapter** (or a few, for oversized chapters) and gets verse-level
timestamps from the TTS API itself instead of mutagen arithmetic.

## Goal

- One management command supporting two generation modes:
  - `verse` — current per-verse synthesis + concat (unchanged).
  - `chapter` — whole-chapter synthesis with verse timestamps from
    SSML marks.
- Both artifact sets coexist in the same GCS bucket under distinct
  prefixes.
- The Latvian audio API exposes **two audio paths**: existing
  per-verse-generated audio keeps working; per-chapter-generated
  audio is served via a second audio fileset id.

## Design

### 1. Chapter-mode synthesis via SSML marks

Cloud TTS supports `enable_time_pointing=["SSML_MARK"]`: put
`<mark name="v3"/>` before each verse in the SSML input and the
response's `timepoints` give exact `mark_name → time_seconds`
offsets. Build one SSML doc per chapter:

```xml
<speak><mark name="v1"/>…verse 1 text… <mark name="v2"/>…
</speak>
```

This gives **exact** verse timestamps — no reliance on the
assumption that per-verse and whole-chapter synthesis produce
similar timing (they won't be identical; marks make that moot).

Length limit: the sync `synthesize_speech` API caps input at
5000 bytes. Most chapters fit; long ones (Psalm 119 ≈ 10k+ chars)
must be **segmented**: group consecutive verses into SSML docs under
a byte budget (≈4500 bytes incl. markup), synthesize each segment
once, concatenate MP3 bytes, and offset each segment's mark
timepoints by the cumulative duration of prior segments (mutagen, as
today). Worst case ≈2–3 API calls per chapter vs. N verse calls.

SPIKE FIRST (blocks the design): verify the configured voice
(`lv-LV-Chirp3-HD-Sadachbia`) honors `enable_time_pointing` +
`<mark>`. Chirp 3 HD supports only an SSML subset. If marks are
unsupported: either pick a voice that supports them, or keep
`chapter` mode audio but reuse per-verse-derived timestamps
(approximate — not recommended), or drop chapter mode. Decide after
the spike; also fold in the TTS-provider research (see §8) — if we
switch providers (e.g. Gemini TTS), mark support must be re-verified.

### 2. Code changes (`bible_research/`)

**`google_tts/client.py`**
- Add `synthesize_ssml(ssml, language_code, voice_name,
  sample_rate_hertz) -> (bytes, list[Timepoint])` using
  `SynthesisInput(ssml=…)` +
  `enable_time_pointing=[SSML_MARK]`; same `QuotaExceeded` mapping.

**`google_tts/synthesizer.py`**
- `Synthesizer.run(..., mode="verse")` dispatching to:
  - `_run_per_verse` — existing loop, unchanged.
  - `_run_per_chapter` — build segmented SSML with `<mark>` tags
    (XML-escape verse text), call `synthesize_ssml`, map
    `timepoints` to `{verse_start, timestamp}` (strip the `v`
    prefix, add segment offset), concatenate MP3 segments.
    `chars_used` = billed chars (SSML markup chars are billed too —
    account for that in the budget check).
- Output shape (`ChapterArtifacts`,
  `{duration_seconds, data:[{verse_start, timestamp}]}`) unchanged.

**`storage/gcs.py`**
- Add `mode: str = "verse"` param to `chapter_object_paths`,
  `chapter_audio_exists`, `list_completed_chapters`,
  `upload_chapter_artifacts`, `read_timestamps_json`,
  `signed_audio_url`. `mode="verse"` keeps today's
  `audio/{fileset}/{voice}/…` paths (existing objects stay valid);
  `mode="chapter"` uses `audio_chapter/{fileset}/{voice}/…`.
- Add `mode` to the `_signed_url_cache` key.
- Per-mode run state: `state/{kind}/{fileset}/{mode}/{ym}.json` so
  the two modes have independent locks (`already_completed` for
  verse must not block chapter). Note: this gives each mode its own
  `MONTHLY_TTS_CHAR_LIMIT` budget (total potential spend = 2× cap);
  acceptable, or share `usage` while keeping per-mode `lock` —
  decide at implementation time.

**`sword/registry.py`**
- Add a second audio fileset id to `LVSGLU8` meta, e.g.
  `"audio_fileset_id_chapter": "LVSGLU8C2DA"`, resolved by
  `canonical_sword_fileset_id`.
- New helper `audio_mode_for_fileset(fileset_id) -> "verse" |
  "chapter"` (default `"verse"`).

**`generate_chapter_audio.py`**
- `--mode {verse,chapter,both}` (default `verse` — preserves current
  job behavior). `both` runs verse pass then chapter pass, each with
  its own lock/usage/budget pass over the same worklist.
- Pass `mode` through to `list_completed_chapters`,
  `acquire_run_lock`, `read_monthly_usage`,
  `increment_monthly_usage`, `mark_run_complete`,
  `upload_chapter_artifacts`, `Synthesizer.run`.

### 3. API: two audio paths (read side)

Idiomatic approach — a second audio fileset id, no new query params:

- `LVSGLU8C1DA` → verse-mode artifacts (unchanged).
- `LVSGLU8C2DA` → chapter-mode artifacts.
- `BiblePassageSerializer` (audio branch) and `AudioTimestampView`
  derive `mode = audio_mode_for_fileset(fileset_id)` and pass it to
  the gcs helpers. Response shapes unchanged (`audio_url`,
  `duration_seconds`, `file_size_bytes`, `{data:[…]}`).
- `SwordClient.get_translation_listing` emits both audio filesets so
  the frontend can pick either.

Alternative considered and not recommended: an `audio_mode` query
param on both endpoints — it bypasses the fileset-routing convention
and forces frontend param plumbing.

### 4. Infra

- `terraform/audio_generator.tf`: add `--mode both` to the job
  command (or a second `google_cloud_run_v2_job` per mode — `--mode
  both` in one job is simpler and keeps a single image lifecycle).
- Scheduler stays commented out per AGENTS.md.
- New env/config only if the provider changes (§8) — e.g. a Gemini
  API key via Secret Manager.

### 5. Tests (pytest, mock at the seams like existing tests)

- Synthesizer chapter mode: fake `synthesize_ssml` returning fixed
  timepoints + fake MP3 durations → assert mark→verse mapping,
  segment offsets, concatenation, `chars_used`.
- Segment splitting: oversized mock chapter → >1 SSML call, correct
  offsets.
- gcs: `chapter_object_paths` per mode; `list_completed_chapters`
  isolation between modes.
- Command: `--mode chapter/both` wiring, dry-run, per-mode lock.
- API: `LVSGLU8C2DA` resolves to chapter paths in serializer +
  timestamps view (patch gcs like `test_audio_timestamps_endpoint`).

### 6. Docs

- Update `bible_research/DEVELOPER_GUIDE.md` (audio section: modes,
  GCS layout, second fileset id, new flag) — required by workspace
  rules.
- Update `bible_research/AGENTS.md` fileset list (`LVSGLU8C2DA`).

### 7. Rollout

1. Spike: SSML-mark support check for the Chirp voice (§1).
2. Implement behind `--mode` (default `verse` = no behavior change).
3. Run `--mode chapter` once locally/Cloud Run for a few chapters;
   spot-check timestamps against audio.
4. Backfill all chapters (`--mode chapter` or `both`); publish
   `LVSGLU8C2DA` in the translations listing.
5. Frontend selects the new fileset id when the user wants
   chapter-synthesized audio.

### 8. TTS provider research — findings (better Latvian voice)

Full investigation done; summary of what matters for this plan:

- **Current voice is already Google's best lv-LV Cloud TTS option**:
  `lv-LV-Chirp3-HD-*` (GA 2025). Standard/WaveNet are worse; nothing
  better exists in classic Cloud TTS for Latvian.
- **"Gemini app" quality = Gemini TTS**, available via API two ways:
  - Gemini API (`gemini-2.5-flash-preview-tts` /
    `gemini-2.5-pro-preview-tts`): lv-LV officially supported,
    8192-token input limit (whole chapter per request — good fit for
    chapter mode!), style-steerable prompts. Free tier ≈10–15
    req/day — too small for the ~1.2k-chapter batch but fine for
    testing. Paid ≈$0.015/min → **~$70 whole Bible, ~$35 Batch API**.
  - Cloud TTS `gemini-2.5-flash-tts`: same models, no free
    allotment, ~150 QPM.
  - **No SSML, no timestamps** — returns raw PCM. This is the key
    tension with §1's mark-based timestamps.
- **Azure Speech**: `lv-LV-EveritaNeural`/`lv-LV-NilsNeural`, good
  neural quality, **native word-boundary timestamps** (unique among
  paid options — solves verse sync without marks), free tier only
  0.5M chars/mo, ~$60–80 paid for the batch.
- **Free/open**: Piper `lv_LV-aivars-medium` (trained on Latvian
  audiobook narration — best free option, self-host, no timestamps);
  Meta `mms-tts-lav` (CC-BY-NC license — non-commercial only).
- Rejected: Polly (no Latvian), ElevenLabs (~$400+, marginal),
  OpenAI TTS (unverified Latvian), edge-tts (ToS gray area),
  Tilde (enterprise pricing, worth an email for native quality).

**Design implication — provider × timestamp strategy matrix:**

| Provider | Chapter audio | Verse timestamps |
|---|---|---|
| Cloud TTS Chirp HD | segments + concat | SSML marks (spike §1) |
| Gemini TTS | 1 req/chapter | none — forced align or per-verse |
| Azure | SSML/marks or word boundary | word-boundary events (native) |
| Piper/MMS | segments + concat | none — forced align |

If we adopt Gemini for quality, chapter-mode timestamps need either
(a) keep per-verse mode for the timestamped path and offer chapter
audio without sync, (b) forced-alignment post-pass, or (c) Azure
instead. Decide after the §1 spike + a quality A/B of Gemini vs
Chirp on one Latvian chapter.

**If a second provider is adopted**: wrap it behind a small
provider interface (`synthesize(text/ssml) -> audio + optional
timepoints`) alongside `GoogleTTSClient`, add provider/voice to the
GCS path (voice already in path covers this if voice names are
namespaced per provider, e.g. `gemini-2.5-flash-tts:Kore`), and add
per-provider usage counters.
