from __future__ import annotations

import asyncio

import litellm

from app.core.exceptions import LLMServiceError
from app.core.logging import get_logger
from app.core.services.llm import BaseLLMService
from app.core.settings import settings

logger = get_logger(__name__)

# Transient failures only — an explicit allowlist, never a broad `except Exception`.
#
# Two reasons the allowlist is load-bearing rather than fussy:
#
#   1. Celery's SoftTimeLimitExceeded derives from Exception. A broad retry would
#      swallow it and keep retrying past the deadline, defeating the mechanism
#      app/tasks/ingestion.py:72-76 relies on to still write
#      ingestion_status='failed' before the hard SIGKILL. Retrying the signal that
#      says "stop" is worse than not retrying at all.
#   2. Permanent failures must fail fast. A bad model name or a rejected request
#      will fail identically on every attempt, and Stage C issues ~472 sequential
#      calls per document — paying 3x on a doomed call, 472 times, turns a clear
#      error into a very slow one.
#
# Note litellm re-exports OpenAI's exception classes, so `litellm.APIError` is NOT
# a parent of `litellm.BadRequestError` (verified against litellm 1.92.0:
# issubclass(BadRequestError, APIError) is False). Catching a base class here would
# be both too broad and, confusingly, not broad enough — hence naming each one.
_RETRYABLE: tuple[type[BaseException], ...] = (
    litellm.Timeout,
    litellm.APIConnectionError,
    litellm.RateLimitError,
    litellm.InternalServerError,
    litellm.ServiceUnavailableError,
)


class LiteLLMService(BaseLLMService):
    """LiteLLM-backed completion with bounded retry on transient transport failures.

    Retry lives here, at the single point every LLM call funnels through (§4), for
    the same reason the timeout does: no caller can forget it, and no node has to
    grow its own policy. This closes finding D31.

    D31's arithmetic is why it matters. Stage C enrichment makes one sequential
    call per chunk — ~472 for the MVP corpus — and a single failure discards the
    whole document's enrichment. At a 99.5% per-call success rate the chance of a
    document completing is 0.995^472 = 0.094, i.e. **roughly one attempt in eleven
    succeeds**. With three attempts per call, per-call success becomes
    1 - 0.005^3, and the document completes with probability ~0.9999.

    The cost is near zero: at a 0.5% failure rate only ~2.4 of 472 calls retry at
    all, so the expected added wall-clock is a few seconds against a job measured
    in tens of minutes.

    Callers above this layer still see one failure per call and are unchanged —
    `app/ingestion/enrich.py` still raises EnrichmentError on an exception, and its
    contract's "do not retry" clause still holds *at that layer*. What changed is
    that the exception now means the transport genuinely gave up.
    """

    async def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
    ) -> str:
        # api_base is scoped to Ollama models: it is the local runtime's address,
        # and pinning it globally would hijack every other provider (OpenRouter,
        # etc.) into pointing at localhost. Non-Ollama models use LiteLLM's own
        # provider routing, which already knows their endpoints.
        call_kwargs: dict[str, object] = {
            # A call with no ceiling can wait forever on a stalled request.
            # Applied here, in the single place every LLM call funnels through (§4),
            # rather than per node — no caller can forget it.
            "timeout": settings.LLM_TIMEOUT_SECONDS,
        }
        if model.startswith("ollama/"):
            call_kwargs["api_base"] = settings.OLLAMA_BASE_URL

        attempts = settings.LLM_MAX_ATTEMPTS
        last_error: BaseException | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    **call_kwargs,
                )
                content = response.choices[0].message.content

                # A structurally valid response carrying no text is a failure the
                # caller cannot use: `complete` is annotated `-> str`, and a None
                # would propagate into parsing code as a confusing TypeError far
                # from its cause. This is a real observed mode, not defensive
                # padding — a thinking model can spend its whole budget on
                # reasoning tokens and return empty content (see .env.local's
                # minimax-m3 probe note). Treated as retryable: the next attempt
                # may well produce text.
                if content is None or not content.strip():
                    raise LLMServiceError(
                        f"{model} returned an empty completion "
                        f"(attempt {attempt}/{attempts})"
                    )
                return content

            except _RETRYABLE as exc:
                last_error = exc
                if attempt == attempts:
                    break
                delay = settings.LLM_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "llm_call_retrying",
                    model=model,
                    attempt=attempt,
                    attempts=attempts,
                    delay_seconds=delay,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                await asyncio.sleep(delay)

            except LLMServiceError as exc:
                last_error = exc
                if attempt == attempts:
                    break
                delay = settings.LLM_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "llm_empty_completion_retrying",
                    model=model,
                    attempt=attempt,
                    attempts=attempts,
                    delay_seconds=delay,
                )
                await asyncio.sleep(delay)

        # Exhausted. Re-raise the last error rather than wrapping it in a new type:
        # callers already classify failures by exception (enrich.py maps any
        # exception to EnrichmentError), and a new wrapper would hide whether the
        # transport timed out, was refused, or returned nothing.
        logger.error(
            "llm_call_failed",
            model=model,
            attempts=attempts,
            error_type=type(last_error).__name__ if last_error else None,
            error=str(last_error) if last_error else None,
        )
        assert last_error is not None  # unreachable: the loop only breaks after setting it
        raise last_error
