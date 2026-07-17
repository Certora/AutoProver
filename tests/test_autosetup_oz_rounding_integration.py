"""End-to-end autosetup tests for the OZ Math.Rounding v4/v5/mixed summaries.

Each case copies a test_scenarios fixture into tmp_path and runs the real
non-LLM autosetup pipeline (compilation analysis -> curated summary matching &
materialization -> typechecker loop) via the certora-autosetup CLI entry point
with in-process argv, then asserts on the materialized OZ_Math spec and on an
independent `certoraRun --compilation_steps_only` typecheck of the base conf.

Nothing here talks to the cloud: sanity/hashing/warmup and the ConfRunner tail
(only reached when sanity specs exist) are all skipped.
"""

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import pytest

pytestmark = [
    pytest.mark.expensive,
    pytest.mark.skipif(
        shutil.which("certoraRun") is None or shutil.which("solc") is None,
        reason="requires certoraRun and solc on PATH",
    ),
]

SCENARIOS_DIR = Path(__file__).parent.parent / "test_scenarios"


@dataclass
class Case:
    fixture: str
    main: str
    additional: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)


CASES = {
    "v4": Case(
        fixture="oz_math_v4",
        main="src/HarnessV4.sol:HarnessV4",
        files=["src/HarnessV4.sol:HarnessV4"],
    ),
    "v5": Case(
        fixture="oz_math_v5",
        main="src/HarnessV5.sol:HarnessV5",
        files=["src/HarnessV5.sol:HarnessV5"],
    ),
    "mixed": Case(
        fixture="oz_math_mixed",
        main="src/HarnessV5.sol:HarnessV5",
        additional=["src/HarnessV4.sol:HarnessV4"],
        files=["src/HarnessV5.sol:HarnessV5", "src/HarnessV4.sol:HarnessV4"],
    ),
}


def _run_autosetup(case: Case, tmp_path, monkeypatch) -> Path:
    """Copy the fixture to tmp, run the autosetup CLI in-process, return project dir."""
    project = tmp_path / case.fixture
    # Ignore tool-run artifacts a developer may have left inside the checked-in
    # fixture (the dirs its .gitignore anticipates) — stale certora/ trees would
    # otherwise leak into the test project and fail assertions spuriously.
    shutil.copytree(
        SCENARIOS_DIR / case.fixture,
        project,
        ignore=shutil.ignore_patterns(
            ".certora_internal", ".CertoraProverLiteReports", ".cachefs",
            "out", "cache", "certora",
        ),
    )
    monkeypatch.chdir(project)
    # Keep the content cache local to the tmp project (init_cache_fs falls back
    # to cwd when the SaaS env vars are absent).
    monkeypatch.delenv("PREAUDIT_S3_BUCKET", raising=False)
    monkeypatch.delenv("PREAUDIT_REPO_CACHE_PREFIX", raising=False)

    argv = [
        "certora-autosetup",
        "--main-contract", case.main,
        "--skip-llm",
        "--skip-sanity-setup",
        "--skip-proxy-detection",
        "--skip-harnessing",
        "--skip-hashing-bound-detection", "1024",
        "--skip-setup-check",
        "--skip-warmup",
        "--use-local-runner",
        "--no-cache",
        *case.files,
    ]
    if case.additional:
        argv += ["--additional-contracts", *case.additional]
    monkeypatch.setattr(sys, "argv", argv)

    from certora_autosetup.autosetup import cli

    with pytest.raises(SystemExit) as excinfo:  # cli.main() always sys.exit(0)s on success
        cli.main()
    assert excinfo.value.code == 0
    return project


def _read_oz_math_specs(project: Path) -> str:
    specs = sorted((project / "certora/specs/summaries/OpenZeppelin").glob("OZ_Math-*.spec"))
    assert specs, "no materialized OZ_Math spec found"
    return "\n".join(p.read_text() for p in specs)


def _uncommented(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))


def _assert_common(project: Path) -> None:
    certora_tree = list((project / "certora").rglob("*.spec")) + list(
        (project / "certora").rglob("*.conf")
    )
    for path in certora_tree:
        content = path.read_text()
        assert "AUTO-DISABLED" not in content, f"blind disable survived in {path}"
        assert "$UINT_ROUND_UP$" not in content and "$COMMENT" not in content, (
            f"unsubstituted placeholder in {path}"
        )


def _typecheck(project: Path, main_name: str) -> None:
    confs = list((project / "certora/confs").glob("*.conf"))
    assert confs, "no base conf generated"
    conf = next((c for c in confs if main_name in c.name), confs[0])
    result = subprocess.run(
        ["certoraRun", str(conf), "--compilation_steps_only"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, (
        f"final typecheck failed for {conf.name}:\n{result.stdout}\n{result.stderr}"
    )


def test_oz_v4_uses_up(tmp_path, monkeypatch) -> None:
    project = _run_autosetup(CASES["v4"], tmp_path, monkeypatch)
    spec = _read_oz_math_specs(project)
    assert "mulDivDirectionalSummary" in _uncommented(spec)
    assert "Math.Rounding.Up" in spec
    assert "Math.Rounding.Ceil" not in spec
    _assert_common(project)
    _typecheck(project, "HarnessV4")


def test_oz_v5_uses_ceil(tmp_path, monkeypatch) -> None:
    project = _run_autosetup(CASES["v5"], tmp_path, monkeypatch)
    spec = _read_oz_math_specs(project)
    assert "mulDivDirectionalSummary" in _uncommented(spec)
    assert "Math.Rounding.Ceil" in spec
    assert "Math.Rounding.Up" not in spec
    _assert_common(project)
    _typecheck(project, "HarnessV5")


def test_oz_mixed_qualifies_by_harness(tmp_path, monkeypatch) -> None:
    project = _run_autosetup(CASES["mixed"], tmp_path, monkeypatch)
    spec = _read_oz_math_specs(project)
    # Each Rounding definition qualified by the harness that imports it
    # (certora-cli >= 8.17.1 disambiguation), wildcard receivers throughout.
    assert "HarnessV4.Rounding.Up" in spec
    assert "HarnessV5.Rounding.Ceil" in spec
    assert "function _.mulDiv(" in spec
    # The purged ambiguous name must not survive outside comments.
    assert not re.search(r"\bMath\.Rounding\b", _uncommented(spec))
    assert not re.search(r"\bfunction Math\.", _uncommented(spec))
    _assert_common(project)
    _typecheck(project, "HarnessV5")
