# Status

## Wed Oct 14: Foundation

**Done**
- Repo scaffold: `pyproject.toml`, ruff, pytest, `config.yaml`, `pricing.yaml`.
- `models.py` (Trace/Step/ToolCall/Scenario/EvalResult/RunSummary), config loading with env overrides.
- `ModelClient` interface, `BedrockClient` (Converse API with tool use), `MockClient` (scripted or policy-driven).
- `CostTracker` with per-call ledger, USD and token caps, loud warnings for unpriced models.
- Sandbox store: 8 customers, 16 orders, 14 help docs, 5 fake tools (2 sensitive), per-scenario poisoning.
- Deterministic evaluators: tool_calls, answer_content, scope, injection canary, redundancy, efficiency.
- 27 offline tests passing, including 6+ hand-written good and bad traces.

**In progress**
- Sample agent v1/v2, runner, SQLite, scenarios.

**Blocked**
- Nothing yet. Live runs will need AWS access (humans, plan section 14).

**Next three tasks**
1. Sample agent loop (v1 naive / v2 hardened plus argument validator) and the offline simulated mock policy.
2. Runner with budget caps and SQLite storage; CLI `run --mock`.
3. 25 scenario YAMLs.
