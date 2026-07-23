"""Phase-2 LLM judge: a single structured call over the static report + code
excerpts, clamped to ±1 level of the static provisional score."""

from certora_autosetup.amenability.judge.agent import JudgeError, judge_report

__all__ = ["judge_report", "JudgeError"]
