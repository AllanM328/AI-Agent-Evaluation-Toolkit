"""Data shaping for the dashboard (pure functions, unit-testable without Streamlit)."""

from __future__ import annotations

import pandas as pd

from agentgauge.models import ScenarioResult

# Reference palette (dataviz skill): categorical slots in fixed order; status colors reserved for pass/fail.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
STATUS_GOOD = "#0ca30c"
STATUS_CRITICAL = "#d03b3b"
STATUS_WARNING = "#fab219"
MUTED = "#898781"
GRID = "#e1e0d9"


def scenarios_frame(results: list[ScenarioResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        failing = [e.name for e in r.evals if e.applicable and e.required and not e.passed]
        rows.append({
            "scenario": r.scenario_id,
            "category": r.category,
            "result": "PASS" if r.passed else "FAIL",
            "compromised": {True: "YES", False: "no", None: "-"}[r.compromised],
            "status": r.trace.status,
            "judge": r.judge_score,
            "steps": len(r.trace.model_steps()),
            "tool_calls": len(r.trace.all_tool_calls()),
            "blocked": len(r.trace.blocked_tool_calls()),
            "cost_usd": r.trace.total_cost_usd,
            "tokens_in": r.trace.total_input_tokens,
            "tokens_out": r.trace.total_output_tokens,
            "latency_ms": r.trace.total_latency_ms,
            "failing": ", ".join(failing) or "-",
            "description": r.description,
        })
    return pd.DataFrame(rows)


def category_frame(results: list[ScenarioResult]) -> pd.DataFrame:
    rows = []
    for cat in ("functional", "injection", "efficiency"):
        rs = [r for r in results if r.category == cat]
        if rs:
            passed = sum(r.passed for r in rs)
            rows.append({"category": cat, "result": "PASS", "count": passed})
            rows.append({"category": cat, "result": "FAIL", "count": len(rs) - passed})
    return pd.DataFrame(rows)


def evaluator_frame(results: list[ScenarioResult]) -> pd.DataFrame:
    stats: dict[str, dict] = {}
    for r in results:
        for e in r.evals:
            if not e.applicable:
                continue
            s = stats.setdefault(e.name, {"evaluator": e.name, "dimension": e.dimension, "applicable": 0,
                                          "passed": 0, "required_in": 0})
            s["applicable"] += 1
            s["passed"] += int(e.passed)
            s["required_in"] += int(e.required)
    df = pd.DataFrame(list(stats.values()))
    if not df.empty:
        df["pass_rate"] = (100 * df["passed"] / df["applicable"]).round(1)
    return df


def steps_frame(results: list[ScenarioResult]) -> pd.DataFrame:
    """One row per model step: tokens and latency split, for the cost & efficiency page."""
    rows = []
    for r in results:
        for n, s in enumerate(r.trace.model_steps(), 1):
            rows.append({
                "scenario": r.scenario_id, "model_step": n, "step_index": s.index,
                "input_tokens": s.input_tokens, "output_tokens": s.output_tokens,
                "model_latency_ms": s.latency_ms, "tool_latency_ms": sum(c.latency_ms for c in s.tool_calls),
                "tool_calls": ", ".join(c.name + (" [BLOCKED]" if c.blocked else "") for c in s.tool_calls) or "-",
            })
    return pd.DataFrame(rows)


def judge_samples_frame(results: list[ScenarioResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        e = r.get_eval("judge")
        if e and e.applicable and e.details.get("scores"):
            rows.append({"scenario": r.scenario_id, "final_score": e.details.get("final_score"),
                         "scores": ", ".join(map(str, e.details["scores"])),
                         "agreement": e.details.get("agreement"), "spread": e.details.get("score_spread"),
                         "verdict": "PASS" if e.passed else "FAIL", "required": e.required})
    return pd.DataFrame(rows)
