import pytest

from agentgauge.config import BudgetConfig
from agentgauge.cost import AGENT, JUDGE, BudgetExceeded, CostTracker, ModelPrice, Pricing

from conftest import REPO_ROOT


def pricing():
    return Pricing({"m": ModelPrice(input_per_1k=0.001, output_per_1k=0.002),
                    "amazon.nova-x": ModelPrice(input_per_1k=0.01, output_per_1k=0.02)})


def test_pricing_cost_and_profile_prefix_fallback():
    p = pricing()
    assert p.cost("m", 1000, 500) == pytest.approx(0.002)
    assert p.cost("us.amazon.nova-x", 1000, 0) == pytest.approx(0.01)
    assert p.cost("unknown", 10, 10) is None


def test_load_repo_pricing_file():
    p = Pricing.load(REPO_ROOT / "pricing.yaml")
    assert p.get("mock-model") is not None


def test_unpriced_model_warns_and_reports_none():
    t = CostTracker(pricing(), BudgetConfig())
    t.start_run()
    assert t.record("mystery", 100, 10) is None
    t.record("mystery", 100, 10)
    usage = t.end_run()
    assert usage.cost_usd is None and usage.input_tokens == 200
    assert len(t.warnings) == 1 and "NO PRICING" in t.warnings[0]
    assert len(t.ledger) == 2


def test_run_usd_cap():
    t = CostTracker(pricing(), BudgetConfig(max_usd_per_run=0.003, max_usd_per_session=1))
    t.start_run()
    t.ensure_can_spend()
    t.record("m", 1000, 500)  # 0.002
    t.check()
    t.record("m", 1000, 500)  # 0.004 > 0.003
    with pytest.raises(BudgetExceeded) as e:
        t.check()
    assert e.value.scope == "run"
    with pytest.raises(BudgetExceeded):
        t.ensure_can_spend()
    t.end_run()
    t.start_run()
    t.ensure_can_spend()  # fresh run is fine


def test_session_cap_applies_to_judge_calls_but_run_cap_does_not():
    t = CostTracker(pricing(), BudgetConfig(max_usd_per_run=0.001, max_usd_per_session=0.005))
    t.start_run()
    t.record("m", 1000, 1000, purpose=JUDGE)  # 0.003, judge spend does not count toward run cap
    t.check(AGENT)
    t.record("m", 1000, 1000, purpose=JUDGE)  # session 0.006 > 0.005
    with pytest.raises(BudgetExceeded) as e:
        t.check(JUDGE)
    assert e.value.scope == "session"
    assert t.session_exhausted


def test_token_cap_applies_even_when_unpriced():
    t = CostTracker(Pricing({}), BudgetConfig(max_tokens_per_run=150))
    t.start_run()
    t.record("unpriced", 100, 60)
    with pytest.raises(BudgetExceeded) as e:
        t.check()
    assert e.value.unit == "tokens"
