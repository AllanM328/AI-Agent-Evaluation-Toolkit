"""Model access behind one interface: Amazon Bedrock (Converse API), OpenRouter, or an offline mock.

All components that need a model go through `ModelClient.converse`, which takes and returns
Bedrock Converse-shaped data (`OpenRouterClient` translates to and from OpenAI chat format).
`MockClient` is deterministic and needs no credentials; every unit test and CI run uses it.
"""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, Field

MOCK_MODEL_ID = "mock-model"


class ToolUseRequest(BaseModel):
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    text: str | None = None
    tool_uses: list[ToolUseRequest] = Field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    model_id: str | None = None
    # Assistant message content exactly as the model returned it (Converse content blocks),
    # appended back into the conversation so multi-turn tool use stays faithful.
    raw_content: list[dict[str, Any]] = Field(default_factory=list)

    def assistant_content(self) -> list[dict[str, Any]]:
        if self.raw_content:
            return self.raw_content
        content: list[dict[str, Any]] = []
        if self.text:
            content.append({"text": self.text})
        for tu in self.tool_uses:
            content.append({"toolUse": {"toolUseId": tu.id, "name": tu.name, "input": tu.input}})
        return content


class ModelClient(ABC):
    model_id: str

    @abstractmethod
    def converse(
        self,
        system: str | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> ModelResponse:
        """Send a Converse-format conversation and return the model's next turn."""

    @property
    def is_mock(self) -> bool:
        return False


# --------------------------------------------------------------------------- Bedrock


class BedrockClient(ModelClient):
    """Amazon Bedrock Runtime Converse API client (supports tool use)."""

    def __init__(self, model_id: str, region: str | None = None, client: Any | None = None,
                 profile: str | None = None):
        if not model_id:
            raise ValueError("BedrockClient needs a model_id (set it in config.yaml or via env vars).")
        self.model_id = model_id
        self.region = region
        self.profile = profile
        if client is None:
            from botocore.config import Config as BotoConfig

            client = aws_session(profile).client(
                "bedrock-runtime",
                region_name=region,
                config=BotoConfig(retries={"max_attempts": 6, "mode": "adaptive"}, read_timeout=120),
            )
        self._client = client

    def converse(self, system, messages, tools=None, max_tokens=1024, temperature=0.0) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": messages,
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if system:
            kwargs["system"] = [{"text": system}]
        if tools:
            kwargs["toolConfig"] = {"tools": tools}
        start = time.perf_counter()
        resp = self._client.converse(**kwargs)
        measured_ms = int((time.perf_counter() - start) * 1000)
        return parse_converse_response(resp, self.model_id, measured_ms)


def parse_converse_response(resp: dict[str, Any], model_id: str, measured_ms: int = 0) -> ModelResponse:
    content = resp.get("output", {}).get("message", {}).get("content", []) or []
    texts = [b["text"] for b in content if "text" in b]
    tool_uses = [
        ToolUseRequest(id=b["toolUse"]["toolUseId"], name=b["toolUse"]["name"],
                       input=b["toolUse"].get("input") or {})
        for b in content if "toolUse" in b
    ]
    usage = resp.get("usage", {}) or {}
    metrics = resp.get("metrics", {}) or {}
    return ModelResponse(
        text="\n".join(texts) if texts else None,
        tool_uses=tool_uses,
        stop_reason=resp.get("stopReason", "end_turn"),
        input_tokens=int(usage.get("inputTokens", 0)),
        output_tokens=int(usage.get("outputTokens", 0)),
        latency_ms=int(metrics.get("latencyMs") or measured_ms),
        model_id=model_id,
        raw_content=content,
    )


def aws_session(profile: str | None = None) -> Any:
    """boto3 session; a named profile is passed explicitly so it beats any AWS_* key env vars."""
    import boto3

    return boto3.Session(profile_name=profile) if profile else boto3.Session()


def list_bedrock_models(region: str | None, profile: str | None = None) -> list[dict[str, str]]:
    """List text foundation models and inference profiles available in `region`."""
    bedrock = aws_session(profile).client("bedrock", region_name=region)
    rows: list[dict[str, str]] = []
    for m in bedrock.list_foundation_models(byOutputModality="TEXT").get("modelSummaries", []):
        rows.append({
            "id": m.get("modelId", ""),
            "kind": "foundation-model",
            "provider": m.get("providerName", ""),
            "name": m.get("modelName", ""),
            "inference": ",".join(m.get("inferenceTypesSupported", [])),
        })
    token: str | None = None
    while True:
        kwargs = {"nextToken": token} if token else {}
        page = bedrock.list_inference_profiles(**kwargs)
        for p in page.get("inferenceProfileSummaries", []):
            rows.append({
                "id": p.get("inferenceProfileId", ""),
                "kind": "inference-profile",
                "provider": "",
                "name": p.get("inferenceProfileName", ""),
                "inference": p.get("type", ""),
            })
        token = page.get("nextToken")
        if not token:
            break
    return rows


# --------------------------------------------------------------------------- OpenRouter

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"

# (method, url, headers, body or None) -> (HTTP status, parsed JSON body). Swappable so tests stay offline.
Transport = Callable[[str, str, dict[str, str], dict[str, Any] | None], tuple[int, dict[str, Any]]]


class OpenRouterError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"OpenRouter HTTP {status}: {message}")
        self.status = status


def urllib_transport(method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None,
                     timeout: float = 120) -> tuple[int, dict[str, Any]]:
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        return exc.code, payload


def _openrouter_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
            "X-Title": "AgentGauge"}


def _error_message(status: int, payload: dict[str, Any]) -> str:
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(err or payload or f"HTTP {status}")


def converse_to_openai_messages(system: str | None, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Converse-format conversation -> OpenAI chat messages (what OpenRouter accepts)."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for msg in messages:
        texts = [b["text"] for b in msg.get("content", []) if "text" in b]
        if msg["role"] == "assistant":
            tool_calls = [
                {"id": b["toolUse"]["toolUseId"], "type": "function",
                 "function": {"name": b["toolUse"]["name"], "arguments": json.dumps(b["toolUse"].get("input") or {})}}
                for b in msg.get("content", []) if "toolUse" in b
            ]
            m: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) if texts else None}
            if tool_calls:
                m["tool_calls"] = tool_calls
            out.append(m)
            continue
        # User turn: tool results become `tool` messages, which must directly follow the assistant tool_calls.
        for b in msg.get("content", []):
            if "toolResult" in b:
                tr = b["toolResult"]
                parts = [c["text"] if "text" in c else json.dumps(c.get("json"), default=str)
                         for c in tr.get("content", [])]
                out.append({"role": "tool", "tool_call_id": tr["toolUseId"], "content": "\n".join(parts)})
        if texts:
            out.append({"role": "user", "content": "\n".join(texts)})
    return out


def converse_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t["toolSpec"]["name"],
                                              "description": t["toolSpec"].get("description", ""),
                                              "parameters": t["toolSpec"]["inputSchema"]["json"]}}
            for t in tools]


_FINISH_TO_STOP = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens",
                   "content_filter": "content_filtered"}


def parse_openai_response(resp: dict[str, Any], model_id: str, measured_ms: int = 0) -> ModelResponse:
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or None
    tool_uses: list[ToolUseRequest] = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except ValueError:
            # Keep the malformed call visible: the sandbox rejects the unknown argument and the trace shows it.
            args = {"_unparseable_arguments": raw}
        tool_uses.append(ToolUseRequest(id=tc.get("id") or f"call_{len(tool_uses)}", name=fn.get("name", ""),
                                        input=args if isinstance(args, dict) else {"_arguments": args}))
    usage = resp.get("usage") or {}
    content: list[dict[str, Any]] = [{"text": text}] if text else []
    content += [{"toolUse": {"toolUseId": t.id, "name": t.name, "input": t.input}} for t in tool_uses]
    stop = _FINISH_TO_STOP.get(choice.get("finish_reason") or "", "end_turn")
    if tool_uses:
        stop = "tool_use"
    return ModelResponse(
        text=text, tool_uses=tool_uses, stop_reason=stop,
        input_tokens=int(usage.get("prompt_tokens") or 0), output_tokens=int(usage.get("completion_tokens") or 0),
        latency_ms=measured_ms, model_id=model_id, raw_content=content,
    )


class OpenRouterClient(ModelClient):
    """OpenRouter chat-completions client. Speaks Converse-shaped data to the rest of AgentGauge."""

    RETRY_STATUSES = (408, 429, 500, 502, 503, 504)

    def __init__(self, model_id: str, api_key: str | None = None, transport: Transport | None = None,
                 base_url: str = OPENROUTER_BASE_URL, max_attempts: int = 4, backoff_s: float = 2.0):
        if not model_id:
            raise ValueError("OpenRouterClient needs a model_id (set it in config.yaml or via env vars).")
        self.model_id = model_id
        self.api_key = api_key or os.environ.get(OPENROUTER_KEY_ENV)
        if not self.api_key:
            raise ValueError(f"{OPENROUTER_KEY_ENV} is not set.")
        self._transport = transport or urllib_transport
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s

    def converse(self, system, messages, tools=None, max_tokens=1024, temperature=0.0) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model_id,
            "messages": converse_to_openai_messages(system, messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = converse_tools_to_openai(tools)
        start = time.perf_counter()
        for attempt in range(1, self.max_attempts + 1):
            status, payload = self._transport("POST", f"{self.base_url}/chat/completions",
                                              _openrouter_headers(self.api_key), body)
            # OpenRouter can also report upstream failures inside a 200 response.
            if status == 200 and "error" not in payload:
                break
            status = payload.get("error", {}).get("code", status) if isinstance(payload.get("error"), dict) else status
            if not isinstance(status, int) or status not in self.RETRY_STATUSES or attempt == self.max_attempts:
                raise OpenRouterError(status if isinstance(status, int) else 0, _error_message(status, payload))
            time.sleep(self.backoff_s * 2 ** (attempt - 1))
        measured_ms = int((time.perf_counter() - start) * 1000)
        return parse_openai_response(payload, self.model_id, measured_ms)


def openrouter_key_info(api_key: str | None = None, transport: Transport | None = None) -> dict[str, Any]:
    """GET /key: validates the key (free, no tokens) and returns its usage/limit info."""
    key = api_key or os.environ.get(OPENROUTER_KEY_ENV)
    if not key:
        raise ValueError(f"{OPENROUTER_KEY_ENV} is not set")
    status, payload = (transport or urllib_transport)("GET", f"{OPENROUTER_BASE_URL}/key",
                                                      _openrouter_headers(key), None)
    if status != 200:
        raise OpenRouterError(status, _error_message(status, payload))
    return payload.get("data") or {}


def list_openrouter_models(transport: Transport | None = None) -> list[dict[str, str]]:
    """Public model catalog (no key needed), tool-capable models only, with USD per 1M tokens."""
    status, payload = (transport or urllib_transport)("GET", f"{OPENROUTER_BASE_URL}/models", {}, None)
    if status != 200:
        raise OpenRouterError(status, _error_message(status, payload))
    rows: list[dict[str, str]] = []
    for m in payload.get("data", []):
        if "tools" not in (m.get("supported_parameters") or []):
            continue
        p = m.get("pricing") or {}
        try:
            price = f"${float(p.get('prompt', 0)) * 1e6:g} / ${float(p.get('completion', 0)) * 1e6:g}"
        except (TypeError, ValueError):
            price = "?"
        rows.append({"id": m.get("id", ""), "kind": "chat (tools)", "provider": m.get("id", "").split("/")[0],
                     "name": m.get("name", ""), "inference": f"{price} per 1M in/out"})
    return rows


FATAL_ERROR_MARKERS = ("per day", "openrouter http 401", "openrouter http 402", "openrouter http 403")


def is_fatal_provider_error(message: str | None) -> bool:
    """Errors that will fail every remaining call this session (daily quota, bad key, no credits)."""
    msg = (message or "").lower()
    return any(m in msg for m in FATAL_ERROR_MARKERS)


# --------------------------------------------------------------------------- Mock


class PolicyOutput(BaseModel):
    text: str | None = None
    tool_uses: list[ToolUseRequest] = Field(default_factory=list)


Policy = Callable[[str | None, list[dict[str, Any]], list[dict[str, Any]] | None], PolicyOutput]


def approx_tokens(obj: Any) -> int:
    """Rough token count (~4 characters per token), used only by the mock."""
    text = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return max(1, len(text) // 4)


class MockClient(ModelClient):
    """Deterministic offline model.

    Either replays a fixed `script` of responses in order (unit tests), or asks a `policy`
    callable for the next turn (end-to-end mock runs). Token counts are approximated from the
    actual request/response size so cost estimates stay meaningful; latency is simulated
    deterministically (no sleeping).
    """

    def __init__(
        self,
        script: Sequence[ModelResponse | PolicyOutput | dict[str, Any]] | None = None,
        policy: Policy | None = None,
        model_id: str = MOCK_MODEL_ID,
    ):
        if script is None and policy is None:
            raise ValueError("MockClient needs a script or a policy")
        self.model_id = model_id
        self._script = list(script) if script is not None else None
        self._policy = policy
        self.calls: list[dict[str, Any]] = []  # every request, for assertions in tests

    @property
    def is_mock(self) -> bool:
        return True

    def converse(self, system, messages, tools=None, max_tokens=1024, temperature=0.0) -> ModelResponse:
        request = {"system": system, "messages": messages, "tools": tools,
                   "max_tokens": max_tokens, "temperature": temperature}
        self.calls.append(json.loads(json.dumps(request, default=str)))
        if self._script is not None:
            if not self._script:
                raise RuntimeError("MockClient script exhausted")
            item = self._script.pop(0)
            if isinstance(item, dict):
                item = ModelResponse.model_validate(item)
        else:
            assert self._policy is not None
            item = self._policy(system, messages, tools)

        if isinstance(item, ModelResponse):
            resp = item.model_copy()
        else:
            resp = ModelResponse(text=item.text, tool_uses=item.tool_uses,
                                 stop_reason="tool_use" if item.tool_uses else "end_turn")
        if not resp.input_tokens:
            resp.input_tokens = approx_tokens({"s": system, "m": messages, "t": tools})
        if not resp.output_tokens:
            resp.output_tokens = approx_tokens(resp.assistant_content())
        if not resp.latency_ms:
            resp.latency_ms = 150 + 6 * resp.output_tokens  # simulated, deterministic
        resp.model_id = self.model_id
        if resp.tool_uses and resp.stop_reason == "end_turn":
            resp.stop_reason = "tool_use"
        return resp


def make_client(model_id: str | None, *, mock: bool, region: str | None = None,
                policy: Policy | None = None, profile: str | None = None,
                provider: str = "bedrock") -> ModelClient:
    """Factory used by the runner and CLI."""
    if mock:
        if policy is None:
            raise ValueError("mock clients need a policy")
        return MockClient(policy=policy)
    if not model_id:
        raise ValueError(
            "No model ID configured. Set models.target_model_id / models.judge_model_id in config.yaml "
            "or AGENTGAUGE_TARGET_MODEL_ID / AGENTGAUGE_JUDGE_MODEL_ID (see `agentgauge list-models`), "
            "or pass --mock to run offline."
        )
    if provider == "openrouter":
        return OpenRouterClient(model_id=model_id)
    return BedrockClient(model_id=model_id, region=region, profile=profile)
