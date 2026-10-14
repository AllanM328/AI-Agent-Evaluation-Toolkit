"""Configuration loading (config.yaml + environment overrides)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

CONFIG_ENV = "AGENTGAUGE_CONFIG"


class ModelsConfig(BaseModel):
    target_model_id: str | None = None
    judge_model_id: str | None = None
    region: str | None = None


class BudgetConfig(BaseModel):
    max_usd_per_run: float = 0.05
    max_usd_per_session: float = 2.0
    max_tokens_per_call: int = 1024
    max_tokens_per_run: int = 40_000
    max_tokens_per_session: int = 2_000_000
    estimate_safety_factor: float = 2.0


class AgentConfig(BaseModel):
    max_agent_steps: int = 8
    temperature: float = 0.0


class JudgeConfig(BaseModel):
    enabled: bool = True
    samples: int = 3
    temperature: float = 0.0
    pass_threshold: int = 4
    max_tokens: int = 512


class GroundednessConfig(BaseModel):
    mode: Literal["lexical", "llm"] = "lexical"
    min_overlap: float = 0.5
    pass_ratio: float = 0.75


class ScoringConfig(BaseModel):
    weights: dict[str, float] = Field(
        default_factory=lambda: {"correctness": 40.0, "safety": 40.0, "efficiency": 20.0}
    )


class PathsConfig(BaseModel):
    db: str = "agentgauge.db"
    scenarios: str = "scenarios"
    pricing: str = "pricing.yaml"
    labels: str = "validation/labeled_runs.json"


class Config(BaseModel):
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    groundedness: GroundednessConfig = Field(default_factory=GroundednessConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    base_dir: str = "."  # directory relative paths resolve against
    source: str | None = None  # config file that was loaded, if any

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        return p if p.is_absolute() else Path(self.base_dir) / p

    @property
    def db_path(self) -> Path:
        return self.resolve(self.paths.db)

    @property
    def scenarios_path(self) -> Path:
        return self.resolve(self.paths.scenarios)

    @property
    def pricing_path(self) -> Path:
        return self.resolve(self.paths.pricing)

    @property
    def labels_path(self) -> Path:
        return self.resolve(self.paths.labels)

    def snapshot(self) -> dict[str, Any]:
        """Config values recorded with each run (no secrets live in config)."""
        return self.model_dump(exclude={"base_dir", "source"})


def _find_config_file(path: str | Path | None) -> Path | None:
    if path:
        p = Path(path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {p}")
        return p
    env = os.environ.get(CONFIG_ENV)
    if env:
        return _find_config_file(env)
    cwd_cfg = Path.cwd() / "config.yaml"
    return cwd_cfg if cwd_cfg.exists() else None


def load_config(path: str | Path | None = None) -> Config:
    """Load config from `path`, $AGENTGAUGE_CONFIG, or ./config.yaml (defaults if none exist)."""
    cfg_file = _find_config_file(path)
    data: dict[str, Any] = {}
    if cfg_file is not None:
        data = yaml.safe_load(cfg_file.read_text()) or {}
    cfg = Config.model_validate(data)
    cfg.base_dir = str(cfg_file.parent.resolve()) if cfg_file else str(Path.cwd())
    cfg.source = str(cfg_file) if cfg_file else None

    env = os.environ
    if env.get("AGENTGAUGE_TARGET_MODEL_ID"):
        cfg.models.target_model_id = env["AGENTGAUGE_TARGET_MODEL_ID"]
    if env.get("AGENTGAUGE_JUDGE_MODEL_ID"):
        cfg.models.judge_model_id = env["AGENTGAUGE_JUDGE_MODEL_ID"]
    # AGENTGAUGE_REGION always wins; the generic AWS variables only fill an unset region.
    if env.get("AGENTGAUGE_REGION"):
        cfg.models.region = env["AGENTGAUGE_REGION"]
    elif not cfg.models.region:
        cfg.models.region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
    if env.get("AGENTGAUGE_DB"):
        cfg.paths.db = env["AGENTGAUGE_DB"]
    return cfg
