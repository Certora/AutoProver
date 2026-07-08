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
    FormalizeInput, FormalizeSession, Observation, PhaseSpec, Verdict, VerdictInput,
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

    fn new_session(&self, _input: FormalizeInput) -> Box<dyn FormalizeSession> {
        Box::new(Phase1Stub)
    }

    fn fetch_verdicts(&self, _input: VerdictInput) -> BTreeMap<String, Verdict> {
        // No verdicts until the authoring loop lands.
        BTreeMap::new()
    }
}

autoprover_sdk::export_app!(crucible_app, CrucibleApp);
