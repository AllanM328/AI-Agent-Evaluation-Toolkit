"""Full offline pipeline: mock runs of all 25 scenarios on v1 and v2, storage, compare, reports, replay."""

import json

import pytest

from agentgauge.adapters import load_traces
from agentgauge.db import Database
from agentgauge.report import compare_runs, render_compare_markdown, render_html, render_markdown
from agentgauge.runner import estimate_cost, run_agent_suite, run_replay
from agentgauge.scenarios import load_scenarios

from conftest import REPO_ROOT


@pytest.fixture
def runs(config):
    db = Database(config.db_path)
    v1 = run_agent_suite(config, "sample_v1", label="baseline", mock=True, db=db)
    v2 = run_agent_suite(config, "sample_v2", label="hardened", mock=True, db=db)
    return db, v1, v2


def test_scenario_library_shape(config):
    scenarios = load_scenarios(config.scenarios_path)
    cats = [s.category for s in scenarios]
    assert len(scenarios) == 25
    assert cats.count("functional") == 10 and cats.count("injection") == 10 and cats.count("efficiency") == 5
    assert sum(s.is_benign_control for s in scenarios) == 1
    for s in scenarios:
        assert s.rubric, s.id
        if s.category == "injection" and not s.is_benign_control:
            assert s.setup.canary and s.setup.canary in s.expected.must_not_contain, s.id


def test_mock_suite_runs_and_stores(runs):
    db, v1, v2 = runs
    for s in (v1, v2):
        assert s.status == "COMPLETED" and s.n_scenarios == 25 and s.mode == "mock"
        assert s.reliability_score is not None and set(s.components) == {"correctness", "safety", "efficiency"}
        assert s.total_cost_usd is not None and s.total_cost_usd > 0
        assert s.injection_attacks == 9 and s.benign_control_passed is not None
        assert any("MOCK RUN" in n for n in s.notes)
        results = db.get_results(s.run_id)
        assert len(results) == 25
        for r in results:
            assert {e.name for e in r.evals} >= {"tool_calls", "scope", "injection", "redundancy", "efficiency",
                                                 "judge", "groundedness"}
            assert r.trace.status in ("OK", "STEP_LIMIT", "ABORTED_BUDGET", "ERROR")
    assert [r.run_id for r in db.list_runs()][:2] == [v2.run_id, v1.run_id]
    ledger = db.get_ledger(v1.run_id)
    assert ledger and {e["purpose"] for e in ledger} == {"agent", "judge"}
    # every agent cost is traceable to the ledger
    by_scn = {}
    for e in ledger:
        if e["purpose"] == "agent":
            by_scn[e["scenario_id"]] = by_scn.get(e["scenario_id"], 0) + e["cost_usd"]
    for r in db.get_results(v1.run_id):
        assert r.trace.total_cost_usd == pytest.approx(by_scn[r.scenario_id])


def test_v2_guardrail_does_not_reduce_injection_resistance_in_mock(runs):
    """With the gullible mock model, v2's code-level validator blocks tool-based attacks."""
    db, v1, v2 = runs
    assert v2.injection_resistance_rate >= v1.injection_resistance_rate
    blocked = [c for r in db.get_results(v2.run_id) for _, c in r.trace.blocked_tool_calls()]
    assert blocked, "expected the v2 validator to block at least one injected call"


def test_compare_and_reports(runs, tmp_path):
    db, v1, v2 = runs
    c = compare_runs(v1, db.get_results(v1.run_id), v2, db.get_results(v2.run_id))
    assert len(c.scenarios) == 25
    assert {m.key for m in c.metrics} >= {"reliability_score", "injection_resistance_rate"}
    md = render_compare_markdown(c)
    assert "Flipped fail → pass" in md
    report_md = render_markdown(v1, db.get_results(v1.run_id))
    assert "MOCK RUN" in report_md and "inj_doc_email_001" in report_md
    html = render_html(v2, db.get_results(v2.run_id))
    assert html.startswith("<!doctype html>") and "<script" not in html


def test_run_resolution_by_label_and_prefix(runs):
    db, v1, v2 = runs
    assert db.resolve_run_id("baseline") == v1.run_id
    assert db.resolve_run_id("latest") == v2.run_id
    with pytest.raises(KeyError):
        db.resolve_run_id("nope")


def test_session_budget_stops_suite(config):
    config.budget.max_usd_per_session = 0.003
    db = Database(config.db_path)
    s = run_agent_suite(config, "sample_v1", mock=True, db=db, use_judge=False)
    assert s.status == "ABORTED_BUDGET" and s.n_skipped > 0
    assert s.n_scenarios + s.n_skipped == 25


def test_estimate_cost_uses_live_prices(config, tmp_path):
    pricing = tmp_path / "pricing.yaml"
    pricing.write_text("models:\n  live-target:\n    input_per_1k: 0.001\n    output_per_1k: 0.004\n"
                       "  live-judge:\n    input_per_1k: 0.003\n    output_per_1k: 0.015\n")
    config.paths.pricing = str(pricing)
    config.models.target_model_id, config.models.judge_model_id = "live-target", "live-judge"
    scenarios = load_scenarios(config.scenarios_path)
    est = estimate_cost(config, "sample_v1", scenarios)
    assert est.expected_usd and est.expected_usd > 0 and est.within_session_cap
    assert est.agent_output_tokens >= 150 * est.agent_calls
    config.models.judge_model_id = "unpriced-judge"
    est2 = estimate_cost(config, "sample_v1", scenarios)
    assert est2.expected_usd is None and any("no price" in w for w in est2.warnings)


def test_replay_example_trace(config):
    traces = load_traces(REPO_ROOT / "examples" / "traces" / "example_trace.json")
    db = Database(config.db_path)
    s = run_replay(config, traces, mock=True, db=db, label="byo")
    assert s.mode == "replay" and s.n_scenarios == len(traces)
    results = {r.scenario_id: r for r in db.get_results(s.run_id)}
    compromised = results["inj_doc_email_001"]
    assert compromised.compromised is True
    assert "COMPROMISED" in compromised.get_eval("injection").evidence


def test_replay_adhoc_trace(config, tmp_path):
    t = {"trace_id": "x1", "agent_name": "my_agent", "scenario_id": "custom_001", "user_prompt": "hello",
         "steps": [{"index": 0, "role": "user", "text": "hello"},
                   {"index": 1, "role": "assistant", "text": "Hi! How can I help?"}],
         "final_answer": "Hi! How can I help?", "status": "OK"}
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(t) + "\n")
    s = run_replay(config, load_traces(p), mock=True, db=Database(config.db_path))
    assert s.n_scenarios == 1 and any("generic expectations" in n for n in s.notes)
