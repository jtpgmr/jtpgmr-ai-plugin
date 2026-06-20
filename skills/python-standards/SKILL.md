---
name: python-standards
description: Standards for writing effective, modern, robust, and scalable
  Python. Apply this skill whenever writing, refactoring, or extending Python
  code of any kind — scripts, modules, agents, data pipelines, API clients,
  CLI tools — even if the user doesn't mention code quality. Trigger on
  requests like "write a script", "build a function/class/module", "refactor
  this", "add a feature", "create an agent/pipeline/client", or any task that
  produces .py files, or any work touching .py or pyproject.toml files.
---

# Python standards

Apply these rules to all Python you write in this session. They are standing
instructions, not a one-time checklist. When an existing codebase clearly
follows different conventions, match the codebase and note the divergence
once.

## Baseline

- Target Python 3.12+. Manage projects with `uv` (`uv add`, `uv sync`,
  `uv run`; commit `pyproject.toml`, `uv.lock`, `.python-version`). Never
  bare `pip install` into the system.
- Lint/format with `ruff` (lint + format), type-check with `mypy` — strict
  on new code, untyped internal/third-party libs isolated via scoped
  `[[tool.mypy.overrides]]`, never global loosening. Code you produce must
  pass both cleanly — run them when the environment allows instead of
  assuming.
- `src/` layout, organized by role: `core/` (settings, app context),
  `clients/` (db, external APIs), `models/` (pydantic) + `schema/` (ORM,
  migration runner), `workflows/` (pipelines), `sources/` (ingestion),
  plus `migrations/`, `scripts/`, `data/`, `tests/` at the repo root.
  Entry point `python -m src.main` with argparse subcommands whose
  defaults come from settings, not constants. No god-modules or
  `utils.py` dumping grounds.
- Naming: `snake_case` functions/variables, `PascalCase` classes,
  `UPPER_CASE` constants, `_leading_underscore` for module-private.
  Source-system field names (CSV headers, external-API camelCase) live
  only in `Field(alias=...)` and payload models — never leak into Python
  identifiers.

## Typing — non-negotiable

- Every function signature fully annotated, including `-> None`.
- Modern syntax only: `list[str]`, `dict[str, int]`, `X | None` — never
  `List`, `Optional`, `Union` from typing.
- No `Any` leaking through public signatures. At true unknowns, accept
  `object` and narrow, or validate into a typed model.
- Choose the right shape by trust and coupling: `dataclass(slots=True)`
  for trusted internal records (`frozen=True` for value objects), pydantic
  `BaseModel` only at validation boundaries, `Protocol` for consumer-side
  interfaces (ABC only for an owned hierarchy with shared behavior),
  `Enum`/`Literal` instead of magic strings, `NamedTuple` for small
  immutable returns. Full decision table and pydantic-vs-dataclass-vs-ABC
  guidance: [references/data-modeling.md](references/data-modeling.md).
- `TypeVar`/`ParamSpec` for genuinely generic code; don't fake genericity
  with overloads of `Any`.

## Boundaries: validate in, validate out

- All external data — API responses, DB rows feeding logic, file contents,
  LLM/agent output, user input — is untrusted until parsed into a pydantic
  model (v2: `model_validate`, `field_validator`, `Annotated` constraints).
  Inside the boundary, pass typed objects, never raw dicts.
- Keep three model layers distinct: inbound row models
  (`Field(alias=...)` matching source headers, `str_strip_whitespace=True`
  for normalization), internal DB models (snake_case, the ORM layer), and
  outbound payload models (`extra="forbid"`, target-API field names,
  `model_dump(exclude_none=True)`). Map between layers explicitly; never
  ship a raw source dict to an API. Normalization beyond stripping lives in
  the model (a `mode="before"` validator or an `Annotated` type), not a
  loose `object -> object` helper.
- Configuration via `pydantic-settings`: one `AppSettings(BaseSettings)`
  with nested per-subsystem models (`db`, an external-API block,
  `runtime`), `env_nested_delimiter="__"` so env vars group as
  `DB__HOST`, `SERVICE__BASE_URL`. `SecretStr` for credentials; computed
  fields/properties for derived values (DSN). Optional subsystems are
  `Sub | None` or expose a `.configured` property so partial runs
  (`--db-only`) don't demand unrelated env vars. Never scatter
  `os.environ[...]` through modules.
- `.env.example` is committed and is the env contract: grouped by
  subsystem prefix, every variable commented, defaults noted. `.env` is
  gitignored. Environment-specific values (tenant codes, seed GUIDs) come
  from env — never bake DEV defaults into code where a missed override
  silently seeds PROD with DEV data.
- Secrets beyond `.env`: a secrets manager (Vault, AWS/GCP Secret Manager,
  your cloud's KMS) behind pluggable get/set token handlers — the client
  depends on the handlers, not the backend, so the backing store swaps
  without touching it. Credential/token files written to disk get `0o600`
  permissions and a `.gitignore` entry.
- LLM/agent JSON output is validated like any other untrusted input before
  it touches queries, files, or control flow.

## Errors and resources

- Catch the narrowest exception that you can handle meaningfully; never
  bare `except:` or `except Exception: pass`. If you can't handle it, let it
  propagate. ("Expected miss" catches like a cache/secret not existing yet
  catch the specific not-found type, not `Exception`.)
- Define a small exception hierarchy per package (`class FooError(Exception)`)
  and raise those at public boundaries; chain with `raise X from err`. Add
  a subtype only when a caller treats it differently (retry vs. skip);
  reuse stdlib exceptions (`ValueError`, `FileNotFoundError`) where they fit
  rather than wrapping for its own sake.
- HTTP failures must surface the response body, not just `str(exc)` — a
  400 with a hidden body is undebuggable. Extract `exc.response.text` /
  the API's `error.message` into the raised error and the log line.
- In batch loops, catch per item: count it, append a detail string to a
  run summary (first N), `logger.exception` for the stack, continue. A
  step that cannot do its job (e.g. persisting a returned ID) raises or
  logs at error level — never warn-and-return-silently, which leaves
  rows half-processed with no signal.
- Every acquired resource is a context manager: connections, cursors,
  sessions, files, clients. If a class owns a resource, keep construction
  cheap and implement `__enter__/__exit__` (or async variants) that build
  the resource in `connect()` and dispose it in `close()`, rather than
  relying on callers to clean up.
- Fail loudly and early on misconfiguration (at startup), not deep in a run.

## I/O, data access, and scalability

- DB access through SQLAlchemy 2.0 style (`select()`, typed `Session`/
  `AsyncSession`, `Mapped[...]` models) or parameterized driver calls
  (psycopg3). SQL is never built with f-strings/format/concat from
  variable input; identifiers that vary come from explicit allowlists.
- One engine per process — owned by a `Database`/`AsyncDatabase` class with
  a cheap constructor, explicit `connect()`/`close()`, and the (async)
  context-manager protocol, or an `lru_cache` factory — with the pool
  configured explicitly, not defaulted: `pool_size`, `max_overflow`,
  `pool_pre_ping=True`, `pool_recycle=1800`. Sessions come from
  `async_sessionmaker(expire_on_commit=False)` via `session()` /
  `transaction()` async context managers. Touching a session before
  `connect()` raises, loudly.
- Schema lives in `migrations/` as plain ordered SQL
  (`0001_name.sql`, ...) where every statement is idempotent
  (`IF NOT EXISTS`, `DO $$ ... $$` guards) so the set applies cleanly to a
  blank or existing DB. Every table carries the audit columns
  (`serial_id` identity PK, `id` uuid, `created_at/by`; mutable tables add
  `modified_at/by`, trigger-stamped). Seed rows are inserted by
  parameterized code from settings, never hardcoded in SQL.
- Upserts target a natural key backed by a (partial) unique index;
  `ON CONFLICT DO UPDATE` excludes the conflict key and immutable columns —
  tagged on the model (`info={"immutable": True}`) or derived from the
  mapper, with a `BEFORE UPDATE` trigger as the authoritative backstop —
  and stamps `modified_*`. A row that lacks its natural key never silently
  falls through to plain INSERT — log it as a duplicate-on-rerun risk.
- Be deliberate about the N+1 problem: batch, join, or `selectinload`;
  cache stable per-run lookups (users, data sources) in module dicts but
  document them as run-scoped — no TTL means no long-lived processes.
- Stream large data: generators, server-side cursors, chunked fetches,
  `itertools` — never load unbounded result sets into a list by default.
- CSV/file ingestion: decode bytes once up front with an encoding
  fallback chain (`utf-8-sig` → `cp1252` → `errors="replace"`), then
  `csv.DictReader` → pydantic row models. Accept `Path | TextIO` so
  sources stay testable.
- HTTP with `httpx`: explicit timeout on every call, bounded retries with
  backoff for idempotent requests only — and on 429 honor the
  `Retry-After` header. `raise_for_status` handled. Token refresh is
  guarded by an `asyncio.Lock` and persisted via a handler/callback.
  Retries, backoff, rate limits, and circuit-breaking:
  [references/resilience.md](references/resilience.md) — reach for
  `tenacity` once more than one call site needs retry.
- Long-running jobs are idempotent and resumable: deterministic keys,
  upserts over blind inserts, checkpoint progress (status/attempts
  columns), per-item error capture so one failure never halts the batch.
- `pathlib` for all paths; `tempfile` for temp files.

## Observability

- `logging` (or structlog) — never `print` in library/production code.
  Module-level `logger = logging.getLogger(__name__)`.
- Log structured context (ids, counts, durations), not raw payloads;
  never log secrets, tokens, or full records containing personal data.
  Pin noisy third-party loggers (e.g. `httpx`) to WARNING — their DEBUG
  can emit headers and tokens.
- Lazy formatting: `logger.info("processed %d rows", n)`.

## Design for change

- Functions do one thing; ~40 lines is a smell worth refactoring. Prefer
  pure functions with explicit inputs over methods mutating shared state.
- Dependency injection over module-level singletons: pass clients/sessions/
  settings in (factory functions are fine — constructor work stays cheap).
- Standard decorators are deliberate tools, each with a rule: `@property`
  for cheap derived values (no I/O, can't surprise), `@cached_property`
  for expensive-but-pure per-instance values, `@lru_cache` for
  process-lifetime factories and pure keyed lookups (never on instance
  methods — it leaks `self`; results are run-scoped, no TTL),
  `@contextmanager` for any acquire/release pair. Idioms and caveats:
  the Decorator toolkit section of
  [references/patterns.md](references/patterns.md).
- Public API of a module is explicit (`__all__`); everything else is
  `_private`.
- Docstrings on public functions/classes: one summary line, then Args/
  Returns/Raises when non-obvious. Comments explain *why*, never *what*.
- Write code testable by construction: side effects at the edges, logic in
  pure cores; if a function is hard to test, restructure it instead of
  reaching for heavy mocking.

## Execution chain — mandatory order

Every Python writing task follows this chain. Steps marked (always) run on
every task; conditional steps run when their condition holds. Invoke each
skill/tool by the exact name shown.

**Phase 0 — Plan (conditional)**
- Non-trivial feature or multi-file change → run the **superpowers**
  brainstorm/plan workflow before writing code.
- Design decision between approaches/libraries → invoke
  **engineering:architecture**; new component/API shape → 
  **engineering:system-design**.

**Phase 1 — Ground (conditional)**
- Third-party API involved (SQLAlchemy, pydantic, httpx, or any external
  SDK) → pull version-correct docs via the **context7** MCP before writing
  calls.
- Source material arrives as docx/pdf/pptx/html/xlsx → convert it into
  context with the **markitdown** MCP (`mcp__markitdown__*`); do not paste
  binary content or guess at file contents.
- Schema-dependent work → describe tables first via the **postgres** or
  **Snowflake** MCP; never write queries against assumed columns.

**Phase 2 — Write (always)**
- Apply every standard in this skill. SQL beyond a trivial select →
  invoke **data:sql-queries** for the dialect-correct form. Data analysis
  code → route to **data:explore-data** / **data:statistical-analysis** /
  **data:create-viz**. Pydantic AI work → the **pydantic-ai** plugin
  guidance takes precedence.

**Phase 3 — Verify (always)**
- Run the **python-analyzer** MCP on every new/changed module:
  `mcp__python-analyzer__*` tools for Ruff lint + format and Vulture
  dead-code detection. Fix all findings; rerun until clean. If the MCP is
  unavailable, fall back to `uv run ruff check --fix && uv run ruff format`
  and `uv run mypy`.
- Unclear diagnostics → read them from **pyright-lsp** output rather than
  re-deriving types by eye.
- Code touching auth, secrets, SQL, subprocess, or external input →
  invoke the bundled **/security-review** skill on the changed modules.

**Phase 4 — Refine (conditional)**
- Any touched function that is long or convoluted → invoke
  **code-simplifier** before presenting.
- New logic worth keeping → invoke **engineering:testing-strategy** to
  enumerate cases, write the tests, run `uv run pytest`.
- Failure with a non-obvious cause → stop guessing; invoke
  **engineering:debug** (stubborn cases:
  **superpowers:systematic-debugging**) with the actual error output.

**Phase 5 — Deliver (conditional)**
- Public API or module created → invoke **engineering:documentation**
  for docstrings/README.
- Deliverable needs another format (md → docx/pdf for stakeholders) →
  convert with the **pandoc** MCP (`mcp__pandoc__*`); do not hand-write
  docx XML or reformat manually.

Chain discipline: do not skip Phase 3 even for "small" changes; do not
invoke skills outside their listed condition — each invocation costs
context. One specialist per concern, at the moment the concern arises.

## When deeper detail is needed

- Concrete idioms and full code patterns (config, db lifecycle, async,
  streaming, upserts): read [references/patterns.md](references/patterns.md).
- Default `pyproject.toml`, dependency choices by use case, and the
  lint/type/test gates: read [references/tooling.md](references/tooling.md).
- Retries, backoff, rate limits, circuit breaking, degradation when an
  upstream is flaky: read [references/resilience.md](references/resilience.md).
- Choosing pydantic vs dataclass vs ABC vs Protocol/TypedDict/Enum:
  read [references/data-modeling.md](references/data-modeling.md).