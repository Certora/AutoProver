//! What the host sends *into* the authoring/gating callouts, and the prompt it gets back.

use serde::{Deserialize, Serialize};

/// One property to formalize (mirrors `composer.spec.types.PropertyFormulation`), plus a
/// host-assigned unique `slug` used to name its unit/artifact (Crucible: `c_<slug>`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Property {
    pub title: String,
    /// One of "attack_vector" | "safety_property" | "invariant".
    pub sort: String,
    pub description: String,
    #[serde(default)]
    pub slug: String,
}

/// Where the code under analysis lives as a compilation unit, for a wheel that must *depend* on
/// it (Crucible's harness declares the program under test as a path dependency). The host resolves
/// it from the main source file's manifest (`composer.spec.cargo`) because none of the three parts
/// follows from `AuthorInput::program`: that is the *analysis* identifier, while a crate's
/// directory, package name and lib name are independent of each other and of it (a real lending
/// program we hit had directory `programs/lend`, package `example-lending`, lib `example_lending`).
///
/// Every field of one a *backend* receives is populated: the FFI boundary runs
/// [`ProgramCrate::resolved`] on every payload carrying one, so the partly-empty shape a host that
/// resolved nothing sends is never what a callout sees.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ProgramCrate {
    /// The crate directory relative to the project root, forward-slashed (`"."` = the root).
    #[serde(default)]
    pub dir: String,
    /// `[package] name` — the key a dependent's `[dependencies]` must use.
    #[serde(default)]
    pub package: String,
    /// The lib target name — the Rust identifier (`use <lib>::*`) and the built artifact's
    /// basename (`target/deploy/<lib>.so`). NOT interchangeable with `package`, which may
    /// contain `-`.
    #[serde(default)]
    pub lib: String,
    /// The crate's declared `anchor-lang` requirement, verbatim (`"0.29.0"`, `"^1.0.1"`, `">=0.31"`;
    /// workspace inheritance already resolved by the host). Empty when the crate declares none, so
    /// it is not an Anchor program — or the host couldn't tell.
    ///
    /// A *depending* wheel needs this because the dependency is only usable when the program's
    /// Anchor major matches the one the wheel's own stack links: Anchor's generated
    /// `InstructionData` / `ToAccountMetas` impls are tied to the exact `anchor-lang` crate that
    /// generated them, so a program on a different major can't satisfy a harness's trait bounds no
    /// matter how the versions are pinned.
    #[serde(default)]
    pub anchor: String,
}

impl ProgramCrate {
    /// This crate with every empty part filled from the conventional layout: the crate sits at
    /// `programs/<program>` and is named `<program>`. That fallback only holds for a workspace
    /// whose directory and package names happen to match the analysis identifier — it is what a
    /// host that resolves nothing gets, not something to rely on.
    ///
    /// Applied by the FFI boundary to every inbound payload, so a backend calling it again gets
    /// the same value back. Public for a caller building one outside a callout.
    pub fn resolved(&self, program: &str) -> ProgramCrate {
        let package =
            if self.package.is_empty() { program.to_string() } else { self.package.clone() };
        ProgramCrate {
            dir: if self.dir.is_empty() { format!("programs/{program}") } else { self.dir.clone() },
            lib: if self.lib.is_empty() { package.replace('-', "_") } else { self.lib.clone() },
            package,
            anchor: self.anchor.clone(),
        }
    }

    /// The compatibility unit of the crate's declared `anchor-lang` requirement — the major, or
    /// `(0, minor)` for a `0.x` release, since Cargo treats a `0.x` minor as a major. `None` when
    /// no requirement is declared or it isn't a plain version (a git/path dep, say).
    ///
    /// Compare it against the wheel's own `anchor-lang` to decide whether depending on the program
    /// crate is even possible (see the `anchor` field).
    pub fn anchor_compat(&self) -> Option<(u64, u64)> {
        anchor_compat_key(&self.anchor)
    }
}

/// The `(major, minor-if-0.x)` compatibility unit of a version requirement: leading operators and
/// whitespace are dropped (`">=0.31"` → `(0, 31)`, `"^1.0.1"` → `(1, 0)`), and anything that isn't
/// a plain `major[.minor]` is `None`.
pub fn anchor_compat_key(req: &str) -> Option<(u64, u64)> {
    let digits = req.trim_start_matches(['^', '~', '=', '>', '<', ' ']);
    let mut parts = digits.split(['.', ',', ' ', '-', '+']);
    let major: u64 = parts.next()?.parse().ok()?;
    let minor: u64 = parts.next().unwrap_or("0").parse().unwrap_or(0);
    Some(if major == 0 { (0, minor) } else { (major, 0) })
}

/// The input to the authoring/gating callouts for one artifact. `context` carries backend
/// dependencies (e.g. the shared fixture source a component builds on). `component`/`context` are
/// opaque JSON the backend interprets.
///
/// `kind` selects what is being authored or gated. The host sends three:
///
///  * `"preflight"` — nothing is authored; the wheel renders its own skeleton and `compile` gates
///    the prepared workspace (see [`PreflightSpec`](crate::PreflightSpec)). `props` is empty and
///    `component` carries nothing: it runs before analysis has finished.
///  * `"setup"` — the shared artifact every unit builds on (Crucible's fixture), authored once from
///    every unit's properties (see [`SetupSpec`](crate::SetupSpec)). `component` carries the
///    analyzed model.
///  * `"component"` — one unit's spec. `component` carries that unit, `context` the setup artifact.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthorInput {
    pub kind: String,
    /// The analysis identifier of the program/contract under test (the `Name` half of the host's
    /// `path:Name` argument) — a label and a namespace, NOT a Cargo name: see [`ProgramCrate`].
    pub program: String,
    /// The compilation unit holding the program's source, when the host's language has one.
    #[serde(default)]
    pub program_crate: ProgramCrate,
    #[serde(default)]
    pub component: serde_json::Value,
    #[serde(default)]
    pub props: Vec<Property>,
    #[serde(default)]
    pub context: serde_json::Value,
}

/// An authoring instruction (+ optional backend-defined system prompt) for one LLM turn.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Prompt {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub system: Option<String>,
    pub instruction: String,
}

/// Whether a draft was rejected by the toolchain or by the optional LLM judge — so a
/// re-author can frame the retry correctly (a judge rejection is NOT a build failure: the
/// draft compiled). Defaults to `Compile` for backends/hosts that predate the field.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureKind {
    #[default]
    Compile,
    Judge,
}

/// Why a draft was rejected — the failing `draft` plus the compiler errors / judge feedback
/// — fed into the next `author_prompt` as revise context. `draft` is carried because each
/// authoring turn is fresh (no LLM-side memory of the prior attempt). `kind` says which gate
/// rejected it so the revise prompt can distinguish compiler errors from review feedback.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Failure {
    #[serde(default)]
    pub draft: String,
    pub errors: String,
    #[serde(default)]
    pub kind: FailureKind,
}
