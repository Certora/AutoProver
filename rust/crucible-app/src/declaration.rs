//! The declaration the Python host reads at load time: the phases it synthesizes a run from, the
//! flags it adds to argparse, the events the frontend renders, and where the run's files land
//! (`docs/rust-applications.md` §3).

use askama::Template;

use autoprover_sdk::descriptor::{
    AppDescriptor, ArgDefault, ArgSpec, ArtifactLayout, DeliverableMode, EventKind, PhaseRole,
    PhaseSpec,
};

use crate::layout::{CRATE_ROOT, HARNESS_ROOT, REPORT_ROOT};
use crate::templates::BackendGuidance;

/// Everything this application declares about itself.
pub(crate) fn descriptor() -> AppDescriptor {
    AppDescriptor {
        name: "crucible".to_string(),
        header_text: "Crucible — Solana fuzzing backend | AutoProver".to_string(),
        // Selects the shared `solana` ecosystem front half (system model + prompts).
        ecosystem: "solana".to_string(),
        backend_tag: "crucible".to_string(),
        backend_guidance: BackendGuidance.render().expect("render backend_guidance"),
        analysis_key: "crucible-solana-analysis".to_string(),
        // A phase's role is also the declaration of the step it groups, so the two host-run
        // steps below (the preflight gate, the shared fixture) need nothing else said.
        phases: vec![
            // Discover the design doc when one isn't supplied (§host).
            PhaseSpec::step("discover_design_doc", "Design Doc Discovery", 0, PhaseRole::Discovery),
            // The program `.so` + IDL + the skeleton harness build (§5.0). It runs CONCURRENTLY
            // with the two phases below it — the order is the declaration's, which is what the
            // frontend lists sections by, not a claim about sequencing. Gating the whole
            // toolchain surface once, up front, against a skeleton this wheel authors itself:
            // a dependency or codegen problem is not something a fixture author can fix, so it
            // must not first appear as compiler errors in its draft.
            PhaseSpec::step("preflight", "Build Preflight", 1, PhaseRole::Preflight),
            PhaseSpec::step("analysis", "System Analysis", 2, PhaseRole::Analysis),
            PhaseSpec::step("extraction", "Property Extraction", 3, PhaseRole::Extraction),
            // The shared fixture, authored once every property is known (§5.2).
            PhaseSpec::step("harness_fixture", "Harness Fixture", 4, PhaseRole::Setup),
            PhaseSpec::step("formalization", "Harness Authoring", 5, PhaseRole::Formalization),
            PhaseSpec::step("report", "Report", 6, PhaseRole::Report),
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
            // The IDL path is chosen automatically for a program whose Anchor major this
            // harness can't link (§6.1), but only if an IDL can be produced — which needs the
            // *program's* `anchor` CLI. This flag supplies one instead (any Anchor IDL format;
            // the generator converts legacy IDLs itself), and also forces the IDL path for a
            // program that would otherwise be linked directly.
            ArgSpec {
                flag: "--program-idl".to_string(),
                help: "Path to the program's IDL JSON. Generates the harness's types from it \
                       instead of depending on the program crate — required for a program \
                       built against a different Anchor/Solana stack than Crucible's."
                    .to_string(),
                default: ArgDefault::Str { value: None },
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
        // `certora/crucible/` — the split Foundry uses — while the crate deliverable is the one
        // file under [`HARNESS_ROOT`] (the callout mode's `primary` + the finalize render).
        artifact_layout: ArtifactLayout {
            deliverable_dir: "certora/crucible".into(),
            internal_dir: ".certora_internal/crucible".into(),
            report_dir: REPORT_ROOT.into(),
            artifact_dir: "certora/crucible/harnesses".into(),
            artifact_prefix: "harness".into(),
            artifact_extension: "rs".into(),
            property_suffix: "property_tests".into(),
        },
        // One crate assembled by finalize (callout), all toolchain runs serialized on the one
        // crate/target, and confined by default (untrusted native builds).
        deliverable_mode: DeliverableMode::Callout {
            primary: Some(format!("{HARNESS_ROOT}/{{program}}/{CRATE_ROOT}")),
        },
        serialize_toolchain: true,
        confine_by_default: true,
        // Units are the Solana ecosystem's `ProgramComponent`s, so the SDK default noun is
        // right — this used to say "instruction", from the long-gone per-instruction units.
        component_noun: None,
        // A Crucible check is one property's assertion inside the component's
        // `#[invariant_test]` fn, which is what its own prompts and generated code call it.
        check_noun: Some("invariant".into()),
        // What an author can actually produce here: the harness build's diagnostics, the
        // fuzzer's output, and the reproducing action sequence behind a crash.
        evidence_kinds: vec![
            "build_failure".into(),
            "fuzz_output".into(),
            "counterexample".into(),
            "manual_citation".into(),
            "reasoned".into(),
        ],
    }
}
