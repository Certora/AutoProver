"""Unit tests for the harness compile gate (``composer.spec.source.harness``)."""

import json

from composer.spec.source.harness import _compile_check, _forge_errors


def _report(*diagnostics: dict) -> str:
    return json.dumps({
        "errors": list(diagnostics),
        "sources": {},
        "contracts": {},
        "build_infos": [],
    })


def test_forge_errors_keeps_errors_and_drops_warnings():
    out = _report(
        {
            "severity": "warning",
            "errorCode": "2394",
            "message": "Transient storage can break composability",
            "formattedMessage": "Warning: Transient storage can break composability",
        },
        {
            "severity": "error",
            "errorCode": "3656",
            "message": 'Contract "TokenInstance1" should be marked as abstract.',
            "formattedMessage": 'TypeError: Contract "TokenInstance1" should be marked as abstract.\n --> certora/harness_typecheck/TokenInstance1.sol:7:1:',
        },
    )
    assert _forge_errors(out) == [
        'TypeError: Contract "TokenInstance1" should be marked as abstract.\n --> certora/harness_typecheck/TokenInstance1.sol:7:1:'
    ]


def test_forge_errors_empty_on_clean_build():
    assert _forge_errors(_report({"severity": "warning", "message": "unused variable"})) == []
    assert _forge_errors(json.dumps({"sources": {}, "contracts": {}})) == []


def test_forge_errors_none_when_output_is_not_a_report():
    # forge failed before compiling — nothing to hold the harnesses against.
    assert _forge_errors("Error: failed to resolve remappings") is None
    assert _forge_errors("") is None


def test_compile_check_skipped_without_a_foundry_project(tmp_path):
    sources = {"certora/harnesses/TokenInstance1.sol": "contract TokenInstance1 is Token { }"}
    assert _compile_check(str(tmp_path), sources) is None


def test_compile_check_reports_paths_the_agent_knows(tmp_path, monkeypatch):
    """Diagnostics name ``certora/harnesses``, not the scratch directory the
    candidates were compiled from."""
    import composer.spec.source.harness as harness_mod

    (tmp_path / "foundry.toml").write_text("[profile.default]\n")
    monkeypatch.setattr(harness_mod.shutil, "which", lambda _: "/usr/local/bin/forge")

    class _Proc:
        stdout = _report({
            "severity": "error",
            "message": 'Contract "TokenInstance1" should be marked as abstract.',
            "formattedMessage": 'TypeError: Contract "TokenInstance1" should be marked as abstract.\n --> certora/harness_typecheck/TokenInstance1.sol:7:1:',
        })
        stderr = ""

    monkeypatch.setattr(harness_mod.subprocess, "run", lambda *a, **k: _Proc())

    result = _compile_check(
        str(tmp_path),
        {"certora/harnesses/TokenInstance1.sol": "contract TokenInstance1 is Token { }"},
    )
    assert result is not None
    assert "certora/harnesses/TokenInstance1.sol" in result
    assert "harness_typecheck" not in result
    # The scratch directory does not outlive the check.
    assert not (tmp_path / "certora" / "harness_typecheck").exists()
