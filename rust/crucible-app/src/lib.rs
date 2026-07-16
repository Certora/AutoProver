//! The **Crucible** application — AutoProver's Solana verification backend, which
//! authors [Crucible](https://github.com/asymmetric-research/crucible) fuzz harnesses
//! and gates them with the local `crucible` CLI. Pairs with the shared `solana`
//! ecosystem front half (see `docs/crucible-application.md`).
//!
//! A passive [`Backend`] (`docs/rust-backend-api.md`): it supplies the descriptor,
//! toolchain precondition checks, the per-invariant `units`, the authoring prompts
//! (fixture + tests), and the two gating callouts — `compile` (a `crucible … --dry-run`
//! build) and `validate` (one `crucible … --mode explore` fuzz run per unit) — which run
//! the toolchain through the shared `run_confined` launcher. Python owns the loop.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use autoprover_sdk::{
    run_confined, AppDescriptor, ArgDefault, ArgSpec, ArtifactLayout, AuthorInput, Backend,
    CommandOutput, CompileResult, CoreSlot, DeliverableMode, EventKind, Failure, FailureKind,
    PhaseSpec, Prompt, Sandbox, SandboxGrants, SetupSpec, Unit, ValidateOutcome, Verdict,
    WorkspacePrep,
};

// The crucible/solana/anchor stack a harness pins (docs/crucible-application.md §6.1). Hardcoded
// for now to the combination the installed toolchain matches (was Python's `CrucibleHarness`).
const ANCHOR_VERSION: &str = "1.0.1";
const SOLANA_VERSION: &str = "3.0";
const LIBAFL_VERSION: &str = "0.15.1";

/// The crucible checkout that resolves the harness crate's path deps (`$CRUCIBLE_REPO`). Read
/// here so crate rendering is fully wheel-owned; `validate_preconditions` guarantees it is set.
fn crucible_repo() -> Option<PathBuf> {
    std::env::var("CRUCIBLE_REPO").ok().map(PathBuf::from)
}

/// The `[dependencies]` block for the harness crate — the pinned crucible/solana/anchor stack
/// plus the program-under-test as a path dep (was `CrucibleDep::render_deps`).
fn crucible_deps(program: &str, repo: &Path) -> String {
    let crates = repo.join("crates");
    format!(
        "crucible-fuzzer = {{ path = \"{cf}\" }}\n\
         crucible-test-context = {{ path = \"{ctc}\" }}\n\
         anchor-lang = \"{ANCHOR_VERSION}\"\n\
         arbitrary = {{ version = \"1\", features = [\"derive\"] }}\n\
         ctrlc = \"3.4\"\n\
         libafl = {{ version = \"{LIBAFL_VERSION}\", features = [\"std\", \"cli\", \"prelude\"] }}\n\
         libafl_bolts = {{ version = \"{LIBAFL_VERSION}\", features = [\"std\"] }}\n\
         {program} = {{ path = \"../../programs/{program}\", features = [\"no-entrypoint\"] }}\n\
         solana-keypair = \"{SOLANA_VERSION}\"\n\
         solana-pubkey = \"{SOLANA_VERSION}\"\n\
         solana-signer = \"{SOLANA_VERSION}\"",
        cf = crates.join("crucible-fuzzer").display(),
        ctc = crates.join("crucible-test-context").display(),
    )
}

/// The harness `Cargo.toml`: one `[[bin]]` (`invariant_test`) selected by a per-component Cargo
/// feature. `features` are inert (`f = []`) — Crucible's macro self-gates `main()` by fn name ==
/// feature — so a build only needs the feature it selects declared (was `CrucibleHarness`).
fn render_cargo_toml(program: &str, repo: &Path, features: &[String]) -> String {
    let feats = if features.is_empty() {
        "# (no components yet)".to_string()
    } else {
        features.iter().map(|f| format!("{f} = []")).collect::<Vec<_>>().join("\n")
    };
    format!(
        "[package]\n\
         name = \"{program}_fuzz\"\n\
         version = \"0.1.0\"\n\
         edition = \"2021\"\n\
         \n\
         [workspace]\n\
         \n\
         [dependencies]\n\
         {deps}\n\
         \n\
         [[bin]]\n\
         name = \"invariant_test\"\n\
         path = \"src/main.rs\"\n\
         \n\
         [features]\n\
         {feats}\n",
        deps = crucible_deps(program, repo),
    )
}

/// The crate's on-disk files for a confined build: `src/main.rs` plus a `Cargo.toml` declaring
/// exactly `features` (materialized per run — with `serialize_toolchain` there is no concurrent
/// writer, so no shared-manifest race and no cumulative feature reservation).
fn crate_files(program: &str, main_rs: String, features: &[String]) -> BTreeMap<String, String> {
    let mut files = BTreeMap::new();
    files.insert(format!("fuzz/{program}/src/main.rs"), main_rs);
    if let Some(repo) = crucible_repo() {
        files.insert(
            format!("fuzz/{program}/Cargo.toml"),
            render_cargo_toml(program, &repo, features),
        );
    }
    files
}

/// The directory of `bin` on `$PATH` (for a read-only sandbox grant), if found.
fn which_dir(bin: &str) -> Option<String> {
    let path = std::env::var("PATH").ok()?;
    std::env::split_paths(&path)
        .find(|dir| dir.join(bin).is_file())
        .map(|dir| dir.display().to_string())
}
/// Backend-guidance prose injected into the property-extraction prompt. Crucible is
/// a fuzzer, so — like Foundry — refutations are valuable but universals can't be
/// *proven*; a handful of property kinds are a poor fit for sampling.
const CRUCIBLE_BACKEND_GUIDANCE: &str = "\
These properties will be checked with Crucible, a coverage-guided fuzzer for Solana \
programs. As a fuzzer it cannot *prove* universally quantified properties or invariants, \
but it approximates them well and any *refutation* (a fuzzing counterexample / crash) is \
extremely valuable. So state universal safety properties and invariants freely — do not \
restrict yourself because a fuzzer cannot definitively prove them.

A few categories are a poor fit and should be skipped: properties about off-chain events \
(key compromise, social engineering, oracle manipulation outside the modeled accounts), \
and pure hash-collision resistance (\"no two inputs ever collide\"), which sampling cannot \
refute. Arithmetic-overflow and type-level facts are worth stating: Rust overflow panics \
and Anchor constraint failures surface as crashes the fuzzer can find.";

/// The compiled binaries a Crucible run needs on `PATH`. Checked up-front so a run
/// fails fast with an actionable message rather than deep in the build phase.
const REQUIRED_BINARIES: &[&str] = &["crucible", "cargo-build-sbf", "anchor"];

/// Is `bin` an executable file reachable via `$PATH`? A pure filesystem scan — we do
/// not *run* anything here (validate_preconditions must stay a cheap, sync check).
fn on_path(bin: &str) -> bool {
    let Ok(path) = std::env::var("PATH") else {
        return false;
    };
    std::env::split_paths(&path).any(|dir| dir.join(bin).is_file())
}
/// Concise Crucible harness API reference injected into the authoring prompt (the
/// §7.5 static cheat-sheet; RAG over a `crucible_kb` is layered on at packaging).
const HARNESS_CHEAT_SHEET: &str = r#"
Crucible harness API (author a FIXTURE only — no test fns):

- Imports:
    use crucible_fuzzer::*;                          // TestContext, macros, fuzz_assert_*
    use crucible_fuzzer::anchor_lang::system_program;
    use <program>::*;                                // instruction, accounts, ID, state types
    use solana_keypair::Keypair; use solana_pubkey::Pubkey; use solana_signer::Signer;
    use std::rc::Rc;

- The fixture struct MUST be named `Fixture` and derive Clone; keypairs go in `Rc`:
    #[derive(Clone)]
    struct Fixture { ctx: TestContext, program_id: Pubkey, /* pdas, users (Rc<Keypair>) */ }

- #[fuzz_fixture] impl Fixture { ... } with:
    pub fn setup() -> Self {
        let mut ctx = TestContext::new();
        let program_id = Pubkey::new_from_array(ID.to_bytes());
        ctx.add_program(&program_id, "../../target/deploy/<program>.so").unwrap();
        // create funded accounts: ctx.create_account().pubkey(kp.pubkey())
        //     .lamports(N).owner(system_program::ID).create().unwrap();
        // derive PDAs: Pubkey::find_program_address(&[b"seed", key.as_ref()], &program_id)
        // run any init instruction (see calling convention). Panic on setup failure.
        Self { ctx, program_id, /* ... */ }
    }
    // one `action_<name>` per instruction; fuzzable args get #[range(lo..hi)]:
    pub fn action_<name>(&mut self, #[range(0..1_000_000)] amount: u64) -> bool {
        self.ctx.program(self.program_id)
            .call(instruction::<Name> { amount })
            .accounts(accounts::<Name> { /* fields */ })
            .signers(&[&*self.some_keypair])
            .send().map(|o| o.is_success()).unwrap_or(false)
    }

- Anchor path conventions (use the API FACTS below — do NOT guess these):
    * `use <program>::*;` brings the crate's generated items into scope. `<program>` is
      the crate id in the facts, NOT the `#[program] pub mod <name>` module name — the
      module name does NOT change these crate-root paths.
    * Instruction args struct: `instruction::<PascalName>` — snake_case `foo_bar` → `instruction::FooBar`.
    * Accounts struct: `accounts::<PascalName>` — same PascalCase as the instruction.
    * Program id: the `ID` constant; make a Pubkey via `Pubkey::new_from_array(ID.to_bytes())`.
- Read the program source (via the tools) to confirm exact field names, PDA seeds
  (binary vs string), and signer requirements — the API facts + model below are a summary.
- Output ONLY the fixture module source (imports + struct + #[fuzz_fixture] impl).
  Do NOT write `fn main`, `#[invariant_test]`, or `#[crucible_fuzz]` — those are added later.
"#;

/// A complete, compiling worked example (a DIFFERENT program — an escrow) so the author
/// can pattern-match the exact shape: imports, `Rc<Keypair>` accounts, `TestContext`
/// setup with a funded account + PDA + an init call, and `action_*` methods that build
/// `instruction::`/`accounts::` structs and `.send()`. Adapt it to the target program's
/// instructions/accounts/PDA seeds — do NOT copy it verbatim.
const EXAMPLE_FIXTURE: &str = r#"
EXAMPLE — a full, compiling fixture for a *different* program (an `escrow`). Study the
shape, then write the equivalent for THIS program:

```rust
use crucible_fuzzer::anchor_lang::system_program;
use crucible_fuzzer::*;
use solana_keypair::Keypair;
use solana_pubkey::Pubkey;
use solana_signer::Signer;
use std::rc::Rc;
use escrow::*;                                   // the crate id — NOT the `#[program] mod` name

#[derive(Clone)]
struct Fixture {                                 // MUST be named `Fixture`
    ctx: TestContext,
    program_id: Pubkey,
    depositor: Rc<Keypair>,
    vault_pda: Pubkey,
}

#[fuzz_fixture]
impl Fixture {
    pub fn setup() -> Self {
        let mut ctx = TestContext::new();
        let program_id = Pubkey::new_from_array(ID.to_bytes());
        ctx.add_program(&program_id, "../../target/deploy/escrow.so").unwrap();

        let depositor = Rc::new(Keypair::new());
        ctx.create_account().pubkey(depositor.pubkey())
            .lamports(10_000_000_000).owner(system_program::ID).create().unwrap();

        let (vault_pda, _) =
            Pubkey::find_program_address(&[b"vault", depositor.pubkey().as_ref()], &program_id);

        ctx.program(program_id)
            .call(instruction::Initialize {})     // args struct; `{}` when the ix has no args
            .accounts(accounts::Initialize {
                vault: vault_pda,
                depositor: depositor.pubkey(),
                system_program: system_program::ID,
            })
            .signers(&[&*depositor])
            .send().unwrap();                     // panic in setup() if init fails

        Self { ctx, program_id, depositor, vault_pda }
    }

    pub fn action_deposit(&mut self, #[range(1..1_000_000)] amount: u64) -> bool {
        self.ctx.program(self.program_id)
            .call(instruction::Deposit { amount })
            .accounts(accounts::Deposit {
                vault: self.vault_pda,
                depositor: self.depositor.pubkey(),
                system_program: system_program::ID,
            })
            .signers(&[&*self.depositor])
            .send().map(|o| o.is_success()).unwrap_or(false)
    }
}
```
"#;

/// A `#[invariant_test]` probe appended (by the host) only to validate the fixture
/// via `crucible run … --dry-run`: it must compile and `setup()` must run once.
const PROBE_FN: &str = "\n\n#[invariant_test]\nfn c_probe(fixture: &mut Fixture) {\n    let _ = fixture;\n}\n";
/// Extract just the rustc error diagnostics from a (possibly long) cargo build log so
/// the revise prompt leads with the actual errors instead of pages of "Compiling …".
/// Keeps each `error[..]`/`error:` block with its `-->`/`|`/`=` context; drops warnings
/// and progress. Returns "" if there are no error lines.
fn compiler_diagnostics(out: &str) -> String {
    let mut kept: Vec<&str> = Vec::new();
    let mut in_err = false;
    for line in out.lines() {
        let t = line.trim_start();
        if t.starts_with("error[") || t.starts_with("error:") {
            in_err = true;
            kept.push(line);
        } else if in_err {
            if line.is_empty()
                || line.starts_with(' ')
                || t.starts_with("-->")
                || t.starts_with('|')
                || t.starts_with('=')
            {
                kept.push(line);
            } else {
                in_err = false;
            }
        }
    }
    while kept.last().is_some_and(|l| l.trim().is_empty()) {
        kept.pop();
    }
    let joined = kept.join("\n");
    // Cap so a pathological error count can't blow up the prompt.
    joined[..joined.len().min(4000)].to_string()
}

/// snake_case → PascalCase — Anchor's `instruction`/`accounts` struct naming.
fn to_pascal(snake: &str) -> String {
    snake
        .split('_')
        .filter(|s| !s.is_empty())
        .map(|w| {
            let mut c = w.chars();
            match c.next() {
                Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
                None => String::new(),
            }
        })
        .collect()
}

/// A concise, high-signal "API facts" block mined from the analyzed model so the author
/// need not dig through the full JSON (or rediscover Anchor names by exploring): the crate
/// id, declare_id, state types, and each instruction's snake→Pascal name + args + accounts.
/// Returns "" if the model shape isn't recognized.
fn api_facts(analyzed: &serde_json::Value, program: &str) -> String {
    let components = match analyzed.get("components").and_then(|c| c.as_array()) {
        Some(c) => c,
        None => return String::new(),
    };
    let is_prog = |c: &&serde_json::Value| c.get("instructions").is_some_and(|i| i.is_array());
    let prog = components
        .iter()
        .find(|c| {
            is_prog(c)
                && (c.get("program_identifier").and_then(|v| v.as_str()) == Some(program)
                    || c.get("name").and_then(|v| v.as_str()) == Some(program))
        })
        .or_else(|| components.iter().find(is_prog));
    let prog = match prog {
        Some(p) => p,
        None => return String::new(),
    };

    let str_of = |v: Option<&serde_json::Value>| v.and_then(|x| x.as_str()).unwrap_or("?").to_string();
    let mut out = String::from("PROGRAM API FACTS (use these EXACT names — do not guess):\n");
    // The crate id is the harness's actual Cargo dependency name — the program name
    // (== CrucibleDep's crate), NOT the analysis's `program_identifier`, which may be the
    // `#[program] pub mod` name and would mis-resolve `use <id>::*`.
    out.push_str(&format!("  crate id (for `use <id>::*`): {program}\n"));
    let analysis_id = str_of(prog.get("program_identifier"));
    if analysis_id != program && analysis_id != "?" {
        out.push_str(&format!(
            "  (note: `#[program] pub mod {analysis_id}` is the module name — it does NOT change \
             the crate-root paths `{program}::instruction::*` / `{program}::accounts::*`)\n"
        ));
    }
    out.push_str(&format!(
        "  declare_id / program id: {}\n",
        prog.get("program_id").and_then(|v| v.as_str()).unwrap_or("(not declared)")
    ));
    if let Some(types) = prog.get("account_types").and_then(|v| v.as_array()) {
        let names: Vec<String> = types.iter().filter_map(|t| t.as_str().map(String::from)).collect();
        if !names.is_empty() {
            out.push_str(&format!("  state/account types: {}\n", names.join("; ")));
        }
    }
    out.push_str("  instructions (snake handler → Anchor Pascal structs):\n");
    if let Some(ixs) = prog.get("instructions").and_then(|v| v.as_array()) {
        for ix in ixs {
            let name = str_of(ix.get("name"));
            let pascal = to_pascal(&name);
            let args: Vec<String> = ix
                .get("args")
                .and_then(|v| v.as_array())
                .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
                .unwrap_or_default();
            let accts: Vec<String> = ix
                .get("accounts")
                .and_then(|v| v.as_array())
                .map(|a| {
                    a.iter()
                        .filter_map(|x| x.get("name").and_then(|n| n.as_str()).map(String::from))
                        .collect()
                })
                .unwrap_or_default();
            out.push_str(&format!(
                "    - {name} → instruction::{pascal}, accounts::{pascal}; args: [{}]; accounts: [{}]\n",
                args.join(", "),
                accts.join(", "),
            ));
        }
    }
    out
}

/// The "previous attempt failed, fix it" suffix shared by both authoring loops: lead with
/// the *extracted* compiler errors, then the prior source, then a trimmed raw-log tail.
fn revise_suffix(prev_src: &str, raw: &str) -> String {
    let errors_block = match compiler_diagnostics(raw) {
        d if d.is_empty() => String::new(),
        d => format!("COMPILER ERRORS to fix (extracted):\n{d}\n\n"),
    };
    let tail = &raw[raw.len().saturating_sub(2500)..];
    format!(
        "\n\nThe previous attempt FAILED to build / dry-run. Fix it.\n{errors_block}\
         Prior source:\n```rust\n{prev_src}\n```\n\nRaw build/dry-run output (tail):\n{tail}"
    )
}

/// The "previous attempt was rejected by the reviewer" suffix. Unlike `revise_suffix`, the
/// draft *compiled* — so frame it as review feedback to address, not compiler errors to fix
/// (otherwise the author thrashes hunting for build errors that do not exist).
fn judge_revise_suffix(prev_src: &str, feedback: &str) -> String {
    format!(
        "\n\nYour previous suite COMPILED but a security reviewer REJECTED it — this is NOT a \
         build error. Revise the tests to address the review feedback below (each point names a \
         criterion and a concrete fix):\n{feedback}\n\n\
         Prior source:\n```rust\n{prev_src}\n```"
    )
}

/// Dispatch the re-author suffix on which gate rejected the prior attempt.
fn revise_for(f: &Failure) -> String {
    match f.kind {
        FailureKind::Judge => judge_revise_suffix(&f.draft, &f.errors),
        FailureKind::Compile => revise_suffix(&f.draft, &f.errors),
    }
}
const TEST_CHEAT_SHEET: &str = r#"
Write ONE Crucible test function (no fixture — it already exists as `Fixture`):

- `#[invariant_test] fn <name>(fixture: &mut Fixture) { ... }` runs AFTER EACH
  fuzzed action — use it for state invariants (conservation, solvency, bounds).
- `#[crucible_fuzz] fn <name>(fixture: &mut Fixture, #[range(..)] x: u64) { ... }`
  runs single random operations — use it for per-instruction properties.
- Assert against ON-CHAIN state, not local mirrors:
    let acct = fixture.ctx.read_anchor_account::<SomeState>(&fixture.some_pda).unwrap();
    fuzz_assert_le!(acct.balance, cap, "message");   // fuzz_assert_{eq,ne,lt,le,gt,ge}
- Read RAW lamports / data / owner (bypassing Anchor deserialization) via
  `read_account` / `get_account`, which return `Result<Account>` — unwrap/`?` FIRST,
  THEN the field (there is NO `get_account_lamports`, and `Result` has no `.lamports`):
    let lamports = fixture.ctx.read_account(&fixture.some_pda).unwrap().lamports;
  Existence check: `fixture.ctx.account_exists(&fixture.some_pda)` (returns bool).
- TRANSACTION FEE — do NOT assert an EXACT lamport delta on a signer. The fee payer (the
  FIRST signer of the `action_*`'s `.send()`, e.g. the `depositor`/`authority`) is debited the
  tx fee (~5000 lamports/signature) ON TOP OF whatever lamports the instruction moves, so
  `depositor_before - amount == depositor_after` is WRONG (off by the fee). Assert exact deltas
  on NON-signer accounts (e.g. the vault PDA, which only receives the transfer); for a signer,
  subtract the fee or assert a bound (`<=`) instead of `==`.
- Drive state via the fixture's existing `action_*` methods; do not re-`send()`
  instructions yourself unless necessary.
- Return ONLY the annotated test fn. It MUST be named exactly `{feature}`.
"#;

/// Reviewer persona for the optional `judge_prompt` turn — the Crucible peer of Foundry's
/// `foundry_property_judge_system_prompt.j2`. The source-exploration / RAG / memory tools are
/// injected by the host, so this only sets the stance.
const CRUCIBLE_JUDGE_SYSTEM: &str = "\
You are a senior Solana security engineer reviewing a colleague's Crucible fuzz-test suite, \
written to demonstrate a set of security properties of a Solana program. A Crucible test is an \
experiment the fuzzer drives: it calls the fixture's `action_*` methods in arbitrary sequences \
and, after each, runs the `#[invariant_test]`/`#[crucible_fuzz]` assertions. Judge whether a GREEN \
campaign is real evidence for each property — i.e. under which program implementations this suite \
would actually FAIL. Use the source-exploration tools to read the program's Rust source and the \
fixture before asserting anything about behavior; record durable findings with the memory tool.";

/// The judge's evaluation criteria — modeled on Foundry's `foundry_feedback_prompt.j2` but
/// retargeted to Crucible/LiteSVM fuzzing: the load-bearing axis is *reachability* (can the
/// fuzzer, through the fixture's `action_*` methods, drive a state where the property could
/// fail?) rather than Solidity cheatcode fidelity. Inserted verbatim, so it holds the literal
/// JSON of the output contract (no format-string escaping).
const CRUCIBLE_JUDGE_GUIDANCE: &str = r#"Read the test functions AND the program source (via the
tools); several criteria require comparing the tests' claims against what the program actually
does. Then evaluate against the criteria below. The examples show the SHAPE of each defect; they
are not exhaustive — a defect matching a criterion is in scope even if it resembles no example.

## Criterion 1 — Vacuous / tautological assertions
Flag assertions that hold regardless of the program's behavior:
- bounds guaranteed by the type or by arithmetic (`fuzz_assert_ge!(x, 0)` on a `u64`; a bound a
  checked subtraction already guarantees);
- asserting a value the test/fixture itself just wrote, via a path that bypasses the logic under
  test (seed an account field, then read the same field back);
- degenerate operands: solvency (`assets >= liabilities`) where liabilities are zero all campaign;
  "unauthorized caller is rejected" where no one was ever authorized; a preservation invariant
  that only ever runs in the initial state (holds by init, not by preservation).
An assertion is tautological IFF it holds under every program implementation.

## Criterion 2 — Claim / assertion gap
For each test, compare (a) the property it claims (name, comments) against (b) the strongest fact
its assertions actually establish. Flag any test where (b) is materially weaker. Typical shapes:
- asserting an upstream precursor while the claimed consequence lives only in comments (assert a
  field was stored, when the property is that an unauthorized withdraw is *rejected*; assert an
  authority was rotated, when the property is that the old authority's calls now fail);
- comments narrate an attack the executable test never drives;
- the property specifies a sequence/interleaving (a stale read, a specific instruction order) but
  the test never constructs it through the `action_*` sequence.
Prose proves nothing; a test's evidentiary value is exactly its assertions on on-chain state.

## Criterion 3 — Reachability: can the fuzzer reach a state where the property could FAIL?
This is the load-bearing criterion for a fuzzing backend — an invariant is only as strong as the
states the campaign can drive it into. Audit the fixture + the `action_*` set the property leans on:
- **Missing actions**: no `action_*` exists for the instruction(s) the property is about, so the
  campaign never drives the relevant transition and the invariant is only ever checked over states
  that trivially satisfy it.
- **Collapsed input domains**: `#[range(lo..hi)]` narrowed to near-constants, a range that excludes
  the violating region, or a fuzzed arg that influences no assertion.
- **Actions that never succeed**: an `action_*` whose `.send()` fails on most inputs (wrong
  accounts / signers / funding) returns `false` and leaves state near-initial — a green invariant
  over states never reached is evidence of nothing.
- **Degenerate fixtures**: a single actor/account for a multi-party property; zero balances or
  empty state; identity-element params (a fee of 0, a rate of 1) that cannot distinguish a correct
  program from a plausible wrong one.
Precondition seeding is fine; substituting for the enforced logic is not (see C4).

## Criterion 4 — Real execution vs bypassed logic
Crucible runs the real program `.so` in LiteSVM, so fidelity is usually high — but check the
fixture/test does not bypass the logic under test:
- seeding an account's state directly (`create_account` / raw data) instead of driving the
  program's instruction, when the property is about that instruction *computing or enforcing* that
  state (injecting state the subject must compute assumes the conclusion; injecting state it merely
  consumes is legitimate setup);
- reads must go through `read_anchor_account::<State>(&pda)` against the PDA the program actually
  writes — not a local Rust mirror the test maintains alongside the chain.

## Criterion 5 — Oracle independence
Flag tests whose expected value is the program's own logic fed back to itself (the same formula
transcribed into the test; the same derivation/hash the program uses). Prefer anchors knowable
without the implementation: conservation identities (sum of balances constant), boundary values,
round-trips, monotonicity between concrete states, input/output pairs computed offline.

## Criterion 6 — Pass/fail directionality (especially attack-vector properties)
For each test ask: if the property regressed tomorrow, would this test FAIL?
- Attack-vector properties ("the exploit cannot occur"): a campaign that stays GREEN while the
  attack succeeds has inverted semantics. A green suite must never certify a live vulnerability. If
  the author genuinely found the attack possible, the correct artifact is an explicit finding plus
  a test marked as a known-vulnerability demonstration — never a silently-passing test.
- "Must be rejected" checks: confirm the action fails FOR THE RIGHT REASON. On Solana an
  instruction can fail for reasons unrelated to the property (missing signer, unfunded account,
  wrong PDA, arithmetic overflow in setup). Assert the specific failure — a custom program error,
  or that the guarded on-chain state is unchanged — and confirm a CONTROL action SUCCEEDS when the
  guarded condition is absent. `action_*` returning `false` is not, by itself, evidence the
  program's own check rejected the call.

## Criterion 7 — Fuzz / invariant mechanics
- Right macro for the property: `#[invariant_test]` runs after EACH action (preservation
  invariants — conservation, solvency, monotonic state); `#[crucible_fuzz]` runs one random op
  (a per-instruction property). Flag a single-shot `#[crucible_fuzz]` used for what is really a
  preservation invariant, or vice-versa.
- Assertions must read committed on-chain state via `read_anchor_account`; PDAs/addresses must
  match what the program writes.
- Real-execution costs (a false-oracle trap): under LiteSVM the transaction fee payer — the
  FIRST signer of an `action_*`'s `.send()` — is debited the tx fee (~5000 lamports/signature)
  on top of any lamports the instruction moves, and accounts owe rent-exemption. An EXACT
  lamport-delta assertion on a signer/fee-payer that ignores the fee (e.g.
  `depositor_before - amount == depositor_after`) is a false oracle: it FAILS on a correct
  program. Flag it — exact-delta assertions belong on non-signer accounts (the receiving PDA),
  or must subtract the fee / assert a bound.
- Ties back to C3: confirm the reachable state space actually includes the property's danger
  region under the available `action_*` sequences.

## Criterion 8 — Coverage and redundancy
- Every listed property must have a correctly-named test fn. A property "addressed" only by tests
  failing C1–C3 is NOT covered — say so explicitly and reject.
- Evaluate any declared skip critically: Crucible can drive real instructions, arbitrary account
  state, multiple signers, and fuzz — few properties of on-chain logic are genuinely untestable.
  Sketch the test you believe possible before accepting a skip.
- Flag redundant tests (different names, same fact about the same state); note any property left
  uncovered as a result.

Discard low-value feedback: style/naming/organization; compute-unit or performance quibbles;
"brittleness" (tests are tied to the implementation they verify); the exact bounds/magnitudes
chosen unless they make a test degenerate under C3; demands for tests beyond the listed properties.
The goal is to rule OUT low-quality tests, not to demonstrate thoroughness by volume — a short list
of load-bearing defects, each tied to a criterion and a concrete fix, beats an exhaustive
enumeration.

Do NOT assert Crucible / LiteSVM / Anchor semantics you have not verified (from memory or the
docs). Before claiming a test is wrong about the program's behavior, read the relevant source and
cite what it does. Never contradict prior-round feedback unless you are ~95% certain it was in error.

Output contract: use tools and reason as needed, but your FINAL message MUST be a single JSON
object and NOTHING else:
  {"accept": true,  "feedback": ""}
  {"accept": false, "feedback": "<load-bearing defects — each tied to a criterion and a concrete fix>"}
Reject (set accept:false) if any listed property is uncovered, or is covered only by tests failing
Criteria 1–3."#;

/// Did the build fail (as opposed to the harness building and fuzzing)?
fn is_build_error(out: &str) -> bool {
    out.contains("could not compile") || out.contains("error[") || out.contains("Build failed")
}

/// Pull the human-readable finding out of a `crucible run` log so a `BAD` verdict explains
/// itself. Crucible prints `[FUZZ_FINDING] crash:<id> reproduces:<bool> summary:<msg>`, where
/// `<msg>` is the failed `fuzz_assert_*` message (with the actual vs expected values). Returns
/// `crash <id>: <msg>`, or the raw marker line if the summary can't be parsed, or None.
fn finding_detail(out: &str) -> Option<String> {
    let line = out.lines().find(|l| l.contains("[FUZZ_FINDING]"))?.trim();
    match line.split_once("summary:") {
        Some((head, summary)) if !summary.trim().is_empty() => {
            let crash =
                head.split_whitespace().find_map(|t| t.strip_prefix("crash:")).unwrap_or("?");
            Some(format!("crash {crash}: {}", summary.trim()))
        }
        _ => Some(line.to_string()),
    }
}

// ===========================================================================
// Backend glue: small pure helpers shared by the callouts.
// ===========================================================================

/// A string field from the input's `context` blob (e.g. the shared fixture source).
fn ctx_str(input: &AuthorInput, key: &str) -> String {
    input.context.get(key).and_then(|v| v.as_str()).unwrap_or_default().to_string()
}

/// A u64 field from the input's `context` blob, with a default.
fn ctx_u64(input: &AuthorInput, key: &str, default: u64) -> u64 {
    input.context.get(key).and_then(|v| v.as_u64()).unwrap_or(default)
}

/// The compiler errors to hand back to the model — extracted diagnostics, else a raw tail.
fn build_errors(out: &CommandOutput) -> String {
    let combined = format!("{}\n{}", out.stdout, out.stderr);
    let d = compiler_diagnostics(&combined);
    if d.is_empty() {
        combined[combined.len().saturating_sub(2000)..].to_string()
    } else {
        d
    }
}

// ===========================================================================
// The backend.
// ===========================================================================

struct CrucibleApp;

impl Backend for CrucibleApp {
    fn descriptor(&self) -> AppDescriptor {
        AppDescriptor {
            name: "crucible".to_string(),
            header_text: "Crucible — Solana fuzzing backend | AutoProver".to_string(),
            // Selects the shared `solana` ecosystem front half (system model + prompts).
            ecosystem: "solana".to_string(),
            backend_tag: "crucible".to_string(),
            backend_guidance: CRUCIBLE_BACKEND_GUIDANCE.to_string(),
            analysis_key: "crucible-solana-analysis".to_string(),
            phases: vec![
                // UI-only phase: discover the design doc when one isn't supplied (§host).
                PhaseSpec { key: "discover_design_doc".into(), label: "Design Doc Discovery".into(), order: 0, core_slot: None },
                PhaseSpec { key: "analysis".into(), label: "System Analysis".into(), order: 1, core_slot: Some(CoreSlot::Analysis) },
                PhaseSpec { key: "extraction".into(), label: "Property Extraction".into(), order: 2, core_slot: Some(CoreSlot::Extraction) },
                // UI-only phase: build the program `.so` + IDL + shared fixture (§5.1).
                PhaseSpec { key: "build_harness".into(), label: "Build Harness".into(), order: 3, core_slot: None },
                PhaseSpec { key: "formalization".into(), label: "Harness Authoring".into(), order: 4, core_slot: Some(CoreSlot::Formalization) },
                PhaseSpec { key: "report".into(), label: "Report".into(), order: 5, core_slot: Some(CoreSlot::Report) },
            ],
            // Only `--fuzz-timeout` is wired through to `crucible run`. Other tuning knobs
            // (parallel cores, stateful mode, a version pin) are deliberately omitted until
            // they're actually threaded to the fuzz command — an inert flag is worse than none.
            args: vec![
                ArgSpec {
                    flag: "--fuzz-timeout".to_string(),
                    help: "Per-test fuzzing budget in seconds (`crucible run --timeout`).".to_string(),
                    default: ArgDefault::Int { value: Some(60) },
                    required: false,
                },
            ],
            rag_db_default: Some("crucible_kb".to_string()),
            event_kinds: vec![
                EventKind::log("fuzz_pulse", "Fuzzing"),
                EventKind::log("fuzz_finding", "Finding"),
                EventKind::log("build_output", "Build"),
                // The reviewer (judge) turn's accept/reject on a compiled test suite.
                EventKind::log("judge", "Review"),
                // The per-invariant verdict — surfaced as a persistent callout + toast.
                EventKind::notice("verdict", "Verdict"),
            ],
            // Metadata (properties.json / commentary / property→tests map) lands under
            // `certora/crucible/` — the split Foundry uses — while the crate deliverable is the
            // one file under `fuzz/<program>/` (deliverable_primary + the finalize render).
            artifact_layout: ArtifactLayout {
                deliverable_dir: "certora/crucible".into(),
                internal_dir: ".certora_internal/crucible".into(),
                report_dir: "certora/crucible/reports".into(),
                artifact_dir: "certora/crucible/harnesses".into(),
                artifact_prefix: "harness".into(),
                artifact_extension: "rs".into(),
                property_suffix: "property_tests".into(),
                deliverable_primary: Some("fuzz/{program}/src/main.rs".into()),
            },
            // A shared fixture authored once (the setup step), one crate assembled by finalize
            // (callout), all toolchain runs serialized on the one crate/target, confined by
            // default (untrusted native builds), and "instruction" as the unit noun.
            setup: Some(SetupSpec {
                phase_key: "build_harness".into(),
                label: "Build Harness".into(),
                context_key: "fixture".into(),
            }),
            deliverable_mode: DeliverableMode::Callout,
            serialize_toolchain: true,
            confine_by_default: true,
            component_noun: Some("instruction".into()),
        }
    }

    fn validate_preconditions(&self, args: &serde_json::Value) -> Result<(), String> {
        let mut problems: Vec<String> = Vec::new();

        let missing: Vec<&str> = REQUIRED_BINARIES
            .iter()
            .copied()
            .filter(|b| !on_path(b))
            .collect();
        if !missing.is_empty() {
            problems.push(format!(
                "required tool(s) not found on PATH: {}. Install the Solana toolchain \
                 (solana-cli / cargo-build-sbf), Anchor, and the crucible CLI \
                 (`cargo install --path crates/crucible-fuzz-cli`).",
                missing.join(", ")
            ));
        }

        // The target must be a buildable Cargo/Anchor workspace (cf. foundry's
        // foundry.toml precondition). We only check structure here; the actual build
        // happens in the build phase.
        if let Some(root) = args.get("project_root").and_then(|v| v.as_str()) {
            if !Path::new(root).join("Cargo.toml").is_file() {
                problems.push(format!(
                    "{root}/Cargo.toml not found — Crucible needs a buildable Cargo/Anchor \
                     workspace with the program under programs/<name>/."
                ));
            }
        } else {
            problems.push("no project_root in args".to_string());
        }

        // The crucible checkout resolves the harness crate's path deps (§6.1). Was
        // `resolve_crucible_repo` in Python; now the wheel owns it (it renders the deps).
        match std::env::var("CRUCIBLE_REPO") {
            Ok(repo) if Path::new(&repo).join("crates/crucible-fuzzer").is_dir() => {}
            Ok(repo) => problems.push(format!(
                "$CRUCIBLE_REPO={repo} has no crates/crucible-fuzzer — set it to a local crucible \
                 clone (the harness deps resolve against it)."
            )),
            Err(_) => problems.push(
                "$CRUCIBLE_REPO is not set — point it at a local crucible clone (must contain \
                 crates/crucible-fuzzer); the harness crate's path deps resolve against it."
                    .to_string(),
            ),
        }

        if problems.is_empty() {
            Ok(())
        } else {
            Err(problems.join("\n"))
        }
    }

    fn units(&self, input: &AuthorInput) -> Vec<Unit> {
        // The setup fixture has no report units; a component has one per property.
        if input.kind == "setup" {
            return Vec::new();
        }
        input
            .props
            .iter()
            .enumerate()
            .map(|(i, p)| {
                let slug = if p.slug.is_empty() { format!("inv{i}") } else { p.slug.clone() };
                Unit { property: p.title.clone(), unit: format!("c_{slug}") }
            })
            .collect()
    }

    fn author_prompt(&self, input: &AuthorInput, failure: Option<&Failure>) -> Prompt {
        let program = &input.program;
        let instruction = if input.kind == "setup" {
            // Author the shared fixture from the analyzed model (carried in `component`).
            let analyzed = &input.component;
            let model =
                serde_json::to_string_pretty(analyzed).unwrap_or_else(|_| analyzed.to_string());
            let mut task = format!(
                "Author a Crucible fuzz-harness FIXTURE (only) for the Solana program `{program}`.\n\
                 {cheat}\n\n{example}\n\n{facts}\n\
                 Full analyzed system model (accounts, PDAs, authorities, requirements):\n{model}\n\n\
                 Use the source-exploration tools to read the program's Rust source for exact \
                 signatures. Return the complete fixture module source as your final answer.",
                cheat = HARNESS_CHEAT_SHEET.replace("<program>", program),
                example = EXAMPLE_FIXTURE,
                facts = api_facts(analyzed, program),
                model = model,
            );
            if let Some(f) = failure {
                task.push_str(&revise_for(f));
            }
            task
        } else {
            // Author one #[invariant_test]/#[crucible_fuzz] fn per unit, against the fixture.
            let listed: Vec<String> = self
                .units(input)
                .into_iter()
                .zip(input.props.iter())
                .map(|(u, p)| format!("- fn `{}` — [{}] {}: {}", u.unit, p.sort, p.title, p.description))
                .collect();
            let component = serde_json::to_string_pretty(&input.component)
                .unwrap_or_else(|_| input.component.to_string());
            let fixture = ctx_str(input, "fixture");
            let mut task = format!(
                "Author {n} Crucible test function(s) for the `{program}` program — ONE per \
                 property below. Each function MUST be named EXACTLY as shown (the name is its \
                 fuzz-target selector) and check its property:\n{listed}\n\n\
                 Each must hold after ANY sequence of actions the fuzzer drives — prefer an \
                 `#[invariant_test]` that reads on-chain state and asserts the property.\n\n\
                 Program API (drive instructions via the fixture's `action_*` methods):\n{component}\n\n\
                 {cheat}\n\n\
                 The shared fixture is ALREADY defined (do not redefine it); use `Fixture` and its \
                 `action_*` methods. Fixture source for reference:\n```rust\n{fixture}\n```",
                n = listed.len(),
                listed = listed.join("\n"),
                component = component,
                cheat = TEST_CHEAT_SHEET,
            );
            if let Some(f) = failure {
                task.push_str(&revise_for(f));
            }
            task
        };
        Prompt { system: None, instruction }
    }

    fn judge_prompt(&self, input: &AuthorInput, spec: &str) -> Option<Prompt> {
        // The shared fixture is scaffolding, not test evidence — the compile/dry-run gate
        // already vets it, and there is no property to judge it against. Judge only the
        // per-component test suites (the peer of Foundry's feedback judge).
        if input.kind == "setup" {
            return None;
        }
        let program = &input.program;
        let listed: Vec<String> = self
            .units(input)
            .into_iter()
            .zip(input.props.iter())
            .map(|(u, p)| format!("- fn `{}` — [{}] {}: {}", u.unit, p.sort, p.title, p.description))
            .collect();
        let component = serde_json::to_string_pretty(&input.component)
            .unwrap_or_else(|_| input.component.to_string());
        let fixture = ctx_str(input, "fixture");
        let instruction = format!(
            "Evaluate the Crucible fuzz-test suite below for the Solana program `{program}`. \
             Decide, per property, whether a PASSING fuzz campaign is real evidence — not merely \
             that the tests compile (the build gate already ensures that).\n\n\
             Properties this suite must demonstrate (one test fn each, named EXACTLY as shown):\n\
             {listed}\n\n\
             Program API (instructions / accounts / state — driven via the fixture's `action_*` \
             methods):\n{component}\n\n\
             The shared fixture the tests build on (already compiled — do not re-review it, but \
             use it to judge what states the `action_*` methods can reach):\n```rust\n{fixture}\n```\n\n\
             Test suite under review:\n```rust\n{spec}\n```\n\n{guidance}",
            listed = listed.join("\n"),
            guidance = CRUCIBLE_JUDGE_GUIDANCE,
        );
        Some(Prompt { system: Some(CRUCIBLE_JUDGE_SYSTEM.to_string()), instruction })
    }

    fn compile(
        &self,
        input: &AuthorInput,
        spec: &str,
        workdir: &Path,
        sandbox: &Sandbox,
    ) -> CompileResult {
        let program = &input.program;
        // Setup: dry-run the fixture behind a probe test. Component: fixture + the authored
        // tests, dry-run behind the first unit's feature (all fns compile regardless of which
        // feature gates `main`, so one build proves the whole harness compiles).
        let (main_rs, feature) = if input.kind == "setup" {
            (format!("{spec}{PROBE_FN}"), "c_probe".to_string())
        } else {
            let fixture = ctx_str(input, "fixture");
            let feature = self
                .units(input)
                .into_iter()
                .next()
                .map(|u| u.unit)
                .unwrap_or_else(|| "c_probe".to_string());
            (format!("{fixture}\n\n{spec}"), feature)
        };
        let files = crate_files(program, main_rs, std::slice::from_ref(&feature));
        let args = vec![
            "run".to_string(),
            program.clone(),
            feature,
            "--release".to_string(),
            "--dry-run".to_string(),
        ];
        match run_confined(sandbox, "crucible", &args, &files, workdir) {
            Ok(out)
                if out.exit_code == 0
                    && !is_build_error(&format!("{}\n{}", out.stdout, out.stderr)) =>
            {
                CompileResult::Ok
            }
            Ok(out) => CompileResult::Failed { errors: build_errors(&out) },
            Err(e) => CompileResult::Failed { errors: e },
        }
    }

    fn validate(
        &self,
        input: &AuthorInput,
        spec: &str,
        unit: &str,
        workdir: &Path,
        sandbox: &Sandbox,
    ) -> ValidateOutcome {
        let program = &input.program;
        let fixture = ctx_str(input, "fixture");
        let timeout = ctx_u64(input, "fuzz_timeout", 30);
        let files = crate_files(program, format!("{fixture}\n\n{spec}"), std::slice::from_ref(&unit.to_string()));
        let args = vec![
            "run".to_string(),
            program.clone(),
            unit.to_string(),
            "--release".to_string(),
            "--mode".to_string(),
            "explore".to_string(),
            "--timeout".to_string(),
            timeout.to_string(),
        ];
        let verdict = |o: &str| ValidateOutcome::Verdict { verdict: Verdict::with_outcome(o) };
        match run_confined(sandbox, "crucible", &args, &files, workdir) {
            Ok(out) => {
                let combined = format!("{}\n{}", out.stdout, out.stderr);
                // Order matters: a fuzz finding and a clean run both mean the harness BUILT, so
                // classify those first — only a *non-zero* exit with build markers is a real
                // build failure. This keeps `error[...]`-looking runtime/log text in a clean
                // (exit 0) fuzz run from being misread as a build failure.
                if combined.contains("[FUZZ_FINDING]") {
                    // A crash = the property was refuted (a counterexample). Carry the finding
                    // (assertion message + crash id) so the BAD verdict explains itself.
                    let mut v = Verdict::with_outcome("BAD");
                    v.detail = finding_detail(&combined);
                    ValidateOutcome::Verdict { verdict: v }
                } else if out.exit_code == 0 {
                    verdict("GOOD") // ran to the budget with no violation = held
                } else if is_build_error(&combined) {
                    // Shared build; re-author the whole spec (docs/rust-backend-api.md).
                    ValidateOutcome::BuildFailed { errors: build_errors(&out) }
                } else {
                    // Non-zero exit with no build markers and no finding — capture the tail.
                    let mut v = Verdict::with_outcome("ERROR");
                    v.detail = Some(build_errors(&out));
                    ValidateOutcome::Verdict { verdict: v }
                }
            }
            Err(e) => {
                let mut v = Verdict::with_outcome("ERROR");
                v.detail = Some(e);
                ValidateOutcome::Verdict { verdict: v }
            }
        }
    }

    fn sandbox_grants(&self, _args: &serde_json::Value) -> SandboxGrants {
        // Read-only grants beyond the launcher's discovered Rust toolchain: the crucible checkout
        // (path deps) and the `crucible` binary's dir. Was Python's `crucible_sandbox` extra_ro.
        let mut extra_ro = Vec::new();
        if let Ok(repo) = std::env::var("CRUCIBLE_REPO") {
            extra_ro.push(repo);
        }
        if let Some(dir) = which_dir("crucible") {
            extra_ro.push(dir);
        }
        SandboxGrants { extra_ro, extra_env: Vec::new() }
    }

    fn workspace_prep(&self, input: &AuthorInput) -> WorkspacePrep {
        // Place a deps-only harness manifest (probe feature) so warming has a manifest and the
        // setup dry-run can select a feature; per-run builds overwrite it with their own feature.
        // Then warm the harness crate's deps and build the program `.so` (the host runs both with
        // the shared helpers — fetch unconfined, build confined+offline).
        let program = &input.program;
        let mut files = BTreeMap::new();
        if let Some(repo) = crucible_repo() {
            files.insert(
                format!("fuzz/{program}/Cargo.toml"),
                render_cargo_toml(program, &repo, std::slice::from_ref(&"c_probe".to_string())),
            );
        }
        WorkspacePrep {
            files,
            warm_dirs: vec![format!("fuzz/{program}")],
            build_program: Some(program.clone()),
        }
    }

    fn finalize(&self, outcomes: &serde_json::Value) -> BTreeMap<String, String> {
        // Assemble the one deliverable crate: the shared fixture + every delivered invariant's
        // test section, keyed by its feature (was Python's `CrucibleHarness`/`CrucibleArtifactStore`).
        let program = outcomes.get("program").and_then(|v| v.as_str()).unwrap_or_default();
        let fixture = outcomes.get("setup").and_then(|v| v.as_str()).unwrap_or_default();

        // feature -> test section (BTreeMap keeps a stable, sorted crate — matches the old store).
        let mut sections: BTreeMap<String, String> = BTreeMap::new();
        if let Some(comps) = outcomes.get("components").and_then(|v| v.as_array()) {
            for c in comps {
                if !c.get("delivered").and_then(|v| v.as_bool()).unwrap_or(false) {
                    continue;
                }
                let text = c.get("artifact_text").and_then(|v| v.as_str()).unwrap_or_default();
                // property_units: [[title, [feature, ...]], ...]
                if let Some(pu) = c.get("property_units").and_then(|v| v.as_array()) {
                    for entry in pu {
                        if let Some(units) = entry.get(1).and_then(|v| v.as_array()) {
                            for u in units.iter().filter_map(|v| v.as_str()) {
                                sections.insert(u.to_string(), text.trim().to_string());
                            }
                        }
                    }
                }
            }
        }
        if program.is_empty() || sections.is_empty() {
            return BTreeMap::new();
        }

        let features: Vec<String> = sections.keys().cloned().collect();
        let body = features.iter().map(|f| sections[f].clone()).collect::<Vec<_>>().join("\n\n");
        let main_rs = format!(
            "{}\n\n{}{}",
            fixture.trim_end(),
            body,
            if body.is_empty() { "" } else { "\n" }
        );
        let mut files = BTreeMap::new();
        files.insert(format!("fuzz/{program}/src/main.rs"), main_rs);
        if let Some(repo) = crucible_repo() {
            files.insert(
                format!("fuzz/{program}/Cargo.toml"),
                render_cargo_toml(program, &repo, &features),
            );
        }
        files
    }
}

autoprover_sdk::export_app!(crucible_app, CrucibleApp);
