"""Dashboard smoke tests with Streamlit's AppTest (headless, offline)."""

from pathlib import Path

import pytest

from agentgauge.dashboard.data import category_frame, evaluator_frame, scenarios_frame, steps_frame
from agentgauge.db import Database
from agentgauge.runner import run_agent_suite

APP = Path(__file__).resolve().parent.parent / "src" / "agentgauge" / "dashboard" / "app.py"
PAGES = ["Overview", "Run detail", "Compare runs", "Cost & efficiency", "Evaluator trust"]


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
