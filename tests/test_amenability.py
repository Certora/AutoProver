"""Tests for certora-fv-amenability (phase 1: static scorer).

The signal tests run against a committed AST dump of a self-authored bait
contract (tests/fixtures/amenability/signals_bait.sol): PackedBook trips every
signal on purpose, CleanVault trips none. Regenerate the dump with
tests/fixtures/amenability/generate.py after editing the contract.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from certora_autosetup.amenability.compile import CannotScoreError, resolve_dumps
from certora_autosetup.amenability.context import AnalysisContext
from certora_autosetup.amenability.report import Level, Severity
from certora_autosetup.amenability.scoring import ScoringConfig, aggregate
from certora_autosetup.amenability.signals import ALL_SIGNALS
from certora_autosetup.amenability.signals.arithmetic import mixed_theory, unchecked_nonlinear
from certora_autosetup.amenability.signals.assembly import asm_fp_manipulation, asm_trampoline
from certora_autosetup.amenability.signals.base import SignalResult
from certora_autosetup.amenability.signals.bitmask import bitmask_style
from certora_autosetup.amenability.signals.functions import function_length
from certora_autosetup.amenability.signals.loops import dynamic_loops
from certora_autosetup.amenability.signals.storage import storage_packing
from certora_autosetup.amenability.signals.summaries import curated_summary_hits
from certora_autosetup.solidity_ast import AstDump

FIXTURES = Path(__file__).parent / "fixtures" / "amenability"


@pytest.fixture(scope="module")
def bait_ctx() -> AnalysisContext:
    dump = AstDump.load(FIXTURES / "signals_bait.asts.json")
    return AnalysisContext(project_root=FIXTURES, dumps=[dump])


class TestSignalsOnBait:
    def test_fp_manipulation_detected(self, bait_ctx):
        r = asm_fp_manipulation(bait_ctx)
        assert r.raw["fp_writes"] >= 1
        assert r.score <= 0.2
        assert any(e.severity == Severity.HIGH and "0x40" in e.detail for e in r.evidence)
        assert any(e.function == "PackedBook.forward" for e in r.evidence)

    def test_trampoline_detected(self, bait_ctx):
        r = asm_trampoline(bait_ctx)
        assert r.raw["asm_delegatecalls"] >= 1
        assert r.raw["solidity_delegatecalls"] >= 1
        assert r.score < 0.5
        assert any(e.severity == Severity.HIGH for e in r.evidence)

    def test_bitmask_inline_style_detected(self, bait_ctx):
        r = bitmask_style(bait_ctx)
        assert r.raw["inline_bit_ops"] >= 5
        # CleanVault's accessor-style bit use is counted on the accessor side
        assert r.raw["accessor_bit_ops"] >= 1

    def test_long_function_detected(self, bait_ctx):
        r = function_length(bait_ctx)
        assert r.raw["max_lines"] > 150
        assert any(e.function == "PackedBook.veryLong" for e in r.evidence)

    def test_unchecked_nonlinear_detected(self, bait_ctx):
        r = unchecked_nonlinear(bait_ctx)
        assert r.raw["sites"] >= 3
        assert any(e.function == "PackedBook.unsafeMath" for e in r.evidence)

    def test_mixed_theory_detected(self, bait_ctx):
        r = mixed_theory(bait_ctx)
        assert r.raw["flagged_functions"] >= 1
        assert "PackedBook.decodeAndPrice" in r.raw["per_function"]

    def test_hand_rolled_muldiv_detected(self, bait_ctx):
        r = curated_summary_hits(bait_ctx)
        assert any("mulDiv" in h for h in r.raw["hand_rolled"])
        assert r.score <= 0.3

    def test_computed_slot_storage_detected(self, bait_ctx):
        r = storage_packing(bait_ctx)
        assert r.raw["computed_slot_accesses"] >= 1
        assert any(e.function == "PackedBook.rawRead" for e in r.evidence)

    def test_dynamic_loops_detected(self, bait_ctx):
        r = dynamic_loops(bait_ctx)
        assert r.raw["dynamic_loops"] >= 2
        assert r.raw["storage_length_bounded"] >= 1

    def test_no_signal_fires_on_clean_contract_functions(self, bait_ctx):
        clean_evidence = [
            e
            for sig in ALL_SIGNALS
            for e in sig(bait_ctx).evidence
            if e.function and e.function.startswith("CleanVault.")
        ]
        assert clean_evidence == []

    def test_evidence_has_line_anchors(self, bait_ctx):
        r = asm_fp_manipulation(bait_ctx)
        assert all(e.line > 0 and e.file == "signals_bait.sol" for e in r.evidence)


class TestScoring:
    def test_bait_project_scores_low_or_medium(self, bait_ctx):
        config = ScoringConfig.load()
        static = aggregate([sig(bait_ctx) for sig in ALL_SIGNALS], config)
        assert static.provisional_level in (Level.LOW, Level.MEDIUM)

    def test_hard_rule_caps_high_at_medium(self):
        config = ScoringConfig.load()
        # all-perfect scores except one high-severity FP-manipulation finding
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
    def test_explicit_dump(self, tmp_path):
        res = resolve_dumps(tmp_path, [FIXTURES / "signals_bait.asts.json"])
        assert len(res.dumps) == 1

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

    def test_end_to_end_report(self):
        proc = self._run(str(FIXTURES), "--ast-dump", str(FIXTURES / "signals_bait.asts.json"))
        assert proc.returncode == 0, proc.stderr
        report = json.loads(proc.stdout)
        assert report["level"] in ("low", "medium")
        assert report["mode"] == "ast"
        assert {"PackedBook", "CleanVault"} <= set(report["contracts_analyzed"])
        assert report["evidence"], "expected evidence on the bait contract"
        assert report["recommendations"]
        assert report["static"]["sub_scores"]["surface_shape"]["weight"] == 0.0

    def test_cannot_score_without_compilation(self, tmp_path):
        proc = self._run(str(tmp_path))
        assert proc.returncode == 1
        err = json.loads(proc.stdout)
        assert err["error"] == "does-not-compile"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
