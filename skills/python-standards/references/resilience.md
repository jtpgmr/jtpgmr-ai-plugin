# Resilience — retries, backoff, rate limits, degradation

Rule zero: **only retry what is safe to repeat.** GET/HEAD and true
upserts (deterministic natural key) are retryable; a plain POST that
creates records is not — retrying it duplicates data.

## Retryable vs not — decide per failure, not per call

| Failure                 | Retry?                | Notes                                                      |
| ----------------------- | --------------------- | ---------------------------------------------------------- |
| 429 Too Many Requests   | yes                   | honor `Retry-After`; this is the API telling you the wait  |
| 500 / 502 / 503 / 504   | yes (idempotent only) | exponential backoff + jitter                               |
| Connect/read timeout    | yes (idempotent only) | the request may have landed — only safe for idempotent ops |
| 401 once after refresh  | yes, once             | refresh token under `asyncio.Lock`, retry the call once    |
| 400 / 422 validation    | **no**                | your payload is wrong; surface the response body and fix   |
| 403 / 404               | **no**                | auth/config or data problem — retrying changes nothing     |
| `ValidationError`, bugs | **no**                | code path, not transport                                   |

## Rate limits

- Respect `Retry-After` (seconds or HTTP-date) before any computed backoff.
- Pre-emptively bound your own rate: the fan-out `Semaphore` is also your
  rate limiter — size it to the API's documented concurrency, not just
  your DB pool.
- For per-second quotas add a token-bucket (`aiolimiter.AsyncLimiter`)
  around the client rather than sleeping in business logic.

## Auth refresh — exactly once

```python
class ApiClient:
    def __init__(self) -> None:
        self._auth_lock = asyncio.Lock()

    async def _ensure_token(self) -> str:
        async with self._auth_lock:               # one refresher; others wait and reuse
            if self._token is None or self._expires_soon(buffer_s=300):
                self._token = await self._refresh()
                self._save(self._token)           # persist via token handler
            return self._token
```

On 401: refresh once, retry once, then fail. A 401 loop means the
credential is dead — retrying hides a config problem.

## Circuit-breaker-lite for batch jobs

A 5,000-row run against a dead API should fail in seconds, not retry
5,000 × 4 times. Track consecutive failures and trip:

```python
class Breaker:
    def __init__(self, threshold: int = 10) -> None:
        self._consecutive = 0
        self.threshold = threshold

    def success(self) -> None:
        self._consecutive = 0

    def failure(self) -> None:
        self._consecutive += 1
        if self._consecutive >= self.threshold:
            raise UpstreamUnavailable(
                f"{self._consecutive} consecutive failures — aborting batch; "
                "fix upstream and rerun (idempotent)."
            )
```

Call `breaker.failure()` in the per-item except, `breaker.success()` after
each good item. Tripping is safe *because* the pipeline is idempotent and
checkpointed — rerunning resumes where it left off.

## Graceful degradation

When a **non-critical** source is down, degrade explicitly, never
silently:

- Decide per source at design time: is it required (abort) or enriching
  (skip)? Write the decision in the module docstring.
- Skipping records a marker the consumer can see: a `sources_skipped`
  field in the result / summary, a WARNING log with counts — not a quiet
  `None`.
- Optional subsystems gate on `.configured` (see patterns.md) so "not set
  up" and "set up but failing" are distinguishable states.

## Timeout budget

Every external call has a deadline; nest them so the outer bound always
wins: per-request `httpx.Timeout` < per-item `asyncio.timeout(...)` <
job-level expectation. A task with no timeout is a hang waiting for a
quiet Friday deploy.
