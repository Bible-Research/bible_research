# Plan: Order-ID badge + "move after" dropdown for tag notes

## User request

In `TagNotesRoute`, when the sort order is **Custom: Ascending** or
**Custom: Descending**, each note card should show its order ID next
to the existing action buttons (view/edit/delete/etc.). Clicking it
opens a dropdown listing **all** order IDs in the tag together with
each note's passage (`Book chapter:verse[-verse]`). Clicking an entry
moves the note to right after the selected note. Must work for tags
with up to **2000 notes**.

## Current state (2026-10-13)

**Implemented.** Both halves exist on `feat/note-order-move`
branches — committed and pushed to origin, not yet merged:

- **Backend** (`bible_research`, commit `b70bec1`):
  `GET /api/v1/notes/order-index/` (`order_index` action,
  ~line 773) and `POST /api/v1/notes/move/` (`move` action,
  ~line 841) in `annotations/views.py`, tests in
  `annotations/tests.py` (+428), `DEVELOPER_GUIDE.md` endpoint
  docs.
- **Frontend** (`reactive-bible`, commit `d71d32d`):
  `getNoteOrderIndex`/`moveNote` in `src/api.tsx`,
  `NoteOrderIndexEntry`/`NoteOrderMenuData` in `src/types.ts`,
  move action in `src/store.tsx`, new
  `src/components/NoteOrderMenu.tsx`, badge/menu prop threading
  through `NoteCard`/`SortableNoteCard`/`TagSection`/
  `TagNotesRoute`, tests (`NoteOrderMenu.test.tsx`,
  `TagNotesRoute.orderMenu.test.tsx`), `DEVELOPER_GUIDE.md`.

**Remaining:** open the two PRs (one per repo), run full
verification (backend pytest, frontend lint/build/tests),
manual UI check.

## Current state (findings)

### Frontend (`reactive-bible`)

- `src/routes/TagNotesRoute.tsx` — owns sort order
  (`custom_asc`/`custom_desc`/...), pagination, and renders
  `TagSection`. Passes `isDraggable` only for the two custom orders
  + `isAuthenticated`, plus `onReorder={reorderNotes}`.
- `src/components/TagSection.tsx` — dnd-kit drag-and-drop; on drag
  end sends the whole page's note ids to `onReorder` (reversed when
  `custom_desc`). Non-draggable path renders plain `NoteCard`s.
- `src/components/NoteCard.tsx` — renders the heading
  (`Book C:V[-V]` from `note.verses`) and the action-icon group in
  two variants (folded / unfolded). This is where the order badge
  goes. Also used by `NoteDetailRoute` — badge must be opt-in via
  prop.
- `src/store.tsx` — `reorderNotes(tagId, noteIds, currentPage,
  pageSize)` computes sequential integer positions
  (`startPosition + index + 1`) and calls `api.reorderNotes`.
- `src/api.tsx` — `reorderNotes` → `POST /api/v1/notes/reorder/`
  (`authenticatedFetch`, `ConflictError` on 409).
- `src/types.ts` — `Note.tag_position: number | null`.

### Backend (`bible_research`)

- `Note.tag_position` = `FloatField`, default = Unix timestamp at
  creation; `UniqueConstraint(tag, tag_position)` named
  `unique_tag_position_per_tag` (`annotations/models.py:122-157`).
- `POST /api/v1/notes/reorder/` (`annotations/views.py:476`) accepts
  `{tag_id, updates: [{note_id, position}]}` — **max 100 updates**,
  two-phase write to satisfy the unique constraint, 409 on conflict.
  Positions are arbitrary floats — fractional midpoints already work.
- `GET /api/v1/notes/?tag_id=&ordering=` — paginated
  (`NotesPagination`: default 25, **max page_size 100**). `custom` =
  `tag_position` ASC (nulls last), `-custom` = DESC (nulls first).
- Note payloads are heavy (full verse objects incl. text), so
  fetching all pages just to build the dropdown is wasteful:
  2000 notes = 20 requests of full note bodies.

## Does the backend need changes? — Yes

Two gaps, both driven by the 2000-note requirement:

1. **Dropdown data.** There is no lightweight way to list every
   note's position + passage for a tag. Paginating `GET /notes/` at
   page_size 100 → 20 sequential requests with heavy payloads.
   → New compact read endpoint (see below).

2. **Single-note move.** Options considered:
   - *Reuse `reorder/` with a computed midpoint*
     (`{note_id, position: (target + next) / 2}`): works today —
     floats are accepted and a 1-item update is valid — but repeated
     bisections exhaust double precision (~50 splits between the same
     neighbours) → silent collisions/409s, and the client must
     replicate server ordering logic.
   - *Raise the 100-update cap to 2000* and resend the full list per
     move: wasteful, non-atomic UX, conflict-prone.
   - **Chosen:** dedicated `move` action that computes the midpoint
     server-side inside a transaction and rebalances when the gap is
     exhausted.

## Backend changes (`bible_research` repo)

**Status: implemented (committed on `feat/note-order-move`).**

### 1. `GET /api/v1/notes/order-index/?tag_id=<id>`

Returns every note in the tag in canonical custom order
(`tag_position` ASC, nulls last, `-created_at` tiebreak — same as
`ordering=custom`), unpaginated, compact:

```json
[
  {"id": "NOT_...", "tag_position": 1788499374.59,
   "book": "John", "chapter": 3,
   "verse_start": 16, "verse_end": 18}
]
```

- Implement as a `@action(detail=False, methods=['get'],
  url_path='order-index')` on `NoteViewSet` (same class that already
  handles `reorder`).
- Scope with the existing `get_accessible_notes_qs(user)` +
  `tag_id` filter (public notes for anonymous viewers).
- `verse_start`/`verse_end` = min/max verse of the note's first
  book+chapter block (mirrors the `PlaylistItem` label logic in
  `TagNotesRoute`); `null`s when the note has no verses.
- One query + one prefetch for verse refs; ~2000 compact rows
  (~150 KB) is fine.

### 2. `POST /api/v1/notes/move/`

```json
{"tag_id": "TAG_...", "note_id": "NOT_...",
 "target_note_id": "NOT_...", "placement": "after"}
```

- `placement`: `"after" | "before"`, interpreted in **canonical
  ascending order** (`tag_position` ASC). Frontend translates
  display-order "after" into before/after when in `custom_desc`.
- Inside `transaction.atomic()` + `select_for_update()` on the tag's
  ordered notes:
  - Compute neighbours of `target_note_id`; new position = midpoint
    (or `last + 1` / `first - 1` at the ends).
  - If the gap is below an epsilon (float exhaustion), **rebalance**:
    renumber all notes in the tag to `1..N` in current order, then
    assign the moved note its slot — one transaction.
- Return `200 {"note_id", "tag_position", "rebalanced": bool}`.
- Same ownership/validation style as `reorder` (`user=request.user`,
  notes must belong to `tag_id`), 400 on invalid input, 404/400 on
  unknown ids.
- Optionally reuse internally for `placement` at boundaries:
  `target_note_id` of the first note + `"before"` = move to top —
  the dropdown gets a "Move to top" item for free.

No migration needed (`tag_position` is already a float).

### 3. Backend tests (`annotations/tests.py`)

Mirror `TestPartialReordering`: move middle/end/start, `before`,
rebalance path (tiny gap), wrong-user/wrong-tag rejection,
`order-index` ordering + payload shape, >100-notes tag.

## Frontend changes (`reactive-bible` repo)

**Status: implemented (committed on `feat/note-order-move`).**

### 1. `src/api.tsx`

- `getNoteOrderIndex(tagId): Promise<NoteOrderIndexEntry[]>`
  (`authenticatedFetch`).
- `moveNote(tagId, noteId, targetNoteId, placement)`
  → `POST /api/v1/notes/move/`; parse `{tag_position, rebalanced}`;
  keep existing error/`ConflictError` conventions.

### 2. `src/types.ts`

```ts
export interface NoteOrderIndexEntry {
  id: string;
  tag_position: number | null;
  book: string | null;
  chapter: number | null;
  verse_start: number | null;
  verse_end: number | null;
}
```

### 3. `src/store.tsx`

- `moveNoteAfter(tagId, noteId, targetNoteId, placement)`:
  call api → set moved note's `tag_position` in `state.notes` →
  success notification. On `rebalanced: true` or `ConflictError`:
  `fetchNotes(tagId, {ordering, page})` refresh (existing pattern).

### 4. `TagNotesRoute.tsx`

- New state `orderIndex: NoteOrderIndexEntry[] | null`; fetch lazily
  on first badge open (or when `sortOrder` is custom_* and
  `isAuthenticated`), refetch after every move. Keep it out of the
  persisted store — it's derived data.
- **Order ID definition:** 1-based rank in canonical ascending order
  (`index + 1` in `orderIndex`). Same ID in both sort directions —
  `custom_desc` only reverses display, not the IDs.
- Compute `orderIdByNoteId: Map<string, number>`; pass
  `orderIndex`/`orderIdByNoteId`/`onMoveAfter` into `TagSection`.
- `onMoveAfter(note, targetEntry)`: map display "after" to canonical
  placement — `custom_asc` → `{target: entry.id, 'after'}`;
  `custom_desc` → `{target: entry.id, 'before'}` (visual "after" in
  desc = canonical before). Then call `moveNoteAfter`.

### 5. New component `src/components/NoteOrderMenu.tsx`

- Mantine `Menu` whose target is a subtle `Button`/`Badge` showing
  `#<orderId>` (e.g. `#12`); `aria-label="note-order-<id>"`.
- Dropdown: `ScrollArea.Autosize` (~maxHeight 320) + a `TextInput`
  filter at top (passage text / id substring) — needed at 2000 rows.
- Items: `#k — John 3:16-18`; "Move to top" item at the head;
  current note's own entry excluded/disabled.
- On select → `onMoveAfter`.

### 6. `NoteCard.tsx` / `SortableNoteCard.tsx` / `TagSection.tsx`

- Thread optional props down:
  `orderBadge?: { orderId: number }`,
  `orderMenu?: { index: NoteOrderIndexEntry[],
  onMoveAfter(noteId, targetId): void }` — simplest is two props
  `orderId?: number` + `orderMenuData` passed to a new
  `<NoteOrderMenu>` rendered inside the existing action-icon `Group`
  in **both** folded and unfolded variants, before "View in Bible".
- Only rendered when the prop is present → `NoteDetailRoute` and
  non-custom sorts are unaffected.
- Gate in `TagNotesRoute` identically to `isDraggable`:
  `sortOrder in (custom_asc, custom_desc) && isAuthenticated`.

### 7. Frontend tests

- `TagNotesRoute` test: badge visible only in custom sorts +
  authed; menu opens, lists entries with passage labels; clicking an
  entry calls the move API with the right placement
  (asc → after, desc → before); index refetched after move.
- `NoteOrderMenu` unit test: filter narrows items; "Move to top"
  item present.

## Behaviour notes / decisions

- **Order ID = canonical ascending rank**, not the raw
  `tag_position` float (which is a Unix timestamp or fraction —
  meaningless to users).
- **Display-order "after" in `custom_desc`** is translated to a
  canonical `before` server-side, so the UI always does what the
  user sees.
- Drag-and-drop keeps working unchanged; the two features coexist
  (badge is an additional reorder affordance, especially for moving
  across pages where DnD can't reach).
- Badge is hidden for anonymous/shared views and non-custom sorts,
  matching `isDraggable`.
- Notes with `tag_position = NULL` appear at the end of the index
  (nulls last) and are still valid move targets.

## PR breakdown

1. **Backend PR** (`bible_research`): `order-index` action + `move`
   action + tests + `DEVELOPER_GUIDE.md` endpoint docs.
2. **Frontend PR** (`reactive-bible`): api/types/store additions,
   `NoteOrderMenu`, prop threading, tests. Depends on backend
   endpoint names; can merge in either order since the badge only
   appears when the index loads (graceful empty state if the
   endpoint is missing → hide badge).

## Verification

- Backend: `python -m pytest annotations/tests.py -q` (new test
  classes only run).
- Frontend: `npm run lint`, `npm run build`,
  `npm test -- --run` (or scoped to touched test files).
- Manual: open `/notes/tag/<id>?sort=custom_asc` → badges show
  `#1..#n`; open dropdown, filter, click an entry → note moves;
  switch to `custom_desc` → same IDs, move lands visually after the
  picked entry; paginated tag → dropdown shows all notes, not just
  the page.
