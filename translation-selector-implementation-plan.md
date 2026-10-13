# Plan: Normalize translation/audio selection UX

Audience: AI agent implementing this change. Two repos, one workspace:

- Backend: `bible_research/` — Django 5.2 + DRF, provider routing by
  `fileset_id` (DBT default, SWORD `LVSGLU8*`, ESV `ENGESV_API`).
- Frontend: `reactive-bible/` — React 18 + TS, Mantine v6, Zustand
  (`src/store.tsx`), all API calls in `src/api.tsx`.

Workspace rules that apply: 79-char max line length in every file;
never run Django migrations; commit format `Type: Capitalized message`;
feature branches only; update each repo's `DEVELOPER_GUIDE.md`.

Companion doc with raw API dumps:
`translation-selector-investigation-and-plan.md` (same directory)

## Current state (2026-10-13)

- Frontend: fully merged on `reactive-bible` main —
  `src/utils/filesetGroups.ts` (`groupFilesets`,
  `resolveAudioFileset` with the `alternates` chain,
  `resolveTextFileset`, size→coverage parsing),
  `BookNotInFilesetError` in `src/api.tsx`, and wiring in
  `Audio.tsx`, `hooks/useAudioPlaylist.ts`, `PassageView.tsx`,
  `useVerseTexts.ts`, `TranslationSelector.tsx` (rewritten
  selector), `AddTagNoteModal.tsx`, `CopyrightNotice.tsx`, and
  the ENGKJV fileset injection, plus tests
  (`filesetGroups.test.ts`, `useAudioPlaylist.test.ts`, …).
- Backend: implemented but NOT merged — exists only on
  `bible_research` branch `feat/fileset-groups`:
  `bible/services/fileset_groups.py`, `text_options`/
  `audio_options` fields in `translation_service.py` responses,
  the `book_not_in_fileset` 404 contract (`serializers.py`,
  `views.py`), tests `test_fileset_groups.py` +
  `test_book_not_in_fileset.py`. None of it is on `main`
  (`git show main:bible/services/fileset_groups.py` fails). The
  deployed frontend works via its client-side grouping fallback.
- Remaining work: rebase `feat/fileset-groups` onto current
  `main` and merge. Watch the noted regression risk around
  `bible/utils/provider_errors.py`: that module was added on
  `main` AFTER the branch's merge-base, so `git diff
  main...feat/fileset-groups -- bible/utils/provider_errors.py`
  is empty and the file does not exist on the branch. `main`'s
  `serializers.py`/`views.py` now import
  `PassageNotFoundError`/`provider_error_fields` from it, while
  the branch still inlines `_book_not_in_fileset_body` — expect
  merge conflicts there; resolve so the `provider_errors`
  refactor survives and the new `book_not_in_fileset` bodies
  keep the same `error`/`error_code` field names.

## Problem (verified against live API, 2026-10-02)

`/api/v1/bible/translations/?language_iso=` returns raw provider
filesets `{id, type, size}`; the selector renders them as raw radio
options (e.g. `Audio OTP (LATBSLP1DA-opus16)`). Symptoms:

- Up to 8 audio radios per translation: {plain,drama} × {OT,NT,OTP…}
  × {mp3,opus16} — users must manually pick testament and codec.
- Text filesets are also testament-split (`ENGESVO_ET`/`ENGESVN_ET`,
  `ENGKJVN_ET`/`ENGKJVO_ET`, `ENGNAS*`, `EN1WEB*`…). Text has NO
  auto-switch — only an after-the-fact hint in `PassageView`.
- Audio auto-switch exists but uses N↔O id surgery
  (`findTestamentFallback`, `src/utils/bibleUtils.ts:393`) which
  returns null for `P\d` ids — LAVLVR's `LATLVRP1DA`↔`LATLVRN1DA`
  pair is unreachable.
- `filesetCoversTestament` (`bibleUtils.ts:310`) mis-parses `NTPOTP`,
  `OTP`, `NTP` (`startsWith` on the wrong side).
- Uncovered book → backend returns HTTP 200
  `{"verses": [], "message": "No verses found…"}` — even for audio.
  No way to distinguish "book not in fileset" from real failures.
- `ENGKJV` translation has no `ENGKJV` fileset — the offline bundled
  KJV (`getVersesInChapter` special-cases `'ENGKJV'`) is unreachable
  via the selector.
- Audio-only translations (ENGCSB, ENGGNB, both Latvian audio Bibles)
  leave `activeTextFilesetId` stale → `PassageView` spins forever.
- Fileset id prefix ≠ translation abbr (ENGWWH → `EN1WEB*`) — never
  group by global prefix; only within one translation's list.

## Target UX

Pick a VERSION; audio is a capability. Selector layout:

```
[English | Latvian]                       (SegmentedControl)
Version  [ searchable Select: name (abbr) + chips:
           "Audio" / "Text only" / "Audio only" ]
Text     auto-resolved per testament (no picker); show resolved
         fileset id(s) as dimmed info text
Audio    ( ) None
         (•) Read aloud — Full Bible             ← kind=audio
         ( ) Dramatized — New Testament only     ← kind=audio_drama
         ( ) Generated voice — Full Bible        ← kind=generated
Advanced ▸ (collapsed; raw fileset radios for debugging)
```

Decisions confirmed with user:
- Keep the raw-fileset section, collapsed, for debugging.
- Audio-only translation: if the currently active text fileset
  belongs to a different language, auto-select a text translation in
  the SAME language (prefer complete-coverage text). If none exists
  in that language, keep current text and show an info note.
- opus16 hidden from UI; it is the default codec, mp3 is the
  automatic fallback.

## Data model: "fileset groups" (a.k.a. audio/text options)

Normalize `{id,type,size}` filesets within ONE translation into
options:

```ts
interface FilesetOption {
  id: string;                    // synthetic: `${abbr}:${kind}:${n}`
  kind: 'audio' | 'audio_drama' | 'generated' | 'text';
  /** testament → codec → concrete fileset id */
  byTestament: {
    OT?: { mp3?: string; opus16?: string; generated?: string };
    NT?: { mp3?: string; opus16?: string; generated?: string };
  };
  coverageLabel: string;         // 'Full Bible' | 'New Testament
                                 //  only' | '…(partial)' etc.
  partial: { OT: boolean; NT: boolean };
}
```

Grouping algorithm (implement once, mirror semantics both sides):

1. Split codec: id ending `-opus16` → codec `opus16`, base id =
   stripped id; else codec `mp3`. (`LVSGLU8C1DA`, `ENGESV_API` →
   codec `generated`/`api` — treat as single-variant, see below.)
2. Audio grouping key: `(type, digit)` where digit comes from the
   trailing `([CONPS])(\d)DA` id suffix — `1` plain, `2` drama.
   Filesets not matching the suffix pattern (`LVSGLU8C1DA`,
   `ENGESV_API`) each become their own option; kind `generated` for
   the SWORD TTS id, `audio` for `ENGESV_API`.
3. Coverage comes from `size`, NOT the id letter: `C` → OT+NT;
   else `size` containing `NT` → NT, containing `OT` → OT;
   `P` anywhere in size → mark those testaments partial.
   (`NTPOTP` → both, partial; `OTP` → OT partial; `NT` → NT.)
4. Merge group members into `byTestament`. When two members claim
   the same testament prefer complete over partial, and non-'P'
   lettered id over 'P' id.
5. Text grouping: only `type === 'text_plain'`. Ignore
   `text_usx`/`text_json`/`text_format`. Same size→coverage rule;
   a translation needs at most one `text` option — merge all
   `text_plain` filesets into a single `byTestament` map.

## Backend changes (`bible_research/`)

DONE ON BRANCH `feat/fileset-groups` (unmerged — none of this is
on `main`; see "Current state" above). The items below describe
what that branch implements; nothing remains to write, only to
rebase + merge.

1. `bible/services/translation_service.py` — after merging DBT +
   SWORD + ESV entries, compute `text_options`/`audio_options` per
   translation using the algorithm above and include them in the
   response (keep `filesets` unchanged for backwards compat).
   Put grouping in a new module e.g. `bible/services/fileset_groups.py`
   with unit tests.
2. Error contract — in `bible/serializers.py` DBT path and the SWORD
   audio path:
   - DBT `v4_bible_filesets_show_chapter` raising `ApiException` 404
     (or returning empty `data`) → surface
     `{"error_code": "book_not_in_fileset", "error": "…"}` with
     HTTP 404 from `BiblePassageView`. Do this for BOTH text and
     audio requests.
   - Audio requests that resolve to `audio_url: None` or lack `path`
     → same `book_not_in_fileset` 404 (the existing
     `format=='audio' and audio_url is None → 404` branch already
     covers the SWORD case; add `error_code` to that body).
   - Keep all other failures unchanged (still 200+message or
     `provider_error` per the existing contract).
3. Pass through `codec`/`bitrate` in `_process_translations` if DBT
   provides them (optional; the `-opus16` id suffix is authoritative
   today).
4. Tests: `bible/tests/` — grouping matrix (C/NT/OT/NTP/OTP/NTPOTP ×
   digit 1/2 × ±opus16), EN1WEB-style mixed prefixes, coverage-merge
   precedence, and the 404 `book_not_in_fileset` contract (mock the
   DBT client).

## Frontend changes (`reactive-bible/`)

DONE — merged on `main`. All items below are implemented
(`filesetGroups.ts`, `BookNotInFilesetError`, playback + text
resolution, selector rewrite, ENGKJV injection, tests).

### New util: `src/utils/filesetGroups.ts`

- `groupFilesets(translation): { text, audio[] }` implementing the
  same algorithm (consume backend `audio_options`/`text_options`
  when present — feature-detect; fall back to grouping `filesets`).
- `resolveTextFileset(storedId, bookId, translations): string|null`
- `resolveAudioFileset(storedId, bookId, translations):
    { filesetId, codec, alternates: string[] } | null`
  - `storedId` may be a product id OR any member id — resolve via the
    product containing it (keeps persisted ids working; no store
    migration needed).
  - codec preference: `opus16` first; `alternates` = ordered list:
    [mp3 sibling, other-testament member, same-kind other option,
    any audio option in translation].
- Fix/replace `filesetCoversTestament` with a `sizeToCoverage`
  parser (handles `C`, `NT`, `OT`, `NTP`, `OTP`, `NTPOTP`,
  `NT1`-style codes).
- KJV injection: in `getAvailableTranslations` post-processing, add
  `{id:'ENGKJV', type:'text_plain', size:'C'}` to the ENGKJV
  translation (so the local bundle becomes selectable) and ensure
  its audio options include an `ENGKJV` audio product (wordpocket
  path already keyed on `filesetId === 'ENGKJV'`).

### `src/api.tsx`

- New `BookNotInFilesetError`; `getBibleAudioUrl`/`getVersesFromApi`
  throw it when response has `error_code === 'book_not_in_fileset'`
  OR (backwards compat) 200 with empty `verses`/`message` for an
  audio request / missing `audio_url`.

### Playback (`Audio.tsx`, `hooks/useAudioPlaylist.ts`)

- Replace `resolveFilesetFor`/`findTestamentFallback` usage with
  `resolveAudioFileset`. On `BookNotInFilesetError` or Howler
  load/play error: walk `alternates`; codec fallback opus16→mp3 is
  the FIRST retry. Only notify after alternates exhausted:
  "No audio for {book} in {translation}". Update
  `resolveTimestampsFilesetId` to use the resolved base fileset
  (strip `-opus16`); keep the `ENGESV_API`→`ENGESVO1DA/N1DA` special
  case or fold it into the product's member ids.
- Prefetch (`prefetchAudioUrl`, `prefetchAdjacentChapters`) should
  resolve through the same function.

### Text (`PassageView.tsx`)

- Resolve `activeTextFilesetId` through `resolveTextFileset` per
  book before fetching (handles `_ET` splits transparently). Keep
  the mismatch-hint UI as the last-resort error display.

### `TranslationSelector.tsx` rewrite

Per "Target UX" above. Behaviour details:

- Audio-only translation: if it has no text option AND the current
  `activeTextFilesetId` resolves to a different language, pick a
  same-language text translation (prefer `coverage 'Full Bible'`,
  else first) and show an info line ("Text: {name} — auto-selected").
  Same rule symmetrically for English.
- Store `activeAudioFilesetId` = product id going forward;
  `activeTextFilesetId` = product id or a member id (resolver
  accepts both).
- Advanced section: collapsed `Accordion`/`Collapse` with raw
  fileset radios (current labels are fine for debugging); choosing a
  raw id stores that concrete id — resolution still works via
  product lookup.
- Save applies immediately as today (`handleSave`).

### Tests

- `filesetGroups` unit tests: all size codes, drama/plain, opus16
  pairing, EN1WEB prefixes, `LVSGLU8C1DA`/`ENGESV_API` singletons,
  `AUSWBTP1DA` (NTPOTP) both-testament-partial.
- Resolver tests: testament switching incl. LAVLVR P↔N pair, codec
  fallback order, unknown/persisted legacy ids.
- Selector render tests; playlist auto-switch test update
  (`useAudioPlaylist.test.ts`); `bibleUtils.test.ts` updates if
  helpers are superseded (keep exports used elsewhere or remove
  cleanly).
- Mock data (`src/__tests__/mocks/data.ts`, `helpers/factories.ts`):
  update to the real API shape (add `audio_options` or rely on
  grouping) — existing `ENGKJV` mock text fileset matches the new
  injected entry.

## Rollout order

1. Backend: grouping fields + `book_not_in_fileset` contract
   (additive, safe to deploy alone). — DONE on
   `feat/fileset-groups`; remaining: rebase + merge to `main`
   (see "Current state" for the `provider_errors.py` conflict
   watch-out).
2. Frontend: `filesetGroups` + resolvers + error class
   (no UI change yet). — DONE, merged.
3. Wire playback + PassageView resolution + KJV injection.
   — DONE, merged.
4. Selector UI rewrite. — DONE, merged.
5. Tests green (`npm run lint`, `npm test -- --run`; backend
   `python -m pytest`), update `DEVELOPER_GUIDE.md` in both repos
   and workspace `AGENTS.md` fileset notes. — PARTIAL: frontend
   tests + guide updates merged; backend tests and its
   `DEVELOPER_GUIDE.md` update exist on the branch — verify the
   full suite green after that branch lands.

## Explicit non-goals / watch-outs

- Do NOT persist normalized data server-side — stay live.
- Do NOT expose codec selection UI; do NOT remove the `filesets`
  array from the API response.
- Drama audio filesets may lack timestamps — keep timestamp fetch
  failure silent.
- `size`-based coverage is advisory for partial sets; individual
  books may still 404 — the `book_not_in_fileset` fallback chain
  handles that.
- Mind `activeAudioFilesetId === 'ENGKJV'` branches (wordpocket,
  no timestamps) when resolving products.
