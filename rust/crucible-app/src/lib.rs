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
    FormalizeInput, FormalizeSession, Formalized, Observation, PhaseSpec, SetupInput, Verdict,
    VerdictInput,
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

/// A session that does nothing yet — phase 1 does not implement the authoring loop.
/// If the driver ever reaches formalization with this build, it declines cleanly
/// rather than looping.
struct Phase1Stub;

impl FormalizeSession for Phase1Stub {
    fn resume(&mut self, _observation: Observation) -> Command {
        Command::GiveUp {
            reason: "crucible authoring loop is not implemented yet (phase 1 covers \
                     preconditions + build/IDL + dry-run only)"
                .to_string(),
        }
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

- Read the program source for exact instruction args, Accounts structs, PDA seeds
  (binary vs string), and signer requirements — the model below is a summary.
- Output ONLY the fixture module source (imports + struct + #[fuzz_fixture] impl).
  Do NOT write `fn main`, `#[invariant_test]`, or `#[crucible_fuzz]` — those are added later.
"#;

/// A `#[invariant_test]` probe appended (by the host) only to validate the fixture
/// via `crucible run … --dry-run`: it must compile and `setup()` must run once.
const PROBE_FN: &str = "\n\n#[invariant_test]\nfn c_probe(fixture: &mut Fixture) {\n    let _ = fixture;\n}\n";

const SETUP_MAX_ATTEMPTS: u32 = 4;

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
             Analyzed system model (instructions, accounts, PDAs, authorities):\n{model}\n\n\
             Use the source-exploration tools to read the program's Rust source for exact \
             signatures. Return the complete fixture module source as your final answer.",
            program = self.program,
            cheat = HARNESS_CHEAT_SHEET.replace("<program>", &self.program),
            model = model,
        );
        if let Some(err) = error {
            task.push_str(&format!(
                "\n\nThe previous fixture FAILED to build / dry-run. Fix it. Prior fixture:\n\
                 ```rust\n{prev}\n```\nBuild/dry-run output:\n{err}",
                prev = self.fixture,
                err = &err[err.len().saturating_sub(4000)..],
            ));
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

    fn new_session(&self, _input: FormalizeInput) -> Box<dyn FormalizeSession> {
        Box::new(Phase1Stub)
    }

    fn fetch_verdicts(&self, _input: VerdictInput) -> BTreeMap<String, Verdict> {
        // No verdicts until the authoring loop lands.
        BTreeMap::new()
    }
}

autoprover_sdk::export_app!(crucible_app, CrucibleApp);
