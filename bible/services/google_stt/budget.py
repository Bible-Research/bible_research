"""Tracks remaining monthly Cloud STT seconds for a single run.

Clone of ``google_tts.budget.CharBudget`` measured in billed
audio seconds: STT V1 bills every successfully processed request
in 1-second increments, so the budget unit is the chapter's
audio duration (rounded up), not a request count.
"""


class BudgetExceeded(Exception):
    """Raised when the next chapter would exceed the remaining budget."""


class SecondsBudget:
    """Monotonically-decreasing in-memory budget.

    The Cloud Run Job constructs this as
    ``SecondsBudget(remaining=settings.MONTHLY_STT_SECONDS_LIMIT -
    already_used)`` after reading the persisted monthly usage from
    GCS. The counter is global (STT bills per account), unlike the
    per-fileset TTS char counters."""

    def __init__(self, remaining: int):
        if remaining < 0:
            remaining = 0
        self._initial = remaining
        self._remaining = remaining

    @property
    def remaining(self) -> int:
        return self._remaining

    @property
    def used(self) -> int:
        return self._initial - self._remaining

    def can_afford(self, n: int) -> bool:
        return n <= self._remaining

    def consume(self, n: int) -> None:
        if n > self._remaining:
            raise BudgetExceeded(
                f"requested={n} remaining={self._remaining}"
            )
        self._remaining -= n
