"""Deterministic final-answer checks: required mentions and forbidden strings."""

from __future__ import annotations

from agentgauge.evaluators.base import EvalContext, Evaluator, normalize_text
from agentgauge.models import EvalResult, Scenario, Trace


class AnswerContentEvaluator(Evaluator):
    name = "answer_content"
    dimension = "correctness"

    def evaluate(self, trace: Trace, scenario: Scenario, ctx: EvalContext) -> EvalResult:
        exp = scenario.expected
        if not exp.final_answer_must_mention and not exp.must_not_contain:
            return self.not_applicable("scenario defines no final-answer keyword checks")
        answer = trace.final_answer or ""
        norm = normalize_text(answer)
        failures: list[str] = []
        hits: list[str] = []
        for entry in exp.final_answer_must_mention:
            options = [entry] if isinstance(entry, str) else list(entry)
            found = next((o for o in options if normalize_text(o) in norm), None)
            if found is None:
                failures.append(f"final answer does not mention any of {options}")
            else:
                hits.append(found)
        for bad in exp.must_not_contain:
            if bad.lower() in answer.lower():
                failures.append(f"final answer contains forbidden text '{bad}'")
        checks = len(exp.final_answer_must_mention) + len(exp.must_not_contain)
        details = {"mentioned": hits, "failures": failures, "answer_chars": len(answer)}
        if not answer:
            failures.insert(0, "no final answer")
        if failures:
            return self.result(False, 1 - min(len(failures), checks) / checks, "FAIL: " + " | ".join(failures),
                               details)
        parts = []
        if hits:
            parts.append(f"mentions {hits}")
        if exp.must_not_contain:
            parts.append(f"avoids {len(exp.must_not_contain)} forbidden string(s)")
        return self.result(True, 1.0, "PASS: final answer " + "; ".join(parts), details)
