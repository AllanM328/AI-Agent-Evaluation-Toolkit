"""Evaluator interface and shared helpers."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agentgauge.models import Dimension, EvalResult, Scenario, ToolCall, ToolCallExpectation, Trace

if TYPE_CHECKING:
    from agentgauge.config import Config
    from agentgauge.cost import CostTracker
    from agentgauge.model_client import ModelClient
    from agentgauge.sandbox import Sandbox


@dataclass
class EvalContext:
    config: Config
    sandbox: Sandbox  # fresh sandbox with the scenario setup applied (reference data only)
    judge_client: ModelClient | None = None
    tracker: CostTracker | None = None


class Evaluator(ABC):
    name: str = "evaluator"
    dimension: Dimension = "correctness"
    required: bool = True

    @abstractmethod
    def evaluate(self, trace: Trace, scenario: Scenario, ctx: EvalContext) -> EvalResult:
        ...

    def result(self, passed: bool, score: float, evidence: str, details: dict[str, Any] | None = None,
               *, applicable: bool = True, required: bool | None = None) -> EvalResult:
        return EvalResult(
            name=self.name, passed=passed, score=max(0.0, min(1.0, score)), evidence=evidence,
            details=details or {}, dimension=self.dimension,
            required=self.required if required is None else required, applicable=applicable,
        )

    def not_applicable(self, reason: str) -> EvalResult:
        return self.result(True, 1.0, f"N/A: {reason}", applicable=False)


# --------------------------------------------------------------------------- helpers

_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")


def normalize_value(value: Any) -> Any:
    """Normalize a scalar for comparison: numbers -> float, strings -> stripped lowercase."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        s = value.strip().lower().lstrip("#")
        if _NUMBER.match(s.replace(",", "")):
            return float(s.replace(",", ""))
        return s
    return value


def args_match(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    """True if every expected key is present with an equal value.

    Values are normalized (numbers compare numerically, strings case-insensitively). For string
    values, the expected string may also be a substring of the actual one (e.g. a search query).
    """
    for key, exp in expected.items():
        if key not in actual:
            return False
        a, e = normalize_value(actual[key]), normalize_value(exp)
        if a == e:
            continue
        if isinstance(a, str) and isinstance(e, str) and e in a:
            continue
        return False
    return True


def matches(call: ToolCall, pattern: ToolCallExpectation) -> bool:
    return call.name == pattern.name and args_match(call.arguments, pattern.args_contain)


def describe_call(step: int, call: ToolCall, max_len: int = 120) -> str:
    args = json.dumps(call.arguments, default=str)
    if len(args) > max_len:
        args = args[: max_len - 3] + "..."
    flag = " [BLOCKED]" if call.blocked else ""
    return f"step {step}: {call.name}({args}){flag}"


def stringify(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)


def normalize_text(text: str) -> str:
    """Lowercase, unify dashes, whitespace and simple plurals so '30-day' matches '30 days'."""
    text = text.lower().replace("–", "-").replace("—", "-").replace("’", "'")
    text = re.sub(r"(?<=\w)-(?=\w)", " ", text)
    text = re.sub(r"\b(\w{3,}?)s\b", r"\1", text)
    return re.sub(r"\s+", " ", text)
