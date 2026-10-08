# minimail

Personal, read-only local mirror of an Exchange Online mailbox and its default calendar, searchable
offline. Sibling of [miniteams](../miniteams): same sign-in, same archive habits.

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
| `uv run minimail sync [--folder PATH]… [--retry-failed]` | Mirror messages (verbatim MIME + metadata), then, without `--folder`, the default calendar (events from 2000-01-01 to now + 2 years: series masters, single events, exceptions and occurrences; masters, singles and exceptions as their full Graph JSON). First run = full backfill, then incremental; resumes after a crash. An event deleted on the server is kept, marked deleted; the window end moves forward daily without refetching unchanged events |
| `uv run minimail sync --live [--retry-failed]` | Same, forever: Inbox every 30 s, the calendar right after each Inbox round, other folders every 5 min, folder tree every 15 min. The calendar and other folders yield to the Inbox between pages; a folder that keeps failing (Inbox and calendar included) is logged and does not hold the others back; a failed tree refresh keeps the previous tree; a failed cycle (sign-in, network, throttling, archive disk), even at start-up, is logged and retried 30 s later. Ctrl-C / SIGTERM stops it (exit 130). Takes no `--folder` |
| `uv run minimail reconcile [--folder PATH]…` | Sync the folder tree, then compare each folder's server message ids to the local ones: a local message the server no longer lists is marked deleted (gone, or soft-deleted out of sight) or moved; a local message marked deleted that the server still lists there is restored (its MIME, if missing, is fetched by the next `sync`); server messages stored nowhere are only counted (`sync` fetches them). A folder that fails is logged and the others still run (exit 1). Catches deletions Graph's delta never reports (deleting from Deleted Items). Stop `sync --live` first (single writer) |
| `uv run minimail verify [--refetch N]` | Check every stored MIME against its sha256; `--refetch N` compares N random messages to the server byte for byte |
| `uv run minimail reindex` | Rebuild the mail and event search indexes, then recompute each message's search text and calendar UID from its stored MIME, no download (stop `sync` first: single writer) |
| `uv run minimail search QUERY [--folder PATH] [--limit N] [--deleted] [--json]` | Full-text search over subject, sender, recipients and decoded text, accent- and case-insensitive; words are ANDed, operators and punctuation are plain text (`--` before a query starting with `-`). Newest first, 20 hits by default, messages deleted on the server only with `--deleted`. Per hit: short id, date (UTC), folder, sender, subject, then a snippet; `--json`: one object per line |
| `uv run minimail search --events QUERY [--limit N] [--deleted] [--json]` | Same over calendar events: subject, body (visible text), location, organizer and attendee names/addresses. Latest start first; occurrences are left out (their series matches). Per hit: short id, start (local time), subject, then a snippet. Takes no `--folder` |
| `uv run minimail agenda [--from DATE] [--to DATE] [--json]` | Events overlapping the local days `--from` to `--to`, both included (default: today to today + 7), in local time: series expanded into occurrences and exceptions, cancelled and deleted events left out. Per event: short id, start–end, subject, organizer, `@ location`, `[Teams]` for a Teams meeting; an all-day event shows its first and last day with `(all day)` and is listed on exactly those local days; `--json`: one object per line |
| `uv run minimail event ID [--json]` | An event's subject, times, type, organizer, location, attendees, cancelled/deleted state, Teams meeting chat id (`19:meeting_…@thread.v2`, decoded from the join URL), iCalUId, the mails carrying it (invitations, responses: short id, date, subject), then its body. An occurrence shows its series' details. `ID`: full id, or a unique prefix of it or of the short id |
| `uv run minimail show ID [--raw]` | Decoded headers, folder, read/flag/categories, attachment list and text body, control characters shown as `�`; `--raw` writes the verbatim MIME to stdout. `ID`: full id, or a unique prefix of it or of the short id |
| `uv run minimail attachments ID --out DIR` | Write every attachment into `DIR`, decoded, files `0600`; a forwarded mail is written verbatim as `.eml`, followed by its own attachments (opened 3 forwards deep; deeper ones are written, not opened). On a write error the partial file is removed and the files already written are listed. Names are sanitised (no path, no control character, no leading dot); unnamed parts become `part-N.<ext>`; a name already taken gets `-1`, `-2`… — nothing is overwritten, nothing lands outside `DIR` |

## Configuration (`MINIMAIL_*`, env or `.env`)

| Variable | Default | |
|---|---|---|
| `MINIMAIL_TENANT_ID` | — (required) | Entra tenant id |
| `MINIMAIL_DATA_DIR` | `data` | Archive directory (`mail.db`); often a symlink to larger storage |
| `MINIMAIL_MSAL_CACHE` | `~/.config/miniteams/msal_cache.json` | Token cache shared with miniteams |
| `MINIMAIL_LOG_LEVEL` | `INFO` | Logs go to stderr (JSON when not a TTY) |
| `MINIMAIL_INBOX_EVERY` | `30` | `sync --live`: seconds between Inbox (then calendar) rounds, and the retry delay after a failed cycle |
| `MINIMAIL_FOLDERS_EVERY` | `300` | `sync --live`: seconds between rounds of the other folders |
| `MINIMAIL_TREE_EVERY` | `900` | `sync --live`: seconds between folder tree refreshes |
| `MINIMAIL_RECONCILE_EVERY` | `86400` | `sync --live`: seconds between reconcile passes over every folder (first one after this delay); low priority, yields to the Inbox |
| `MINIMAIL_MAX_RPS` | `8` | Graph requests started per second, all threads together; a throttled (429/503/504) answer holds every thread back for its `Retry-After` (capped at 60 s). The quota (~10k requests / 10 min per app and mailbox) is shared with the Teams client, whose app id minimail borrows |

Exit codes: `2` no usable token, missing/invalid setting, unknown `--folder`, `--live` with `--folder`,
unknown/ambiguous message or event `ID`, `search --events` with `--folder`, an invalid `agenda` date, no archive at `MINIMAIL_DATA_DIR` or one that needs `minimail reindex` (`search`, `show`, `attachments`, `agenda` and `event` only read the archive, never create or upgrade it), `3` sign-in service unavailable (retry), `1` Graph error, archive
error (e.g. locked by another writer; traceback in the log), `verify` mismatch, a message whose MIME
is not stored or is too deeply nested to parse (`show --raw` still works), or attachments that cannot
be written, `130` interrupted (Ctrl-C / SIGTERM).

The first `sync`, `folders`, `verify`, `reconcile` or `reindex` run after upgrading to the search index (or to the
calendar tables) builds it from the stored messages, holding the archive's write lock meanwhile: stop `sync --live` first (single
writer; otherwise either side may fail with `database is locked`; rerun it). Until then `search`,
`show`, `attachments`, `agenda` and `event` refuse the old archive (exit 2). `reindex` also records, from
the stored MIME, the calendar UID of meeting mails already archived (what links them to their event).

The archive is personal data: its directory is `0700`, its files `0600`. Never commit `data/`.
