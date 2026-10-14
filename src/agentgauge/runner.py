"""Scenario runner: drives an agent through scenarios, evaluates, enforces budgets, stores results."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from agentgauge.adapters import AGENT_ALIASES, AgentAdapter, ReplayAdapter, SampleAgent
from agentgauge.config import BudgetConfig, Config
from agentgauge.cost import CostTracker, ModelPrice, Pricing
from agentgauge.db import Database
from agentgauge.evaluators import EvalContext, all_evaluators, run_evaluators
from agentgauge.evaluators.judge import JUDGE_SYSTEM, build_judge_prompt
from agentgauge.mock_policies import HeuristicJudge, LexicalSupportJudge, SimulatedSupportAgent
from agentgauge.model_client import MOCK_MODEL_ID, MockClient, ModelClient, approx_tokens, make_client
from agentgauge.models import RunMode, RunSummary, Scenario, ScenarioResult, Trace
from agentgauge.sandbox import Sandbox
from agentgauge.scenarios import load_scenarios
from agentgauge.scoring import scenario_compromised, scenario_passed, summarize

Progress = Callable[[int, int, Scenario, ScenarioResult], None]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_run_id() -> str:
    return f"run_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"


# --------------------------------------------------------------------------- construction


def resolve_agent_version(alias: str) -> str:
    try:
        return AGENT_ALIASES[alias]
    except KeyError:
        raise ValueError(f"Unknown agent '{alias}'. Use sample_v1 or sample_v2.") from None


class _JudgeRouter:
    """Mock judge policy that answers judge prompts and groundedness prompts."""

    def __init__(self) -> None:
        self.judge, self.ground = HeuristicJudge(), LexicalSupportJudge()

    def __call__(self, system, messages, tools):
        if system == JUDGE_SYSTEM:
            return self.judge(system, messages, tools)
        return self.ground(system, messages, tools)


def build_target_client(config: Config, mock: bool) -> ModelClient:
    return make_client(config.models.target_model_id, mock=mock, region=config.models.region,
                       policy=SimulatedSupportAgent() if mock else None)


def build_judge_client(config: Config, mock: bool) -> ModelClient:
    return make_client(config.models.judge_model_id, mock=mock, region=config.models.region,
                       policy=_JudgeRouter() if mock else None)


def build_agent(alias: str, client: ModelClient, config: Config) -> SampleAgent:
    return SampleAgent(client, version=resolve_agent_version(alias), max_steps=config.agent.max_agent_steps,
                       max_tokens=config.budget.max_tokens_per_call, temperature=config.agent.temperature)


# --------------------------------------------------------------------------- evaluation


def evaluate_trace(trace: Trace, scenario: Scenario, config: Config, run_id: str, *,
                   judge_client: ModelClient | None, tracker: CostTracker) -> ScenarioResult:
    before = (tracker.session.priced_cost_usd, tracker.session.input_tokens, tracker.session.output_tokens,
              tracker.session.unpriced_calls)
    ctx = EvalContext(config=config, sandbox=Sandbox(scenario.setup), judge_client=judge_client, tracker=tracker)
    evals = run_evaluators(trace, scenario, ctx, all_evaluators())
    s = tracker.session
    unpriced = s.unpriced_calls - before[3]
    return ScenarioResult(
        run_id=run_id, scenario_id=scenario.id, category=scenario.category, description=scenario.description,
        tags=scenario.tags, trace=trace, evals=evals,
        passed=scenario_passed(trace.status, evals),
        compromised=scenario_compromised(scenario.category, evals),
        eval_cost_usd=None if unpriced else round(s.priced_cost_usd - before[0], 8),
        eval_input_tokens=s.input_tokens - before[1], eval_output_tokens=s.output_tokens - before[2],
    )


# --------------------------------------------------------------------------- suite execution


def execute(
    config: Config, agent: AgentAdapter, scenarios: list[Scenario], *, mode: RunMode, suite: str,
    label: str | None, judge_client: ModelClient | None, tracker: CostTracker, db: Database | None,
    progress: Progress | None = None, run_id: str | None = None,
) -> RunSummary:
    run_id = run_id or new_run_id()
    summary = RunSummary(
        run_id=run_id, label=label, agent_name=agent.name, mode=mode, suite=suite, status="RUNNING",
        created_at=now_iso(), target_model_id=agent.model_id,
        judge_model_id=judge_client.model_id if judge_client else None,
    )
    if db:
        db.save_run(summary, config.snapshot())

    results: list[ScenarioResult] = []
    status = "COMPLETED"
    for i, scenario in enumerate(scenarios, 1):
        if tracker.session_exhausted:
            status = "ABORTED_BUDGET"
            summary.notes.append(f"Session budget exhausted: {len(scenarios) - i + 1} scenario(s) not run.")
            break
        tracker.current_scenario = scenario.id
        if isinstance(agent, ReplayAdapter):
            trace = agent.run(scenario)
        else:
            trace = agent.run(scenario, Sandbox(scenario.setup), tracker)
        result = evaluate_trace(trace, scenario, config, run_id, judge_client=judge_client, tracker=tracker)
        results.append(result)
        if db:
            db.save_result(result)
        if progress:
            progress(i, len(scenarios), scenario, result)
    tracker.current_scenario = None

    summary.status = status
    summary.n_skipped = len(scenarios) - len(results)
    summary.finished_at = now_iso()
    summary.notes += tracker.warnings
    if mode == "mock":
        summary.notes.insert(0, "MOCK RUN: simulated rule-based model, not real model behavior.")
    final = summarize(summary, results, config.scoring.weights)
    if db:
        db.save_run(final)
        db.save_ledger(run_id, tracker.ledger)
    return final


def run_agent_suite(
    config: Config, agent_alias: str, *, suite: str = "all", label: str | None = None, mock: bool = False,
    use_judge: bool = True, scenario_ids: list[str] | None = None, db: Database | None = None,
    progress: Progress | None = None, tracker: CostTracker | None = None,
) -> RunSummary:
    scenarios = load_scenarios(config.scenarios_path, suite, scenario_ids)
    if not scenarios:
        raise ValueError(f"No scenarios found for suite '{suite}' in {config.scenarios_path}")
    tracker = tracker or CostTracker(Pricing.load(config.pricing_path), config.budget)
    agent = build_agent(agent_alias, build_target_client(config, mock), config)
    judge = build_judge_client(config, mock) if use_judge and config.judge.enabled else None
    return execute(config, agent, scenarios, mode="mock" if mock else "live", suite=suite, label=label,
                   judge_client=judge, tracker=tracker, db=db, progress=progress)


def run_replay(
    config: Config, traces: list[Trace], *, label: str | None = None, mock: bool = False, use_judge: bool = True,
    db: Database | None = None, progress: Progress | None = None,
) -> RunSummary:
    """Score externally supplied traces. Scenario expectations come from the scenario library when the
    trace's scenario_id matches; otherwise an ad-hoc scenario with generic checks is used."""
    pricing = Pricing.load(config.pricing_path)
    library = {s.id: s for s in load_scenarios(config.scenarios_path, "all")} if config.scenarios_path.exists() else {}
    scenarios: list[Scenario] = []
    adhoc = 0
    for t in traces:
        if t.total_cost_usd is None and t.model_id:
            cost = pricing.cost(t.model_id, t.total_input_tokens, t.total_output_tokens)
            if cost is not None:
                t.total_cost_usd = round(cost, 8)
                t.metadata["cost_source"] = "computed from logged tokens and pricing.yaml"
        if t.scenario_id in library:
            scenarios.append(library[t.scenario_id])
        else:
            adhoc += 1
            scenarios.append(Scenario(id=t.scenario_id, category="functional", user_prompt=t.user_prompt,
                                      description="Ad-hoc scenario (no matching scenario file): generic checks only",
                                      tags=["adhoc"]))
    tracker = CostTracker(pricing, config.budget)
    judge = build_judge_client(config, mock) if use_judge and config.judge.enabled else None
    summary = execute(config, ReplayAdapter(traces), scenarios, mode="replay", suite="replay", label=label,
                      judge_client=judge, tracker=tracker, db=db, progress=progress)
    if adhoc:
        summary.notes.append(f"{adhoc} trace(s) had no matching scenario file and used generic expectations.")
        if db:
            db.save_run(summary)
    return summary


# --------------------------------------------------------------------------- cost dry run


AGENT_OUTPUT_FLOOR = 150  # mock answers are terse; assume real models write at least this much per call
JUDGE_OUTPUT_TOKENS = 200


class CostEstimate(BaseModel):
    scenarios: int
    agent_calls: int = 0
    agent_input_tokens: int = 0
    agent_output_tokens: int = 0
    judge_calls: int = 0
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0
    target_model_id: str | None = None
    judge_model_id: str | None = None
    target_priced: bool = False
    judge_priced: bool = True
    safety_factor: float = 2.0
    agent_usd: float | None = None
    judge_usd: float | None = None
    expected_usd: float | None = None  # includes the safety factor
    max_scenario_usd: float | None = None
    session_cap_usd: float = 0.0
    warnings: list[str] = Field(default_factory=list)

    @property
    def within_session_cap(self) -> bool | None:
        return None if self.expected_usd is None else self.expected_usd <= self.session_cap_usd


def estimate_cost(config: Config, agent_alias: str, scenarios: list[Scenario], *, use_judge: bool = True,
                  pricing: Pricing | None = None) -> CostEstimate:
    """Dry run: replay the suite with the offline mock model, measure tokens, price them at live rates."""
    pricing = pricing or Pricing.load(config.pricing_path)
    f = config.budget.estimate_safety_factor
    target_id, judge_id = config.models.target_model_id, config.models.judge_model_id
    judging = use_judge and config.judge.enabled
    est = CostEstimate(scenarios=len(scenarios), target_model_id=target_id, judge_model_id=judge_id,
                       safety_factor=f, session_cap_usd=config.budget.max_usd_per_session,
                       target_priced=pricing.get(target_id) is not None,
                       judge_priced=(pricing.get(judge_id) is not None) if judging else True)
    agent = build_agent(agent_alias, MockClient(policy=SimulatedSupportAgent()), config)
    # The dry run itself is free and unlimited; only its token counts matter.
    dry = CostTracker(Pricing({MOCK_MODEL_ID: ModelPrice(input_per_1k=0, output_per_1k=0)}),
                      BudgetConfig(max_usd_per_run=1e9, max_usd_per_session=1e9,
                                   max_tokens_per_run=10**12, max_tokens_per_session=10**12))
    per_scenario: list[float | None] = []
    agent_usd_total = 0.0
    judge_usd_total = 0.0
    for sc in scenarios:
        trace = agent.run(sc, Sandbox(sc.setup), dry)
        sc_in = sc_out = 0
        for step in trace.model_steps():
            est.agent_calls += 1
            sc_in += step.input_tokens
            sc_out += max(step.output_tokens, AGENT_OUTPUT_FLOOR)
        est.agent_input_tokens += sc_in
        est.agent_output_tokens += sc_out
        a_cost = pricing.cost(target_id, sc_in, sc_out)
        j_cost: float | None = 0.0
        if judging:
            calls = config.judge.samples + (1 if config.groundedness.mode == "llm" else 0)
            j_in = (approx_tokens(JUDGE_SYSTEM) + approx_tokens(build_judge_prompt(trace, sc))) * calls
            j_out = JUDGE_OUTPUT_TOKENS * calls
            est.judge_calls += calls
            est.judge_input_tokens += j_in
            est.judge_output_tokens += j_out
            j_cost = pricing.cost(judge_id, j_in, j_out)
        if a_cost is not None:
            agent_usd_total += a_cost
        if j_cost is not None:
            judge_usd_total += j_cost
        per_scenario.append(None if a_cost is None else a_cost * f)

    if est.target_priced:
        est.agent_usd = round(agent_usd_total * f, 6)
        est.max_scenario_usd = round(max(c for c in per_scenario if c is not None), 6) if per_scenario else 0.0
        if est.max_scenario_usd > config.budget.max_usd_per_run:
            est.warnings.append(f"Largest scenario estimate ${est.max_scenario_usd:.4f} exceeds max_usd_per_run "
                                f"${config.budget.max_usd_per_run}: some runs may end ABORTED_BUDGET.")
    else:
        est.warnings.append(f"Target model '{target_id}' has no price in pricing.yaml: USD estimate unavailable.")
    if judging:
        if est.judge_priced:
            est.judge_usd = round(judge_usd_total * f, 6)
        else:
            est.warnings.append(f"Judge model '{judge_id}' has no price in pricing.yaml: USD estimate unavailable.")
    if est.target_priced and est.judge_priced:
        est.expected_usd = round((est.agent_usd or 0) + (est.judge_usd or 0), 6)
    return est
