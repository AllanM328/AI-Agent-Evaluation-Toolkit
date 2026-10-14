"""Judge honesty: compare LLM-judge verdicts with human labels.

Workflow:
  1. agentgauge export-labels --run <id> [--run <id2>]   -> blind CSV (judge verdict hidden)
  2. Humans fill in `human_label` (pass/fail) and `labeler` for 20-30 rows; 5-10 rows by both teammates.
  3. agentgauge import-labels <csv>                       -> merges into validation/labeled_runs.json
  4. agentgauge validate-judge                            -> agreement rate + Cohen's kappa
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agentgauge.db import Database
from agentgauge.evaluators.judge import summarize_trace_for_judge

LABEL_FIELDS = ["run_id", "scenario_id", "category", "agent_name", "user_prompt", "tool_calls", "final_answer",
                "rubric_or_description", "human_label", "labeler", "notes"]


class HumanLabel(BaseModel):
    run_id: str
    scenario_id: str
    human_label: Literal["pass", "fail"]
    labeler: str = "unknown"
    notes: str = ""


class Disagreement(BaseModel):
    run_id: str
    scenario_id: str
    human: str
    judge: str
    judge_score: float | None = None


class AgreementStats(BaseModel):
    n: int = 0
    agreement: float | None = None
    kappa: float | None = None


class JudgeValidationReport(BaseModel):
    labels_file: str
    n_labels: int = 0
    n_items: int = 0
    n_matched: int = 0  # items with a human consensus label and a judge verdict
    n_ties: int = 0
    judge_vs_human: AgreementStats = Field(default_factory=AgreementStats)
    human_vs_human: AgreementStats = Field(default_factory=AgreementStats)
    confusion: dict[str, int] = Field(default_factory=dict)  # judge_pass/human_pass etc.
    disagreements: list[Disagreement] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)  # labeled items not found in the DB

    @property
    def status(self) -> str:
        if self.n_labels == 0:
            return "pending: no human labels yet"
        if self.n_matched == 0:
            return "pending: labels do not match any judged result in this database"
        return "ok"


def cohens_kappa(a: list[bool], b: list[bool]) -> float | None:
    """Cohen's kappa for two binary raters. None when undefined (no items, or chance agreement = 1)."""
    n = len(a)
    if n == 0 or n != len(b):
        return None
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    if pe >= 1:
        return None
    return round((po - pe) / (1 - pe), 4)


def load_labels(path: str | Path) -> list[HumanLabel]:
    p = Path(path)
    if not p.exists():
        return []
    raw = json.loads(p.read_text() or "[]")
    if isinstance(raw, dict):  # tolerate {"labels": [...]}
        raw = raw.get("labels", [])
    return [HumanLabel.model_validate(x) for x in raw]


def save_labels(path: str | Path, labels: list[HumanLabel]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps([lab.model_dump() for lab in labels], indent=2) + "\n")


def validate_judge(db: Database, labels_path: str | Path) -> JudgeValidationReport:
    labels = load_labels(labels_path)
    report = JudgeValidationReport(labels_file=str(labels_path), n_labels=len(labels))
    by_item: dict[tuple[str, str], list[HumanLabel]] = defaultdict(list)
    for lab in labels:
        by_item[(lab.run_id, lab.scenario_id)].append(lab)
    report.n_items = len(by_item)

    # human vs human on doubly-labeled items (first two distinct labelers)
    h1: list[bool] = []
    h2: list[bool] = []
    for labs in by_item.values():
        distinct: dict[str, HumanLabel] = {}
        for lab in labs:
            distinct.setdefault(lab.labeler, lab)
        if len(distinct) >= 2:
            x, y = list(distinct.values())[:2]
            h1.append(x.human_label == "pass")
            h2.append(y.human_label == "pass")
    if h1:
        report.human_vs_human = AgreementStats(
            n=len(h1), agreement=round(sum(x == y for x, y in zip(h1, h2, strict=True)) / len(h1), 4),
            kappa=cohens_kappa(h1, h2))

    results_cache: dict[str, dict] = {}
    judge_v: list[bool] = []
    human_v: list[bool] = []
    confusion = {"judge_pass_human_pass": 0, "judge_pass_human_fail": 0, "judge_fail_human_pass": 0,
                 "judge_fail_human_fail": 0}
    for (run_id, scenario_id), labs in by_item.items():
        votes = [lab.human_label == "pass" for lab in labs]
        if sum(votes) * 2 == len(votes):
            report.n_ties += 1
            continue
        human = sum(votes) * 2 > len(votes)
        if run_id not in results_cache:
            try:
                results_cache[run_id] = {r.scenario_id: r for r in db.get_results(run_id)}
            except KeyError:
                results_cache[run_id] = {}
        result = results_cache[run_id].get(scenario_id)
        judge_eval = result.get_eval("judge") if result else None
        if judge_eval is None or not judge_eval.applicable or judge_eval.details.get("final_score") is None:
            report.missing.append(f"{run_id}/{scenario_id}")
            continue
        judge = judge_eval.passed
        judge_v.append(judge)
        human_v.append(human)
        confusion[f"judge_{'pass' if judge else 'fail'}_human_{'pass' if human else 'fail'}"] += 1
        if judge != human:
            report.disagreements.append(Disagreement(
                run_id=run_id, scenario_id=scenario_id, human="pass" if human else "fail",
                judge="pass" if judge else "fail", judge_score=judge_eval.details.get("final_score")))
    report.n_matched = len(judge_v)
    report.confusion = confusion
    if judge_v:
        agree = sum(x == y for x, y in zip(judge_v, human_v, strict=True)) / len(judge_v)
        report.judge_vs_human = AgreementStats(n=len(judge_v), agreement=round(agree, 4),
                                               kappa=cohens_kappa(judge_v, human_v))
    return report


def export_labeling_sheet(db: Database, run_refs: list[str], out_path: str | Path,
                          scenarios_by_id: dict | None = None) -> int:
    """Write a blind labeling CSV (no judge verdicts). Returns the number of rows written."""
    rows = []
    for ref in run_refs:
        run = db.get_run(ref)
        for r in db.get_results(run.run_id):
            sc = (scenarios_by_id or {}).get(r.scenario_id)
            rows.append({
                "run_id": run.run_id, "scenario_id": r.scenario_id, "category": r.category,
                "agent_name": run.agent_name, "user_prompt": r.trace.user_prompt,
                "tool_calls": summarize_trace_for_judge(r.trace), "final_answer": r.trace.final_answer or "",
                "rubric_or_description": (sc.rubric if sc and sc.rubric else r.description),
                "human_label": "", "labeler": "", "notes": "",
            })
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LABEL_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def import_labels(csv_path: str | Path, labels_path: str | Path) -> tuple[int, int]:
    """Merge filled-in CSV rows into the labels JSON. Returns (added, skipped_blank)."""
    existing = load_labels(labels_path)
    key = {(lab.run_id, lab.scenario_id, lab.labeler): i for i, lab in enumerate(existing)}
    added = skipped = 0
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            value = (row.get("human_label") or "").strip().lower()
            if value in ("p", "1", "yes", "y", "true"):
                value = "pass"
            elif value in ("f", "0", "no", "n", "false"):
                value = "fail"
            if value not in ("pass", "fail"):
                skipped += 1
                continue
            lab = HumanLabel(run_id=row["run_id"], scenario_id=row["scenario_id"], human_label=value,
                             labeler=(row.get("labeler") or "unknown").strip() or "unknown",
                             notes=(row.get("notes") or "").strip())
            k = (lab.run_id, lab.scenario_id, lab.labeler)
            if k in key:
                existing[key[k]] = lab
            else:
                key[k] = len(existing)
                existing.append(lab)
                added += 1
    save_labels(labels_path, existing)
    return added, skipped
