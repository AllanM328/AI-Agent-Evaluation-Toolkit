"""CLI commands via typer's CliRunner, fully offline."""

import csv
import json

import pytest
from typer.testing import CliRunner

from agentgauge.cli import app

from conftest import REPO_ROOT

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("COLUMNS", "250")  # keep rich tables from truncating cells
    db = tmp_path / "cli.db"
    return ["--db", str(db)], tmp_path


def invoke(*args, input=None):
    return runner.invoke(app, list(args), catch_exceptions=False, input=input)


def test_run_mock_list_report_compare(cli_env):
    db, tmp = cli_env
    r1 = invoke("run", "--agent", "sample_v1", "--suite", "all", "--label", "baseline", "--mock", *db)
    assert r1.exit_code == 0, r1.output
    assert "MOCK RUN" in r1.output and "inj_doc_email_001" in r1.output
    r2 = invoke("run", "--agent", "sample_v2", "--suite", "injection", "--label", "hardened", "--mock", *db)
    assert r2.exit_code == 0, r2.output

    listing = invoke("list-runs", *db)
    assert "baseline" in listing.output and "hardened" in listing.output

    md = tmp / "r.md"
    rep = invoke("report", "--run", "baseline", "--format", "md", "--out", str(md), *db)
    assert rep.exit_code == 0 and md.read_text().startswith("# AgentGauge report")
    html = tmp / "r.html"
    assert invoke("report", "--run", "latest", "--format", "html", "--out", str(html), *db).exit_code == 0
    assert "<table>" in html.read_text()

    cmp_md = tmp / "cmp.md"
    cmp = invoke("compare", "baseline", "hardened", "--out", str(cmp_md), *db)
    assert cmp.exit_code == 0 and "Flipped fail → pass" in cmp.output and cmp_md.exists()


def test_run_single_scenario_without_judge(cli_env):
    db, _ = cli_env
    r = invoke("run", "--agent", "sample_v2", "--scenario", "fn_order_status_001", "--no-judge", "--mock", *db)
    assert r.exit_code == 0 and "1/1" in r.output


def test_live_run_refuses_without_model_or_credentials(cli_env):
    db, _ = cli_env
    r = invoke("run", "--agent", "sample_v1", *db)
    assert r.exit_code == 1 and "No target model configured" in r.output


def test_live_run_refuses_without_aws_credentials(cli_env, monkeypatch):
    monkeypatch.setenv("AGENTGAUGE_PROVIDER", "bedrock")
    db, _ = cli_env
    monkeypatch.setenv("AGENTGAUGE_TARGET_MODEL_ID", "some-model")
    monkeypatch.setenv("AGENTGAUGE_JUDGE_MODEL_ID", "some-judge")
    monkeypatch.setenv("AGENTGAUGE_PROFILE", "agentgauge")
    seen = []

    def no_creds(profile=None):
        seen.append(profile)
        return "LoginRefreshRequired: session expired"

    monkeypatch.setattr("agentgauge.cli._aws_credentials_error", no_creds)
    r = invoke("run", "--agent", "sample_v1", *db)
    assert r.exit_code == 1 and "No usable AWS credentials for profile 'agentgauge'" in r.output
    assert "LoginRefreshRequired" in r.output and "aws login" in r.output and "boto3[crt]" in r.output
    assert seen == ["agentgauge"]  # the configured profile is what gets checked


def test_live_run_stops_when_unpriced_or_over_budget(cli_env, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTGAUGE_PROVIDER", "bedrock")
    db, _ = cli_env
    monkeypatch.setenv("AGENTGAUGE_TARGET_MODEL_ID", "unpriced-model")
    monkeypatch.setenv("AGENTGAUGE_JUDGE_MODEL_ID", "unpriced-judge")
    monkeypatch.setattr("agentgauge.cli._aws_credentials_error", lambda profile=None: None)
    r = invoke("run", "--agent", "sample_v1", *db)
    assert r.exit_code == 1 and "missing from pricing.yaml" in r.output and "Cost estimate" in r.output

    # priced, but the estimate exceeds the session cap -> stop before any live call
    cfg = tmp_path / "config.yaml"
    pricing = tmp_path / "pricing.yaml"
    pricing.write_text("models:\n  pricey:\n    input_per_1k: 10\n    output_per_1k: 10\n")
    cfg.write_text(f"paths:\n  scenarios: {REPO_ROOT / 'scenarios'}\n  pricing: {pricing}\n"
                   "budget:\n  max_usd_per_session: 1.0\n")
    monkeypatch.setenv("AGENTGAUGE_TARGET_MODEL_ID", "pricey")
    monkeypatch.setenv("AGENTGAUGE_JUDGE_MODEL_ID", "pricey")
    r2 = invoke("run", "--agent", "sample_v1", "--config", str(cfg), *db)
    assert r2.exit_code == 1 and "exceeds max_usd_per_session" in r2.output


def _priced_config(tmp_path, cap):
    cfg, pricing = tmp_path / "config.yaml", tmp_path / "pricing.yaml"
    pricing.write_text("models:\n  judge-x:\n    input_per_1k: 0.003\n    output_per_1k: 0.015\n")
    cfg.write_text(f"paths:\n  scenarios: {REPO_ROOT / 'scenarios'}\n  pricing: {pricing}\n"
                   f"budget:\n  max_usd_per_session: {cap}\n")
    return cfg


def test_live_replay_is_gated_like_run(cli_env, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTGAUGE_PROVIDER", "bedrock")
    db, _ = cli_env
    trace = str(REPO_ROOT / "examples/traces/example_trace.json")
    calls = []
    monkeypatch.setattr("agentgauge.runner.build_judge_client", lambda *a, **k: calls.append(1))
    monkeypatch.setenv("AGENTGAUGE_JUDGE_MODEL_ID", "unpriced-judge")

    monkeypatch.setattr("agentgauge.cli._aws_credentials_error", lambda profile=None: "no credentials found")
    r = invoke("replay", "--trace", trace, *db)
    assert r.exit_code == 1 and "No usable AWS credentials" in r.output

    monkeypatch.setattr("agentgauge.cli._aws_credentials_error", lambda profile=None: None)
    r = invoke("replay", "--trace", trace, *db)
    assert r.exit_code == 1 and "missing from pricing.yaml" in r.output and "Cost estimate" in r.output

    monkeypatch.setenv("AGENTGAUGE_JUDGE_MODEL_ID", "judge-x")
    r = invoke("replay", "--trace", trace, "--config", str(_priced_config(tmp_path, 0.000001)), *db)
    assert r.exit_code == 1 and "exceeds max_usd_per_session" in r.output

    r = invoke("replay", "--trace", trace, "--config", str(_priced_config(tmp_path, 2.0)), *db, input="n\n")
    assert r.exit_code == 1 and "Judge 2 trace(s) live on Bedrock?" in r.output
    assert calls == []  # no live judge client was ever built


def test_live_replay_runs_after_confirmation(cli_env, monkeypatch, tmp_path):
    from agentgauge.model_client import MockClient
    from agentgauge.runner import _JudgeRouter

    db, _ = cli_env
    monkeypatch.setenv("AGENTGAUGE_JUDGE_MODEL_ID", "judge-x")
    monkeypatch.setattr("agentgauge.cli._aws_credentials_error", lambda profile=None: None)
    monkeypatch.setattr("agentgauge.runner.build_judge_client", lambda *a, **k: MockClient(policy=_JudgeRouter()))
    r = invoke("replay", "--trace", str(REPO_ROOT / "examples/traces/example_trace.json"),
               "--config", str(_priced_config(tmp_path, 2.0)), "--yes", *db)
    assert r.exit_code == 0, r.output
    assert "Cost estimate" in r.output and "Stored as" in r.output


def test_list_models_passes_profile_and_region(cli_env, monkeypatch):
    monkeypatch.setenv("AGENTGAUGE_PROVIDER", "bedrock")
    seen = []
    monkeypatch.setattr("agentgauge.model_client.list_bedrock_models",
                        lambda region, profile=None: seen.append((region, profile)) or [])
    monkeypatch.setenv("AGENTGAUGE_PROFILE", "agentgauge")
    assert invoke("list-models").exit_code == 0
    assert invoke("list-models", "--profile", "other", "--region", "us-west-2").exit_code == 0
    assert seen == [("us-east-2", "agentgauge"), ("us-west-2", "other")]


def test_estimate_command(cli_env):
    r = invoke("estimate", "--agent", "sample_v2", "--suite", "functional")
    assert r.exit_code == 0 and "Cost estimate" in r.output


def test_replay_command(cli_env):
    db, _ = cli_env
    r = invoke("replay", "--trace", str(REPO_ROOT / "examples/traces/example_trace.json"), "--mock", *db)
    assert r.exit_code == 0, r.output
    assert "COMPROMISED" in r.output


def test_labeling_roundtrip_and_validate_judge(cli_env):
    db, tmp = cli_env
    r = invoke("run", "--agent", "sample_v1", "--suite", "functional", "--label", "lab", "--mock", *db)
    assert r.exit_code == 0
    labels = tmp / "labels.json"
    pending = invoke("validate-judge", "--labels", str(labels), *db)
    assert "pending" in pending.output

    sheet = tmp / "sheet.csv"
    r = invoke("export-labels", "--run", "lab", "--out", str(sheet), *db)
    assert r.exit_code == 0
    rows = list(csv.DictReader(sheet.open()))
    assert len(rows) == 10 and "judge" not in ",".join(rows[0].keys())  # blind sheet
    for i, row in enumerate(rows):  # synthetic labels for the test only
        row["human_label"] = "pass" if i % 3 else "fail"
        row["labeler"] = "A"
    rows += [dict(rows[0], labeler="B"), dict(rows[1], labeler="B", human_label="fail")]
    with sheet.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    imp = invoke("import-labels", str(sheet), "--labels", str(labels))
    assert "Added 12 label(s)" in imp.output
    assert len(json.loads(labels.read_text())) == 12

    v = invoke("validate-judge", "--labels", str(labels), *db)
    assert v.exit_code == 0 and "Judge vs human" in v.output and "Cohen's kappa" in v.output
    assert "Human vs human: n=2" in v.output


def test_list_models_mock():
    r = invoke("list-models", "--mock")
    assert r.exit_code == 0 and "mock-model" in r.output
