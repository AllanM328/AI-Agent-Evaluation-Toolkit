"""OpenRouterClient request/response mapping and CLI gating, using a fake transport instead of the network."""

import json

import pytest
from typer.testing import CliRunner

from agentgauge.adapters import SampleAgent
from agentgauge.cli import app
from agentgauge.cost import CostTracker, Pricing
from agentgauge.model_client import OpenRouterClient, OpenRouterError, is_fatal_provider_error, make_client
from agentgauge.sandbox import Sandbox

from conftest import REPO_ROOT, scenario


class FakeTransport:
    """Replays (status, payload) pairs and records every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, method, url, headers, body):
        self.requests.append({"method": method, "url": url, "headers": headers,
                              "body": json.loads(json.dumps(body)) if body is not None else None})
        return self.replies.pop(0)


def chat(content=None, tool_calls=None, finish="stop", usage=(100, 20)):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return 200, {"choices": [{"message": msg, "finish_reason": finish}],
                 "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}


def tool_call(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def client(transport, **kw):
    return OpenRouterClient("vendor/model-x", api_key="test-key", transport=transport, backoff_s=0, **kw)


def test_request_shape_and_tool_call_parsing():
    t = FakeTransport(chat("Let me check.", [tool_call("call_1", "lookup_order", {"order_id": "1042"})],
                           finish="tool_calls", usage=(321, 45)))
    resp = client(t).converse("system text", [{"role": "user", "content": [{"text": "hi"}]}],
                              Sandbox.converse_tools(), max_tokens=256, temperature=0.0)
    req = t.requests[0]
    assert req["url"].endswith("/chat/completions")
    assert req["headers"]["Authorization"] == "Bearer test-key"
    body = req["body"]
    assert body["model"] == "vendor/model-x" and body["max_tokens"] == 256 and body["temperature"] == 0.0
    assert body["messages"] == [{"role": "system", "content": "system text"}, {"role": "user", "content": "hi"}]
    fn = body["tools"][0]["function"]
    assert body["tools"][0]["type"] == "function" and fn["parameters"]["type"] == "object"
    assert resp.text == "Let me check." and resp.stop_reason == "tool_use"
    assert resp.tool_uses[0].name == "lookup_order" and resp.tool_uses[0].input == {"order_id": "1042"}
    assert (resp.input_tokens, resp.output_tokens) == (321, 45)
    assert resp.assistant_content() == [
        {"text": "Let me check."},
        {"toolUse": {"toolUseId": "call_1", "name": "lookup_order", "input": {"order_id": "1042"}}},
    ]


def test_multi_turn_tool_results_become_tool_messages():
    t = FakeTransport(chat("It shipped."))
    messages = [
        {"role": "user", "content": [{"text": "where is 1042?"}]},
        {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "call_1", "name": "lookup_order", "input": {"order_id": "1042"}}}]},
        {"role": "user", "content": [
            {"toolResult": {"toolUseId": "call_1", "content": [{"text": '{"status": "shipped"}'}]}}]},
    ]
    client(t).converse(None, messages)
    sent = t.requests[0]["body"]["messages"]
    call = {"id": "call_1", "type": "function",
            "function": {"name": "lookup_order", "arguments": '{"order_id": "1042"}'}}
    assert sent[1] == {"role": "assistant", "content": None, "tool_calls": [call]}
    assert sent[2] == {"role": "tool", "tool_call_id": "call_1", "content": '{"status": "shipped"}'}
    assert "tools" not in t.requests[0]["body"]


def test_malformed_tool_arguments_stay_visible():
    bad = {"id": "c1", "type": "function", "function": {"name": "lookup_order", "arguments": "{oops"}}
    resp = client(FakeTransport(chat(None, [bad], finish="tool_calls"))).converse(None, [])
    assert resp.tool_uses[0].input == {"_unparseable_arguments": "{oops"}


def test_retries_transient_errors_then_succeeds():
    t = FakeTransport((429, {"error": {"code": 429, "message": "rate limited"}}),
                      (200, {"error": {"code": 502, "message": "upstream down"}}),
                      chat("ok"))
    assert client(t).converse(None, []).text == "ok"
    assert len(t.requests) == 3


@pytest.mark.parametrize("status,fatal", [(402, True), (401, True), (400, False)])
def test_client_errors_raise_without_retry(status, fatal):
    t = FakeTransport((status, {"error": {"code": status, "message": "nope"}}))
    with pytest.raises(OpenRouterError) as exc:
        client(t).converse(None, [])
    assert len(t.requests) == 1 and exc.value.status == status
    assert is_fatal_provider_error(str(exc.value)) is fatal


def test_key_comes_from_env_only(monkeypatch):
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        make_client("vendor/model-x", mock=False, provider="openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    assert make_client("vendor/model-x", mock=False, provider="openrouter").api_key == "env-key"


def test_sample_agent_runs_a_tool_loop_over_openrouter(config):
    t = FakeTransport(chat(None, [tool_call("call_1", "lookup_order", {"order_id": "1042"})], finish="tool_calls"),
                      chat("Your order 1042 has shipped."))
    agent = SampleAgent(client(t), version="v1", max_steps=4, max_tokens=256, temperature=0.0)
    sc = scenario(user_prompt="Where is order 1042?")
    trace = agent.run(sc, Sandbox(sc.setup), CostTracker(Pricing.load(config.pricing_path), config.budget))
    assert trace.status == "OK" and trace.final_answer == "Your order 1042 has shipped."
    assert [c.name for s in trace.model_steps() for c in s.tool_calls] == ["lookup_order"]
    assert t.requests[1]["body"]["messages"][-1]["role"] == "tool"


# --------------------------------------------------------------------------- CLI

runner = CliRunner()


@pytest.fixture
def or_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("COLUMNS", "250")
    monkeypatch.setenv("AGENTGAUGE_PROVIDER", "openrouter")
    monkeypatch.setenv("AGENTGAUGE_TARGET_MODEL_ID", "openai/gpt-4.1-mini")
    monkeypatch.setenv("AGENTGAUGE_JUDGE_MODEL_ID", "anthropic/claude-haiku-5.5")
    return ["--db", str(tmp_path / "cli.db")]


def test_live_run_needs_openrouter_key(or_cli):
    r = runner.invoke(app, ["run", "--scenario", "fn_order_status_001", *or_cli])
    assert r.exit_code == 1 and "OPENROUTER_API_KEY is not set" in r.output


def test_live_run_checks_key_and_asks_before_spending(or_cli, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr("agentgauge.model_client.openrouter_key_info",
                        lambda: {"usage": 0.5, "limit_remaining": 9.5})

    def no_calls(*a, **k):
        raise AssertionError("no model call may happen before confirmation")

    monkeypatch.setattr("agentgauge.model_client.urllib_transport", no_calls)
    r = runner.invoke(app, ["run", "--scenario", "fn_order_status_001", *or_cli], input="n\n")
    assert r.exit_code == 1
    assert "OpenRouter key OK" in r.output and "$9.5000" in r.output
    assert "Cost estimate" in r.output and "live on OpenRouter?" in r.output


def test_list_models_openrouter(or_cli, monkeypatch):
    monkeypatch.setattr("agentgauge.model_client.list_openrouter_models",
                        lambda: [{"id": "openai/gpt-4.1-mini", "kind": "chat (tools)", "provider": "openai",
                                  "name": "GPT-4.1 Mini", "inference": "$0.4 / $1.6 per 1M in/out"}])
    r = runner.invoke(app, ["list-models"])
    assert r.exit_code == 0 and "openai/gpt-4.1-mini" in r.output and "pricing.yaml" in r.output
