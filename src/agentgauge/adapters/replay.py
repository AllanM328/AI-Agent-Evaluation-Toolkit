"""Trace-replay mode: load externally produced traces ("bring your own agent")."""

from __future__ import annotations

import json
from pathlib import Path

from agentgauge.adapters.base import AgentAdapter
from agentgauge.models import Scenario, Trace


def load_traces(path: str | Path) -> list[Trace]:
    """Load one trace object, a JSON list of traces, or JSON Lines (one trace per line)."""
    text = Path(path).read_text().strip()
    if not text:
        return []
    if text.startswith("["):
        return [Trace.model_validate(t) for t in json.loads(text)]
    try:
        return [Trace.model_validate(json.loads(text))]
    except json.JSONDecodeError:
        return [Trace.model_validate_json(line) for line in text.splitlines() if line.strip()]


class ReplayAdapter(AgentAdapter):
    """Returns pre-recorded traces by scenario id instead of running anything."""

    def __init__(self, traces: list[Trace]):
        self.traces = {t.scenario_id: t for t in traces}
        names = {t.agent_name for t in traces}
        self.name = names.pop() if len(names) == 1 else "replay"

    @property
    def model_id(self) -> str | None:
        ids = {t.model_id for t in self.traces.values() if t.model_id}
        return ids.pop() if len(ids) == 1 else None

    def run(self, scenario: Scenario, sandbox=None, tracker=None) -> Trace:
        try:
            return self.traces[scenario.id]
        except KeyError:
            raise KeyError(f"no replayed trace for scenario '{scenario.id}'") from None
