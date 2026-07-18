"""Unit tests for certora-fv-amenability that need no AST-dump fixture.

The signal-detection tests run against a committed AST dump of a bait contract;
that dump and the fixture-backed tests are maintained in a separate private test
corpus (which mounts this package as a submodule). Set FV_AMENABILITY_FIXTURES to
a local checkout of that fixtures directory to run the dump-dependent tests here
as well; otherwise they are skipped.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from certora_autosetup.amenability.compile import CannotScoreError, resolve_dumps
from certora_autosetup.amenability.report import Level, Severity
from certora_autosetup.amenability.scoring import ScoringConfig, aggregate
from certora_autosetup.amenability.signals.base import SignalResult

# Optional local fixtures dir (the private repo mounts this package as a
# submodule and points here); when unset, the dump-dependent tests skip.
_FIXTURES_ENV = os.environ.get("FV_AMENABILITY_FIXTURES")
FIXTURES = Path(_FIXTURES_ENV) if _FIXTURES_ENV else None
_needs_fixtures = pytest.mark.skipif(
    FIXTURES is None or not (FIXTURES / "signals_bait.asts.json").is_file(),
    reason="set FV_AMENABILITY_FIXTURES to the fixtures dir to run dump-dependent tests",
)


class TestScoring:
    def test_hard_rule_caps_high_at_medium(self):
        config = ScoringConfig.load()
        from certora_autosetup.amenability.report import Evidence

        results = [
            SignalResult(sig_id, 1.0)
            for sig_id in config.weights
            if sig_id != "asm_fp_manipulation"
        ]
        results.append(SignalResult(
            "asm_fp_manipulation", 1.0,
            evidence=[Evidence(signal="asm_fp_manipulation", severity=Severity.HIGH,
                               file="a.sol", line=1, detail="mstore(0x40, ...)")],
        ))
        static = aggregate(results, config)
        assert static.provisional_level is Level.MEDIUM

    def test_severe_count_forces_low(self):
        config = ScoringConfig.load()
        ids = list(config.weights)
        results = [SignalResult(s, 0.2) for s in ids[:3]] + [
            SignalResult(s, 1.0) for s in ids[3:]
        ]
        static = aggregate(results, config)
        assert static.provisional_level is Level.LOW


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
