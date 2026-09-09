"""The eighth munge kind: a whole module compiled from a stand-in.

``docs/cvlr-backend-plan.md`` §7.12 item 3 is why it exists — no attribute munge reaches a method,
because ``mock_fn`` aliases an item and an alias inside an ``impl`` is not one. The corpus answers
that by redirecting the *file*, and these tests are about the three things that makes true: the
deployed build must be untouched, the stand-in must land where ``#[path]`` will look for it, and a
replay must be idempotent.
"""

from pathlib import PurePosixPath

import pytest

from composer.spec.cvlr.munge import (
    AlreadyMunged,
    FunctionAmbiguous,
    ModuleNotFound,
    ModuleOutsideCrateSource,
    ModuleRedirect,
    Munged,
    mirror_path,
    munge_history,
)
from composer.spec.cvlr.tree import replay

FEATURE = "unit_withdrawals_share_redemption"
DECLARING = "programs/lending/src/invokes/mod.rs"

INVOKES = """\
//! Cross-program invocation wrappers.

pub mod f_token;
pub mod liquidity_layer;

pub use f_token::*;
pub use liquidity_layer::*;
"""

STAND_IN = """\
use anchor_lang::prelude::*;
use cvlr::nondet;

pub struct OperateCpiAccounts<'info> {
    pub liquidity_program: AccountInfo<'info>,
}

impl<'info> OperateCpiAccounts<'info> {
    pub fn operate_with_signer(&self, _p: (), _s: &[&[&[u8]]]) -> Result<(u64, u64)> {
        Ok((nondet(), nondet()))
    }
}
"""


def _redirect(module: str = "liquidity_layer", **kw) -> ModuleRedirect:
    return ModuleRedirect(
        path=kw.pop("path", DECLARING),
        module=module,
        substitute=kw.pop("substitute", STAND_IN),
        why="the liquidity CPI raises [3308] and its effects are not what the property is about",
        feature=kw.pop("feature", FEATURE),
    )


def _applied(source: str = INVOKES, **kw) -> str:
    from composer.spec.cvlr.munge import apply_module_redirect

    result = apply_module_redirect(source, _redirect(**kw))
    assert isinstance(result, Munged), result
    return result.source


# ---------------------------------------------------------------------------------------------
# where the stand-in goes


def test_the_stand_in_mirrors_the_module_under_the_mocks_tree():
    """The convention every corpus project follows, and which the scaffold's own
    ``certora/mocks/mod.rs`` already states: the mocks tree mirrors the source tree."""
    assert mirror_path(DECLARING, "liquidity_layer") == PurePosixPath(
        "programs/lending/src/certora/mocks/invokes/liquidity_layer.rs"
    )


def test_a_module_declared_at_the_crate_root_mirrors_directly_under_mocks():
    assert mirror_path("programs/lending/src/lib.rs", "utils") == PurePosixPath(
        "programs/lending/src/certora/mocks/utils.rs"
    )


def test_a_declaring_file_outside_a_crate_source_root_is_refused():
    """The mirror is derived from the crate's ``src/``; without one there is nowhere to put the
    stand-in, and inventing a home would produce a ``#[path]`` resolving to nothing."""
    from composer.spec.cvlr.munge import apply_module_redirect

    assert mirror_path("build.rs", "helpers") is None
    result = apply_module_redirect(INVOKES, _redirect(path="build.rs"))

    assert isinstance(result, ModuleOutsideCrateSource)


def test_the_path_attribute_is_relative_to_the_declaring_files_directory():
    """Rust resolves ``#[path]`` against the declaring file's directory, not the crate root. Getting
    this wrong compiles to an error that names the *module*, not the attribute."""
    out = _applied()

    assert 'path = "../certora/mocks/invokes/liquidity_layer.rs"' in out


def test_the_stand_in_is_carried_as_a_created_file():
    """No pristine source backs a substitute, so the tree cannot derive it by replaying onto the
    developer's copy — it goes into the overlay whole."""
    created = _redirect().created

    assert created == {
        "programs/lending/src/certora/mocks/invokes/liquidity_layer.rs": STAND_IN
    }


def test_the_other_kinds_create_nothing():
    """``created`` is on every kind so a caller need not match on which one it has."""
    from composer.spec.cvlr.munge import DeriveSwap, FunctionMunge, InlineNever

    assert FunctionMunge(path="a.rs", function="f", kind=InlineNever(), why="w").created == {}
    assert DeriveSwap(path="a.rs", swaps=(), why="w").created == {}


# ---------------------------------------------------------------------------------------------
# the deployed build


def test_the_declaration_itself_is_untouched():
    """With the feature off the developer's `mod` line is exactly what they wrote — the whole
    safety story, and the same one every other kind tells."""
    out = _applied()

    assert "pub mod liquidity_layer;" in out
    assert out.count("pub mod liquidity_layer;") == 1


def test_the_sibling_module_is_left_alone():
    out = _applied()

    assert "\npub mod f_token;\n" in out, "only the named module is gated"


def test_the_gate_names_the_recording_unit():
    out = _applied(feature="unit_other")

    assert 'feature = "unit_other"' in out


# ---------------------------------------------------------------------------------------------
# replay


def test_replaying_a_landed_redirect_is_a_no_op():
    """The tree is rebuilt from pristine and replayed on every build, so this runs constantly."""
    from composer.spec.cvlr.munge import apply_module_redirect

    once = _applied()

    assert isinstance(apply_module_redirect(once, _redirect()), AlreadyMunged)


def test_replay_applies_it_through_the_shared_driver():
    out, drifted = replay(INVOKES, (_redirect(),))

    assert drifted == ()
    assert "#[cfg_attr(feature" in out


def test_a_module_that_is_gone_reports_rather_than_guessing():
    from composer.spec.cvlr.munge import apply_module_redirect

    moved = INVOKES.replace("pub mod liquidity_layer;", "pub mod liquidity_bridge;")
    result = apply_module_redirect(moved, _redirect())

    assert isinstance(result, ModuleNotFound)
    assert "liquidity_bridge" in result.nearby, "the refusal says what the file does declare"


def test_an_inline_module_is_not_redirectable():
    """``#[path]`` names the file a module is loaded from, and an inline module is not loaded from
    one — so the attribute would compile and change nothing."""
    from composer.spec.cvlr.munge import apply_module_redirect

    inline = "pub mod liquidity_layer { pub fn go() {} }\n"
    result = apply_module_redirect(inline, _redirect())

    assert isinstance(result, ModuleNotFound)


def test_two_declarations_of_one_module_are_refused_rather_than_guessed():
    from composer.spec.cvlr.munge import apply_module_redirect

    doubled = INVOKES + '#[cfg(test)]\npub mod liquidity_layer;\n'
    result = apply_module_redirect(doubled, _redirect())

    assert isinstance(result, FunctionAmbiguous)
    assert len(result.lines) == 2


def test_an_indented_declaration_keeps_its_indentation():
    """A `mod` inside a `#[cfg]`-gated block is indented, and an attribute at column zero above it
    is still valid Rust but reads as though it belongs to something else."""
    from composer.spec.cvlr.munge import apply_module_redirect

    nested = "mod outer {\n    pub mod liquidity_layer;\n}\n"
    result = apply_module_redirect(nested, _redirect())
    assert isinstance(result, Munged)

    assert "\n    #[cfg_attr(feature" in result.source


# ---------------------------------------------------------------------------------------------
# identity


def test_the_edit_id_moves_with_the_stand_in():
    """A re-authored substitute is a different edit and must not inherit the previous approval or
    prover stamp — the stand-in *is* what the compiler sees."""
    base = _redirect().edit_id
    other = _redirect(substitute=STAND_IN + "\n// and one more thing\n").edit_id

    assert base != other


def test_the_edit_id_is_stable_across_rewordings():
    """`why` is prose. Correcting it must not cost a prover submission."""
    a = ModuleRedirect(DECLARING, "liquidity_layer", STAND_IN, "one reason", FEATURE)
    b = ModuleRedirect(DECLARING, "liquidity_layer", STAND_IN, "another entirely", FEATURE)

    assert a.edit_id == b.edit_id


def test_two_units_redirecting_the_same_module_do_not_collide():
    mine, theirs = _redirect().edit_id, _redirect(feature="unit_other").edit_id

    assert mine != theirs


def test_it_reaches_version_history_like_every_other_kind():
    (token,) = munge_history((_redirect(),))

    assert token.startswith("munge:") and "module[liquidity_layer" in token


def test_describe_reads_as_a_sentence():
    assert _redirect().describe() == "`liquidity_layer` is compiled from a stand-in module"


@pytest.mark.parametrize("decl", ["mod m;", "pub mod m;", "pub(crate) mod m;", "pub(super) mod m;"])
def test_every_visibility_a_declaration_can_carry_is_matched(decl: str):
    from composer.spec.cvlr.munge import apply_module_redirect

    result = apply_module_redirect(decl + "\n", _redirect(module="m"))

    assert isinstance(result, Munged), result


# ---------------------------------------------------------------------------------------------
# the cross-crate case


PROGRAM_MANIFEST = """\
[package]
name = "lending"

[features]
default = []
no-entrypoint = []
certora = ["no-entrypoint", "dep:cvlr", "library/certora"]
"""


def test_a_forwarded_feature_is_recognised():
    from composer.spec.cvlr.munge import forwards_feature

    assert forwards_feature(PROGRAM_MANIFEST, "library", "certora")


@pytest.mark.parametrize("dependency", ["liquidity", "lending_reward_rate_model"])
def test_a_dependency_the_program_does_not_forward_to_is_reported(dependency: str):
    """The precondition worth checking because its absence is *silent*: a `cfg_attr` naming a
    feature the crate does not enable compiles perfectly and never activates, so the munge lands,
    the build succeeds, and the Prover reports exactly what it reported before."""
    from composer.spec.cvlr.munge import forwards_feature

    assert not forwards_feature(PROGRAM_MANIFEST, dependency, "certora")


def test_an_unparseable_manifest_is_treated_as_not_forwarding():
    """Fail closed: guessing "probably fine" here buys a silent no-op."""
    from composer.spec.cvlr.munge import forwards_feature

    assert not forwards_feature("this is not toml [[[", "library", "certora")


def test_a_feature_that_is_not_a_list_is_treated_as_not_forwarding():
    from composer.spec.cvlr.munge import forwards_feature

    assert not forwards_feature(
        '[features]\ncertora = "yes"\n', "library", "certora"
    )


def test_the_run_global_munges_are_the_ones_no_unit_feature_gates():
    """A redirect inside a dependency is gated on the shared `certora`, so it is compiled into
    every unit's build whoever recorded it — and every unit's judge has to be shown it."""
    from composer.spec.cvlr.conf import DEFAULT_FEATURE
    from composer.spec.cvlr.tree import SharedTree, UnitEdits
    from pathlib import Path

    mine = _redirect(feature=FEATURE)
    shared = _redirect(module="safe_math", path="crates/library/src/math/mod.rs",
                       feature=DEFAULT_FEATURE)
    tree = SharedTree(pristine=Path("/nonexistent"), root=Path("/nonexistent"))
    tree._units["a"] = UnitEdits(module_path=Path("a.rs"), draft="", munges=(mine, shared))

    assert tree.run_global_munges() == (shared,), "only the un-gateable one travels"


def test_two_units_recording_the_same_run_global_munge_yield_one():
    """Deduplicated by `edit_id`, so a judge does not read the same caveat twice."""
    from composer.spec.cvlr.conf import DEFAULT_FEATURE
    from composer.spec.cvlr.tree import SharedTree, UnitEdits
    from pathlib import Path

    shared = _redirect(module="safe_math", path="crates/library/src/math/mod.rs",
                       feature=DEFAULT_FEATURE)
    tree = SharedTree(pristine=Path("/nonexistent"), root=Path("/nonexistent"))
    for unit in ("a", "b"):
        tree._units[unit] = UnitEdits(module_path=Path(f"{unit}.rs"), draft="", munges=(shared,))

    assert tree.run_global_munges() == (shared,)


def test_the_briefing_marks_a_run_global_munge_as_everyones():
    """A judge told only "the author edited the program" would read a dependency-wide swap as this
    unit's own choice, and would not think to ask whether its rules touch it."""
    from composer.spec.cvlr.conf import DEFAULT_FEATURE
    from composer.spec.cvlr.state import HarnessAssumptions

    shared = _redirect(module="safe_math", path="crates/library/src/math/mod.rs",
                       feature=DEFAULT_FEATURE)
    briefing = "\n".join(HarnessAssumptions(summaries=(), munges=(shared,)).briefing())

    assert "IN FORCE FOR EVERY UNIT OF THIS RUN" in briefing


def test_a_unit_scoped_munge_is_not_marked_that_way():
    from composer.spec.cvlr.state import HarnessAssumptions

    briefing = "\n".join(HarnessAssumptions(summaries=(), munges=(_redirect(),)).briefing())

    assert "IN FORCE FOR EVERY UNIT" not in briefing


def test_a_units_own_run_global_munge_is_not_shown_twice():
    """The recording unit has it in its own state *and* sees it in the run-global set."""
    from composer.spec.cvlr.conf import DEFAULT_FEATURE
    from composer.spec.cvlr.state import harness_assumptions

    shared = _redirect(module="safe_math", path="crates/library/src/math/mod.rs",
                       feature=DEFAULT_FEATURE)
    state = {"munges": [shared], "summaries": []}

    got = harness_assumptions(state, None, (shared,))  # type: ignore[arg-type]

    assert got.munges == (shared,)
