//! The **Crucible** application — AutoProver's Solana verification backend, which
//! authors [Crucible](https://github.com/asymmetric-research/crucible) fuzz
//! harnesses and gates them with the local `crucible` CLI. Pairs with the shared
//! `solana` ecosystem front half (see `docs/crucible-application.md`).
//!
//! **Phase 1** (this file today) provides the declarative descriptor and a real
//! `validate_preconditions` (toolchain + project checks). The authoring loop
//! (`new_session`) is a deliberate stub — phase 1's gate is the build + IDL +
//! `crucible run --dry-run` infrastructure, exercised from Python, with no LLM and
//! no property authoring. Later phases fill in the decider.

use std::collections::BTreeMap;
use std::path::Path;

use autoprover_sdk::{
    AppDescriptor, ArgDefault, ArgSpec, Application, ArtifactLayout, Command, CoreSlot, EventKind,
    FormalizeInput, FormalizeSession, Formalized, Observation, PhaseSpec, Property, SetupInput,
    Verdict, VerdictInput,
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

/// A session that immediately gives up with a fixed reason (e.g. malformed input).
struct GiveUpNow(String);

impl FormalizeSession for GiveUpNow {
    fn resume(&mut self, _observation: Observation) -> Command {
        Command::GiveUp { reason: self.0.clone() }
    }
}

// ===========================================================================
// Setup session: author the shared fixture + actions once (docs §5.2).
// ===========================================================================

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

/// A `#[invariant_test]` probe appended (by the host) only to validate the fixture
/// via `crucible run … --dry-run`: it must compile and `setup()` must run once.
const PROBE_FN: &str = "\n\n#[invariant_test]\nfn c_probe(fixture: &mut Fixture) {\n    let _ = fixture;\n}\n";

const SETUP_MAX_ATTEMPTS: u32 = 7;

#[derive(PartialEq)]
enum SetupStage {
    Start,
    AwaitDraft,
    AwaitBuild,
    Done,
}

/// The fixture-authoring decider: draft (CallLlm) → validate (RunCommand dry-run)
/// → revise on failure → publish the fixture source, or give up.
struct SetupSession {
    program: String,
    analyzed: serde_json::Value,
    fixture: String,
    attempts: u32,
    stage: SetupStage,
}

impl SetupSession {
    fn author_prompt(&self, error: Option<&str>) -> serde_json::Value {
        let model = serde_json::to_string_pretty(&self.analyzed)
            .unwrap_or_else(|_| self.analyzed.to_string());
        let mut task = format!(
            "Author a Crucible fuzz-harness FIXTURE (only) for the Solana program `{program}`.\n\
             {cheat}\n\n\
             {facts}\n\
             Full analyzed system model (accounts, PDAs, authorities, requirements):\n{model}\n\n\
             Use the source-exploration tools to read the program's Rust source for exact \
             signatures. Return the complete fixture module source as your final answer.",
            program = self.program,
            cheat = HARNESS_CHEAT_SHEET.replace("<program>", &self.program),
            facts = api_facts(&self.analyzed, &self.program),
            model = model,
        );
        if let Some(err) = error {
            task.push_str(&revise_suffix(&self.fixture, err));
        }
        serde_json::json!({ "instruction": task })
    }

    fn validate_command(&self) -> Command {
        let mut main_rs = self.fixture.clone();
        main_rs.push_str(PROBE_FN);
        let mut files = BTreeMap::new();
        files.insert(format!("fuzz/{}/src/main.rs", self.program), main_rs);
        Command::RunCommand {
            program: "crucible".to_string(),
            args: vec![
                "run".into(),
                self.program.clone(),
                "c_probe".into(),
                "--release".into(),
                "--dry-run".into(),
            ],
            files,
        }
    }
}

/// Strip a leading/trailing ```rust code fence if the model wrapped its answer.
fn strip_code_fence(text: &str) -> String {
    let t = text.trim();
    if let Some(rest) = t.strip_prefix("```") {
        // drop an optional language tag on the first line, and the trailing fence
        let rest = rest.splitn(2, '\n').nth(1).unwrap_or(rest);
        return rest.trim_end().trim_end_matches("```").trim_end().to_string();
    }
    t.to_string()
}

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
    out.push_str(&format!(
        "  crate id (for `use <id>::*`): {}\n",
        str_of(prog.get("program_identifier"))
    ));
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

impl FormalizeSession for SetupSession {
    fn resume(&mut self, observation: Observation) -> Command {
        match self.stage {
            SetupStage::Start => {
                self.stage = SetupStage::AwaitDraft;
                Command::CallLlm { messages: self.author_prompt(None) }
            }
            SetupStage::AwaitDraft => {
                if let Observation::LlmReply { text } = observation {
                    self.fixture = strip_code_fence(&text);
                    self.stage = SetupStage::AwaitBuild;
                    self.validate_command()
                } else {
                    self.stage = SetupStage::Done;
                    Command::GiveUp { reason: "expected an LLM reply while drafting the fixture".into() }
                }
            }
            SetupStage::AwaitBuild => match observation {
                Observation::CommandResult { exit_code: 0, .. } => {
                    self.stage = SetupStage::Done;
                    Command::Publish {
                        result: Formalized::new(
                            self.fixture.clone(),
                            format!("Crucible shared fixture for `{}` (dry-run OK).", self.program),
                        ),
                    }
                }
                Observation::CommandResult { stdout, stderr, .. } => {
                    if self.attempts + 1 >= SETUP_MAX_ATTEMPTS {
                        self.stage = SetupStage::Done;
                        Command::GiveUp {
                            reason: format!(
                                "fixture did not pass --dry-run after {SETUP_MAX_ATTEMPTS} attempts"
                            ),
                        }
                    } else {
                        self.attempts += 1;
                        self.stage = SetupStage::AwaitDraft;
                        let err = format!("{stdout}\n{stderr}");
                        Command::CallLlm { messages: self.author_prompt(Some(&err)) }
                    }
                }
                _ => {
                    self.stage = SetupStage::Done;
                    Command::GiveUp { reason: "expected a command result after dry-run".into() }
                }
            },
            SetupStage::Done => Command::GiveUp { reason: "setup session already finished".into() },
        }
    }
}

// ===========================================================================
// Per-component session: author one test, fuzz it, verdict (docs §5.3–5.4).
// ===========================================================================

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

const PC_MAX_ATTEMPTS: u32 = 7;

#[derive(PartialEq)]
enum PcStage {
    Start,
    AwaitDraft,
    AwaitFuzz,
    Done,
}

/// Author one component's test, fuzz it, and bake in the verdict.
struct PerComponentSession {
    program: String,
    feature: String, // == the test fn name (macro self-gates), c_<slug>
    fixture: String,
    component: serde_json::Value,
    props: Vec<Property>,
    fuzz_timeout: u64,
    test_src: String,
    attempts: u32,
    stage: PcStage,
}

impl PerComponentSession {
    fn property_units(&self) -> Vec<(String, Vec<String>)> {
        self.props
            .iter()
            .map(|p| (p.title.clone(), vec![self.feature.clone()]))
            .collect()
    }

    fn author_prompt(&self, error: Option<&str>) -> serde_json::Value {
        let props: Vec<String> = self
            .props
            .iter()
            .map(|p| format!("- [{}] {}: {}", p.sort, p.title, p.description))
            .collect();
        let component = serde_json::to_string_pretty(&self.component)
            .unwrap_or_else(|_| self.component.to_string());
        let mut task = format!(
            "Author ONE Crucible test function named exactly `{feature}` that checks these \
             properties of the `{program}` program's instruction:\n{props}\n\n\
             Instruction / component:\n{component}\n\n\
             {cheat}\n\n\
             The shared fixture is ALREADY defined (do not redefine it); use `Fixture` and its \
             `action_*` methods. Fixture source for reference:\n```rust\n{fixture}\n```",
            feature = self.feature,
            program = self.program,
            props = props.join("\n"),
            component = component,
            cheat = TEST_CHEAT_SHEET.replace("{feature}", &self.feature),
            fixture = self.fixture,
        );
        if let Some(err) = error {
            task.push_str(&revise_suffix(&self.test_src, err));
        }
        serde_json::json!({ "instruction": task })
    }

    fn fuzz_command(&self) -> Command {
        let mut main_rs = self.fixture.clone();
        main_rs.push('\n');
        main_rs.push('\n');
        main_rs.push_str(&self.test_src);
        let mut files = BTreeMap::new();
        files.insert(format!("fuzz/{}/src/main.rs", self.program), main_rs);
        Command::RunCommand {
            program: "crucible".to_string(),
            args: vec![
                "run".into(),
                self.program.clone(),
                self.feature.clone(),
                "--release".into(),
                "--mode".into(),
                "explore".into(),
                "--timeout".into(),
                self.fuzz_timeout.to_string(),
            ],
            files,
        }
    }

    fn publish(&self, outcome: &str, note: &str) -> Command {
        let mut verdicts = BTreeMap::new();
        verdicts.insert(self.feature.clone(), Verdict::with_outcome(outcome));
        Command::Publish {
            result: Formalized {
                commentary: format!("{note} ({} propert(ies)).", self.props.len()),
                artifact_text: self.test_src.clone(),
                property_units: self.property_units(),
                skipped: Vec::new(),
                output_link: None,
                verdicts,
            },
        }
    }
}

/// Did the build fail (as opposed to the harness building and fuzzing)?
fn is_build_error(out: &str) -> bool {
    out.contains("could not compile") || out.contains("error[") || out.contains("Build failed")
}

impl FormalizeSession for PerComponentSession {
    fn resume(&mut self, observation: Observation) -> Command {
        match self.stage {
            PcStage::Start => {
                self.stage = PcStage::AwaitDraft;
                Command::CallLlm { messages: self.author_prompt(None) }
            }
            PcStage::AwaitDraft => {
                if let Observation::LlmReply { text } = observation {
                    self.test_src = strip_code_fence(&text);
                    self.stage = PcStage::AwaitFuzz;
                    self.fuzz_command()
                } else {
                    self.stage = PcStage::Done;
                    Command::GiveUp { reason: "expected an LLM reply while drafting the test".into() }
                }
            }
            PcStage::AwaitFuzz => match observation {
                Observation::CommandResult { stdout, stderr, .. } => {
                    let combined = format!("{stdout}\n{stderr}");
                    if is_build_error(&combined) {
                        if self.attempts + 1 >= PC_MAX_ATTEMPTS {
                            self.stage = PcStage::Done;
                            Command::GiveUp {
                                reason: format!("test did not compile after {PC_MAX_ATTEMPTS} attempts"),
                            }
                        } else {
                            self.attempts += 1;
                            self.stage = PcStage::AwaitDraft;
                            Command::CallLlm { messages: self.author_prompt(Some(&combined)) }
                        }
                    } else if combined.contains("[FUZZ_FINDING]") {
                        // A crash = the property was refuted (a real counterexample).
                        self.stage = PcStage::Done;
                        self.publish("BAD", "Crucible refuted the property (fuzzing counterexample)")
                    } else {
                        // Ran to the timeout with no violation = held within the budget.
                        self.stage = PcStage::Done;
                        self.publish("GOOD", "No violation found within the fuzzing budget")
                    }
                }
                _ => {
                    self.stage = PcStage::Done;
                    Command::GiveUp { reason: "expected a command result after fuzzing".into() }
                }
            },
            PcStage::Done => Command::GiveUp { reason: "session already finished".into() },
        }
    }
}

struct CrucibleApp;

impl Application for CrucibleApp {
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
                PhaseSpec { key: "analysis".into(), label: "System Analysis".into(), order: 0, core_slot: Some(CoreSlot::Analysis) },
                PhaseSpec { key: "extraction".into(), label: "Property Extraction".into(), order: 1, core_slot: Some(CoreSlot::Extraction) },
                // UI-only phase: build the program `.so` + IDL + shared fixture (§5.1).
                PhaseSpec { key: "build_harness".into(), label: "Build Harness".into(), order: 2, core_slot: None },
                PhaseSpec { key: "formalization".into(), label: "Harness Authoring".into(), order: 3, core_slot: Some(CoreSlot::Formalization) },
                PhaseSpec { key: "report".into(), label: "Report".into(), order: 4, core_slot: Some(CoreSlot::Report) },
            ],
            args: vec![
                ArgSpec {
                    flag: "--crucible-version".to_string(),
                    help: "Crucible release tag / git ref to build the harness against (pins the \
                           litesvm/anchor/solana stack; see docs §6.1).".to_string(),
                    default: ArgDefault::Str { value: None },
                    required: false,
                },
                ArgSpec {
                    flag: "--fuzz-timeout".to_string(),
                    help: "Per-test fuzzing budget in seconds (`crucible run --timeout`).".to_string(),
                    default: ArgDefault::Int { value: Some(60) },
                    required: false,
                },
                ArgSpec {
                    flag: "--fuzz-cores".to_string(),
                    help: "Parallel fuzzer workers per run (`crucible run --cores`).".to_string(),
                    default: ArgDefault::Int { value: Some(1) },
                    required: false,
                },
                ArgSpec {
                    flag: "--stateful".to_string(),
                    help: "Use Crucible stateful mode (single action per iteration, state pool).".to_string(),
                    default: ArgDefault::Bool { value: false },
                    required: false,
                },
            ],
            rag_db_default: Some("crucible_kb".to_string()),
            event_kinds: vec![
                EventKind { kind: "fuzz_pulse".into(), label: "Fuzzing".into() },
                EventKind { kind: "fuzz_finding".into(), label: "Finding".into() },
                EventKind { kind: "build_output".into(), label: "Build".into() },
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

    fn new_setup_session(&self, input: SetupInput) -> Option<Box<dyn FormalizeSession>> {
        Some(Box::new(SetupSession {
            program: input.program,
            analyzed: input.analyzed,
            fixture: String::new(),
            attempts: 0,
            stage: SetupStage::Start,
        }))
    }

    fn new_session(&self, input: FormalizeInput) -> Box<dyn FormalizeSession> {
        let cfg = &input.config;
        let s = |k: &str| cfg.get(k).and_then(|v| v.as_str()).map(str::to_string);

        // The shared fixture + the component's slug are threaded in via `config` by
        // the host (from the setup session / the artifact store's slug).
        let (Some(fixture), Some(slug)) = (s("fixture"), s("slug")) else {
            return Box::new(GiveUpNow(
                "crucible new_session requires config.fixture and config.slug".into(),
            ));
        };
        let program = s("program")
            .or_else(|| input.component.get("program").and_then(|v| v.as_str()).map(str::to_string))
            .unwrap_or_default();
        let fuzz_timeout = cfg.get("fuzz_timeout").and_then(|v| v.as_u64()).unwrap_or(30);

        Box::new(PerComponentSession {
            program,
            feature: format!("c_{slug}"),
            fixture,
            component: input.component,
            props: input.props,
            fuzz_timeout,
            test_src: String::new(),
            attempts: 0,
            stage: PcStage::Start,
        })
    }

    fn fetch_verdicts(&self, _input: VerdictInput) -> BTreeMap<String, Verdict> {
        // No verdicts until the authoring loop lands.
        BTreeMap::new()
    }
}

autoprover_sdk::export_app!(crucible_app, CrucibleApp);
