# Status

## Wed Oct 14: Foundation, plus everything that can be built without AWS

All code milestones in plan section 12 that don't need AWS access or human labels are built, tested offline, and committed.

**Done**
- Foundation: pydantic models, config with env overrides, `ModelClient` (Bedrock Converse + Mock), `CostTracker` (USD and token caps, ledger, loud unpriced warnings), sandbox store and tools.
- Evaluators: tool_calls, answer_content, scope, injection canary, redundancy, efficiency (deterministic); LLM judge (3 samples, median, JSON validation with retry, agreement); groundedness (lexical default, LLM optional, advisory).
- Target agent v1 (naive) and v2 (hardened prompt + argument validator), runner with budgets, SQLite storage, trace replay, mock-based cost estimate gating live runs.
- 25 scenarios (10 functional, 10 injection including a benign false-positive control, 5 efficiency) and an example external trace.
- CLI: run / estimate / replay / report (md, html) / compare / list-runs / validate-judge / export-labels / import-labels / list-models / dashboard / demo.
- Streamlit dashboard (overview, run detail, compare, cost & efficiency, evaluator trust).
- Judge-validation tooling (blind labeling CSV, import, agreement and Cohen's kappa). No labels are fabricated.
- Docs: README, DECISIONS, LIMITATIONS, DEMO_SCRIPT; GitHub Actions CI (ruff + pytest on 3.11/3.12 + offline demo).
- 73 offline tests passing; `ruff check` clean.

**In progress / not yet verified**
- **No live Bedrock call has been made yet.** The Converse request/response mapping is unit-tested against a stub, but the first real run may surface model-specific quirks (e.g. tool-use support, inference-profile ids).

**Blocked (needs humans, plan section 14)**
- AWS account, Bedrock model access, credentials, and an AWS Budgets alarm.
- `pricing.yaml` prices for the chosen target and judge models.
- Human labels for 20–30 runs (after the first live pass).

**Next three tasks**
1. Humans: AWS setup, then `agentgauge list-models`; set `AGENTGAUGE_TARGET_MODEL_ID` / `AGENTGAUGE_JUDGE_MODEL_ID`, fill in `pricing.yaml`, and record the chosen IDs in the README table.
2. `agentgauge estimate`, then a 2–3 scenario live smoke test (`--scenario fn_order_status_001 --scenario inj_doc_email_001`); fix any provider quirks; then full v1 and v2 live passes.
3. `agentgauge export-labels --run baseline --run hardened`; humans label; `import-labels`; `validate-judge`. Spot-check scenario fairness and evidence strings against the live traces.

**Optional (cut line #1):** containerize the dashboard and deploy it to App Runner or ECS Fargate with CDK or Terraform.
