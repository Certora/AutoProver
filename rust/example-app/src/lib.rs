//! The "echo prover" — a minimal, self-contained demonstration of a Rust-based
//! AutoProver [`Backend`] on `autoprover-sdk`. It is intentionally not a real
//! verifier: it authors a "spec" from an LLM turn, treats compilation as a no-op,
//! and validates every unit as GOOD — enough to exercise the Python host + FFI
//! round-trip (descriptor, checks, author_prompt, compile, validate) without any real
//! toolchain. A production backend keeps this exact shape and swaps the callouts for
//! real ones (see `docs/rust-applications.md`).

use autoprover_sdk::authoring::{AuthorInput, Prompt};
use autoprover_sdk::descriptor::{
    default_evidence_kinds, AppDescriptor, ArgDefault, ArgSpec, ArtifactLayout, DeliverableMode,
    EventKind, PhaseRole, PhaseSpec,
};
use autoprover_sdk::outcome::{Check, CompileResult, Outcome, Target, ValidateOutcome};
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
        }
    }

    fn checks(&self, input: &AuthorInput) -> Vec<Check> {
        input
            .props
            .iter()
            .enumerate()
            .map(|(i, p)| {
                let slug = if p.slug.is_empty() { format!("p{i}") } else { p.slug.clone() };
                Check { property: p.title.clone(), name: format!("rule_{slug}"), target: None }
            })
            .collect()
    }

    fn author_prompt(&self, input: &AuthorInput) -> Prompt {
        let titles: Vec<&str> = input.props.iter().map(|p| p.title.as_str()).collect();
        Prompt {
            system: None,
            instruction: format!(
                "Author a spec with a rule per property: {}.",
                titles.join(", ")
            ),
        }
    }

    fn compile(&self, _input: &AuthorInput, _spec: &str, _ws: &Workspace) -> CompileResult {
        // The demo accepts any well-formed spec — no build gate.
        CompileResult::Ok
    }

    fn validate(
        &self,
        _input: &AuthorInput,
        _spec: &str,
        target: &Target,
        _ws: &Workspace,
    ) -> ValidateOutcome {
        // Self-contained: any well-formed spec builds, so every check the target covers "passes".
        target.all(Outcome::Good, None)
    }
}

autoprover_sdk::export_app!(echoprover, EchoApp);
