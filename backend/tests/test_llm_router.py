import pytest

from app.llm.base import LLMProviderError
from app.llm.router import AllProvidersFailedError, LLMRouter


class FakeProvider:
    def __init__(self, response: str | None = None, error: str | None = None):
        self.response = response
        self.error = error
        self.calls = 0

    def complete_json(self, system: str, user: str) -> str:
        self.calls += 1
        if self.error:
            raise LLMProviderError(self.error)
        return self.response


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_uses_first_provider_when_it_succeeds():
    primary = FakeProvider(response="ok")
    backup = FakeProvider(response="unused")
    router = LLMRouter([("primary", primary), ("backup", backup)])

    result = router.complete_json("sys", "user")

    assert result == "ok"
    assert backup.calls == 0


def test_falls_back_to_next_provider_on_failure():
    primary = FakeProvider(error="rate limited")
    backup = FakeProvider(response="from backup")
    router = LLMRouter([("primary", primary), ("backup", backup)])

    result = router.complete_json("sys", "user")

    assert result == "from backup"


def test_raises_when_all_providers_fail():
    primary = FakeProvider(error="down")
    backup = FakeProvider(error="also down")
    router = LLMRouter([("primary", primary), ("backup", backup)])

    with pytest.raises(AllProvidersFailedError):
        router.complete_json("sys", "user")


def test_failed_provider_is_skipped_during_cooldown():
    """A provider that just failed shouldn't be retried on every request -
    it should cool down for cooldown_seconds before being tried again."""
    clock = FakeClock()
    primary = FakeProvider(error="down")
    backup = FakeProvider(response="from backup")
    router = LLMRouter([("primary", primary), ("backup", backup)], cooldown_seconds=60, clock=clock)

    router.complete_json("sys", "user")
    assert primary.calls == 1

    clock.advance(30)
    router.complete_json("sys", "user")
    assert primary.calls == 1, "still cooling down - should not have been retried"


def test_provider_is_retried_after_cooldown_expires():
    clock = FakeClock()
    primary = FakeProvider(error="down")
    backup = FakeProvider(response="from backup")
    router = LLMRouter([("primary", primary), ("backup", backup)], cooldown_seconds=60, clock=clock)

    router.complete_json("sys", "user")
    assert primary.calls == 1

    clock.advance(61)
    router.complete_json("sys", "user")
    assert primary.calls == 2, "cooldown expired - should have been retried"


def test_provider_recovers_after_succeeding_again():
    clock = FakeClock()
    primary = FakeProvider(error="down")
    backup = FakeProvider(response="from backup")
    router = LLMRouter([("primary", primary), ("backup", backup)], cooldown_seconds=60, clock=clock)

    router.complete_json("sys", "user")  # primary fails, falls back
    clock.advance(61)
    primary.error = None
    primary.response = "primary is back"

    result = router.complete_json("sys", "user")

    assert result == "primary is back"
