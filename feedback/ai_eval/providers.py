"""Model providers behind one small interface.

Only Gemini is wired up. The local-model provider is an interface placeholder:
Ollama is installed on the development machine but not running and has no
models downloaded (see docs/ai-eval.md), so it refuses to run instead of
silently producing nothing.
"""

import time
from typing import NamedTuple

from django.conf import settings

from ..ai_report_service import _wait_for_request_slot, create_gemini_client, generation_options
from ..ai_stage_service import _finish_reason, _response_payload, _usage_metrics


class ProviderResult(NamedTuple):
    payload: object
    prompt_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    finish_reason: str | None


class ProviderUnavailable(Exception):
    pass


class GeminiProvider:
    name = "gemini"

    def __init__(self, model=None):
        self.model = model or settings.GEMINI_MODEL
        self._client = None

    def generate(self, *, system, schema, contents):
        from google.genai import types

        if self._client is None:
            self._client = create_gemini_client()
        _wait_for_request_slot()
        started = time.perf_counter()
        response = self._client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                **generation_options("standard"),
                response_mime_type="application/json",
                response_schema=schema,
                http_options=types.HttpOptions(
                    timeout=settings.GEMINI_TIMEOUT_SECONDS * 1000,
                    retry_options=types.HttpRetryOptions(attempts=1, http_status_codes=[429]),
                ),
            ),
        )
        latency_ms = round((time.perf_counter() - started) * 1000)
        usage = _usage_metrics(response)
        return ProviderResult(
            payload=_response_payload(response),
            prompt_tokens=usage["prompt_token_count"],
            output_tokens=usage["candidates_token_count"],
            latency_ms=latency_ms,
            finish_reason=_finish_reason(response),
        )


class ReplayProvider:
    """Returns a saved model output, so scoring changes can be re-evaluated without new API calls."""

    name = "replay"

    def __init__(self, record):
        self.model = record.get("model", "replay")
        self._record = record

    def generate(self, *, system, schema, contents):
        record = self._record
        return ProviderResult(
            payload=record["raw_payload"],
            prompt_tokens=record.get("prompt_tokens"),
            output_tokens=record.get("output_tokens"),
            latency_ms=record.get("latency_ms", 0),
            finish_reason=record.get("finish_reason"),
        )


class OllamaProvider:
    """Interface for a local model served by Ollama (OpenAI-compatible API on port 11434).

    Not enabled yet: implement `generate` with Ollama's structured-output `format`
    parameter once a model is downloaded and the service is running.
    """

    name = "ollama"

    def __init__(self, model, base_url="http://127.0.0.1:11434"):
        self.model = model
        self.base_url = base_url

    def generate(self, *, system, schema, contents):
        raise ProviderUnavailable("本機模型尚未啟用：Ollama 已安裝但未啟動、沒有下載模型（見 docs/ai-eval.md）。")
