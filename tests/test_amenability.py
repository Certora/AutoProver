"""Unit tests for certora-fv-amenability that need no AST-dump fixture.

The signal-detection tests run against a committed AST dump of a bait contract;
that dump and the fixture-backed tests are maintained in a separate private test
corpus (which mounts this package as a submodule).
"""

import json
import subprocess
import sys

import pytest

from certora_autosetup.amenability.compile import CannotScoreError, resolve_dumps
from certora_autosetup.amenability.report import Level
from certora_autosetup.amenability.scoring import ScoringConfig, aggregate
from certora_autosetup.amenability.signals.base import SignalResult


class TestScoring:
    def _results(self, config, overrides: dict[str, float]) -> list[SignalResult]:
        return [SignalResult(s, overrides.get(s, 1.0)) for s in config.weights]

    def test_clean_project_scores_high(self):
        config = ScoringConfig.load()
        static = aggregate(self._results(config, {}), config)
        assert static.provisional_level is Level.HIGH

    def test_few_severe_killers_is_not_low(self):
        # Fewer than killers_for_low structural killers firing → medium, never low,
        # even though those signals are severe (config-solvable friction).
        config = ScoringConfig.load()
        killers = list(config.structural_killers)
        overrides = {k: 0.1 for k in killers[: config.killers_for_low - 1]}
        static = aggregate(self._results(config, overrides), config)
        assert static.provisional_level is not Level.LOW

    def test_killer_cooccurrence_forces_low(self):
        # killers_for_low structural killers severe together + weak overall → low.
        config = ScoringConfig.load()
        killers = list(config.structural_killers)
        overrides = {k: 0.05 for k in killers[: config.killers_for_low]}
        # also drag the friction signals down so the mean is genuinely weak
        overrides.update({s: 0.2 for s in config.weights
                          if s not in config.structural_killers})
        static = aggregate(self._results(config, overrides), config)
        assert static.provisional_level is Level.LOW

    def test_non_structural_signals_never_force_low(self):
        # Even if every friction signal craters, without killer co-occurrence the
        # verdict stays medium — friction is config-solvable, not a rewrite.
        config = ScoringConfig.load()
        overrides = {s: 0.0 for s in config.weights
                     if s not in config.structural_killers}
        static = aggregate(self._results(config, overrides), config)
        assert static.provisional_level is not Level.LOW


class TestDumpResolution:
    def test_missing_explicit_dump(self, tmp_path):
        with pytest.raises(CannotScoreError) as exc:
            resolve_dumps(tmp_path, [tmp_path / "nope.json"])
        assert exc.value.error == "no-ast-dump"

    def test_no_dump_is_does_not_compile(self, tmp_path):
        with pytest.raises(CannotScoreError) as exc:
            resolve_dumps(tmp_path, [])
        assert exc.value.error == "does-not-compile"


class TestCli:
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "certora_autosetup.amenability.cli", *args],
            capture_output=True, text=True,
        )

    def test_cannot_score_without_compilation(self, tmp_path):
        proc = self._run(str(tmp_path))
        assert proc.returncode == 1
        err = json.loads(proc.stdout)
        assert err["error"] == "does-not-compile"


class TestJudgeGuardrails:
    def test_clamp_requires_two_citations(self):
        from certora_autosetup.amenability.judge.agent import _clamp

        level, clamped = _clamp(Level.LOW, Level.MEDIUM, citations=1)
        assert level is Level.LOW and clamped

    def test_clamp_limits_to_one_step(self):
        from certora_autosetup.amenability.judge.agent import _clamp

        level, clamped = _clamp(Level.LOW, Level.HIGH, citations=5)
        assert level is Level.MEDIUM and clamped

    def test_agreement_passes_through(self):
        from certora_autosetup.amenability.judge.agent import _clamp

        level, clamped = _clamp(Level.MEDIUM, Level.MEDIUM, citations=0)
        assert level is Level.MEDIUM and not clamped

    def test_one_step_with_citations_allowed(self):
        from certora_autosetup.amenability.judge.agent import _clamp

        level, clamped = _clamp(Level.MEDIUM, Level.HIGH, citations=2)
        assert level is Level.HIGH and not clamped

    def test_rubric_loads_with_version_and_sha(self):
        from certora_autosetup.amenability.judge.agent import load_rubric

        ver, sha, text = load_rubric()
        assert ver == "1"
        assert len(sha) == 64
        assert "low" in text and "Judging discipline" in text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
