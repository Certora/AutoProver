//! The "echo prover" — a minimal, self-contained demonstration of a Rust-based
//! AutoProver [`Backend`] on `autoprover-sdk`. It is intentionally not a real
//! verifier: it authors a "spec" from an LLM turn, treats compilation as a no-op,
//! and treats reading the spec as its whole checker — enough to exercise the Python
//! host + FFI round-trip (descriptor, author_prompt, compile, validate) without any
//! real toolchain. A production backend keeps this exact shape and swaps the callouts
//! for real ones (see `docs/rust-applications.md`).

use autoprover_sdk::authoring::{AuthorInput, Prompt};
use autoprover_sdk::descriptor::{
    default_evidence_kinds, AppDescriptor, ArgDefault, ArgSpec, ArtifactLayout, DeliverableMode,
    EventKind, PhaseRole, PhaseSpec,
};
use autoprover_sdk::outcome::{CompileResult, Outcome, Target, ValidateOutcome, Verdict};
use autoprover_sdk::sandbox::Workspace;
use autoprover_sdk::Backend;

struct EchoApp;

impl Backend for EchoApp {
    fn descriptor(&self) -> AppDescriptor {
        AppDescriptor {
            name: "echoprover".to_string(),
            header_text: "Echo Prover (Rust demo) | AutoProver".to_string(),
            ecosystem: "evm".to_string(),
            // Must be a tag the *report* knows (`ReportBackend`: prover | foundry | none) — it
            // picks the outcome wording, and the Python host validates it when it parses this
            // descriptor. The demo has no vocabulary of its own, so it borrows "prover"; a real
            // backend adds its own literal to `ReportBackend` instead.
            backend_tag: "prover".to_string(),
            backend_guidance: "These properties are checked by the echo backend, a demo that \
                accepts any well-formed spec. Feel free to state universal properties."
                .to_string(),
            analysis_key: "echoprover-analysis".to_string(),
            phases: vec![
                PhaseSpec::step("analysis", "System Analysis", 0, PhaseRole::Analysis),
                PhaseSpec::step("extraction", "Property Extraction", 1, PhaseRole::Extraction),
                // A phase that only groups (cf. autoprove's harness/autosetup).
                PhaseSpec::grouping("solving", "Solving", 2),
                PhaseSpec::step("formalization", "Formalization", 3, PhaseRole::Formalization),
                PhaseSpec::step("report", "Report", 4, PhaseRole::Report),
            ],
            args: vec![ArgSpec {
                flag: "--echo-tag".to_string(),
                help: "An arbitrary tag stamped into the echo spec.".to_string(),
                default: ArgDefault::Str { value: Some("demo".to_string()) },
                required: false,
            }],
            rag_db_default: None,
            event_kinds: vec![EventKind::log("solver_line", "Solver")],
            artifact_layout: ArtifactLayout {
                deliverable_dir: "certora/echo".into(),
                internal_dir: ".certora_internal/echo".into(),
                report_dir: "certora/echo/reports".into(),
                artifact_dir: "certora/echo/specs".into(),
                artifact_prefix: "echospec".into(),
                artifact_extension: "espec".into(),
                property_suffix: "property_rules".into(),
            },
            // A simple per-component wheel: nothing to gate ahead of analysis (there is no
            // workspace to prepare and `compile` is a no-op) and no shared setup — neither role is
            // claimed by a phase above — plus one file per component and no toolchain
            // confinement/serialization. All defaults.
            deliverable_mode: DeliverableMode::PerComponent,
            serialize_toolchain: false,
            confine_by_default: false,
            component_noun: None,
            // The demo authors `rule_<slug>` checks, so that is what its prompts should call them.
            check_noun: Some("rule".into()),
            evidence_kinds: default_evidence_kinds(),
            // Reading the spec back is this demo's whole checker, so a refuted rule means the spec
            // did not say what was asked — nothing about the program. There is no finding to write
            // up, and declining is how a wheel says so.
            findings: None,
        }
    }

    fn author_prompt(&self, input: &AuthorInput) -> Prompt {
        let titles: Vec<&str> = input.props.iter().map(|p| p.title.as_str()).collect();
        Prompt {
            system: None,
            instruction: format!(
                "Author a spec with a rule per property: {}. Declare each rule on its own line as \
                 `rule <name>:`, and map every property to the rules that verify it.",
                titles.join(", ")
            ),
        }
    }

    fn compile(&self, _input: &AuthorInput, _spec: Option<&str>, _ws: &Workspace) -> CompileResult {
        // The demo accepts any well-formed spec — no build gate.
        CompileResult::Ok
    }

    fn validate(
        &self,
        _input: &AuthorInput,
        spec: &str,
        target: &Target,
        _ws: &Workspace,
    ) -> ValidateOutcome {
        // The check names are the author's, so a run has to hold them to the artifact (the verdict
        // contract on `Backend::validate`). This wheel has no toolchain, so reading the spec IS its
        // checker — `rule <name>:` is the whole language. A real backend must corroborate with
        // something its checker observed; reading source text for a name that *looks* like a check
        // would not do, since a name in a comment or in dead code reads exactly the same.
        let declared: Vec<&str> = spec
            .lines()
            .filter_map(|l| l.trim().strip_prefix("rule ")?.split(':').next())
            .map(str::trim)
            .collect();
        target.verdicts(|c| {
            if declared.contains(&c.name.as_str()) {
                Verdict::with_outcome(Outcome::Good)
            } else {
                Verdict::with_outcome(Outcome::Error)
                    .with_detail(Some(format!("the spec declares no rule named `{}`", c.name)))
            }
        })
    }
}

autoprover_sdk::export_app!(echoprover, EchoApp);
