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

from composer.cargo.metadata import CratePackage, LibTarget
from composer.pipeline.ecosystem import SOLANA, SOLANA_PROPERTY_SYSTEM_TEMPLATE, SOROBAN
from composer.spec.code_explorer import code_explorer_sys_prompt
from composer.spec.cvlr.crates import CvlrSources
from composer.spec.cvlr.guidance import SOLANA_CVLR_GUIDANCE
from composer.spec.cvlr.crate_mount import MAX_MATCHES, MountedCrates, mount
from composer.spec.cvlr.source_tools import cvlr_source_tools
from composer.spec.cvlr.author import CvlrMountParams
from composer.spec.prop_inference import PropertySystemPromptParams
from composer.spec.types import PropertyFormulation
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
        lib=LibTarget(
            name=name.replace("-", "_"),
            src_path=crate_dir / "src" / "lib.rs",
            crate_types=("lib",),
        ),
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
        lib=LibTarget(
            name="cvlr_solana",
            src_path=tmp_path / "gone-0.5.0" / "src" / "lib.rs",
            crate_types=("lib",),
        ),
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


def test_the_guidance_does_not_repeat_the_evm_exclusion_that_inverts_here():
    """``CERTORA_BACKEND_GUIDANCE`` suppresses properties spanning many functions because they are
    expensive on EVM. Here ``cvlr_rules!`` fans one property across a grid of handlers for a line, so
    repeating that exclusion would drop exactly the properties that are cheapest to check."""
    assert "cheaper, not dearer" in SOLANA_CVLR_GUIDANCE


def test_panic_freedom_is_excluded_rather_than_advertised():
    """An earlier version called "cannot be made to panic" real, checkable and frequently violated.
    It is none of the three here.

    A panic aborts the instruction and the runtime rolls back every account mutation, exactly as a
    returned error does — so a panicking path cannot leave state that violates an invariant, and the
    analysis prunes those paths to agree with the runtime. A reachable panic is an availability
    concern. And the flag that would report panics as violations is per-conf while one conf covers a
    whole component, so it cannot be turned on for one property without corrupting its neighbours'
    verdicts.

    Measured cost of getting this wrong: a gate run extracted two panic-freedom properties, wrote
    them up as real attacks the prover was hiding, and skipped both — two of that unit's eight skips
    spent on a property class that has no rule.

    Arithmetic that *wraps* is a different matter and stays in the checkable list: the wrong value
    survives the call, so a rule catches it.
    """
    assert "Panic freedom" in SOLANA_CVLR_GUIDANCE
    excluded = _flat(SOLANA_CVLR_GUIDANCE.split("should not be extracted as a property")[1])
    assert "Panic freedom" in excluded
    assert "rolls back every account mutation" in excluded
    assert "is a different matter and belongs above" in excluded


def test_the_guidance_scopes_verification_below_the_dispatcher():
    """The manual's Methodology §4: a rule that starts at ``process_instruction`` is the single most
    reliable way to produce one that times out."""
    assert "process_instruction" in SOLANA_CVLR_GUIDANCE


def test_no_soroban_guidance_has_been_guessed():
    """§4.4 ship order. A second constant written before Solana verifies a real property would be a
    guess dressed as a deliverable — and the place it would be imported from is here."""
    import composer.spec.cvlr.guidance as guidance

    assert not [name for name in vars(guidance) if "SOROBAN" in name]


# --------------------------------------------------------------------------------------------
# Anchor harness idioms in the author's system prompt
# --------------------------------------------------------------------------------------------


def _author_system_prompt() -> str:
    """The author's system prompt, rendered.

    Separate from :func:`_property_system_prompt`: that one is the *extraction* agent's, which
    decides which properties to state, and this one is the agent that writes the Rust.
    """
    from composer.spec.cvlr.author import (
        CvlrAuthorSystemParams,
        _PropertyGenSysTemplate,
    )

    params: CvlrAuthorSystemParams = {"cvlr_versions": "cvlr 0.6.1", "module": "spec"}
    return _PropertyGenSysTemplate.bind(params).render_to(load_jinja_template)


def test_the_author_is_told_to_call_the_handler_not_anchors_dispatch():
    """A rule starting at ``entry`` pays for Anchor's whole dispatch — discriminator matching,
    account validation, serialization — to reach a handler it could have called directly, and no
    property in a batch is about any of that.

    The prover also cannot analyze ``entry`` on a program with an ``#[error_code]`` enum
    (``docs/upstream-defects.md`` P4), but that is deliberately not the reason given: see
    :func:`test_avoiding_the_dispatcher_is_justified_by_cost_not_by_pointer_analysis`."""
    prompt = _author_system_prompt()
    assert "Call the handler, not the dispatcher" in prompt
    assert "crate::entry" in prompt


def test_the_worked_example_unwraps_the_handler_rather_than_branching_on_it():
    """The example in this prompt taught ``if handler(..).is_ok() { assert }``, and every authoring
    run copied it — which is how a whole phase came to be reported as blocked on Anchor.

    ``.unwrap()`` makes the failure path a panic, which the prover prunes because ``assert_on_panic``
    defaults to false, so the error value is never built. ``.is_ok()`` keeps that path live and merges
    it back, forcing the analysis through the ``#[error_code]`` enum's generated ``Display`` and its
    ``String`` allocation, which comes back as ``[3308]`` and no verdict at all.

    Measured as two rules in one job, same program and same property, differing in nothing else: the
    ``.unwrap()`` rule VERIFIED with its vacuity check passing, the ``.is_ok()`` rule ERRORed
    (``docs/upstream-defects.md`` P4, the correction). The two forms check the assertion over the same
    states, so this costs the rule nothing.

    Pinned as an example-shape test rather than a prose test because the prose was never the problem.
    """
    prompt = _author_system_prompt()
    assert ".unwrap();" in prompt
    assert "if crate::vault_program::deposit(ctx, amount).is_ok()" not in _flat(prompt)
    assert _flat("Consume the handler's `Result` with `.unwrap()`, not by branching on it") in _flat(
        prompt
    )


def test_the_author_is_warned_that_re_reading_an_account_yields_pre_state():
    """The idiom that silently produces a *wrong answer* rather than an error, so it is the one worth
    guarding. An Anchor ``Account<T>`` is a deserialized copy written back only in ``exit``, which
    nothing calls when a rule invokes a handler directly — so a rule that re-deserializes to observe
    post-state fails against a correct program, and the failure looks like a finding.

    Measured both ways on a real Anchor handler: re-reading gave a counterexample, and reading
    through the borrowed struct verified the same property (``docs/cvlr-backend-plan.md`` §7.6.2).
    """
    prompt = _author_system_prompt()
    assert "never by deserializing the account" in prompt
    assert "pre-state" in prompt
    assert "looks exactly like a real finding" in prompt


def test_the_author_is_told_to_heap_the_account_array_without_pinning_its_lifetime():
    """Two halves, and the second was found by the first draft of the prompt's own example failing
    to compile: heap the array, and do not annotate its lifetime. A wrapper returning
    ``[AccountInfo<'static>; 16]`` forces the borrow to be ``'static`` too and the accounts struct
    then will not build."""
    prompt = _author_system_prompt()
    assert "Box::new(cvlr_deserialize_nondet_accounts())" in prompt
    assert "do not annotate that array's lifetime" in prompt


def test_the_author_gets_a_worked_anchor_rule_rather_than_only_rules():
    """Four idioms stated as prose is a checklist somebody can satisfy individually and still get
    wrong together. The example is what shows them composing — construction, the pre-read, the
    borrow, and the post-read through the same struct.

    **The example compiles.** Verified 2026-09-01 against ``test_scenarios/solana_vault_idl``
    scaffolded with the Anchor fork; to re-verify, paste it into that project's
    ``src/certora/specs/mod.rs`` inside a ``#[rule] pub fn`` with
    ``use anchor_lang::prelude::*; use cvlr::prelude::*;`` and
    ``use cvlr_solana::cvlr_deserialize_nondet_accounts;``, then ``cargo check --features certora``.
    A worked example that does not compile is worse than none, and the first draft of this one did
    not — see the lifetime caution it now carries."""
    prompt = _author_system_prompt()
    assert "Context::new(&crate::ID" in prompt
    # the pre-read and the post-read must both go through the same binding
    assert "let before = ix.vault.balance;" in prompt
    assert "cvlr_assert!(ix.vault.balance == before + amount);" in prompt


def test_the_author_is_told_a_cpi_is_a_stand_in_and_that_this_is_inherited():
    """Flagged as inherited rather than measured, because it is: nothing here has tested a property
    about a CPI's effect. Saying which claims are measured is what keeps the rest trustworthy."""
    prompt = _author_system_prompt()
    assert "not measured here" in prompt
    assert "unconstrained stand-in" in prompt


# --------------------------------------------------------------------------------------------
# What the rule is allowed to be about
# --------------------------------------------------------------------------------------------
#
# The authoring loop's first real run delivered three units, and two of them formalized every
# property against a hand-written mirror of the handler rather than the handler: a `fn
# deposit_balance_update` and a `fn withdraw_logic`, each with a doc comment explaining that
# `[3308]` made the real one unanalyzable, each transcribing the handler's arithmetic by hand. Both
# passed the prover, both passed the judge, both published. Nothing downstream could tell.
#
# The author did not go off-script. Four things in these prompts left the door open, and these are
# the tests for having closed them.


def _flat(text: str) -> str:
    """Prompt text with line wrapping collapsed.

    These templates are hand-wrapped at 100 columns, so a phrase worth pinning routinely straddles a
    newline and a literal substring check fails for a reason that has nothing to do with the prompt.
    """
    return " ".join(text.split())


def _author_task_prompt() -> str:
    from composer.spec.cvlr.author import CvlrPropertyGenParams, _PropertyGenTemplate

    params: CvlrPropertyGenParams = {
        "context": None,
        "properties": [
            PropertyFormulation(title="p", sort="invariant", description="the balance rises")
        ],
        "program": "vault",
        "module": "spec",
        "cvlr_versions": "cvlr 0.6.1",
        "sort": "existing",
    }
    return _PropertyGenTemplate.bind(params).render_to(load_jinja_template)


def _judge_system_prompt() -> str:
    from composer.spec.cvlr.author import _JudgeSystemTemplate

    params: CvlrMountParams = {"cvlr_versions": "cvlr 0.6.1"}
    return _JudgeSystemTemplate.bind(params).render_to(load_jinja_template)


def test_the_author_is_told_the_subject_of_a_rule_is_the_programs_code():
    """The system prompt used to define a rule body as "call the code under test" and never say
    whose code that was. The properties are stated behaviourally, so a faithful mirror satisfies
    them on their own terms."""
    prompt = _flat(_author_system_prompt())
    assert "The code under test is the program's, always" in prompt
    assert "A rule that drives a function *you* wrote proves a property of your function" in prompt


def test_descending_below_the_handler_is_allowed_only_into_the_program_s_own_code():
    """This replaces a flat "the handler is the floor" rule, which over-shot.

    That rule was written to stop rules driving a harness-authored *copy* of a handler, and it did —
    but it also forbade the move every shipped verification project makes when a CPI is in the way:
    drive the program's own accounting core with nondet domain structs. The prover's CPI stand-in
    havocs the caller's deserialized ``Account<T>`` (``docs/upstream-defects.md`` P6), so a post-state
    property cannot be carried at handler level at all, and forbidding the descent leaves the author
    with only bad options.

    What matters is authorship, not depth: ``crate::<module>::<fn>`` narrows scope honestly, a ``fn``
    in the spec module verifies the author. So the prompt must keep prefering the handler, permit the
    descent into program code, and require it be declared and its cost stated.
    """
    prompt = _flat(_author_system_prompt())
    assert "Start at the handler, and descend only for a reason you can name" in prompt
    assert "The function must be the program's" in prompt
    assert "writing your own version of that function in this module is not descending" in prompt
    # the descent has to reach the report, or it is a silent narrowing
    assert "Declare it" in prompt
    assert "Say what it costs, in the commentary" in prompt


def test_avoiding_the_dispatcher_is_justified_by_cost_not_by_pointer_analysis():
    """The paragraph this replaces said not to start at ``entry`` because the program's own
    ``#[error_code]`` reaches string formatting the pointer analysis rejects. That is true, and it
    is a worked example of *routing around a pointer-analysis failure by moving the call target* —
    which is exactly the move both failing units then made one level deeper, in headers that
    paraphrase it. The reasons to prefer a handler are cost and scope; keep the technique out of
    them."""
    prompt = _flat(_author_system_prompt())
    body = prompt.split("Call the handler, not the dispatcher")[1].split("Start at the handler")[0]
    assert "pointer analysis" not in body
    assert "all of which the prover pays for" in body


def test_the_ladder_has_a_rung_for_a_rule_the_prover_cannot_analyze():
    """Step 3 enumerated four outcomes — compile error, VIOLATED, vacuous, timeout — and [3308] is
    none of them: it is an error, not a verdict. Every listed remedy was "change your rule", so an
    author facing an unnamed outcome generalized the pattern until the prover was happy."""
    prompt = _flat(_author_task_prompt())
    assert "A rule the prover could not analyze" in prompt
    for code in ("[3308]", "[3006]"):
        assert code in prompt


def test_the_three_remedies_are_named_and_reimplementation_is_not_one_of_them():
    prompt = _flat(_author_task_prompt())
    rung = prompt.split("A rule the prover could not analyze")[1].split("Step 4")[0]
    assert "summarize_for_prover(symbol_pattern, why)" in rung
    assert "weaken the rule to a sub-property" in rung
    assert "skip the property" in rung
    assert "Do not respond by moving the rule onto code the prover finds easier" in rung


def test_an_unanalyzable_handler_is_a_legitimate_skip():
    """The deeper cause under all four gaps: with the honest exits closed — the skip guidance is a
    list of discouragements, and an expected failure means "the program is defective", which was not
    true here — the mirror was the only door left open."""
    prompt = _flat(_author_task_prompt())
    assert "the prover cannot analyze the handler the property is about" in prompt
    assert "not an admission of defeat" in prompt


def test_publishing_requires_saying_what_each_rule_drives():
    assert "result(commentary, property_rules, rule_subjects)" in _author_task_prompt()
    assert "rule_subjects" in _author_system_prompt()


def test_the_judge_is_asked_what_code_is_under_test_before_anything_else():
    """The six-item checklist had no item a mirror would fail. It is non-vacuous, its assertion
    matches the property's words, coverage is complete, the logging is fine. Ordering is claimed to
    be by frequency, and this is what actually went wrong most."""
    prompt = _flat(_judge_system_prompt())
    assert prompt.index("What code is actually under test") < prompt.index("Vacuity")
    assert "proves a property of that helper" in prompt


def test_prover_evidence_does_not_license_verifying_a_copy_of_the_handler():
    """Without this the judge fix is inert: ``prover_output`` rebuttals are near-binding, and the
    author had a genuine [3308] transcript. Any objection would have been conceded away."""
    carve = _flat(_judge_system_prompt()).split("One carve-out")[1]
    assert "never binds you on *whether* the rule reaches the program" in carve


def test_the_first_remedy_names_a_tool_that_exists():
    """The gap that made the previous fix half-inert. The ladder told the author to "summarize the
    offending call", and nothing could: the tuning files are written once by the scaffold and
    `put_harness` writes the harness module. It worked that out itself — *"the only writing tool
    available is put_harness (writes this module only)"* — and reimplemented the handler instead."""
    prompt = _flat(_author_task_prompt())
    assert "summarize_for_prover(symbol_pattern, why)" in prompt
    assert "summarize_for_prover" in _flat(_author_system_prompt())


def test_the_author_is_warned_that_a_summary_is_unsound():
    """It is a sharper instrument than the mirror it replaces: summarizing the code a property is
    about gives a passing rule that checked nothing, and unlike a mirror it leaves no trace in the
    harness at all."""
    prompt = _flat(_author_task_prompt())
    assert "summarizing the code a property is *about*" in prompt
    assert "never the handler" in prompt


def test_the_judge_weighs_summaries_the_same_way_it_weighs_mirrors():
    prompt = _flat(_judge_system_prompt())
    item_one = prompt.split("2. **Vacuity")[0]
    assert "check the summaries the same way" in item_one
    assert "hollows out" in item_one


def test_an_unanalyzable_handler_skip_names_the_tool_it_tried():
    prompt = _flat(_author_task_prompt())
    assert "`summarize_for_prover` and weakening both failed" in prompt


def test_the_author_is_told_a_directive_can_match_nothing():
    """Otherwise the only feedback is a repeat of the same error, and the response to that is to
    guess another spelling — which is what happened five times in one unit."""
    prompt = _flat(_author_task_prompt())
    assert "matched **no symbol at all**" in prompt
    assert "the library function one level out" in prompt


def test_no_specific_symbol_is_offered_as_a_worked_example():
    """The example this replaces named `<vault::VaultError as core::fmt::Display>::fmt`, which is in
    no build of that program — the compiler inlines it. Two separate runs wrote it because the
    prompt did, kept it after being told it matched nothing, and paid a submission each time. A
    prompt cannot know a build's symbols; the report can, so it is the report's job."""
    prompt = _flat(_author_task_prompt())
    assert "VaultError" not in prompt


def test_the_author_is_told_when_to_stop_summarizing():
    """Nothing bounded the remedy, so it did not stop: eleven directives over nine submissions,
    seven matching real symbols, walking a formatting chain frame by frame while the error stayed
    identical. The code was inlined into the handler, where no summary reaches."""
    prompt = _flat(_author_task_prompt())
    assert "Give up on this remedy after about three directives down one path" in prompt
    assert "inlined *into the handler itself*" in prompt
