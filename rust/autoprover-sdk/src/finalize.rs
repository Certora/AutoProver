//! The full outcome set [`Backend::finalize`](crate::Backend::finalize) renders run-level
//! artifacts from.

use serde::{Deserialize, Serialize};

use crate::chain::ChainData;
use crate::outcome::SkippedProperty;

/// What one component's formalization produced, when it produced anything.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct Delivered {
    /// The authored spec, verbatim — the source a
    /// [`DeliverableMode::Callout`](crate::descriptor::DeliverableMode::Callout) wheel assembles its
    /// deliverable from.
    pub artifact_text: String,
    /// The validation targets this component's checks ran under, in the order they ran. The key a
    /// callout-mode wheel writes its sections under: they are what the gated builds selected,
    /// unlike `property_checks`, which are report rows and gate nothing.
    pub targets: Vec<String>,
    /// Each property title and the names of the checks carrying it — the report's property→check
    /// map.
    pub property_checks: Vec<(String, Vec<String>)>,
    /// The properties the author declined to formalize, each with its justification. Disjoint from
    /// `property_units`: the publish gate rejects a mapping that claims a skipped property.
    pub skipped: Vec<SkippedProperty>,
    #[serde(deserialize_with = "crate::required::present")]
    pub unit_file: Option<String>,
    #[serde(deserialize_with = "crate::required::present")]
    pub run_link: Option<String>,
}

/// Whether a component reached the deliverable. An enum rather than a `delivered` flag beside
/// always-present fields: a component that gave up has no spec, no targets and no checks, so there
/// is nothing for a caller to read past the name.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub enum ComponentOutcome {
    Delivered(Delivered),
    /// Formalization gave up on this component; it contributes nothing to the deliverable.
    GaveUp,
}

/// One component's line in the outcome set.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct FinalizeComponent {
    pub name: String,
    pub outcome: ComponentOutcome,
}

/// The complete outcome set. Everything a wheel needs to render the whole deliverable: the same
/// project facts the gated builds used (so what ships is what was checked), the compiled shared
/// setup artifact, and every component's result.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct FinalizeInput {
    pub program: String,
    /// As every authoring callout received it — see
    /// [`AuthorInput::source_unit`](crate::authoring::AuthorInput::source_unit).
    pub source_unit: ChainData,
    /// As every authoring callout received it — see
    /// [`AuthorInput::prep_facts`](crate::authoring::AuthorInput::prep_facts).
    pub prep_facts: ChainData,
    /// The compiled shared setup artifact, when the wheel declared a
    /// [`PhaseRole::Setup`](crate::descriptor::PhaseRole::Setup) phase.
    #[serde(deserialize_with = "crate::required::present")]
    pub setup: Option<String>,
    pub components: Vec<FinalizeComponent>,
}

impl FinalizeInput {
    /// The components that reached the deliverable, as `(name, result)`.
    pub fn delivered(&self) -> impl Iterator<Item = (&str, &Delivered)> {
        self.components.iter().filter_map(|c| match &c.outcome {
            ComponentOutcome::Delivered(d) => Some((c.name.as_str(), d)),
            ComponentOutcome::GaveUp => None,
        })
    }
}
