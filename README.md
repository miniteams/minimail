# minimail

Personal, read-only local mirror of an Exchange Online mailbox and its default calendar, searchable
offline. Sibling of [miniteams](https://github.com/miniteams/miniteams): same sign-in, same archive habits.

## Setup

```bash
uv sync
echo "MINIMAIL_TENANT_ID=<your tenant id>" > .env
```

Sign-in is miniteams' (`miniteams login`): minimail only reads tokens from its MSAL cache
(`~/.config/miniteams/msal_cache.json`), silently, and never prompts.

## Usage

| Command | Does |
|---|---|
| `uv run minimail folders` | Sync the folder tree; print per folder the server item count, the server message count (`?` if Graph would not count it) and the local count; `≠` marks a local count that differs from the message count (from the item count when there is none) |
| `uv run minimail sync [--folder PATH]… [--retry-failed]` | Mirror messages (verbatim MIME + metadata), then, without `--folder`, the default calendar (events from 2000-01-01 to now + 2 years: series masters, single events, exceptions and occurrences; masters, singles and exceptions as their full Graph JSON). First run = full backfill, then incremental; resumes after a crash. A message whose MIME download fails on its own (5xx, dropped or stalled body) is retried with back-off for about a day, then recorded as failed; `--retry-failed` resets every recorded or pending failure. An event deleted on the server is kept, marked deleted; the window end moves forward daily without refetching unchanged events |
| `uv run minimail sync --live [--retry-failed]` | Same, forever: Inbox every 30 s, the calendar right after each Inbox round, other folders every 5 min, folder tree every 15 min. The calendar and other folders yield to the Inbox between pages; a folder that keeps failing (Inbox and calendar included) is logged and does not hold the others back; a failed tree refresh keeps the previous tree; a failed cycle (sign-in, network, throttling, Graph outage, archive disk), even at start-up, is logged and retried 30 s later, doubling per consecutive failure up to 10 min. Ctrl-C / SIGTERM stops it (exit 130). Takes no `--folder` |
| `uv run minimail reconcile [--folder PATH]…` | Sync the folder tree, then compare each folder's server message ids to the local ones: a local message the server no longer lists is marked deleted (gone, or soft-deleted out of sight) or moved; a local message marked deleted that the server still lists there is restored (its MIME, if missing, is fetched by the next `sync`); server messages stored nowhere are only counted (`sync` fetches them). A folder that fails is logged and the others still run (exit 1). Catches deletions Graph's delta never reports (deleting from Deleted Items). Stop `sync --live` first (single writer) |
| `uv run minimail verify [--refetch N]` | Check every stored MIME against its sha256; `--refetch N` compares N random messages to the server byte for byte |
| `uv run minimail reindex` | Rebuild the mail and event search indexes, then recompute each message's search text and calendar UID from its stored MIME, no download (stop `sync` first: single writer) |
| `uv run minimail search QUERY [--folder PATH] [--limit N] [--deleted] [--json]` | Full-text search over subject, sender, recipients and decoded text, accent- and case-insensitive; words are ANDed, operators and punctuation are plain text (`--` before a query starting with `-`). Newest first, 20 hits by default, messages deleted on the server only with `--deleted`. Per hit: short id, date (UTC), folder, sender, subject, then a snippet; `--json`: one object per line |
| `uv run minimail search --events QUERY [--limit N] [--deleted] [--json]` | Same over calendar events: subject, body (visible text), location, organizer and attendee names/addresses. Latest start first; occurrences are left out (their series matches). Per hit: short id, start (local time), subject, then a snippet. Takes no `--folder` |
| `uv run minimail agenda [--from DATE] [--to DATE] [--json]` | Events overlapping the local days `--from` to `--to`, both included (default: today to today + 7), in local time: series expanded into occurrences and exceptions, cancelled and deleted events left out. Per event: short id, start–end, subject, organizer, `@ location`, `[Teams]` for a Teams meeting; an all-day event shows its first and last day with `(all day)` and is listed on exactly those local days; `--json`: one object per line |
| `uv run minimail event ID [--json]` | An event's subject, times, type, organizer, location, attendees, cancelled/deleted state, Teams meeting chat id (`19:meeting_…@thread.v2`, decoded from the join URL), iCalUId, the mails carrying it (invitations, responses: short id, date, subject), then its body. An occurrence shows its series' details. `ID`: full id, or a unique prefix of it or of the short id |
| `uv run minimail show ID [--raw]` | Decoded headers, folder, read/flag/categories, attachment list and text body, control characters shown as `�`; `--raw` writes the verbatim MIME to stdout. `ID`: full id, or a unique prefix of it or of the short id |
| `uv run minimail attachments ID --out DIR` | Write every attachment into `DIR`, decoded, files `0600`; a forwarded mail is written verbatim as `.eml`, followed by its own attachments (opened 3 forwards deep; deeper ones are written, not opened). On a write error the partial file is removed and the files already written are listed. Names are sanitised (no path, no control character, no leading dot); unnamed parts become `part-N.<ext>`; a name already taken gets `-1`, `-2`… — nothing is overwritten, nothing lands outside `DIR` |
| `uv run minimail mcp` | MCP server over stdio for Claude (see [MCP](#mcp)) |

## Configuration (`MINIMAIL_*`, env or `.env`)

| Variable | Default | |
|---|---|---|
| `MINIMAIL_TENANT_ID` | — (required) | Entra tenant id |
| `MINIMAIL_DATA_DIR` | `data` | Archive directory (`mail.db`); often a symlink to larger storage |
| `MINIMAIL_CLIENT_ID` | `1fec8e78-bce4-4aaf-ab1b-5451cc387264` | Entra app the token is minted for: the Teams first-party client miniteams signs in with (its Graph `.default` carries `Mail.ReadWrite` / `Calendars.ReadWrite`); change it only together with miniteams' |
| `MINIMAIL_MSAL_CACHE` | `~/.config/miniteams/msal_cache.json` | Token cache shared with miniteams |
| `MINIMAIL_EXPORT_DIR` | `<MINIMAIL_DATA_DIR>/exports` | The only place MCP `get_attachments` writes to (one `<short id>/` directory per message); created `0700`, refused if a symlink, not yours or group/other-writable |
| `MINIMAIL_LOG_LEVEL` | `INFO` | Logs go to stderr (JSON when not a TTY) |
| `MINIMAIL_INBOX_EVERY` | `30` | `sync --live`: seconds between Inbox (then calendar) rounds, and the first retry delay after a failed cycle |
| `MINIMAIL_FOLDERS_EVERY` | `300` | `sync --live`: seconds between rounds of the other folders |
| `MINIMAIL_TREE_EVERY` | `900` | `sync --live`: seconds between folder tree refreshes |
| `MINIMAIL_RECONCILE_EVERY` | `86400` | `sync --live`: seconds between reconcile passes over every folder (first one after this delay); low priority, yields to the Inbox |
| `MINIMAIL_MAX_RPS` | `8` | Graph requests started per second, all threads together; a throttled (429/503/504) answer holds every thread back for its `Retry-After` (capped at 60 s). The quota (~10k requests / 10 min per app and mailbox) is shared with the Teams client, whose app id minimail borrows |

Exit codes: `2` no usable token, missing/invalid setting, unknown `--folder`, `--live` with `--folder`,
unknown/ambiguous message or event `ID`, `search --events` with `--folder`, an invalid `agenda` date, no archive at `MINIMAIL_DATA_DIR` or one that needs `minimail reindex` (`search`, `show`, `attachments`, `agenda` and `event` only read the archive, never create or upgrade it), `3` sign-in service unavailable (retry), `1` Graph error, a folder (or the calendar) that failed during `sync` (the others still run), archive
error (e.g. locked by another writer; traceback in the log), `verify` mismatch, a message whose MIME
is not stored or is too deeply nested to parse (`show --raw` still works), or attachments that cannot
be written, `130` interrupted (Ctrl-C / SIGTERM).

The first `sync`, `folders`, `verify`, `reconcile` or `reindex` run after upgrading to the search index (or to the
calendar tables) builds it from the stored messages, holding the archive's write lock meanwhile: stop `sync --live` first (single
writer; otherwise either side may fail with `database is locked`; rerun it). Until then `search`,
`show`, `attachments`, `agenda` and `event` refuse the old archive (exit 2). `reindex` also records, from
the stored MIME, the calendar UID of meeting mails already archived (what links them to their event).

The archive is personal data: its directory is `0700`, its files `0600`. Never commit `data/`.

## MCP

`minimail mcp` serves the archive to an MCP client over stdin/stdout (newline-delimited JSON-RPC,
same shape as miniteams'). Read-only on the archive, no network, no Graph call; logs on stderr.

| Tool | Returns |
|---|---|
| `list_folders` | Folder paths, server and local message counts |
| `search_mail` | `query` (words ANDed, accents ignored), optional `folder` path, `limit` (≤ 100, default 20), `include_deleted`; newest first, with `short_id` and a snippet |
| `read_mail` | `id` (full or short id prefix): decoded headers, folder, read/flag/categories, attachment list, text body (50 000 characters per call, `offset` / `next_offset` for the rest). Never the raw MIME |
| `get_attachments` | `id`: the attachment list; with `extract` (optionally one `index`), writes them under `MINIMAIL_EXPORT_DIR/<short id>/` (same naming rules as `attachments`; a file already there with the same bytes is reused, not copied again) and returns the paths. The client cannot choose the directory. The root must be yours, not group/other-writable and not itself a symlink; a symlink at the message's directory is refused |
| `agenda` | `from` / `to` (`YYYY-MM-DD`, default today to +7, at most 92 days): events as `agenda --json` |
| `search_events` | `query`, `limit`, `include_deleted`: as `search --events` |
| `read_event` | `id`: as `event --json` (linked mail ids, Teams `meeting_thread_id` for miniteams) plus the body, paged like `read_mail` |

Results are bounded: lists cut at 100 items (`<name>_total` gives the full count), strings at
2 000 characters (200 inside a list, e.g. recipients or file names; ids and folder paths are never
cut), bodies paged at 50 000 characters. The caps count characters before JSON encoding: escaped
quotes or control characters make the encoded result longer. Mail and event text is third-party content (the server instructions
say so). Every result carries `source: "archive"`, `mail_synced_at` and `calendar_synced_at` (last progress
saved by `sync`, UTC): an old value means the sync stopped. A missing archive, or one that needs
`minimail reindex`, is a tool error; the server stays up and picks the archive up once it is ready.

Register with Claude Code (`MINIMAIL_TENANT_ID` comes from the repo's `.env`, so run from the repo):

```sh
claude mcp add minimail -- uv run --directory /path/to/minimail minimail mcp
```

or a gitignored `.mcp.json` at the repo root:
`{"mcpServers": {"minimail": {"command": "uv", "args": ["run", "minimail", "mcp"]}}}`.

## License

[Apache-2.0](LICENSE)
