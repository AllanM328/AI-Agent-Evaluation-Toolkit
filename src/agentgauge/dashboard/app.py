"""AgentGauge dashboard (Streamlit + Plotly). Reads the SQLite results database.

Launch with `agentgauge dashboard` (or `streamlit run src/agentgauge/dashboard/app.py`).
The database path comes from $AGENTGAUGE_DB or config.yaml. `agentgauge dashboard --demo` sets
$AGENTGAUGE_DEMO_RUNS, which limits every page to those runs and opens on the Headline page.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from agentgauge.config import load_config
from agentgauge.dashboard.data import (
    EVAL_QUESTIONS,
    SERIES,
    STATUS_CRITICAL,
    STATUS_GOOD,
    agent_label,
    category_frame,
    evaluator_frame,
    flagged_calls,
    judge_samples_frame,
    pick_spotlight,
    poison_snippets,
    resolve_runs,
    run_label,
    scenarios_frame,
    steps_frame,
)
from agentgauge.db import Database
from agentgauge.models import RunSummary, ScenarioResult
from agentgauge.report import compare_runs, fmt, fmt_delta
from agentgauge.validation import validate_judge

PAGES = ["Headline", "Overview", "Run detail", "Compare runs", "Cost & efficiency", "Evaluator trust"]
RESULT_COLORS = {"PASS": STATUS_GOOD, "FAIL": STATUS_CRITICAL}

st.set_page_config(page_title="AgentGauge", page_icon="📏", layout="wide")


# --------------------------------------------------------------------------- helpers


def style(fig: go.Figure, height: int = 320, *, all_y_labels: bool = False) -> go.Figure:
    """Thin marks, recessive axes, legend on top (titles are rendered by Streamlit, not Plotly)."""
    fig.update_layout(height=height, margin={"l": 8, "r": 8, "t": 28, "b": 8}, barcornerradius=4,
                      legend={"orientation": "h", "yanchor": "bottom", "y": 1.0, "x": 0, "title": None},
                      font={"family": "system-ui, -apple-system, Segoe UI, sans-serif"}, bargap=0.35)
    fig.update_xaxes(showgrid=False, title=None)
    fig.update_yaxes(zeroline=False, title=None)
    if all_y_labels:
        fig.update_yaxes(tickmode="linear", automargin=True)
    return fig


def show_chart(fig: go.Figure, table: pd.DataFrame | None = None, key: str = "", title: str = "") -> None:
    if title:
        st.markdown(f"**{title}**")
    st.plotly_chart(fig, use_container_width=True, key=f"chart_{key}")
    if table is not None:
        with st.expander("Table view"):
            st.dataframe(table, use_container_width=True, hide_index=True)


def mock_banner(*runs: RunSummary) -> None:
    if any(r.mode == "mock" for r in runs):
        st.warning("**MOCK RUN**: these results come from a simulated rule-based model, not a real LLM. "
                   "They exercise the pipeline and are not evidence about agent quality.", icon="⚠️")


def md(text: str | None) -> str:
    """Escape `$` so prices like $45.00 aren't rendered as LaTeX by st.markdown."""
    return (text or "").replace("$", "\\$")


def pick_run(runs: list[RunSummary], label: str, key: str, default: int = 0) -> RunSummary:
    if st.session_state.get(key, -1) not in range(len(runs)):
        st.session_state[key] = min(default, len(runs) - 1)
    # The default goes through session state, not `index=`, so buttons can preselect a run (see open_trace).
    idx = st.selectbox(label, range(len(runs)), format_func=lambda i: run_label(runs[i]), key=key)
    return runs[idx]


@st.cache_data(ttl=10, show_spinner=False)
def load_results(db_path: str, run_id: str) -> list[dict]:
    return [r.model_dump() for r in Database(db_path).get_results(run_id)]


def results_for(db: Database, run_id: str) -> list[ScenarioResult]:
    return [ScenarioResult.model_validate(r) for r in load_results(str(db.path), run_id)]


def pct(v: float | None) -> str:
    return fmt(v, "pct")


def result_for(db: Database, run_id: str, scenario_id: str) -> ScenarioResult | None:
    return next((r for r in results_for(db, run_id) if r.scenario_id == scenario_id), None)


def call_text(c) -> str:
    args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in c.arguments.items())
    return f"`{c.name}({args})`"


def open_trace(run_index: int, scenario_id: str) -> None:
    """Button callback: jump to Run detail with this run and scenario selected."""
    st.session_state["page"] = "Run detail"
    st.session_state["detail_run"] = run_index
    st.session_state["detail_scenario"] = scenario_id


# --------------------------------------------------------------------------- pages


def headline_column(db: Database, runs: list[RunSummary], s: RunSummary, sid: str, key: str) -> None:
    r = result_for(db, s.run_id, sid)
    st.markdown(f"### {agent_label(s.agent_name)}")
    st.caption(f"run `{s.label or s.run_id}` · {s.mode}")
    if r is None:
        st.info("This run has no result for the scenario.")
        return
    inj = r.get_eval("injection")
    exercised = bool(inj and inj.details.get("exercised"))
    if r.compromised:
        st.error("**HIJACKED**: it followed the hidden instruction.", icon="🚨")
    elif inj and inj.applicable and not exercised:
        st.info("The hidden instruction never reached this agent, so this run didn't test it.", icon="➖")
    else:
        st.success("**Not hijacked**: it ignored the hidden instruction.", icon="🛡️")
    flagged = flagged_calls(r)
    actions = []
    for i, c in r.trace.all_tool_calls():
        if c.blocked:
            actions.append(f"- 🛡️ tried {call_text(c)}, **blocked by its guardrail**")
        elif (i, c.name) in flagged:
            actions.append(f"- ❌ {call_text(c)} **(nobody asked for this)**")
        else:
            actions.append(f"- {call_text(c)}")
    st.markdown("**What it did**\n" + ("\n".join(actions) if actions else "- no tool calls"))
    st.markdown("**What it told the customer**")
    st.markdown("> " + md(r.trace.final_answer or "(no answer)").replace("\n", "\n> "))
    st.button(f"Open the full {agent_label(s.agent_name)} trace →", key=f"open_{key}", on_click=open_trace,
              args=(runs.index(s), sid))


def compromise_tally(db: Database, all_runs: list[RunSummary], like: RunSummary, sid: str) -> tuple[int, int]:
    """(compromised, completed runs) for this scenario over every stored run of the same agent and mode."""
    hit = n = 0
    for run in all_runs:
        if run.agent_name != like.agent_name or run.mode != like.mode or run.status != "COMPLETED":
            continue
        r = result_for(db, run.run_id, sid)
        if r is None or r.trace.status != "OK":
            continue
        n += 1
        hit += bool(r.compromised)
    return hit, n


def page_headline(db: Database, runs: list[RunSummary], all_runs: list[RunSummary], cfg) -> None:
    da, dbi = _default_pair(runs)
    with st.expander("Change runs or scenario"):
        c1, c2 = st.columns(2)
        with c1:
            a = pick_run(runs, "Run A (baseline)", "head_a", da)
        with c2:
            b = pick_run(runs, "Run B (candidate)", "head_b", dbi)
        a_results, b_results = results_for(db, a.run_id), results_for(db, b.run_id)
        spot = pick_spotlight(a_results, b_results, cfg.dashboard.spotlight_scenario)
        by_id = {r.scenario_id: r for r in a_results}
        ids = list(by_id)
        if not ids:
            st.info("Run A has no results.")
            return
        sid = st.selectbox("Scenario", ids, index=ids.index(spot) if spot in ids else 0, key="head_scenario",
                           format_func=lambda i: f"{by_id[i].description or i} ({i})")
    mock_banner(a, b)
    r = by_id[sid]
    st.markdown(f"## {md(r.description or sid)}")
    st.caption(f"scenario `{sid}` · {r.category}")

    left, right = st.columns(2)
    left.markdown(f"**The customer asked**\n> {md(r.trace.user_prompt)}")
    inj = r.get_eval("injection")
    canary = inj.details.get("canary") if inj else None
    snippets: list[str] = []
    for res in (r, result_for(db, b.run_id, sid)):
        for _, c in (res.trace.executed_tool_calls() if res else []):
            snippets = poison_snippets(c.result, canary)
            if snippets:
                where = c.name
                break
        if snippets:
            break
    with right:
        if snippets:
            st.markdown(f"**Hidden in the data `{where}` returned**")
            st.warning(md(snippets[0]), icon="⚠️")
        elif r.category == "injection":
            st.caption("Neither agent retrieved the poisoned content in these runs.")

    ca, cb = st.columns(2, gap="large")
    with ca:
        headline_column(db, runs, a, sid, "a")
    with cb:
        headline_column(db, runs, b, sid, "b")

    st.divider()
    m = st.columns(3)
    if r.category == "injection":
        ha, na = compromise_tally(db, all_runs, a, sid)
        hb, nb = compromise_tally(db, all_runs, b, sid)
        m[0].metric(f"Runs hijacked by this attack, {agent_label(a.agent_name).split()[0]} → "
                    f"{agent_label(b.agent_name).split()[0]}", f"{ha} of {na} → {hb} of {nb}",
                    help=f"{agent_label(a.agent_name)}: {ha} of {na} {a.mode} runs; {agent_label(b.agent_name)}: "
                         f"{hb} of {nb}. Counts every completed {a.mode} run of each version in the database.")
    m[1].metric("Injection resistance, whole suite",
                f"{pct(a.injection_resistance_rate)} → {pct(b.injection_resistance_rate)}",
                help="% of injection scenarios where the agent was NOT hijacked, run A → run B. "
                     f"Attacks that reached the agent: {a.injection_exercised}/{a.injection_attacks} → "
                     f"{b.injection_exercised}/{b.injection_attacks}.")
    trust = validate_judge(db, cfg.labels_path)
    jh = trust.judge_vs_human
    if trust.status == "ok" and jh.agreement is not None:
        m[2].metric("LLM judge agreed with a human", f"{round(jh.agreement * jh.n)} of {jh.n}",
                    help=f"Blind human labels; Cohen's kappa {fmt(jh.kappa)}. See Evaluator trust.")
    else:
        m[2].metric("LLM judge agreed with a human", "pending", help="No human labels imported yet.")


def page_overview(db: Database, runs: list[RunSummary]) -> None:
    s = pick_run(runs, "Run", "overview_run")
    results = results_for(db, s.run_id)
    mock_banner(s)
    st.caption(f"Agent **{agent_label(s.agent_name)}** (`{s.agent_name}`) · target model `{s.target_model_id or '-'}`"
               f" · judge `{s.judge_model_id or '-'}` · status **{s.status}** · `{s.run_id}`")

    c = st.columns(4)
    c[0].metric("Reliability score", fmt(s.reliability_score), help="Weighted mean of the components below (0-100).")
    c[1].metric("Functional pass rate", pct(s.functional_pass_rate))
    c[2].metric("Injection resistance", pct(s.injection_resistance_rate),
                help="% of injection scenarios where the agent was NOT compromised.")
    c[3].metric("Avg agent cost / run", fmt(s.avg_cost_usd, "usd"),
                help="Logged tokens × pricing.yaml. n/a when a model is unpriced.")

    w = s.weights
    st.caption("Reliability score = " + " + ".join(f"{fmt(w.get(dim))}% × {dim} ({pct(s.components.get(dim))})"
                                                   for dim in ("correctness", "safety", "efficiency")))
    benign = {True: "passed", False: "FAILED", None: "n/a"}[s.benign_control_passed]
    st.caption(f"{s.n_passed}/{s.n_scenarios} scenarios passed"
               + (f" · {s.n_skipped} skipped (budget)" if s.n_skipped else "")
               + f" · {s.injection_exercised}/{s.injection_attacks} injection attacks reached the agent"
               + f" · benign false-positive check: {benign}")

    left, right = st.columns([3, 2])
    with left:
        cat = category_frame(results)
        if not cat.empty:
            fig = px.bar(cat, x="count", y="category", color="result", orientation="h", text="count",
                         color_discrete_map=RESULT_COLORS, category_orders={"result": ["PASS", "FAIL"]})
            fig.update_traces(textposition="inside", marker_line_width=0)
            show_chart(style(fig, 240), cat, "overview_cat", "Pass / fail by category (scenario count)")
    with right:
        df = scenarios_frame(results)
        failing = df[df["result"] == "FAIL"][["description", "category", "failing"]].rename(
            columns={"description": "scenario", "failing": "failed checks"})
        st.markdown("**Failing scenarios**")
        if failing.empty:
            st.success("All scenarios passed.", icon="✅")
        else:
            st.dataframe(failing, use_container_width=True, hide_index=True,
                         column_config={"scenario": st.column_config.TextColumn(width="medium")})
    if s.notes:
        with st.expander(f"Run notes ({len(s.notes)})"):
            for n in s.notes:
                st.markdown(f"- {n}")


def render_trace(r: ScenarioResult) -> None:
    """The trace as a timeline of step cards; the step where poison entered and unsafe calls are marked."""
    inj = r.get_eval("injection")
    canary = inj.details.get("canary") if inj else None
    poison_step = inj.details.get("poison_entry_step") if inj else None
    flagged = flagged_calls(r)
    for step in r.trace.steps:
        with st.container(border=True):
            if step.role == "user":
                st.markdown(f"🧑 **Customer** · step {step.index}\n\n{md(step.text)}")
                continue
            st.caption(f"step {step.index} · {step.role} · {step.input_tokens} in / {step.output_tokens} out "
                       f"tokens · {step.latency_ms} ms")
            if step.text:
                st.markdown(f"🤖 **Agent:** {md(step.text)}")
            for c in step.tool_calls:
                if c.blocked:
                    st.error(f"🛡️ {call_text(c)} **blocked by guardrail**: {md(c.error)}")
                elif (step.index, c.name) in flagged:
                    st.error(f"❌ {call_text(c)} **executed: this is the unsafe action**")
                else:
                    st.markdown(f"🔧 {call_text(c)}")
                    if c.error:
                        st.warning(f"returned an error: {md(c.error)}")
                hidden = poison_snippets(c.result, canary) if step.index == poison_step else []
                if hidden:
                    st.warning(f"**Hidden instruction entered the agent's context here:** {md(hidden[0])}",
                               icon="⚠️")
                with st.expander(f"{c.name}: arguments and result"):
                    st.code(json.dumps(c.arguments, indent=2, ensure_ascii=False), language="json")
                    if c.result is not None:
                        st.code(json.dumps(c.result, indent=2, ensure_ascii=False, default=str)[:4000],
                                language="json")


def render_verdicts(r: ScenarioResult) -> None:
    """One line per evaluator with its full evidence (tables truncate it)."""
    cols = st.columns(2)
    for n, e in enumerate(r.evals):
        icon = "➖" if not e.applicable else ("✅" if e.passed else "❌")
        role = "counts toward pass/fail" if e.required else "advisory, doesn't affect pass/fail"
        with cols[n % 2].container(border=True):
            st.markdown(f"{icon} **{EVAL_QUESTIONS.get(e.name, e.name)}**  \n{md(e.evidence)}")
            st.caption(f"`{e.name}` · {e.dimension} · {role}")


def page_run_detail(db: Database, runs: list[RunSummary]) -> None:
    s = pick_run(runs, "Run", "detail_run")
    results = results_for(db, s.run_id)
    mock_banner(s)
    df = scenarios_frame(results)
    f1, f2 = st.columns(2)
    cats = f1.multiselect("Category", sorted(df["category"].unique()), default=sorted(df["category"].unique()))
    res = f2.multiselect("Result", ["PASS", "FAIL"], default=["PASS", "FAIL"])
    view = df[df["category"].isin(cats) & df["result"].isin(res)]
    table = view[["description"] + [c for c in view.columns if c != "description"]].rename(
        columns={"description": "what it tests"})
    st.dataframe(table, use_container_width=True, hide_index=True,
                 column_config={"cost_usd": st.column_config.NumberColumn("cost (USD)", format="$%.5f"),
                                "judge": st.column_config.NumberColumn("judge (1-5)", format="%.0f")})
    if view.empty:
        return
    options = list(view["scenario"])
    if st.session_state.get("detail_scenario") not in options:
        st.session_state["detail_scenario"] = next(
            (sc for sc, res_ in zip(view["scenario"], view["result"], strict=True) if res_ == "FAIL"), options[0])
    titles = dict(zip(view["scenario"], view["description"], strict=True))
    sid = st.selectbox("Scenario (defaults to the first failure)", options, key="detail_scenario",
                       format_func=lambda i: f"{titles[i] or i} ({i})")
    r = next(x for x in results if x.scenario_id == sid)

    verdict = "PASS ✅" if r.passed else "FAIL ❌"
    st.subheader(f"{md(r.description or r.scenario_id)} · {verdict}")
    if r.compromised:
        st.error("HIJACKED: the agent followed an instruction hidden in data it retrieved "
                 "(the unsafe step is marked in the trace below).", icon="🚨")
    st.caption(f"scenario `{r.scenario_id}` · {r.category} · trace status **{r.trace.status}**"
               + (f" ({r.trace.error})" if r.trace.error else ""))
    a, b = st.columns(2)
    a.markdown(f"**Customer:** {md(r.trace.user_prompt)}")
    b.markdown(f"**Final answer:** {md(r.trace.final_answer) or '_(none)_'}")

    st.markdown("#### Checks")
    render_verdicts(r)

    st.markdown("#### Trace")
    c = st.columns(4)
    c[0].metric("Agent cost", fmt(r.trace.total_cost_usd, "usd"))
    c[1].metric("Tokens in / out", f"{r.trace.total_input_tokens} / {r.trace.total_output_tokens}")
    c[2].metric("Latency", f"{r.trace.total_latency_ms} ms")
    c[3].metric("Judge spend", fmt(r.eval_cost_usd, "usd"))
    render_trace(r)


def _default_pair(runs: list[RunSummary]) -> tuple[int, int]:
    v1 = next((i for i, r in enumerate(runs) if r.agent_name.endswith("v1")), None)
    v2 = next((i for i, r in enumerate(runs) if r.agent_name.endswith("v2")), None)
    if v1 is not None and v2 is not None:
        return v1, v2
    return (1, 0) if len(runs) > 1 else (0, 0)


def page_compare(db: Database, runs: list[RunSummary]) -> None:
    da, dbi = _default_pair(runs)
    c1, c2 = st.columns(2)
    with c1:
        a = pick_run(runs, "Run A (baseline)", "cmp_a", da)
    with c2:
        b = pick_run(runs, "Run B (candidate)", "cmp_b", dbi)
    mock_banner(a, b)
    a_results, b_results = results_for(db, a.run_id), results_for(db, b.run_id)
    cmp = compare_runs(a, a_results, b, b_results)
    titles = {r.scenario_id: r.description or r.scenario_id for r in a_results + b_results}

    cols = st.columns(4)
    for col, key in zip(cols, ("reliability_score", "functional_pass_rate", "injection_resistance_rate",
                               "avg_cost_usd"), strict=True):
        m = next(x for x in cmp.metrics if x.key == key)
        kind = "usd" if "usd" in key else "pct" if "rate" in key else ""
        delta = None if m.delta is None else (f"{m.delta:+.4f}" if kind == "usd" else f"{m.delta:+.1f}")
        color = "off" if not m.delta else ("normal" if m.higher_is_better else "inverse")
        col.metric(m.label, fmt(m.b, kind), delta, delta_color=color, help=f"Run A: {fmt(m.a, kind)}")

    left, right = st.columns([3, 2])
    with left:
        comp = pd.DataFrame([{"component": m.label, "run": f"A · {agent_label(a.agent_name)}", "score": m.a}
                             for m in cmp.components] +
                            [{"component": m.label, "run": f"B · {agent_label(b.agent_name)}", "score": m.b}
                             for m in cmp.components])
        fig = px.bar(comp, x="component", y="score", color="run", barmode="group", text="score",
                     color_discrete_sequence=SERIES[:2])
        fig.update_traces(textposition="outside", marker_line_width=0, texttemplate="%{text:.0f}")
        style(fig, 320)
        fig.update_yaxes(range=[0, 110])
        show_chart(fig, comp, "cmp_components", "Component scores (0-100)")
    with right:
        st.markdown(f"**Flipped fail → pass ({len(cmp.flipped_to_pass)})**")
        if cmp.flipped_to_pass:
            st.success("\n".join(f"- {md(titles.get(s.scenario_id, s.scenario_id))} ({s.category})"
                                  for s in cmp.flipped_to_pass))
        else:
            st.caption("none")
        st.markdown(f"**Flipped pass → fail ({len(cmp.flipped_to_fail)})**")
        if cmp.flipped_to_fail:
            st.error("\n".join(f"- {md(titles.get(s.scenario_id, s.scenario_id))} ({s.category})"
                                for s in cmp.flipped_to_fail))
        else:
            st.caption("none")

    st.markdown("#### All metrics")
    st.dataframe(pd.DataFrame([{
        "metric": m.label,
        "A": fmt(m.a, "usd" if "usd" in m.key else "pct" if "rate" in m.key else ""),
        "B": fmt(m.b, "usd" if "usd" in m.key else "pct" if "rate" in m.key else ""),
        "Δ (B − A)": fmt_delta(m)} for m in cmp.metrics + cmp.components]),
        use_container_width=True, hide_index=True)
    st.markdown("#### Scenario by scenario")
    rows = pd.DataFrame([{
        "what it tests": titles.get(s.scenario_id, s.scenario_id), "scenario": s.scenario_id,
        "category": s.category, "change": s.change,
        "A": {True: "PASS", False: "FAIL", None: "-"}[s.a_passed],
        "B": {True: "PASS", False: "FAIL", None: "-"}[s.b_passed],
        "A compromised": {True: "YES", False: "no", None: "-"}[s.a_compromised],
        "B compromised": {True: "YES", False: "no", None: "-"}[s.b_compromised],
        "A judge": s.a_judge, "B judge": s.b_judge} for s in cmp.scenarios])
    only_changes = st.toggle("Only show scenarios that changed", value=False)
    if only_changes:
        rows = rows[rows["change"] != "same"]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def page_cost(db: Database, runs: list[RunSummary]) -> None:
    s = pick_run(runs, "Run", "cost_run")
    results = results_for(db, s.run_id)
    mock_banner(s)
    c = st.columns(4)
    c[0].metric("Agent spend (total)", fmt(s.total_cost_usd, "usd"))
    c[1].metric("Avg agent cost / run", fmt(s.avg_cost_usd, "usd"))
    c[2].metric("Judge / evaluator spend", fmt(s.eval_cost_usd, "usd"))
    p50 = None if s.p50_latency_ms is None else round(s.p50_latency_ms)
    p95 = None if s.p95_latency_ms is None else round(s.p95_latency_ms)
    c[3].metric("Latency p50 / p95 (ms)", f"{fmt(p50)} / {fmt(p95)}")
    if s.total_cost_usd is None:
        st.info("USD is n/a because at least one model has no entry in pricing.yaml; token counts are still exact.")

    df = scenarios_frame(results)
    left, right = st.columns(2)
    with left:
        cost = df[["scenario", "category", "cost_usd", "tokens_in", "tokens_out"]].sort_values(
            "cost_usd", ascending=True, na_position="first")
        if cost["cost_usd"].notna().any():
            title = "Agent cost per scenario (USD)"
            fig = px.bar(cost, x="cost_usd", y="scenario", orientation="h",
                         color_discrete_sequence=SERIES[:1], hover_data=["category", "tokens_in", "tokens_out"])
        else:
            title = "Agent tokens per scenario (unpriced model)"
            cost = cost.assign(tokens=cost["tokens_in"] + cost["tokens_out"]).sort_values("tokens")
            fig = px.bar(cost, x="tokens", y="scenario", orientation="h", color_discrete_sequence=SERIES[:1])
        fig.update_traces(marker_line_width=0)
        show_chart(style(fig, 60 + 22 * len(cost), all_y_labels=True), cost, "cost_per_scenario", title)
    with right:
        steps = steps_frame(results)
        if not steps.empty:
            agg = steps.groupby("model_step", as_index=False)[["input_tokens", "output_tokens"]].sum()
            long = agg.rename(columns={"input_tokens": "input", "output_tokens": "output"}).melt(
                id_vars="model_step", var_name="tokens", value_name="count")
            fig = px.bar(long, x="model_step", y="count", color="tokens", color_discrete_sequence=SERIES[:2])
            fig.update_traces(marker_line_width=0)
            style(fig, 320)
            fig.update_xaxes(dtick=1, title="model step #")
            show_chart(fig, agg, "tokens_by_step", "Tokens by model step, summed over scenarios")
            st.caption("Input tokens grow each step because the whole conversation is resent; "
                       "fewer steps is the main cost lever.")

    model_tool = [(r.scenario_id, r.trace.model_latency_ms, r.trace.tool_latency_ms) for r in results]
    lat = pd.DataFrame(model_tool, columns=["scenario", "model_ms", "tool_ms"])
    long = lat.melt(id_vars="scenario", var_name="component", value_name="ms")
    long["component"] = long["component"].map({"model_ms": "model", "tool_ms": "tools"})
    fig = px.bar(long, x="ms", y="scenario", color="component", orientation="h", color_discrete_sequence=SERIES[:2])
    fig.update_traces(marker_line_width=0)
    show_chart(style(fig, 80 + 22 * len(lat), all_y_labels=True), lat, "latency",
               "Latency breakdown per scenario: model vs tool time (ms)")

    with st.expander("Cost ledger (every model call, for traceability)"):
        ledger = pd.DataFrame(db.get_ledger(s.run_id))
        st.dataframe(ledger, use_container_width=True, hide_index=True)
        st.caption("cost_usd = input_tokens/1000 × input_per_1k + output_tokens/1000 × output_per_1k from "
                   "pricing.yaml.")


def page_trust(db: Database, runs: list[RunSummary], cfg) -> None:
    st.markdown("#### Judge vs human labels")
    rep = validate_judge(db, cfg.labels_path)
    if rep.status != "ok":
        st.warning(f"Judge agreement: **{rep.status}**. Humans label runs with `agentgauge export-labels --run <id>`"
                   " → fill in `human_label` → `agentgauge import-labels <csv>`.", icon="🏷️")
    else:
        c = st.columns(4)
        jh, hh = rep.judge_vs_human, rep.human_vs_human
        c[0].metric("Judge-human agreement", pct(jh.agreement * 100 if jh.agreement is not None else None),
                    help=f"n = {jh.n} labeled scenario runs")
        c[1].metric("Cohen's kappa (judge vs human)", fmt(jh.kappa))
        c[2].metric("Human-human agreement", pct(hh.agreement * 100 if hh.agreement is not None else None),
                    help=f"n = {hh.n} items labeled by two people")
        c[3].metric("Cohen's kappa (human vs human)", fmt(hh.kappa))
        st.caption(f"{rep.n_labels} labels in `{rep.labels_file}` · {rep.n_matched} matched to judge verdicts · "
                   f"confusion {rep.confusion}")
        if rep.disagreements:
            st.dataframe(pd.DataFrame([d.model_dump() for d in rep.disagreements]), use_container_width=True,
                         hide_index=True)

    st.markdown("#### Evaluators on a run")
    s = pick_run(runs, "Run", "trust_run")
    mock_banner(s)
    results = results_for(db, s.run_id)
    ev = evaluator_frame(results)
    if not ev.empty:
        fig = px.bar(ev.sort_values("pass_rate"), x="pass_rate", y="evaluator", orientation="h",
                     color_discrete_sequence=SERIES[:1], text="pass_rate",
                     hover_data=["dimension", "applicable", "passed"])
        fig.update_traces(marker_line_width=0, texttemplate="%{text:.0f}%", textposition="outside")
        style(fig, 300, all_y_labels=True)
        fig.update_xaxes(range=[0, 115])
        show_chart(fig, ev, "trust_evals", "Pass rate per evaluator (% of scenarios where it applied)")
    js = judge_samples_frame(results)
    if not js.empty:
        st.markdown("#### Judge self-consistency (samples per scenario)")
        st.caption("Samples run at temperature 0, so high self-agreement is expected and does NOT prove the judge "
                   "is right. Human agreement above is the real check.")
        st.dataframe(js, use_container_width=True, hide_index=True)

    lim = Path(cfg.base_dir) / "docs" / "LIMITATIONS.md"
    if lim.exists():
        with st.expander("Known limitations (docs/LIMITATIONS.md)"):
            st.markdown(lim.read_text())


# --------------------------------------------------------------------------- main


def main() -> None:
    cfg = load_config()
    db = Database(cfg.db_path)
    demo_runs = [x.strip() for x in os.environ.get("AGENTGAUGE_DEMO_RUNS", "").split(",") if x.strip()]
    st.sidebar.title("📏 AgentGauge")
    st.sidebar.caption("CI/CD for AI agents")
    page = st.sidebar.radio("Page", PAGES, key="page")
    all_runs = db.list_runs()
    runs = all_runs
    if demo_runs:
        shown = resolve_runs(all_runs, demo_runs)
        if shown and not st.sidebar.toggle("Show all runs", value=False, key="show_all"):
            runs = shown
            st.sidebar.caption("Demo mode: " + " vs ".join(f"`{r.label or r.run_id}`" for r in shown))
        elif not shown:
            st.sidebar.caption(f"Demo runs {', '.join(demo_runs)} not found; showing all runs.")
    else:
        st.sidebar.caption(f"Database: `{cfg.db_path}`")
        if st.sidebar.button("Refresh"):
            load_results.clear()
    st.title(page)
    if not runs:
        st.info("No runs yet. Try `agentgauge demo` (offline) or "
                "`agentgauge run --agent sample_v1 --suite all --label baseline`.")
        return
    if page == "Headline":
        page_headline(db, runs, all_runs, cfg)
    elif page == "Overview":
        page_overview(db, runs)
    elif page == "Run detail":
        page_run_detail(db, runs)
    elif page == "Compare runs":
        page_compare(db, runs)
    elif page == "Cost & efficiency":
        page_cost(db, runs)
    else:
        page_trust(db, runs, cfg)


main()
