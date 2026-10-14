"""BedrockClient request/response mapping, using a stub instead of AWS."""

from agentgauge.model_client import BedrockClient, MockClient, ModelResponse
from agentgauge.sandbox import Sandbox


class StubRuntime:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def converse(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def test_converse_request_and_tool_use_parsing():
    stub = StubRuntime({
        "output": {"message": {"role": "assistant", "content": [
            {"text": "Let me check."},
            {"toolUse": {"toolUseId": "tu_1", "name": "lookup_order", "input": {"order_id": "1042"}}},
        ]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 321, "outputTokens": 45, "totalTokens": 366},
        "metrics": {"latencyMs": 812},
    })
    client = BedrockClient("example.model-v1:0", region="us-east-1", client=stub)
    tools = Sandbox.converse_tools()
    resp = client.converse("system text", [{"role": "user", "content": [{"text": "hi"}]}], tools,
                           max_tokens=256, temperature=0.0)
    assert stub.kwargs["modelId"] == "example.model-v1:0"
    assert stub.kwargs["system"] == [{"text": "system text"}]
    assert stub.kwargs["inferenceConfig"] == {"maxTokens": 256, "temperature": 0.0}
    assert stub.kwargs["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]["type"] == "object"
    assert resp.text == "Let me check." and resp.stop_reason == "tool_use"
    assert resp.tool_uses[0].name == "lookup_order" and resp.tool_uses[0].input == {"order_id": "1042"}
    assert (resp.input_tokens, resp.output_tokens, resp.latency_ms) == (321, 45, 812)
    assert resp.assistant_content() == stub.response["output"]["message"]["content"]


def test_no_tool_config_when_no_tools():
    stub = StubRuntime({"output": {"message": {"content": [{"text": "{}"}]}}, "usage": {}})
    BedrockClient("m", client=stub).converse(None, [{"role": "user", "content": [{"text": "x"}]}])
    assert "toolConfig" not in stub.kwargs and "system" not in stub.kwargs


def test_mock_client_fills_usage_deterministically():
    a = MockClient(script=[ModelResponse(text="hello world")])
    b = MockClient(script=[ModelResponse(text="hello world")])
    msgs = [{"role": "user", "content": [{"text": "same input"}]}]
    ra, rb = a.converse("s", msgs), b.converse("s", msgs)
    assert ra.input_tokens == rb.input_tokens > 0 and ra.latency_ms == rb.latency_ms > 0
