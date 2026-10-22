"""Data shaping for the dashboard (pure functions, unit-testable without Streamlit)."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from agentgauge.models import RunSummary, ScenarioResult

# Reference palette (dataviz skill): categorical slots in fixed order; status colors reserved for pass/fail.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
STATUS_GOOD = "#0ca30c"
STATUS_CRITICAL = "#d03b3b"
STATUS_WARNING = "#fab219"
MUTED = "#898781"
GRID = "#e1e0d9"

# Plain-English names for the screen; internal names stay in captions so evidence can be traced.
AGENT_LABELS = {"sample_agent_v1": "v1 naive", "sample_agent_v2": "v2 hardened"}
EVAL_QUESTIONS = {
    "tool_calls": "Called the right tools?",
    "answer_content": "Answer has the required facts?",
    "scope": "Stayed within what was asked?",
    "injection": "Resisted hidden instructions?",
    "redundancy": "No wasted or repeated steps?",
    "efficiency": "Within cost and step budget?",
    "judge": "LLM judge: good answer? (1-5)",
    "groundedness": "Claims backed by tool results?",
}
EVAL_SHORT = {
    "tool_calls": "wrong tools", "answer_content": "missing facts", "scope": "out of scope",
    "injection": "hijacked", "redundancy": "wasted steps", "efficiency": "over budget",
    "judge": "judge score", "groundedness": "unsupported claims",
}


def agent_label(agent_name: str) -> str:
    return AGENT_LABELS.get(agent_name, agent_name)


def run_label(r: RunSummary) -> str:
    when = r.created_at[:16].replace("T", " ")
    return f"{r.label or r.run_id} · {agent_label(r.agent_name)} · {r.mode} · {when}"


def resolve_runs(runs: list[RunSummary], wanted: list[str]) -> list[RunSummary]:
    """Runs matching each wanted label or run id, in the order given (newest run wins a shared label)."""
    out = []
    for w in wanted:
        hit = next((r for r in runs if r.run_id == w), None) or next((r for r in runs if r.label == w), None)
        if hit is not None and hit not in out:
            out.append(hit)
    return out


def pick_spotlight(a: list[ScenarioResult], b: list[ScenarioResult], preferred: str | None = None) -> str | None:
    """Scenario for the Headline page: the preferred one, else the clearest A-vs-B safety difference."""
    ids = [r.scenario_id for r in a]
    if preferred in ids:
        return preferred
    b_by_id = {r.scenario_id: r for r in b}

    def b_ok(r: ScenarioResult) -> bool:
        other = b_by_id.get(r.scenario_id)
        return other is not None and not other.compromised

    for test in (lambda r: r.compromised and b_ok(r),
                 lambda r: r.category == "injection" and not r.passed and b_by_id.get(r.scenario_id) is not None
                 and b_by_id[r.scenario_id].passed,
                 lambda r: r.category == "injection"):
        hit = next((r.scenario_id for r in a if test(r)), None)
        if hit:
            return hit
    return ids[0] if ids else None


def poison_snippets(value: Any, marker: str | None) -> list[str]:
    """String fields inside a tool result that carry the poison marker (e.g. the planted order note)."""
    if not marker:
        return []
    if isinstance(value, str):
        return [value.strip()] if marker in value else []
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return [s for v in value for s in poison_snippets(v, marker)]
    return []


_CITED_CALL = re.compile(r"step (\d+): (\w+)\(")


def flagged_calls(r: ScenarioResult) -> set[tuple[int, str]]:
    """(step, tool) pairs that a failing scope or injection verdict cites as the unsafe action."""
    out: set[tuple[int, str]] = set()
    for e in r.evals:
        if e.name in ("scope", "injection") and e.applicable and not e.passed:
            out |= {(int(i), name) for i, name in _CITED_CALL.findall(e.evidence)}
    return out


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
            "failing": ", ".join(EVAL_SHORT.get(f, f) for f in failing) or "-",
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
