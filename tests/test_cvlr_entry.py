"""The CVLR CLI entry point: what it decides before a run starts, and what it hands the backend.

Everything here is either a pure function or a wiring assertion — no services, no cargo, no models.
The wiring half exists because the three things this module gets wrong are all silent at runtime:
a harness written into the wrong crate, a source surface that admits ``target/``, and a build that
runs unconfined because the default leaked in from a library seam with different callers.
"""

import dataclasses
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from composer.cargo.metadata import CratePackage, LibTarget, Workspace
from composer.pipeline.ecosystem import SOLANA
from composer.spec.cvlr import entry, preflight
from composer.spec.cvlr.pipeline import BUILD_DIR, WORK_DIR, CvlrPhase


def _package(root: Path, name: str, *, lib: bool = True) -> CratePackage:
    package_root = root / "programs" / name
    return CratePackage(
        name=name,
        version="0.1.0",
        manifest_path=package_root / "Cargo.toml",
        lib=LibTarget(
            name=name, src_path=package_root / "src" / "lib.rs", crate_types=("cdylib",)
        ) if lib else None,
        features=(),
        source=None,
    )


def _workspace(root: Path, *members: CratePackage) -> Workspace:
    return Workspace(
        root=root, target_directory=root / "target", members=members, packages=members
    )


@pytest.fixture
def fake_cargo(monkeypatch):
    """``read_workspace`` replaced by a canned answer, as in ``test_cvlr_scaffold``."""
    def install(workspace: Workspace):
        async def fake(root, *, offline=False, features=(), timeout_s=0):
            return workspace
        monkeypatch.setattr(preflight, "read_workspace", fake)
    return install


# ---------------------------------------------------------------------------
# Which package a run verifies


@pytest.mark.asyncio
async def test_the_crate_owning_the_main_program_is_the_package(tmp_path, fake_cargo):
    """A multi-program workspace needs no second flag: cargo already knows which member owns the
    file the main program was named by. This is the case ``_pick_package`` refuses on its own, and
    the reason a real target (a lending protocol with five programs) does not need ``--package``."""
    lending, farms = _package(tmp_path, "lending"), _package(tmp_path, "farms")
    fake_cargo(_workspace(tmp_path, lending, farms))

    selected = await preflight.select_package(
        tmp_path, None, main_source=tmp_path / "programs" / "farms" / "src" / "lib.rs"
    )

    assert selected.name == "farms"
    assert selected.package_dir == Path("programs/farms")
    assert selected.workspace_root == tmp_path


@pytest.mark.asyncio
async def test_an_explicit_package_wins_over_the_owning_crate(tmp_path, fake_cargo):
    """``--package`` is for the case where the crate to build is not the one the main program's
    file sits in — a facade program whose logic lives in a sibling, say."""
    lending, farms = _package(tmp_path, "lending"), _package(tmp_path, "farms")
    fake_cargo(_workspace(tmp_path, lending, farms))

    selected = await preflight.select_package(
        tmp_path, "lending", main_source=tmp_path / "programs" / "farms" / "src" / "lib.rs"
    )

    assert selected.name == "lending"


@pytest.mark.asyncio
async def test_a_main_program_outside_every_member_still_refuses_to_guess(tmp_path, fake_cargo):
    """Nothing owns the path, so the fallback is the single-library rule — and with two candidates
    that is a refusal, not a pick. The refusal is the whole point: which program is under
    verification is a fact about the engagement."""
    fake_cargo(_workspace(tmp_path, _package(tmp_path, "lending"), _package(tmp_path, "farms")))

    with pytest.raises(preflight.PreflightFailed, match="name the one to verify"):
        await preflight.select_package(tmp_path, None, main_source=tmp_path / "docs" / "notes.md")


@pytest.mark.asyncio
async def test_a_binary_only_owner_is_not_a_verifiable_package(tmp_path, fake_cargo):
    """A member with no library target builds no loadable object, so owning the file does not make
    it the answer — the run falls back, and here that finds the one crate that does have one."""
    tool = _package(tmp_path, "xtask", lib=False)
    lending = _package(tmp_path, "lending")
    fake_cargo(_workspace(tmp_path, tool, lending))

    selected = await preflight.select_package(
        tmp_path, None, main_source=tmp_path / "programs" / "xtask" / "src" / "main.rs"
    )

    assert selected.name == "lending"


@pytest.mark.asyncio
async def test_a_package_that_is_not_a_member_fails_before_the_run(tmp_path, fake_cargo):
    fake_cargo(_workspace(tmp_path, _package(tmp_path, "lending")))

    with pytest.raises(preflight.PreflightFailed, match="is not a member"):
        await preflight.select_package(tmp_path, "typo")


# ---------------------------------------------------------------------------
# The main program's identifier


def test_a_cargo_package_name_is_refused_as_a_program_identifier():
    """The mistake this exists for, priced: ``spl-stake-pool`` is a legal cargo package and an
    impossible Rust identifier, and passing it cost 38 minutes and ~30 heavy-tier calls before
    anyone could tell. It does not fail as a bad argument — the analysis is *ordered* to declare a
    program by this name, cannot, and re-submits against the rejection until the budget stops it."""
    with pytest.raises(ValueError, match="not a Rust identifier") as e:
        entry.parse_main_program("program/src/lib.rs:spl-stake-pool")

    # The message carries the fix, because the person who hit this had no way to see the cause.
    assert "program/src/lib.rs:spl_stake_pool" in str(e.value)


def test_a_valid_identifier_is_returned_with_its_path():
    assert entry.parse_main_program("programs/vault/src/lib.rs:vault") == (
        "programs/vault/src/lib.rs", "vault"
    )
    # A path containing a colon is not a thing here, but the split has to be on the FIRST colon
    # either way: the identifier is the tail.
    assert entry.parse_main_program("a/b.rs:x_1") == ("a/b.rs", "x_1")


def test_a_main_program_without_an_identifier_is_a_usage_error():
    for bad in ("programs/vault/src/lib.rs", ":vault"):
        with pytest.raises(ValueError, match="expected <path>:<identifier>"):
            entry.parse_main_program(bad)


def test_an_identifier_with_no_repair_is_still_refused_plainly():
    # Nothing to suggest, so the message must not invent one.
    with pytest.raises(ValueError, match="not a Rust identifier") as e:
        entry.parse_main_program("a/b.rs:9lives")
    assert "Did you mean" not in str(e.value)


# ---------------------------------------------------------------------------
# Confinement


def test_builds_are_confined_unless_a_developer_says_otherwise(monkeypatch):
    """``docs/cvlr-backend-plan.md`` §3 item 3. ``SandboxConfig.from_env`` defaults to ``none``,
    which is right for a seam with many callers and wrong for the one that compiles somebody
    else's ``build.rs`` — so this entry point does not use it."""
    monkeypatch.delenv("COMPOSER_SANDBOX_PROVIDER", raising=False)

    sandbox = entry.build_confinement()

    assert sandbox.provider == "launcher"
    assert sandbox.enabled


def test_opting_out_is_explicit(monkeypatch):
    monkeypatch.setenv("COMPOSER_SANDBOX_PROVIDER", "none")

    assert not entry.build_confinement().enabled


def test_the_platform_tools_root_is_granted(monkeypatch):
    """``cargo build-sbf`` reads its toolchain from there. The recipe grants the two default
    locations already; what this adds is the one ``$CERTORA_PLATFORM_TOOLS_ROOT`` names."""
    from composer.cargo.sbf import PLATFORM_TOOLS_ROOT

    assert PLATFORM_TOOLS_ROOT in entry.build_confinement().extra_ro


# ---------------------------------------------------------------------------
# The parser


def test_the_parser_takes_a_program_the_way_the_pipeline_splits_it():
    args = entry.build_parser().parse_args(
        ["/proj", "programs/vault/src/lib.rs:vault", "design.md"]
    )

    # ``cli_pipeline`` splits ``main_contract`` on the first colon; the identifier half is what the
    # Solana analysis has to name, and the path half is what locates the crate.
    assert args.main_contract.split(":", 1) == ["programs/vault/src/lib.rs", "vault"]
    assert args.system_doc == "design.md"
    assert args.package is None


def test_the_corpus_defaults_to_the_cvlr_knowledge_base():
    """The expensive gate ran with no corpus on purpose — measuring the floor. A CLI run is not the
    floor, and an absent database degrades to no search tools rather than failing."""
    assert entry.build_parser().parse_args(["/proj", "src/lib.rs:p"]).rag_corpus == "cvlr_kb"


def test_the_run_can_be_bounded_by_what_it_takes_on():
    """``--budget`` bounds spend and curtails whatever is in flight when it runs out;
    ``--max-properties`` bounds the work before any of it is paid for. Easing into an unfamiliar
    program wants both."""
    args = entry.build_parser().parse_args(["/proj", "src/lib.rs:p", "--max-properties", "5"])

    assert args.max_properties == 5
    assert entry.build_parser().parse_args(["/proj", "src/lib.rs:p"]).max_properties is None


def test_the_design_doc_is_optional():
    assert entry.build_parser().parse_args(["/proj", "src/lib.rs:p"]).system_doc is None


# ---------------------------------------------------------------------------
# What the backend is handed


@dataclasses.dataclass
class _Wiring:
    """What the entry point passed to the staging seam and to the pipeline."""
    kwargs: dict
    backend: object = None
    ecosystem: object = None
    env: object = None


@pytest.fixture
def wiring(monkeypatch, tmp_path):
    """Run ``_entry_point``'s body with the services, tool builders and pipeline stubbed out.

    Only the decisions this module owns are left real: the package, the sandbox, the artifact
    store's directory, the source-exclusion rule, and the counterexample namespace.
    """
    recorded = _Wiring(kwargs={})

    @asynccontextmanager
    async def fake_cli_pipeline(**kwargs):
        recorded.kwargs = kwargs
        staged = SimpleNamespace(
            source=SimpleNamespace(
                project_root=str(tmp_path), forbidden_read=kwargs["forbidden_read"]
            ),
            llm_models=object(),
            embed_model="staged-embedder",
            conns=SimpleNamespace(indexed_store=object(), store=object()),
            root_key="rootkey",
        )

        async def cont(env, backend, ecosystem):
            recorded.env, recorded.backend, recorded.ecosystem = env, backend, ecosystem
            return "result"

        yield staged, cont

    monkeypatch.setattr(entry, "cli_pipeline", fake_cli_pipeline)
    monkeypatch.setattr(entry, "build_layered_source_tools", lambda backends, **kw: backends)
    monkeypatch.setattr(entry, "build_source_tools", lambda basic, *a, **kw: basic)
    monkeypatch.setattr(entry, "make_prover_options", lambda **kw: kw)
    monkeypatch.setattr(
        entry, "PureServiceHost",
        lambda **kw: SimpleNamespace(
            bind_source_tools=lambda tools: {**kw, "source_tools": tools}
        ),
    )
    monkeypatch.setattr(entry, "build_rag_tools", lambda tag, model: (f"tools:{tag}:{model}",))
    return recorded


async def _run(argv: list[str], monkeypatch, wiring: _Wiring) -> _Wiring:
    """Drive the executor from parsed args — the seam ``autoprove_executor`` exists for, so a test
    reaches the run without going through ``sys.argv``."""
    args = entry.build_parser().parse_args(argv)
    async with entry.cvlr_executor(args, SimpleNamespace()) as runner:  # type: ignore[arg-type]
        await runner(object())  # type: ignore[arg-type]
    return wiring


@pytest.fixture
def project(tmp_path, fake_cargo):
    fake_cargo(_workspace(tmp_path, _package(tmp_path, "vault")))
    return tmp_path


@pytest.mark.asyncio
async def test_the_harness_lands_in_the_crate_that_owns_the_program(
    project, monkeypatch, wiring
):
    """The artifact store writes into ``<package_dir>/src/specs``, and in a workspace that is not
    the project root. Getting it wrong writes a harness into a crate nothing builds."""
    await _run([str(project), "programs/vault/src/lib.rs:vault"], monkeypatch, wiring)

    backend = wiring.backend
    assert backend is not None
    assert backend.package == "vault"
    assert backend.artifact_store._package_dir == Path("programs/vault")


@pytest.mark.asyncio
async def test_the_main_program_is_located_relative_to_the_project(project, monkeypatch, wiring):
    """A relative ``main_program`` path is resolved against the project root rather than the
    process's working directory. Both readings agree when you run from inside the project; the
    difference is running the CLI from anywhere else, where resolving against the cwd produced a
    path outside the project — and, before ``cli_pipeline`` rejected it, an owning-crate lookup that
    silently found nothing and fell through to the single-library rule."""
    await _run([str(project), "programs/vault/src/lib.rs:vault"], monkeypatch, wiring)

    assert wiring.backend.package == "vault"  # type: ignore[union-attr]
    assert wiring.backend.artifact_store._package_dir == Path("programs/vault")  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_the_source_surface_is_cargos_not_soliditys(project, monkeypatch, wiring):
    """``cli_pipeline`` defaults to the Solidity exclusion rule, which admits ``target/`` — on a
    project that has been built once, the largest thing in the tree."""
    await _run([str(project), "programs/vault/src/lib.rs:vault"], monkeypatch, wiring)

    assert wiring.kwargs["forbidden_read"] == SOLANA.language.default_forbidden_read
    assert wiring.ecosystem is SOLANA


@pytest.mark.asyncio
async def test_the_author_reads_the_working_tree_above_the_project(project, monkeypatch, wiring):
    """First hit wins, so a file the run has munged reads as munged and everything else reads as
    the developer wrote it. Reading pristine source while the prover verifies the tree is what cost
    a run (``docs/the-tree-is-a-vfs.md`` §1)."""
    await _run([str(project), "programs/vault/src/lib.rs:vault"], monkeypatch, wiring)

    tree, checkout = wiring.env["source_tools"]  # type: ignore[index]
    assert tree.root == project / WORK_DIR / BUILD_DIR
    assert checkout.root == project
    # Re-enumerated per call: the tree appears mid-run, and a cached listing taken before the
    # formalizer made it would never show it.
    assert not tree.cache_listing and not checkout.cache_listing


def test_the_working_tree_itself_is_not_listed():
    """It is served as a *layer*; the path through it is withheld. Otherwise every source file
    appears twice — once pristine, once derived — and the derived spelling is the one that reads
    like an ordinary project file."""
    import re as _re

    surface = entry._source_surface(SOLANA.language.default_forbidden_read)
    assert _re.fullmatch(surface, ".cvlr_work/build/program/src/processor.rs")
    assert not _re.fullmatch(surface, "program/src/processor.rs")


@pytest.mark.asyncio
async def test_design_doc_discovery_has_a_phase_of_its_own(project, monkeypatch, wiring):
    await _run([str(project), "programs/vault/src/lib.rs:vault"], monkeypatch, wiring)

    assert wiring.kwargs["design_doc_phase"] is CvlrPhase.DISCOVER_DESIGN_DOC


@pytest.mark.asyncio
async def test_the_property_cap_reaches_the_pipeline(project, monkeypatch, wiring):
    await _run(
        [str(project), "programs/vault/src/lib.rs:vault", "--max-properties", "5"],
        monkeypatch, wiring,
    )

    assert wiring.kwargs["max_properties"] == 5


@pytest.mark.asyncio
async def test_counterexamples_are_namespaced_by_run(project, monkeypatch, wiring):
    """Rule names repeat across runs; a shared namespace would let one run's counterexamples be
    read as another's — and findings are synthesized from exactly that store."""
    await _run([str(project), "programs/vault/src/lib.rs:vault"], monkeypatch, wiring)

    kind, thread = wiring.backend.cex_analysis.namespace  # type: ignore[union-attr]
    assert kind == "cvlr_cex"
    assert thread == wiring.kwargs["thread_id"]


def test_a_threat_model_can_seed_property_extraction():
    """The front half is the ecosystem's and reads a threat model the same way the EVM pipeline
    does; only the flag to hand it one was missing."""
    args = entry.build_parser().parse_args(
        ["/proj", "src/lib.rs:p", "--threat-model", "threats.md"]
    )

    assert args.threat_model == "threats.md"
    assert entry.build_parser().parse_args(["/proj", "src/lib.rs:p"]).threat_model is None


@pytest.mark.asyncio
async def test_the_prover_is_cloud_only(project, monkeypatch, wiring):
    await _run([str(project), "programs/vault/src/lib.rs:vault"], monkeypatch, wiring)

    assert wiring.backend.prover_opts == {"cloud": True, "app": "solana"}  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_the_corpus_can_be_turned_off(project, monkeypatch, wiring):
    await _run(
        [str(project), "programs/vault/src/lib.rs:vault", "--rag-corpus", "none"],
        monkeypatch, wiring,
    )

    assert wiring.env["rag_tools"] == ()  # type: ignore[index]


@pytest.mark.asyncio
async def test_the_corpus_is_searched_by_default(project, monkeypatch, wiring):
    await _run([str(project), "programs/vault/src/lib.rs:vault"], monkeypatch, wiring)

    # The embedder the run already staged, not a second load of the same transformer.
    assert wiring.env["rag_tools"] == ("tools:cvlr_kb:staged-embedder",)  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_bad_package_fails_before_any_service_starts(project, monkeypatch, wiring):
    """The selection happens outside ``cli_pipeline``, so a usage error costs nothing — no
    Postgres, no models, and no partial analysis agent."""
    args = entry.build_parser().parse_args(
        [str(project), "programs/vault/src/lib.rs:vault", "--package", "typo"]
    )

    with pytest.raises(preflight.PreflightFailed):
        async with entry.cvlr_executor(args, SimpleNamespace()):  # type: ignore[arg-type]
            pass

    assert wiring.kwargs == {}
