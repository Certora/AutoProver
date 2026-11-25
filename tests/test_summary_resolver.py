"""Unit tests for ASTExtraction stdout parsing in summary_resolver.

Regression guard: ASTExtraction.jar (EVMVerifier OutPrinter) prints `Warning:`
diagnostic lines to stdout before the JSON payload, so a raw json.loads of stdout
fails at char 0. `_decode_ast_payload` must strip those leading lines and parse the
JSON that follows.
"""

import json

import pytest

from certora_autosetup.setup.summary_resolver import _decode_ast_payload

_AST = {"ast": {"importedMethods": []}}
_AST_JSON = json.dumps(_AST)


def test_pure_json_no_warnings():
    assert _decode_ast_payload(_AST_JSON, 0, "") == _AST


def test_warning_lines_before_json_are_stripped():
    stdout = (
        "Warning: Syntax warning in spec file dummy file:1:1: Registered the contract alias x\n"
        "Warning: Syntax warning in spec file dummy file:2:1: Registered the contract alias y\n"
        + _AST_JSON
    )
    assert _decode_ast_payload(stdout, 0, "") == _AST


def test_ast_null_is_kept_in_the_payload():
    assert _decode_ast_payload(json.dumps({"ast": None}), 1, "")["ast"] is None


def test_empty_stdout_raises_with_exit_and_stderr():
    with pytest.raises(RuntimeError, match="produced no JSON.*exit 3.*boom"):
        _decode_ast_payload("", 3, "boom")


def test_warnings_only_no_json_raises():
    with pytest.raises(RuntimeError, match="produced no JSON"):
        _decode_ast_payload("Warning: something\n", 0, "")


def test_non_json_after_stripping_raises_with_context():
    with pytest.raises(RuntimeError, match="was not JSON after stripping diagnostics"):
        _decode_ast_payload("Warning: w\nnot json at all", 0, "stderr text")


# ---------------------------------------------------------------------------
# extract_cvl_ast with `import` and `use` declarations (runs the real ASTExtraction.jar)
# ---------------------------------------------------------------------------

_USE_SPEC = """\
import "other.spec";

invariant inv() true;

use invariant inv {
    preserved { require true; }
}
use rule imported_rule filtered { f -> !f.isView }
use invariant named_elsewhere;  // a comment with ü after it

rule r() { assert true; }
"""


def _ast_jar_present() -> bool:
    from certora_cli.Shared.certoraUtils import find_jar
    from pathlib import Path
    return Path(find_jar("ASTExtraction.jar")).is_file()


@pytest.mark.skipif(not _ast_jar_present(), reason="needs the Certora CLI jars and Java")
def test_import_and_use_declarations_do_not_stop_the_parse():
    from composer.pipeline.property_tags import rule_lines

    assert rule_lines(_USE_SPEC) == {"r": (11, 11), "inv": (3, 3)}

