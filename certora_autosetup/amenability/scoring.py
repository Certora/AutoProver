"""Aggregate signal results into a level, driven entirely by weights.yaml.

Level philosophy:
- `low` is RARE — reserved for the profile that needs a full reference
  implementation, where a small rewrite won't suffice (the whole execution model
  is hand-assembled: delegatecall trampolines + hand-rolled storage layouts +
  pervasive mixed-theory monoliths, several at once). It is decided by
  CO-OCCURRENCE of multiple severe *structural* killers, not by a low mean.
- `medium` is the default for a project with friction that autosetup can still
  handle with scoped config (summaries, harnesses, linking, loop_iter).
- `high` is a clean project that should pass as-is.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from certora_autosetup.amenability.report import Level, StaticReport, SubScore
from certora_autosetup.amenability.signals.base import SignalResult

DEFAULT_WEIGHTS = Path(__file__).parent / "weights.yaml"


@dataclass
class ScoringConfig:
    weights: dict[str, float]
    high_min: float
    structural_killers: set[str]
    killer_severe_score: float
    killers_for_low: int
    structural_low_max: float = field(default=0.5)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_WEIGHTS) -> "ScoringConfig":
        with open(path) as f:
            data = yaml.safe_load(f)
        thresholds = data["thresholds"]
        hard = data["hard_rules"]
        return cls(
            weights=data["weights"],
            high_min=thresholds["high_min"],
            structural_killers=set(hard["structural_killers"]),
            killer_severe_score=hard["killer_severe_score"],
            killers_for_low=hard["killers_for_low"],
            structural_low_max=thresholds.get("structural_low_max", 0.5),
        )


def aggregate(results: list[SignalResult], config: ScoringConfig) -> StaticReport:
    sub_scores: dict[str, SubScore] = {}
    weighted_sum = 0.0
    weight_total = 0.0
    severe_killers = 0

    for r in results:
        weight = config.weights.get(r.signal_id, 1.0)
        sub_scores[r.signal_id] = SubScore(score=r.score, weight=weight, raw=r.raw)
        weighted_sum += r.score * weight
        weight_total += weight
        if (r.signal_id in config.structural_killers
                and r.score <= config.killer_severe_score):
            severe_killers += 1

    weighted = weighted_sum / weight_total if weight_total else 1.0

    # `low` only when several structural killers fire together AND the overall
    # picture is genuinely weak — the reference-implementation profile. Anything
    # short of that is medium (config-solvable) or high (clean).
    if severe_killers >= config.killers_for_low and weighted < config.structural_low_max:
        level = Level.LOW
    elif weighted >= config.high_min:
        level = Level.HIGH
    else:
        level = Level.MEDIUM

    return StaticReport(
        provisional_level=level,
        weighted_score=round(weighted, 4),
        sub_scores=sub_scores,
    )
