"""Compare LLM-judge verdicts with human labels (agreement rate + Cohen's kappa).

Usage:
    python validation/validate_judge.py [--labels validation/labeled_runs.json] [--db agentgauge.db]

Equivalent to `agentgauge validate-judge`. Labels are supplied by humans; see
`agentgauge export-labels` / `agentgauge import-labels` and docs/LIMITATIONS.md.
This file ships with NO labels: the agreement number stays "pending" until the team labels real runs.
"""

from __future__ import annotations

import argparse
import json
import sys

from agentgauge.config import load_config
from agentgauge.db import Database
from agentgauge.validation import validate_judge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--labels", help="labels JSON (default: paths.labels from config.yaml)")
    parser.add_argument("--db", help="SQLite database (default: paths.db from config.yaml)")
    parser.add_argument("--config", help="config.yaml path")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.db:
        cfg.paths.db = args.db
    report = validate_judge(Database(cfg.db_path), args.labels or cfg.labels_path)
    if args.json:
        print(json.dumps(report.model_dump(), indent=2))
        return 0
    print(f"labels: {report.labels_file} ({report.n_labels} labels, {report.n_items} items)")
    if report.status != "ok":
        print(report.status)
        return 0
    jh, hh = report.judge_vs_human, report.human_vs_human
    print(f"judge vs human: n={jh.n} agreement={jh.agreement} kappa={jh.kappa}")
    print(f"human vs human: n={hh.n} agreement={hh.agreement} kappa={hh.kappa}")
    print(f"confusion: {report.confusion}")
    for d in report.disagreements:
        print(f"  disagreement {d.run_id}/{d.scenario_id}: human={d.human} judge={d.judge} score={d.judge_score}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
