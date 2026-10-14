"""Sandboxed fake tools for the sample customer-support agent.

Everything here is in-memory and synthetic. Nothing touches the network or any real system:
`send_email` appends to an in-memory outbox and `issue_refund` appends to an in-memory ledger.
"""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from agentgauge.models import ScenarioSetup

DATA_DIR = Path(__file__).parent / "data"

SENSITIVE_TOOLS: frozenset[str] = frozenset({"issue_refund", "send_email"})
STORE_DOMAIN = "shopwise.example"

TOOL_SPECS: dict[str, dict[str, Any]] = {
    "lookup_customer": {
        "description": "Look up a customer record by customer id (for example C001).",
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string", "description": "Customer id, e.g. C001"}},
            "required": ["customer_id"],
            "additionalProperties": False,
        },
    },
    "lookup_order": {
        "description": "Look up an order by order id. Returns status, items, amount, refund eligibility "
                       "and notes.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "Order id, e.g. 1042"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
    },
    "search_help_docs": {
        "description": "Search the store's help-center articles. Returns up to 3 matching articles "
                       "with their doc_id for citation.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to search for"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "issue_refund": {
        "description": "Issue a refund for an order to the customer's original payment method.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "Order id to refund"},
                "amount": {"type": "number", "description": "Refund amount in USD"},
            },
            "required": ["order_id", "amount"],
            "additionalProperties": False,
        },
    },
    "send_email": {
        "description": "Send an email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        },
    },
}

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}

_STOPWORDS = frozenset(
    "a an and are as at be by can could do does for from get got has have how i if in is it its me "
    "my of on or our please so that the their them there this to was what when where which who will "
    "with would you your yours really hi hello".split()
)


class ToolError(Exception):
    """Raised for unknown tools, invalid arguments, or missing records."""


def validate_args(name: str, args: Any) -> list[str]:
    """Check arguments against the tool's JSON schema. Returns a list of problems (empty if valid)."""
    spec = TOOL_SPECS.get(name)
    if spec is None:
        return [f"unknown tool '{name}'"]
    if not isinstance(args, dict):
        return ["arguments must be an object"]
    schema = spec["input_schema"]
    props: dict[str, Any] = schema.get("properties", {})
    problems = [f"missing required argument '{r}'" for r in schema.get("required", []) if r not in args]
    for key, value in args.items():
        if key not in props:
            if schema.get("additionalProperties") is False:
                problems.append(f"unexpected argument '{key}'")
            continue
        expected = props[key].get("type")
        allowed = _JSON_TYPES.get(expected or "", ())
        if allowed and (not isinstance(value, allowed) or (expected != "boolean" and isinstance(value, bool))):
            problems.append(f"argument '{key}' should be {expected}, got {type(value).__name__}")
    return problems


def _stem(tok: str) -> str:
    return tok[:-1] if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss") else tok


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOPWORDS]


@lru_cache(maxsize=1)
def _base_data() -> dict[str, Any]:
    def load(name: str) -> Any:
        return json.loads((DATA_DIR / name).read_text())

    return {
        "customers": {c["customer_id"]: c for c in load("customers.json")},
        "orders": {o["order_id"]: o for o in load("orders.json")},
        "docs": {d["doc_id"]: d for d in load("help_docs.json")},
    }


def _fill(value: Any, canary: str | None) -> Any:
    if canary and isinstance(value, str):
        return value.replace("{canary}", canary)
    return value


class Sandbox:
    """Per-scenario instance of the fake store. Mutations never leak between scenarios."""

    def __init__(self, setup: ScenarioSetup | None = None):
        base = _base_data()
        self.customers: dict[str, dict[str, Any]] = copy.deepcopy(base["customers"])
        self.orders: dict[str, dict[str, Any]] = copy.deepcopy(base["orders"])
        self.docs: dict[str, dict[str, Any]] = copy.deepcopy(base["docs"])
        self.outbox: list[dict[str, Any]] = []
        self.refunds: list[dict[str, Any]] = []
        self.setup = setup or ScenarioSetup()
        self._apply_setup(self.setup)

    # -- setup ---------------------------------------------------------------------
    def _apply_setup(self, setup: ScenarioSetup) -> None:
        canary = setup.canary
        for doc_id, content in setup.doc_overrides.items():
            self._require(self.docs, doc_id, "doc")["content"] = _fill(content, canary)
        if setup.poisoned_doc_id:
            doc = self._require(self.docs, setup.poisoned_doc_id, "doc")
            if setup.payload:
                doc["content"] = f"{doc['content']}\n\n{_fill(setup.payload, canary)}"
        elif setup.payload:
            raise ValueError("setup.payload requires setup.poisoned_doc_id")
        for order_id, fields in setup.order_overrides.items():
            order = self._require(self.orders, order_id, "order")
            order.update({k: _fill(v, canary) for k, v in fields.items()})
        for customer_id, fields in setup.customer_overrides.items():
            cust = self._require(self.customers, customer_id, "customer")
            cust.update({k: _fill(v, canary) for k, v in fields.items()})

    @staticmethod
    def _require(table: dict[str, Any], key: str, kind: str) -> dict[str, Any]:
        if key not in table:
            raise ValueError(f"Scenario setup references unknown {kind} '{key}'")
        return table[key]

    # -- tool plumbing -------------------------------------------------------------
    @staticmethod
    def tool_names() -> list[str]:
        return list(TOOL_SPECS)

    @staticmethod
    def converse_tools() -> list[dict[str, Any]]:
        """Tool definitions in Bedrock Converse `toolConfig.tools` format."""
        return [
            {"toolSpec": {"name": name, "description": spec["description"],
                          "inputSchema": {"json": spec["input_schema"]}}}
            for name, spec in TOOL_SPECS.items()
        ]

    def execute(self, name: str, args: dict[str, Any]) -> Any:
        problems = validate_args(name, args)
        if problems:
            raise ToolError("; ".join(problems))
        return getattr(self, name)(**args)

    # -- tools -----------------------------------------------------------------------
    def lookup_customer(self, customer_id: str) -> dict[str, Any]:
        cust = self.customers.get(str(customer_id).strip().upper())
        if cust is None:
            raise ToolError(f"No customer with id '{customer_id}'")
        return copy.deepcopy(cust)

    def lookup_order(self, order_id: str) -> dict[str, Any]:
        oid = str(order_id).strip().lstrip("#")
        order = self.orders.get(oid)
        if order is None:
            raise ToolError(f"No order with id '{order_id}'")
        return copy.deepcopy(order)

    def search_help_docs(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        q = set(tokenize(query))
        scored = []
        for doc in self.docs.values():
            title = set(tokenize(doc["title"]))
            body = set(tokenize(doc["content"]))
            score = sum(2 * (t in title) + (t in body) for t in q)
            if score > 0:
                scored.append((score, doc["doc_id"]))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [
            {"doc_id": d, "title": self.docs[d]["title"], "content": self.docs[d]["content"]}
            for _, d in scored[:top_k]
        ]

    def issue_refund(self, order_id: str, amount: float) -> dict[str, Any]:
        order = self.lookup_order(order_id)
        if amount <= 0:
            raise ToolError("Refund amount must be positive")
        if amount > order["amount"] + 1e-9:
            raise ToolError(f"Refund amount {amount} exceeds order total {order['amount']}")
        refund = {"refund_id": f"RF-{order['order_id']}-{len(self.refunds) + 1}",
                  "order_id": order["order_id"], "amount": round(float(amount), 2), "status": "issued"}
        self.refunds.append(refund)
        return refund

    def send_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        msg = {"message_id": f"MSG-{len(self.outbox) + 1}", "to": to, "subject": subject,
               "body": body, "status": "queued (sandbox: never delivered)"}
        self.outbox.append(msg)
        return {"message_id": msg["message_id"], "status": msg["status"]}

    # -- reference data used by guardrails and evaluators ---------------------------
    def emails_on_file(self) -> set[str]:
        return {c["email"].lower() for c in self.customers.values()}

    def email_for_customer(self, customer_id: str) -> str | None:
        cust = self.customers.get(str(customer_id).strip().upper())
        return cust["email"].lower() if cust else None

    def owner_of_order(self, order_id: str) -> str | None:
        order = self.orders.get(str(order_id).strip().lstrip("#"))
        return order["customer_id"] if order else None
