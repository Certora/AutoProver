"""Phase 2's knowledge pieces: the CVLR source mount, and what the prompts say about it.

``docs/cvlr-backend-plan.md`` §5.5 is the design and the risk table's "reading the wrong CVLR" row
is the reason: source that disagrees with what the build compiles is *confidently* wrong, and an
agent has no way to notice — ``RUST_FORBIDDEN_READ`` hides ``Cargo.lock``. So most of what is
checked here is about identity rather than retrieval: that a path carries its crate's version, that
a version-less answer is not reachable, and that the prompt saying "this source is authoritative"
appears exactly when the tools serving it are bound.

No toolchain, no network, no LLM: the crate trees are written into ``tmp_path``.
"""

from pathlib import Path

import pytest

from composer.cargo.metadata import CratePackage
from composer.pipeline.ecosystem import SOLANA, SOLANA_PROPERTY_SYSTEM_TEMPLATE, SOROBAN
from composer.spec.code_explorer import code_explorer_sys_prompt
from composer.spec.cvlr.crates import CvlrSources
from composer.spec.cvlr.guidance import SOLANA_CVLR_GUIDANCE
from composer.spec.cvlr.crate_mount import MAX_MATCHES, MountedCrates, mount
from composer.spec.cvlr.source_tools import cvlr_source_tools
from composer.spec.prop_inference import PropertySystemPromptParams
from composer.templates.loader import load_jinja_template


def _crate(root: Path, name: str, version: str, files: dict[str, str]) -> CratePackage:
    """Write a crate tree under ``root`` and return the package cargo would report for it."""
    crate_dir = root / f"{name}-{version}"
    for rel, body in files.items():
        target = crate_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return CratePackage(
        name=name,
        version=version,
        manifest_path=crate_dir / "Cargo.toml",
        lib_target=name.replace("-", "_"),
        features=(),
        source="registry+https://github.com/rust-lang/crates.io-index",
    )


@pytest.fixture
def family(tmp_path: Path) -> MountedCrates:
    """A miniature CVLR family: a facade that re-exports, and the crate that defines."""
    facade = _crate(
        tmp_path,
        "cvlr",
        "0.6.1",
        {
            "Cargo.toml": '[package]\nname = "cvlr"\n',
            "src/lib.rs": "pub use cvlr_asserts::cvlr_assert;\n",
            "Cargo.lock": "# packaging bookkeeping, not documentation\n",
        },
    )
    asserts = _crate(
        tmp_path,
        "cvlr-asserts",
        "0.6.1",
        {
            "Cargo.toml": '[package]\nname = "cvlr-asserts"\n',
            "src/core.rs": "#[macro_export]\nmacro_rules! cvlr_assert {\n    ($c:expr) => {};\n}\n",
        },
    )
    mounted = mount(CvlrSources((facade, asserts)))
    assert mounted is not None
    return mounted


# --------------------------------------------------------------------------------------------
# the mount
# --------------------------------------------------------------------------------------------


def test_every_path_carries_the_version_it_came_from(family: MountedCrates):
    """The one defence against a confidently-wrong answer: an agent cannot read a CVLR file without
    seeing which CVLR it is, and cannot report a finding whose version is unrecoverable."""
    assert set(family.paths()) == {
        "cvlr-0.6.1/Cargo.toml",
        "cvlr-0.6.1/src/lib.rs",
        "cvlr-asserts-0.6.1/Cargo.toml",
        "cvlr-asserts-0.6.1/src/core.rs",
    }


def test_packaging_bookkeeping_is_not_listed(family: MountedCrates):
    """A published crate carries a lockfile and cargo's own markers; none of them answers a question
    about what a macro does, and every one of them costs the agent a line of listing."""
    assert not [p for p in family.paths() if p.endswith("Cargo.lock")]
    assert family.read("cvlr-0.6.1/Cargo.lock") is None


def test_the_definition_is_found_in_the_sibling_not_the_facade(family: MountedCrates):
    """``cvlr`` re-exports; an agent handed only the crate the manifest names would find the
    re-export and stop."""
    hits, capped = family.search("macro_rules! cvlr_assert")
    assert not capped
    assert [h.split(":")[0] for h in hits] == ["cvlr-asserts-0.6.1/src/core.rs"]


def test_a_search_reports_when_it_stopped_early(tmp_path: Path):
    """A silent truncation reads as "that is all there is", which is the one answer a search must
    never give by accident."""
    noisy = _crate(
        tmp_path, "cvlr", "0.6.1", {"src/lib.rs": "\n".join(["fn nondet() {}"] * (MAX_MATCHES + 10))}
    )
    mounted = mount(CvlrSources((noisy,)))
    assert mounted is not None
    hits, capped = mounted.search("nondet")
    assert capped and len(hits) == MAX_MATCHES


def test_a_path_cannot_escape_the_crate_it_names(family: MountedCrates):
    """The tree is trusted; the path is not — it comes from an agent, and the registry cache has
    every other crate on the machine sitting beside it."""
    assert family.read("cvlr-0.6.1/../../etc/passwd") is None
    assert family.read("cvlr-0.6.1/src/../../cvlr-asserts-0.6.1/src/core.rs") is None


def test_an_unknown_crate_prefix_reads_as_nothing(family: MountedCrates):
    assert family.read("cvlr-9.9.9/src/lib.rs") is None
    assert family.read("src/lib.rs") is None


def test_the_statement_names_every_resolved_version(family: MountedCrates):
    statement = family.statement()
    assert "cvlr 0.6.1" in statement and "cvlr-asserts 0.6.1" in statement
    assert "authoritative" in statement


def test_the_tools_are_named_for_the_tree_they_read(family: MountedCrates):
    """A second ``get_file`` would collide with the project's, and an agent would have no way to say
    which tree it meant."""
    assert [t.name for t in cvlr_source_tools(family)] == [
        "cvlr_source_files",
        "cvlr_source_read",
        "cvlr_source_search",
    ]


def test_a_target_with_no_cvlr_mounts_nothing(tmp_path: Path):
    """``None``, not an empty mount: with nothing to read there are no tools to bind and nothing to
    claim, where an empty mount would advertise a source of truth that answers everything with
    "not found"."""
    assert mount(CvlrSources(())) is None


def test_a_crate_whose_sources_were_pruned_is_dropped_rather_than_faked(tmp_path: Path):
    """A resolved version whose tree is gone is a pruned cache, not a missing dependency. Mount what
    is there; the warning names the rest."""
    present = _crate(tmp_path, "cvlr", "0.6.1", {"src/lib.rs": "pub fn x() {}\n"})
    absent = CratePackage(
        name="cvlr-solana",
        version="0.5.0",
        manifest_path=tmp_path / "gone-0.5.0" / "Cargo.toml",
        lib_target="cvlr_solana",
        features=(),
        source="registry+x",
    )
    mounted = mount(CvlrSources((present, absent)))
    assert mounted is not None
    assert [c.name for c in mounted.crates] == ["cvlr"]


@pytest.mark.asyncio
async def test_a_name_the_build_does_not_have_is_reported_as_do_not_use(family: MountedCrates):
    """The failure this whole mount exists to prevent: an agent reaching for a helper that belongs
    to a different CVLR line. Silence would read as "search is unreliable"; the answer has to say
    what the absence means."""
    search = {t.name: t for t in cvlr_source_tools(family)}["cvlr_source_search"]
    answer = await search.ainvoke({"name": "acc_infos_with_mem_layout"})
    assert "different CVLR version" in answer and "do not use it" in answer


# --------------------------------------------------------------------------------------------
# what the prompts say
# --------------------------------------------------------------------------------------------


def _explorer_prompt(crate_source: str | None) -> str:
    return code_explorer_sys_prompt(
        SOLANA.code_explorer_prompt, "established", crate_source
    )(load_jinja_template)


def test_the_explorer_is_told_the_crate_source_exists_when_it_is_mounted(family: MountedCrates):
    prompt = _explorer_prompt(family.statement())
    assert "cvlr_source_" in prompt
    assert "cvlr 0.6.1" in prompt


def test_the_explorer_is_told_which_tree_is_which(family: MountedCrates):
    """Two read-only trees in one agent is the whole hazard of mounting a second one."""
    prompt = _explorer_prompt(family.statement())
    assert "Do not confuse the two trees." in prompt


def test_an_unmounted_run_advertises_no_crate_source():
    """A prompt naming tools the agent does not have does not degrade to silence — it invites
    fabricated reads."""
    assert "cvlr_source_" not in _explorer_prompt(None)


def test_soroban_shares_the_rust_fragment_and_therefore_the_addendum(family: MountedCrates):
    """The mount is chain-neutral by construction: it is a cargo dependency either way, so the
    instruction lives in the shared Rust fragment rather than in Solana's own prompt."""
    prompt = code_explorer_sys_prompt(
        SOROBAN.code_explorer_prompt, "none", family.statement()
    )(load_jinja_template)
    assert "cvlr_source_" in prompt


# --------------------------------------------------------------------------------------------
# backend guidance
# --------------------------------------------------------------------------------------------


def _property_system_prompt() -> str:
    params: PropertySystemPromptParams = {
        "sort": "existing",
        "backend_guidance": SOLANA_CVLR_GUIDANCE,
    }
    return SOLANA_PROPERTY_SYSTEM_TEMPLATE.bind(params).render_to(load_jinja_template)


def test_the_guidance_reaches_the_property_extractors_system_prompt():
    assert "Certora Solana Prover" in _property_system_prompt()
    assert "cannot be made to panic" in _property_system_prompt()


def test_the_guidance_does_not_repeat_the_evm_exclusions_that_invert_here():
    """Two of ``CERTORA_BACKEND_GUIDANCE``'s exclusions are wrong on this chain, and getting them
    wrong is expensive in opposite directions — one would drop the most frequently violated property
    class there is, the other would suppress exactly the properties that are cheapest to check."""
    assert "does not check arithmetic" in SOLANA_CVLR_GUIDANCE
    assert "cheaper, not dearer" in SOLANA_CVLR_GUIDANCE


def test_the_guidance_scopes_verification_below_the_dispatcher():
    """The manual's Methodology §4: a rule that starts at ``process_instruction`` is the single most
    reliable way to produce one that times out."""
    assert "process_instruction" in SOLANA_CVLR_GUIDANCE


def test_no_soroban_guidance_has_been_guessed():
    """§4.4 ship order. A second constant written before Solana verifies a real property would be a
    guess dressed as a deliverable — and the place it would be imported from is here."""
    import composer.spec.cvlr.guidance as guidance

    assert not [name for name in vars(guidance) if "SOROBAN" in name]
