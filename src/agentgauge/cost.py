"""Token/USD accounting and budget enforcement.

Every model call is recorded in a ledger with its token counts and the pricing.yaml entry
used, so every cost figure can be traced back to logged tokens and a price.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel

from agentgauge.config import BudgetConfig

logger = logging.getLogger("agentgauge.cost")

# Cross-region inference profile prefixes (e.g. "us.amazon.nova-lite-v1:0").
_PROFILE_PREFIX = re.compile(r"^(us|eu|apac|us-gov|ca|jp|au|global)\.")

AGENT = "agent"  # purposes; only agent calls count toward per-run caps
JUDGE = "judge"
GROUNDEDNESS = "groundedness"


class ModelPrice(BaseModel):
    input_per_1k: float
    output_per_1k: float


class Pricing:
    def __init__(self, prices: dict[str, ModelPrice] | None = None, source: str | None = None):
        self.prices = prices or {}
        self.source = source

    @classmethod
    def load(cls, path: str | Path) -> Pricing:
        p = Path(path)
        if not p.exists():
            logger.warning("Pricing file %s not found: costs will be reported as tokens only.", p)
            return cls({}, str(p))
        raw = yaml.safe_load(p.read_text()) or {}
        models = raw.get("models") or {}
        prices = {str(k): ModelPrice.model_validate(v) for k, v in models.items() if v}
        return cls(prices, str(p))

    def get(self, model_id: str | None) -> ModelPrice | None:
        if not model_id:
            return None
        if model_id in self.prices:
            return self.prices[model_id]
        stripped = _PROFILE_PREFIX.sub("", model_id)
        return self.prices.get(stripped)

    def cost(self, model_id: str | None, input_tokens: int, output_tokens: int) -> float | None:
        price = self.get(model_id)
        if price is None:
            return None
        return input_tokens / 1000 * price.input_per_1k + output_tokens / 1000 * price.output_per_1k


class BudgetExceeded(Exception):
    def __init__(self, scope: str, unit: str, limit: float, spent: float):
        self.scope, self.unit, self.limit, self.spent = scope, unit, limit, spent
        amount = f"${spent:.4f} > ${limit:.4f}" if unit == "usd" else f"{int(spent)} > {int(limit)} tokens"
        super().__init__(f"Budget cap exceeded ({scope}): {amount}")


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    priced_cost_usd: float = 0.0
    unpriced_calls: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float | None:
        """USD spend, or None if any call could not be priced (never under-report)."""
        return None if self.unpriced_calls else self.priced_cost_usd

    def add(self, input_tokens: int, output_tokens: int, cost: float | None) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        if cost is None:
            self.unpriced_calls += 1
        else:
            self.priced_cost_usd += cost


@dataclass
class LedgerEntry:
    timestamp: str
    model_id: str | None
    purpose: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


@dataclass
class CostTracker:
    pricing: Pricing
    budget: BudgetConfig
    session: Usage = field(default_factory=Usage)
    run: Usage | None = None
    ledger: list[LedgerEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _warned_models: set[str] = field(default_factory=set)

    # -- run lifecycle -------------------------------------------------------------
    def start_run(self) -> None:
        self.run = Usage()

    def end_run(self) -> Usage:
        usage = self.run or Usage()
        self.run = None
        return usage

    # -- recording -----------------------------------------------------------------
    def record(self, model_id: str | None, input_tokens: int, output_tokens: int, purpose: str = AGENT
               ) -> float | None:
        cost = self.pricing.cost(model_id, input_tokens, output_tokens)
        if cost is None:
            self._warn_unpriced(model_id)
        self.session.add(input_tokens, output_tokens, cost)
        if purpose == AGENT and self.run is not None:
            self.run.add(input_tokens, output_tokens, cost)
        self.ledger.append(LedgerEntry(
            timestamp=datetime.now(UTC).isoformat(timespec="milliseconds"),
            model_id=model_id, purpose=purpose,
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost,
        ))
        return cost

    def _warn_unpriced(self, model_id: str | None) -> None:
        key = model_id or "<unknown>"
        if key in self._warned_models:
            return
        self._warned_models.add(key)
        msg = (f"NO PRICING for model '{key}' in {self.pricing.source or 'pricing.yaml'}: reporting "
               "tokens only; USD budget caps cannot be enforced for this model (token caps still apply).")
        self.warnings.append(msg)
        logger.warning("!" * 8 + " " + msg)

    # -- enforcement ---------------------------------------------------------------
    def _violations(self, purpose: str, strict: bool) -> BudgetExceeded | None:
        """strict=True: spent > limit (after a call). strict=False: spent >= limit (before a call)."""
        def over(spent: float, limit: float) -> bool:
            return spent > limit if strict else spent >= limit

        # USD caps can only see priced calls; unpriced calls are bounded by the token caps.
        b = self.budget
        s = self.session
        if over(s.priced_cost_usd, b.max_usd_per_session):
            return BudgetExceeded("session", "usd", b.max_usd_per_session, s.priced_cost_usd)
        if over(s.tokens, b.max_tokens_per_session):
            return BudgetExceeded("session", "tokens", b.max_tokens_per_session, s.tokens)
        if purpose == AGENT and self.run is not None:
            r = self.run
            if over(r.priced_cost_usd, b.max_usd_per_run):
                return BudgetExceeded("run", "usd", b.max_usd_per_run, r.priced_cost_usd)
            if over(r.tokens, b.max_tokens_per_run):
                return BudgetExceeded("run", "tokens", b.max_tokens_per_run, r.tokens)
        return None

    def ensure_can_spend(self, purpose: str = AGENT) -> None:
        """Call before a model call: raises if a cap is already reached."""
        v = self._violations(purpose, strict=False)
        if v:
            raise v

    def check(self, purpose: str = AGENT) -> None:
        """Call after recording a model call: raises if the call pushed spend over a cap."""
        v = self._violations(purpose, strict=True)
        if v:
            raise v

    @property
    def session_exhausted(self) -> bool:
        try:
            self.ensure_can_spend(purpose=JUDGE)
        except BudgetExceeded:
            return True
        return False
