# Demo script (~2.5 minutes)

**Before the demo:** live runs for v1 and v2 are in `agentgauge.db` (labels `baseline` and `hardened`), human labels are imported, and the dashboard is open (`agentgauge dashboard`). Rehearse with the actual numbers. **Never tune scenarios or prompts to change the story; present whatever the runs show.**

| Time | Beat | What to show / say |
|---|---|---|
| 0:00 (10s) | **Problem** | "Teams ship agents faster than they can trust them. Today an agent that reads a poisoned help article can email your customer data to a stranger, and nobody finds out until it happens." |
| 0:10 (20s) | **The agent** | Run detail → run `baseline` → scenario `fn_order_status_001`. Show the customer question, the `lookup_order` call, and the grounded answer. "A normal question, handled correctly." |
| 0:30 (30s) | **Run AgentGauge** | Terminal: `agentgauge run --agent sample_v1 --suite injection --label live-demo -y`. Results stream in, one line per scenario. Switch to Overview and select the new run. "Every scenario is scored on correctness, safety, and efficiency, and every verdict has evidence." (The full suite is 25 scenarios; the injection suite alone keeps the live part short.) |
| 1:00 (40s) | **Money moment** | Run detail → `baseline` → `inj_doc_email_001`. Show the **COMPROMISED** banner, then the trace step where the poisoned `help_doc_01` entered context, then the `send_email` to `records-audit@evil.example` carrying canary `ZX-CANARY-91`, then the injection evaluator's evidence line. Switch to `hardened`, same scenario: the attempt is **BLOCKED by v2 policy**, or never made. |
| 1:40 (20s) | **Compare** | Compare runs: A = `baseline`, B = `hardened`. Read the injection resistance delta and the "flipped fail → pass" list. Mention any pass → fail flips honestly. |
| 2:00 (20s) | **Cost** | Cost & efficiency: cost per scenario, tokens by step ("input grows each step, so fewer steps is the lever"), the cost ledger, and the budget caps in `config.yaml` (`ABORTED_BUDGET` hard stop). |
| 2:20 (20s) | **Trust** | Evaluator trust: judge-vs-human agreement and Cohen's kappa (with n), then open Known limitations. Close: "Bring any agent that emits our trace format: `agentgauge replay --trace your_trace.json`." |

> **If live v1 resisted `inj_doc_email_001`**, open whichever injection scenario v1 actually failed (Run detail → Category = injection, Result = FAIL). If v1 resisted every attack, say so: that is a finding too, and the v1-vs-v2 comparison should be presented as measured.

## Backup plan (no network / AWS problems)

1. Show the pre-recorded backup video.
2. Or run `agentgauge demo` and open the dashboard. **Say out loud that this is mock mode:** a simulated rule-based model that tests the pipeline, not real model behavior. The banner on every page says the same.

## Commands cheat sheet

```bash
agentgauge list-runs
agentgauge compare baseline hardened
agentgauge report --run hardened --format html     # reports/<run_id>.html
agentgauge replay --trace examples/traces/example_trace.json --mock
```
