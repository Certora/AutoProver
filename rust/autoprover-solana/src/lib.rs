//! # autoprover-solana
//!
//! Solana's half of the project seam: the types a wheel targeting Solana and the host's Solana
//! `ProjectToolchain` (`composer/rustapp/toolchain.py`) share across
//! [`ChainData`](autoprover_sdk::chain::ChainData).
//!
//! The SDK carries these payloads without a schema, on purpose — a Cargo package name, a lib target
//! and an `anchor-lang` requirement are one ecosystem's vocabulary, and a framework that declared
//! them would make the next ecosystem an edit to the framework. So the vocabulary lives here, once
//! per chain rather than once per wheel: Crucible's fuzz harness and a future CVLR backend read the
//! same Cargo manifest and build the same program.
//!
//! Three types, one per direction of the seam:
//!
//! * [`SolanaSourceUnit`] — what the host resolved about the analyzed crate
//!   ([`AuthorInput::source_unit`](autoprover_sdk::authoring::AuthorInput::source_unit)).
//! * [`SolanaPrep`] — what a wheel asks the toolchain to do
//!   ([`WorkspacePrep::toolchain_request`](autoprover_sdk::prep::WorkspacePrep::toolchain_request)).
//! * [`SolanaPrepFacts`] — what that prep established
//!   ([`AuthorInput::prep_facts`](autoprover_sdk::authoring::AuthorInput::prep_facts)).

use autoprover_sdk::authoring::AuthorInput;
use autoprover_sdk::chain::ChainData;
use serde::{Deserialize, Serialize};

/// Where the code under analysis lives as a Cargo compilation unit, for a wheel that must *depend*
/// on it (Crucible's harness declares the program under test as a path dependency).
///
/// The host resolves it from the main source file's manifest (`composer.spec.cargo`) because none of
/// the parts follows from the analysis identifier: that is a label, while a crate's directory,
/// package name and lib name are independent of each other and of it (a real lending program we hit
/// had directory `programs/lend`, package `example-lending`, lib `example_lending`).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SolanaSourceUnit {
    /// The crate directory relative to the project root, forward-slashed (`"."` = the root).
    pub dir: String,
    /// `[package] name` — the key a dependent's `[dependencies]` must use.
    pub package: String,
    /// The lib target name — the Rust identifier (`use <lib>::*`) and the built artifact's basename
    /// (`target/deploy/<lib>.so`). NOT interchangeable with `package`, which may contain `-`.
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
    pub anchor: String,
}

impl SolanaSourceUnit {
    /// The analyzed crate as this wheel should read it: what the host resolved, with anything it
    /// could not resolve filled in from the conventional layout.
    ///
    /// The one place a Solana wheel needs for the whole seam. Empty project facts (no toolchain
    /// registered, an unreadable layout) and another chain's facts both land on the convention here —
    /// a wheel that wants to tell those apart reads
    /// [`ChainData`](autoprover_sdk::chain::ChainData) itself.
    pub fn from_input(input: &AuthorInput) -> Self {
        Self::of(&input.source_unit, &input.program)
    }

    /// As [`SolanaSourceUnit::from_input`], for the two payloads that carry project facts without
    /// being an `AuthorInput`: [`AppArgs`](autoprover_sdk::args::AppArgs) before the run starts, and
    /// [`FinalizeInput`](autoprover_sdk::finalize::FinalizeInput) at the end of it. All three must
    /// read one value — what ships has to be what was checked — which is why they share a function
    /// rather than each unwrapping their own way.
    pub fn of(data: &ChainData, program: &str) -> Self {
        data.parse::<Self>().unwrap_or_default().resolved(program)
    }

    /// This unit with every empty part filled from the conventional layout: the crate sits at
    /// `programs/<program>` and is named `<program>`.
    ///
    /// That fallback only holds for a workspace whose directory and package names happen to match the
    /// analysis identifier — it is what a wheel gets when nothing was resolved, not something to rely
    /// on. Idempotent, so calling it twice gives the same value back.
    pub fn resolved(&self, program: &str) -> Self {
        let package =
            if self.package.is_empty() { program.to_string() } else { self.package.clone() };
        Self {
            dir: if self.dir.is_empty() { format!("programs/{program}") } else { self.dir.clone() },
            lib: if self.lib.is_empty() { package.replace('-', "_") } else { self.lib.clone() },
            package,
            anchor: self.anchor.clone(),
        }
    }

    /// The compatibility unit of the crate's declared `anchor-lang` requirement — the major, or
    /// `(0, minor)` for a `0.x` release, since Cargo treats a `0.x` minor as a major. `None` when no
    /// requirement is declared or it isn't a plain version (a git/path dep, say).
    ///
    /// Compare it against the wheel's own `anchor-lang` to decide whether depending on the program
    /// crate is even possible (see the [`anchor`](SolanaSourceUnit::anchor) field).
    pub fn anchor_compat(&self) -> Option<(u64, u64)> {
        anchor_compat_key(&self.anchor)
    }
}

/// The `(major, minor-if-0.x)` compatibility unit of a version requirement: leading operators and
/// whitespace are dropped (`">=0.31"` → `(0, 31)`, `"^1.0.1"` → `(1, 0)`), and anything that isn't a
/// plain `major[.minor]` is `None`.
pub fn anchor_compat_key(req: &str) -> Option<(u64, u64)> {
    let digits = req.trim_start_matches(['^', '~', '=', '>', '<', ' ']);
    let mut parts = digits.split(['.', ',', ' ', '-', '+']);
    let major: u64 = parts.next()?.parse().ok()?;
    let minor: u64 = parts.next().unwrap_or("0").parse().unwrap_or(0);
    Some(if major == 0 { (0, minor) } else { (major, 0) })
}

/// What a wheel asks the host's Solana toolchain to do, beyond writing the plan's files. The wheel
/// declares it; the host executes it — no command line crosses the seam, so the network posture stays
/// Python-owned (`docs/command-sandbox.md` §5).
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct SolanaPrep {
    /// Project-relative manifest dirs to `cargo fetch` (unconfined, network) so a later confined +
    /// offline build finds every dep warm in the private `CARGO_HOME`.
    pub warm_dirs: Vec<String>,
    /// If set, build the workspace and expect this artifact (`cargo-build-sbf` →
    /// `target/deploy/<name>.so`). It names the *build artifact*, so it is the crate's
    /// [`lib`](SolanaSourceUnit::lib) target — not the analysis identifier, which need not match it.
    pub build_program: Option<String>,
    /// If set, the wheel needs the program's **IDL** and wants it at this workdir-relative path. The
    /// host obtains it (an operator-supplied file, else `anchor idl build`) and reports where it
    /// landed as [`SolanaPrepFacts::idl`]. A hard error if it can't be produced: a wheel only asks
    /// when it cannot proceed without one.
    ///
    /// This is what lets a harness target a program whose toolchain it can't link against (see
    /// [`anchor`](SolanaSourceUnit::anchor)): types generated from the IDL belong to the *wheel's*
    /// stack, so the program's own dependency graph never enters the harness build.
    pub idl_dest: Option<String>,
}

/// What the Solana prep established. Every field is `#[serde(default)]` because "the prep placed
/// nothing" is spelled as an empty object on the wire — one absent-means-nothing rule for the whole
/// chain payload, rather than a null per field the host would have to remember to send.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct SolanaPrepFacts {
    /// Where the program's IDL was placed, project-root-relative. `Some` means the file *is in
    /// place*, which is the signal a wheel reads to decide how it sources the program's types;
    /// `None` means it depends on the program's crate directly.
    pub idl: Option<String>,
}

impl SolanaPrepFacts {
    /// What the prep established, as this wheel should read it — nothing established and facts this
    /// wheel doesn't recognize both read as "no IDL", which is the state it must handle anyway.
    pub fn from_input(input: &AuthorInput) -> Self {
        Self::of(&input.prep_facts)
    }

    /// As [`SolanaPrepFacts::from_input`], for the outcome set `finalize` renders from — the
    /// deliverable must source its types the way the gated builds did.
    pub fn of(data: &ChainData) -> Self {
        data.parse().unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use autoprover_sdk::chain::ChainData;

    /// One component input carrying the given project facts, as the host sends it.
    fn input_with(source_unit: &str, prep_facts: &str) -> AuthorInput {
        serde_json::from_str(&format!(
            r#"{{"kind":"component","program":"vault","unit":{{}},
                 "source_unit":{source_unit},"prep_facts":{prep_facts},
                 "props":[],"run_props":[],"setup":null,"args":{{}}}}"#
        ))
        .expect("parse")
    }

    #[test]
    fn a_resolved_crate_reaches_the_wheel_untouched() {
        // The lend shape: every part differs from the analysis identifier, and the convention would
        // have got every one of them wrong.
        let unit = SolanaSourceUnit::from_input(&input_with(
            r#"{"dir":"programs/lend","package":"example-lending","lib":"example_lending",
                "anchor":"0.29.0"}"#,
            "{}",
        ));
        assert_eq!(
            (unit.dir.as_str(), unit.package.as_str(), unit.lib.as_str()),
            ("programs/lend", "example-lending", "example_lending")
        );
        assert_eq!(unit.anchor_compat(), Some((0, 29)));
    }

    #[test]
    fn an_unresolved_crate_falls_back_to_the_anchor_layout() {
        // What the host sends when no toolchain is registered for the chain, or the layout couldn't
        // be read. The convention is this crate's to apply — the SDK has none.
        let unit = SolanaSourceUnit::from_input(&input_with("{}", "{}"));
        assert_eq!(unit.dir, "programs/vault");
        assert_eq!(unit.package, "vault");
        assert_eq!(unit.lib, "vault");
        assert_eq!(unit.anchor_compat(), None, "an unresolved crate declares no Anchor");
    }

    #[test]
    fn a_package_name_is_mangled_into_a_lib_name_only_when_one_is_missing() {
        let dashed = SolanaSourceUnit { package: "example-lending".into(), ..Default::default() };
        assert_eq!(dashed.resolved("vault").lib, "example_lending");
        // Idempotent: the host re-sends what it resolved, and every callout must agree on the value.
        let once = dashed.resolved("vault");
        assert_eq!(once.resolved("vault"), once);
    }

    #[test]
    fn an_anchor_requirement_is_read_as_a_compatibility_unit() {
        // A 0.x minor is a major to Cargo, so 0.29 and 0.31 are incompatible; past 1.0 the minor
        // stops mattering. Anything that isn't a plain version (a git or path dep) is unknown
        // rather than guessed.
        assert_eq!(anchor_compat_key("0.29.0"), Some((0, 29)));
        assert_eq!(anchor_compat_key(">=0.31"), Some((0, 31)));
        assert_eq!(anchor_compat_key("^1.0.1"), Some((1, 0)));
        assert_eq!(anchor_compat_key(""), None);
        assert_eq!(anchor_compat_key("git:https://…"), None);
    }

    #[test]
    fn a_prep_request_and_what_it_established_travel_as_chain_data() {
        let request = SolanaPrep {
            warm_dirs: vec!["fuzz/vault".into()],
            build_program: Some("example_lending".into()),
            idl_dest: Some("fuzz/vault/idls/example_lending.json".into()),
        };
        let carried = ChainData::of(&request).expect("an object");
        let back: SolanaPrep = carried.parse().expect("the same shape");
        assert_eq!(back.idl_dest.as_deref(), Some("fuzz/vault/idls/example_lending.json"));

        let placed = SolanaPrepFacts::from_input(&input_with(
            "{}",
            r#"{"idl":"fuzz/vault/idls/example_lending.json"}"#,
        ));
        assert_eq!(placed.idl.as_deref(), Some("fuzz/vault/idls/example_lending.json"));
        // An empty object is the whole spelling of "the prep established nothing".
        assert_eq!(SolanaPrepFacts::from_input(&input_with("{}", "{}")).idl, None);
    }
}
