"""The local model provider: Ollama's native ``/api/chat`` with JSON-schema output.

Deliberately **not** the OpenAI-compatible endpoint. Ollama's own ``format`` field takes a
JSON Schema and constrains generation to it, which is the only reason a 8B model is
trustworthy enough to screen signals at all; the compatibility shim only offers
``json_object``, which produces valid JSON of the wrong shape.

What this provider does and does not claim:

* ``caps``: structured output yes, tools no, skills no. A task with a tool profile is
  therefore *skipped*, never downgraded — unless it declares ``local_mode:
  context_pack``, in which case the router hands it a pre-assembled pack and the **host**
  writes any output file (:mod:`runs.llm.context_packs`).
* One **schema repair retry**: if the output does not validate, the same prompt is sent
  once more with the validation error appended. A second failure is ``schema_invalid``,
  which ``switching.on`` routes as ``retry_then_next``.
* Tokens come from ``prompt_eval_count`` / ``eval_count``; ``cost_usd`` is ``0.0`` and
  ``auth_source`` is ``"local"``, so the usage views can separate free local work from
  metered API spend without special-casing.

The HTTP client is injectable (``httpx.MockTransport`` in the tests), so nothing here
needs a running daemon to be exercised.
"""

from __future__ import annotations

import json
import time
from typing import Any

from runs.decision_core import StageMeta, StageResult
from runs.llm.base import BaseProvider
from runs.llm.types import LLMError, LLMRequest, ProviderCaps, ProviderDown

__all__ = ["OllamaProvider"]

_SYSTEM_JSON = (
    "You answer with a single JSON document that validates against the supplied schema. "
    "No prose, no markdown fence, no explanation outside the JSON."
)


class OllamaProvider(BaseProvider):
    """A local Ollama endpoint. Structured output only; free; never leaves the machine."""

    key = "ollama"
    caps = ProviderCaps(structured_output=True, tools_readonly=False,
                        tools_write=False, skills=False)

    def __init__(
        self,
        base_url: str | None,
        *,
        client: Any | None = None,
        timeout_s: float = 120.0,
        keep_alive: str | None = "10m",
        options: dict[str, Any] | None = None,
        max_ctx: int | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/") or None
        self._client = client
        self.timeout_s = float(timeout_s)
        self.keep_alive = keep_alive
        self.options = dict(options or {})
        if max_ctx is not None:
            self.caps = ProviderCaps(structured_output=True, tools_readonly=False,
                                     tools_write=False, skills=False, max_ctx=max_ctx)

    # -- plumbing --------------------------------------------------------------

    def _http(self) -> tuple[Any, bool]:
        if self._client is not None:
            return self._client, False
        import httpx

        return httpx.Client(timeout=self.timeout_s), True

    def health(self) -> bool:
        if not self.base_url:
            return False
        from ops.lib import ollama as ollama_lib

        client, owned = self._http()
        try:
            return ollama_lib.probe_url(self.base_url, client=client).ok
        finally:
            if owned:
                client.close()

    # -- provider API ----------------------------------------------------------

    def run(self, req: LLMRequest) -> StageResult:
        self.ensure_can_serve(req)
        if not self.base_url:
            raise ProviderDown("no Ollama endpoint detected")
        started = time.monotonic()
        client, owned = self._http()
        try:
            text, usage = self._chat(client, req, req.prompt)
            if req.output_schema is not None:
                error = _schema_error(text, req.output_schema)
                if error is not None:
                    repaired = (
                        f"{req.prompt}\n\nYour previous answer did not validate against "
                        f"the required schema: {error}\nReturn corrected JSON only."
                    )
                    text, usage = self._chat(client, req, repaired)
                    error = _schema_error(text, req.output_schema)
                    if error is not None:
                        raise LLMError(f"schema validation failed twice: {error}",
                                       failure_class="schema_invalid")
        finally:
            if owned:
                client.close()
        meta = StageMeta(
            subtype="success",
            cost_usd=0.0,
            input_tokens=usage.get("prompt_eval_count"),
            output_tokens=usage.get("eval_count"),
            num_turns=1,
            served_model=req.model.model_id,
            applied_effort=req.effort,
            auth_source="local",
        )
        del started
        return StageResult(ok=True, text=text, meta=meta)

    def _chat(self, client: Any, req: LLMRequest,
              prompt: str) -> tuple[str, dict[str, Any]]:
        options = dict(self.options)
        if req.model.model_id and self.caps.max_ctx:
            options.setdefault("num_ctx", self.caps.max_ctx)
        payload: dict[str, Any] = {
            "model": req.model.model_id,
            "messages": (
                ([{"role": "system", "content": _SYSTEM_JSON}]
                 if req.output_schema is not None else [])
                + [{"role": "user", "content": prompt}]
            ),
            "stream": False,
            "options": options,
        }
        if self.keep_alive:
            payload["keep_alive"] = self.keep_alive
        if req.output_schema is not None:
            payload["format"] = req.output_schema
        timeout = min(self.timeout_s, float(req.deadline_s)) or self.timeout_s
        try:
            resp = client.post(f"{self.base_url}/api/chat", json=payload, timeout=timeout)
        except Exception as e:  # noqa: BLE001 — transport failure means the daemon is gone
            if type(e).__name__ in ("ReadTimeout", "ConnectTimeout", "TimeoutException",
                                    "PoolTimeout"):
                raise LLMError(f"ollama timed out after {timeout}s",
                               failure_class="timeout") from e
            raise ProviderDown(f"ollama unreachable at {self.base_url}: {e}") from e
        status = getattr(resp, "status_code", 0)
        if status != 200:
            body = _body(resp)
            if status in (404, 400):
                raise LLMError(f"ollama rejected the request (HTTP {status}): {body}",
                               failure_class="error")
            raise ProviderDown(f"ollama HTTP {status}: {body}")
        try:
            data = resp.json() or {}
        except ValueError as e:
            raise LLMError(f"ollama returned non-JSON: {_body(resp)[:200]}",
                           failure_class="error") from e
        content = ((data.get("message") or {}).get("content") or "").strip()
        if not content:
            raise LLMError("ollama returned an empty message",
                           failure_class="empty_output")
        return content, data


def _body(resp: Any) -> str:
    try:
        return str(getattr(resp, "text", ""))[:500]
    except Exception:  # noqa: BLE001  # pragma: no cover
        return ""


def _schema_error(text: str, schema: dict[str, Any]) -> str | None:
    """``None`` when ``text`` is JSON that validates; otherwise a short reason."""
    try:
        parsed = json.loads(text)
    except (ValueError, json.JSONDecodeError) as e:
        return f"not JSON ({e})"
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema is a hard dependency
        return None
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as e:
        path = "/".join(str(p) for p in e.absolute_path) or "<root>"
        return f"{path}: {e.message}"
    except jsonschema.SchemaError as e:  # pragma: no cover - our own schema is broken
        return f"invalid schema: {e.message}"
    return None
