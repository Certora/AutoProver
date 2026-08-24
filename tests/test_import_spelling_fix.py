"""Unit tests for the import spelling fixer.

Every test writes ONE real file and a mis-spelled import string, then asserts on the plan or
on the rewritten bytes. That is deliberate: the developer filesystem (macOS) is
case-insensitive and cannot hold two files differing only in case, so a test that needed such
a pair would pass only on a case-sensitive filesystem. The one place a pair is unavoidable —
two entries in a directory matching one component — is covered against the pure
``choose_spelling`` helper, which takes the entry names as an argument.
"""

from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from certora_autosetup.setup.import_spelling_fix import (
    ImportSpellingRewrite,
    mask_comments,
    apply_import_spelling_fixes,
    choose_spelling,
    plan_import_spelling_fixes,
    revert_import_spelling_fixes,
)

# Shaped like certoraRun/solc output for a source-not-found failure. solc hard-wraps its
# diagnostics, so the phrase is split across lines; the fixer only logs what it finds here,
# but it must not choke on the wrapping.
WRAPPED_SOURCE_NOT_FOUND = (
    "Compiling src/Main.sol...\n"
    'src/Main.sol:4:1: ParserError: Source "src/interfaces/iwidgetstore.sol" not\n'
    "found: File not found.\n"
    ' --> src/Main.sol:4:1:\n'
)

CONTRACT_BODY = "\ncontract Main {}\n"


class RecordingLog:
    """Log function that keeps what it was called with, like a caplog for a log_func."""

    def __init__(self) -> None:
        self.records: List[Tuple[str, str]] = []

    def __call__(self, message: str, level: str = "INFO") -> None:
        self.records.append((level, message))

    def messages(self, level: Optional[str] = None) -> List[str]:
        return [text for record_level, text in self.records if level in (None, record_level)]


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def source_with_import(import_path: str) -> str:
    return (
        "// SPDX-License-Identifier: UNLICENSED\n"
        "pragma solidity ^0.8.20;\n"
        "\n"
        f'import {{IWidgetStore}} from "{import_path}";\n'
        f"{CONTRACT_BODY}"
    )


@pytest.fixture
def log() -> RecordingLog:
    return RecordingLog()


def test_basename_case_is_fixed(tmp_path: Path, log: RecordingLog) -> None:
    write(tmp_path / "src" / "IWidgetStore.sol", "interface IWidgetStore {}\n")
    main = write(tmp_path / "src" / "Main.sol", source_with_import("./iwidgetstore.sol"))
    before = main.read_bytes()

    rewrites = plan_import_spelling_fixes(tmp_path, log_func=log)

    assert [(r.file, r.original_path, r.updated_path) for r in rewrites] == [
        (main, "./iwidgetstore.sol", "./IWidgetStore.sol")
    ]
    assert apply_import_spelling_fixes(rewrites, log_func=log) == rewrites
    assert 'from "./IWidgetStore.sol";' in main.read_text()
    # A rewrite is a single-line, in-place substitution: the line count cannot move, or the
    # import patcher's line-indexed revert would corrupt the file.
    assert len(main.read_text().splitlines()) == len(before.decode().splitlines())
    assert any("-> " in message for message in log.messages("WARNING"))


def test_directory_component_case_is_fixed(tmp_path: Path, log: RecordingLog) -> None:
    write(tmp_path / "src" / "interfaces" / "IWidgetStore.sol", "interface IWidgetStore {}\n")
    main = write(
        tmp_path / "src" / "Main.sol", source_with_import("./Interfaces/IWidgetStore.sol")
    )

    rewrites = plan_import_spelling_fixes(tmp_path, log_func=log)

    assert [r.updated_path for r in rewrites] == ["./interfaces/IWidgetStore.sol"]


def test_extension_case_is_fixed(tmp_path: Path, log: RecordingLog) -> None:
    write(tmp_path / "src" / "IWidgetStore.sol", "interface IWidgetStore {}\n")
    write(tmp_path / "src" / "Main.sol", source_with_import("./IWidgetStore.SOL"))

    rewrites = plan_import_spelling_fixes(tmp_path, log_func=log)

    assert [r.updated_path for r in rewrites] == ["./IWidgetStore.sol"]


def test_a_missing_extension_is_never_invented(tmp_path: Path, log: RecordingLog) -> None:
    write(tmp_path / "src" / "IWidgetStore.sol", "interface IWidgetStore {}\n")
    write(tmp_path / "src" / "Main.sol", source_with_import("./IWidgetStore"))

    assert plan_import_spelling_fixes(tmp_path, log_func=log) == []


def test_windows_separators_are_normalized(tmp_path: Path, log: RecordingLog) -> None:
    write(tmp_path / "src" / "IWidgetStore.sol", "interface IWidgetStore {}\n")
    write(tmp_path / "src" / "Main.sol", source_with_import("src\\IWidgetStore.sol"))

    rewrites = plan_import_spelling_fixes(tmp_path, log_func=log)

    assert [r.updated_path for r in rewrites] == ["src/IWidgetStore.sol"]


def test_multiline_import_is_rewritten_on_its_path_line(
    tmp_path: Path, log: RecordingLog
) -> None:
    write(tmp_path / "src" / "interfaces" / "IWidgetStore.sol", "interface IWidgetStore {}\n")
    main = write(
        tmp_path / "src" / "Main.sol",
        "pragma solidity ^0.8.20;\n"
        "\n"
        "import {\n"
        "    IWidgetStore\n"
        '} from "./interfaces/iwidgetstore.sol";\n'
        f"{CONTRACT_BODY}",
    )

    rewrites = plan_import_spelling_fixes(tmp_path, log_func=log)

    assert len(rewrites) == 1
    # The path literal of a multi-line import sits on the closing line, not the `import` line.
    assert rewrites[0].line == 4
    apply_import_spelling_fixes(rewrites, log_func=log)
    assert '} from "./interfaces/IWidgetStore.sol";' in main.read_text()


def test_right_basename_in_the_wrong_directory_is_relocated(
    tmp_path: Path, log: RecordingLog
) -> None:
    write(tmp_path / "src" / "utils" / "Helper.sol", "library Helper {}\n")
    write(tmp_path / "src" / "Main.sol", source_with_import("./Helper.sol"))

    rewrites = plan_import_spelling_fixes(tmp_path, log_func=log)

    assert [r.updated_path for r in rewrites] == ["./utils/Helper.sol"]


def test_two_candidates_for_one_basename_abort(tmp_path: Path, log: RecordingLog) -> None:
    write(tmp_path / "src" / "a" / "Helper.sol", "library Helper {}\n")
    write(tmp_path / "src" / "b" / "Helper.sol", "library Helper {}\n")
    write(tmp_path / "src" / "Main.sol", source_with_import("./Helper.sol"))

    assert plan_import_spelling_fixes(tmp_path, log_func=log) == []
    assert any("2 files on disk are named" in message for message in log.messages())


def test_entries_differing_only_in_case_are_not_guessed_between() -> None:
    # Two files differing only in case are legal on a case-sensitive filesystem, and no
    # tiebreak is defensible for a source rewrite.
    chosen, reason = choose_spelling("helper.sol", ["Helper.sol", "helper.SOL"])

    assert chosen is None
    assert reason is not None and "matches 2 entries" in reason


def test_an_exact_entry_wins_over_a_case_variant() -> None:
    assert choose_spelling("Helper.sol", ["Helper.sol", "helper.sol"]) == ("Helper.sol", None)


def test_package_prefix_import_is_left_alone(tmp_path: Path, log: RecordingLog) -> None:
    write(tmp_path / "lib" / "vendor" / "src" / "Token.sol", "contract Token {}\n")
    write(tmp_path / "src" / "Main.sol", source_with_import("@vendor/src/token.sol"))

    assert plan_import_spelling_fixes(tmp_path, log_func=log) == []
    assert any("package prefix" in message for message in log.messages())


def test_unknown_root_segment_is_left_alone(tmp_path: Path, log: RecordingLog) -> None:
    write(tmp_path / "lib" / "vendor" / "src" / "Token.sol", "contract Token {}\n")
    write(tmp_path / "src" / "Main.sol", source_with_import("vendor/src/token.sol"))

    assert plan_import_spelling_fixes(tmp_path, log_func=log) == []


def test_a_project_relative_import_into_a_dependency_is_fixed(
    tmp_path: Path, log: RecordingLog
) -> None:
    write(tmp_path / "lib" / "vendor" / "src" / "Token.sol", "contract Token {}\n")
    write(tmp_path / "src" / "Main.sol", source_with_import("lib/vendor/src/token.sol"))

    rewrites = plan_import_spelling_fixes(tmp_path, log_func=log)

    assert [r.updated_path for r in rewrites] == ["lib/vendor/src/Token.sol"]


def test_a_file_inside_a_dependency_is_never_rewritten(
    tmp_path: Path, log: RecordingLog
) -> None:
    write(tmp_path / "lib" / "vendor" / "src" / "helpers" / "Helper.sol", "library Helper {}\n")
    dependency_source = write(
        tmp_path / "lib" / "vendor" / "src" / "Token.sol",
        source_with_import("./helpers/helper.sol"),
    )
    before = dependency_source.read_bytes()
    write(tmp_path / "src" / "IWidgetStore.sol", "interface IWidgetStore {}\n")
    main = write(tmp_path / "src" / "Main.sol", source_with_import("./iwidgetstore.sol"))

    rewrites = plan_import_spelling_fixes(tmp_path, log_func=log)

    # The dependency carries the same class of mis-spelling and is still not touched.
    assert [r.file for r in rewrites] == [main]
    apply_import_spelling_fixes(rewrites, log_func=log)
    assert dependency_source.read_bytes() == before


def test_a_genuinely_absent_file_is_left_alone(tmp_path: Path, log: RecordingLog) -> None:
    main = write(tmp_path / "src" / "Main.sol", source_with_import("./interfaces/IMissing.sol"))
    before = main.read_bytes()

    assert plan_import_spelling_fixes(tmp_path, log_func=log) == []
    assert main.read_bytes() == before


def test_solc_output_is_logged_but_does_not_gate_the_scan(
    tmp_path: Path, log: RecordingLog
) -> None:
    write(tmp_path / "src" / "interfaces" / "IWidgetStore.sol", "interface IWidgetStore {}\n")
    write(tmp_path / "src" / "Main.sol", source_with_import("./interfaces/iwidgetstore.sol"))

    rewrites = plan_import_spelling_fixes(
        tmp_path, log_func=log, compiler_output=WRAPPED_SOURCE_NOT_FOUND
    )

    assert len(rewrites) == 1
    assert any(
        "src/interfaces/iwidgetstore.sol" in message and "solc reported" in message
        for message in log.messages()
    )


def test_apply_then_revert_restores_the_bytes(tmp_path: Path, log: RecordingLog) -> None:
    write(tmp_path / "src" / "interfaces" / "IWidgetStore.sol", "interface IWidgetStore {}\n")
    main = write(
        tmp_path / "src" / "Main.sol", source_with_import("./Interfaces/iwidgetstore.sol")
    )
    before = main.read_bytes()

    rewrites = plan_import_spelling_fixes(tmp_path, log_func=log)
    applied = apply_import_spelling_fixes(rewrites, log_func=log)
    assert applied and main.read_bytes() != before

    assert revert_import_spelling_fixes(applied, log_func=log) == applied
    assert main.read_bytes() == before


def test_a_literal_occurring_twice_on_a_line_is_skipped(
    tmp_path: Path, log: RecordingLog
) -> None:
    # Two statements on one line name the same path, so neither occurrence is the one an
    # unambiguous column can be recorded for.
    write(tmp_path / "src" / "IWidgetStore.sol", "interface IWidgetStore {}\n")
    write(
        tmp_path / "src" / "Main.sol",
        'import {A} from "./iwidgetstore.sol"; import {B} from "./iwidgetstore.sol";\n'
        f"{CONTRACT_BODY}",
    )

    assert plan_import_spelling_fixes(tmp_path, log_func=log) == []
    assert any("occurs 2 times" in message for message in log.messages())


def test_a_trailing_comment_repeating_the_path_does_not_block_the_fix(
    tmp_path: Path, log: RecordingLog
) -> None:
    """The path literal appears twice in the raw line but once in code, so the column is
    unambiguous and the import is fixed."""
    write(tmp_path / "src" / "IWidgetStore.sol", "interface IWidgetStore {}\n")
    main = write(
        tmp_path / "src" / "Main.sol",
        'import "./iwidgetstore.sol"; // see also "./iwidgetstore.sol"\n'
        f"{CONTRACT_BODY}",
    )

    planned = plan_import_spelling_fixes(tmp_path, log_func=log)
    assert [(r.original_path, r.updated_path) for r in planned] == [
        ("./iwidgetstore.sol", "./IWidgetStore.sol")
    ]
    apply_import_spelling_fixes(planned, log_func=log)
    text = main.read_text()
    assert text.startswith('import "./IWidgetStore.sol";')
    # The comment keeps the spelling it had; only code was rewritten.
    assert 'see also "./iwidgetstore.sol"' in text


def test_two_rewrites_on_one_line_round_trip(tmp_path: Path, log: RecordingLog) -> None:
    # Written by hand: the extractor yields one import per statement, so two literals on one
    # line only reach the writer when a caller plans them. The columns are recorded against
    # the original line, so applying the left one moves the right one.
    line = 'import "./a.sol"; import "./bb.sol";\n'
    source = write(tmp_path / "src" / "Main.sol", line)
    before = source.read_bytes()
    rewrites = [
        ImportSpellingRewrite(
            file=source, line=0, column=line.index('"./a.sol"'),
            original='"./a.sol"', updated='"./AAAA.sol"',
        ),
        ImportSpellingRewrite(
            file=source, line=0, column=line.index('"./bb.sol"'),
            original='"./bb.sol"', updated='"./B.sol"',
        ),
    ]

    assert apply_import_spelling_fixes(rewrites, log_func=log) == rewrites
    assert source.read_text() == 'import "./AAAA.sol"; import "./B.sol";\n'
    revert_import_spelling_fixes(rewrites, log_func=log)
    assert source.read_bytes() == before


def test_a_dependency_copy_counts_towards_the_ambiguity(
    tmp_path: Path, log: RecordingLog
) -> None:
    # The rewrite would only ever point at the project's own file, but a dependency holding the
    # same basename means the filesystem does not decide which file the import named.
    write(tmp_path / "lib" / "vendor" / "src" / "Token.sol", "contract Token {}\n")
    write(tmp_path / "src" / "mocks" / "Token.sol", "contract Token {}\n")
    write(tmp_path / "src" / "Main.sol", source_with_import("./Token.sol"))

    assert plan_import_spelling_fixes(tmp_path, log_func=log) == []
    assert any("2 files on disk are named" in message for message in log.messages())


def test_an_import_into_an_installed_dependency_is_not_redirected(
    tmp_path: Path, log: RecordingLog
) -> None:
    # The dependency file the import names exists; only the directory chain leading to it is
    # wrong, which is a dependency-layout problem and not the project's mock's business.
    write(tmp_path / "lib" / "vendor" / "src" / "tokens" / "Token.sol", "contract Token {}\n")
    write(tmp_path / "src" / "mocks" / "Token.sol", "contract Token {}\n")
    write(tmp_path / "src" / "Main.sol", source_with_import("../lib/vendor/tokens/Token.sol"))

    assert plan_import_spelling_fixes(tmp_path, log_func=log) == []
    assert any("inside a dependency checkout" in message for message in log.messages())


def test_an_import_into_an_absent_dependency_is_not_redirected(
    tmp_path: Path, log: RecordingLog
) -> None:
    # No lib/ at all — the state a failed dependency install leaves, and the state in which the
    # project's own same-named mock is the most tempting wrong answer.
    write(tmp_path / "src" / "mocks" / "Token.sol", "contract Token {}\n")
    write(tmp_path / "src" / "Main.sol", source_with_import("../lib/vendor/src/Token.sol"))

    assert plan_import_spelling_fixes(tmp_path, log_func=log) == []
    assert any("inside a dependency checkout" in message for message in log.messages())


def test_a_remapped_prefix_wins_over_a_root_directory_of_the_same_name(
    tmp_path: Path, log: RecordingLog
) -> None:
    # `contracts/` is both a real project directory and a remapping prefix: solc resolves the
    # import through the remapping, so the project tree has no say in where it leads.
    write(tmp_path / "contracts" / "Readme.sol", "// nothing to import\n")
    write(tmp_path / "src" / "Token.sol", "contract Token {}\n")
    write(tmp_path / "src" / "Main.sol", source_with_import("contracts/Token.sol"))

    assert plan_import_spelling_fixes(
        tmp_path, log_func=log, packages=["contracts/=lib/vendor/contracts/"]
    ) == []
    assert any("remaps the prefix 'contracts/'" in message for message in log.messages())
    # Without that packages entry the same import is the project's to resolve.
    assert [r.updated_path for r in plan_import_spelling_fixes(tmp_path, log_func=log)] == [
        "src/Token.sol"
    ]


def test_crlf_line_endings_survive_apply_and_revert(tmp_path: Path, log: RecordingLog) -> None:
    write(tmp_path / "src" / "IWidgetStore.sol", "interface IWidgetStore {}\n")
    main = tmp_path / "src" / "Main.sol"
    main.write_bytes(source_with_import("./iwidgetstore.sol").replace("\n", "\r\n").encode())
    before = main.read_bytes()

    rewrites = plan_import_spelling_fixes(tmp_path, log_func=log)
    applied = apply_import_spelling_fixes(rewrites, log_func=log)

    assert applied and b'"./IWidgetStore.sol"' in main.read_bytes()
    assert main.read_bytes().count(b"\r\n") == before.count(b"\r\n")
    revert_import_spelling_fixes(applied, log_func=log)
    assert main.read_bytes() == before


# ---------------------------------------------------------------------------
# Comment masking
# ---------------------------------------------------------------------------


def test_mask_comments_blanks_a_line_comment_but_keeps_the_terminator() -> None:
    masked = mask_comments(["uint x; // note\n"])
    assert masked == ["uint x;        \n"]
    assert len(masked[0]) == len("uint x; // note\n")


def test_mask_comments_spans_a_block_comment_across_lines() -> None:
    lines = ["a;\n", "/* import Old\n", "   still comment\n", "*/ b;\n"]
    masked = mask_comments(lines)
    assert masked[0] == "a;\n"
    assert masked[1].strip() == ""
    assert masked[2].strip() == ""
    assert masked[3] == "   b;\n"
    assert [len(m) for m in masked] == [len(line) for line in lines]


def test_mask_comments_leaves_a_url_inside_a_string_alone() -> None:
    # The reason this is a scanner and not a `//` match: the slashes here are data.
    line = 'string constant U = "https://example.com/x"; // trailing\n'
    masked = mask_comments([line])[0]
    assert 'string constant U = "https://example.com/x";' in masked
    assert "trailing" not in masked
    assert len(masked) == len(line)


def test_mask_comments_ignores_comment_openers_inside_strings() -> None:
    line = 'string constant S = "/* not a comment */"; uint y;\n'
    masked = mask_comments([line])[0]
    assert masked == line


def test_mask_comments_handles_an_escaped_quote_before_a_comment() -> None:
    line = 'string constant S = "a\\"b"; // gone\n'
    masked = mask_comments([line])[0]
    assert 'string constant S = "a\\"b";' in masked
    assert "gone" not in masked


def test_a_commented_out_import_is_not_planned(tmp_path: Path, log: RecordingLog) -> None:
    """A commented-out import with no semicolon used to make the scan run on to the next `;`
    anywhere in the file and rewrite whatever literal it found there."""
    root = tmp_path
    (root / "src").mkdir()
    (root / "src" / "Config.sol").write_text("// config\n")
    main = root / "src" / "Main.sol"
    main.write_text(
        "pragma solidity ^0.8.0;\n"
        "/*\n"
        "import OldThing\n"
        "*/\n"
        'contract Main { string constant P = "./nope/Config.sol"; }\n'
    )
    before = main.read_bytes()

    assert plan_import_spelling_fixes(root, log_func=log) == []
    assert main.read_bytes() == before


def test_a_real_import_after_a_commented_one_is_still_fixed(tmp_path: Path, log: RecordingLog) -> None:
    root = tmp_path
    (root / "src").mkdir()
    (root / "src" / "Config.sol").write_text("// config\n")
    main = root / "src" / "Main.sol"
    main.write_text(
        "pragma solidity ^0.8.0;\n"
        "// import {Old} from './Old.sol';\n"
        'import {Config} from "./config.sol";\n'
        "contract Main {}\n"
    )

    planned = plan_import_spelling_fixes(root, log_func=log)
    assert [(r.original_path, r.updated_path) for r in planned] == [("./config.sol", "./Config.sol")]
    apply_import_spelling_fixes(planned, log_func=log)
    assert 'import {Config} from "./Config.sol";' in main.read_text()
    # The commented line is untouched, comment and all.
    assert "// import {Old} from './Old.sol';" in main.read_text()
