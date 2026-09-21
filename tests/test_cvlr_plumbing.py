"""The deterministic half of the CVLR backend, tested without a toolchain, a network or an LLM.

``docs/cvlr-backend-plan.md`` §6 puts "deterministic first" ahead of everything else, and the claim
that phase 1b is unit-testable is only worth making if these tests need nothing installed. So
nothing here shells out to cargo and nothing submits: what is checked is the reading and the
layering — the parse of ``cargo metadata``, the parse of a conf, and which conf keys a run owns.
"""

import json
from pathlib import Path

import pytest

from composer.cargo.metadata import parse_metadata
from composer.spec.cvlr import conf as cvlr_conf
from composer.spec.cvlr.crates import resolve
from composer.spec.cvlr_reference import SOLANA


# --------------------------------------------------------------------------------------------
# cargo metadata
# --------------------------------------------------------------------------------------------

#: A two-member workspace with one published dependency, in cargo's own shape. Hand-written rather
#: than recorded so that every field this codebase reads is visible in the test that reads it.
_METADATA = {
    "workspace_root": "/w",
    "target_directory": "/w/target",
    "workspace_members": ["path+file:///w/programs/lend#0.1.0", "path+file:///w#0.1.0"],
    "packages": [
        {
            "id": "path+file:///w/programs/lend#0.1.0",
            "name": "example-lending",
            "version": "0.1.0",
            "manifest_path": "/w/programs/lend/Cargo.toml",
            "source": None,
            "features": {"certora": [], "no-entrypoint": []},
            "targets": [
                {
                    "name": "example_lending",
                    "kind": ["cdylib"],
                    "crate_types": ["cdylib"],
                    "src_path": "/w/programs/lending/src/lib.rs",
                },
                {
                    "name": "bench",
                    "kind": ["bench"],
                    "crate_types": ["bin"],
                    "src_path": "/w/programs/lending/benches/b.rs",
                },
            ],
        },
        {
            "id": "path+file:///w#0.1.0",
            "name": "workspace-root-crate",
            "version": "0.1.0",
            "manifest_path": "/w/Cargo.toml",
            "source": None,
            "features": {},
            "targets": [{"name": "root", "kind": ["lib"], "crate_types": ["lib"], "src_path": "/w/src/lib.rs"}],
        },
        {
            "id": "registry+https://github.com/rust-lang/crates.io-index#cvlr@0.6.1",
            "name": "cvlr",
            "version": "0.6.1",
            "manifest_path": "/home/u/.cargo/registry/src/idx/cvlr-0.6.1/Cargo.toml",
            "source": "registry+https://github.com/rust-lang/crates.io-index",
            "features": {},
            "targets": [{"name": "cvlr", "kind": ["lib"], "crate_types": ["lib"], "src_path": "/reg/cvlr/src/lib.rs"}],
        },
        {
            "id": "registry+https://github.com/rust-lang/crates.io-index#cvlr-log@0.6.1",
            "name": "cvlr-log",
            "version": "0.6.1",
            "manifest_path": "/home/u/.cargo/registry/src/idx/cvlr-log-0.6.1/Cargo.toml",
            "source": "registry+https://github.com/rust-lang/crates.io-index",
            "features": {},
            "targets": [{"name": "cvlr_log", "kind": ["lib"], "crate_types": ["lib"], "src_path": "/reg/cvlr-log/src/lib.rs"}],
        },
    ],
}


def test_the_lib_target_is_the_one_an_artifact_is_named_after():
    """A package's bench and bin targets are not what a verification build produces."""
    lend = parse_metadata(_METADATA).member("example-lending")
    assert lend is not None
    assert lend.lib is not None
    assert lend.lib.name == "example_lending"
    assert lend.lib.artifact_stem == "example_lending"


def test_a_dash_in_a_lib_name_becomes_an_underscore_in_the_artifact():
    """Cargo names the file after the target with ``-`` normalized, and the ``.so`` path in the
    build manifest follows that, not the package name."""
    payload = json.loads(json.dumps(_METADATA))
    payload["packages"][0]["targets"][0]["name"] = "example-lending"
    lend = parse_metadata(payload).member("example-lending")
    assert lend is not None and lend.lib is not None
    assert lend.lib.artifact_stem == "example_lending"


def test_the_owning_crate_is_the_deepest_one_containing_the_file():
    """A workspace whose root is itself a package contains every nested crate's files too, so the
    shallow match is always available and always wrong."""
    workspace = parse_metadata(_METADATA)
    owner = workspace.owning(Path("/w/programs/lend/src/lib.rs"))
    assert owner is not None and owner.name == "example-lending"


def test_a_file_in_no_member_has_no_owning_crate():
    workspace = parse_metadata(_METADATA)
    assert workspace.owning(Path("/elsewhere/src/lib.rs")) is None


def test_a_crate_family_is_recognized_by_name():
    """``cvlr`` does not declare its family anywhere; the helper crates come in as ordinary
    dependencies, and an agent asking what a macro expands to needs all of them."""
    workspace = parse_metadata(_METADATA)
    assert [c.name for c in workspace.family("cvlr")] == ["cvlr", "cvlr-log"]


def test_a_published_dependency_is_distinguished_from_a_workspace_member():
    workspace = parse_metadata(_METADATA)
    cvlr = workspace.resolved("cvlr")
    lend = workspace.resolved("example-lending")
    assert cvlr is not None and not cvlr.is_local
    assert lend is not None and lend.is_local


# --------------------------------------------------------------------------------------------
# CVLR source resolution (§5.5)
# --------------------------------------------------------------------------------------------


def test_the_cvlr_source_roots_are_the_crate_directories_the_build_resolved():
    sources = resolve(parse_metadata(_METADATA))
    assert sources.core is not None and sources.core.version == "0.6.1"
    assert sources.roots() == (
        Path("/home/u/.cargo/registry/src/idx/cvlr-0.6.1"),
        Path("/home/u/.cargo/registry/src/idx/cvlr-log-0.6.1"),
    )


def test_a_project_on_the_reference_core_but_without_the_chain_crate_reports_that_gap():
    """Two different statements, and the corpus's advice depends on which one holds: an old
    ``cvlr-solana`` and no ``cvlr-solana`` at all."""
    gaps = {g.crate: g for g in resolve(parse_metadata(_METADATA)).gaps(SOLANA)}
    assert "cvlr" not in gaps, "the fixture pins the reference core, so it is not a gap"
    assert gaps["cvlr-solana"].resolved is None
    assert "is not a dependency of this project" in gaps["cvlr-solana"].describe()


def test_an_older_cvlr_than_the_corpus_was_written_against_is_reported():
    payload = json.loads(json.dumps(_METADATA))
    payload["packages"][2]["version"] = "0.4.1"
    gaps = {g.crate: g for g in resolve(parse_metadata(payload)).gaps(SOLANA)}
    assert gaps["cvlr"].resolved == "0.4.1"
    assert gaps["cvlr"].reference == "0.6.1"


# --------------------------------------------------------------------------------------------
# the conf
# --------------------------------------------------------------------------------------------

#: The public examples' ``Default.conf``, abridged. Both of its JSON5-isms are load-bearing here:
#: it would not survive ``json.loads``.
_REAL_CONF = """\
{
    // the rules this project means to check
    "rule": ["rule_correct_add", "rule_vacuous"],
    "prover_args": [
        "-solanaTACOptimize 0",
        "-unsatCoresForAllAsserts true",
    ],
    "loop_iter": 3,
    "rule_sanity": "basic",
}
"""


def test_a_real_conf_parses_despite_comments_and_trailing_commas():
    parsed = cvlr_conf.parse_conf(_REAL_CONF)
    assert parsed["rule"] == ["rule_correct_add", "rule_vacuous"]
    assert parsed["rule_sanity"] == "basic"


def test_an_integer_conf_value_stays_a_string():
    """``certoraUtils.read_conf_file`` reads ints as strings, which is why confs in the wild say
    ``"loop_iter": "1"``. Round-tripping a project's conf must not silently retype its values."""
    assert cvlr_conf.parse_conf(_REAL_CONF)["loop_iter"] == "3"


def test_a_conf_that_sets_a_key_twice_is_rejected():
    with pytest.raises(cvlr_conf.MalformedConf):
        cvlr_conf.parse_conf('{"loop_iter": "1", "loop_iter": "2"}')


def test_the_recommended_starting_point_is_the_base_when_a_project_has_no_conf():
    """Not an empty conf: an empty one has no loop bound, no SMT timeout and no prover flags, which
    verifies differently rather than neutrally."""
    base = cvlr_conf.load_base(None)
    assert base["loop_iter"] == "2"


# ---------------------------------------------------------------------------------------------
# Whose conf a run submits under
#
# The layering this module describes — project conf as the base, the run owning a small set of keys
# — was built and then never wired: the one production call site passed `load_base(None)`, so every
# run used the recommended starting point's settings and a project that had tuned its own prover
# flags was verified without them. The reference project keeps a `run.conf` carrying a twelve-seed
# solver portfolio and LIA/NIA hints aimed squarely at nonlinear arithmetic; a run against it used
# none of that and never said so.


def test_a_project_that_keeps_a_base_conf_is_verified_under_it(tmp_path):
    confs = tmp_path / "src" / "certora" / "confs"
    confs.mkdir(parents=True)
    (confs / "base.conf").write_text('{"loop_iter": "3", "prover_args": ["-smt_useNIA true"]}')

    base = cvlr_conf.load_base(cvlr_conf.project_conf(confs))

    assert base["loop_iter"] == "3"
    assert base["prover_args"] == ["-smt_useNIA true"]


def test_base_conf_wins_over_run_conf():
    """Both names appear in the corpus and three projects carry both. `base.conf` is named for the
    job — its siblings reach it through `override_base_config` — so it is the one that means "the
    project's settings", where `run.conf` may be one particular run's."""
    assert cvlr_conf.PROJECT_CONF_NAMES.index("base.conf") < cvlr_conf.PROJECT_CONF_NAMES.index(
        "run.conf"
    )


def test_a_per_rule_set_conf_is_not_adopted_as_the_base(tmp_path):
    """The reason discovery is a closed list rather than "any conf in the directory". Every other
    file there carries a `rule` list, and `InheritRules` would adopt it — grading this run on
    somebody else's rule selection, silently."""
    confs = tmp_path / "src" / "certora" / "confs"
    confs.mkdir(parents=True)
    (confs / "accounting_solvency_p2.conf").write_text('{"rule": ["their_rule"], "loop_iter": "9"}')

    assert cvlr_conf.project_conf(confs) is None
    assert "rule" not in cvlr_conf.load_base(None)


# ---------------------------------------------------------------------------------------------
# The two settings the author may move
#
# Both are sound — they decide how the prover spends its time, never what a green verdict means —
# which is the line the editable set may not cross. The portfolio is one named recipe rather than an
# editable flag list because `docs/upstream-defects.md` P8 measured what a plausible-looking flag
# does in the wrong combination: an eighteen-rule harness went from 6.7 minutes green to a two-hour
# timeout with thirteen rules unverified, on one argument.


def test_the_portfolio_is_the_recipe_the_corpus_uses_and_only_sound_flags():
    flags = cvlr_conf.NONLINEAR_SOLVER_PORTFOLIO
    assert any(f.startswith("-backendStrategy") for f in flags)
    assert "-smt_useNIA true" in flags and "-smt_useLIA true" in flags
    assert sum(f.startswith("-solvers ") for f in flags) >= 3
    # Nothing that changes what a verdict means may be smuggled in here.
    banned = ("optimistic_loop", "-solanaOptimistic", "rule_sanity", "-solanaTACSoundSignedMath")
    assert not [f for f in flags for b in banned if b in f]


def test_turning_the_portfolio_on_replaces_a_projects_own_solver_settings():
    """Through `merge_prover_args`: a project that already sets `-backendStrategy` or its own
    `-solvers` line gets those replaced, not duplicated. A conf naming one flag twice has two
    intentions in it and the prover picks."""
    base = {"prover_args": ["-solanaTACMathInt true", "-backendStrategy singleRace"]}
    on = cvlr_conf.with_solver_portfolio(base, True)
    assert on["prover_args"].count("-backendStrategy singleRace") == 0
    assert "-backendStrategy adaptive" in on["prover_args"]
    assert "-solanaTACMathInt true" in on["prover_args"]


def test_all_twelve_solver_instances_survive_being_applied():
    """`-solvers` is repeatable — each entry adds a parallel solver configuration — so it is the
    exception to `merge_prover_args`, which dedupes on the flag so `-solanaTACOptimize 2` overrides
    `-solanaTACOptimize 0`. Merging the portfolio the ordinary way collapses four `-solvers` lines
    into one and throws away nine of the twelve instances, silently, which is most of the recipe.
    Caught by building a real conf through this function rather than by hand."""
    out = cvlr_conf.with_solver_portfolio({"prover_args": ["-solanaTACMathInt true"]}, True)
    assert sum(a.startswith("-solvers ") for a in out["prover_args"]) == 4


def test_a_projects_own_solver_lines_are_replaced_as_a_set_not_appended_to():
    """A portfolio is one decision. Keeping the project's lines beside ours would run a mixture
    neither side chose."""
    base = {"prover_args": ["-solvers [z3:def{randomSeed=1}]", "-solvers [z3:def{randomSeed=2}]"]}
    out = cvlr_conf.with_solver_portfolio(base, True)
    assert sum(a.startswith("-solvers ") for a in out["prover_args"]) == 4
    assert "randomSeed=1" not in " ".join(out["prover_args"])


def test_turning_it_off_leaves_everything_that_was_not_the_portfolio():
    base = {"prover_args": ["-solanaTACMathInt true"]}
    roundtrip = cvlr_conf.with_solver_portfolio(cvlr_conf.with_solver_portfolio(base, True), False)
    assert roundtrip["prover_args"] == ["-solanaTACMathInt true"]


def test_the_loop_assumption_reads_both_spellings_and_defaults_off():
    """Absent means off — which is what the Prover does with a key it was not given, and what the
    Solana spec template intends. A hand-written conf may spell the bool as a string, the way one
    spells `loop_iter`, so both are read."""
    from composer.spec.cvlr.conf import has_optimistic_loop, with_optimistic_loop

    assert not has_optimistic_loop({})
    assert not has_optimistic_loop({"optimistic_loop": False})
    assert not has_optimistic_loop({"optimistic_loop": "false"})
    assert has_optimistic_loop({"optimistic_loop": True})
    assert has_optimistic_loop({"optimistic_loop": "true"})
    assert has_optimistic_loop(with_optimistic_loop({"loop_iter": "2"}, True))
    assert not has_optimistic_loop(with_optimistic_loop({"optimistic_loop": True}, False))


def test_the_loop_assumption_is_off_in_the_default_base():
    """Reachable is not the same as default. The template's position and the corpus's is off, and
    what changed is that the author has a rung to reach, not that the run starts on it."""
    from composer.spec.cvlr.conf import TEMPLATE_BASE, has_optimistic_loop

    assert not has_optimistic_loop(dict(TEMPLATE_BASE))


def test_a_project_that_already_wrote_the_portfolio_by_hand_is_recognized():
    """The reference project's own conf carries these settings. Recognizing them by flag rather
    than by a marker is what stops the author being told to turn on what is already on."""
    assert not cvlr_conf.has_solver_portfolio({"prover_args": ["-solanaTACMathInt true"]})
    assert cvlr_conf.has_solver_portfolio(
        cvlr_conf.with_solver_portfolio({"prover_args": []}, True)
    )


def test_the_loop_bound_is_written_the_way_a_conf_spells_an_integer():
    """Confs in the wild say `"loop_iter": "1"`, which is `read_conf`'s own `parse_int=str`."""
    assert cvlr_conf.with_loop_iter({}, 4)["loop_iter"] == "4"


def test_a_project_conf_that_never_mentions_rule_sanity_still_gets_vacuity_checking():
    """The hazard reading the project's conf introduces, and the reason it is closed here rather
    than hoped about. The recommended starting point and two of the five corpus base confs omit the
    key entirely, so adopting one wholesale would turn vacuity checking off — silently, while the
    author's prompt goes on saying it is on."""
    conf = cvlr_conf.solana_conf({"loop_iter": "3"}, cvlr_conf.RunOverlay(build_script="/w/o.py"))
    assert conf["rule_sanity"] == "basic"


def test_a_project_asking_for_more_vacuity_checking_keeps_it():
    """A floor, not an owned key: `advanced` is stronger and the project asking for it knows
    something this code does not."""
    base = {"rule_sanity": "advanced"}
    conf = cvlr_conf.solana_conf(base, cvlr_conf.RunOverlay(build_script="/w/o.py"))
    assert conf["rule_sanity"] == "advanced"


def test_turning_vacuity_checking_off_is_not_a_setting_a_run_honors():
    """`"none"` is the documented way to disable it and is exactly the configuration
    `docs/upstream-defects.md` P5 shows to be unsafe: with the check off, a [3308] raised inside
    the generated vacuity rule is reported as a clean VERIFIED."""
    base = {"rule_sanity": "none"}
    conf = cvlr_conf.solana_conf(base, cvlr_conf.RunOverlay(build_script="/w/o.py"))
    assert conf["rule_sanity"] == "basic"


def test_the_run_decides_which_server_whatever_the_base_says():
    """Corpus confs that name a server all say "production", and the run passes `--server` from the
    deployment environment. Two answers that agree until they do not."""
    base = {**cvlr_conf.parse_conf(_REAL_CONF), "server": "production"}
    assert "server" not in cvlr_conf.solana_conf(base, cvlr_conf.RunOverlay(build_script="/w/o.py"))


def test_a_conf_change_invalidates_a_stamp_earned_before_it():
    """A verdict earned under one loop bound, one `rule_sanity` or one solver portfolio is not a
    verdict under another, so the conf belongs in `version_history` beside the summaries and the
    munges."""
    one = cvlr_conf.conf_history(dict(cvlr_conf.TEMPLATE_BASE))
    two = cvlr_conf.conf_history({**cvlr_conf.TEMPLATE_BASE, "loop_iter": "3"})
    assert one != two
    assert cvlr_conf.conf_history(dict(cvlr_conf.TEMPLATE_BASE)) == one


def test_loops_are_bounded_soundly_and_the_bound_is_raised_instead():
    """A soundness-relevant pair of defaults, so it gets its own test rather than a line in another.

    ``optimistic_loop`` assumes the loop halt conditions hold rather than proving them, so it hides
    any violation reachable only after more iterations. It is a last resort: the intended remedies are
    to bound whatever determines the trip count, or to munge the loop, and only then to raise
    ``loop_iter``. The corpus agrees — of 354 confs across fifteen Solana projects, exactly one sets
    it true.

    The bound is 2, not the template's 1, because 1 is what made an earlier revision reach for
    ``optimistic_loop`` in the first place: with a bound of 1 *any* loop inside a handler fails before
    the rule's own property is reached, measured as VIOLATED on "Unwinding condition in a loop"
    against a loop in an Anchor handler's own borsh path (``docs/cvlr-backend-plan.md`` §7.6.2).
    Raising the bound answers that without assuming anything away.

    A future edit that flips this back should have to delete this test and say why.
    """
    base = cvlr_conf.load_base(None)
    assert base["optimistic_loop"] is False
    assert int(base["loop_iter"]) > 1


def test_the_recommended_starting_point_enables_no_optimistic_solana_flags():
    """The five ``-solanaOptimistic*`` flags are the most-attested block in the whole survey and the
    template sets none of them. A future edit that "restores" them should have to delete this."""
    flags = cvlr_conf.TEMPLATE_BASE["prover_args"]
    assert isinstance(flags, list)
    assert not [f for f in flags if f.startswith("-solanaOptimistic")]


def test_an_overlay_prover_arg_replaces_the_base_setting_of_the_same_flag():
    merged = cvlr_conf.merge_prover_args(
        ["-solanaTACOptimize 0", "-solanaStackSize 8192"], ["-solanaTACOptimize 2"]
    )
    assert merged == ["-solanaTACOptimize 2", "-solanaStackSize 8192"]


def test_a_new_overlay_flag_is_appended_rather_than_replacing_anything():
    merged = cvlr_conf.merge_prover_args(["-solanaTACOptimize 0"], ["-solanaTACMathInt true"])
    assert merged == ["-solanaTACOptimize 0", "-solanaTACMathInt true"]


def _overlay(**kwargs) -> dict:
    return cvlr_conf.solana_conf(
        cvlr_conf.parse_conf(_REAL_CONF),
        cvlr_conf.RunOverlay(build_script="/w/.certora_build/confined_build.py", **kwargs),
    )


def test_the_run_owns_the_build_script_whatever_the_base_says():
    """Fifteen of sixteen surveyed projects name their own build script, and every one of them runs
    the build unconfined inside the prover's process."""
    base = {**cvlr_conf.parse_conf(_REAL_CONF), "build_script": "scripts/certora_build.py"}
    conf = cvlr_conf.solana_conf(base, cvlr_conf.RunOverlay(build_script="/w/ours.py"))
    assert conf["build_script"] == "/w/ours.py"


def test_a_prebuilt_artifact_in_the_base_is_dropped():
    """``run_rust_build`` asserts the context has no ``files`` before a build script may set them,
    so keeping both would fail inside the prover instead of here."""
    base = {**cvlr_conf.parse_conf(_REAL_CONF), "files": ["target/deploy/x.so"]}
    assert "files" not in cvlr_conf.solana_conf(base, cvlr_conf.RunOverlay(build_script="/w/o.py"))


def test_inheriting_rules_keeps_the_projects_own_selection():
    assert _overlay()["rule"] == ["rule_correct_add", "rule_vacuous"]


def test_selecting_rules_replaces_the_projects_selection():
    assert _overlay(rules=cvlr_conf.SelectRules(("rule_vacuous",)))["rule"] == ["rule_vacuous"]


def test_asking_for_all_rules_removes_the_selection_entirely():
    """Distinct from inheriting: against a base naming two of thirty rules, one runs two and the
    other runs thirty."""
    assert "rule" not in _overlay(rules=cvlr_conf.AllRules())


def test_the_env_files_are_left_for_the_build_manifest_to_supply():
    """``cargo certora-sbf`` reads them from ``[package.metadata.certora]`` and
    ``certoraParseBuildScript`` applies them only when the conf has none; setting them here would
    override the project's own declaration with a guess at it."""
    conf = _overlay()
    assert "solana_inlining" not in conf and "solana_summaries" not in conf


def test_the_conf_is_emitted_as_plain_json():
    """JSON5 is what a conf may be written in, not what this writes: a file we emit and re-read is
    the one place a trailing comma buys nothing."""
    assert json.loads(cvlr_conf.dump_conf(_overlay()))["msg"] == ""

