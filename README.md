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
| `uv run minimail folders` | Sync the folder tree; print server vs local item counts per folder |
| `uv run minimail sync [--folder PATH]… [--retry-failed]` | Mirror messages (verbatim MIME + metadata). First run = full backfill, then incremental; resumes after a crash |
| `uv run minimail verify [--refetch N]` | Check every stored MIME against its sha256; `--refetch N` compares N random messages to the server byte for byte |
| `uv run minimail reindex` | Recompute search text from stored MIME, no download (stop `sync` first: single writer) |

## Configuration (`MINIMAIL_*`, env or `.env`)

| Variable | Default | |
|---|---|---|
| `MINIMAIL_TENANT_ID` | — (required) | Entra tenant id |
| `MINIMAIL_DATA_DIR` | `data` | Archive directory (`mail.db`); often a symlink to larger storage |
| `MINIMAIL_MSAL_CACHE` | `~/.config/miniteams/msal_cache.json` | Token cache shared with miniteams |
| `MINIMAIL_LOG_LEVEL` | `INFO` | Logs go to stderr (JSON when not a TTY) |

Exit codes: `2` no usable token, missing setting or unknown `--folder`, `3` sign-in service unavailable
(retry), `1` Graph error or `verify` mismatch.

The archive is personal data: its directory is `0700`, its files `0600`. Never commit `data/`.
