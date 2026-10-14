"""AgentGauge command-line interface."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from agentgauge.config import Config, load_config
from agentgauge.cost import CostTracker, Pricing
from agentgauge.db import Database
from agentgauge.models import RunSummary, Scenario, ScenarioResult
from agentgauge.report import (
    compare_runs,
    fmt,
    fmt_delta,
    headline_rows,
    render_compare_markdown,
    render_html,
    render_markdown,
)
from agentgauge.runner import estimate_cost, resolve_agent_version, run_agent_suite, run_replay
from agentgauge.scenarios import ScenarioError, load_scenarios

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="AgentGauge: CI/CD for AI agents. Use --mock on any command to stay fully offline.")
console = Console()
err = Console(stderr=True)

ConfigOpt = typer.Option(None, "--config", "-c", help="Path to config.yaml (default: ./config.yaml).")
DbOpt = typer.Option(None, "--db", help="SQLite database path (overrides config).")
MockOpt = typer.Option(False, "--mock", help="Run fully offline with the deterministic mock model.")


def _setup(config: Path | None, db: Path | None) -> tuple[Config, Database]:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(config)
    except FileNotFoundError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    if db:
        cfg.paths.db = str(db)
    return cfg, Database(cfg.db_path)


def _fail(msg: str, code: int = 1) -> typer.Exit:
    err.print(f"[red]Error:[/red] {msg}")
    return typer.Exit(code)


def _print_summary(s: RunSummary) -> None:
    if s.mode == "mock":
        console.print("[yellow]MOCK RUN: simulated rule-based model, not real model behavior.[/yellow]")
    t = Table(title=f"{s.run_id} · {s.agent_name} · {s.label or '-'} · {s.mode} · {s.status}", show_header=False)
    for k, v in headline_rows(s):
        t.add_row(k, v)
    console.print(t)


def _progress(i: int, n: int, sc: Scenario, r: ScenarioResult) -> None:
    mark = "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]"
    comp = " [red]COMPROMISED[/red]" if r.compromised else ""
    failing = [e.name for e in r.evals if e.applicable and e.required and not e.passed]
    extra = f" ({', '.join(failing)})" if failing else ""
    console.print(f"[{i:>2}/{n}] {sc.id:<28} {mark}{comp} status={r.trace.status} "
                  f"steps={len(r.trace.model_steps())} cost={fmt(r.trace.total_cost_usd, 'usd')}{extra}")


def _has_aws_credentials(profile: str | None = None) -> bool:
    try:
        from agentgauge.model_client import aws_session

        return aws_session(profile).get_credentials() is not None
    except Exception:  # missing profile, expired login, missing awscrt, ...
        return False


def _print_estimate(est) -> None:
    t = Table(title="Cost estimate (dry run with the mock model, priced at live rates)", show_header=False)
    t.add_row("Scenarios", str(est.scenarios))
    t.add_row("Target model", f"{est.target_model_id or '-'}{'' if est.target_priced else '  [red](unpriced)[/red]'}")
    t.add_row("Agent calls / tokens in / out",
              f"{est.agent_calls} / {est.agent_input_tokens} / {est.agent_output_tokens}")
    t.add_row("Judge model", f"{est.judge_model_id or '-'}{'' if est.judge_priced else '  [red](unpriced)[/red]'}")
    t.add_row("Judge calls / tokens in / out",
              f"{est.judge_calls} / {est.judge_input_tokens} / {est.judge_output_tokens}")
    t.add_row("Agent USD (×safety)", fmt(est.agent_usd, "usd"))
    t.add_row("Judge USD (×safety)", fmt(est.judge_usd, "usd"))
    t.add_row(f"[b]Expected total (×{est.safety_factor})[/b]", f"[b]{fmt(est.expected_usd, 'usd')}[/b]")
    t.add_row("Session hard cap (max_usd_per_session)", fmt(est.session_cap_usd, "usd"))
    console.print(t)
    for w in est.warnings:
        console.print(f"[yellow]Warning:[/yellow] {w}")


# --------------------------------------------------------------------------- run / estimate


@app.command()
def run(
    agent: str = typer.Option("sample_v1", "--agent", "-a", help="sample_v1 (naive) or sample_v2 (hardened)."),
    suite: str = typer.Option("all", "--suite", "-s", help="all | functional | injection | efficiency | <path>"),
    label: str = typer.Option(None, "--label", "-l", help="Free-text label for this run (e.g. baseline)."),
    scenario: list[str] = typer.Option(None, "--scenario", help="Only run these scenario ids (repeatable)."),
    mock: bool = MockOpt,
    no_judge: bool = typer.Option(False, "--no-judge", help="Skip the LLM judge (deterministic evaluators only)."),
    allow_unpriced: bool = typer.Option(False, "--allow-unpriced",
                                        help="Allow live runs with models missing from pricing.yaml."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation before live runs."),
    config: Path = ConfigOpt,
    db: Path = DbOpt,
) -> None:
    """Run a scenario suite against the sample agent and store the results."""
    cfg, database = _setup(config, db)
    try:
        resolve_agent_version(agent)
        scenarios = load_scenarios(cfg.scenarios_path, suite, scenario or None)
    except (ValueError, ScenarioError) as exc:
        raise _fail(str(exc)) from exc
    if not scenarios:
        raise _fail(f"No scenarios for suite '{suite}' in {cfg.scenarios_path}")
    use_judge = not no_judge and cfg.judge.enabled

    if not mock:
        if not cfg.models.target_model_id:
            raise _fail("No target model configured. Set models.target_model_id in config.yaml or "
                        "AGENTGAUGE_TARGET_MODEL_ID (see `agentgauge list-models`), or use --mock.")
        if use_judge and not cfg.models.judge_model_id:
            raise _fail("No judge model configured. Set models.judge_model_id / AGENTGAUGE_JUDGE_MODEL_ID, "
                        "or pass --no-judge.")
        if not _has_aws_credentials(cfg.models.profile):
            who = f"profile '{cfg.models.profile}'" if cfg.models.profile else "the default credential chain"
            raise _fail(f"No usable AWS credentials for {who}. Sign in with `aws login --profile <name>` and set "
                        "AGENTGAUGE_PROFILE (or models.profile), or use --mock.")
        est = estimate_cost(cfg, agent, scenarios, use_judge=use_judge)
        _print_estimate(est)
        if est.expected_usd is None and not allow_unpriced:
            raise _fail("Cannot estimate USD cost because a model is missing from pricing.yaml. Fill in "
                        "pricing.yaml, or pass --allow-unpriced to run with token caps only.")
        if est.expected_usd is not None and est.expected_usd > cfg.budget.max_usd_per_session:
            raise _fail(f"Estimated cost {fmt(est.expected_usd, 'usd')} exceeds max_usd_per_session "
                        f"{fmt(cfg.budget.max_usd_per_session, 'usd')}. Reduce the suite or raise the cap.")
        if not yes and not typer.confirm(f"Run {len(scenarios)} scenario(s) live on Bedrock?", default=False):
            raise typer.Exit(1)

    tracker = CostTracker(Pricing.load(cfg.pricing_path), cfg.budget)
    console.print(f"Running {len(scenarios)} scenario(s) · agent={agent} · mode={'mock' if mock else 'live'} · "
                  f"judge={'on' if use_judge else 'off'} · db={cfg.db_path}")
    try:
        summary = run_agent_suite(cfg, agent, suite=suite, label=label, mock=mock, use_judge=use_judge,
                                  scenario_ids=scenario or None, db=database, progress=_progress, tracker=tracker)
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    _print_summary(summary)
    console.print(f"Stored as [b]{summary.run_id}[/b]. Next: `agentgauge report --run {summary.run_id}` "
                  f"or `agentgauge dashboard`.")


@app.command()
def estimate(
    agent: str = typer.Option("sample_v1", "--agent", "-a"),
    suite: str = typer.Option("all", "--suite", "-s"),
    scenario: list[str] = typer.Option(None, "--scenario"),
    no_judge: bool = typer.Option(False, "--no-judge"),
    mock: bool = MockOpt,
    config: Path = ConfigOpt,
) -> None:
    """Dry-run cost estimate for a live run (offline; uses the mock model to measure tokens)."""
    cfg = load_config(config)
    try:
        scenarios = load_scenarios(cfg.scenarios_path, suite, scenario or None)
        est = estimate_cost(cfg, agent, scenarios, use_judge=not no_judge and cfg.judge.enabled)
    except (ValueError, ScenarioError) as exc:
        raise _fail(str(exc)) from exc
    _print_estimate(est)


# --------------------------------------------------------------------------- replay


@app.command()
def replay(
    trace: list[Path] = typer.Option(..., "--trace", "-t",
                                     help="Trace JSON file (object, list, or JSONL). Repeatable."),
    label: str = typer.Option(None, "--label", "-l"),
    mock: bool = MockOpt,
    no_judge: bool = typer.Option(False, "--no-judge"),
    config: Path = ConfigOpt,
    db: Path = DbOpt,
) -> None:
    """Score externally produced traces without running any agent ("bring your own agent")."""
    from agentgauge.adapters import load_traces

    cfg, database = _setup(config, db)
    traces = []
    for p in trace:
        try:
            traces.extend(load_traces(p))
        except Exception as exc:
            raise _fail(f"Could not load {p}: {exc}") from exc
    if not traces:
        raise _fail("No traces found.")
    use_judge = not no_judge and cfg.judge.enabled
    if use_judge and not mock and not cfg.models.judge_model_id:
        raise _fail("No judge model configured: pass --mock (offline judge), --no-judge, or configure one.")
    summary = run_replay(cfg, traces, label=label, mock=mock, use_judge=use_judge, db=database, progress=_progress)
    _print_summary(summary)
    console.print(f"Stored as [b]{summary.run_id}[/b].")


# --------------------------------------------------------------------------- reporting


@app.command("list-runs")
def list_runs(mock: bool = MockOpt, config: Path = ConfigOpt, db: Path = DbOpt) -> None:
    """List stored runs (newest first)."""
    _, database = _setup(config, db)
    runs = database.list_runs()
    if not runs:
        console.print("No runs yet. Try: agentgauge run --agent sample_v1 --mock")
        return
    t = Table("run_id", "agent", "label", "mode", "status", "score", "func %", "inj resist %", "avg cost", "created")
    for r in runs:
        t.add_row(r.run_id, r.agent_name, r.label or "-", r.mode, r.status, fmt(r.reliability_score),
                  fmt(r.functional_pass_rate), fmt(r.injection_resistance_rate), fmt(r.avg_cost_usd, "usd"),
                  r.created_at)
    console.print(t)


@app.command()
def report(
    run_ref: str = typer.Option("latest", "--run", "-r", help="Run id, unique prefix, label, or 'latest'."),
    format: str = typer.Option("md", "--format", "-f", help="md | html"),
    out: Path = typer.Option(None, "--out", "-o", help="Output file (default: reports/<run_id>.<ext>)."),
    mock: bool = MockOpt,
    config: Path = ConfigOpt,
    db: Path = DbOpt,
) -> None:
    """Export a run report as Markdown or HTML."""
    cfg, database = _setup(config, db)
    if format not in ("md", "html"):
        raise _fail("--format must be md or html")
    try:
        summary = database.get_run(run_ref)
    except KeyError as exc:
        raise _fail(str(exc)) from exc
    results = database.get_results(summary.run_id)
    text = render_markdown(summary, results) if format == "md" else render_html(summary, results)
    out = out or cfg.resolve(f"reports/{summary.run_id}.{format}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    _print_summary(summary)
    console.print(f"Wrote {out}")


@app.command()
def compare(
    run_a: str = typer.Argument(..., help="Baseline run (id, prefix, or label)."),
    run_b: str = typer.Argument(..., help="Candidate run (id, prefix, or label)."),
    out: Path = typer.Option(None, "--out", "-o", help="Also write the comparison as Markdown."),
    mock: bool = MockOpt,
    config: Path = ConfigOpt,
    db: Path = DbOpt,
) -> None:
    """Compare two runs: metric deltas and scenarios that flipped."""
    _, database = _setup(config, db)
    try:
        a, b = database.get_run(run_a), database.get_run(run_b)
    except KeyError as exc:
        raise _fail(str(exc)) from exc
    c = compare_runs(a, database.get_results(a.run_id), b, database.get_results(b.run_id))
    t = Table("Metric", f"A: {a.agent_name} {a.label or ''}", f"B: {b.agent_name} {b.label or ''}", "Δ (B − A)",
              title=f"{a.run_id} → {b.run_id}")
    for m in c.metrics + c.components:
        kind = "usd" if "usd" in m.key else "pct" if "rate" in m.key else ""
        t.add_row(m.label, fmt(m.a, kind), fmt(m.b, kind), fmt_delta(m))
    console.print(t)
    console.print(f"[green]Flipped fail → pass ({len(c.flipped_to_pass)}):[/green] "
                  + (", ".join(s.scenario_id for s in c.flipped_to_pass) or "none"))
    console.print(f"[red]Flipped pass → fail ({len(c.flipped_to_fail)}):[/red] "
                  + (", ".join(s.scenario_id for s in c.flipped_to_fail) or "none"))
    if out:
        out.write_text(render_compare_markdown(c), encoding="utf-8")
        console.print(f"Wrote {out}")


# --------------------------------------------------------------------------- judge validation


@app.command("validate-judge")
def validate_judge_cmd(
    labels: Path = typer.Option(None, "--labels", help="Labels JSON (default: paths.labels in config)."),
    mock: bool = MockOpt,
    config: Path = ConfigOpt,
    db: Path = DbOpt,
) -> None:
    """Agreement rate and Cohen's kappa between the LLM judge and human labels."""
    from agentgauge.validation import validate_judge

    cfg, database = _setup(config, db)
    rep = validate_judge(database, labels or cfg.labels_path)
    console.print(f"Labels: {rep.labels_file} ({rep.n_labels} label(s), {rep.n_items} item(s))")
    if rep.status != "ok":
        console.print(f"[yellow]{rep.status}[/yellow]. Export a sheet with `agentgauge export-labels --run <id>`, "
                      "fill in human_label, then `agentgauge import-labels <csv>`.")
        return
    jh, hh = rep.judge_vs_human, rep.human_vs_human
    console.print(f"Judge vs human: n={jh.n}, agreement={fmt(jh.agreement and jh.agreement * 100, 'pct')}, "
                  f"Cohen's kappa={fmt(jh.kappa)}")
    console.print(f"Human vs human: n={hh.n}, agreement={fmt(hh.agreement and hh.agreement * 100, 'pct')}, "
                  f"Cohen's kappa={fmt(hh.kappa)}")
    console.print(f"Confusion: {rep.confusion}")
    if rep.n_ties:
        console.print(f"{rep.n_ties} item(s) skipped: human labelers tied.")
    if rep.missing:
        console.print(f"[yellow]{len(rep.missing)} labeled item(s) have no judge verdict in this DB[/yellow]")
    for d in rep.disagreements:
        console.print(f"  disagreement: {d.run_id}/{d.scenario_id} human={d.human} judge={d.judge} "
                      f"(score {fmt(d.judge_score)})")


@app.command("export-labels")
def export_labels(
    run_ref: list[str] = typer.Option(..., "--run", "-r", help="Run(s) to include (repeatable)."),
    out: Path = typer.Option(Path("validation/labeling_sheet.csv"), "--out", "-o"),
    mock: bool = MockOpt,
    config: Path = ConfigOpt,
    db: Path = DbOpt,
) -> None:
    """Write a blind labeling CSV (judge verdicts hidden) for humans to fill in."""
    from agentgauge.validation import export_labeling_sheet

    cfg, database = _setup(config, db)
    library = {s.id: s for s in load_scenarios(cfg.scenarios_path, "all")} if cfg.scenarios_path.exists() else {}
    try:
        n = export_labeling_sheet(database, run_ref, out, library)
    except KeyError as exc:
        raise _fail(str(exc)) from exc
    console.print(f"Wrote {n} row(s) to {out}. Fill in human_label (pass/fail) and labeler, then run "
                  f"`agentgauge import-labels {out}`.")


@app.command("import-labels")
def import_labels_cmd(
    csv_path: Path = typer.Argument(..., help="Filled-in labeling CSV."),
    labels: Path = typer.Option(None, "--labels", help="Labels JSON to merge into."),
    mock: bool = MockOpt,
    config: Path = ConfigOpt,
) -> None:
    """Merge human labels from a CSV into validation/labeled_runs.json."""
    from agentgauge.validation import import_labels

    cfg = load_config(config)
    target = labels or cfg.labels_path
    added, skipped = import_labels(csv_path, target)
    console.print(f"Added {added} label(s) to {target} ({skipped} blank/invalid row(s) skipped).")


# --------------------------------------------------------------------------- misc


@app.command("list-models")
def list_models(
    region: str = typer.Option(None, "--region", help="AWS region (default: config / AWS env)."),
    profile: str = typer.Option(None, "--profile", help="AWS CLI profile (default: config / AGENTGAUGE_PROFILE)."),
    contains: str = typer.Option(None, "--contains", help="Filter by substring (e.g. nova, claude, llama)."),
    mock: bool = MockOpt,
    config: Path = ConfigOpt,
) -> None:
    """List Bedrock text models and inference profiles available to your account."""
    cfg = load_config(config)
    if mock:
        console.print("Mock mode: the offline model id is 'mock-model'. No AWS call made.")
        return
    from agentgauge.model_client import list_bedrock_models

    try:
        rows = list_bedrock_models(region or cfg.models.region, profile or cfg.models.profile)
    except Exception as exc:
        raise _fail(f"Could not list models ({type(exc).__name__}: {exc}). Check the AWS profile, sign-in and Region."
        ) from exc
    t = Table("id", "kind", "provider", "name", "inference")
    for r in rows:
        if contains and contains.lower() not in (r["id"] + r["name"]).lower():
            continue
        t.add_row(r["id"], r["kind"], r["provider"], r["name"], r["inference"])
    console.print(t)
    console.print("Pick a tool-use capable model for the target (cheap) and a stronger one for the judge. "
                  "Models that only list INFERENCE_PROFILE must be called via an inference-profile id.")


@app.command()
def dashboard(
    port: int = typer.Option(8501, "--port"),
    mock: bool = MockOpt,
    config: Path = ConfigOpt,
    db: Path = DbOpt,
) -> None:
    """Launch the Streamlit dashboard."""
    cfg = load_config(config)
    env = dict(os.environ)
    env["AGENTGAUGE_DB"] = str(db.resolve() if db else cfg.db_path)
    if cfg.source:
        env["AGENTGAUGE_CONFIG"] = str(Path(cfg.source).resolve())
    app_path = Path(__file__).parent / "dashboard" / "app.py"
    console.print(f"Dashboard on http://localhost:{port}  (db: {env['AGENTGAUGE_DB']})")
    raise typer.Exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app_path),
                                      "--server.port", str(port), "--server.headless", "true",
                                      "--browser.gatherUsageStats", "false"], env=env))


@app.command()
def demo(config: Path = ConfigOpt, db: Path = DbOpt) -> None:
    """Offline demo: run the full suite on v1 and v2 with the mock model, then compare."""
    cfg, database = _setup(config, db)
    runs = []
    for agent, label in (("sample_v1", "mock-baseline"), ("sample_v2", "mock-hardened")):
        console.rule(f"{agent} (mock)")
        runs.append(run_agent_suite(cfg, agent, label=label, mock=True, db=database, progress=_progress))
        _print_summary(runs[-1])
    a, b = runs
    c = compare_runs(a, database.get_results(a.run_id), b, database.get_results(b.run_id))
    console.rule("v1 → v2")
    for m in c.metrics + c.components:
        kind = "usd" if "usd" in m.key else "pct" if "rate" in m.key else ""
        console.print(f"{m.label:<32} {fmt(m.a, kind):>10} → {fmt(m.b, kind):>10}   {fmt_delta(m)}")
    console.print(f"Flipped fail → pass: {', '.join(s.scenario_id for s in c.flipped_to_pass) or 'none'}")
    console.print(f"Flipped pass → fail: {', '.join(s.scenario_id for s in c.flipped_to_fail) or 'none'}")
    console.print("[yellow]Reminder: mock runs use a simulated model; they test the pipeline, not a real agent."
                  "[/yellow]\nOpen the dashboard with `agentgauge dashboard`.")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
