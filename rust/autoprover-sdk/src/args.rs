//! The run's inputs as the entry point resolved them — what the two argument-shaped callouts
//! ([`Backend::validate_preconditions`](crate::Backend::validate_preconditions) and
//! [`Backend::sandbox_grants`](crate::Backend::sandbox_grants)) receive.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

use crate::chain::ChainData;

/// The parsed values of the descriptor's declared CLI flags, keyed the way the host's argparse
/// keys them: leading dashes stripped and `-` folded to `_` (`--fuzz-timeout` → `fuzz_timeout`).
///
/// Untyped because the *wheel* declares these flags ([`ArgSpec`](crate::descriptor::ArgSpec)) —
/// the host parses and forwards them without a schema of its own. Read one through
/// [`DeclaredArgs::get`] rather than reaching into the map.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(transparent)]
pub struct DeclaredArgs(serde_json::Map<String, serde_json::Value>);

impl DeclaredArgs {
    /// One declared flag's value as a `T`. `None` when the flag wasn't declared, was left at a
    /// null default, or doesn't hold a `T` — all three being "the operator gave me nothing", which
    /// is what a caller's `unwrap_or` handles.
    pub fn get<T: serde::de::DeserializeOwned>(&self, dest: &str) -> Option<T> {
        serde_json::from_value(self.0.get(dest)?.clone()).ok()
    }

    /// A non-empty string flag. `--flag ""` and an absent flag are the same answer here, which is
    /// the check a caller reaching for [`DeclaredArgs::get`] on a path/name flag actually wants.
    pub fn text(&self, dest: &str) -> Option<String> {
        self.get::<String>(dest).filter(|s| !s.is_empty())
    }
}

impl From<serde_json::Map<String, serde_json::Value>> for DeclaredArgs {
    fn from(map: serde_json::Map<String, serde_json::Value>) -> Self {
        DeclaredArgs(map)
    }
}

/// The run's resolved inputs. Every part the host already knows is a field: a callout never has to
/// re-derive one by splitting a joined string (`program` and `source_path` are the two halves of
/// the entry point's `path:Name` argument, split here rather than there).
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[cfg_attr(feature = "fuzz", derive(arbitrary::Arbitrary))]
#[serde(deny_unknown_fields)]
pub struct AppArgs {
    /// The project root, absolute.
    pub project_root: PathBuf,
    /// The analysis identifier of the program/contract under test — a label and a namespace, never
    /// the name of a build-system unit: see [`AppArgs::source_unit`].
    pub program: String,
    /// The main source file, project-root-relative.
    pub source_path: String,
    /// The design doc, when one was named on the command line.
    #[serde(deserialize_with = "crate::required::present")]
    pub system_doc: Option<String>,
    /// Where the analyzed code lives as a unit of its own build system —
    /// [chain-shaped](ChainData), and the same value every later callout receives as
    /// [`AuthorInput::source_unit`](crate::authoring::AuthorInput::source_unit). Empty when nothing
    /// was resolved.
    ///
    /// These two callouts run before workspace prep, so there are no prep facts to accompany it.
    pub source_unit: ChainData,
    /// The wheel's own declared flags.
    pub declared: DeclaredArgs,
}
