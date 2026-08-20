//! One component's **section**: the crate-root lines that declare it, and the module file that
//! holds what the author wrote.
//!
//! The two are rendered independently, because they are written at different times — the crate root
//! once per run, the file per callout — and the `#[cfg]` on the declaration is what seals one
//! component's helpers off from another's.

use std::collections::BTreeMap;

use askama::Template;
use autoprover_sdk::finalize::FinalizeInput;

use crate::layout::{CHECK_PREFIX, DEFAULT_HARNESS_FN};
use crate::templates::{GaveUpSection, SectionEntry, SectionFile};

/// The two places one component's section occupies in the crate — rendered independently, because
/// they are now written at different times: the crate root once per run, the file per callout.
pub(crate) struct Section;

impl Section {
    /// The crate root's lines for one component: the `mod`, gated on the union of its checks'
    /// features, and one `#[invariant_test]` entry per check delegating into it.
    ///
    /// Depends only on names — the component's module and its checks', both derived from host slugs
    /// — which is why the root can still be written before any component has authored anything. A
    /// check is named for the property it carries, and the run's properties are known then, so
    /// declaring a target per check needs nothing the author has not yet written.
    pub(crate) fn entry(module: &str, checks: &[String]) -> String {
        SectionEntry { module, checks }.render().expect("render section_entry")
    }

    /// The module file for component `module`, holding the authored `body`.
    ///
    /// Two normalizations are applied to the authored source first, because the entries cannot
    /// reach the body without them:
    ///
    /// * **Any `#[invariant_test]` on an authored fn is dropped.** The prompt asks for bare
    ///   `pub fn`s, but a model that adds the attribute anyway would expand `fn main()` *inside* the
    ///   module, where it is not a binary entry point — a confusing link error rather than a
    ///   compile error the revise loop could act on.
    /// * **Every authored check fn is made `pub`.** They are called from the crate root, so a
    ///   private one is `E0603`. Cheaper to fix here than to spend a revise round on it.
    ///
    /// The second is anchored on [`CHECK_PREFIX`] rather than on one known name, since the author
    /// now writes one fn per check. It rewrites only `fn c_…` at the start of a line: a private
    /// helper the author named otherwise stays private, which is what a helper should be.
    pub(crate) fn file(module: &str, body: &str) -> String {
        let body = body.replace("#[invariant_test]\n", "").replace("#[invariant_test]", "");
        let body = publicize_checks(&body);
        let body = as_module_body(&body);
        SectionFile { module, body: body.trim() }.render().expect("render section_file")
    }

    /// The module file for a component formalization gave up on: no tests, and a `compile_error!`
    /// carrying the author's reason.
    ///
    /// The crate root declares a `mod`/feature per unit before any of them has authored anything, so
    /// the target exists whether or not a suite arrives behind it. What a user selecting it should
    /// meet is an honest refusal — a build-time error naming the gap — and emphatically **not** a
    /// test that fails: `validate` reads a fuzz finding as a refuted property, so a failing test here
    /// would be indistinguishable from a real counterexample against the user's program. The gate is
    /// `#[cfg]`, so this never affects a build of any other feature.
    pub(crate) fn gave_up(module: &str, unit: &str, reason: &str) -> String {
        GaveUpSection { module, unit, reason: &reason.replace('"', "'") }
            .render()
            .expect("render gave_up_section")
    }
}

/// Fix up text authored as though it were a whole file, for placement *inside* one.
///
/// A model writing what looks like a module file naturally opens it with `//!` docs and a
/// `use super::*;`. The prompt asks for neither, but both arrive anyway, and below the header and
/// `use` that `section_file.j2` already supplies they are:
///
/// * **`//!` → `//`.** An inner doc comment is only legal before any item, so an authored one is
///   `E0753: expected outer doc comment`. Observed on *both* components of the first e2e run after
///   sections moved into their own files — the same text was harmless when a section was
///   concatenated at crate root, which is why moving it introduced the failure. Self-healing (the
///   revise loop fixes it) but it costs a round per component, which is exactly what the other
///   normalizations here exist to avoid.
/// * **A repeated `use super::*;` is dropped.** Legal — glob imports may repeat — but noise in a
///   file a user reads, and it is what the revise loop left behind when it fixed the doc comments.
///
/// Every authored check fn made `pub`, so the crate root's entries can reach it.
///
/// Anchored at line starts and on [`CHECK_PREFIX`]: an fn the author named anything else is a
/// helper and stays private, and `fn c_…` appearing inside a string or a comment is not at column
/// zero. Idempotent — an fn the author already made `pub` is left alone.
fn publicize_checks(body: &str) -> String {
    body.lines()
        .map(|line| match line.strip_prefix(&format!("fn {CHECK_PREFIX}")) {
            Some(rest) => format!("pub fn {CHECK_PREFIX}{rest}"),
            None => line.to_string(),
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// Anchored at line starts, so a `//!` inside a string literal is left alone.
fn as_module_body(body: &str) -> String {
    body.lines()
        .filter(|l| l.trim() != "use super::*;")
        .map(|l| {
            let trimmed = l.trim_start();
            match trimmed.strip_prefix("//!") {
                Some(rest) => format!("{}//{rest}", &l[..l.len() - trimmed.len()]),
                None => l.to_string(),
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// The delivered crate's `harness fn -> authored section` map, from the host's outcome set.
///
/// Keyed by the *validation target* each component ran under, so the crate declares exactly the
/// features the gated builds selected. Never keyed off `property_checks`: those are the **checks**,
/// one per property, and none of them is a feature that gates anything — keying on them is what
/// once wrote the harness fn once per property. A `BTreeMap` keeps the emitted crate stable and
/// sorted.
///
/// Split out of `finalize` so the assembly rule is testable without a crucible checkout (the
/// manifest half of `finalize`'s output only materializes when `$CRUCIBLE_REPO` is set).
pub(crate) fn delivered_sections(outcomes: &FinalizeInput) -> BTreeMap<String, String> {
    let mut sections: BTreeMap<String, String> = BTreeMap::new();
    for (_name, delivered) in outcomes.delivered() {
        let text = delivered.artifact_text.trim().to_string();
        if delivered.targets.is_empty() {
            // A host that sends no targets ran one unnamed target; use the fallback fn.
            sections.insert(DEFAULT_HARNESS_FN.to_string(), text);
        } else {
            for t in &delivered.targets {
                sections.insert(t.clone(), text.clone());
            }
        }
    }
    sections
}

#[cfg(test)]
mod tests {
    //! Each component's authored tests are sealed behind their Cargo feature, so two components
    //! can define same-named helpers without interfering. This is the fix for the klend run of
    //! 2026-08-03, where two components each emitted an ungated
    //! `impl Fixture { fn read_token_balance }` and the delivered crate compiled for **no**
    //! feature — while all 14 gated builds had passed, because each assembled only its own section.
    use super::*;
    use crate::testkit::{at, code_only, delivered_component, delivered_files, gated, scaffold};

    /// Two components' sections, each with its own same-named private helper — verbatim the shape
    /// that collided (`E0592 duplicate definitions with name read_token_balance`). Each names its
    /// check fn after the property it carries, which is what the prompt asks for.
    const SEC_A: &str =
        "pub fn c_a_holds(fixture: &mut Fixture) { let _ = fixture.read_token_balance(); }\n\
         impl Fixture { fn read_token_balance(&self) -> u64 { 1 } }";
    const SEC_B: &str =
        "pub fn c_b_holds(fixture: &mut Fixture) { let _ = fixture.read_token_balance(); }\n\
         impl Fixture { fn read_token_balance(&self) -> u64 { 2 } }";

    /// A path inside the `lending` harness crate, as the host receives it (workdir-relative).
    fn at_lending(rel: &str) -> String {
        at("lending", rel)
    }

    #[test]
    fn a_section_is_gated_and_its_entry_point_delegates_into_it() {
        // One target per CHECK: the module is gated on the union of its checks' features, and each
        // check gets its own entry (docs/per-check-targets.md). The component is not a feature.
        let main_rs = &scaffold(&["a"])[&at_lending("src/main.rs")];
        assert!(main_rs.contains("#[cfg(any(feature = \"c_a_holds\"))]\nmod c_a;"), "{main_rs}");
        // The entry is ours, at crate root, gated on that check's feature, delegating in.
        assert!(
            main_rs.contains(
                "#[cfg(feature = \"c_a_holds\")]\n#[invariant_test]\nfn c_a_holds(fixture: &mut Fixture) {\n    \
                 c_a::c_a_holds(fixture)\n}"
            ),
            "{main_rs}"
        );
        assert!(!main_rs.contains("feature = \"c_a\""), "the component is still a feature:\n{main_rs}");
        // The body lives in the file that `mod c_a;` resolves to, not in the crate root.
        let section = &gated("c_a", SEC_A)[&at_lending("src/c_a.rs")];
        assert!(section.contains("use super::*;"), "{section}");
        assert!(section.contains("fn read_token_balance"), "{section}");
        assert!(!main_rs.contains("read_token_balance"), "body leaked into main.rs:\n{main_rs}");
    }

    #[test]
    fn an_authored_invariant_test_attribute_is_dropped() {
        // A model that adds the attribute anyway would expand `fn main()` INSIDE the module, which
        // is not a binary entry point — a link error rather than something the revise loop can fix.
        let files = gated("c_a", &format!("#[invariant_test]\n{SEC_A}"));
        let section = code_only(&files[&at_lending("src/c_a.rs")]);
        assert!(!section.contains("#[invariant_test]"), "{section}");
        // Every `#[invariant_test]` in the crate is one we generated, in the crate root: one per
        // unit plus the wheel's own preflight.
        let main_rs = code_only(&scaffold(&["a"])[&at_lending("src/main.rs")]);
        assert_eq!(main_rs.matches("#[invariant_test]").count(), 2, "{main_rs}");
    }

    #[test]
    fn authored_file_scaffolding_is_stripped_rather_than_costing_a_revise_round() {
        // Verbatim the shape both components produced in the 2026-08-07 e2e run: the model wrote
        // what looks like a whole module file, opening with `//!` docs and a `use super::*;`. Below
        // the header this template already supplies, the `//!` is E0753 and the `use` is a duplicate.
        // Harmless when a section was concatenated at crate root, so introduced by moving sections
        // into files of their own, and therefore this wrapper's to absorb.
        let authored = "//! Authored invariants for the `Vault_Initialization` component.\n\
                        //!\n\
                        //! Called once after every fuzzed action.\n\
                        \n\
                        use super::*;\n\
                        \n\
                        pub fn invariants(fixture: &mut Fixture) {\n\
                        \x20   fuzz_assert!(true, \"[p] //! not a doc comment\");\n\
                        }";
        let section = &gated("c_a", authored)[&at_lending("src/c_a.rs")];
        // No inner doc comment survives below the header — that is the E0753.
        assert!(
            !section.lines().skip(4).any(|l| l.trim_start().starts_with("//!")),
            "an authored inner doc comment reached the body:\n{section}",
        );
        // The text is kept, just as an ordinary comment.
        assert!(section.contains("// Authored invariants for the `Vault_Initialization`"), "{section}");
        // Exactly one `use super::*;` — ours.
        assert_eq!(section.matches("use super::*;").count(), 1, "{section}");
        // Anchored at line starts, so a `//!` inside a string literal is untouched.
        assert!(section.contains(r#""[p] //! not a doc comment""#), "{section}");
    }

    #[test]
    fn the_authored_fn_is_made_visible_to_the_generated_entry() {
        // Called from the crate root, so a private fn is E0603. Cheaper than a revise round.
        let files = gated("c_a", "fn c_a_holds(fixture: &mut Fixture) {}\nfn helper() {}");
        let section = files.get(&at_lending("src/c_a.rs")).expect("section file");
        assert!(section.contains("pub fn c_a_holds(fixture: &mut Fixture)"), "{section}");
        // A helper the author named anything else is not a check, and stays private.
        assert!(section.contains("\nfn helper()"), "{section}");
    }

    #[test]
    fn an_already_public_fn_is_left_alone() {
        let files = gated("c_a", SEC_A);
        let section = files.get(&at_lending("src/c_a.rs")).expect("section file");
        assert_eq!(section.matches("pub fn c_a_holds").count(), 1, "no double-pub:\n{section}");
        assert!(!section.contains("pub pub"), "{section}");
    }

    #[test]
    fn two_components_may_define_the_same_helper_name() {
        let files = delivered_files(serde_json::json!([
            delivered_component("A", &["c_a"], SEC_A),
            delivered_component("B", &["c_b"], SEC_B),
        ]));
        // Each helper is in its own file, so they never coexist in a build…
        let a = &files[&at_lending("src/c_a.rs")];
        let b = &files[&at_lending("src/c_b.rs")];
        assert_eq!(a.matches("fn read_token_balance").count(), 1, "{a}");
        assert_eq!(b.matches("fn read_token_balance").count(), 1, "{b}");
        // …and the `#[cfg]`, not the file split, is what guarantees it: an inherent `impl Fixture`
        // contributes its methods GLOBALLY, so separate modules alone would still be E0592.
        let code = code_only(&scaffold(&["a", "b"])[&at_lending("src/main.rs")]);
        assert!(code.contains("#[cfg(any(feature = \"c_a_holds\"))]\nmod c_a;"), "{code}");
        assert!(code.contains("#[cfg(any(feature = \"c_b_holds\"))]\nmod c_b;"), "{code}");
        // One gated entry per feature — one check each here, plus the preflight — so exactly one
        // `fn main()` exists in any build.
        assert_eq!(code.matches("#[invariant_test]").count(), 3, "{code}");
    }

    #[test]
    fn what_the_gate_fuzzed_is_byte_for_byte_what_ships() {
        // The whole reason a green gate could ship a broken crate: the two used to be assembled
        // separately. Now the crate root is written once for both, and the only per-component file
        // either produces comes from the same `Section::file` — so there is nothing left to drift.
        let gate = gated("c_a", SEC_A);
        let ship = delivered_files(serde_json::json!([
            delivered_component("A", &["c_a"], SEC_A),
            delivered_component("B", &["c_b"], SEC_B),
        ]));
        assert_eq!(
            gate.get(&at_lending("src/c_a.rs")),
            ship.get(&at_lending("src/c_a.rs")),
            "the fuzzed section is not the shipped section"
        );
    }

    #[test]
    fn a_component_that_gave_up_gets_a_compile_error_not_a_failing_test() {
        // The crate root declares a feature per unit before any of them has authored anything, so
        // this target exists either way. What goes behind it must be an honest refusal: `validate`
        // reads a fuzz finding as a REFUTED PROPERTY, so a failing test here would be indistinguish-
        // able from a real counterexample against the user's own program.
        let files = delivered_files(serde_json::json!([
            delivered_component("A", &["c_a"], SEC_A),
            { "name": "Referrals", "outcome": { "status": "gave_up",
              "unit": { "slug": "referrals" },
              "reason": "no action mints referral fees" } },
        ]));
        let section = &files[&at_lending("src/c_referrals.rs")];
        assert!(section.contains("compile_error!"), "{section}");
        assert!(section.contains("no action mints referral fees"), "{section}");
        // Nothing that could be mistaken for a check, or run at all.
        assert!(!section.contains("fuzz_assert"), "{section}");
        assert!(!section.contains(&format!("fn {CHECK_PREFIX}")), "{section}");
        // The delivered component beside it is untouched by any of this.
        assert!(files[&at_lending("src/c_a.rs")].contains("pub fn c_a_holds"), "{files:?}");
    }

    #[test]
    fn a_body_shared_by_two_targets_is_emitted_once() {
        // Emitting it twice would give the second feature a module file defining a fn the crate
        // root's entry for the FIRST one already claims — one authored body, one file.
        let files = delivered_files(serde_json::json!([
            delivered_component("A", &["c_a", "c_b"], SEC_A),
        ]));
        assert_eq!(
            files.keys().collect::<Vec<_>>(),
            vec![&at_lending("src/c_a.rs")],
            "the shared body belongs to the first target only",
        );
    }
}
