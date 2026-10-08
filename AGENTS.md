# AGENTS.md

## Setup / checks

- `uv sync`; `uv run pytest --cov` (coverage floor in `pyproject.toml`); `uv run pre-commit run --all-files`.
- Stage new files before `pre-commit run --all-files`: it skips untracked files (mypy then fails at commit).

## Conventions

- Specs live in `specs/NNN-*.md`; tests covering an acceptance criterion cite it as `001-AC3`.
- Graph calls go through `graph.Graph` (immutable ids, retries, host pinning) — never a raw httpx call.
- Writes to the archive happen on one thread; readers open it with `store.open_readonly`.

## Sensitive data

- `data/` holds real mail: never commit it, never paste its content, counts or ids into code, tests,
  specs or commits. Fixtures use invented names and addresses (`example.test`).
- Live checks against the real mailbox are read-only; any write (test mail/event) needs the user's go.
