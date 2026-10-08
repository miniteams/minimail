# minimail

Personal, read-only local mirror of an Exchange Online mailbox (and, later, calendar), searchable
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
| `uv run minimail sync [--folder PATH]… [--retry-failed]` | Mirror messages (verbatim MIME + metadata). First run = full backfill, then incremental; resumes after a crash |
| `uv run minimail sync --live [--retry-failed]` | Same, forever: Inbox every 30 s, other folders every 5 min, folder tree every 15 min. Other folders yield to the Inbox between pages; a folder that keeps failing (Inbox included) is logged and does not hold the others back; a failed tree refresh keeps the previous tree; a failed cycle (sign-in, network, throttling, archive disk), even at start-up, is logged and retried 30 s later. Ctrl-C / SIGTERM stops it (exit 130). Takes no `--folder` |
| `uv run minimail verify [--refetch N]` | Check every stored MIME against its sha256; `--refetch N` compares N random messages to the server byte for byte |
| `uv run minimail reindex` | Recompute search text from stored MIME, no download (stop `sync` first: single writer) |

## Configuration (`MINIMAIL_*`, env or `.env`)

| Variable | Default | |
|---|---|---|
| `MINIMAIL_TENANT_ID` | — (required) | Entra tenant id |
| `MINIMAIL_DATA_DIR` | `data` | Archive directory (`mail.db`); often a symlink to larger storage |
| `MINIMAIL_MSAL_CACHE` | `~/.config/miniteams/msal_cache.json` | Token cache shared with miniteams |
| `MINIMAIL_LOG_LEVEL` | `INFO` | Logs go to stderr (JSON when not a TTY) |
| `MINIMAIL_INBOX_EVERY` | `30` | `sync --live`: seconds between Inbox rounds, and the retry delay after a failed cycle |
| `MINIMAIL_FOLDERS_EVERY` | `300` | `sync --live`: seconds between rounds of the other folders |
| `MINIMAIL_TREE_EVERY` | `900` | `sync --live`: seconds between folder tree refreshes |
| `MINIMAIL_MAX_RPS` | `8` | Graph requests started per second, all threads together; a throttled (429/503/504) answer holds every thread back for its `Retry-After` (capped at 60 s). The quota (~10k requests / 10 min per app and mailbox) is shared with the Teams client, whose app id minimail borrows |

Exit codes: `2` no usable token, missing/invalid setting, unknown `--folder` or `--live` with `--folder`,
`3` sign-in service unavailable (retry), `1` Graph error, archive error (e.g. locked by
another writer; traceback in the log) or `verify` mismatch, `130` interrupted
(Ctrl-C / SIGTERM).

The archive is personal data: its directory is `0700`, its files `0600`. Never commit `data/`.
