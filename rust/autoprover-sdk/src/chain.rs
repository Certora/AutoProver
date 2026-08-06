//! The one shape on this seam the SDK deliberately does not define: a JSON object whose fields
//! belong to the **chain under analysis** rather than to the framework.
//!
//! An application is written in Rust; the project it analyzes need not be. A Cargo crate's package
//! and lib-target names, an Anchor IDL, a Move package's named addresses are all vocabulary of one
//! ecosystem's build system — knowledge the host has no business holding a shape for, since holding
//! one is what makes the next ecosystem an edit to the framework instead of a registration.
//!
//! So the facts travel opaquely. One chain-scoped implementation produces them (`ProjectToolchain`,
//! in `composer/rustapp/toolchain.py`) and the wheels targeting that chain read them, both through
//! the types a chain's support crate defines. This seam guarantees only that the object arrives
//! whole — and *which* type is inside follows from the descriptor's declared
//! [`ecosystem`](crate::descriptor::AppDescriptor::ecosystem), not from inspecting the keys.
//!
//! It is the same treatment [`Authored`](crate::authoring::Authored)'s `model` and `unit` payloads
//! already get, for the same reason: the analyzed system's shape is the ecosystem's.

use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// A JSON object this seam carries but does not interpret — see the module docs.
///
/// `#[serde(transparent)]`, so the wire form is the bare object and the host mirrors it as a plain
/// `dict[str, Any]`; an empty one is how the host says it established nothing. A non-object fails to
/// deserialize, which keeps "opaque" from also meaning "any JSON at all".
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ChainData(Map<String, Value>);

impl ChainData {
    /// These facts as the type that owns their shape.
    ///
    /// A `Result` rather than an `Option`: an empty object and one holding another chain's fields are
    /// different problems — the first is the documented "nothing was resolved" (test it with
    /// [`ChainData::is_empty`]), while the second means a wheel and a toolchain disagree about the
    /// chain, which should be reported rather than read as "nothing". A caller that wants its own
    /// convention for both spells that at the call site (`.parse().unwrap_or_default()`).
    pub fn parse<T: DeserializeOwned>(&self) -> Result<T, serde_json::Error> {
        serde_json::from_value(Value::Object(self.0.clone()))
    }

    /// Nothing was established — no resolver for this chain, no such unit in the language, a layout
    /// that couldn't be read, or a prep that asked for nothing.
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    /// The wire form of a value whose shape the chain owns — how a wheel builds the
    /// [`WorkspacePrep::toolchain_request`](crate::prep::WorkspacePrep::toolchain_request) it wants
    /// executed, from the request type it shares with that chain's toolchain.
    pub fn of<T: Serialize>(value: &T) -> Result<Self, serde_json::Error> {
        match serde_json::to_value(value)? {
            Value::Object(map) => Ok(ChainData(map)),
            other => Err(serde::ser::Error::custom(format!(
                "chain data must serialize to a JSON object, got {other}"
            ))),
        }
    }
}

impl From<Map<String, Value>> for ChainData {
    fn from(map: Map<String, Value>) -> Self {
        ChainData(map)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Stands in for what a chain's support crate defines — the SDK's tests can't name a real one,
    /// which is the property under test.
    #[derive(Debug, Default, PartialEq, Serialize, Deserialize)]
    struct Facts {
        dir: String,
        package: String,
    }

    #[test]
    fn a_chain_shaped_value_round_trips_through_the_opaque_form() {
        let facts = Facts { dir: "programs/lend".into(), package: "example-lending".into() };
        let data = ChainData::of(&facts).expect("an object");
        assert!(!data.is_empty());
        assert_eq!(data.parse::<Facts>().expect("the same shape"), facts);
    }

    #[test]
    fn nothing_resolved_and_the_wrong_chains_facts_are_different_answers() {
        // Both fail to parse, and a wheel has to be able to tell them apart: the first is the
        // documented state a resolver-less chain produces, the second is a wiring bug.
        let nothing = ChainData::default();
        assert!(nothing.is_empty() && nothing.parse::<Facts>().is_err());

        let other_chain: ChainData =
            serde_json::from_str(r#"{"named_addresses":{"vault":"0x1"}}"#).expect("an object");
        assert!(!other_chain.is_empty() && other_chain.parse::<Facts>().is_err());
    }

    #[test]
    fn only_an_object_is_chain_data() {
        // "Opaque" is about the *fields*, not the JSON type: a list or a string here would mean the
        // two ends disagree about more than a field name.
        assert!(serde_json::from_str::<ChainData>("[]").is_err());
        assert!(serde_json::from_str::<ChainData>(r#""idl.json""#).is_err());
        assert!(serde_json::from_str::<ChainData>("{}").expect("an object").is_empty());
    }
}
