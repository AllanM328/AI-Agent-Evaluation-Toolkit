"""AgentAdapter: the interface the runner uses to drive any agent and get back a Trace."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from agentgauge.models import Scenario, Trace

if TYPE_CHECKING:
    from agentgauge.cost import CostTracker
    from agentgauge.sandbox import Sandbox


class AgentAdapter(ABC):
    name: str = "agent"

    @property
    def model_id(self) -> str | None:
        return None

    @abstractmethod
    def run(self, scenario: Scenario, sandbox: Sandbox, tracker: CostTracker) -> Trace:
        """Run the agent on one scenario against `sandbox`, recording spend in `tracker`."""
