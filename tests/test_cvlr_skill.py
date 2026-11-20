"""The ``certora-cvlr`` commands the Solana and Soroban skills run.

The tests marked ``skill_interface`` hold the code to :mod:`composer.spec.cvlr.skill_interface`,
the list of what the skills name. One that fails here means either the change breaks a skill, or
the interface module and the skill's text both need the new name.
"""

import re
import shutil
import tomllib
from pathlib import Path

import pytest

from composer.cli import certora_cvlr
from composer.cli.certora_cvlr import build_parser, main
from prover_output_utility.models import JobStatus
from composer.prover.core import JobFailed, ProverReport, RunFailed
from composer.prover.ptypes import Counterexample, RulePath, RuleResult, UserAssertion
from composer.spec.cvlr import skill_interface as interface
from composer.spec.cvlr.conf import DEFAULT_FEATURE
from composer.spec.cvlr.harness import SHARED_INVARIANTS, HarnessModule, declare_modules
from composer.spec.cvlr.scaffold import SPECS_DIR
from composer.spec.cvlr import skill
from composer.spec.cvlr.skill import (
    INVARIANT_RULES,
    REFERENCE_DIR,
    SUMMARY_LAYERS_DIR,
    ComponentNameRejected,
    Running,
    component_modules,
    reference_documents,
    selected_rules,
)
from composer.prover.conf import ExcludeRules, InheritRules, SelectRules
from composer.spec.cvlr.tuning import (
    SUMMARIES,
    SummaryDirective,
    compose_env,
    extend_composite,
    layer_patterns,
    render_layer,
)

_REPO = Path(__file__).resolve().parent.parent


def _subcommand_flags() -> dict[str, set[str]]:
    parser = build_parser()
    (commands,) = [a for a in parser._actions if a.dest == "command"]
    assert isinstance(commands.choices, dict)
    return {
        name: {flag for action in sub._actions for flag in action.option_strings}
        for name, sub in commands.choices.items()
    }


@pytest.mark.skill_interface
def test_the_console_script_the_skill_runs_is_installed():
    scripts = tomllib.loads((_REPO / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts[interface.COMMAND] == "composer.cli.certora_cvlr:main"


@pytest.mark.skill_interface
def test_every_subcommand_and_flag_the_skill_passes_exists():
    flags = _subcommand_flags()
    for command, wanted in interface.SUBCOMMANDS.items():
        assert command in flags, f"no subcommand {command!r}"
        assert set(wanted) <= flags[command], set(wanted) - flags[command]


@pytest.mark.skill_interface
def test_the_reference_holds_every_file_and_section_the_skill_reads():
    docs = reference_documents(package="vault", cvlr_versions="cvlr 0.6.2", caveats=(), shim=None)
    assert str(REFERENCE_DIR) == interface.REFERENCE_DIR
    assert {Path(f) for f in interface.REFERENCE_FILES} <= set(docs)
    author = docs[Path("author.md")].splitlines()
    assert [h for h in interface.AUTHOR_SECTIONS if h not in author] == []


def _soroban_documents() -> dict[Path, str]:
    return reference_documents(
        package="token",
        cvlr_versions="cvlr 0.6.2, cvlr-soroban 0.4.0",
        caveats=("The CVLR manual is written for Solana.",),
        shim=None,
        chain="soroban",
        token_model=("`token_a` in `src/lib.rs`",),
    )


@pytest.mark.skill_interface
def test_the_soroban_reference_holds_every_file_and_section_the_soroban_skill_reads():
    docs = _soroban_documents()
    assert {Path(f) for f in interface.SOROBAN_REFERENCE_FILES} <= set(docs)
    # No recipes: the pipeline's own Soroban author is handed none, so the reference ships none.
    assert [p for p in docs if p.parent == Path("recipes")] == []
    author = docs[Path("author.md")].splitlines()
    assert [h for h in interface.SOROBAN_AUTHOR_SECTIONS if h not in author] == []


def test_the_soroban_reference_speaks_soroban_not_solana():
    docs = _soroban_documents()
    assert "Soroban Prover" in docs[Path("cvlr.md")]
    assert "cvlr_deserialize_nondet_accounts" not in docs[Path("cvlr.md")]
    assert "storage" in docs[Path("invariants.md")]
    assert "`recipes/" not in docs[Path("README.md")]
    assert "summarize_for_prover" not in docs[Path("README.md")]


@pytest.mark.parametrize("chain", ["solana", "soroban"])
def test_the_readme_translates_every_tool_the_author_and_judge_texts_define(chain):
    """A tool those texts name but the table omits leaves the skill's agent reading instructions
    for something it does not have. Their tools are defined in bullets, ``* `name(...)` —``."""
    docs = (
        _soroban_documents()
        if chain == "soroban"
        else reference_documents(package="vault", cvlr_versions="cvlr 0.6.2", caveats=(), shim=None)
    )
    table = {
        name
        for row in docs[Path("README.md")].splitlines()
        if row.startswith("| `")
        for name in re.findall(r"`([a-z_]+)(?:\(\w*\))?`", row.split("|")[1])
    }
    defined = {
        name
        for doc in ("author.md", "judge.md")
        for name in re.findall(r"^\* `([a-z_]+)\(", docs[Path(doc)], flags=re.MULTILINE)
    }
    assert defined - table == set()
    assert "rule_subjects" in table


@pytest.mark.skill_interface
def test_the_harness_layout_is_where_the_skill_says():
    assert str(SPECS_DIR) == interface.SPECS_DIR
    assert str(SPECS_DIR / f"{SHARED_INVARIANTS}.rs") == interface.SHARED_INVARIANTS_FILE
    assert interface.UNIT_FEATURE.format(module="deposits") == HarnessModule("deposits").feature
    assert interface.HARNESS_FEATURE == DEFAULT_FEATURE
    assert interface.INVARIANT_RULES == INVARIANT_RULES
    assert interface.SUMMARY_LAYER.format(module="deposits") == str(
        SUMMARY_LAYERS_DIR / "deposits.txt"
    )


@pytest.mark.skill_interface
@pytest.mark.parametrize(
    ("outcome", "state"),
    [
        (Running("RUNNING"), "RUNNING"),
        (ProverReport(raw_rule_status={}, result_str="", link="L", certora_run_stdout=""), "FINISHED"),
        (RunFailed("the job failed"), "FAILED"),
    ],
)
def test_wait_prints_one_of_the_states_the_skill_loops_on(monkeypatch, capsys, outcome, state):
    async def fake_wait(link, *, seconds, prover_opts):
        return outcome

    monkeypatch.setattr(certora_cvlr, "wait", fake_wait)
    main(["wait", "https://prover.certora.com/output/1/2?anonymousKey=k", "--for", "1"])
    first = capsys.readouterr().out.split()[0]
    assert first == state and state in interface.WAIT_STATES


@pytest.mark.parametrize(
    ("outcome", "recipe"),
    [
        (
            ProverReport(
                raw_rule_status={RulePath("rule_a"): "SANITY_FAILED"},
                result_str="",
                link="L",
                certora_run_stdout="",
            ),
            "K12",
        ),
        (JobFailed(status=JobStatus.HALTED, link="L"), "K9"),
    ],
)
def test_wait_names_the_recipes_the_author_would_be_handed(monkeypatch, capsys, outcome, recipe):
    async def fake_wait(link, *, seconds, prover_opts):
        return outcome

    monkeypatch.setattr(certora_cvlr, "wait", fake_wait)
    main(["wait", "https://prover.certora.com/output/1/2?anonymousKey=k", "--for", "1"])
    assert f"  recipes/{recipe}.md: " in capsys.readouterr().out


def test_wait_points_at_cex_when_a_rule_is_violated(monkeypatch, capsys):
    async def fake_wait(link, *, seconds, prover_opts):
        return ProverReport(
            raw_rule_status={RulePath("rule_a"): "VIOLATED"},
            result_str="",
            link="L",
            certora_run_stdout="",
        )

    monkeypatch.setattr(certora_cvlr, "wait", fake_wait)
    main(["wait", "https://prover.certora.com/output/1/2?anonymousKey=k", "--for", "1"])
    assert "certora-cvlr cex L --rule <rule>" in capsys.readouterr().out


def test_cex_prints_each_violated_instance_s_trace(monkeypatch, capsys):
    async def fake_violations(link, rule, *, prover_opts):
        return [
            RuleResult(
                path=RulePath(rule),
                counterexample=Counterexample(trace="<trace>t</trace>", origin=UserAssertion()),
                status="VIOLATED",
            )
        ]

    monkeypatch.setattr(certora_cvlr, "violations", fake_violations)
    assert main(["cex", "L", "--rule", "rule_a"]) == 0
    out = capsys.readouterr().out
    assert "== rule_a" in out and "<trace>t</trace>" in out


def test_cex_on_a_rule_with_no_violation_exits_1(monkeypatch, capsys):
    async def fake_violations(link, rule, *, prover_opts):
        return []

    monkeypatch.setattr(certora_cvlr, "violations", fake_violations)
    assert main(["cex", "L", "--rule", "rule_a"]) == 1
    assert "No instance of rule_a is VIOLATED" in capsys.readouterr().out


def test_wait_names_no_recipe_for_a_clean_job(monkeypatch, capsys):
    async def fake_wait(link, *, seconds, prover_opts):
        return ProverReport(
            raw_rule_status={RulePath("rule_a"): "VERIFIED"}, result_str="", link="L", certora_run_stdout=""
        )

    monkeypatch.setattr(certora_cvlr, "wait", fake_wait)
    main(["wait", "https://prover.certora.com/output/1/2?anonymousKey=k", "--for", "1"])
    assert "recipes/" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("selection", "chosen"),
    [
        (InheritRules(), ("rule_a", "rule_b_deposit", "rule_b_withdraw")),
        (SelectRules(("rule_b_*",)), ("rule_b_deposit", "rule_b_withdraw")),
        (ExcludeRules(("rule_b_*",)), ("rule_a",)),
        (SelectRules(("rule_c",)), ()),
    ],
)
def test_a_job_names_the_listed_rules_the_selection_matches(selection, chosen):
    listed = {"rule_a", "rule_b_deposit", "rule_b_withdraw"}
    assert selected_rules(selection, listed) == chosen


def test_a_summaries_layer_s_patterns_are_its_uncommented_lines():
    layer = render_layer(
        (
            SummaryDirective("^a::f$", "formatting only"),
            SummaryDirective("^b::g$", "two lines\nof why", returns="(*i64)(r1+8):num"),
        )
    )
    assert layer_patterns(layer) == ("^a::f$", "^b::g$")


@pytest.mark.skill_interface
def test_unsat_core_takes_exactly_one_rule(capsys):
    assert main(["prove", ".", "--component", "deposits", "--unsat-core"]) == 2
    assert "exactly one --rule" in capsys.readouterr().err


def test_vacuity_prints_the_finding_and_the_core(monkeypatch, capsys):
    core = (_REPO / "tests" / "data" / "cvlr_unsat_core" / "assertion_in_core.txt").read_text()
    fetched: list[tuple[str, str]] = []

    def fake_fetch(api, link, rule):
        fetched.append((link, rule))
        return (core,)

    monkeypatch.setattr(skill, "fetch_unsat_cores", fake_fetch)
    monkeypatch.setattr(skill, "results_api", lambda: None)
    assert main(["vacuity", "L", "--rule", "rule_x"]) == 0
    out = capsys.readouterr().out
    assert fetched == [("L", "rule_x")]
    assert out.startswith("The rule's assertion is in the unsat core")
    assert out.endswith(f"Unsat core data:\n{core}\n")


@pytest.mark.parametrize("argv", [["toolchain"], ["toolchain", "--chain", "soroban"]])
def test_without_rustup_the_toolchain_check_stops_and_installs_nothing(monkeypatch, capsys, argv):
    monkeypatch.setattr(skill.shutil, "which", lambda name: None)
    assert main(argv) == 1
    out = capsys.readouterr().out
    assert out.startswith("MISSING rustup") and "https://rustup.rs" in out
    assert "install:" not in out


def _fake_tools(monkeypatch, run_answers: dict[tuple[str, ...], str | None]) -> None:
    monkeypatch.setattr(skill.shutil, "which", lambda name: f"/usr/bin/{name}")

    async def fake_run(*argv: str) -> str | None:
        return run_answers.get(argv)

    monkeypatch.setattr(skill, "_run", fake_run)


def test_the_soroban_toolchain_check_asks_for_the_wasm_toolchain(monkeypatch, capsys):
    """The Soroban checks: the pinned toolchain with the wasm target, and none of Solana's tools."""
    from composer.cargo.wasm import WASM_TARGET
    from composer.spec.cvlr.conf import SOROBAN_RUST_TOOLCHAIN

    _fake_tools(
        monkeypatch,
        {("rustup", "toolchain", "list"): f"{skill.HOST_RUST_TOOLCHAIN}-aarch64-apple-darwin"},
    )
    assert main(["toolchain", "--chain", "soroban"]) == 1
    out = capsys.readouterr().out
    assert "MISSING Soroban Rust toolchain" in out
    assert f"rustup toolchain install {SOROBAN_RUST_TOOLCHAIN} --profile minimal --target {WASM_TARGET}" in out
    assert "platform tools" not in out and "rustfilt" not in out


def test_the_soroban_toolchain_check_asks_for_the_wasm_target_alone(monkeypatch, capsys):
    from composer.cargo.wasm import WASM_TARGET
    from composer.spec.cvlr.conf import SOROBAN_RUST_TOOLCHAIN

    _fake_tools(
        monkeypatch,
        {
            ("rustup", "toolchain", "list"): "\n".join(
                f"{v}-aarch64-apple-darwin"
                for v in (skill.HOST_RUST_TOOLCHAIN, SOROBAN_RUST_TOOLCHAIN)
            ),
            ("rustup", "target", "list", "--installed", "--toolchain", SOROBAN_RUST_TOOLCHAIN): (
                "aarch64-apple-darwin"
            ),
        },
    )
    assert main(["toolchain", "--chain", "soroban"]) == 1
    out = capsys.readouterr().out
    assert f"MISSING {WASM_TARGET} target" in out
    assert f"rustup target add {WASM_TARGET} --toolchain {SOROBAN_RUST_TOOLCHAIN}" in out


def test_a_reference_set_knows_its_chain():
    from composer.spec.cvlr.reference import SOLANA_V2, SOROBAN_V22, SOROBAN_V25, chain_of

    assert chain_of(SOROBAN_V25) == "soroban"
    assert chain_of(SOROBAN_V22) == "soroban"
    assert chain_of(SOLANA_V2) == "solana"


def test_wait_on_a_soroban_job_names_no_solana_recipe(monkeypatch, capsys):
    """The Soroban reference ships no recipes directory, so pointing at one would misdirect."""

    async def fake_wait(link, *, seconds, prover_opts):
        return ProverReport(
            raw_rule_status={RulePath("rule_a"): "SANITY_FAILED"},
            result_str="",
            link="L",
            certora_run_stdout="",
        )

    monkeypatch.setattr(certora_cvlr, "wait", fake_wait)
    main(["wait", "https://prover.certora.com/output/1/2?anonymousKey=k", "--for", "1", "--chain", "soroban"])
    assert "recipes/" not in capsys.readouterr().out


def test_a_units_summaries_compose_from_the_package_file_as_from_the_layers():
    layer = "; formatting only\n^<vault::VaultError as core::fmt::Display>::fmt$\n"
    package = compose_env(SUMMARIES)
    assert extend_composite(package, layer) == compose_env(SUMMARIES, unit_layer=layer)
    assert extend_composite(package, "") == package


def test_every_recipe_the_index_names_is_a_file_under_its_id():
    docs = reference_documents(package="vault", cvlr_versions="cvlr 0.6.2", caveats=(), shim=None)
    index = docs[Path("recipes") / "index.md"]
    ids = [p.stem for p in docs if p.parent == Path("recipes") and p.name != "index.md"]
    assert ids
    assert [i for i in ids if f"Retrieval ID: {i}\n" not in index] == []


def test_a_shared_module_is_compiled_with_every_unit(tmp_path: Path):
    mod_rs = declare_modules(
        tmp_path, [HarnessModule("withdraw"), HarnessModule("deposit")], shared=("invariants",)
    )
    declarations = [line for line in mod_rs.read_text().splitlines() if not line.startswith("//")]
    assert declarations == [
        "",
        "pub mod invariants;",
        '#[cfg(feature = "certora_unit_deposit")]',
        "pub mod deposit;",
        '#[cfg(feature = "certora_unit_withdraw")]',
        "pub mod withdraw;",
    ]


def test_a_unit_may_not_take_a_shared_module_s_name(tmp_path: Path):
    with pytest.raises(ValueError, match="invariants"):
        declare_modules(tmp_path, [HarnessModule("invariants")], shared=("invariants",))


@pytest.mark.parametrize(
    ("names", "complaint"),
    [
        (["Deposits"], "use 'deposits'"),
        (["admin-config"], "use 'admin_config'"),
        (["invariants"], "declared by setup itself"),
        (["invariant_rules"], "declared by setup itself"),
        (["deposits", "deposits"], "more than once"),
    ],
)
def test_a_component_name_the_agent_could_not_find_as_a_file_is_refused(names, complaint):
    with pytest.raises(ComponentNameRejected, match=complaint):
        component_modules(names)


def test_a_refused_component_name_exits_1_before_anything_is_written(tmp_path: Path, capsys):
    assert main(["setup", str(tmp_path), "--component", "Deposits"]) == 1
    assert "use 'deposits'" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


_MANIFEST = """\
[package]
name = "vault"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib", "lib"]

[dependencies]
solana-program = "2"
"""


@pytest.mark.expensive
@pytest.mark.skill_interface
def test_setup_scaffolds_declares_and_compiles_and_a_second_setup_keeps_the_rules(
    tmp_path: Path, capsys
):
    """Against real cargo, off the network. The second setup names a component the first did not,
    and must keep what was written in the meantime."""
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    (tmp_path / "Cargo.toml").write_text(_MANIFEST)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("pub fn deposit(amount: u64) -> u64 { amount }\n")

    assert main(["setup", str(tmp_path), "--component", "deposits"]) == 0, capsys.readouterr()
    specs = tmp_path / SPECS_DIR
    authored = "//! The rules for deposits.\n// an author's work\n"
    (specs / "deposits.rs").write_text(authored)
    invariants = specs / "invariants.rs"
    invariants.write_text(invariants.read_text() + "// an invariant\n")

    assert main(
        ["setup", str(tmp_path), "--component", "deposits", "--component", "withdrawals"]
    ) == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "compiled with --features certora: ok" in out
    assert (specs / "deposits.rs").read_text() == authored
    assert invariants.read_text().endswith("// an invariant\n")
    assert (specs / "withdrawals.rs").is_file()
    assert (specs / f"{INVARIANT_RULES}.rs").is_file()
    mod_rs = (specs / "mod.rs").read_text()
    assert "pub mod invariants;" in mod_rs and "pub mod withdrawals;" in mod_rs
    manifest = (tmp_path / "Cargo.toml").read_text()
    assert "certora_unit_deposits = []" in manifest and "certora_unit_withdrawals = []" in manifest
    reference = tmp_path / interface.REFERENCE_DIR
    assert all((reference / f).is_file() for f in interface.REFERENCE_FILES)


_SHARED = """\
//! Structural invariants.

pub fn amount_is_small(amount: u64) -> bool {
    amount < 1000
}
"""

_RULES = """\
use cvlr::prelude::*;

use crate::certora::specs::invariants::amount_is_small;

#[rule]
pub fn rule_deposit_returns_its_amount() {
    let amount: u64 = nondet();
    cvlr_assume!(amount_is_small(amount));
    let returned = crate::deposit(amount);
    clog!(amount, returned);
    cvlr_assert_eq!(returned, amount);
}

#[rule]
pub fn rule_deposit_returns_more() {
    let amount: u64 = nondet();
    let returned = crate::deposit(amount);
    clog!(amount, returned);
    cvlr_assert!(returned > amount);
}

#[rule]
pub fn rule_deposit_of_nothing() {
    let amount: u64 = nondet();
    cvlr_assume!(amount < 10);
    cvlr_assume!(amount > 20);
    cvlr_assert_eq!(crate::deposit(amount), amount);
}
"""


def _submitted(out: str) -> str:
    (link,) = [
        line.removeprefix("submitted: ")
        for line in out.splitlines()
        if line.startswith("submitted: ")
    ]
    return link


def _wait_for(link: str, capsys) -> str:
    for _ in range(60):
        main(["wait", link, "--for", "60"])
        out = capsys.readouterr().out
        if not out.startswith("RUNNING"):
            return out
    raise AssertionError(f"{link} still running")


@pytest.mark.expensive
@pytest.mark.skill_interface
def test_a_component_that_assumes_a_shared_invariant_is_checked_proved_and_waited_for(
    tmp_path: Path, capsys
):
    """The whole loop a generator runs, against the cloud: one rule that holds, one that does not,
    and a predicate from the shared module both components' builds compile."""
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    (tmp_path / "Cargo.toml").write_text(_MANIFEST)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("pub fn deposit(amount: u64) -> u64 { amount }\n")
    assert main(["setup", str(tmp_path), "--component", "deposits", "--no-check"]) == 0
    specs = tmp_path / SPECS_DIR
    (specs / "invariants.rs").write_text(_SHARED)
    (specs / "deposits.rs").write_text(_RULES)
    layers = tmp_path / SUMMARY_LAYERS_DIR
    layers.mkdir(parents=True)
    (layers / "deposits.txt").write_text("; matches nothing\n^no_such_crate::nothing$\n")
    capsys.readouterr()

    assert main(["check", str(tmp_path), "--component", "deposits"]) == 0, capsys.readouterr()
    capsys.readouterr()
    assert main(["prove", str(tmp_path), "--component", "deposits"]) == 0, capsys.readouterr()
    proved = capsys.readouterr().out
    assert "^no_such_crate::nothing$" in proved, proved
    out = _wait_for(_submitted(proved), capsys)
    assert out.startswith("FINISHED"), out
    assert "  VERIFIED rule_deposit_returns_its_amount" in out
    assert "  VIOLATED rule_deposit_returns_more" in out
    assert "  SANITY_FAILED rule_deposit_of_nothing" in out

    rerun = ["prove", str(tmp_path), "--component", "deposits", "--unsat-core"]
    assert main([*rerun, "--rule", "rule_deposit_of_nothing"]) == 0, capsys.readouterr()
    link = _submitted(capsys.readouterr().out)
    assert "  VERIFIED rule_deposit_of_nothing" in _wait_for(link, capsys)
    # Contradictory bounds are settled before the solver runs, so this job writes no core: what
    # is checked here is that the core map is read from the real job, and that the agent is told
    # why there is nothing in it. Reading a core is test_vacuity_prints_the_finding_and_the_core.
    assert main(["vacuity", link, "--rule", "rule_deposit_of_nothing"]) == 1
    assert "settled the rule without a solver" in capsys.readouterr().out


_SOROBAN_MANIFEST = """\
[package]
name = "counter"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib", "lib"]

[dependencies]
soroban-sdk = "22.0.8"

[profile.release]
opt-level = "z"
overflow-checks = true
"""

_SOROBAN_CONTRACT = """\
#![no_std]
use soroban_sdk::{contract, contractimpl, symbol_short, Env, Symbol};

const COUNT: Symbol = symbol_short!("COUNT");

#[contract]
pub struct Counter;

#[contractimpl]
impl Counter {
    pub fn increment(e: Env, by: u64) -> u64 {
        let count: u64 = e.storage().instance().get(&COUNT).unwrap_or(0);
        let new = count + by;
        e.storage().instance().set(&COUNT, &new);
        new
    }
}
"""

_SOROBAN_SHARED = """\
//! Structural invariants.

pub fn amount_is_small(amount: u64) -> bool {
    amount < 1000
}
"""

_SOROBAN_RULES = """\
use cvlr::{clog, cvlr_assert, cvlr_assume};
use cvlr::nondet::nondet;
use cvlr_soroban_derive::rule;
use soroban_sdk::{symbol_short, Env, Symbol};

use crate::certora::specs::invariants::amount_is_small;

const COUNT: Symbol = symbol_short!("COUNT");

#[rule]
pub fn rule_increment_adds_exactly_by(e: Env) {
    let by: u64 = nondet();
    cvlr_assume!(amount_is_small(by));
    let before: u64 = e.storage().instance().get(&COUNT).unwrap_or(0);
    cvlr_assume!(before < 1000);
    let returned = crate::Counter::increment(e.clone(), by);
    let after: u64 = e.storage().instance().get(&COUNT).unwrap_or(0);
    clog!(before, by, returned, after);
    cvlr_assert!(after == before + by);
    cvlr_assert!(returned == after);
}

#[rule]
pub fn rule_increment_adds_more(e: Env) {
    let by: u64 = nondet();
    cvlr_assume!(amount_is_small(by));
    let before: u64 = e.storage().instance().get(&COUNT).unwrap_or(0);
    cvlr_assume!(before < 1000);
    let returned = crate::Counter::increment(e, by);
    clog!(before, by, returned);
    cvlr_assert!(returned > before + by);
}

#[rule]
pub fn rule_increment_of_nothing(e: Env) {
    let by: u64 = nondet();
    cvlr_assume!(by < 10);
    cvlr_assume!(by > 20);
    let returned = crate::Counter::increment(e, by);
    cvlr_assert!(returned == by);
}
"""


@pytest.mark.expensive
@pytest.mark.skill_interface
def test_a_soroban_component_is_set_up_checked_proved_and_waited_for(tmp_path: Path, capsys):
    """The Soroban counterpart of the loop test above, against the cloud: the chain is detected
    from the manifest, one rule holds, one is violated, one is vacuous under the conf's sanity
    check."""
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    (tmp_path / "Cargo.toml").write_text(_SOROBAN_MANIFEST)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(_SOROBAN_CONTRACT)
    assert main(["setup", str(tmp_path), "--component", "counting", "--no-check"]) == 0, (
        capsys.readouterr()
    )
    out = capsys.readouterr().out
    assert "soroban" in out.lower(), out
    specs = tmp_path / SPECS_DIR
    (specs / "invariants.rs").write_text(_SOROBAN_SHARED)
    (specs / "counting.rs").write_text(_SOROBAN_RULES)
    reference = tmp_path / interface.REFERENCE_DIR
    assert all((reference / f).is_file() for f in interface.SOROBAN_REFERENCE_FILES)
    assert not (reference / "recipes").exists()

    assert main(["check", str(tmp_path), "--component", "counting"]) == 0, capsys.readouterr()
    capsys.readouterr()
    assert main(["prove", str(tmp_path), "--component", "counting"]) == 0, capsys.readouterr()
    out = _wait_for(_submitted(capsys.readouterr().out), capsys)
    assert out.startswith("FINISHED"), out
    assert "  VERIFIED rule_increment_adds_exactly_by" in out
    assert "  VIOLATED rule_increment_adds_more" in out
    assert "  SANITY_FAILED rule_increment_of_nothing" in out
