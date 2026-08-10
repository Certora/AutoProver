//! What the harness crate's parts are called, and where they sit: the paths a run writes, and the
//! names one unit produces — its Cargo feature, its crate-root entry fn, its module and section
//! file, and its label in a prompt.
//!
//! Every name that varies across a run is derived here, from the slug the host put on the unit, so
//! the deliverable's feature names cannot drift from the report's row names.

use autoprover_sdk::authoring::AuthorInput;
use autoprover_sdk::chain::ChainData;

/// The namespace every **component's** build target lives in. Prefixed onto the unit's slug by
/// [`feature_of`], which is the only thing that names a component target — so every Cargo feature,
/// crate-root entry fn, module and section file derived from a unit starts with this, and nothing
/// else in the crate may.
///
/// That is what keeps the wheel's own scaffolding ([`PREFLIGHT_FEATURE`], [`PREFLIGHT_ROOT`]) from
/// colliding with a component: the two namespaces are disjoint by construction rather than by everyone
/// remembering. A component named "preflight" is not a special case — it slugs to `c_preflight`,
/// which is simply a different target from the scaffolding's `preflight`. Pinned by
/// `the_scaffolding_and_component_namespaces_cannot_collide`.
pub(crate) const COMPONENT_PREFIX: &str = "c_";

/// Fallback harness-fn name, used when the host sends a unit with no `slug` — i.e. a single-unit
/// host, where a collision is impossible by construction. A multi-unit host (the Solana ecosystem's
/// per-component units) always supplies one; see [`harness_fn`]. In the component namespace, because
/// it names a component's target.
pub(crate) const DEFAULT_HARNESS_FN: &str = "c_invariants";

/// The authored fn inside every section module — the same name in every component.
///
/// Constant on purpose: the author is asked for one fixed signature and so can never name an fn
/// that no build selects, which is the failure the per-component fn name used to invite (the cheat
/// sheet and the instruction each had to be told the varying name, and disagreeing was silent). The
/// varying `c_<slug>` name survives only in the wheel-generated crate-root entry that delegates
/// here — the model writes a constant, the wheel owns every name that varies.
pub(crate) const SECTION_FN: &str = "invariants";

/// The feature every gate builds under, and an entry of the delivered crate like any component's.
/// Its body drives no state — what it proves is that the fixture compiles and its program loads — so
/// it is the check a user re-runs to sanity-test the harness:
/// `crucible run <program> preflight --dry-run`.
///
/// Deliberately **outside** [`COMPONENT_PREFIX`], so a component named "preflight" (which slugs to
/// `c_preflight`) is a different target rather than a silent overwrite of this one.
pub(crate) const PREFLIGHT_FEATURE: &str = "preflight";

/// The `[[bin]]` path the preflight gate builds: a crate of exactly one file. The preflight runs
/// before analysis, so nothing that belongs in [`CRATE_ROOT`] exists yet — and writing that path
/// here would leave a half-crate at the deliverable's own root whenever a run died early.
///
/// Same bin *name* as [`CRATE_ROOT`], different path: `crucible run` only ever executes
/// `target/<profile>/invariant_test` (`find_fuzz_binary` in the CLI), so the name is fixed while the
/// path is ours to choose. The manifest repoints `[[bin]]` at [`CRATE_ROOT`] from the setup gate on,
/// leaving this file behind as the record of what the preflight proved.
pub(crate) const PREFLIGHT_ROOT: &str = "src/preflight.rs";

/// The `[[bin]]` path of the deliverable's crate root, written by the setup gate (the first callout
/// holding both the fixture and the unit set) and re-emitted identically by
/// [`Backend::crate_root`](autoprover_sdk::Backend::crate_root).
pub(crate) const CRATE_ROOT: &str = "src/main.rs";

/// The directory holding the run's harness crates, relative to the project root.
///
/// Under this backend's deliverable dir rather than crucible's own default (`./fuzz/`), so a run
/// leaves ONE tree: the crate next to the reports and metadata rendered from the same run. The CLI
/// defaults to `./fuzz/<program>/`, so every invocation passes `-C`
/// ([`HarnessSpec::dir`](crate::harness::HarnessSpec::dir)).
pub(crate) const HARNESS_ROOT: &str = "certora/crucible/fuzz";

/// The harness crate for `program`, relative to the project root.
pub(crate) fn harness_dir(program: &str) -> String {
    format!("{HARNESS_ROOT}/{program}")
}

/// The path from a harness crate back to the project root, with a trailing separator.
///
/// `crucible run` chdirs into the crate, so every relative path the crate itself names — the
/// program's `.so`, the path dep on its sources — resolves from there. Derived from
/// [`HARNESS_ROOT`] (plus the per-program directory under it) so moving the crate carries them
/// along instead of stranding them at the old depth.
pub(crate) fn to_project_root() -> String {
    "../".repeat(HARNESS_ROOT.split('/').count() + 1)
}

/// The unit's human label, for the authoring/judge prompts. Falls back to the program name for a
/// host that sends no component (a single-unit host).
pub(crate) fn unit_name(input: &AuthorInput) -> String {
    match input.unit().and_then(|u| u.get("name")).and_then(|v| v.as_str()) {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => input.program.clone(),
    }
}

/// The part of the host's unit object this wheel reads. The unit carries much more (display name,
/// instructions, rationale); the slug is the part that decides what the build target is called.
#[derive(serde::Deserialize, Default)]
struct UnitRef {
    #[serde(default)]
    slug: String,
}

/// The `#[invariant_test]` fn holding ALL of one component's properties — one harness fn, one
/// build, one fuzz run per component (docs/crucible-component-units.md §8.1). The Crucible macro
/// self-gates `main()` by fn name == feature, so this doubles as the Cargo feature and the
/// `crucible run <program> <feature>` target selector.
///
/// Named from the unit's own slug, which the host puts on the unit object (Solana's
/// `SolanaComponentInstance.feature_json()["slug"]`). Deliberately *not* re-derived by slugifying
/// the display name: that would put the same slug rule in two languages and let the deliverable's
/// feature names drift from the report's. What this *does* own is spelling that slug as a Rust
/// identifier — see [`ident_of`].
///
/// Empty slug ⇒ [`DEFAULT_HARNESS_FN`]: a single-unit host, where a collision is impossible by
/// construction.
pub(crate) fn feature_of(slug: &str) -> String {
    if slug.is_empty() {
        DEFAULT_HARNESS_FN.to_string()
    } else {
        format!("{COMPONENT_PREFIX}{}", ident_of(slug))
    }
}

/// [`feature_of`] for a unit as the run-level callouts receive it — `crate_root` naming the features
/// up front, and `finalize` naming the one behind a component that gave up. Same rule as
/// [`harness_fn`], reached from the other direction, so the crate root's declarations and the gated
/// builds' selectors cannot disagree.
pub(crate) fn feature_of_unit(unit: &ChainData) -> String {
    feature_of(&unit.parse::<UnitRef>().unwrap_or_default().slug)
}

/// [`feature_of`] for the unit on an authoring/gating callout.
pub(crate) fn harness_fn(input: &AuthorInput) -> String {
    let unit = input
        .unit()
        .and_then(|u| serde_json::from_value::<UnitRef>(u.clone()).ok())
        .unwrap_or_default();
    feature_of(&unit.slug)
}

/// A host slug spelled as a Rust identifier fragment: lowercased, with every character that isn't
/// `[a-z0-9_]` folded to `_`.
///
/// The host's slug is only guaranteed **filesystem**-safe, which is weaker than identifier-safe —
/// its `slugify_filename` permits `A-Za-z0-9_-`. Two consequences the raw slug would otherwise
/// carry into the deliverable:
///
///  * **`-` is a syntax error in a fn name.** A component named "Admin-Config" slugs to
///    `Admin-Config`, and `fn c_Admin-Config` does not parse. A Cargo *feature* may contain `-`,
///    so the manifest would look fine while `src/main.rs` failed to compile.
///  * **Capitals trip `non_snake_case`** and, worse, leak into what a user types: the fn name is
///    also the Cargo feature and the `crucible run <program> <fn>` selector, matched exactly.
///
/// Callers prefix `c_`, so a slug starting with a digit is already safe.
fn ident_of(slug: &str) -> String {
    slug.chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '_' { c.to_ascii_lowercase() } else { '_' })
        .collect()
}

#[cfg(test)]
mod tests {
    //! The names one unit produces, and the namespace split that keeps them off the wheel's own.
    use super::*;
    use crate::app::CrucibleApp;
    use crate::harness::HarnessSpec;
    use crate::testkit::{at, component_input, prop, scaffold};
    use autoprover_sdk::authoring::Authored;
    use autoprover_sdk::Backend;
    use autoprover_solana::SolanaSourceUnit;

    #[test]
    fn the_harness_fn_comes_from_the_unit_slug_not_a_re_slugified_name() {
        // The host owns the slug rule (Python's `slugify_filename`); re-deriving it here would let
        // the deliverable's feature names drift from the report's row names.
        let input = component_input("withdraw_queue", "Withdraw Queue", vec![]);
        assert_eq!(harness_fn(&input), "c_withdraw_queue");
        assert_eq!(unit_name(&input), "Withdraw Queue");
    }

    #[test]
    fn the_harness_fn_is_a_valid_snake_case_rust_ident() {
        // The host slug is only filesystem-safe (`slugify_filename` permits `A-Za-z0-9_-`), which
        // is weaker than ident-safe. Both of these reached the delivered crate before the fix:
        // capitals (a `non_snake_case` warning, and case-sensitive in what the user types) and
        // `-` (`fn c_Admin-Config` is a syntax error, though the Cargo feature would look fine).
        let capitals = component_input("Vault_Initialization", "Vault Initialization", vec![]);
        assert_eq!(harness_fn(&capitals), "c_vault_initialization");

        let hyphen = component_input("Admin-Config", "Admin-Config", vec![]);
        assert_eq!(harness_fn(&hyphen), "c_admin_config");

        // The fn name IS the Cargo feature and the `crucible run` selector, so every producer of
        // it must agree — units()' target included.
        let app = CrucibleApp;
        let unit = component_input("Withdraw-Queue", "Withdraw Queue", vec![prop("fifo", "fifo")]);
        assert_eq!(app.checks(&unit)[0].target_or_name(), "c_withdraw_queue");

        for ident in ["c_vault_initialization", "c_admin_config", "c_withdraw_queue"] {
            let body = ident.strip_prefix("c_").unwrap();
            assert!(
                body.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_'),
                "{ident} is not a snake_case ident",
            );
        }
    }

    #[test]
    fn a_unit_without_a_slug_falls_back_to_the_single_harness_name() {
        // Single-unit hosts (and older payloads) send no slug; collision is impossible there.
        let mut input = component_input("", "", vec![]);
        input.authored =
            Authored::Component { unit: serde_json::json!({ "instructions": [] }) };
        assert_eq!(harness_fn(&input), DEFAULT_HARNESS_FN);
        assert_eq!(unit_name(&input), "lending");
    }

    #[test]
    fn the_scaffolding_and_component_namespaces_cannot_collide() {
        // Every component target is `feature_of(slug)`, which prefixes COMPONENT_PREFIX — so keeping
        // the wheel's own names OUT of that prefix makes a collision impossible rather than
        // improbable. Without this, a component named "preflight" would slug to the preflight's own
        // feature, and declare it twice in a crate whose section file for it is the preflight root.
        for slug in ["preflight", "main", "invariants", "Preflight", "pre-flight", "a"] {
            assert!(
                feature_of(slug).starts_with(COMPONENT_PREFIX),
                "component target {slug:?} escaped the component namespace",
            );
        }
        assert!(feature_of("").starts_with(COMPONENT_PREFIX), "the fallback is a component target");
        // …and the scaffolding sits outside it, so nothing a unit is named can reach these.
        assert!(!PREFLIGHT_FEATURE.starts_with(COMPONENT_PREFIX));
        assert!(!PREFLIGHT_ROOT.starts_with(&format!("src/{COMPONENT_PREFIX}")));
        // No component's section file can land on the preflight root, which is the one source file
        // in the crate that is not a section.
        let spec = HarnessSpec::new("lending", SolanaSourceUnit::default(), String::new());
        for slug in ["preflight", "main", "a"] {
            assert_ne!(
                at("lending", PREFLIGHT_ROOT),
                spec.section_path(&feature_of(slug)),
                "a component named {slug:?} would overwrite the preflight root",
            );
        }
    }

    #[test]
    fn a_component_named_preflight_is_a_different_target_from_the_preflight() {
        // The collision this namespace split exists to prevent, end to end.
        let files = scaffold(&["preflight"]);
        let main_rs = &files[&at("lending", "src/main.rs")];
        // Two distinct entries: the wheel's is inline, the component's delegates into its module.
        assert!(main_rs.contains("fn preflight(fixture: &mut Fixture)"), "{main_rs}");
        assert!(main_rs.contains("mod c_preflight;"), "{main_rs}");
        assert!(!main_rs.contains("mod preflight;"), "the wheel's entry has no section:\n{main_rs}");
        // …and the component's section file is its own to author, at a path of its own.
        assert!(!files.contains_key(&at("lending", "src/c_preflight.rs")), "the unit authors its own");
        // …and two distinct features, so the manifest declares each once.
        if let Some(cargo) = files.get(&at("lending", "Cargo.toml")) {
            assert_eq!(cargo.matches("preflight = []").count(), 2, "{cargo}");
            assert!(
                cargo.contains("\npreflight = []") && cargo.contains("\nc_preflight = []"),
                "{cargo}",
            );
        }
    }
}
