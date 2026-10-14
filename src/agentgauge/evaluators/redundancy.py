"""Redundancy / looping: repeated identical tool calls and step counts over budget."""

from __future__ import annotations

from collections import defaultdict

from agentgauge.evaluators.base import EvalContext, Evaluator
from agentgauge.models import EvalResult, Scenario, Trace


class RedundancyEvaluator(Evaluator):
    name = "redundancy"
    dimension = "efficiency"

    def evaluate(self, trace: Trace, scenario: Scenario, ctx: EvalContext) -> EvalResult:
        seen: dict[tuple[str, str], list[int]] = defaultdict(list)
        for i, c in trace.all_tool_calls():
            seen[(c.name, c.args_json())].append(i)
        repeats = {k: v for k, v in seen.items() if len(v) > 1}

        n_steps = len(trace.model_steps())
        max_steps = scenario.expected.max_steps
        failures: list[str] = []
        for (name, args), steps in repeats.items():
            failures.append(f"{name}({args}) repeated {len(steps)}x at steps {steps}")
        if max_steps is not None and n_steps > max_steps:
            failures.append(f"{n_steps} model steps > max_steps {max_steps}")
        if trace.status == "STEP_LIMIT":
            failures.append(f"run hit the agent step limit ({n_steps} steps) without finishing")

        duplicate_calls = sum(len(v) - 1 for v in repeats.values())
        over = max(0, n_steps - max_steps) if max_steps is not None else 0
        score = max(0.0, 1.0 - 0.25 * duplicate_calls - 0.25 * over - (0.5 if trace.status == "STEP_LIMIT" else 0))
        details = {"model_steps": n_steps, "max_steps": max_steps, "duplicate_calls": duplicate_calls,
                   "repeats": [{"tool": n, "args": a, "steps": s} for (n, a), s in repeats.items()]}
        if failures:
            return self.result(False, score, "FAIL: " + " | ".join(failures), details)
        limit = f" (max {max_steps})" if max_steps is not None else ""
        return self.result(True, 1.0, f"PASS: {n_steps} model step(s){limit}, no repeated tool calls", details)
