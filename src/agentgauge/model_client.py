"""Model access behind one interface: Amazon Bedrock (Converse API) or an offline mock.

All components that need a model go through `ModelClient.converse`, which takes and returns
Bedrock Converse-shaped data. `MockClient` is deterministic and needs no AWS credentials;
every unit test and CI run uses it.
"""

from __future__ import annotations

import json
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

    def __init__(self, model_id: str, region: str | None = None, client: Any | None = None):
        if not model_id:
            raise ValueError("BedrockClient needs a model_id (set it in config.yaml or via env vars).")
        self.model_id = model_id
        self.region = region
        if client is None:
            import boto3
            from botocore.config import Config as BotoConfig

            client = boto3.client(
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


def list_bedrock_models(region: str | None) -> list[dict[str, str]]:
    """List text foundation models and inference profiles available in `region`."""
    import boto3

    bedrock = boto3.client("bedrock", region_name=region)
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
                policy: Policy | None = None) -> ModelClient:
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
    return BedrockClient(model_id=model_id, region=region)
