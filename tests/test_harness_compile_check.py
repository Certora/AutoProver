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
    assert _compile_check(str(tmp_path), ["certora/harnesses/TokenInstance1.sol"]) is None


def test_compile_check_builds_the_delivered_harnesses_in_place(tmp_path, monkeypatch):
    """forge is invoked on the delivered paths, inside the directory it was
    handed — the materialized project, whose layout the paths already match."""
    import composer.spec.source.harness as harness_mod

    (tmp_path / "foundry.toml").write_text("[profile.default]\n")
    monkeypatch.setattr(harness_mod.shutil, "which", lambda _: "/usr/local/bin/forge")
    invocations = []

    class _Proc:
        stdout = _report({
            "severity": "error",
            "message": 'Contract "TokenInstance1" should be marked as abstract.',
            "formattedMessage": 'TypeError: Contract "TokenInstance1" should be marked as abstract.\n --> certora/harnesses/TokenInstance1.sol:7:1:',
        })
        stderr = ""

    def _fake_run(cmd, **kwargs):
        invocations.append((cmd, kwargs["cwd"]))
        return _Proc()

    monkeypatch.setattr(harness_mod.subprocess, "run", _fake_run)

    result = _compile_check(
        str(tmp_path),
        ["certora/harnesses/TokenInstance2.sol", "certora/harnesses/TokenInstance1.sol"],
    )
    assert result is not None
    assert "certora/harnesses/TokenInstance1.sol" in result
    (cmd, cwd) = invocations[0]
    assert cmd[1:] == [
        "build",
        "--json",
        "certora/harnesses/TokenInstance1.sol",
        "certora/harnesses/TokenInstance2.sol",
    ]
    assert str(cwd) == str(tmp_path)
