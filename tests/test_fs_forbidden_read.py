"""Tests for ``fs_forbidden_read``, the predicate gating what the agent's source tools
(``list_files`` / ``get_file`` / ``grep_files``) are allowed to read.

Two properties matter, and they pull in opposite directions. Solidity always stays
readable, wherever it sits: the conf's ``packages`` remappings resolve into vendored
dependency trees, and a stock Foundry layout keeps real contracts in ``lib/`` and
``test/``. Machine-generated output stays unreadable, because a minified bundle carries
megabytes on a single line and a content grep reports whole matching lines — one call can
then exceed the model's context window.

graphcore hands the predicate a ``PurePosixPath`` of a project-root-relative path, so
that is how it is exercised here.
"""

from pathlib import PurePosixPath

from composer.spec.util import fs_forbidden_read


def can_read(path: str) -> bool:
    """Mirror graphcore's check: readable when the predicate does not exclude."""
    return not fs_forbidden_read(PurePosixPath(path))


def test_contract_under_analysis_is_readable() -> None:
    assert can_read("pkg/sub/src/Widget.sol")


def test_solidity_is_readable_wherever_it_sits() -> None:
    # A `packages` remapping resolves into a vendored tree, at whatever depth.
    assert can_read("pkg/sub/node_modules/@vendor/artifacts/contracts/src/IThing.sol")
    assert can_read("node_modules/@vendor/contracts/IThing.sol")
    # A stock Foundry layout keeps dependencies in lib/ and contracts in test/.
    assert can_read("lib/openzeppelin-contracts/contracts/token/ERC20.sol")
    assert can_read("test/Widget.t.sol")
    assert can_read("pkg/sub/lib/forge-std/src/Test.sol")
    assert can_read("pkg/sub/test/Harness.sol")
    # Even a name that collides with a generated-output rule stays readable.
    assert can_read("apps/dist/Widget.sol")


def test_non_solidity_in_a_dependency_tree_is_not_readable_at_any_depth() -> None:
    assert not can_read("node_modules/pkg/index.js")
    # Nested dependency trees are the common case in a monorepo, and the reason this rule
    # cannot be bound to the project root.
    assert not can_read("pkg/sub/node_modules/pkg/index.js")
    assert not can_read("pkg/sub/node_modules/pkg/README.md")
    assert not can_read("pkg/sub/lib/forge-std/README.md")
    assert not can_read("test/fixtures/expected.txt")


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
    assert not can_read("package.json")


def test_prover_working_directories_are_withheld_whole_including_solidity() -> None:
    # Their .sol is a verbatim copy of a contract already readable at its canonical
    # path — each certoraRun invocation leaves another one — so the Solidity carve-out
    # deliberately does not reach here.
    assert not can_read("emv-1-verified-Widget/inputs/.certora_sources/src/Widget.sol")
    assert not can_read(".certora_internal/abc123/.certora_sources/src/Widget.sol")
    assert not can_read("pkg/sub/.certora_internal/abc123/.certora_sources/src/Widget.sol")
    assert not can_read(".certora_internal/autoProve/run.log")
    assert not can_read(".git/config")
    assert not can_read("pkg/sub/.git/config")


def test_ordinary_sources_are_not_caught_by_the_generated_output_rules() -> None:
    # The bundle/minified rules key on a separator before the marker, so hand-written
    # files whose names merely end in those words stay readable.
    assert can_read("apps/src/bundle.js")
    assert can_read("apps/src/min.js")
    assert can_read("services/daemon.mjs")
    assert can_read("README.md")
