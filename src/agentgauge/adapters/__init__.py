from agentgauge.adapters.base import AgentAdapter
from agentgauge.adapters.replay import ReplayAdapter, load_traces
from agentgauge.adapters.sample_agent import ArgumentValidator, SampleAgent

AGENT_ALIASES = {
    "sample_v1": "v1",
    "sample_agent_v1": "v1",
    "v1": "v1",
    "sample_v2": "v2",
    "sample_agent_v2": "v2",
    "v2": "v2",
}

__all__ = ["AGENT_ALIASES", "AgentAdapter", "ArgumentValidator", "ReplayAdapter", "SampleAgent", "load_traces"]
