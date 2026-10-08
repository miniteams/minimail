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
- Contacts, tasks.
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
  mime BLOB, mime_sha256, mime_size, mime_error, mime_attempts, mime_retry_at, search_text,
  deleted_at), `sync_state` (key,
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
  (exit 130); `--live` refuses `--folder`. Reconciliation: `minimail reconcile [--folder PATH]…` and,
  in `--live`, a pass over every folder every `MINIMAIL_RECONCILE_EVERY` (default 1 day, first one a
  day after start-up), after the other folders and yielding to the Inbox like them; an item whose
  `$value` and `GET` both 404 is a tombstone, never a live message.
- **Covers**: AC4, AC5, AC6
- **Data model impact**: none (`sync_state` arrives in phase 2)
- **DoD**: live test: send self a mail → searchable < 60 s; delete/move/flag one → reflected without a
  MIME refetch (log shows no `$value` call).

### Phase 4 — Search, show, attachments
- Work: FTS5 (`unicode61 remove_diacritics 2`) over subject/sender/recipients/search_text, kept in
  step by triggers, added to existing archives in place; `search` (words ANDed, user text never an
  FTS5 operator, newest first, tombstones only with `--deleted`, `--json`), `show` (decoded headers,
  folder, flags, attachment list, text body; `--raw`), `attachments --out DIR`; `reindex` rebuilds
  the index first; SIGINT re-armed at start-up (a background child of a non-interactive shell
  inherits it ignored).
- **Covers**: AC7, AC8, AC9
- **Data model impact**: `messages_fts` (external-content FTS5 on `messages`: subject, sender,
  recipients, search_text) + insert/update/delete triggers (the update trigger fires only `WHEN` an
  indexed column actually changed)
- **DoD**: AC7/AC8 fixture tests green; attachment extraction test incl. a `../` filename.

### Phase 5 — Calendar
- Work: `calendarView/delta` over [2000-01-01, now + 2 years] (`sync_state` key `calendar`, resumable,
  resync on 410/`syncStateNotFound`), full `GET me/events/{id}` for masters/singles/exceptions, kept
  verbatim and refetched only when the item's `changeKey` (else `lastModifiedDateTime`) moved;
  occurrences keep their delta item; `@removed` → tombstone; daily window slide; `agenda`, `event`,
  `search --events` (FTS); the UID of a mail's `text/calendar` part stored on the message at sync
  and by `reindex` (`iCalUId`/`uid` ↔ mail link); `joinUrl` → thread id; in `--live`, a calendar
  round after every Inbox round, isolated and yielding to the Inbox like the other folders; one-shot
  `sync` without `--folder` includes it.
- **Covers**: AC14, AC15, AC16, AC17, AC18, AC19
- **Data model impact**: `events` (id, type, series_master_id, ical_uid, start_utc, end_utc,
  is_cancelled, meeting_thread_id, subject, raw JSON, version, search_text, first_seen_at,
  updated_at, deleted_at, event_attempts, event_retry_at, fetch_error), `events_fts` (external content: subject, search_text) + triggers,
  `messages.ical_uid`, `sync_state.window_end` (the calendar row: window end + cursors; the start is
  fixed) — all added to existing archives in place, in one transaction
- **DoD**: real backfill count matches AC14; live test: create/move/delete a test event → reflected
  < 60 s; `agenda` for a week with a recurring meeting and an exception matches Outlook; a known
  Teams meeting resolves to a thread id that miniteams `read_messages` accepts.

### Phase 6 — MCP stdio
- Work: `minimail mcp` with mail tools (`list_folders`, `search_mail`, `read_mail`, `get_attachments`)
  and calendar tools (`agenda`, `search_events`, `read_event`), same shape as miniteams' MCP.
  Hand-written stdio JSON-RPC (no SDK), archive opened read-only per call; results carry
  `mail_synced_at` / `calendar_synced_at` (max `sync_state.updated_at`); `show`/`event` logic shared
  with the CLI (`search.message_view`, `calendar.view`); `MINIMAIL_EXPORT_DIR` for extraction.
- **Covers**: AC12, AC20
- **Data model impact**: none
- **DoD**: MCP tool tests green; registered in Claude, a mail search and a "this week" agenda return
  real results.

## Data model impact (summary)

New SQLite archive `${MINIMAIL_DATA_DIR}/mail.db`: `folders`, `messages` (+ `ical_uid`, `mime_attempts`,
`mime_retry_at`), `messages_fts` (+ its triggers; update only `WHEN` an indexed column changed),
`events` (+ `event_attempts`, `event_retry_at`, `fetch_error`), `events_fts` (+ its triggers), `sync_state` (+ `window_end`).

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
- **Permanent 4xx are recorded as download failures at once**; a `$value` failing on its own
  (5xx left after retries, read/write timeout, connection dropped mid-body — on the `$value` or
  on the lookup after its 404) leaves the MIME missing and the cursor advances; it is retried with back-off (`mime_retry_at`:
  1 min, doubling, capped at 4 h; at most one attempt per round) and recorded only once its attempts
  (`mime_attempts`) span a day, so an outage never costs a MIME and neither a page nor the catch-up
  can wedge; `--retry-failed` resets both. Mailbox-wide failures (401/429 after retries, sign-in,
  connection errors) stop the round. Every round ends by fetching any MIME still missing (and due)
  in the folder.
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
- **Window start fixed at 2000-01-01**, before any event of this mailbox (a 2000-01-01 window was
  measured to work): it covers "from the earliest event" with no probe round and no second backfill
  over a narrowed window. End = now + 730 days, at day granularity, so it moves once a day.
- **Window slide = one more round over the wider window** (AC17): when the end has moved, the round
  first finishes the old window's delta (no change lost), then lists [2000-01-01, new end] afresh
  and keeps that round's cursor. The listing re-reads every item (pages only), but a master, single or
  exception whose `changeKey` is unchanged costs no `GET`: only events entering the future edge (or
  changed) are fetched. A round per extra slice instead would leave one delta cursor per day to poll
  forever. A pause between the two halves redoes the old delta (idempotent). Relies on a fresh
  round's token reporting what changes while it is listed (live check).
- **Refetch rule**: the delta item's `changeKey` (else `lastModifiedDateTime`; measured on every
  master, single and exception, never on occurrences) is stored beside the full JSON it was fetched
  for. A full `GET` failing on its own (any status but 401/429 left after retries, a read/write
  timeout) is logged (`event_fetch_failed`) and does not stop the round: a new event keeps its delta
  item, an archived one keeps its full JSON; either way with no version, so the next listing fetches
  it again (a deletion arrives as `@removed`); delta re-lists only changed events, so each round
  ends by re-fetching up to a page of events whose GET failed transiently once their back-off is due
  (`event_retry_at`, the mail schedule: 1 min doubling to 4 h, given up after a day, yielding to a
  pause). A permanent status records `fetch_error` and leaves the event to a delta re-listing (or
  the daily window slide); a successful GET clears both. Mailbox-wide failures (401/429 after retries, sign-in, connection errors) stop
  the round before its cursor moves.
- **Unchanged re-listing writes nothing**: a row is rewritten only if its JSON or version changed or it
  was a tombstone, so occurrences re-listed every round cause no index churn.
- **Occurrences are shown through their master**: their delta item is slim, so `agenda`/`event` read
  an occurrence's JSON over its master's (subject, organizer, location, join URL); `search --events`
  leaves occurrences out (each would repeat its master's text); `agenda` leaves masters out (each
  would repeat its first occurrence) and drops occurrences of a cancelled or deleted master.
- **Event ↔ mail = the `text/calendar` part's UID** (unfolded `UID` line), matched against the
  event's `iCalUId` and its series-wide `uid` (Graph's `iCalUId` differs per occurrence; an
  invitation to a series carries the series UID). Equality of the ICS UID and Graph's ids: live check.
- **Times are UTC as Graph sends them** (no `Prefer: outlook.timezone`): stored `YYYY-MM-DDTHH:MM:SSZ`,
  shown in the machine's local time; any other `timeZone` fails the calendar round loudly rather than
  storing a wrong time. All-day events are floating (Graph: midnight to midnight UTC, measured): they
  overlap a period by their dates and are shown as dates, never shifted to local time.
- **Teams flag** = a thread id in the join URL or `onlineMeetingProvider = teamsForBusiness` (the short
  `teams.microsoft.com/meet/…` links carry no thread id).
- **Calendar in `--live`**: a round right after every Inbox round, with the Inbox's own pause test
  between pages (a page = up to 100 full `GET`s on the download pool, the same unit as a mail page),
  so the AC4 bound holds; a calendar failing on its own is logged and retried at the next Inbox
  round, mailbox-wide errors fail the cycle. While its first backfill runs, it uses each interval
  and the other folders wait for it to finish.
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
- **A failing folder does not stop the others** (one-shot `sync` too, which then exits 1): a folder
  round (Inbox included) failing on its own
  — HTTP error status (folder gone, forbidden, 5xx including 503/504 left after retries), a read
  or write timeout (one huge or stuck item), non-JSON
  or malformed delta page, a link the client refuses — is logged; an other folder is retried once
  at the back of the pass, then skipped until the next pass. Mailbox-wide statuses (401 after
  re-mint, 429 after retries), sign-in errors, connection errors and SQLite operational errors abort the
  whole cycle, which is retried one Inbox interval later. A failed tree walk aborts the cycle only
  before the first successful walk; afterwards it is logged, the previous tree keeps serving and
  the walk is retried one tree interval later.
- **Reconcile beside delta**: Graph's message delta never reports a message leaving Deleted Items by a
  delete (soft delete into Recoverable Items): the cursor moves past it with no `@removed`. So a
  reconcile lists each folder's ids (`messages?$select=id`, 1000 per page with a per-request
  `Prefer: odata.maxpagesize=1000`: listing is latency-bound, ~490 sequential pages for 49k ids at 100) and sends every live local id the server
  no longer lists through the `@removed` logic (404 or hidden parent → tombstone, visible folder →
  move); a lookup also clears a false hit (a message stored after the listing). A local tombstone the
  server still lists in that folder (lookup confirms the parent) is revived: a false tombstone (a
  transient 404, a stale tree) heals and the catch-up fetches its missing MIME. Server ids stored
  nowhere are only counted: additions stay the delta's. In `--live` a unit is one listing page or up to 100
  lookups, so the AC4 bound holds; a paused folder resumes its listing, a folder failing on its own
  is skipped for that pass. Deleted Items' delta also lists non-mail items typed as messages that
  `/messages` does not list (`$value` and `GET` 404): stored as tombstones without `mime_error`, so
  AC1 never counts them and a fresh round that lists them again re-checks and re-tombstones them.
- **Delta links are checked before they are stored**: a nextLink/deltaLink off the Graph host is
  refused before the page is applied, so it never becomes the folder's saved cursor.
- **Search index = external-content FTS5 + triggers**: no second copy of the text. Created and filled
  (`'rebuild'`) in place by the first writer command (`sync`, `folders`, `verify`, `reconcile`, `reindex`), under the
  write lock, re-checked once held, like `message_count`; `search`/`show`/`attachments` open read-only
  and refuse an archive not yet migrated (exit 2). Its rows
  key on `messages.rowid`, which a VACUUM may renumber (no INTEGER PRIMARY KEY): `reindex` rebuilds
  the index before re-extracting, since the update trigger can only edit an index that matches.
- **Indexed subject = Graph's `subject`** (Exchange decodes RFC 2047); the MIME's own decoding is
  `mime.extract`'s, tested separately.
- **Short id = first 12 hex of sha256(id)**: Graph ids share a long per-mailbox prefix, so no prefix
  of the id itself is short. `show`/`attachments` take the full id or a unique, case-sensitive
  prefix of it, or a prefix of the short id; resolving hashes every id (milliseconds at this size).
- **Attachments** = every leaf part with a filename or an `attachment` disposition, plus any non-text
  leaf (inline images). A `message/rfc822` part is one too, written as `<name>.eml` (`part-N.eml`
  unnamed) byte-identical to what the outer MIME carries (the parser is told to keep it a leaf, since
  re-serialising a parsed message refolds headers and line ends), then walked into, 3 levels deep
  at most (each level copies its bytes). Content = the part after transfer decoding
  (`get_payload(decode=True)`), byte for byte.
  Files open with `O_CREAT|O_EXCL|O_NOFOLLOW` relative to the directory's fd: never overwrite, never
  follow a planted symlink; a taken name gets `-N`.
- **Throttling is per mailbox**: a 429/503/504 pushes back the next request slot of every thread, and
  `Retry-After` is capped at 60 s. On SIGTERM/SIGINT the client is closed before the download pool
  is drained: closing wakes every thread asleep in a back-off or a pacing slot.
- **MCP extraction is confined to `MINIMAIL_EXPORT_DIR/<short id>/`** (default `<data dir>/exports`,
  inside the owner-only archive directory): the client names no path, attachment names go through
  `safe_filename`; the root and the message's directory are opened `O_NOFOLLOW` and written through
  their fds (no check-then-open race), the root must be the user's and not group/other-writable, and
  a file already holding the same bytes is reused (a repeating agent cannot fill the archive volume). The archive is opened per call,
  so a missing or not-yet-migrated archive is a tool error and the server never migrates it.
- **MCP results are bounded**: bodies paged (50 000 characters, `offset`/`next_offset`), every list
  cut at 100 items with a `<name>_total`, strings at 2 000 characters (200 inside lists; ids and
  folder paths never), `agenda` at most 92 days, so one wide message (thousands of recipients or
  attachments) cannot flood the agent. Caps count characters before JSON encoding (escaping can lengthen
  the encoded result). The server instructions flag mail and event text as
  third-party data, never instructions.
