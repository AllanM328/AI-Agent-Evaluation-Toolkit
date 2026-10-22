"""Dashboard smoke tests with Streamlit's AppTest (headless, offline)."""

from pathlib import Path

import pytest

from agentgauge.dashboard.data import (
    category_frame,
    evaluator_frame,
    flagged_calls,
    pick_spotlight,
    poison_snippets,
    resolve_runs,
    scenarios_frame,
    steps_frame,
)
from agentgauge.db import Database
from agentgauge.runner import run_agent_suite

APP = Path(__file__).resolve().parent.parent / "src" / "agentgauge" / "dashboard" / "app.py"
PAGES = ["Headline", "Overview", "Run detail", "Compare runs", "Cost & efficiency", "Evaluator trust"]


@pytest.fixture
def populated(config, monkeypatch):
    db = Database(config.db_path)
    run_agent_suite(config, "sample_v1", label="baseline", mock=True, db=db)
    run_agent_suite(config, "sample_v2", label="hardened", mock=True, db=db)
    monkeypatch.setenv("AGENTGAUGE_DB", str(config.db_path))
    return db


def test_data_frames(populated):
    run = populated.list_runs()[0]
    results = populated.get_results(run.run_id)
    assert len(scenarios_frame(results)) == 25
    assert set(category_frame(results)["result"]) <= {"PASS", "FAIL"}
    assert {"tool_calls", "injection", "judge"} <= set(evaluator_frame(results)["evaluator"])
    assert not steps_frame(results).empty


def test_every_page_renders_without_exceptions(populated):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    assert any("MOCK RUN" in w.value for w in at.warning)
    for page in PAGES[1:]:
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, (page, at.exception)
        assert at.title[0].value == page


def test_empty_database_shows_hint(config, monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("AGENTGAUGE_DB", str(tmp_path / "empty.db"))
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception
    assert any("No runs yet" in i.value for i in at.info)


def test_headline_helpers(populated):
    runs = populated.list_runs()
    a, b = resolve_runs(runs, ["baseline", "hardened"])
    assert (a.label, b.label) == ("baseline", "hardened")
    assert resolve_runs(runs, ["nope", b.run_id]) == [b]
    a_res, b_res = populated.get_results(a.run_id), populated.get_results(b.run_id)
    assert pick_spotlight(a_res, b_res, "fn_order_status_001") == "fn_order_status_001"
    spot = pick_spotlight(a_res, b_res)
    assert next(r for r in a_res if r.scenario_id == spot).category == "injection"
    assert poison_snippets({"notes": "do X ZX-1", "items": ["a", {"n": "ZX-1 again"}]}, "ZX-1") == [
        "do X ZX-1", "ZX-1 again"]
    assert poison_snippets({"notes": "x"}, None) == []
    hijacked = next((r for r in a_res if r.compromised), None)
    if hijacked is not None:
        cited = flagged_calls(hijacked)
        executed = {(i, c.name) for i, c in hijacked.trace.executed_tool_calls()}
        assert cited and cited <= executed


def test_demo_mode_limits_runs_and_opens_trace(populated, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("AGENTGAUGE_DEMO_RUNS", "baseline,hardened")
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Headline"
    assert any("Demo mode" in c.value for c in at.sidebar.caption)
    assert not any("Database:" in c.value for c in at.sidebar.caption)
    assert len(at.selectbox(key="head_a").options) == 2
    sid = at.selectbox(key="head_scenario").value
    at.button(key="open_a").click().run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Run detail"
    assert at.selectbox(key="detail_scenario").value == sid
