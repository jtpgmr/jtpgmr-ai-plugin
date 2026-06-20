# Data modeling — pydantic vs dataclass vs ABC vs Protocol

Pick the lightest shape that does the job. Two questions decide it:

1. **Data or behavior?** Are you describing the *shape of a thing*
   (a record, a payload, a value) or *what a class can do* (a contract,
   a hierarchy)? Data → pydantic / dataclass / NamedTuple / TypedDict.
   Behavior → Protocol / ABC.
2. **For data: how much trust, how much coupling?** Trust — does it need
   validating? Untrusted or leaving the process → pydantic; already trusted
   and in-process → dataclass. Coupling — does the contract need enforcing
   across a boundary you don't fully own?

The two axes are independent: a class can validate data *and* satisfy a
behavioral contract, and those are answered by different tools (see
[Combining them](#combining-them)).

Which concrete library to install for a given job — psycopg, httpx,
pydantic-settings, etc. — is in [tooling.md](tooling.md). This file is
only about *shape*.

## Decision tables

**Data shapes** — "what does this thing look like?"

| Need                                                          | Use                                                |
| ------------------------------------------------------------- | -------------------------------------------------- |
| Validate/normalize untrusted data (CSV, API, LLM, user input) | pydantic `BaseModel`                               |
| Configuration from env                                        | pydantic `BaseSettings` (`pydantic-settings`)      |
| Outbound payload (serialization, aliases, `extra="forbid"`)   | pydantic `BaseModel`                               |
| Trusted internal record passed between your own functions     | `dataclass(slots=True)`                            |
| Immutable value object / dict key / dedupe key                | `dataclass(frozen=True, slots=True)`               |
| Small multi-value return                                      | `NamedTuple`                                       |
| Typing a dict you don't control (kwargs, JSON you only read)  | `TypedDict`                                        |
| Closed set of values                                          | `Enum`/`StrEnum`, or `Literal[...]` for 2–3 inline |

**Contracts & behavior** — "what can this class do?"

| Need                                                               | Use        |
| ------------------------------------------------------------------ | ---------- |
| Interface a function depends on (DI, fakes in tests)               | `Protocol` |
| Shared concrete behavior + subclasses you own must override pieces | `ABC`      |

## Pydantic — at boundaries, and only at boundaries

Pydantic buys validation, coercion, aliases, and serialization — and pays
for them on every instantiation. That's the right trade exactly where data
is untrusted or leaves the process:

```python
class UserImportRow(BaseModel):         # inbound: untrusted CSV
    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True)
    email: str | None = Field(default=None, alias="E-mail Address")


class CreateUserPayload(BaseModel):     # outbound: serialization contract
    model_config = ConfigDict(extra="forbid")
    email: str
    display_name: str
```

Not for hot-path internal records: constructing a `BaseModel` per row in a
tight loop is validation you already did at the boundary, paid again. Once
data is inside, it travels as dataclasses or stays in the boundary model
*unmodified* — re-wrapping the same data in new models at each layer is a
smell.

### Fields, properties, methods, constructors

The shape of a pydantic class follows one rule: **fields are the data;
everything else is derived value or behavior.**

```python
class Invoice(BaseModel):
    subtotal: Decimal                       # fields = the data (nouns, annotated)
    tax_rate: Decimal

    @field_validator("tax_rate")            # normalize/validate — NOT a constructor
    @classmethod
    def _clamp(cls, v: Decimal) -> Decimal:
        return max(v, Decimal(0))

    @computed_field                         # derived AND in model_dump()/JSON
    @property
    def total(self) -> Decimal:
        return self.subtotal * (1 + self.tax_rate)

    @property                               # derived, internal only — not serialized
    def is_taxable(self) -> bool:
        return self.tax_rate > 0

    def apply_discount(self, pct: Decimal) -> "Invoice":   # behavior → verb method
        return self.model_copy(update={"subtotal": self.subtotal * (1 - pct)})

    @classmethod
    def from_line_items(cls, items: list[LineItem]) -> "Invoice":   # named constructor
        return cls(subtotal=sum(i.amount for i in items), tax_rate=Decimal(0))
```

- **Don't write `__init__`.** Pydantic generates it from the field
  declarations; hand-writing it fights the framework. Normalize with
  validators, not by intercepting construction.
- **Alternate construction → named `@classmethod` factories**
  (`from_line_items`, `from_csv_row`). Their names document the input;
  `__init__` can't.
- **`@computed_field @property`** when the derived value should appear in
  the serialized output; **plain `@property`** when it's for internal use
  only. Choosing wrong here either leaks or hides fields — the common slip.
- Methods are verbs (behavior); prefer returning a new instance
  (`model_copy`) over mutating, especially for models shared across tasks.

## Dataclasses — trusted internal structure

```python
@dataclass(slots=True)
class RunSummary:                       # mutable accumulator, internal only
    processed: int = 0
    errored: int = 0
    error_details: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Money:                            # value object: immutable, hashable
    amount: Decimal
    currency: str
```

- `slots=True` by default: less memory, and typo'd attributes raise
  instead of silently creating new ones.
- `frozen=True` when the instance is a *value* — usable in sets/dict keys,
  safe to share across tasks. Module-level constant/lookup tables are
  always frozen.
- No validation happens — that's the point. If you find yourself writing
  `__post_init__` checks on external data, you wanted pydantic. (Use
  `__post_init__` only for cheap derived fields, not validation.)
- Alternate constructors are `@classmethod` factories here too; don't
  hand-write `__init__` — the generated one is the contract.
- A dataclass holding `dict[str, str]` soup that crosses a module boundary
  (e.g. a workflow result consumed elsewhere) is a candidate for promotion
  to pydantic or to typed fields — runtime trust must match the type story.

## Protocol vs ABC — structural vs nominal

**Default to `Protocol`** for "I need *something that can do X*":

```python
class TokenStore(Protocol):
    def load(self) -> str | None: ...
    def save(self, token: str) -> None: ...


def connect(token_store: TokenStore) -> Client: ...
# A Redis-backed impl, a file impl, and an in-memory test fake all satisfy
# it WITHOUT importing TokenStore — zero coupling, trivial fakes.
```

Use an **ABC** only when you own the hierarchy and there is real shared
*behavior* to inherit plus pieces subclasses must supply:

```python
class Source(ABC):
    def run(self) -> RunSummary:        # shared template logic
        summary = RunSummary()
        for row in self.parse_rows():
            ...
        return summary

    @abstractmethod
    def parse_rows(self) -> Iterator[dict[str, str]]: ...
```

Rules of thumb:

- Consumer-side contracts → Protocol (the consumer defines what it needs).
  Implementation-side reuse → ABC (the base provides what children share).
- An ABC with exactly one subclass, or with only abstract methods and no
  shared code, is a Protocol wearing a costume — or premature abstraction.
  Start concrete; extract the Protocol when the second implementation (or
  the test fake) actually appears.
- `@runtime_checkable` Protocols check method *names* only at `isinstance`
  time — don't lean on it for validation.
- Framework base classes (SQLAlchemy's `DeclarativeBase`, pydantic's
  `BaseModel`) are framework mechanisms, not ABC territory — follow the
  framework's idiom rather than bolting an ABC on top.

## Combining them

Data validation and behavioral contracts are orthogonal, so they rarely
belong on the same object — and when a data model *does* need to satisfy
an interface, reach for a Protocol, not ABC inheritance:

```python
class UserProvider(Protocol):           # behavioral contract the consumer needs
    def fetch(self) -> list[User]: ...


class ApiUserProvider:                  # satisfies it structurally — no inheritance
    def fetch(self) -> list[User]:
        return [User.model_validate(r) for r in self._get()]
```

Don't write `class User(BaseModel, ABC)`: pydantic's `ModelMetaclass` and
`ABCMeta` clash, and conceptually you'd be welding a data shape to a
behavioral contract that wants to live on the *service*, not the record.
Keep the pydantic model as pure data; put the contract on a Protocol the
service implements.

## Enums and Literals

- `Literal["draft", "published"]` for a small closed set used in one or two
  signatures.
- `Enum`/`StrEnum` once the set is shared, iterated, or stored — gives a
  single home for the vocabulary and exhaustive `match` checking. (Watch
  rendering: `(str, Enum)` vs `StrEnum` differ in `str()` output — pick per
  serialization need.)
- Magic strings/ints sprinkled at call sites are never acceptable — that's
  what enums and named constants are for.

## Anti-patterns

Smells that mean you reached for the wrong shape:

- **Re-wrapping at every layer.** The same data passes through three
  models on its way in. Validate once at the boundary; pass the typed
  object inward unchanged.
- **`__post_init__` validating external data.** A dataclass policing its
  inputs wanted to be a pydantic model.
- **Hand-written `__init__` on a model or dataclass.** Use validators
  (normalize) and `@classmethod` factories (alternate construction); let
  the generated constructor stand.
- **`dict[str, str]` soup crossing a module boundary.** A typed shape is
  owed wherever data is consumed elsewhere.
- **ABC with one subclass, or only abstract methods.** That's a Protocol —
  or abstraction you don't need yet.
- **`class Model(BaseModel, ABC)`.** Split data (pydantic) from contract
  (Protocol on the service); see [Combining them](#combining-them).
- **pydantic `BaseModel` constructed per row in a hot loop.** Boundary
  validation paid twice; move it to dataclasses once inside.
- **Magic strings/ints at call sites.** Promote to `Enum`/`Literal`.

## See also

- Which library for a given job, and project tooling: [tooling.md](tooling.md).
- Full config/db/async code idioms: [patterns.md](patterns.md).