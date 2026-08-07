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
    /// `property_checks`: the publish gate rejects a mapping that claims a skipped property.
    pub skipped: Vec<SkippedProperty>,
    #[serde(deserialize_with = "crate::required::present")]
    pub unit_file: Option<String>,
    #[serde(deserialize_with = "crate::required::present")]
    pub run_link: Option<String>,
}

/// Whether a component reached the deliverable. An enum rather than a `delivered` flag beside
/// always-present fields: the two outcomes share no data — a component that gave up has no spec, no
/// targets and no checks, and what it does have (the unit, the reason) means nothing for one that
/// delivered.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub enum ComponentOutcome {
    Delivered(Delivered),
    /// Formalization gave up on this component: the author reached the point where anything it could
    /// publish would only *look* checked, and said so instead.
    GaveUp(GaveUp),
}

/// What a component that gave up contributes: not a spec, but enough to say so in the deliverable.
///
/// A [`Delivered`] component is keyed by the targets its gated builds actually selected. One that
/// gave up ran no build, so it has no targets — which is why it carries its unit instead. Re-deriving
/// a key from the display name would put the host's slug rule in a second language, and the wheel
/// already owns unit → build-target naming.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct GaveUp {
    /// The unit that was being formalized — the same [chain-shaped](ChainData) value its component
    /// callouts received.
    pub unit: ChainData,
    /// The author's own account of why it stopped, as recorded by the give-up tool. Reported to the
    /// user, so it must never be paraphrased into something that reads like a finding.
    pub reason: String,
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
/// setup spec, and every component's result.
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
    /// The compiled shared setup spec, when the wheel declared a
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
            ComponentOutcome::GaveUp(_) => None,
        })
    }

    /// The components formalization gave up on, as `(name, gave_up)`. A deliverable that declares a
    /// build target per unit needs these: the target exists either way, and what a user gets when
    /// they select it should say why there is nothing behind it.
    pub fn gave_up(&self) -> impl Iterator<Item = (&str, &GaveUp)> {
        self.components.iter().filter_map(|c| match &c.outcome {
            ComponentOutcome::GaveUp(g) => Some((c.name.as_str(), g)),
            ComponentOutcome::Delivered(_) => None,
        })
    }
}
