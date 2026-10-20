"""Core pydantic data models: traces, scenarios, evaluator results and run summaries.

The Trace/Step/ToolCall models follow the project spec (section 7). Fields marked
"extension" were added because other parts of the spec need them (see docs/DECISIONS.md).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["functional", "injection", "efficiency"]
TraceStatus = Literal["OK", "STEP_LIMIT", "ABORTED_BUDGET", "ERROR"]
Dimension = Literal["correctness", "safety", "efficiency"]
RunMode = Literal["live", "mock", "replay"]
RunStatus = Literal["RUNNING", "COMPLETED", "ABORTED_BUDGET", "ERROR"]

CATEGORIES: tuple[str, ...] = ("functional", "injection", "efficiency")
DIMENSIONS: tuple[str, ...] = ("correctness", "safety", "efficiency")


# --------------------------------------------------------------------------- traces


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    error: str | None = None
    latency_ms: int = 0
    # extension: section 6 requires every tool call to be timestamped
    timestamp: str | None = None
    # extension: True when a guardrail (e.g. the v2 argument validator) refused to execute the call
    blocked: bool = False

    def args_json(self) -> str:
        return json.dumps(self.arguments, sort_keys=True, default=str)


class Step(BaseModel):
    index: int
    role: Literal["assistant", "tool", "user"]
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


class Trace(BaseModel):
    trace_id: str
    agent_name: str  # e.g. "sample_agent_v1"
    scenario_id: str
    user_prompt: str
    steps: list[Step]
    final_answer: str | None = None
    status: TraceStatus
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float | None = None
    total_latency_ms: int = 0
    # extension: needed so every cost figure is traceable to a pricing.yaml entry
    model_id: str | None = None
    # extension: error message when status == "ERROR" (or abort reason)
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def all_tool_calls(self) -> list[tuple[int, ToolCall]]:
        """Every tool call (executed or blocked) with the index of the step it belongs to."""
        return [(s.index, tc) for s in self.steps for tc in s.tool_calls]

    def executed_tool_calls(self) -> list[tuple[int, ToolCall]]:
        """Tool calls that actually ran (not blocked by a guardrail)."""
        return [(i, tc) for i, tc in self.all_tool_calls() if not tc.blocked]

    def blocked_tool_calls(self) -> list[tuple[int, ToolCall]]:
        return [(i, tc) for i, tc in self.all_tool_calls() if tc.blocked]

    def model_steps(self) -> list[Step]:
        """Steps produced by the model (role == assistant). This is what max_steps counts."""
        return [s for s in self.steps if s.role == "assistant"]

    @property
    def tool_latency_ms(self) -> int:
        return sum(tc.latency_ms for _, tc in self.all_tool_calls())

    @property
    def model_latency_ms(self) -> int:
        return sum(s.latency_ms for s in self.model_steps())


# --------------------------------------------------------------------------- scenarios


class ToolCallExpectation(BaseModel):
    """A tool call pattern. args_contain is a subset match (see evaluators.base.args_match)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    args_contain: dict[str, Any] = Field(default_factory=dict)


class ScenarioSetup(BaseModel):
    """How the sandbox is modified for a scenario. "{canary}" in any string is replaced by `canary`."""

    model_config = ConfigDict(extra="forbid")

    poisoned_doc_id: str | None = None  # help doc that receives `payload` (appended)
    payload: str | None = None
    canary: str | None = None
    doc_overrides: dict[str, str] = Field(default_factory=dict)  # doc_id -> full replacement content
    order_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)  # order_id -> fields
    customer_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)  # customer_id -> fields

    @property
    def has_injection(self) -> bool:
        return bool(
            self.canary
            or self.payload
            or self.doc_overrides
            or self.order_overrides
            or self.customer_overrides
        )


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_tool_calls: list[ToolCallExpectation] = Field(default_factory=list)
    forbidden_tool_calls: list[ToolCallExpectation] = Field(default_factory=list)
    # Sensitive tools (issue_refund, send_email) the user explicitly asked for in this scenario.
    allowed_sensitive_tools: list[str] = Field(default_factory=list)
    # Tool names whose first occurrences must appear in this relative order.
    order: list[str] | None = None
    # Each entry is a phrase, or a list of alternative phrases (any one satisfies it).
    final_answer_must_mention: list[str | list[str]] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    max_steps: int | None = None  # counts model (assistant) steps
    max_cost_usd: float | None = None
    max_tool_calls: int | None = None


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: Category
    description: str
    user_prompt: str
    setup: ScenarioSetup = Field(default_factory=ScenarioSetup)
    expected: Expected = Field(default_factory=Expected)
    rubric: str | None = None
    tags: list[str] = Field(default_factory=list)

    @property
    def is_benign_control(self) -> bool:
        return "benign_control" in self.tags


# --------------------------------------------------------------------------- evaluation


class EvalResult(BaseModel):
    name: str
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    evidence: str  # human-readable; references step indexes ("step 3: ...")
    details: dict[str, Any] = Field(default_factory=dict)
    dimension: Dimension = "correctness"
    required: bool = True  # counts toward scenario pass/fail
    applicable: bool = True  # False => shown as N/A, ignored by scoring


class ScenarioResult(BaseModel):
    run_id: str
    scenario_id: str
    category: Category
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    trace: Trace
    evals: list[EvalResult]
    passed: bool
    compromised: bool | None = None  # injection scenarios only
    eval_cost_usd: float | None = 0.0  # judge / groundedness model spend
    eval_input_tokens: int = 0
    eval_output_tokens: int = 0

    def get_eval(self, name: str) -> EvalResult | None:
        return next((e for e in self.evals if e.name == name), None)

    @property
    def judge_score(self) -> float | None:
        ev = self.get_eval("judge")
        if ev is None or not ev.applicable:
            return None
        return ev.details.get("final_score")


class CategoryStats(BaseModel):
    n: int = 0
    passed: int = 0
    pass_rate: float | None = None


class RunSummary(BaseModel):
    run_id: str
    label: str | None = None
    agent_name: str
    mode: RunMode
    suite: str = "all"
    status: RunStatus = "RUNNING"
    created_at: str
    finished_at: str | None = None
    target_model_id: str | None = None
    judge_model_id: str | None = None

    n_scenarios: int = 0
    n_passed: int = 0
    n_skipped: int = 0
    n_errored: int = 0  # trace status ERROR (API failure): counted as not passed, excluded from components
    pass_rate: float | None = None
    by_category: dict[str, CategoryStats] = Field(default_factory=dict)
    functional_pass_rate: float | None = None
    injection_resistance_rate: float | None = None
    injection_scenarios: int = 0
    injection_attacks: int = 0  # injection scenarios excluding benign controls
    injection_exercised: int = 0  # attacks whose poisoned content actually reached the agent
    benign_control_passed: bool | None = None

    total_cost_usd: float | None = None  # agent spend
    avg_cost_usd: float | None = None
    eval_cost_usd: float | None = None  # judge/groundedness spend
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    p50_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    avg_steps: float | None = None

    components: dict[str, float | None] = Field(default_factory=dict)
    weights: dict[str, float] = Field(default_factory=dict)
    reliability_score: float | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def display_name(self) -> str:
        label = f" [{self.label}]" if self.label else ""
        return f"{self.run_id} · {self.agent_name}{label} · {self.mode}"
