"""``validate_solidity_connectivity`` over the source paths the analysis agent authors.

The component-analysis agent writes each contract's ``path`` field by hand, and everything
downstream — the harness builder, the summarizer, the verification config — treats it as a real
file. The validator is the one place that resolves those strings against the project tree, and it
runs inside the agent's own retry loop, so a wrong path comes back as feedback the agent can act
on rather than as a failure hours later.

The tree these tests build puts the build project one directory below the project root, which is
the layout that makes a path easy to get wrong: the agent sees the build's own source directory
and can report a path missing the leading directory that the file tools show.
"""

import os
from pathlib import Path
from typing import Any

import pytest

from composer.spec.system_analysis import (
    ANALYSIS_INITIAL_TEMPLATE,
    ANALYSIS_SYSTEM_TEMPLATE,
    run_component_analysis,
    validate_solidity_connectivity,
)
from composer.spec.system_model import (
    Application,
    FromSourceApplication,
    SourceApplication,
)
from composer.spec.types import SourceIdentifier

MAIN_ID = SourceIdentifier("Vault")

#: Where the build project sits inside the project root — one directory down, so a path that
#: drops its leading component still names something plausible.
BUILD_DIR = "pkg"
VAULT_PATH = f"{BUILD_DIR}/core/src/Vault.sol"
LEDGER_PATH = f"{BUILD_DIR}/core/src/ledger/Ledger.sol"
ORACLE_PATH = f"{BUILD_DIR}/core/src/interfaces/IOracle.sol"
#: The same filename elsewhere in the tree, so a relocation hint has to narrow by path tail and
#: not merely by basename.
DECOY_LEDGER_PATH = f"{BUILD_DIR}/legacy/Ledger.sol"
#: A third copy, under a directory the agent's file tools withhold whole. Real trees carry these:
#: every prover run leaves its own copy of the sources under ``.certora_internal``.
WITHHELD_LEDGER_PATH = ".certora_internal/latest/run/Ledger.sol"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project root whose Foundry project is one directory down. Returns the root — the
    directory the agent's file tools list from, and the frame every declared path is read in."""
    root = tmp_path / "repo"
    for rel in (VAULT_PATH, LEDGER_PATH, ORACLE_PATH, DECOY_LEDGER_PATH, WITHHELD_LEDGER_PATH):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// solidity\n")
    return root


def _component(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"What {name} does.",
        "external_entry_points": [],
        "state_variables": [],
        "interactions": [],
        "requirements": [],
    }


def _contract(name: str, path: str) -> dict[str, Any]:
    return {
        "sort": "singleton",
        "name": name,
        "solidity_identifier": name,
        "description": f"The {name} contract.",
        "path": path,
        "components": [_component(f"{name} Core")],
    }


def _raw() -> dict[str, Any]:
    """A ``SourceApplication`` payload whose every path is correct, mutated per case."""
    return {
        "application_type": "Vault",
        "description": "A vault with a ledger, priced by an oracle.",
        "components": [
            _contract("Vault", VAULT_PATH),
            _contract("Ledger", LEDGER_PATH),
            {
                "name": "Oracle",
                "description": "A price feed owned by someone else.",
                "assumptions": ["Reports a fresh price."],
                "path": ORACLE_PATH,
            },
        ],
    }


def _app(raw: dict[str, Any]) -> SourceApplication:
    return SourceApplication.model_validate(raw)


def _contract_named(raw: dict[str, Any], name: str) -> dict[str, Any]:
    return next(c for c in raw["components"] if c["name"] == name)


def test_declared_paths_that_exist_are_accepted(project: Path) -> None:
    assert validate_solidity_connectivity(_app(_raw()), MAIN_ID, project) is None


def test_a_path_missing_its_leading_directory_is_rejected(project: Path) -> None:
    # The dropped-leading-directory case: a path written as the build project sees it, missing
    # the directory the build project itself lives in.
    raw = _raw()
    dropped = LEDGER_PATH.split("/", 1)[1]
    _contract_named(raw, "Ledger")["path"] = dropped

    result = validate_solidity_connectivity(_app(raw), MAIN_ID, project)

    assert result is not None
    assert "Contract Ledger declares path" in result
    assert repr(dropped) in result
    # Narrowed by path tail, so the decoy of the same name is not offered.
    assert LEDGER_PATH in result
    assert DECOY_LEDGER_PATH not in result
    assert "relative to the project root" in result


def test_several_files_share_the_name(project: Path) -> None:
    # A bare filename tail-matches both copies, so neither can be singled out.
    raw = _raw()
    _contract_named(raw, "Ledger")["path"] = "Ledger.sol"

    result = validate_solidity_connectivity(_app(raw), MAIN_ID, project)

    assert result is not None
    assert LEDGER_PATH in result
    assert DECOY_LEDGER_PATH in result
    # The copy under the withheld directory is not one the agent could open, so offering it would
    # send the retry at a path the next validation rejects again.
    assert WITHHELD_LEDGER_PATH not in result


def test_a_declared_path_under_a_withheld_directory_is_rejected(project: Path) -> None:
    # The file is there, but the agent's file tools do not hand it back — the same surface the
    # relocation hints are drawn from, so accepting it would be the validator disagreeing with
    # itself about what counts as a source file.
    raw = _raw()
    _contract_named(raw, "Ledger")["path"] = WITHHELD_LEDGER_PATH

    result = validate_solidity_connectivity(_app(raw), MAIN_ID, project)

    assert result is not None
    assert "Contract Ledger declares path" in result
    assert "not readable through your file tools" in result


def test_no_file_of_that_name_anywhere(project: Path) -> None:
    raw = _raw()
    _contract_named(raw, "Ledger")["path"] = f"{BUILD_DIR}/core/src/Absent.sol"

    result = validate_solidity_connectivity(_app(raw), MAIN_ID, project)

    assert result is not None
    assert "No file named 'Absent.sol' is readable anywhere in the project" in result
    assert "grep_files" in result


def test_a_directory_is_reported_as_a_directory(project: Path) -> None:
    raw = _raw()
    _contract_named(raw, "Ledger")["path"] = f"{BUILD_DIR}/core/src/ledger"

    result = validate_solidity_connectivity(_app(raw), MAIN_ID, project)

    assert result is not None
    assert "is a directory, not a Solidity file" in result
    assert "does not exist" not in result


@pytest.mark.parametrize("escaping", ["/etc/hosts", "../outside.sol"])
def test_absolute_and_escaping_paths_are_rejected(project: Path, escaping: str) -> None:
    # Containment is decided lexically, so it holds even when the escaping path names a file that
    # really is there.
    (project.parent / "outside.sol").write_text("// solidity\n")
    raw = _raw()
    _contract_named(raw, "Ledger")["path"] = escaping

    result = validate_solidity_connectivity(_app(raw), MAIN_ID, project)

    assert result is not None
    assert "does not name a file under the project root" in result


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs elevation on Windows")
def test_a_symlinked_dependency_is_accepted(project: Path) -> None:
    # Vendored dependency trees are routinely symlinked into a project, and their Solidity is
    # readable through the agent's file tools, so a path leading through one is a legitimate
    # answer. Resolving the path before the containment check would reject it with nothing the
    # agent could say instead.
    external = project.parent / "external"
    (external / "src").mkdir(parents=True)
    (external / "src" / "Dep.sol").write_text("// solidity\n")
    lib = project / BUILD_DIR / "core" / "lib"
    lib.mkdir(parents=True)
    (lib / "dep").symlink_to(external, target_is_directory=True)

    raw = _raw()
    _contract_named(raw, "Ledger")["path"] = f"{BUILD_DIR}/core/lib/dep/src/Dep.sol"

    assert validate_solidity_connectivity(_app(raw), MAIN_ID, project) is None


def test_every_bad_path_is_reported_in_one_message(project: Path) -> None:
    raw = _raw()
    _contract_named(raw, "Vault")["path"] = "core/src/Vault.sol"
    _contract_named(raw, "Ledger")["path"] = "core/src/ledger/Ledger.sol"

    result = validate_solidity_connectivity(_app(raw), MAIN_ID, project)

    assert result is not None
    assert result.startswith("Multiple validation errors found; fix all of them before resubmitting:")
    assert result.count("declares path") == 2
    assert "For reference, the names you declared in your submission:" in result
    # The frame paragraph hangs off the reference block, so it is stated once however many paths
    # are wrong.
    assert result.count("list_files") == 1


def test_graph_errors_and_path_errors_arrive_together(project: Path) -> None:
    # One round trip fixes both kinds of mistake; sequencing the two checks would cost two.
    raw = _raw()
    raw["components"].append(_contract("Ledger", LEDGER_PATH))
    _contract_named(raw, "Vault")["path"] = "core/src/Vault.sol"

    result = validate_solidity_connectivity(_app(raw), MAIN_ID, project)

    assert result is not None
    assert "Duplicate contract names: Ledger" in result
    assert "Contract Vault declares path" in result


def test_an_external_actor_without_a_path_is_accepted(project: Path) -> None:
    raw = _raw()
    next(c for c in raw["components"] if c["name"] == "Oracle")["path"] = None

    assert validate_solidity_connectivity(_app(raw), MAIN_ID, project) is None


def test_an_external_actor_with_a_missing_path_is_rejected(project: Path) -> None:
    raw = _raw()
    next(c for c in raw["components"] if c["name"] == "Oracle")["path"] = "core/src/interfaces/IOracle.sol"

    result = validate_solidity_connectivity(_app(raw), MAIN_ID, project)

    assert result is not None
    assert "External actor Oracle declares path" in result
    # The actor's path is optional, so dropping it is a way out the contract case does not have.
    assert "omit the path instead" in result


def test_no_project_root_means_no_path_check(project: Path) -> None:
    raw = _raw()
    _contract_named(raw, "Ledger")["path"] = "nowhere/at/all/Ledger.sol"

    assert validate_solidity_connectivity(_app(raw), MAIN_ID, None) is None


def test_greenfield_application_is_unaffected(project: Path) -> None:
    # A greenfield ``Application`` declares no paths at all; handing it a real root changes nothing.
    raw = _raw()
    for component in raw["components"]:
        component.pop("path", None)

    assert validate_solidity_connectivity(Application.model_validate(raw), MAIN_ID, project) is None


def test_from_source_paths_are_checked_and_new_contracts_are_not(project: Path) -> None:
    raw = _raw()
    _contract_named(raw, "Vault")["tag"] = "edited"
    ledger = _contract_named(raw, "Ledger")
    ledger["tag"] = "unchanged"
    ledger["path"] = "core/src/ledger/Ledger.sol"
    fresh = _contract("Router", "")
    del fresh["path"]
    fresh["tag"] = "new"
    raw["components"].append(fresh)

    result = validate_solidity_connectivity(FromSourceApplication.model_validate(raw), MAIN_ID, project)

    assert result is not None
    assert "Contract Ledger declares path" in result
    assert "Vault" not in result.split("For reference")[0]
    assert "Router" not in result.split("For reference")[0]


# --- the cache, the other way into an analyzed model ---------------------------------


class _StopAtGeneration(Exception):
    """Raised where ``run_component_analysis`` begins assembling the agent — as far past a cache
    miss as these tests need to go, and with no LLM, store, or template rendering behind it."""


class _Ctx:
    """Enough ``WorkflowContext`` for the cache branch: it always hits, with the model it was
    built on. ``get_memory_tool`` is the first call past the cache exit, so raising there is how
    a fall-through to generation is observed."""

    recursion_limit = 10

    def __init__(self, cached: SourceApplication) -> None:
        self.cached = cached

    async def cache_get(self, _ty: type) -> SourceApplication:
        return self.cached

    def get_memory_tool(self) -> Any:
        raise _StopAtGeneration()


class _Env:
    """A ``ServiceHost`` stub: the workflow sort is all the cache branch reads off it."""

    sort = "existing"


async def _analyze(ctx: _Ctx, project: Path) -> SourceApplication | None:
    return await run_component_analysis(
        SourceApplication,
        ctx,  # type: ignore[arg-type]
        None,
        _Env(),  # type: ignore[arg-type]
        [],
        MAIN_ID,
        project_root=project,
        system_template=ANALYSIS_SYSTEM_TEMPLATE,
        initial_template=ANALYSIS_INITIAL_TEMPLATE,
        validate=validate_solidity_connectivity,
    )


@pytest.mark.asyncio
async def test_a_cached_analysis_that_validates_is_returned(project: Path) -> None:
    ctx = _Ctx(_app(_raw()))

    assert await _analyze(ctx, project) is ctx.cached


@pytest.mark.asyncio
async def test_a_cached_analysis_with_a_bad_path_is_re_derived(project: Path) -> None:
    # The cache key covers the project and the contract, not the source tree or the checks in
    # force, so an entry stands for as long as the repo does. Replaying one the validator would
    # reject puts a bad path downstream on every rerun, which is the one place the agent's own
    # retry loop cannot reach.
    raw = _raw()
    _contract_named(raw, "Ledger")["path"] = "core/src/ledger/Ledger.sol"
    ctx = _Ctx(_app(raw))

    with pytest.raises(_StopAtGeneration):
        await _analyze(ctx, project)
