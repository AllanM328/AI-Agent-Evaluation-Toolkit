import pytest

from agentgauge.db import Database
from agentgauge.runner import run_agent_suite
from agentgauge.validation import HumanLabel, cohens_kappa, save_labels, validate_judge


def test_cohens_kappa_known_values():
    assert cohens_kappa([True, False, True, False], [True, False, True, False]) == 1.0
    # po=0.8, pe=0.5 -> kappa 0.6
    a = [True] * 5 + [False] * 5
    b = [True, True, True, True, False, False, False, False, False, True]
    assert cohens_kappa(a, b) == pytest.approx(0.6)
    assert cohens_kappa([True, True], [True, True]) is None  # chance agreement = 1
    assert cohens_kappa([], []) is None


def test_validate_judge_against_db(config, tmp_path):
    db = Database(config.db_path)
    run = run_agent_suite(config, "sample_v1", suite="functional", mock=True, db=db)
    results = db.get_results(run.run_id)
    labels = []
    for r in results:
        judge_pass = r.get_eval("judge").passed
        labels.append(HumanLabel(run_id=run.run_id, scenario_id=r.scenario_id,
                                 human_label="pass" if judge_pass else "fail", labeler="A"))
    labels[0] = labels[0].model_copy(update={"human_label": "fail" if labels[0].human_label == "pass" else "pass"})
    labels.append(HumanLabel(run_id=run.run_id, scenario_id="does_not_exist", human_label="pass", labeler="A"))
    path = tmp_path / "labels.json"
    save_labels(path, labels)
    rep = validate_judge(db, path)
    assert rep.status == "ok"
    assert rep.n_matched == len(results)
    assert rep.judge_vs_human.agreement == pytest.approx((len(results) - 1) / len(results), abs=1e-4)
    assert len(rep.disagreements) == 1 and rep.missing == [f"{run.run_id}/does_not_exist"]


def test_empty_labels_are_pending(config, tmp_path):
    rep = validate_judge(Database(config.db_path), tmp_path / "none.json")
    assert rep.status.startswith("pending") and rep.n_labels == 0
