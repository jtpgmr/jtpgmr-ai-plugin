# Patterns — concrete idioms

Read the section relevant to the code being written. These are reference
implementations of the rules in SKILL.md.

## Project layout

```
my-project/
├── pyproject.toml  uv.lock  .python-version
├── .env.example           # committed env contract; .env is gitignored
├── alembic.ini
├── README.md               # commands table, workflow, data-safety notes
├── migrations/             
│   ├── alembic/            # Alembic's internal configuration
│   │   ├── env.py          
│   │   └── script.py.mako  
│   ├── versions/          
│   ├── 0001_init.sql       
│   └── 0002_add_users.sql  
├── data/                   # local inputs/outputs (gitignored)
├── scripts/                # one-off operational scripts (still typed + linted)
├── tests/
└── src/
    ├── main.py             # entry point: python -m src.main, argparse subcommands
    ├── core/               # settings.py, context.py — app composition
    ├── clients/            # db/, dynamics/, <api>/ — one package per external system
    ├── models/             # pydantic: inbound rows, outbound payloads, shared typing
    ├── schema/             # SQLAlchemy ORM tables, base.py, migrate.py runner
    └── workflows/          # one module per pipeline
```

## Configuration (pydantic-settings)

One `AppSettings` at the composition root; per-subsystem nested models;
env vars use the `PREFIX__FIELD` shape via `env_nested_delimiter`.

```python
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseModel):
    host: str
    user: str
    password: SecretStr
    database_name: str
    port: int = 5432
    pool_size: int = 10
    max_overflow: int = 10

    @property
    def dsn(self) -> str:
        pwd = self.password.get_secret_value()
        url = f"postgresql+psycopg://{self.user}:{pwd}@{self.host}:{self.port}/{self.database_name}"
        return url


class ServiceApiSettings(BaseModel):
    """An external REST API reached with OAuth client credentials."""

    base_url: str | None = None
    client_id: str | None = None
    client_secret: SecretStr | None = None
    token_cache_file: Path | None = None     # where refreshed tokens are persisted
    http_timeout: float = 30.0


class RuntimeSettings(BaseModel):
    environment: Literal["dev", "qa", "prod"] = "dev"
    log_level: str = "INFO"
    default_concurrency: int = 10
    dry_run: bool = False


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )
    db: DatabaseSettings
    dynamics: DynamicsSettings | None = None  # optional: --db-only runs need no DYNAMICS__*
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)

settings = AppSettings()  # at the composition root only — fails loudly if env is wrong
```

Rules this encodes:

- Never read env vars inside business logic; pass settings (or sub-models)
  down.
- `SecretStr` for credentials; `.get_secret_value()` only at point of use.
- Optional subsystems are `Sub | None` or expose `.configured` — partial
  runs must not demand unrelated env vars.
- Environment-specific values (tenant codes, seed GUIDs) have **no DEV
  defaults in code**. A missed PROD override must fail, not silently use
  DEV values.
- Credential/token files written to disk: `path.write_text(...)` then
  `path.chmod(0o600)`, plus a `.gitignore` entry.

### `.env.example` template (committed; `.env` is gitignored)

```bash
# --- Database (Postgres) ---
DB__HOST=localhost
DB__PORT=5432
DB__USER=postgres
DB__PASSWORD=change-me
DB__DATABASE_NAME=postgres
DB__POOL_SIZE=10
DB__MAX_OVERFLOW=10

# --- External API (omit entirely for --db-only runs) ---
SERVICE__BASE_URL=
SERVICE__CLIENT_ID=
SERVICE__CLIENT_SECRET=
# SERVICE__TOKEN_CACHE_FILE=.service_token.json   # chmod 0600, gitignored
SERVICE__HTTP_TIMEOUT=30

# --- Runtime ---
RUNTIME__ENVIRONMENT=dev         # dev | qa | prod — gates seeds and safety checks
RUNTIME__LOG_LEVEL=INFO
RUNTIME__DEFAULT_CONCURRENCY=10
RUNTIME__DRY_RUN=false
```

Grouped by subsystem prefix, every variable present, comments explain
purpose and defaults. This file is the env contract for the project.






## Validation boundary — the three model layers
 
Inbound (source-shaped) → internal (DB-shaped) → outbound (API-shaped).
Each layer has its own model; mapping between them is explicit code. The
internal layer is the SQLAlchemy ORM model (see the Database section); the
two pydantic layers are below.
 
Normalization is a small, *typed* function reused through an `Annotated`
type — not an `object -> object` catch-all:
 
```python
from typing import Annotated
 
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
 
 
class UserImportRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True)

    record_id: str | None = Field(default=None, alias="Record ID")
    full_name: str | None = Field(default=None, alias="Full Name")
    email: str | None = Field(default=None, alias="E-mail Address")
 
 
class CreateUserPayload(BaseModel):
    """Outbound: target-API field names; unknown keys are a bug."""
 
    model_config = ConfigDict(extra="forbid")
 
    name: str
    email: str | None = None
 
    def to_dict(self) -> dict[str, object]:
        return self.model_dump(exclude_none=True, exclude_unset=True)
```
 
## Exceptions
 
```python
class CollectorError(Exception):
    """Base for this package."""
 
class UpstreamUnavailable(CollectorError):
    """Upstream returned a retryable failure."""
```


### Always surface the HTTP response body
 
A 400 whose body is hidden behind `str(exc)` is undebuggable. Extract it:
 
```python
def http_error_detail(exc: Exception) -> str:
    """Pull the API's error message out of an HTTP exception."""
    body = getattr(exc, "response_body", None)          # SDK-style exceptions
    if body is None and (resp := getattr(exc, "response", None)) is not None:
        try:
            body = resp.json()                           # httpx.HTTPStatusError
        except ValueError:
            return f"{exc} :: {resp.text[:500]}"
    if isinstance(body, dict):
        err = body.get("error", body)
        msg = err.get("message") if isinstance(err, dict) else None
        if msg:
            return f"{exc} :: {msg}"
    return str(exc)
```
 
Raise/log with this detail at the call site:
 
```python
try:
    resp = client.get(url)
    resp.raise_for_status()
except httpx.HTTPStatusError as err:
    if err.response.status_code in (429, 502, 503):
        raise UpstreamUnavailable(http_error_detail(err)) from err
    raise CollectorError(http_error_detail(err)) from err
```
 
"Expected miss" lookups catch the *specific* not-found type, never blanket
`Exception`:
 
```python
try:
    token = token_cache_file.read_text().strip()
except FileNotFoundError:      # first run, nothing cached — expected
    token = None               # anything else (permissions, decode) propagates
```




## Database (async SQLAlchemy 2.0 + psycopg3)
 
One engine and pool per process. Construction is **cheap** — it only stores
config; `connect()` builds the pool and `close()` disposes it, and the
class implements the context-manager protocol so the lifecycle can't be
skipped. Touching a session before `connect()` fails loud:
 
```python
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
 
from pydantic import PostgresDsn
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
 
 
class AsyncDatabase:
    """Owns one async engine + pool. Cheap to construct; `connect()` builds
    the pool, `close()` disposes it. Use as an async context manager."""
 
    def __init__(
        self,
        dsn: str | PostgresDsn,
        *,
        pool_size: int = 10,
        max_overflow: int = 10,
        echo: bool = False,
    ) -> None:
        self._dsn = str(dsn)
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._echo = echo
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None
 
    async def connect(self) -> None:
        self._engine = create_async_engine(
            self._dsn,
            pool_size=self._pool_size,
            max_overflow=self._max_overflow,
            pool_pre_ping=True,      # detect stale/killed connections
            pool_recycle=1800,       # beat idle-timeout (managed Postgres often ~30min)
            echo=self._echo,
        )
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False, autoflush=False
        )
 
    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None
 
    async def __aenter__(self) -> "AsyncDatabase":
        await self.connect()
        return self
 
    async def __aexit__(self, *exc: object) -> None:
        await self.close()
 
    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("AsyncDatabase.connect() not called")
        return self._engine
 
    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._sessionmaker is None:
            raise RuntimeError("AsyncDatabase.connect() not called")
        async with self._sessionmaker() as s:
            yield s
 
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.session() as s, s.begin():
            yield s
```
 
The sync `Database` is the exact mirror, for code that isn't on an event
loop (one-off scripts, the migration runner) — same shape, sync primitives:
 
```python
class Database:
    """Sync mirror of AsyncDatabase for scripts and the migration runner."""
 
    def __init__(
        self,
        dsn: str | PostgresDsn,
        *,
        pool_size: int = 10,
        max_overflow: int = 10,
        echo: bool = False,
    ) -> None:
        self._dsn = str(dsn)
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._echo = echo
        self._engine: Engine | None = None
        self._sessionmaker: sessionmaker[Session] | None = None
 
    def connect(self) -> None:
        self._engine = create_engine(
            self._dsn,
            pool_size=self._pool_size,
            max_overflow=self._max_overflow,
            pool_pre_ping=True,
            pool_recycle=1800,
            echo=self._echo,
        )
        self._sessionmaker = sessionmaker(self._engine, expire_on_commit=False, autoflush=False)
 
    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._sessionmaker = None
 
    def __enter__(self) -> "Database":
        self.connect()
        return self
 
    def __exit__(self, *exc: object) -> None:
        self.close()
 
    @contextmanager
    def session(self) -> Iterator[Session]:
        if self._sessionmaker is None:
            raise RuntimeError("Database.connect() not called")
        with self._sessionmaker() as s:
            yield s
 
    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self.session() as s, s.begin():
            yield s
```
 
Lifecycle rules this encodes:
 
- **Cheap constructor.** `__init__` stores config only — no I/O, no pool.
  Cheap to create in tests; the composition root decides *when* the pool
  opens. (Matches the skill's "constructor work stays cheap".)
- **Resource owner ⇒ context manager.** The class owns the engine, so it
  implements `__aenter__/__aexit__` (sync: `__enter__/__exit__`).
  `connect()`/`close()` stay public for frameworks that own the lifecycle
  differently (a long-lived service connecting once at startup).
- **Fail loud.** `session()`, `transaction()`, and `engine` raise
  `RuntimeError` before `connect()` — a clear message, not an
  `AttributeError` deep inside a query.
- **One pool per process.** Construct once at the composition root and
  inject the instance; never build an engine per request or per row.
- **Sync vs async.** Async for pipelines and concurrent fan-out; sync
  `Database` for scripts and the migration runner that run outside an event
  loop. The same `postgresql+psycopg://` DSN drives both engines.
ORM models use 2.0 `Mapped` syntax with recyclable declarative bases: an
insert-only audit base, an updatable mixin, and a schema-scoped abstract
base per Postgres schema. This is the standard base for all SQL tables:
 
```python
from __future__ import annotations
 
import datetime
import uuid
 
from sqlalchemy import DateTime, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
 
 
class BaseTable(DeclarativeBase):
    """Insert-only audit base."""
 
    serial_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    id: Mapped[uuid.UUID] = mapped_column(
        unique=True,
        server_default=text("gen_random_uuid()"),
        nullable=False,
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    created_by_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
 
 
class UpdatableBaseTable:
    """Mixin for mutable tables; modified_* stamped by DB trigger on UPDATE."""
 
    modified_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    modified_by_id: Mapped[uuid.UUID | None]
 
 
class CoreBase(BaseTable):
    __abstract__ = True
    __table_args__: dict[str, str] = {"schema": "core"}
 
 
class Example(CoreBase, UpdatableBaseTable):
    __tablename__ = "table"
 
    a_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    b_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    c: Mapped[str] = mapped_column(nullable=False)
```
 
Why this shape: `serial_id` is the internal join key (fast int PK), `id`
the externally shared UUID; `DateTime(timezone=True)` everywhere — naive
timestamps are a bug; one abstract `CoreBase` per Postgres schema keeps
`__table_args__` in a single place; the updatable mixin is opt-in so
insert-only link tables don't carry dead `modified_*` columns.
 


## SQL & DDL
 
```sql
CREATE TABLE IF NOT EXISTS users (
    serial_id       INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id              UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    ...
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_id   UUID NOT NULL,
    -- if table records are intended to be updatable, audit
    -- modified_at     TIMESTAMPTZ,
    -- modified_by_id  UUID
);
```




## Decorator toolkit
 
Reach for these deliberately — each has a rule, not just a syntax.
 
### `@property` — cheap derived values only
 
```python
class DatabaseSettings(BaseModel):
    @property
    def dsn(self) -> str:              # derives from fields already in memory
        ...
 
 
class ServiceApiSettings(BaseModel):
    @property
    def configured(self) -> bool:      # cheap predicate
        return bool(self.base_url and self.client_id and self.client_secret)
```
 
Rules: no I/O, no network, no DB, O(small), and reading it twice gives the
same answer. The caller sees an attribute — attributes must not surprise.
If it's expensive or can fail in interesting ways, make it a method
(`load_config()`), or `cached_property` if it's expensive-but-pure.
 
### `@cached_property` — expensive-but-pure, once per instance
 
```python
from functools import cached_property
 
class Workbook:
    @cached_property
    def sheet_index(self) -> dict[str, int]:   # built on first access, stored on the instance
        return {ws.title: i for i, ws in enumerate(self._wb.worksheets)}
```
 
Caveat: cached forever on the instance — only for values that can't go
stale within the object's lifetime. Mutating underlying state invalidates
nothing.
 
### `@lru_cache` — process-lifetime factories and pure lookups
 
The standard pattern for one-per-process collaborators (settings, engine,
db, clients, knowledge):
 
```python
from functools import lru_cache
 
@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()
 
@lru_cache
def get_engine():                      # one pool per process
    return create_engine(get_settings().db.dsn, pool_pre_ping=True)
 
@lru_cache(maxsize=256)
def normalize_org_name(raw: str) -> str:   # pure keyed lookup: set a maxsize
    ...
```
 
Rules:
 
- Arguments must be hashable; the cache key IS the args — a settings
  object in the signature usually means you wanted a no-arg factory.
- **Never on instance methods** — it keeps `self` alive forever (leak) and
  shares the cache across instances. Use `cached_property` instead.
- Only for pure/stable results. No TTL exists: a cached value lives until
  process exit. That's correct for factories and run-scoped lookups
  (users, data sources); it's wrong for anything that must reflect
  external change — say so in the docstring ("run-scoped").
- Factories take `maxsize=None`-style bare `@lru_cache`; keyed lookups get
  an explicit `maxsize`.
- Tests that need isolation call `get_settings.cache_clear()` in a fixture.
### `@contextmanager` / `@asynccontextmanager` — resource lifecycles
 
The `session()` / `transaction()` pattern in the Database section. Any
acquire/release pair you write twice becomes one of these.
 
### `@staticmethod` / `@classmethod`
 
- pydantic validators are `@field_validator(...)` + `@classmethod`.
- Alternative constructors: `@classmethod def from_env(cls) -> Self`.
- A `@staticmethod` with no class state is often just a module function —
  prefer the module function unless grouping genuinely aids discovery.