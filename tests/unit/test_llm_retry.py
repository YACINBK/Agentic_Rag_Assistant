"""LiteLLMService retry — D31.

D31 is the arithmetic: Stage C enrichment makes ~472 sequential LLM calls per
document, one failure discards the whole document's enrichment, and there was no
retry at any layer. At 99.5% per-call success, 0.995^472 = 0.094 — roughly one
document in eleven would ever finish ingesting.

These tests pin the four properties that make the retry safe rather than merely
present: transient errors are retried, permanent ones are not, exhaustion
re-raises the original error, and Celery's soft time limit is never swallowed.

asyncio.sleep is patched throughout so the backoff is asserted rather than waited
on — a real 1s + 2s backoff would add seconds to the suite for no information.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import litellm
import pytest
from celery.exceptions import SoftTimeLimitExceeded

from app.core.exceptions import LLMServiceError
from app.services.llm import LiteLLMService

ACOMPLETION = "app.services.llm.litellm.acompletion"
SLEEP = "app.services.llm.asyncio.sleep"

MODEL = "ollama/qwen2.5:14b"
MESSAGES = [{"role": "user", "content": "bonjour"}]


def reply(text: str | None) -> SimpleNamespace:
    """The only shape the service reads: response.choices[0].message.content."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def transient(kind: str = "timeout") -> BaseException:
    common = {"message": "boom", "model": MODEL, "llm_provider": "ollama"}
    return {
        "timeout": lambda: litellm.Timeout(**common),
        "connection": lambda: litellm.APIConnectionError(**common),
        "rate_limit": lambda: litellm.RateLimitError(**common),
        "server": lambda: litellm.InternalServerError(**common),
        "unavailable": lambda: litellm.ServiceUnavailableError(**common),
    }[kind]()


def permanent() -> BaseException:
    return litellm.BadRequestError(message="nope", model=MODEL, llm_provider="ollama")


@pytest.fixture
def three_attempts(monkeypatch):
    from app.core.settings import settings

    monkeypatch.setattr(settings, "LLM_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "LLM_RETRY_BASE_DELAY_SECONDS", 1.0)
    return settings


pytestmark = pytest.mark.asyncio


class TestTransientRetry:
    @pytest.mark.parametrize(
        "kind", ["timeout", "connection", "rate_limit", "server", "unavailable"]
    )
    async def test_each_transient_error_is_retried_then_succeeds(self, three_attempts, kind):
        """Every member of the allowlist must actually be retried, not just the
        first one anyone thought of."""
        calls = AsyncMock(side_effect=[transient(kind), reply("ça marche")])
        with patch(ACOMPLETION, calls), patch(SLEEP, AsyncMock()):
            result = await LiteLLMService().complete(MODEL, MESSAGES)

        assert result == "ça marche"
        assert calls.await_count == 2

    async def test_exhausts_attempts_and_reraises_the_original_error(self, three_attempts):
        """On exhaustion the caller sees the transport's own exception, not a
        wrapper — enrich.py classifies by exception and a new type would hide
        whether the transport timed out, was refused, or returned nothing."""
        calls = AsyncMock(side_effect=[transient(), transient(), transient()])
        with patch(ACOMPLETION, calls), patch(SLEEP, AsyncMock()):
            with pytest.raises(litellm.Timeout):
                await LiteLLMService().complete(MODEL, MESSAGES)

        assert calls.await_count == 3

    async def test_backoff_is_exponential_and_skipped_after_the_last_attempt(
        self, three_attempts
    ):
        """1s then 2s, and no sleep after the final failure — waiting before
        giving up buys nothing and delays the 'failed' write."""
        calls = AsyncMock(side_effect=[transient(), transient(), transient()])
        sleeper = AsyncMock()
        with patch(ACOMPLETION, calls), patch(SLEEP, sleeper):
            with pytest.raises(litellm.Timeout):
                await LiteLLMService().complete(MODEL, MESSAGES)

        assert [c.args[0] for c in sleeper.await_args_list] == [1.0, 2.0]

    async def test_succeeds_without_sleeping_when_the_first_attempt_works(
        self, three_attempts
    ):
        calls = AsyncMock(return_value=reply("direct"))
        sleeper = AsyncMock()
        with patch(ACOMPLETION, calls), patch(SLEEP, sleeper):
            assert await LiteLLMService().complete(MODEL, MESSAGES) == "direct"

        assert calls.await_count == 1
        sleeper.assert_not_awaited()


class TestNonRetryable:
    async def test_permanent_error_fails_on_the_first_attempt(self, three_attempts):
        """A bad request fails identically every time. Stage C issues ~472 calls,
        so paying 3x on a doomed call turns a clear error into a very slow one."""
        calls = AsyncMock(side_effect=permanent())
        sleeper = AsyncMock()
        with patch(ACOMPLETION, calls), patch(SLEEP, sleeper):
            with pytest.raises(litellm.BadRequestError):
                await LiteLLMService().complete(MODEL, MESSAGES)

        assert calls.await_count == 1
        sleeper.assert_not_awaited()

    async def test_soft_time_limit_is_never_retried(self, three_attempts):
        """The trap that makes the allowlist load-bearing.

        SoftTimeLimitExceeded derives from Exception. A broad `except Exception`
        retry would swallow the one signal that lets the orchestrator still write
        ingestion_status='failed' before the hard SIGKILL
        (app/tasks/ingestion.py:72-76) — retrying the instruction to stop.
        """
        calls = AsyncMock(side_effect=SoftTimeLimitExceeded())
        sleeper = AsyncMock()
        with patch(ACOMPLETION, calls), patch(SLEEP, sleeper):
            with pytest.raises(SoftTimeLimitExceeded):
                await LiteLLMService().complete(MODEL, MESSAGES)

        assert calls.await_count == 1
        sleeper.assert_not_awaited()


class TestEmptyCompletion:
    @pytest.mark.parametrize("empty", [None, "", "   \n  "])
    async def test_empty_content_is_retried_then_succeeds(self, three_attempts, empty):
        """An observed mode, not defensive padding: a thinking model can spend its
        whole budget on reasoning tokens and return empty content (.env.local's
        minimax-m3 probe). complete() is annotated -> str, so a None would surface
        as a TypeError far from its cause."""
        calls = AsyncMock(side_effect=[reply(empty), reply("enfin")])
        with patch(ACOMPLETION, calls), patch(SLEEP, AsyncMock()):
            assert await LiteLLMService().complete(MODEL, MESSAGES) == "enfin"

        assert calls.await_count == 2

    async def test_all_empty_raises_llm_service_error(self, three_attempts):
        calls = AsyncMock(side_effect=[reply(None), reply(""), reply(None)])
        with patch(ACOMPLETION, calls), patch(SLEEP, AsyncMock()):
            with pytest.raises(LLMServiceError, match="empty completion"):
                await LiteLLMService().complete(MODEL, MESSAGES)

        assert calls.await_count == 3


class TestAttemptBudget:
    async def test_single_attempt_setting_disables_retry(self, monkeypatch):
        """LLM_MAX_ATTEMPTS=1 must restore exact pre-D31 behaviour — one call, no
        sleep — so the change is reversible by configuration alone."""
        from app.core.settings import settings

        monkeypatch.setattr(settings, "LLM_MAX_ATTEMPTS", 1)
        calls = AsyncMock(side_effect=transient())
        sleeper = AsyncMock()
        with patch(ACOMPLETION, calls), patch(SLEEP, sleeper):
            with pytest.raises(litellm.Timeout):
                await LiteLLMService().complete(MODEL, MESSAGES)

        assert calls.await_count == 1
        sleeper.assert_not_awaited()

    async def test_ollama_api_base_is_still_scoped_to_ollama_models(self, three_attempts):
        """Regression guard: the retry loop must not drop the api_base scoping that
        keeps non-Ollama providers from being pointed at localhost."""
        calls = AsyncMock(return_value=reply("ok"))
        with patch(ACOMPLETION, calls), patch(SLEEP, AsyncMock()):
            await LiteLLMService().complete("ollama/qwen2.5:7b", MESSAGES)
            await LiteLLMService().complete("openrouter/some-model", MESSAGES)

        assert "api_base" in calls.await_args_list[0].kwargs
        assert "api_base" not in calls.await_args_list[1].kwargs
