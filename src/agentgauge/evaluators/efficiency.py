"""Efficiency: tokens, USD and latency per run and per step; flags runs over their cost budget."""

from __future__ import annotations

from agentgauge.evaluators.base import EvalContext, Evaluator
from agentgauge.models import EvalResult, Scenario, Trace


def fmt_usd(value: float | None) -> str:
    return "unpriced" if value is None else f"${value:.4f}"


class EfficiencyEvaluator(Evaluator):
    name = "efficiency"
    dimension = "efficiency"

    def evaluate(self, trace: Trace, scenario: Scenario, ctx: EvalContext) -> EvalResult:
        exp = scenario.expected
        n_tool_calls = len(trace.all_tool_calls())
        per_step = [
            {"step": s.index, "input_tokens": s.input_tokens, "output_tokens": s.output_tokens,
             "model_latency_ms": s.latency_ms,
             "tool_latency_ms": sum(c.latency_ms for c in s.tool_calls), "tool_calls": len(s.tool_calls)}
            for s in trace.model_steps()
        ]
        details = {
            "input_tokens": trace.total_input_tokens, "output_tokens": trace.total_output_tokens,
            "cost_usd": trace.total_cost_usd, "latency_ms": trace.total_latency_ms,
            "model_latency_ms": trace.model_latency_ms, "tool_latency_ms": trace.tool_latency_ms,
            "tool_calls": n_tool_calls, "max_cost_usd": exp.max_cost_usd,
            "max_tool_calls": exp.max_tool_calls, "per_step": per_step,
        }
        summary = (f"{trace.total_input_tokens} in / {trace.total_output_tokens} out tokens, "
                   f"{fmt_usd(trace.total_cost_usd)}, {trace.total_latency_ms} ms "
                   f"(model {trace.model_latency_ms} ms, tools {trace.tool_latency_ms} ms), "
                   f"{n_tool_calls} tool call(s)")

        failures: list[str] = []
        notes: list[str] = []
        score = 1.0
        if trace.status == "ABORTED_BUDGET":
            failures.append(f"run aborted by budget cap ({trace.error or 'cap exceeded'})")
            score = 0.0
        if exp.max_cost_usd is not None:
            if trace.total_cost_usd is None:
                notes.append(f"cost cap ${exp.max_cost_usd} not checked: model is unpriced in pricing.yaml")
            elif trace.total_cost_usd > exp.max_cost_usd:
                failures.append(f"cost ${trace.total_cost_usd:.4f} > max_cost_usd ${exp.max_cost_usd}")
                score = min(score, exp.max_cost_usd / trace.total_cost_usd)
        if exp.max_tool_calls is not None and n_tool_calls > exp.max_tool_calls:
            failures.append(f"{n_tool_calls} tool calls > max_tool_calls {exp.max_tool_calls}")
            score = min(score, 0.5)

        if failures:
            return self.result(False, score, f"FAIL: {' | '.join(failures)}. {summary}", details)
        evidence = f"PASS: {summary}"
        if notes:
            evidence += ". " + "; ".join(notes)
        return self.result(True, 1.0, evidence, details)
