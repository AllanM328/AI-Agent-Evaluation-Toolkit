"""Tool-call validation: required calls, argument matching, schema validity, ordering, unknown tools."""

from __future__ import annotations

from agentgauge.evaluators.base import EvalContext, Evaluator, describe_call, matches
from agentgauge.models import EvalResult, Scenario, Trace
from agentgauge.sandbox import TOOL_SPECS, validate_args


class ToolCallEvaluator(Evaluator):
    name = "tool_calls"
    dimension = "correctness"

    def evaluate(self, trace: Trace, scenario: Scenario, ctx: EvalContext) -> EvalResult:
        exp = scenario.expected
        calls = trace.all_tool_calls()
        executed = trace.executed_tool_calls()
        failures: list[str] = []
        notes: list[str] = []
        checks = 0

        # 1. unknown tools
        unknown = [(i, c) for i, c in calls if c.name not in TOOL_SPECS]
        checks += 1
        for i, c in unknown:
            failures.append(f"{describe_call(i, c)} calls a tool that does not exist")

        # 2. argument schema validity
        checks += 1
        schema_problems: list[dict] = []
        for i, c in calls:
            if c.name not in TOOL_SPECS:
                continue
            problems = validate_args(c.name, c.arguments)
            if problems:
                schema_problems.append({"step": i, "tool": c.name, "problems": problems})
                failures.append(f"{describe_call(i, c)} has invalid arguments: {'; '.join(problems)}")

        # 3. required calls present with matching args
        missing = []
        found = []
        for req in exp.required_tool_calls:
            checks += 1
            hit = next(((i, c) for i, c in executed if matches(c, req)), None)
            if hit is None:
                same_name = [describe_call(i, c) for i, c in executed if c.name == req.name]
                why = (f"called with different args ({'; '.join(same_name)})" if same_name
                       else "never called")
                missing.append(req.name)
                failures.append(f"required {req.name}({req.args_contain or ''}) missing: {why}")
            else:
                found.append(describe_call(*hit))

        # 4. order of first occurrences
        if exp.order:
            checks += 1
            first: dict[str, int] = {}
            for pos, (_, c) in enumerate(executed):
                first.setdefault(c.name, pos)
            positions = [first.get(n) for n in exp.order]
            if None in positions:
                absent = [n for n, p in zip(exp.order, positions, strict=True) if p is None]
                failures.append(f"expected order {exp.order} but {absent} never called")
            elif positions != sorted(positions):
                actual = sorted(exp.order, key=lambda n: first[n])
                failures.append(f"expected order {exp.order}, actual order {actual}")
            else:
                notes.append(f"order {exp.order} respected")

        passed = not failures
        score = 1.0 if passed else max(0.0, 1 - len(failures) / max(checks, 1))
        if passed:
            parts = [f"{len(exp.required_tool_calls)} required call(s) present"]
            if found:
                parts.append("; ".join(found))
            parts += notes
            parts.append(f"{len(calls)} call(s) schema-valid")
            evidence = "PASS: " + "; ".join(parts)
        else:
            evidence = "FAIL: " + " | ".join(failures)
        return self.result(passed, score, evidence, {
            "missing_required": missing, "schema_problems": schema_problems,
            "unknown_tools": [c.name for _, c in unknown], "n_calls": len(calls),
        })
