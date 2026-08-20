//! `Arbitrary` impls for the wire types whose Rust type is wider than the protocol, so the
//! round-trip fuzzer (`tests/test_wire_roundtrip.py`) generates values the protocol actually
//! permits. A divergence it reports is then drift, not a value neither side ever sends.
//!
//! Everything else derives `Arbitrary` at its definition. What needs a hand-written impl is
//! either a field holding a `serde_json::Value` (no `Arbitrary` impl, and none would know how
//! deep to go) or a field whose wire domain is narrower than its Rust type — the cases below,
//! each of which would otherwise fail as a false positive.

use arbitrary::{Arbitrary, Result, Unstructured};
use serde_json::{Map, Value};

use crate::args::DeclaredArgs;
use crate::authoring::Authored;
use crate::chain::ChainData;
use crate::descriptor::{
    AppDescriptor, ArgSpec, ArtifactLayout, DeliverableMode, EventKind, PhaseSpec,
};
use crate::outcome::{Outcome, Verdict};

/// The chain tags the host resolves against its ecosystem registry (`ChainTag`).
const ECOSYSTEMS: &[&str] = &["evm", "solana", "soroban"];

/// The report vocabularies a descriptor may claim (`ReportBackend`).
const REPORT_BACKENDS: &[&str] = &["prover", "foundry", "none"];

/// How deep a generated opaque payload nests. The host treats these as opaque either way, so
/// depth buys no coverage past the point where "it is a JSON document" is established.
const JSON_DEPTH: u32 = 2;

/// Cap on any generated collection — a few elements reach the same code as a long list.
const MAX_ITEMS: usize = 3;

/// A JSON scalar, minus floats: `NaN`/infinity have no JSON spelling (`serde_json` refuses to
/// serialize them), so drawing one would fail the harness rather than either side of the seam.
fn scalar(u: &mut Unstructured) -> Result<Value> {
    Ok(match u.int_in_range(0..=3)? {
        0 => Value::Null,
        1 => Value::Bool(bool::arbitrary(u)?),
        2 => Value::from(i64::arbitrary(u)?),
        _ => Value::String(String::arbitrary(u)?),
    })
}

/// An arbitrary JSON document, nesting at most `depth` deeper. Weighted toward scalars so a draw
/// terminates well inside the entropy a single example carries.
fn json(u: &mut Unstructured, depth: u32) -> Result<Value> {
    if depth == 0 {
        return scalar(u);
    }
    match u.int_in_range(0..=5)? {
        0..=3 => scalar(u),
        4 => {
            let mut items = Vec::new();
            for _ in 0..u.int_in_range(0..=MAX_ITEMS)? {
                items.push(json(u, depth - 1)?);
            }
            Ok(Value::Array(items))
        }
        _ => Ok(Value::Object(object(u, depth - 1)?)),
    }
}

fn object(u: &mut Unstructured, depth: u32) -> Result<Map<String, Value>> {
    let mut map = Map::new();
    for _ in 0..u.int_in_range(0..=MAX_ITEMS)? {
        map.insert(String::arbitrary(u)?, json(u, depth)?);
    }
    Ok(map)
}

/// Opaque on both sides (the wheel declares the flags, so the host has no schema for them), which
/// makes any JSON object a legal value.
impl<'a> Arbitrary<'a> for DeclaredArgs {
    fn arbitrary(u: &mut Unstructured<'a>) -> Result<Self> {
        Ok(object(u, JSON_DEPTH)?.into())
    }
}

/// The project's own build-system vocabulary, opaque to this seam (`chain::ChainData`) — so, like
/// the declared flags, any JSON object is a legal value.
impl<'a> Arbitrary<'a> for ChainData {
    fn arbitrary(u: &mut Unstructured<'a>) -> Result<Self> {
        Ok(object(u, JSON_DEPTH)?.into())
    }
}

/// The `model` and `unit` payloads are the ecosystem's shape, opaque to this seam.
impl<'a> Arbitrary<'a> for Authored {
    fn arbitrary(u: &mut Unstructured<'a>) -> Result<Self> {
        Ok(match u.int_in_range(0..=2)? {
            0 => Authored::Preflight,
            1 => Authored::Setup {
                model: json(u, JSON_DEPTH)?,
                units: Vec::arbitrary(u)?,
            },
            _ => Authored::Component {
                unit: json(u, JSON_DEPTH)?,
            },
        })
    }
}

/// `duration_seconds` is an `f64`, but a duration is finite: drawn from milliseconds so every
/// value has a JSON spelling and few enough significant digits that the comparison tests the
/// protocol rather than two languages' float formatters.
impl<'a> Arbitrary<'a> for Verdict {
    fn arbitrary(u: &mut Unstructured<'a>) -> Result<Self> {
        let seconds = if bool::arbitrary(u)? {
            Some(u.int_in_range(0..=86_400_000)? as f64 / 1000.0)
        } else {
            None
        };
        Ok(Verdict {
            outcome: Outcome::arbitrary(u)?,
            line: Option::arbitrary(u)?,
            duration_seconds: seconds,
            unit_file: Option::arbitrary(u)?,
            detail: Option::arbitrary(u)?,
            accounting: Option::arbitrary(u)?,
            finding: Option::arbitrary(u)?,
        })
    }
}

/// `ecosystem` and `backend_tag` are `String` here but closed sets on the host, which validates
/// them when it parses the descriptor — that narrowing is deliberate (a wheel claiming a tag the
/// report has no wording for should fail at load, before the run starts), so the round trip draws
/// from the sets rather than reporting every unknown tag as drift.
///
/// `tests/test_wire_roundtrip.py::test_descriptor_rejects_unknown_tags` covers the other half:
/// that a tag outside the set is in fact rejected.
impl<'a> Arbitrary<'a> for AppDescriptor {
    fn arbitrary(u: &mut Unstructured<'a>) -> Result<Self> {
        Ok(AppDescriptor {
            name: String::arbitrary(u)?,
            header_text: String::arbitrary(u)?,
            ecosystem: (*u.choose(ECOSYSTEMS)?).to_string(),
            backend_tag: (*u.choose(REPORT_BACKENDS)?).to_string(),
            backend_guidance: String::arbitrary(u)?,
            analysis_key: String::arbitrary(u)?,
            phases: Vec::<PhaseSpec>::arbitrary(u)?,
            args: Vec::<ArgSpec>::arbitrary(u)?,
            rag_db_default: Option::arbitrary(u)?,
            event_kinds: Vec::<EventKind>::arbitrary(u)?,
            artifact_layout: ArtifactLayout::arbitrary(u)?,
            deliverable_mode: DeliverableMode::arbitrary(u)?,
            serialize_toolchain: bool::arbitrary(u)?,
            confine_by_default: bool::arbitrary(u)?,
            component_noun: Option::arbitrary(u)?,
            check_noun: Option::arbitrary(u)?,
            evidence_kinds: Vec::<String>::arbitrary(u)?,
            findings: Option::arbitrary(u)?,
        })
    }
}
