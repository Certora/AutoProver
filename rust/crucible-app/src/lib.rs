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
use std::path::Path;

use autoprover_sdk::{
    run_confined, AppDescriptor, ArgDefault, ArgSpec, ArtifactLayout, AuthorInput, Backend,
    CommandOutput, CompileResult, CoreSlot, EventKind, Failure, PhaseSpec, Prompt, Sandbox, Unit,
    Verdict,
};
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
const TEST_CHEAT_SHEET: &str = r#"
Write ONE Crucible test function (no fixture — it already exists as `Fixture`):

- `#[invariant_test] fn <name>(fixture: &mut Fixture) { ... }` runs AFTER EACH
  fuzzed action — use it for state invariants (conservation, solvency, bounds).
- `#[crucible_fuzz] fn <name>(fixture: &mut Fixture, #[range(..)] x: u64) { ... }`
  runs single random operations — use it for per-instruction properties.
- Assert against ON-CHAIN state, not local mirrors:
    let acct = fixture.ctx.read_anchor_account::<SomeState>(&fixture.some_pda).unwrap();
    fuzz_assert_le!(acct.balance, cap, "message");   // fuzz_assert_{eq,ne,lt,le,gt,ge}
- Drive state via the fixture's existing `action_*` methods; do not re-`send()`
  instructions yourself unless necessary.
- Return ONLY the annotated test fn. It MUST be named exactly `{feature}`.
"#;
/// Did the build fail (as opposed to the harness building and fuzzing)?
fn is_build_error(out: &str) -> bool {
    out.contains("could not compile") || out.contains("error[") || out.contains("Build failed")
}

// ===========================================================================
// Backend glue: small pure helpers shared by the callouts.
// ===========================================================================

/// The single harness source file the crate builds, keyed by its crate-relative path.
fn one_file(program: &str, main_rs: String) -> BTreeMap<String, String> {
    let mut files = BTreeMap::new();
    files.insert(format!("fuzz/{program}/src/main.rs"), main_rs);
    files
}

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
                // The per-invariant verdict — surfaced as a persistent callout + toast.
                EventKind::notice("verdict", "Verdict"),
            ],
            // NOTE: the deliverable model (one shared crate vs per-component files) is
            // settled in phase 2 (docs §7.1); these are provisional.
            artifact_layout: ArtifactLayout {
                deliverable_dir: "fuzz".into(),
                internal_dir: ".certora_internal/crucible".into(),
                report_dir: "certora/crucible/reports".into(),
                artifact_dir: "certora/crucible/harnesses".into(),
                artifact_prefix: "harness".into(),
                artifact_extension: "rs".into(),
                property_suffix: "property_tests".into(),
            },
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
                task.push_str(&revise_suffix(&f.draft, &f.errors));
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
                task.push_str(&revise_suffix(&f.draft, &f.errors));
            }
            task
        };
        Prompt { system: None, instruction }
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
        let files = one_file(program, main_rs);
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
    ) -> Verdict {
        let program = &input.program;
        let fixture = ctx_str(input, "fixture");
        let timeout = ctx_u64(input, "fuzz_timeout", 30);
        let files = one_file(program, format!("{fixture}\n\n{spec}"));
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
        match run_confined(sandbox, "crucible", &args, &files, workdir) {
            Ok(out) => {
                let combined = format!("{}\n{}", out.stdout, out.stderr);
                if is_build_error(&combined) {
                    Verdict::with_outcome("ERROR")
                } else if combined.contains("[FUZZ_FINDING]") {
                    // A crash = the property was refuted (a real counterexample).
                    Verdict::with_outcome("BAD")
                } else {
                    // Ran to the timeout with no violation = held within the budget.
                    Verdict::with_outcome("GOOD")
                }
            }
            Err(e) => {
                let mut v = Verdict::with_outcome("ERROR");
                v.unit_file = Some(e);
                v
            }
        }
    }
}

autoprover_sdk::export_app!(crucible_app, CrucibleApp);
