"""Load scenario YAML files."""

from __future__ import annotations

from pathlib import Path

import yaml

from agentgauge.models import CATEGORIES, Scenario


class ScenarioError(ValueError):
    pass


def load_scenario_file(path: Path) -> list[Scenario]:
    data = yaml.safe_load(path.read_text())
    items = data if isinstance(data, list) else [data]
    out = []
    for item in items:
        try:
            out.append(Scenario.model_validate(item))
        except Exception as exc:
            raise ScenarioError(f"{path}: {exc}") from exc
    return out


def load_scenarios(root: str | Path, suite: str = "all", ids: list[str] | None = None) -> list[Scenario]:
    """Load scenarios.

    `suite` is "all", a category name (functional | injection | efficiency), or a path to a YAML file
    or directory. `ids` optionally filters to specific scenario ids.
    """
    root = Path(root)
    if suite == "all":
        paths = sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml"))
    elif suite in CATEGORIES:
        paths = sorted((root / suite).glob("*.y*ml"))
    else:
        p = Path(suite)
        if p.is_dir():
            paths = sorted(p.rglob("*.y*ml"))
        elif p.is_file():
            paths = [p]
        else:
            raise ScenarioError(f"Unknown suite '{suite}': use all, {', '.join(CATEGORIES)}, or a path")
    scenarios: list[Scenario] = []
    for path in paths:
        scenarios.extend(load_scenario_file(path))
    seen: set[str] = set()
    for s in scenarios:
        if s.id in seen:
            raise ScenarioError(f"Duplicate scenario id '{s.id}'")
        seen.add(s.id)
    if suite in CATEGORIES:
        scenarios = [s for s in scenarios if s.category == suite]
    if ids:
        missing = set(ids) - seen
        if missing:
            raise ScenarioError(f"Unknown scenario id(s): {', '.join(sorted(missing))}")
        scenarios = [s for s in scenarios if s.id in ids]
    order = {c: i for i, c in enumerate(CATEGORIES)}
    return sorted(scenarios, key=lambda s: (order[s.category], s.id))
