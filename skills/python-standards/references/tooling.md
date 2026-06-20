# Tooling — dependencies, configuration, and quality gates

## Package & environment management — uv, NOT pip

`uv` is the only entry point. Using commands that include `pip` or `python`, including pip install` or
`python -m venv`, are not recommended.

- `uv add <pkg>` / `uv add --dev <pkg>` to add a dependency (updates
  `pyproject.toml` *and* `uv.lock` atomically).
- `uv sync` to reproduce the environment exactly from the lockfile.
  `uv sync --frozen` in CI — it fails if the lock is stale rather than
  silently resolving.
- `uv run <cmd>` to execute anything inside the environment. Never call a
  bare `python`, `ruff`, `pytest` — they may resolve to a different
  interpreter.
- **Commit all three:** `pyproject.toml`, `uv.lock`, `.python-version`.
  Their `requires-python` and pinned interpreter must agree.

Version pinning policy:

- **Exact pins** (`pydantic==2.10.3`, `httpx==0.28.1`) for libraries whose
  behavior changes between minor versions and would bite you silently.
- **Ranged pins** (`sqlalchemy>=2.0`) for stable, semver-disciplined APIs.
- **Internal/git-sourced deps** are declared under `[tool.uv.sources]`,
  not pasted as URLs in the dependency list — keeps the version surface in
  one place.


## Recommended dependencies by use case

Reach for the library in the middle column. The right column is the rule
that comes with it (and what *not* to use). The deeper "how to use it
well" lives in `patterns.md` and the skill's I/O section — this table is
just the *what*.

| Use case                              | Reach for                                  | Rule / avoid                                                                       |
| ------------------------------------- | ------------------------------------------ | ---------------------------------------------------------------------------------- |
| Validation at boundaries              | `pydantic` v2                              | `model_validate` untrusted input; see data-modeling.md. Not manual dicts.          |
| Configuration from env                | `pydantic-settings`                        | One nested `AppSettings`; `SecretStr` for creds. Not scattered `os.environ`.       |
| Postgres — driver / parameterized SQL | `psycopg` 3 (`psycopg[binary]`)            | Not psycopg2. SQL never built by f-string/concat from variable input.              |
| Postgres — ORM / query building       | `sqlalchemy` 2.0, **async**                | `create_async_engine`, `AsyncSession`, `Mapped[...]`, `select()`. Not 1.x `Query`. |
| Vector similarity search              | `pgvector`                                 | Use its SQLAlchemy `Vector` column; don't hand-roll distance SQL.                  |
| Schema migrations                     | `alembic`                                  |                                                                                    |
| HTTP / REST clients                   | `httpx` (`AsyncClient`)                    | Not `requests`/`urllib`. Explicit timeout on every call; `raise_for_status`.       |
| Async orchestration                   | stdlib `asyncio`                           |                                                                                    |
| Dates & times                         | stdlib `datetime` + `zoneinfo`             | Store UTC, convert at the edge. No naive datetimes (`DTZ`); not `pytz`.            |
| CLI / entry point                     | `typer` subcommands                        | Defaults come from settings, not constants.                                        |
| Lint + format                         | `ruff`                                     | One tool for both; don't also run black/isort/flake8.                              |
| Type checking                         | `mypy` (`strict`) + `pydantic.mypy` plugin | `pyright` for editor feedback is fine; mypy is the gate.                           |
| Testing                               | `pytest` + `pytest-asyncio` + `hypothesis` | HTTP fakes via `httpx.MockTransport`, not respx/MagicMock                          |
| Dead-code detection                   | `vulture` (`min_confidence=80`) on `src/`  | Delete speculative helpers; reintroduce when actually used.                        |


### Async I/O is wrapped in a class that owns its resource

When the recommended library is an async client (httpx, the SQLAlchemy
engine), don't pass raw clients around or open connections inline. Wrap
each external dependency in a class that **owns the resource and
implements the async context-manager protocol** — so callers acquire and
release it with `async with`, and tests can swap in a fake:

```python
class APIClient:
    def __init__(self, base_url: str, token_store: TokenStore) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=30.0)
        self._token_store = token_store

    async def __aenter__(self) -> "APIClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def get_user(self, user_id: str) -> User:          # behavior → verb method
        resp = await self._client.get(f"/users/{user_id}")
        resp.raise_for_status()
        return User.model_validate(resp.json())              # validate at the boundary
```

- **One engine per process** (app-context attribute or `lru_cache`
  factory), pool configured explicitly. Sessions come from
  `async_sessionmaker(expire_on_commit=False)`, never `Session()` inline.
- The client depends on a `Protocol` (`TokenStore`), not a concrete impl —
  that's what makes the in-memory test fake trivial.
- Full engine/session/token-refresh code is in `patterns.md`; the rule
  here is only *async clients are classes, and classes own their
  resources*.

  ## Default pyproject.toml

```toml
[project]
name = "my-project"
requires-python = ">=3.12"
dependencies = [
    "pydantic==2.10.3",          # exact: validation behavior is load-bearing
    "pydantic-settings>=2.0",
    "sqlalchemy>=2.0",           # ranged: stable, semver-disciplined
    "psycopg[binary]>=3.2",
    "pgvector>=0.3",
    "httpx==0.28.1",             # exact: transport/timeout semantics shift between minors
    "alembic>=1.13.0",
]

[dependency-groups]
dev = ["mypy", "ruff", "pytest", "pytest-asyncio", "hypothesis", "vulture"]
```

Ruff — lint + format in one tool. `line-length` is the only formatting
knob you set; the rest is ruff's opinion, deliberately:

```toml
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = [
    "E", "W",    # pycodestyle
    "F",         # pyflakes
    "I",         # isort
    "B",         # bugbear (mutable defaults, useless expressions)
    "UP",        # pyupgrade (keeps typing syntax modern automatically)
    "SIM",       # simplify
    "C4",        # comprehensions
    "RET",       # return statements
    "ARG",       # unused arguments
    "PTH",       # enforce pathlib over os.path
    "DTZ",       # naive datetime detection
    "S",         # bandit security checks
    "N",         # naming (snake_case funcs, PascalCase classes)
    "RUF",       # ruff-specific
]
ignore = [
    "E501",      # line length — ruff format owns it
    "S101",      # assert is fine in tests
]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S", "ARG"]        # fixtures take unused args; test secrets are fake
```

Mypy — `strict` is the target for *new* code. Existing debt and untyped
imports are isolated per-module, never loosened globally:

```toml
[tool.mypy]
python_version = "3.12"
strict = true
warn_unreachable = true
warn_return_any = true
warn_unused_ignores = true
plugins = ["pydantic.mypy"]      # teaches mypy about model fields/validators

# Untyped internal/third-party libs: scoped override per module, with a
# comment explaining why. A visible, shrinkable list — not `strict = false`.
[[tool.mypy.overrides]]
module = ["some_untyped_lib.*", "legacy_internal_pkg.*"]
ignore_missing_imports = true 
```

Vulture and pytest:

```toml
[tool.vulture]
paths = ["src"]
min_confidence = 80

[tool.pytest.ini_options]
addopts = "-q --strict-markers"
testpaths = ["tests"]
asyncio_mode = "auto"            # every `async def test_*` runs without a marker
```


Database Migrations with Alembic:

```toml
[alembic]
script_location = migrations/alembic
```

## Quality gates — run them, don't assume

```bash
#!/usr/bin/env bash
set -e # Exit immediately if any command fails

echo "--- Syncing environment ---"
uv sync

echo "--- Linting & Formatting ---"
uv run ruff check --fix .
uv run ruff format .

echo "--- Type Checking ---"
uv run mypy src/

echo "--- Dead Code Detection ---"
uv run vulture

echo "--- Running Tests ---"
uv run pytest
```

What each gate buys you:

- **`S` (bandit)** — lint-level security: flags `shell=True`, weak hashes,
  `yaml.load`, hardcoded-password patterns, SQL built by string.
- **`DTZ`** — naive datetimes; pairs with the rule *store UTC, convert at
  the edge* (`zoneinfo.ZoneInfo("America/New_York")` for NYC sources).
- **`N`** — snake_case functions / PascalCase classes. Source-system
  camelCase belongs in `Field(alias=...)`, never in Python identifiers.
- **`PTH` / `UP`** — pathlib over `os.path`; modern typing syntax kept
  current automatically.
- **mypy `strict`** — code you ship must pass cleanly; run it rather than
  eyeballing types.
- **vulture** — catches dead helpers before they rot.


## Project skeleton

See "Project layout" at the top of [patterns.md](patterns.md) — `src/`
with `main.py`, `core/`, `clients/`, `models/`, `schema/`, `sources/`,
`workflows/`, plus `migrations/`, `scripts/`, `data/`, `tests/` at the
repo root.
