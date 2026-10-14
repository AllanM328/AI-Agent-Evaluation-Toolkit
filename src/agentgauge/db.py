"""SQLite storage for runs and per-scenario results (single file, default agentgauge.db)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from agentgauge.models import RunSummary, ScenarioResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    label TEXT,
    agent_name TEXT NOT NULL,
    mode TEXT NOT NULL,
    suite TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    target_model_id TEXT,
    judge_model_id TEXT,
    reliability_score REAL,
    summary_json TEXT NOT NULL,
    config_json TEXT
);
CREATE TABLE IF NOT EXISTS results (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    scenario_id TEXT NOT NULL,
    category TEXT NOT NULL,
    passed INTEGER NOT NULL,
    compromised INTEGER,
    status TEXT NOT NULL,
    cost_usd REAL,
    eval_cost_usd REAL,
    latency_ms INTEGER,
    model_steps INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    judge_score REAL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (run_id, scenario_id)
);
CREATE TABLE IF NOT EXISTS cost_ledger (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    timestamp TEXT,
    scenario_id TEXT,
    model_id TEXT,
    purpose TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    PRIMARY KEY (run_id, seq)
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # -- writes ----------------------------------------------------------------------
    def save_run(self, summary: RunSummary, config: dict[str, Any] | None = None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """INSERT INTO runs (run_id, label, agent_name, mode, suite, status, created_at, finished_at,
                       target_model_id, judge_model_id, reliability_score, summary_json, config_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                       label=excluded.label, status=excluded.status, finished_at=excluded.finished_at,
                       reliability_score=excluded.reliability_score, summary_json=excluded.summary_json,
                       config_json=COALESCE(excluded.config_json, runs.config_json)""",
                (summary.run_id, summary.label, summary.agent_name, summary.mode, summary.suite, summary.status,
                 summary.created_at, summary.finished_at, summary.target_model_id, summary.judge_model_id,
                 summary.reliability_score, summary.model_dump_json(),
                 json.dumps(config, default=str) if config is not None else None),
            )

    def save_result(self, r: ScenarioResult) -> None:
        t = r.trace
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """INSERT OR REPLACE INTO results (run_id, scenario_id, category, passed, compromised, status,
                       cost_usd, eval_cost_usd, latency_ms, model_steps, input_tokens, output_tokens,
                       judge_score, result_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r.run_id, r.scenario_id, r.category, int(r.passed),
                 None if r.compromised is None else int(r.compromised), t.status, t.total_cost_usd,
                 r.eval_cost_usd, t.total_latency_ms, len(t.model_steps()), t.total_input_tokens,
                 t.total_output_tokens, r.judge_score, r.model_dump_json()),
            )

    def save_ledger(self, run_id: str, entries: list[Any]) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "INSERT OR REPLACE INTO cost_ledger VALUES (?,?,?,?,?,?,?,?,?)",
                [(run_id, i, e.timestamp, e.scenario_id, e.model_id, e.purpose, e.input_tokens, e.output_tokens,
                  e.cost_usd)
                 for i, e in enumerate(entries)],
            )

    def delete_run(self, run_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM results WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM cost_ledger WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

    # -- reads -----------------------------------------------------------------------
    def list_runs(self) -> list[RunSummary]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT summary_json FROM runs ORDER BY created_at DESC, rowid DESC").fetchall()
        return [RunSummary.model_validate_json(r["summary_json"]) for r in rows]

    def resolve_run_id(self, ref: str) -> str:
        """Accept a full run id, a unique prefix, a label, or 'latest'."""
        runs = self.list_runs()
        if not runs:
            raise KeyError("No runs in the database yet")
        if ref == "latest":
            return runs[0].run_id
        exact = [r for r in runs if r.run_id == ref]
        if exact:
            return exact[0].run_id
        by_label = [r for r in runs if r.label == ref]
        if by_label:
            return by_label[0].run_id  # most recent run with that label
        prefixed = [r for r in runs if r.run_id.startswith(ref)]
        if len(prefixed) == 1:
            return prefixed[0].run_id
        if len(prefixed) > 1:
            raise KeyError(f"Run reference '{ref}' is ambiguous ({len(prefixed)} matches)")
        raise KeyError(f"No run matching '{ref}'")

    def get_run(self, ref: str) -> RunSummary:
        run_id = self.resolve_run_id(ref)
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT summary_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return RunSummary.model_validate_json(row["summary_json"])

    def get_run_config(self, run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT config_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row["config_json"]) if row and row["config_json"] else None

    def get_results(self, ref: str) -> list[ScenarioResult]:
        run_id = self.resolve_run_id(ref)
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT result_json FROM results WHERE run_id = ? ORDER BY category, scenario_id",
                                (run_id,)).fetchall()
        return [ScenarioResult.model_validate_json(r["result_json"]) for r in rows]

    def get_ledger(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM cost_ledger WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
        return [dict(r) for r in rows]
