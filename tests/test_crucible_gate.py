"""Phase-1 gate for the Crucible backend: the build + dry-run *infrastructure*.

No LLM, no property authoring (those are later phases). This proves the plumbing
that phase 1 delivers, on the ``solana_vault`` Anchor scenario:

1. the descriptor loads and its ecosystem resolves to ``solana``;
2. ``validate_preconditions`` accepts the buildable workspace;
3. the shared Solana build step compiles the program to ``target/deploy/vault.so``;
4. a trivial hand-written Crucible harness passes ``crucible run … --dry-run``
   (compiles against the built ``.so`` + the program crate, and ``setup()`` runs
   one iteration).

Marked ``expensive``: it needs the Solana/Anchor toolchain + the ``crucible`` CLI,
and a local Crucible checkout for the harness's crate deps (``CRUCIBLE_REPO``).
It builds real sBPF + a fuzz harness, so it is slow.
Skips cleanly when any prerequisite is missing. Run with::

    CRUCIBLE_REPO=/path/to/crucible \
      .venv/bin/python -m pytest tests/test_crucible_gate.py -m expensive -q -s
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from composer.sandbox.command import run_local_command
from composer.rustapp.descriptor import DeliverableMode
from composer.rustapp.host import load_descriptor, load_module
from composer.rustapp.result import RustArtifact, RustFormalResult
from composer.rustapp.store import RustArtifactStore
from composer.spec.solana.build import build_program

pytestmark = [pytest.mark.expensive, pytest.mark.asyncio]

_SCENARIO = Path(__file__).parent.parent / "test_scenarios" / "solana_vault"
_PROGRAM = "vault"
_TEST = "invariant_vault"  # crucible test name == the feature gating it


def _crucible_repo() -> Path | None:
    raw = os.environ.get("CRUCIBLE_REPO")
    if not raw:
        return None
    repo = Path(raw)
    return repo if (repo / "crates" / "crucible-fuzzer").is_dir() else None


def _fuzz_cargo_toml(crucible_repo: Path) -> str:
    crates = crucible_repo / "crates"
    return f"""\
[package]
name = "vault_fuzz"
version = "0.1.0"
edition = "2021"

[workspace]

[dependencies]
crucible-fuzzer = {{ path = "{crates / 'crucible-fuzzer'}" }}
crucible-test-context = {{ path = "{crates / 'crucible-test-context'}" }}
anchor-lang = "1.0.1"
arbitrary = {{ version = "1", features = ["derive"] }}
ctrlc = "3.4"
libafl = {{ version = "0.15.1", features = ["std", "cli", "prelude"] }}
libafl_bolts = {{ version = "0.15.1", features = ["std"] }}
vault = {{ path = "../../programs/vault", features = ["no-entrypoint"] }}
solana-keypair = "3.0"
solana-pubkey = "3.0"
solana-signer = "3.0"

[[bin]]
name = "invariant_test"
path = "src/main.rs"

[features]
{_TEST} = []
"""


# A minimal hand-written harness (no LLM): load the built .so, initialize a vault,
# expose deposit/withdraw actions, and a trivial balance invariant. Enough for
# --dry-run (compile + one setup iteration).
_FUZZ_MAIN_RS = """\
use crucible_fuzzer::anchor_lang::system_program;
use crucible_fuzzer::*;
use solana_keypair::Keypair;
use solana_pubkey::Pubkey;
use solana_signer::Signer;
use std::rc::Rc;
use vault::*;

const INITIAL_BALANCE: u64 = 10_000_000_000;

#[derive(Clone)]
struct VaultFixture {
    ctx: TestContext,
    program_id: Pubkey,
    authority: Rc<Keypair>,
    vault_pda: Pubkey,
}

#[fuzz_fixture]
impl VaultFixture {
    pub fn setup() -> Self {
        let mut ctx = TestContext::new();
        let program_id = Pubkey::new_from_array(ID.to_bytes());
        ctx.add_program(&program_id, "../../target/deploy/vault.so").unwrap();

        let authority = Rc::new(Keypair::new());
        ctx.create_account()
            .pubkey(authority.pubkey())
            .lamports(INITIAL_BALANCE)
            .owner(system_program::ID)
            .create()
            .unwrap();

        let (vault_pda, _) =
            Pubkey::find_program_address(&[b"vault", authority.pubkey().as_ref()], &program_id);

        ctx.program(program_id)
            .call(instruction::Initialize {})
            .accounts(accounts::Initialize {
                vault: vault_pda,
                authority: authority.pubkey(),
                system_program: system_program::ID,
            })
            .signers(&[&*authority])
            .send()
            .unwrap();

        Self { ctx, program_id, authority, vault_pda }
    }

    pub fn action_deposit(&mut self, #[range(1..1_000_000)] amount: u64) -> bool {
        self.ctx
            .program(self.program_id)
            .call(instruction::Deposit { amount })
            .accounts(accounts::Deposit {
                vault: self.vault_pda,
                depositor: self.authority.pubkey(),
                system_program: system_program::ID,
            })
            .signers(&[&*self.authority])
            .send()
            .map(|o| o.is_success())
            .unwrap_or(false)
    }

    pub fn action_withdraw(&mut self, #[range(1..1_000_000)] amount: u64) -> bool {
        self.ctx
            .program(self.program_id)
            .call(instruction::Withdraw { amount })
            .accounts(accounts::Withdraw {
                vault: self.vault_pda,
                authority: self.authority.pubkey(),
            })
            .signers(&[&*self.authority])
            .send()
            .map(|o| o.is_success())
            .unwrap_or(false)
    }
}

#[invariant_test]
fn invariant_vault(fixture: &mut VaultFixture) {
    if let Ok(vault) = fixture.ctx.read_anchor_account::<VaultState>(&fixture.vault_pda) {
        fuzz_assert_le!(vault.balance, INITIAL_BALANCE, "recorded balance exceeds initial funds");
    }
}
"""


def _require(cond: bool, why: str) -> None:
    if not cond:
        pytest.skip(why)


async def test_crucible_phase1_build_and_dry_run():
    _require(_SCENARIO.is_dir(), f"scenario missing: {_SCENARIO}")
    _require(shutil.which("cargo-build-sbf") is not None, "cargo-build-sbf not on PATH")
    _require(shutil.which("crucible") is not None, "crucible CLI not on PATH")
    crucible_repo = _crucible_repo()
    _require(crucible_repo is not None, "set CRUCIBLE_REPO to a local crucible checkout")
    assert crucible_repo is not None  # for the type checker

    # 0. Descriptor + ecosystem + preconditions (cheap, no build).
    import composer.bind as _  # noqa: F401  (DI bootstrap)
    from composer.rustapp.host import load_descriptor, load_module, resolve_ecosystem

    module = load_module("crucible_app")
    descriptor = load_descriptor(module)
    assert descriptor.ecosystem == "solana"
    assert resolve_ecosystem(descriptor).name == "solana"
    err = module.validate_preconditions(json.dumps({"project_root": str(_SCENARIO)}))
    assert err is None, f"validate_preconditions rejected the scenario: {err}"

    # 1. Build the program to sBPF.
    built = await build_program(_SCENARIO, _PROGRAM, timeout_s=590)
    assert built.so_path.is_file(), built.so_path

    # 2. Materialize the trivial fuzz harness (crate deps resolved to the checkout).
    fuzz_dir = _SCENARIO / "fuzz" / _PROGRAM
    (fuzz_dir / "src").mkdir(parents=True, exist_ok=True)
    (fuzz_dir / "Cargo.toml").write_text(_fuzz_cargo_toml(crucible_repo))
    (fuzz_dir / "src" / "main.rs").write_text(_FUZZ_MAIN_RS)

    # 3. Dry-run: compiles the harness + runs setup() one iteration. Run from the
    #    project root (crucible resolves fuzz/<program>/ relative to cwd).
    res = await run_local_command(
        "crucible",
        ["run", _PROGRAM, _TEST, "--release", "--dry-run"],
        {},
        workdir=_SCENARIO,
        timeout_s=590,
    )
    assert res.exit_code == 0, (
        f"crucible --dry-run failed (exit {res.exit_code})\n"
        f"STDOUT:\n{res.stdout[-3000:]}\n\nSTDERR:\n{res.stderr[-3000:]}"
    )


# --- Phase 2: the deliverable model (CrucibleArtifactStore assembles the crate) ---

# The shared fixture is the phase-1 harness minus its test fn (the store composes
# fixture + per-component test sections).
_FIXTURE_SRC = _FUZZ_MAIN_RS.partition("#[invariant_test]")[0]

# One component's test section. Its fn name MUST equal its feature (c_deposit) —
# Crucible's #[invariant_test] macro gates main() by #[cfg(feature = "<fn name>")].
_DEPOSIT_SECTION = """\
#[invariant_test]
fn c_deposit(fixture: &mut VaultFixture) {
    if let Ok(vault) = fixture.ctx.read_anchor_account::<VaultState>(&fixture.vault_pda) {
        fuzz_assert_le!(vault.balance, INITIAL_BALANCE, "recorded balance exceeds initial funds");
    }
}
"""


async def test_crucible_phase2_store_assembles_crate():
    _require(_SCENARIO.is_dir(), f"scenario missing: {_SCENARIO}")
    _require(shutil.which("cargo-build-sbf") is not None, "cargo-build-sbf not on PATH")
    _require(shutil.which("crucible") is not None, "crucible CLI not on PATH")
    crucible_repo = _crucible_repo()
    _require(crucible_repo is not None, "set CRUCIBLE_REPO to a local crucible checkout")
    assert crucible_repo is not None

    built = await build_program(_SCENARIO, _PROGRAM, timeout_s=590)
    assert built.so_path.is_file(), built.so_path

    # A co-located EVM (autoprove) deliverable, to prove the layouts don't collide.
    evm_sentinel = _SCENARIO / "certora" / "specs" / "autospec_sentinel.spec"
    evm_sentinel.parent.mkdir(parents=True, exist_ok=True)
    evm_sentinel.write_text("// pretend EVM output\n")

    # The deliverable is now split (docs/rust-pure-app.md): the generic callout-mode store
    # writes the per-component metadata + returns the crate report link; the wheel's `finalize`
    # renders the one crate (fixture + sections) from the full result set. Drive both, exactly
    # as the pipeline does.
    module = load_module("crucible_app")
    layout = load_descriptor(module).artifact_layout
    store = RustArtifactStore(
        str(_SCENARIO), layout, deliverable_mode=DeliverableMode.CALLOUT, program=_PROGRAM
    )
    comp = RustArtifact("deposit", "harness", "rs")
    result = RustFormalResult(
        commentary="The recorded vault balance never exceeds the authority's initial funds.",
        artifact_text=_DEPOSIT_SECTION,
        units=[("balance bounded by initial funds", ["c_deposit"])],
    )
    store.write_properties(comp, [])
    main_rel = store.write_artifact(comp, result)  # metadata + the crate report link
    assert str(main_rel) == f"fuzz/{_PROGRAM}/src/main.rs"

    # finalize renders the crate (fixture + the delivered section, features from property_units).
    payload = json.dumps(
        {
            "program": _PROGRAM,
            "setup": _FIXTURE_SRC,
            "components": [
                {
                    "name": "deposit",
                    "delivered": True,
                    "artifact_text": _DEPOSIT_SECTION,
                    "property_units": [["balance bounded by initial funds", ["c_deposit"]]],
                }
            ],
        }
    )
    for rel, contents in json.loads(module.finalize(payload)).items():
        target = _SCENARIO / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)

    # Deliverable crate under fuzz/<program>/ ...
    assert (_SCENARIO / main_rel).is_file()
    assert (_SCENARIO / "fuzz" / _PROGRAM / "Cargo.toml").is_file()
    assert 'c_deposit = []' in (_SCENARIO / "fuzz" / _PROGRAM / "Cargo.toml").read_text()
    # ... metadata under certora/crucible/ (not under fuzz/, not under certora/specs/) ...
    props = _SCENARIO / "certora" / "crucible" / "properties"
    assert (props / "harness_deposit.commentary.md").is_file()
    assert (props / "harness_deposit.property_tests.json").is_file()
    # ... and the EVM output is untouched (coexistence).
    assert evm_sentinel.read_text() == "// pretend EVM output\n"

    # The assembled crate compiles and dry-runs (feature == the component's).
    res = await run_local_command(
        "crucible",
        ["run", _PROGRAM, "c_deposit", "--release", "--dry-run"],
        {},
        workdir=_SCENARIO,
        timeout_s=590,
    )
    assert res.exit_code == 0, (
        f"assembled-crate --dry-run failed (exit {res.exit_code})\n"
        f"STDOUT:\n{res.stdout[-3000:]}\n\nSTDERR:\n{res.stderr[-3000:]}"
    )
