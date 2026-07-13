//! The "echo prover" — a minimal, self-contained demonstration of a Rust-based
//! AutoProver [`Backend`] on `autoprover-sdk`. It is intentionally not a real
//! verifier: it authors a "spec" from an LLM turn, treats compilation as a no-op,
//! and validates every unit as GOOD — enough to exercise the Python host + FFI
//! round-trip (descriptor, units, author_prompt, compile, validate) without any real
//! toolchain. A production backend keeps this exact shape and swaps the callouts for
//! real ones (see `docs/rust-backend-api.md`).

use std::path::Path;

use autoprover_sdk::{
    AppDescriptor, ArtifactLayout, AuthorInput, Backend, CompileResult, CoreSlot, EventKind,
    Failure, PhaseSpec, Prompt, Sandbox, Unit, Verdict,
};

struct EchoApp;

impl Backend for EchoApp {
    fn descriptor(&self) -> AppDescriptor {
        AppDescriptor {
            name: "echoprover".to_string(),
            header_text: "Echo Prover (Rust demo) | AutoProver".to_string(),
            ecosystem: "evm".to_string(),
            backend_tag: "echoprover".to_string(),
            backend_guidance: "These properties are checked by the echo backend, a demo that \
                accepts any well-formed spec. Feel free to state universal properties."
                .to_string(),
            analysis_key: "echoprover-analysis".to_string(),
            phases: vec![
                PhaseSpec { key: "analysis".into(), label: "System Analysis".into(), order: 0, core_slot: Some(CoreSlot::Analysis) },
                PhaseSpec { key: "extraction".into(), label: "Property Extraction".into(), order: 1, core_slot: Some(CoreSlot::Extraction) },
                // A UI-only phase with no core slot (cf. autoprove's harness/autosetup).
                PhaseSpec { key: "solving".into(), label: "Solving".into(), order: 2, core_slot: None },
                PhaseSpec { key: "formalization".into(), label: "Formalization".into(), order: 3, core_slot: Some(CoreSlot::Formalization) },
                PhaseSpec { key: "report".into(), label: "Report".into(), order: 4, core_slot: Some(CoreSlot::Report) },
            ],
            args: vec![autoprover_sdk::ArgSpec {
                flag: "--echo-tag".to_string(),
                help: "An arbitrary tag stamped into the echo spec.".to_string(),
                default: autoprover_sdk::ArgDefault::Str { value: Some("demo".to_string()) },
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
        }
    }

    fn units(&self, input: &AuthorInput) -> Vec<Unit> {
        input
            .props
            .iter()
            .enumerate()
            .map(|(i, p)| {
                let slug = if p.slug.is_empty() { format!("p{i}") } else { p.slug.clone() };
                Unit { property: p.title.clone(), unit: format!("rule_{slug}") }
            })
            .collect()
    }

    fn author_prompt(&self, input: &AuthorInput, failure: Option<&Failure>) -> Prompt {
        let titles: Vec<&str> = input.props.iter().map(|p| p.title.as_str()).collect();
        let mut instruction = format!(
            "Author a spec with a rule per property: {}. Return the spec source only.",
            titles.join(", ")
        );
        if let Some(f) = failure {
            instruction.push_str(&format!("\n\nThe previous attempt was rejected: {}", f.errors));
        }
        Prompt { system: None, instruction }
    }

    fn compile(
        &self,
        _input: &AuthorInput,
        _spec: &str,
        _workdir: &Path,
        _sandbox: &Sandbox,
    ) -> CompileResult {
        // The demo accepts any well-formed spec — no build gate.
        CompileResult::Ok
    }

    fn validate(
        &self,
        _input: &AuthorInput,
        _spec: &str,
        _unit: &str,
        _workdir: &Path,
        _sandbox: &Sandbox,
    ) -> Verdict {
        // Self-contained: every demonstrated unit "passes".
        Verdict::with_outcome("GOOD")
    }
}

autoprover_sdk::export_app!(echoprover, EchoApp);
