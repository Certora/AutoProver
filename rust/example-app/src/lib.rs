//! The "echo prover" — a minimal but complete demonstration of a Rust-based
//! AutoProver application built on `autoprover-sdk`.
//!
//! It is intentionally not a real verifier: its authoring loop drafts a "spec"
//! from an LLM turn, caches it, "runs a prover", and publishes — exercising
//! every [`Command`] variant (`Emit`, `CacheGet`, `CallLlm`, `CachePut`,
//! `RunProver`, `Publish`, `GiveUp`) so the Python host and the FFI round-trip
//! can be tested end to end without any real service. A production backend keeps
//! this exact shape and swaps the decisions for real ones.

use std::collections::BTreeMap;

use autoprover_sdk::{
    AppDescriptor, Application, ArtifactLayout, Command, CoreSlot, EventKind, FormalizeInput,
    FormalizeSession, Formalized, Observation, PhaseSpec, Property, Verdict, VerdictInput,
};

/// Where the echo session is in its little authoring loop. The stage is what
/// disambiguates otherwise-identical observations (two different `Ack`s).
#[derive(Debug, Clone, Copy, PartialEq)]
enum Stage {
    Start,
    Emitted,
    Cache,
    Llm,
    DraftStored,
    Prove,
    Done,
}

struct EchoSession {
    props: Vec<Property>,
    spec: Option<String>,
    stage: Stage,
}

impl EchoSession {
    /// property title → the unit name that "demonstrates" it.
    fn property_units(&self) -> Vec<(String, Vec<String>)> {
        self.props
            .iter()
            .map(|p| (p.title.clone(), vec![format!("rule_{}", p.title)]))
            .collect()
    }

    fn publish(&self) -> Command {
        let spec = self.spec.clone().unwrap_or_default();
        Command::Publish {
            result: Formalized {
                commentary: format!("Echo-formalized {} propert(ies).", self.props.len()),
                artifact_text: spec,
                property_units: self.property_units(),
                skipped: Vec::new(),
                output_link: Some("local://echo/run".to_string()),
            },
        }
    }
}

impl FormalizeSession for EchoSession {
    fn resume(&mut self, observation: Observation) -> Command {
        match self.stage {
            Stage::Start => {
                // Announce ourselves on the task panel, then look for a cached draft.
                self.stage = Stage::Emitted;
                Command::Emit {
                    event_kind: "solver_line".to_string(),
                    payload: serde_json::json!({ "line": "echo: starting formalization" }),
                }
            }
            Stage::Emitted => {
                // Ack of the Emit.
                self.stage = Stage::Cache;
                Command::CacheGet { key: "echo_draft".to_string() }
            }
            Stage::Cache => {
                if let Observation::Cached { value: Some(v) } = &observation {
                    if let Some(s) = v.as_str() {
                        // Cache hit: skip the LLM, go straight to proving.
                        self.spec = Some(s.to_string());
                        self.stage = Stage::Prove;
                        return Command::RunProver {
                            spec: s.to_string(),
                            config: serde_json::Value::Null,
                            rules: None,
                        };
                    }
                }
                // Cache miss: ask the LLM to draft a spec.
                self.stage = Stage::Llm;
                let titles: Vec<&str> = self.props.iter().map(|p| p.title.as_str()).collect();
                Command::CallLlm {
                    messages: serde_json::json!({
                        "instruction": "Author a spec for these properties.",
                        "properties": titles,
                    }),
                }
            }
            Stage::Llm => {
                if let Observation::LlmReply { text } = observation {
                    self.spec = Some(text.clone());
                    self.stage = Stage::DraftStored;
                    Command::CachePut {
                        key: "echo_draft".to_string(),
                        value: serde_json::Value::String(text),
                    }
                } else {
                    self.stage = Stage::Done;
                    Command::GiveUp { reason: "expected an LLM reply".to_string() }
                }
            }
            Stage::DraftStored => {
                // Ack of the CachePut.
                self.stage = Stage::Prove;
                Command::RunProver {
                    spec: self.spec.clone().unwrap_or_default(),
                    config: serde_json::Value::Null,
                    rules: None,
                }
            }
            Stage::Prove => {
                self.stage = Stage::Done;
                if let Observation::ProverResult { data } = observation {
                    if data.get("verified").and_then(|v| v.as_bool()).unwrap_or(false) {
                        return self.publish();
                    }
                }
                Command::GiveUp { reason: "prover did not verify the spec".to_string() }
            }
            Stage::Done => Command::GiveUp { reason: "session already finished".to_string() },
        }
    }
}

/// The application singleton.
struct EchoApp;

impl Application for EchoApp {
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
            event_kinds: vec![EventKind { kind: "solver_line".into(), label: "Solver".into() }],
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

    fn new_session(&self, input: FormalizeInput) -> Box<dyn FormalizeSession> {
        Box::new(EchoSession { props: input.props, spec: None, stage: Stage::Start })
    }

    fn fetch_verdicts(&self, input: VerdictInput) -> BTreeMap<String, Verdict> {
        // The echo backend is self-contained: every demonstrated unit "passes".
        let mut out = BTreeMap::new();
        for (_title, units) in &input.property_units {
            for unit in units {
                out.insert(unit.clone(), Verdict::good());
            }
        }
        out
    }

    fn finalize(&self, _outcomes: &serde_json::Value) -> BTreeMap<String, String> {
        // No run-level artifact for the demo.
        BTreeMap::new()
    }
}

autoprover_sdk::export_app!(echoprover, EchoApp);
