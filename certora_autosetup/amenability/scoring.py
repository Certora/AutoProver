"""Aggregate signal results into a level, driven entirely by weights.yaml."""

from dataclasses import dataclass
from pathlib import Path

import yaml

from certora_autosetup.amenability.report import Level, Severity, StaticReport, SubScore
from certora_autosetup.amenability.signals.base import SignalResult

DEFAULT_WEIGHTS = Path(__file__).parent / "weights.yaml"


@dataclass
class ScoringConfig:
    weights: dict[str, float]
    high_min: float
    low_max: float
    cap_at_medium_signals: set[str]
    severe_score: float
    severe_count_forces_low: int

    @classmethod
    def load(cls, path: Path | str = DEFAULT_WEIGHTS) -> "ScoringConfig":
        with open(path) as f:
            data = yaml.safe_load(f)
        thresholds = data["thresholds"]
        hard = data["hard_rules"]
        return cls(
            weights=data["weights"],
            high_min=thresholds["high_min"],
            low_max=thresholds["low_max"],
            cap_at_medium_signals=set(hard["cap_at_medium_on_high_evidence"]),
            severe_score=hard["severe_score"],
            severe_count_forces_low=hard["severe_count_forces_low"],
        )


def aggregate(results: list[SignalResult], config: ScoringConfig) -> StaticReport:
    sub_scores: dict[str, SubScore] = {}
    weighted_sum = 0.0
    weight_total = 0.0
    severe = 0
    capped_at_medium = False

    for r in results:
        weight = config.weights.get(r.signal_id, 1.0)
        sub_scores[r.signal_id] = SubScore(score=r.score, weight=weight, raw=r.raw)
        weighted_sum += r.score * weight
        weight_total += weight
        if weight > 0 and r.score <= config.severe_score:
            severe += 1
        if r.signal_id in config.cap_at_medium_signals and any(
            e.severity == Severity.HIGH for e in r.evidence
        ):
            capped_at_medium = True

    weighted = weighted_sum / weight_total if weight_total else 1.0

    if weighted >= config.high_min:
        level = Level.HIGH
    elif weighted < config.low_max:
        level = Level.LOW
    else:
        level = Level.MEDIUM

    if capped_at_medium and level == Level.HIGH:
        level = Level.MEDIUM
    if severe >= config.severe_count_forces_low:
        level = Level.LOW

    return StaticReport(
        provisional_level=level,
        weighted_score=round(weighted, 4),
        sub_scores=sub_scores,
    )
