"""Evaluator pipeline."""

from __future__ import annotations

from agentgauge.evaluators.answer_content import AnswerContentEvaluator
from agentgauge.evaluators.base import EvalContext, Evaluator
from agentgauge.evaluators.efficiency import EfficiencyEvaluator
from agentgauge.evaluators.injection import InjectionCanaryEvaluator
from agentgauge.evaluators.redundancy import RedundancyEvaluator
from agentgauge.evaluators.scope import ScopeEvaluator
from agentgauge.evaluators.tool_calls import ToolCallEvaluator
from agentgauge.models import EvalResult, Scenario, Trace


def deterministic_evaluators() -> list[Evaluator]:
    return [
        ToolCallEvaluator(),
        AnswerContentEvaluator(),
        ScopeEvaluator(),
        InjectionCanaryEvaluator(),
        RedundancyEvaluator(),
        EfficiencyEvaluator(),
    ]


def run_evaluators(trace: Trace, scenario: Scenario, ctx: EvalContext,
                   evaluators: list[Evaluator]) -> list[EvalResult]:
    return [ev.evaluate(trace, scenario, ctx) for ev in evaluators]


__all__ = [
    "AnswerContentEvaluator",
    "EfficiencyEvaluator",
    "EvalContext",
    "Evaluator",
    "InjectionCanaryEvaluator",
    "RedundancyEvaluator",
    "ScopeEvaluator",
    "ToolCallEvaluator",
    "deterministic_evaluators",
    "run_evaluators",
]
