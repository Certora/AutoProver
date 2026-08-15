"""Tests for classifying `ParserError: Source "…" not found` against the conf's packages.

Two independent halves: parsing the source unit names (and importers) out of wrap-prone solc
output, and deciding — from the packages list plus the filesystem — which cause each one is.
"""

from pathlib import Path

from certora_autosetup.utils.import_diagnostics import (
    UnresolvedImportKind,
    classify_unresolved_import,
    describe_unresolved_imports,
    parse_unresolved_imports,
)
from tests.test_compilation_workarounds import (
    SINGLE_LINE_SOURCE_NOT_FOUND,
    UNRELATED_OUTPUT,
    WRAPPED_SOURCE_NOT_FOUND_SPLIT_AT_FILE,
    WRAPPED_SOURCE_NOT_FOUND_SPLIT_AT_QUOTE,
)

# solc's own shape: the diagnostic first, the importing file on the following `-->` line.
SOURCE_NOT_FOUND_WITH_IMPORTER = (
    'ParserError: Source "@vault/core/contracts/IVault.sol" not found: File not found.\n'
    "  --> smart-contracts/src/Widget.sol:5:1:\n"
    "   |\n"
    '5  | import "@vault/core/contracts/IVault.sol";\n'
)

TWO_SOURCES_NOT_FOUND = (
    'ParserError: Source "@vault/core/contracts/IVault.sol" not found: File not found.\n'
    "  --> src/Widget.sol:5:1:\n"
    'ParserError: Source "solady/utils/FixedPointMathLib.sol" not found: File not found.\n'
    "  --> src/Vault.sol:7:1:\n"
)


def test_parses_wrap_split_at_quote() -> None:
    assert parse_unresolved_imports(WRAPPED_SOURCE_NOT_FOUND_SPLIT_AT_QUOTE) == [
        ("@openzeppelin/contracts/token/ERC20/IERC20.sol", None)
    ]


def test_parses_wrap_split_at_file() -> None:
    assert parse_unresolved_imports(WRAPPED_SOURCE_NOT_FOUND_SPLIT_AT_FILE) == [
        ("solady/utils/FixedPointMathLib.sol", None)
    ]


def test_parses_single_line_form() -> None:
    assert parse_unresolved_imports(SINGLE_LINE_SOURCE_NOT_FOUND) == [("src/Foo.sol", None)]


def test_parses_nothing_from_unrelated_output() -> None:
    assert parse_unresolved_imports(UNRELATED_OUTPUT) == []


def test_associates_the_importer_from_the_source_location_line() -> None:
    assert parse_unresolved_imports(SOURCE_NOT_FOUND_WITH_IMPORTER) == [
        ("@vault/core/contracts/IVault.sol", "smart-contracts/src/Widget.sol")
    ]


def test_each_error_keeps_its_own_importer() -> None:
    assert parse_unresolved_imports(TWO_SOURCES_NOT_FOUND) == [
        ("@vault/core/contracts/IVault.sol", "src/Widget.sol"),
        ("solady/utils/FixedPointMathLib.sol", "src/Vault.sol"),
    ]


def test_missing_target_directory_is_a_package_target_miss(tmp_path: Path) -> None:
    # The remapping fired (the source unit lives under its target) and the target does not
    # exist: the dependency is not installed there. This is the class the ancestor walk fixes.
    packages = ["@vault/=smart-contracts/node_modules/@vault/core/"]

    failure = classify_unresolved_import(
        "smart-contracts/node_modules/@vault/core/IVault.sol", packages, tmp_path
    )

    assert failure.kind == UnresolvedImportKind.PACKAGE_TARGET_MISSING
    assert failure.package_key == "@vault/"


def test_installed_package_without_the_remapped_subdirectory_is_its_own_class(
    tmp_path: Path,
) -> None:
    # `resolve_node_modules_target` already decided this case (kind `subpath_missing`) by finding
    # the package directory, so classifying it as "not installed anywhere up to the run root"
    # would contradict the resolver and name a remedy that cannot apply.
    (tmp_path / "node_modules" / "@oz" / "contracts").mkdir(parents=True)
    packages = ["@oz/=node_modules/@oz/contracts/token/"]

    failure = classify_unresolved_import(
        "node_modules/@oz/contracts/token/ERC20.sol", packages, tmp_path
    )

    assert failure.kind == UnresolvedImportKind.PACKAGE_SUBPATH_MISSING
    assert failure.package_root == str(tmp_path / "node_modules/@oz/contracts")
    described = describe_unresolved_imports([failure])
    assert "the package is installed at" in described
    assert "not found in any ancestor" not in described


def test_missing_package_directory_stays_a_package_target_miss(tmp_path: Path) -> None:
    # Same target shape, nothing installed: the class the ancestor walk resolves, unchanged.
    packages = ["@oz/=node_modules/@oz/contracts/token/"]

    failure = classify_unresolved_import(
        "node_modules/@oz/contracts/token/ERC20.sol", packages, tmp_path
    )

    assert failure.kind == UnresolvedImportKind.PACKAGE_TARGET_MISSING
    assert failure.package_root is None


def test_missing_non_node_modules_target_stays_a_package_target_miss(tmp_path: Path) -> None:
    # forge/soldeer targets have no package root to fall back on, so they keep the plain class.
    failure = classify_unresolved_import(
        "lib/openzeppelin/contracts/token/ERC20.sol",
        ["@oz/=lib/openzeppelin/contracts/"],
        tmp_path,
    )

    assert failure.kind == UnresolvedImportKind.PACKAGE_TARGET_MISSING


def test_existing_target_directory_is_a_file_miss(tmp_path: Path) -> None:
    # The package is installed; the file inside it is what is missing, so rebuilding the
    # packages list provably cannot help.
    (tmp_path / "node_modules" / "@vault" / "core").mkdir(parents=True)
    packages = ["@vault/=node_modules/@vault/core/"]

    failure = classify_unresolved_import(
        "node_modules/@vault/core/IVault.sol", packages, tmp_path
    )

    assert failure.kind == UnresolvedImportKind.FILE_MISSING_IN_PACKAGE


def test_absolute_and_relative_spellings_of_the_same_target_both_match(tmp_path: Path) -> None:
    (tmp_path / "node_modules" / "@vault" / "core").mkdir(parents=True)
    absolute = [f"@vault/={tmp_path / 'node_modules/@vault/core'}/"]
    relative = ["@vault/=node_modules/@vault/core/"]
    source_unit = str(tmp_path / "node_modules/@vault/core/IVault.sol")

    from_absolute = classify_unresolved_import(source_unit, absolute, tmp_path)
    from_relative = classify_unresolved_import(source_unit, relative, tmp_path)

    assert from_absolute.kind == UnresolvedImportKind.FILE_MISSING_IN_PACKAGE
    assert from_relative.kind == UnresolvedImportKind.FILE_MISSING_IN_PACKAGE


def test_import_no_entry_covers_is_unmapped(tmp_path: Path) -> None:
    failure = classify_unresolved_import(
        "@vault/core/IVault.sol", ["@widget/=lib/widget/"], tmp_path
    )

    assert failure.kind == UnresolvedImportKind.UNMAPPED_IMPORT
    assert failure.package_key is None


def test_project_tree_source_is_a_missing_project_file(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()

    failure = classify_unresolved_import("src/Widget.sol", [], tmp_path)

    assert failure.kind == UnresolvedImportKind.MISSING_PROJECT_FILE


def test_context_scoped_key_that_misses_the_importer_carries_a_hint(tmp_path: Path) -> None:
    # The key covers the import textually, so the remapping was declared — its context is why
    # it never applied. That needs the importer, which solc does not always print, so it is a
    # hint on an UNMAPPED_IMPORT rather than a kind of its own.
    packages = ["src/Widget/:@vault/=src/Widget/dependencies/vault/"]

    failure = classify_unresolved_import(
        "@vault/IVault.sol", packages, tmp_path, importer="smart-contracts/src/Widget/Widget.sol"
    )

    assert failure.kind == UnresolvedImportKind.UNMAPPED_IMPORT
    assert failure.hint is not None
    assert "src/Widget/" in failure.hint


def test_description_names_the_remedy_per_kind(tmp_path: Path) -> None:
    (tmp_path / "node_modules" / "@widget" / "lib").mkdir(parents=True)
    failures = [
        classify_unresolved_import(
            "node_modules/@vault/core/IVault.sol", ["@vault/=node_modules/@vault/core/"], tmp_path
        ),
        classify_unresolved_import(
            "node_modules/@widget/lib/IWidget.sol", ["@widget/=node_modules/@widget/lib/"], tmp_path
        ),
    ]

    described = describe_unresolved_imports(failures)

    assert "package_target_missing (1):" in described
    assert "file_missing_in_package (1):" in described
    assert "not found in any ancestor node_modules" in described
    assert "rebuilding the packages list cannot help" in described


def test_description_of_nothing_is_empty() -> None:
    assert describe_unresolved_imports([]) == ""
