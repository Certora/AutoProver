"""Tests for ``FS_FORBIDDEN_READ``, the pattern gating what the agent's source tools
(``list_files`` / ``get_file`` / ``grep_files``) are allowed to read.

Two properties matter, and they pull in opposite directions. Solidity reached through the
conf's ``packages`` remappings has to stay readable however deeply it is vendored, because
it is part of the verification target's source. Machine-generated output has to stay
unreadable, because a minified bundle carries megabytes on a single line and a content
grep reports whole matching lines — one call can then exceed the model's context window.

The pattern is consumed by graphcore's VFS via ``re.fullmatch``, so that is how it is
exercised here.
"""

import re

from composer.spec.util import FS_FORBIDDEN_READ

_FORBIDDEN = re.compile(FS_FORBIDDEN_READ)


def can_read(path: str) -> bool:
    """Mirror graphcore's check: a path is readable when it does not fullmatch."""
    return _FORBIDDEN.fullmatch(path) is None


def test_contract_under_analysis_is_readable() -> None:
    assert can_read("pkg/sub/src/Widget.sol")


def test_vendored_solidity_is_readable_at_any_depth() -> None:
    # A `packages` remapping resolves into a nested dependency tree; the Solidity there
    # is source under analysis, including the dependency's own lib/ and test/ subtrees.
    assert can_read("pkg/sub/node_modules/@vendor/artifacts/contracts/src/IThing.sol")
    assert can_read("pkg/sub/node_modules/@vendor/artifacts/contracts/lib/oz/contracts/token/ERC20.sol")
    assert can_read("pkg/sub/node_modules/@vendor/artifacts/contracts/test/Harness.sol")


def test_non_solidity_in_a_dependency_tree_is_not_readable_at_any_depth() -> None:
    assert not can_read("node_modules/pkg/index.js")
    # Nested dependency trees are the common case in a monorepo, and the reason this rule
    # cannot be bound to the project root.
    assert not can_read("pkg/sub/node_modules/pkg/index.js")
    assert not can_read("pkg/sub/node_modules/pkg/README.md")


def test_root_scaffolding_is_not_readable() -> None:
    assert not can_read("lib/forge-std/src/Test.sol")
    assert not can_read("test/Widget.t.sol")
    assert not can_read("emv-1-verified-Widget/Report.html")
    assert not can_read("package.json")


def test_internal_directories_are_not_readable_at_any_depth() -> None:
    assert not can_read(".git/config")
    assert not can_read(".certora_internal/autoProve/run.log")
    # Submodules and sub-project runs put both of these below the root.
    assert not can_read("pkg/sub/.git/config")
    assert not can_read("pkg/sub/.certora_internal/autoProve/run.log")


def test_generated_bundles_and_data_blobs_are_not_readable() -> None:
    # Built web output: the payload that made a single grep exceed the context window.
    assert not can_read("apps/dist/index.html")
    assert not can_read("apps/dist/assets/params.dat")
    # Bundles are named with any of the three separators in practice.
    assert not can_read("shared/app_bundle.js")
    assert not can_read("shared/app-bundle.js")
    assert not can_read("shared/app.bundle.js")
    assert not can_read("shared/vendor.min.js")
    assert not can_read("shared/app.js.map")


def test_ordinary_sources_are_not_caught_by_the_generated_output_rules() -> None:
    # The bundle/minified rules key on a separator before the marker, so hand-written
    # files whose names merely end in those words stay readable.
    assert can_read("apps/src/bundle.js")
    assert can_read("apps/src/min.js")
    assert can_read("services/daemon.mjs")
    assert can_read("README.md")
