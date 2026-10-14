"""Shared fixtures and helpers for building hand-written traces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentgauge.config import Config, load_config
from agentgauge.evaluators.base import EvalContext
from agentgauge.models import Scenario, Step, ToolCall, Trace
from agentgauge.sandbox import Sandbox

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_aws(monkeypatch):
    """Tests must never reach AWS: strip credentials and model-ID env vars."""
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
                "AGENTGAUGE_TARGET_MODEL_ID", "AGENTGAUGE_JUDGE_MODEL_ID", "AGENTGAUGE_DB", "AGENTGAUGE_PROFILE",
                "AGENTGAUGE_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture
def config(tmp_path) -> Config:
    cfg = load_config(REPO_ROOT / "config.yaml")
    cfg.paths.db = str(tmp_path / "test.db")
    cfg.paths.labels = str(tmp_path / "labels.json")
    return cfg


def scenario(**kw: Any) -> Scenario:
    base = {"id": "t_001", "category": "functional", "description": "test", "user_prompt": "hi"}
    base.update(kw)
    return Scenario.model_validate(base)


def call(name: str, args: dict, result: Any = None, *, error: str | None = None, blocked: bool = False,
         cid: str | None = None) -> ToolCall:
    return ToolCall(id=cid or f"tc_{name}_{abs(hash(str(args))) % 10_000}", name=name, arguments=args,
                    result=result, error=error, latency_ms=2, blocked=blocked)


def trace(user_prompt: str, steps_calls: list[list[ToolCall]], final_answer: str | None,
          *, status: str = "OK", cost: float | None = 0.001, scenario_id: str = "t_001",
          tokens_per_step: int = 100) -> Trace:
    steps = [Step(index=0, role="user", text=user_prompt)]
    for calls in steps_calls:
        steps.append(Step(index=len(steps), role="assistant", tool_calls=calls,
                          input_tokens=tokens_per_step, output_tokens=20, latency_ms=100))
    if final_answer is not None:
        steps.append(Step(index=len(steps), role="assistant", text=final_answer,
                          input_tokens=tokens_per_step, output_tokens=40, latency_ms=120))
    model_steps = [s for s in steps if s.role == "assistant"]
    return Trace(
        trace_id="tr_test", agent_name="handwritten", scenario_id=scenario_id, user_prompt=user_prompt,
        steps=steps, final_answer=final_answer, status=status,
        total_input_tokens=sum(s.input_tokens for s in model_steps),
        total_output_tokens=sum(s.output_tokens for s in model_steps),
        total_cost_usd=cost,
        total_latency_ms=sum(s.latency_ms for s in model_steps) + sum(
            c.latency_ms for s in steps for c in s.tool_calls),
        model_id="mock-model",
    )


def ctx_for(sc: Scenario, config: Config, **kw) -> EvalContext:
    return EvalContext(config=config, sandbox=Sandbox(sc.setup), **kw)

