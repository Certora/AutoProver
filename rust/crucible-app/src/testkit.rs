//! Fixtures the modules' tests share: the payloads the host sends this wheel, and the three ways a
//! run writes crate files (the warming plan, a gated build, the deliverable).
//!
//! Written as JSON wherever the host would send JSON, so a test also pins the payload shape the
//! callout is handed rather than a struct the host never sends.

use std::collections::BTreeMap;

use autoprover_sdk::args::DeclaredArgs;
use autoprover_sdk::authoring::{AuthorInput, Authored, Property, PropertyKind};
use autoprover_sdk::chain::ChainData;
use autoprover_sdk::finalize::FinalizeInput;
use autoprover_sdk::outcome::{Check, Exploration, Outcome, Target, ValidateOutcome};
use autoprover_sdk::prep::WorkspacePrep;
use autoprover_sdk::Backend;
use autoprover_solana::{SolanaPrep, SolanaSourceUnit};

use crate::app::CrucibleApp;
use crate::harness::{HarnessSpec, ANCHOR_VERSION};
use crate::layout::harness_dir;

/// A path inside `program`'s harness crate, as the host receives it (workdir-relative). Derived
/// rather than spelled, so a layout move touches the one test that pins the layout itself.
pub(crate) fn at(program: &str, rel: &str) -> String {
    format!("{}/{rel}", harness_dir(program))
}

/// A crate whose directory, package and lib names all differ from the analysis identifier —
/// the lend shape (`programs/lend`, package `example_lending`), which is what the
/// `programs/<program>` convention got wrong. `anchor` is ours, so it stays linkable.
pub(crate) fn distinct_crate() -> SolanaSourceUnit {
    SolanaSourceUnit {
        dir: "programs/lend".into(),
        package: "example-lending".into(),
        lib: "example_lending".into(),
        anchor: ANCHOR_VERSION.into(),
    }
}

/// The same crate on an Anchor major this harness cannot link — lend's real one.
pub(crate) fn skewed_crate() -> SolanaSourceUnit {
    SolanaSourceUnit { anchor: "0.29.0".into(), ..distinct_crate() }
}

/// The crate behind the `lending` program the crate-assembly fixtures analyze.
pub(crate) fn lending_crate() -> SolanaSourceUnit {
    SolanaSourceUnit {
        dir: "p".into(),
        package: "lending_program".into(),
        lib: "lending_program".into(),
        anchor: "".into(),
    }
}

/// A spec for the `vault` program: `idl_at` is the IDL's workdir-relative path, or empty for the
/// crate path.
pub(crate) fn spec_of(cr: SolanaSourceUnit, idl_at: &str) -> HarnessSpec {
    HarnessSpec::new("vault", cr, idl_at.to_string())
}

/// The declared flags as they arrive — off the wire, as JSON.
fn declared(v: serde_json::Value) -> DeclaredArgs {
    serde_json::from_value(v).expect("declared args")
}

/// The input the host sends `workspace_prep`: the preflight one, before anything is analyzed.
/// The crate goes through `ChainData` exactly as it does on the wire, so these tests exercise the
/// wheel's own parse of the chain payload rather than a struct the host never sends.
pub(crate) fn prep_input(cr: SolanaSourceUnit, args: serde_json::Value) -> AuthorInput {
    AuthorInput {
        authored: Authored::Preflight,
        program: "vault".into(),
        source_unit: ChainData::of(&cr).expect("an object"),
        props: vec![],
        run_props: vec![],
        setup: None,
        prep_facts: ChainData::default(),
        args: declared(args),
    }
}

/// The plan's request, in the shape this chain agreed with the host — the wheel writes it as
/// `ChainData`, so a test reading it back is checking the same thing the toolchain will.
pub(crate) fn prep_request(plan: &WorkspacePrep) -> SolanaPrep {
    plan.toolchain_request.parse().expect("Solana's own request shape")
}

/// One component's authoring/gating input, for the `lending` program.
pub(crate) fn component_input(slug: &str, name: &str, props: Vec<Property>) -> AuthorInput {
    AuthorInput {
        authored: Authored::Component {
            unit: serde_json::json!({
                "name": name, "slug": slug,
                "instructions": [{ "name": "deposit" }],
            }),
        },
        program: "lending".into(),
        props,
        setup: Some("struct Fixture { ctx: TestContext }".into()),
        ..prep_input(SolanaSourceUnit::default(), serde_json::json!({}))
    }
}

/// A property owned by `component`.
pub(crate) fn owned(component: &str, title: &str, slug: &str) -> Property {
    Property {
        component: component.into(), title: title.into(),
        sort: PropertyKind::Invariant, description: "d".into(), slug: slug.into(),
    }
}

/// [`owned`], for the tests that don't care which component a property belongs to.
pub(crate) fn prop(title: &str, slug: &str) -> Property {
    owned("Withdraw Queue", title, slug)
}

/// The target one component's properties share, exactly as the host builds it from an author's
/// one-check-per-property declaration — explored to budget, which is what the host asks for on any
/// run whose verdicts are reported.
pub(crate) fn target_over(feature: &str, props: &[Property]) -> Target {
    Target {
        name: feature.into(),
        checks: props
            .iter()
            .map(|p| Check {
                name: format!("c_{}", p.slug),
                properties: vec![p.title.clone()],
                target: Some(feature.into()),
            })
            .collect(),
        exploration: Exploration::ToBudget,
    }
}

/// [`target_over`], for the partial run an author iterates against — which the host lets stop at
/// the first finding, so the checks it did not refute were never explored.
pub(crate) fn iterating_target(feature: &str, props: &[Property]) -> Target {
    Target { exploration: Exploration::UntilFirstFinding, ..target_over(feature, props) }
}

/// One verdict set as `(check name, outcome, detail)` rows, in the order the target lists them.
pub(crate) fn verdicts_of(v: &ValidateOutcome) -> Vec<(String, Outcome, String)> {
    let ValidateOutcome::Verdicts { verdicts } = v else { panic!("not a verdict set: {v:?}") };
    verdicts
        .iter()
        .map(|(n, ver)| (n.to_string(), ver.outcome, ver.detail.clone().unwrap_or_default()))
        .collect()
}

/// The same rows' `accounting` — what the run behind each verdict cost and covered, which is a
/// separate field from the `detail` [`verdicts_of`] reads.
pub(crate) fn accounting_of(v: &ValidateOutcome) -> Vec<String> {
    let ValidateOutcome::Verdicts { verdicts } = v else { panic!("not a verdict set: {v:?}") };
    verdicts.iter().map(|(_, ver)| ver.accounting.clone().unwrap_or_default()).collect()
}

/// The outcome set as the host sends it, parsed. Written as JSON rather than built as a struct
/// so these tests also pin the payload shape `finalize` is handed.
pub(crate) fn outcomes(v: serde_json::Value) -> FinalizeInput {
    serde_json::from_value(v).expect("outcome set")
}

/// One delivered component's line in that payload, with every field the wire requires — absence
/// is an error on this seam (`autoprover_sdk::required`), so a fixture spelling only the fields
/// its own test reads would fail to parse. Spelled once here rather than in each fixture, which
/// pins the shape just as well and cannot drift field by field.
pub(crate) fn delivered_component(
    name: &str, targets: &[&str], artifact_text: &str,
) -> serde_json::Value {
    serde_json::json!({
        "name": name,
        "outcome": {
            "status": "delivered",
            "targets": targets,
            "artifact_text": artifact_text,
            "property_checks": [],
            "skipped": [],
            "unit_file": null,
            "run_link": null,
        },
    })
}

/// The deliverable crate's whole file map, for `components` of the `lending` program.
pub(crate) fn delivered_files(components: serde_json::Value) -> BTreeMap<String, String> {
    CrucibleApp.finalize(&outcomes(serde_json::json!({
        "program": "lending",
        "setup": "struct Fixture {}",
        "source_unit": { "dir": "p", "lib": "lending_program",
                         "package": "lending_program", "anchor": "" },
        "prep_facts": {},
        "components": components,
    })))
}

/// The crate root the run writes once, for units with these slugs — what `crate_root` produces
/// from the whole unit set, before any component has authored anything.
pub(crate) fn scaffold(slugs: &[&str]) -> BTreeMap<String, String> {
    CrucibleApp.crate_root(
        &serde_json::from_value(serde_json::json!({
            "program": "lending",
            "setup": "struct Fixture {}",
            "source_unit": { "dir": "p", "lib": "lending_program",
                             "package": "lending_program", "anchor": "" },
            "prep_facts": {},
            "units": slugs.iter().map(|s| serde_json::json!({ "slug": s })).collect::<Vec<_>>(),
        }))
        .expect("crate root input"),
    )
}

/// What one gated build writes for `feature` — the same `section_files` call `compile` and
/// `validate` make.
pub(crate) fn gated(feature: &str, body: &str) -> BTreeMap<String, String> {
    HarnessSpec::new("lending", lending_crate(), String::new()).section_files(feature, body)
}

/// Rendered source with `//` comment lines dropped. The section templates *explain* the
/// `#[cfg]`/`#[invariant_test]` mechanics in prose, so counting tokens over the raw render
/// would count the documentation too.
pub(crate) fn code_only(s: &str) -> String {
    s.lines().filter(|l| !l.trim_start().starts_with("//")).collect::<Vec<_>>().join("\n")
}
