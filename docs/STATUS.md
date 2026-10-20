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
- 83 offline tests passing; `ruff check` clean.

**In progress / not yet verified**
- **No live Bedrock call has been made yet.** The Converse request/response mapping is unit-tested against a stub, but the first real run may surface model-specific quirks (e.g. tool-use support, inference-profile ids).

**Blocked (needs humans, plan section 14)**
- AWS account, Bedrock model access, credentials, and an AWS Budgets alarm.
- `pricing.yaml` prices for the chosen target and judge models.
- Human labels for 20–30 runs (after the first live pass).

**AWS Agent Toolkit setup (Oct 14)**
- Decided: Steps 3–6 (sign-in, Agent Toolkit, MCP server) run on a teammate's own computer. Cloud sessions block the sign-in host, have no browser, and lose ~/.aws on every new container.
- Done in the repo: CLAUDE.md (Step 7; project guardrails + AWS's rules verbatim, help_level MEDIUM), `AGENTGAUGE_PROFILE` support, `boto3[crt]>=1.41`, README switched to us-east-2 + profile `agentgauge`.

**Models chosen (Oct 20)**
- Target `amazon.nova-lite-v1:0`, judge `us.amazon.nova-pro-v1:0`, priced in pricing.yaml and set via `AGENTGAUGE_TARGET_MODEL_ID` / `AGENTGAUGE_JUDGE_MODEL_ID` (D30). The `agentgauge` profile is signed in. Still to do: AWS Budgets alarm, then a live smoke test.

**First live smoke test blocked (Oct 20)**
- Bedrock returned `Too many tokens per day` with 0 tokens used: every Nova Lite/Pro inference quota in us-east-2 is 0 and non-adjustable (FREE plan). Needs an AWS Support case (Account and billing, free on Basic support) asking for Bedrock on-demand quotas for these models. AgentGauge now excludes errored scenarios from scores and stops on daily-quota errors (D31).

**Switched live runs to OpenRouter (Oct 20)**
- Hackathon is Sunday; Bedrock waits on AWS Support. Provider `openrouter` is now the default (D32): target `openai/gpt-4.1-mini`, judge `anthropic/claude-haiku-5.5`, priced in pricing.yaml, about $0.09 per full pass. All tests are offline (fake transport). Next: export `OPENROUTER_API_KEY` and the two model IDs, run the 2-scenario smoke test, then full v1/v2 passes.

**Next three tasks**
1. Humans, on your own computer: run the Agent Toolkit setup (profile `agentgauge`, Region us-east-2, new AWS experience), then `pip install -e ".[dev]"`, `export AGENTGAUGE_PROFILE=agentgauge` (config.yaml pins Region us-east-2), `agentgauge list-models`; set `AGENTGAUGE_TARGET_MODEL_ID` / `AGENTGAUGE_JUDGE_MODEL_ID`, fill in `pricing.yaml`, and record the chosen IDs in the README table.
2. `agentgauge estimate`, then a 2–3 scenario live smoke test (`--scenario fn_order_status_001 --scenario inj_doc_email_001`); fix any provider quirks; then full v1 and v2 live passes.
3. `agentgauge export-labels --run baseline --run hardened`; humans label; `import-labels`; `validate-judge`. Spot-check scenario fairness and evidence strings against the live traces.

**Optional (cut line #1):** containerize the dashboard and deploy it to App Runner or ECS Fargate with CDK or Terraform.
