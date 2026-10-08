# 001 — Local mailbox + calendar mirror (`minimail sync`, `search`, `agenda`, `mcp`)

**Status**: in progress
**Requested by**: babs
**Date**: 2026-10-08

## Problem

Mail and calendar live only in Exchange Online: no local, verbatim, searchable copy. Outlook search
is slow and misses encoded content, and nothing outside Outlook (scripts, Claude) can query the
mailbox or the agenda. miniteams solved this for Teams chats; mail and calendar have no equivalent.

## Solution

`minimail` keeps a local, read-only archive of the whole mailbox (every folder and subfolder, every
message byte-for-byte, attachments included) and keeps it current within a minute. It searches the
archive offline, accent- and encoding-insensitive. It does the same for the user's own calendar:
every event since the mailbox began and two years ahead, recurrences expanded, so "my meetings this
week" and "the meeting about X" answer offline, linked to their invitation mails and to the Teams
meeting chat in miniteams. All of it is exposed through MCP for Claude. Nothing deleted on the server
is lost locally.

## Scope

- Auth: reuse the miniteams MSAL cache (`~/.config/miniteams/msal_cache.json`, FOCI), silent Graph
  token only; never prompts.
- Folder tree sync (all folders, nested), moves and renames tracked.
- Per message: verbatim MIME (`GET /me/messages/{id}/$value`), Graph metadata (folder, read, flag,
  categories, conversationId, receivedDateTime), and a decoded `search_text`.
- Initial backfill, resumable; then Graph delta polling: Inbox every 30 s, other folders every 5 min,
  folder tree every 15 min (`sync --live`).
- Server deletions → local tombstone; moves → folder updated, history kept.
- Search (FTS5 on decoded text), show a message, extract attachments on demand.
- Calendar (default calendar only): all events from the mailbox's first event to now + 2 years,
  sliding forward; series masters, single events, exceptions and expanded occurrences; raw Graph
  JSON kept verbatim; same delta cadence as Inbox (30 s).
- Calendar queries: agenda for a period, event search (FTS), event ↔ invitation/response mails
  (`iCalUId`), event ↔ Teams meeting chat (thread id parsed from `onlineMeeting.joinUrl`).
- MCP over stdio: mail tools (folders, search, read, attachments) and calendar tools (agenda,
  search, read event with its linked mails and chat thread id).

## Out of scope

- Sending, replying, any write to the mailbox or calendar (accept/decline/create: later spec).
- Other calendars: colleagues' shared calendars, holidays, birthdays, calendar groups.
- Event attachments (files attached to the event itself; invitation mail attachments are covered).
- Fetching the Teams chat/transcript itself: minimail exposes the thread id, miniteams reads it.
- Shared/delegated mailboxes, Online Archive (In-Place Archive is not exposed by Graph).
- Calendar, contacts, tasks.
- Push notifications (Graph webhooks need a public HTTPS endpoint; EWS streaming is being retired).
- A UI/widget.

## Acceptance criteria

- [ ] **AC1** After `minimail sync` completes, every server folder and subfolder exists locally with its
  full path, and per folder the local non-tombstoned count equals the folder's message count
  (`GET …/mailFolders/{id}/messages/$count`).
- [ ] **AC2** Each stored MIME decompresses to bytes identical to `GET …/$value`; `minimail verify`
  re-checks the recorded sha256 of every message and reports mismatches.
- [ ] **AC3** A sync killed mid-backfill (`kill -INT`, `kill -9`) resumes without re-downloading
  messages already stored and without duplicates.
- [ ] **AC4** With `sync --live`, a mail arriving in Inbox is searchable locally within 60 s.
- [ ] **AC5** A mail deleted on the server stays locally, marked deleted with the detection time; a
  mail moved on the server shows its new folder locally; neither re-downloads the MIME.
- [ ] **AC6** A read/flag/category change on the server updates local metadata without re-downloading
  the MIME.
- [ ] **AC7** `minimail search` finds a word present in a body encoded as quoted-printable or base64, in
  ISO-8859-1 / windows-1252 / UTF-8, and in an RFC 2047 encoded subject; `ete` matches `été`.
- [ ] **AC8** For an HTML-only mail, search matches visible text and not tag/attribute/style content.
- [ ] **AC9** `minimail attachments <id> --out DIR` writes each attachment byte-identical to its MIME
  part, with the RFC 2231/2047 filename decoded and path-unsafe names sanitised (no write outside DIR).
- [ ] **AC10** With no usable token, every command exits non-zero with a message pointing at
  `miniteams login`; `sync --live` logs it and retries next cycle instead of crashing or prompting.
- [ ] **AC11** On 429/503 the client waits `Retry-After` and the sync continues without losing data.
- [ ] **AC12** MCP tools `list_folders`, `search_mail`, `read_mail`, `get_attachments` answer from the
  local archive; `read_mail` returns decoded headers + text, never the raw MIME blob.
- [ ] **AC20** MCP tools `agenda`, `search_events`, `read_event` answer from the local archive;
  `read_event` includes linked mail ids and the Teams meeting thread id when present.
- [ ] **AC13** The archive directory is created `0700` and its files `0600`.
- [ ] **AC14** After `minimail sync`, the default calendar's events from its earliest event to
  now + 2 years are stored: one row per series master, single event, exception and occurrence, the
  count matching a fresh `calendarView/delta` over the same window; masters, singles and exceptions
  hold the full `GET /me/events/{id}` JSON verbatim.
- [ ] **AC15** `minimail agenda --from D1 --to D2` lists every non-cancelled event overlapping the
  period, occurrences expanded, in local time, with organizer, location and Teams join flag.
- [ ] **AC16** With `sync --live`, a created / moved / cancelled / deleted event is reflected locally
  within 60 s; a deleted event becomes a tombstone, a cancelled one stays flagged `isCancelled`.
- [ ] **AC17** When the sliding window advances (checked daily), events entering the future edge are
  fetched without re-running the full backfill.
- [ ] **AC18** `minimail search --events` matches subject, body, location and attendee names/addresses,
  accent-insensitive.
- [ ] **AC19** `minimail event <id>` shows the invitation/response mails sharing its `iCalUId` and,
  for a Teams meeting, the `19:meeting_…@thread.v2` id decoded from `joinUrl`.

## Phases

### Phase 1 — Scaffold, auth, folder tree
- Work: uv project (python.md tooling, pre-commit), `MINIMAIL_` settings, shared-cache token source,
  Graph client with Retry-After handling, `folders` table + tree sync, `minimail folders`.
- **Covers**: AC10, AC11, AC13
- **Data model impact**: `folders` (id, parent_id, display_name, path, total_count, deleted_at,
  message_count — added in place to existing archives)
- **DoD**: `minimail folders` prints every server folder with counts matching Graph; tests for 429 retry and
  missing-token exit green; `pre-commit run --all-files` clean.

### Phase 2 — Backfill: verbatim MIME + metadata + search text
- Work: per-folder delta round (the first round is the backfill), `$value` download, zstd-compressed
  blob + sha256, metadata JSON, MIME → `search_text` extraction (charsets, QP/base64, RFC 2047,
  HTML → text), resumable, `minimail verify`, `minimail reindex` (re-extract from stored MIME),
  `sync --retry-failed`.
- **Covers**: AC1, AC2, AC3
- **Data model impact**: `messages` (id, internet_message_id, folder_id, received_at, meta JSON,
  mime BLOB, mime_sha256, mime_size, mime_error, search_text, deleted_at), `sync_state` (key,
  next_link, delta_link, updated_at)
- **DoD**: full backfill of the real mailbox; AC1 counts match; `verify` reports 0 mismatches;
  extraction tests on a fixture corpus (QP, base64, latin-1, cp1252, HTML-only) green.

### Phase 3 — Delta sync and `--live`
- Work: per-folder message delta + folder tree re-walk, tombstones, moves, metadata-only updates,
  cadence loop (30 s / 5 min / 15 min, `MINIMAIL_{INBOX,FOLDERS,TREE}_EVERY`): Inbox → tree refresh →
  other folders (the very first walk precedes the Inbox, which it locates); the other folders
  yield between rounds, pages and catch-up chunks once the Inbox is due; a failing folder is skipped
  for the rest of the pass; a failed cycle (including at start-up) is logged and retried one Inbox
  interval later; client-side request pacing (`MINIMAIL_MAX_RPS`); clean stop on SIGINT/SIGTERM
  (exit 130); `--live` refuses `--folder`.
- **Covers**: AC4, AC5, AC6
- **Data model impact**: none (`sync_state` arrives in phase 2)
- **DoD**: live test: send self a mail → searchable < 60 s; delete/move/flag one → reflected without a
  MIME refetch (log shows no `$value` call).

### Phase 4 — Search, show, attachments
- Work: FTS5 (`unicode61 remove_diacritics 2`) over subject/from/to/search_text, `search`, `show`,
  `attachments`.
- **Covers**: AC7, AC8, AC9
- **Data model impact**: `messages_fts` (external-content FTS5 on `messages`)
- **DoD**: AC7/AC8 fixture tests green; attachment extraction test incl. a `../` filename.

### Phase 5 — Calendar
- Work: `calendarView/delta` over the window, full GET for masters/singles/exceptions, tombstones,
  daily window slide, `agenda`, `event`, event FTS, `iCalUId` ↔ mail link (store `iCalUId` on
  meeting messages), `joinUrl` → thread id.
- **Covers**: AC14, AC15, AC16, AC17, AC18, AC19
- **Data model impact**: `events` (id, type, series_master_id, ical_uid, start_utc, end_utc,
  is_cancelled, meeting_thread_id, raw JSON, search_text, deleted_at), `events_fts`, `messages.ical_uid`,
  `sync_state` row for the calendar (window bounds + delta link)
- **DoD**: real backfill count matches AC14; live test: create/move/delete a test event → reflected
  < 60 s; `agenda` for a week with a recurring meeting and an exception matches Outlook; a known
  Teams meeting resolves to a thread id that miniteams `read_messages` accepts.

### Phase 6 — MCP stdio
- Work: `minimail mcp` with mail tools (`list_folders`, `search_mail`, `read_mail`, `get_attachments`)
  and calendar tools (`agenda`, `search_events`, `read_event`), same shape as miniteams' MCP.
- **Covers**: AC12, AC20
- **Data model impact**: none
- **DoD**: MCP tool tests green; registered in Claude, a mail search and a "this week" agenda return
  real results.

## Data model impact (summary)

New SQLite archive `${MINIMAIL_DATA_DIR}/mail.db`: `folders`, `messages` (+ `ical_uid`),
`messages_fts`, `events`, `events_fts`, `sync_state`.

## Open questions

- None.

## Decisions

- **One SQLite file, MIME as zstd BLOB** — avoids tens of thousands of `.eml` files; zstd (stdlib
  `compression.zstd`, Python 3.14) is lossless, so verbatim is kept and proven by the sha256 of the
  uncompressed bytes. Graph exposes no folder size: measure at the first backfill, revisit if it
  outgrows a single file.
- **`search_text` is a dedicated decoded column**, not FTS over raw MIME — encodings would defeat it.
- **Attachments extracted on demand** from the MIME, not stored twice.
- **Archive semantics**: deletions are tombstones, never local deletes.
- **Deleted = 404 or a parent outside the visible tree** (AC5): a soft delete (Shift+Delete, emptying
  Deleted Items) keeps the message readable under the hidden Recoverable Items folder, so a `@removed`
  whose `GET` answers 200 with a parent that is not a live folder of `folders` is tombstoned like a
  404 (last visible folder kept); a move into a folder newer than the last tree walk heals on that
  folder's first round.
- **Backfill = first delta round** per folder: one code path for backfill and incremental sync, one
  resume cursor (`sync_state.next_link`); hence `sync_state` lands in phase 2.
- **Only permanent 4xx are recorded as download failures**; throttling, auth and 5xx stop the round,
  and every round ends by fetching any MIME still missing in the folder.
- **Data dir** `./data` (gitignored, `MINIMAIL_DATA_DIR` overrides), like miniteams: typically a
  symlink to NAS storage. SQLite over NFS → one writer at a time, all processes on the same host.
- **Personal data**: archive stays local (`0700`/`0600`), CLI + MCP over stdio only, no network
  listener → no auth layer needed.
- **Shared MSAL cache** with miniteams — zero extra login; minimail only redeems silently (Graph
  `.default` already carries `Mail.ReadWrite`, verified 2026-10-08).
- **Cadences** 30 s Inbox + calendar / 5 min other folders / 15 min tree — delta calls are
  near-empty when idle.
- **Calendar = `calendarView/delta` (v1.0)**, not beta `events/delta`: v1.0, and it returns exceptions
  and occurrences (beta returned masters + singles only). Its items are slim, hence the full GET.
- **Calendar verbatim = raw Graph JSON** — Graph has no per-event iCalendar export.
- **Window start = earliest event** (probed once with a wide window); end slides daily.
- **Bounded memory**: `--live` runs forever; any per-message cache is LRU-bounded.
- **AC1 counts messages, not items**: `totalItemCount` also counts the non-mail items a folder holds
  (Deleted Items keeps deleted events, contacts and tasks), which `messages/delta` never lists; the
  tree walk records `messages/$count` beside it (only for non-empty folders). A count that fails on
  its own (any error status but 401/429, a non-integer body) is stored NULL and the item count is the
  reference; 401/429 fail the walk so a known count is never overwritten. The column is added to
  existing archives in place, under a write lock and re-checked once the lock is held.
- **Request pacing** (`MINIMAIL_MAX_RPS`, default 8/s): minimail borrows the Teams client's app id, so
  Graph's per-app-per-mailbox quota (~10k requests / 10 min ≈ 16/s) is shared with the user's own
  Teams; 8/s leaves it half. One lock-guarded slot schedule in `graph.Graph` covers every request,
  retries and download threads included.
- **Live pause = Inbox due, not a page count**: in `--live`, the other folders' pass checks before
  each unit of work (a folder round, a delta page, a catch-up chunk of up to 100 missing MIME)
  whether the Inbox is due; if so it stops and resumes, from the saved cursor, right after the Inbox
  round. A fixed page budget cannot bound the wait: a page costs up to ~100 requests (`$value` or
  move lookups), ~12 s at 8/s. A round's first delta page always runs, so every round progresses.
  AC4 worst case ≈ Inbox interval (30 s) + the one unit already running when the Inbox fell due
  (≤ ~100 requests, ~12 s at 8/s) + the Inbox round itself; a tree refresh never sits in between,
  it runs after the Inbox round.
- **A failing folder does not stop the others**: a folder round (Inbox included) failing on its own
  — HTTP error status (folder gone, forbidden, 5xx including 503/504 left after retries), a read
  or write timeout (one huge or stuck item), non-JSON
  or malformed delta page, a link the client refuses — is logged; an other folder is retried once
  at the back of the pass, then skipped until the next pass. Mailbox-wide statuses (401 after
  re-mint, 429 after retries), sign-in errors, connection errors and SQLite operational errors abort the
  whole cycle, which is retried one Inbox interval later. A failed tree walk aborts the cycle only
  before the first successful walk; afterwards it is logged, the previous tree keeps serving and
  the walk is retried one tree interval later.
- **Delta links are checked before they are stored**: a nextLink/deltaLink off the Graph host is
  refused before the page is applied, so it never becomes the folder's saved cursor.
- **Throttling is per mailbox**: a 429/503/504 pushes back the next request slot of every thread, and
  `Retry-After` is capped at 60 s. On SIGTERM/SIGINT the client is closed before the download pool
  is drained: closing wakes every thread asleep in a back-off or a pacing slot.
