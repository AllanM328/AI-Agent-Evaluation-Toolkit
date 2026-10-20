"""Run comparison and Markdown / HTML report export."""

from __future__ import annotations

import html
from typing import Any, Literal

from pydantic import BaseModel

from agentgauge.models import RunSummary, ScenarioResult

# --------------------------------------------------------------------------- comparison

METRICS: list[tuple[str, str, bool]] = [  # (attribute, label, higher_is_better)
    ("reliability_score", "Reliability score", True),
    ("pass_rate", "Overall pass rate %", True),
    ("functional_pass_rate", "Functional pass rate %", True),
    ("injection_resistance_rate", "Injection resistance %", True),
    ("avg_cost_usd", "Avg agent cost / run (USD)", False),
    ("p50_latency_ms", "p50 latency (ms)", False),
    ("p95_latency_ms", "p95 latency (ms)", False),
    ("avg_steps", "Avg model steps", False),
]


class MetricDelta(BaseModel):
    key: str
    label: str
    a: float | None
    b: float | None
    delta: float | None
    higher_is_better: bool

    @property
    def improved(self) -> bool | None:
        if self.delta is None or self.delta == 0:
            return None
        return (self.delta > 0) == self.higher_is_better


Change = Literal["fail→pass", "pass→fail", "same", "only_a", "only_b"]


class ScenarioDelta(BaseModel):
    scenario_id: str
    category: str
    a_passed: bool | None
    b_passed: bool | None
    a_compromised: bool | None = None
    b_compromised: bool | None = None
    a_judge: float | None = None
    b_judge: float | None = None
    change: Change


class Comparison(BaseModel):
    run_a: RunSummary
    run_b: RunSummary
    metrics: list[MetricDelta]
    components: list[MetricDelta]
    scenarios: list[ScenarioDelta]

    @property
    def flipped_to_pass(self) -> list[ScenarioDelta]:
        return [s for s in self.scenarios if s.change == "fail→pass"]

    @property
    def flipped_to_fail(self) -> list[ScenarioDelta]:
        return [s for s in self.scenarios if s.change == "pass→fail"]


def _delta(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else round(b - a, 6)


def compare_runs(a: RunSummary, results_a: list[ScenarioResult], b: RunSummary,
                 results_b: list[ScenarioResult]) -> Comparison:
    metrics = [MetricDelta(key=k, label=label, a=getattr(a, k), b=getattr(b, k),
                           delta=_delta(getattr(a, k), getattr(b, k)), higher_is_better=hib)
               for k, label, hib in METRICS]
    components = [MetricDelta(key=d, label=d.capitalize(), a=a.components.get(d), b=b.components.get(d),
                              delta=_delta(a.components.get(d), b.components.get(d)), higher_is_better=True)
                  for d in ("correctness", "safety", "efficiency")]
    ra = {r.scenario_id: r for r in results_a}
    rb = {r.scenario_id: r for r in results_b}
    rows = []
    for sid in sorted(set(ra) | set(rb), key=lambda s: ((ra.get(s) or rb[s]).category, s)):
        x, y = ra.get(sid), rb.get(sid)
        if x and y:
            change: Change = ("same" if x.passed == y.passed else "fail→pass" if y.passed else "pass→fail")
        else:
            change = "only_a" if x else "only_b"
        rows.append(ScenarioDelta(
            scenario_id=sid, category=(x or y).category, a_passed=x.passed if x else None,
            b_passed=y.passed if y else None, a_compromised=x.compromised if x else None,
            b_compromised=y.compromised if y else None, a_judge=x.judge_score if x else None,
            b_judge=y.judge_score if y else None, change=change))
    return Comparison(run_a=a, run_b=b, metrics=metrics, components=components, scenarios=rows)


# --------------------------------------------------------------------------- formatting helpers


def fmt(value: Any, kind: str = "") -> str:
    if value is None:
        return "n/a"
    if kind == "usd":
        return f"${value:.4f}"
    if kind == "pct":
        return f"{value:.1f}%"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".") if value != int(value) else f"{int(value)}"
    return str(value)


def _kind(key: str) -> str:
    if "usd" in key:
        return "usd"
    if "rate" in key:
        return "pct"
    return ""


def fmt_delta(m: MetricDelta) -> str:
    if m.delta is None:
        return "n/a"
    sign = "+" if m.delta > 0 else ""
    arrow = {True: " ▲ better", False: " ▼ worse", None: ""}[m.improved]
    body = f"{sign}{m.delta:.4f}" if _kind(m.key) == "usd" else f"{sign}{fmt(m.delta)}"
    return body + arrow


def headline_rows(s: RunSummary) -> list[tuple[str, str]]:
    return [
        ("Reliability score (0-100)", fmt(s.reliability_score)),
        ("Correctness / Safety / Efficiency", " / ".join(fmt(s.components.get(d)) for d in
                                                        ("correctness", "safety", "efficiency"))),
        ("Weights", " / ".join(fmt(s.weights.get(d)) for d in ("correctness", "safety", "efficiency"))),
        ("Scenarios passed", f"{s.n_passed}/{s.n_scenarios}" + (f" ({s.n_skipped} skipped)" if s.n_skipped else "")
         + (f" ({s.n_errored} errored, excluded from scores)" if s.n_errored else "")),
        ("Functional pass rate", fmt(s.functional_pass_rate, "pct")),
        ("Injection resistance", f"{fmt(s.injection_resistance_rate, 'pct')} "
                                 f"({s.injection_exercised}/{s.injection_attacks} attacks exercised)"),
        ("Benign false-positive check", {True: "passed", False: "FAILED", None: "n/a"}[s.benign_control_passed]),
        ("Agent cost total / avg per run", f"{fmt(s.total_cost_usd, 'usd')} / {fmt(s.avg_cost_usd, 'usd')}"),
        ("Evaluator (judge) cost", fmt(s.eval_cost_usd, "usd")),
        ("Tokens in / out (agent)", f"{s.total_input_tokens} / {s.total_output_tokens}"),
        ("Latency p50 / p95", f"{fmt(s.p50_latency_ms)} ms / {fmt(s.p95_latency_ms)} ms"),
        ("Avg model steps", fmt(s.avg_steps)),
    ]


# --------------------------------------------------------------------------- markdown


def render_markdown(s: RunSummary, results: list[ScenarioResult]) -> str:
    out = [f"# AgentGauge report: {s.run_id}", ""]
    if s.mode == "mock":
        out += ["> **MOCK RUN**: produced by a simulated rule-based model, not a real LLM. "
                "Numbers exercise the pipeline only.", ""]
    out += [f"- **Agent:** {s.agent_name}  ", f"- **Label:** {s.label or '-'}  ", f"- **Mode:** {s.mode}  ",
            f"- **Status:** {s.status}  ", f"- **Target model:** {s.target_model_id or '-'}  ",
            f"- **Judge model:** {s.judge_model_id or '-'}  ", f"- **Started:** {s.created_at}  ", "",
            "## Summary", "", "| Metric | Value |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in headline_rows(s)]
    if s.by_category:
        out += ["", "## By category", "", "| Category | Passed | Pass rate |", "|---|---|---|"]
        out += [f"| {c} | {st.passed}/{st.n} | {fmt(st.pass_rate, 'pct')} |" for c, st in s.by_category.items()]
    out += ["", "## Scenarios", "",
            "| Scenario | Category | Result | Compromised | Judge | Steps | Cost | Failing evaluators |",
            "|---|---|---|---|---|---|---|---|"]
    for r in results:
        failing = ", ".join(e.name for e in r.evals if e.applicable and e.required and not e.passed) or "-"
        comp = {True: "YES", False: "no", None: "-"}[r.compromised]
        out.append(f"| {r.scenario_id} | {r.category} | {'PASS' if r.passed else 'FAIL'} | {comp} | "
                   f"{fmt(r.judge_score)} | {len(r.trace.model_steps())} | {fmt(r.trace.total_cost_usd, 'usd')} | "
                   f"{failing} |")
    out += ["", "## Evidence", ""]
    for r in results:
        out += [f"### {r.scenario_id}: {'PASS' if r.passed else 'FAIL'} (trace status {r.trace.status})", "",
                f"*{r.description}*", "", f"**User:** {r.trace.user_prompt}", "",
                f"**Final answer:** {r.trace.final_answer or '(none)'}", ""]
        for e in r.evals:
            if not e.applicable:
                continue
            tag = "required" if e.required else "advisory"
            out.append(f"- **{e.name}** ({e.dimension}, {tag}): {e.evidence}")
        out.append("")
    if s.notes:
        out += ["## Notes", ""] + [f"- {n}" for n in s.notes] + [""]
    out += ["---", "Costs are computed from logged token counts × `pricing.yaml` rates "
            "(see the cost ledger in the database)."]
    return "\n".join(out)


def render_compare_markdown(c: Comparison) -> str:
    a, b = c.run_a, c.run_b
    out = [f"# Compare: {a.run_id} ({a.agent_name}{', ' + a.label if a.label else ''}) → "
           f"{b.run_id} ({b.agent_name}{', ' + b.label if b.label else ''})", ""]
    if "mock" in (a.mode, b.mode):
        out += ["> At least one run is a **MOCK RUN** (simulated model).", ""]
    out += ["| Metric | A | B | Δ (B − A) |", "|---|---|---|---|"]
    for m in c.metrics + c.components:
        k = _kind(m.key)
        out.append(f"| {m.label} | {fmt(m.a, k)} | {fmt(m.b, k)} | {fmt_delta(m)} |")
    out += ["", f"## Flipped fail → pass ({len(c.flipped_to_pass)})", ""]
    out += [f"- {s.scenario_id} ({s.category})" for s in c.flipped_to_pass] or ["- none"]
    out += ["", f"## Flipped pass → fail ({len(c.flipped_to_fail)})", ""]
    out += [f"- {s.scenario_id} ({s.category})" for s in c.flipped_to_fail] or ["- none"]
    return "\n".join(out)


# --------------------------------------------------------------------------- html

_CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f7f7f8;color:#1d1d1f}
main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:1.5rem} h2{margin-top:2rem;font-size:1.2rem} h3{font-size:1rem;margin-bottom:.25rem}
.banner{background:#fff4ce;border:1px solid #e5c76b;padding:10px 14px;border-radius:8px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.card{background:#fff;border:1px solid #e3e3e6;border-radius:10px;padding:12px}
.card .v{font-size:1.5rem;font-weight:600} .card .k{color:#5f5f66;font-size:.85rem}
table{border-collapse:collapse;width:100%;background:#fff;font-size:.9rem}
th,td{border:1px solid #e3e3e6;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f0f0f3} .pass{color:#0a7a2f;font-weight:600} .fail{color:#b3261e;font-weight:600}
.ev{background:#fff;border:1px solid #e3e3e6;border-radius:10px;padding:12px;margin:10px 0}
.ev li{margin:4px 0} code{background:#f0f0f3;padding:1px 4px;border-radius:4px}
.scroll{overflow-x:auto}
"""


def render_html(s: RunSummary, results: list[ScenarioResult]) -> str:
    e = html.escape
    cards = [("Reliability", fmt(s.reliability_score)), ("Functional pass", fmt(s.functional_pass_rate, "pct")),
             ("Injection resistance", fmt(s.injection_resistance_rate, "pct")),
             ("Avg cost / run", fmt(s.avg_cost_usd, "usd"))]
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>AgentGauge {e(s.run_id)}</title><style>{_CSS}</style></head><body><main>",
             f"<h1>AgentGauge report: {e(s.run_id)}</h1>"]
    if s.mode == "mock":
        parts.append("<p class='banner'><b>MOCK RUN</b>: produced by a simulated rule-based model, not a real LLM."
                     "</p>")
    parts.append(f"<p>Agent <code>{e(s.agent_name)}</code> · label <code>{e(s.label or '-')}</code> · mode "
                 f"<code>{e(s.mode)}</code> · status <code>{e(s.status)}</code> · target model "
                 f"<code>{e(s.target_model_id or '-')}</code> · judge <code>{e(s.judge_model_id or '-')}</code></p>")
    parts.append("<div class='cards'>" + "".join(
        f"<div class='card'><div class='v'>{e(v)}</div><div class='k'>{e(k)}</div></div>" for k, v in cards)
        + "</div>")
    parts.append("<h2>Summary</h2><div class='scroll'><table>" + "".join(
        f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in headline_rows(s)) + "</table></div>")
    parts.append("<h2>Scenarios</h2><div class='scroll'><table><tr><th>Scenario</th><th>Category</th>"
                 "<th>Result</th><th>Compromised</th><th>Judge</th><th>Steps</th><th>Cost</th>"
                 "<th>Failing evaluators</th></tr>")
    for r in results:
        failing = ", ".join(x.name for x in r.evals if x.applicable and x.required and not x.passed) or "-"
        cls = "pass" if r.passed else "fail"
        comp = {True: "YES", False: "no", None: "-"}[r.compromised]
        parts.append(f"<tr><td><a href='#{e(r.scenario_id)}'>{e(r.scenario_id)}</a></td><td>{e(r.category)}</td>"
                     f"<td class='{cls}'>{'PASS' if r.passed else 'FAIL'}</td><td>{comp}</td>"
                     f"<td>{e(fmt(r.judge_score))}</td><td>{len(r.trace.model_steps())}</td>"
                     f"<td>{e(fmt(r.trace.total_cost_usd, 'usd'))}</td><td>{e(failing)}</td></tr>")
    parts.append("</table></div><h2>Evidence</h2>")
    for r in results:
        cls = "pass" if r.passed else "fail"
        parts.append(f"<div class='ev' id='{e(r.scenario_id)}'><h3>{e(r.scenario_id)} "
                     f"<span class='{cls}'>{'PASS' if r.passed else 'FAIL'}</span></h3>"
                     f"<p><i>{e(r.description)}</i></p><p><b>User:</b> {e(r.trace.user_prompt)}</p>"
                     f"<p><b>Final answer:</b> {e(r.trace.final_answer or '(none)')}</p><ul>")
        for x in r.evals:
            if x.applicable:
                tag = "required" if x.required else "advisory"
                parts.append(f"<li><b>{e(x.name)}</b> ({e(x.dimension)}, {tag}): {e(x.evidence)}</li>")
        parts.append("</ul></div>")
    if s.notes:
        parts.append("<h2>Notes</h2><ul>" + "".join(f"<li>{e(n)}</li>" for n in s.notes) + "</ul>")
    parts.append("<p><small>Costs = logged token counts × pricing.yaml rates.</small></p></main></body></html>")
    return "".join(parts)
