//! What the host sends *into* the authoring/gating callouts, and the prompt it gets back.

use serde::{Deserialize, Serialize};

use crate::args::DeclaredArgs;
use crate::chain::ChainData;

/// What kind of thing a property states (mirrors `composer.spec.types.PropertyType`). An enum
/// rather than a free string for the reason [`Outcome`](crate::outcome::Outcome) is one: the set
/// is closed and shared with the host, so a typo fails to compile instead of reaching a prompt.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
pub enum PropertyKind {
    AttackVector,
    SafetyProperty,
    Invariant,
}

impl PropertyKind {
    /// The wire spelling, which is also how a prompt listing properties refers to one.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::AttackVector => "attack_vector",
            Self::SafetyProperty => "safety_property",
            Self::Invariant => "invariant",
        }
    }
}

impl std::fmt::Display for PropertyKind {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One property to formalize (mirrors `composer.spec.types.PropertyFormulation`), plus the `slug`
/// the host assigned it: unique within the batch, and what a backend names this property's
/// [`Check`](crate::outcome::Check) after (Crucible: `c_<slug>`; the example app: `rule_<slug>`).
///
/// The slug is only guaranteed *filesystem*-safe, which is weaker than identifier-safe — a backend
/// that spells it into generated code folds it first.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct Property {
    pub title: String,
    pub sort: PropertyKind,
    pub description: String,
    pub slug: String,
}

/// What is being authored or gated, and the payload that exists only for it. The host sends three,
/// and each carries something the other two have nothing to say about — which is why this is a
/// variant rather than a tag beside fields that are empty two times in three.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
#[serde(deny_unknown_fields)]
pub enum Authored {
    /// Nothing is authored: the wheel renders its own skeleton and `compile` gates the prepared
    /// workspace (see [`PhaseRole::Preflight`](crate::descriptor::PhaseRole::Preflight)). It runs
    /// before analysis has finished, so there is no model, no unit, and [`AuthorInput::props`] is
    /// empty.
    Preflight,
    /// The shared artifact every unit builds on (Crucible's fixture), authored once from the
    /// analyzed model and *every* unit's properties (see
    /// [`PhaseRole::Setup`](crate::descriptor::PhaseRole::Setup)).
    Setup {
        /// The analyzed system model, opaque to the SDK — its shape is the ecosystem's.
        model: serde_json::Value,
    },
    /// One unit's spec.
    Component {
        /// The unit being formalized, opaque to the SDK — its shape is the ecosystem's
        /// (`FeatureUnit::feature_json`).
        unit: serde_json::Value,
    },
}

/// The input to the authoring/gating callouts for one artifact.
///
/// The one wire type without `#[serde(deny_unknown_fields)]`: serde rejects that attribute
/// alongside a `flatten` field, and [`Authored`] has to flatten so the tag and the payload it
/// selects arrive at the same level as everything else. A field the host declares and this side
/// doesn't is therefore silently ignored *here* — caught by `tests/test_wire_roundtrip.py`, which
/// re-reads what this side made of a payload, rather than at the callout.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
pub struct AuthorInput {
    /// What is being authored, with its payload.
    #[serde(flatten)]
    pub authored: Authored,
    /// The analysis identifier of the program/contract under test (the `Name` half of the host's
    /// `path:Name` argument) — a label and a namespace, never the name of a build-system unit: that
    /// is [`AuthorInput::source_unit`], and the two are independent.
    pub program: String,
    /// Where the analyzed code lives as a unit of *its own* build system, resolved once per run by
    /// the chain's registered `ProjectToolchain` and carried unchanged from here on — so prep, every
    /// gated build and the delivered artifact all name the same thing.
    ///
    /// [Chain-shaped](ChainData): a Cargo crate is a directory, a package name, a lib target and an
    /// Anchor requirement; a Move package is not. Empty when nothing was resolved, which is when a
    /// wheel applies its own convention.
    pub source_unit: ChainData,
    /// The properties this artifact must make checkable. Empty for a preflight; for a setup, every
    /// unit's.
    pub props: Vec<Property>,
    /// The compiled shared setup artifact, for a wheel that declared a
    /// [`PhaseRole::Setup`](crate::descriptor::PhaseRole::Setup) phase — the fixture a component's
    /// spec builds on.
    #[serde(deserialize_with = "crate::required::present")]
    pub setup: Option<String>,
    /// What workspace prep established, from the wheel's own
    /// [`WorkspacePrep::toolchain_request`](crate::prep::WorkspacePrep::toolchain_request) —
    /// [chain-shaped](ChainData) like [`AuthorInput::source_unit`], and produced by the same
    /// `ProjectToolchain`. Empty when the plan asked for nothing beyond placing files.
    ///
    /// A fact here means *the thing it describes is in place*, which is what a wheel reads to decide
    /// how it sources the program's types (Solana: a generated IDL, or a dependency on the crate).
    pub prep_facts: ChainData,
    /// The run's values for the wheel's own declared flags.
    pub args: DeclaredArgs,
}

impl AuthorInput {
    /// The unit being formalized, on a component turn. `None` on the two turns that formalize no
    /// unit — a preflight (nothing is analyzed yet) and the shared setup artifact.
    pub fn unit(&self) -> Option<&serde_json::Value> {
        match &self.authored {
            Authored::Component { unit } => Some(unit),
            _ => None,
        }
    }

    /// The analyzed system model, on a setup turn.
    pub fn model(&self) -> Option<&serde_json::Value> {
        match &self.authored {
            Authored::Setup { model } => Some(model),
            _ => None,
        }
    }
}

/// An authoring instruction (+ optional backend-defined system prompt) for one LLM turn.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct Prompt {
    #[serde(deserialize_with = "crate::required::present")]
    pub system: Option<String>,
    pub instruction: String,
}
