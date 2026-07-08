"""Unit tests for ASTExtraction stdout parsing in summary_resolver.

Regression guard: ASTExtraction.jar (EVMVerifier OutPrinter) prints `Warning:`
diagnostic lines to stdout before the JSON payload, so a raw json.loads of stdout
fails at char 0. `_parse_ast_payload` must strip those leading lines and parse the
JSON that follows.
"""

import json

import pytest

from certora_autosetup.setup.summary_resolver import _parse_ast_payload

_AST = {"ast": {"importedMethods": []}}
_AST_JSON = json.dumps(_AST)


def test_pure_json_no_warnings():
    assert _parse_ast_payload(_AST_JSON, 0, "") == _AST


def test_warning_lines_before_json_are_stripped():
    stdout = (
        "Warning: Syntax warning in spec file dummy file:1:1: Registered the contract alias x\n"
        "Warning: Syntax warning in spec file dummy file:2:1: Registered the contract alias y\n"
        + _AST_JSON
    )
    assert _parse_ast_payload(stdout, 0, "") == _AST


def test_ast_null_returns_none():
    assert _parse_ast_payload(json.dumps({"ast": None}), 1, "") is None


def test_empty_stdout_raises_with_exit_and_stderr():
    with pytest.raises(RuntimeError, match="produced no JSON.*exit 3.*boom"):
        _parse_ast_payload("", 3, "boom")


def test_warnings_only_no_json_raises():
    with pytest.raises(RuntimeError, match="produced no JSON"):
        _parse_ast_payload("Warning: something\n", 0, "")


def test_non_json_after_stripping_raises_with_context():
    with pytest.raises(RuntimeError, match="was not JSON after stripping diagnostics"):
        _parse_ast_payload("Warning: w\nnot json at all", 0, "stderr text")
