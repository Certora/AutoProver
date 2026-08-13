//! The [`Backend`] itself: the callouts the Python pipeline calls, in the order it calls them.
//!
//! Every one of them is thin on purpose — what a callout *decides* lives in the module that owns
//! that material ([`crate::prompts`], [`crate::harness`], [`crate::triage`]), so this file reads as
//! the seam and nothing else.

use std::collections::BTreeMap;
use std::time::Instant;

use askama::Template;
use autoprover_sdk::args::AppArgs;
use autoprover_sdk::authoring::{AuthorInput, Authored, Judge, Prompt};
use autoprover_sdk::chain::ChainData;
use autoprover_sdk::descriptor::AppDescriptor;
use autoprover_sdk::finalize::FinalizeInput;
use autoprover_sdk::outcome::{
    CompileResult, Exploration, Outcome, Target, ValidateOutcome, Verdict,
};
use autoprover_sdk::prep::{CrateRootInput, SandboxGrants, WorkspacePrep};
use autoprover_sdk::sandbox::Workspace;
use autoprover_sdk::Backend;
use autoprover_solana::{SolanaPrep, SolanaPrepFacts, SolanaSourceUnit};

use crate::build_log::{build_errors, is_build_error, run_failure};
use crate::campaign::Campaign;
use crate::coverage;
use crate::harness::{crate_dep_usable, HarnessSpec};
use crate::layout::{
    feature_of_unit, harness_fn, unit_name, CORPUS_DIR, CRASHES_DIR, PREFLIGHT_FEATURE,
    PREFLIGHT_ROOT, REPORT_ROOT,
};
use crate::optional_accounts;
use crate::section::{delivered_sections, Section};
use crate::templates::SkeletonFixture;
use crate::triage::{attribute_findings, findings, undeclarable};
use crate::{declaration, prompts, toolchain};

/// Every verdict, told which file its assertion lives in — `<feature>.rs`, the section this
/// component's tests were written into (one section per file, `HarnessSpec::section_path`).
///
/// The report identifies a rule row by `(file, name)`, which is what lets one definition seen
/// through several runs collapse into a single row. Crucible's deliverable is ONE crate, so without
/// this every component's checks share its file name and two components that name the same
/// invariant collapse too — a real risk now that the names are the author's, and two authors given
/// the same property title write the same name. The 2026-08-12 vault run had exactly that: 15
/// checks reported as 14 rows, with the second component's verdict dropped.
fn in_section(target: &Target, outcome: ValidateOutcome) -> ValidateOutcome {
    let ValidateOutcome::Verdicts { verdicts } = &outcome else { return outcome };
    let file = format!("{}.rs", target.name);
    let by_name: Vec<(&str, &Verdict)> = verdicts.iter().collect();
    target.verdicts(|c| {
        let mut v = by_name
            .iter()
            .find(|(n, _)| *n == c.name)
            .map(|(_, v)| (*v).clone())
            .unwrap_or_else(|| Verdict::with_outcome(Outcome::Unknown));
        v.unit_file = Some(file.clone());
        v
    })
}

pub(crate) struct CrucibleApp;

impl Backend for CrucibleApp {
    fn descriptor(&self) -> AppDescriptor {
        declaration::descriptor()
    }

    fn validate_preconditions(&self, args: &AppArgs) -> Result<(), String> {
        toolchain::preconditions(args)
    }

    fn target_for(&self, input: &AuthorInput, _check: &str) -> Option<String> {
        // Every check of a component runs in ONE fuzz campaign — the component's harness fn, which
        // is also its Cargo feature and what a gated build selects
        // (docs/crucible-component-units.md §8.1). So the grouping does not depend on the check:
        // one build and one run for the whole property set, still a report row per property.
        // Neither the preflight gate nor the shared fixture formalizes anything, so neither has a
        // check to place.
        let Authored::Component { .. } = input.authored else { return None };
        Some(harness_fn(input))
    }

    fn author_prompt(&self, input: &AuthorInput) -> Prompt {
        prompts::author_prompt(input)
    }

    fn judge(&self, input: &AuthorInput) -> Option<Judge> {
        // The shared fixture is scaffolding, not test evidence — the compile/dry-run gate
        // already vets it, and there is no property to judge it against. Judge only the
        // per-component test suites (the peer of Foundry's feedback judge).
        input.unit()?;
        Some(Judge { system: Some(prompts::judge_system()) })
    }

    fn judge_instruction(&self, input: &AuthorInput, spec: &str) -> String {
        prompts::judge_instruction(input, spec)
    }

    fn compile(&self, input: &AuthorInput, spec: Option<&str>, ws: &Workspace) -> CompileResult {
        let program = &input.program;
        let hspec = HarnessSpec::of(input);
        // Preflight: the wheel's OWN skeleton in a crate of one file — `spec` is `None`, nothing has
        // been authored and no unit exists yet. Setup: the authored fixture in the DELIVERABLE's
        // crate root, dry-run behind the same preflight test; this callout is the first to hold both
        // the fixture and the unit set, so it renders the real root rather than a provisional one.
        // Component: the authored tests, dry-run behind that component's feature (which gates `main`).
        let authored_spec = spec.unwrap_or_default();
        let (files, feature) = match &input.authored {
            Authored::Preflight => {
                let skeleton = SkeletonFixture { crate_id: hspec.crate_id() }
                    .render()
                    .expect("render skeleton_fixture");
                (hspec.preflight_files(&skeleton), PREFLIGHT_FEATURE.to_string())
            }
            Authored::Setup { .. } => {
                let units: Vec<String> = input.units().iter().map(feature_of_unit).collect();
                (hspec.scaffold(authored_spec, &units), PREFLIGHT_FEATURE.to_string())
            }
            Authored::Component { .. } => {
                // ONLY this component's section. The crate root already declares it — written once
                // from the whole unit set by `crate_root` — so a gated build is the delivered crate
                // with one feature selected, not a separately-assembled lookalike
                // (docs/crucible-component-units.md §17).
                let fname = harness_fn(input);
                (hspec.section_files(&fname, authored_spec), fname)
            }
        };
        // Ahead of the build, because a build cannot see it: under the IDL path a `None` optional
        // account compiles, links and dry-runs clean, and only fails once a campaign draws the
        // action ([`crate::optional_accounts`]). Crate mode is unaffected — anchor's own derive
        // emits the program id for a `None`.
        if hspec.is_idl() {
            let absent = optional_accounts::absent_optionals(authored_spec);
            if !absent.is_empty() {
                return CompileResult::Failed { errors: optional_accounts::explain(&absent) };
            }
        }
        let dir = hspec.dir_arg(&ws.dir);
        let args = ["-C", &dir, "run", program, &feature, "--release", "--dry-run"];
        match ws.run("crucible", args, &files) {
            Ok(out)
                if out.exit_code == 0
                    && !is_build_error(&format!("{}\n{}", out.stdout, out.stderr)) =>
            {
                CompileResult::Ok
            }
            Ok(out) => CompileResult::Failed { errors: build_errors(&out) },
            Err(e) => CompileResult::Failed { errors: e },
        }
    }

    fn validate(
        &self,
        input: &AuthorInput,
        spec: &str,
        target: &Target,
        ws: &Workspace,
    ) -> ValidateOutcome {
        // Ahead of the build, because a campaign whose verdicts cannot be attributed is a wasted
        // budget: this backend's unit of evidence is one tagged assertion, so a check the author
        // mapped several properties onto has no answer it can stand behind ([`undeclarable`]).
        if let Some(why) = undeclarable(target) {
            return target.all(Outcome::Error, Some(why));
        }
        let program = &input.program;
        let budget_s: u64 = input.args.get("fuzz_timeout").unwrap_or(30);
        // The target's name is the harness fn, which is also the Cargo feature and the selector.
        let fname = &target.name;
        // Only this component's section, against the crate root `crate_root` already wrote — so what
        // is fuzzed is, byte for byte, what ships.
        let hspec = HarnessSpec::of(input);
        let files = hspec.section_files(fname, spec);
        let dir = hspec.dir_arg(&ws.dir);
        let timeout = budget_s.to_string();
        // `--mode explore` is NOT used, though these are its settings: it also turns on
        // `--stop-on-crash`, and a campaign that quits at the first crash leaves every other check
        // it covers unexplored while still answering for them. Spelling the settings out is what
        // lets `Target::exploration` decide that, rather than the mode preset deciding it for every
        // run. The corpus/crashes paths resolve against the invoking cwd, which is where
        // `crash_meta_paths` looks for the metadata behind a finding — keep them in step. They are
        // `explore`'s own names moved under `.certora_internal/` ([`CORPUS_DIR`]): a campaign fills
        // them without bound and the source tools must never list them.
        //
        // `--coverage` makes the campaign emit its own LCOV as it goes ([`crate::coverage`]) rather
        // than needing a second pass over the corpus. It requires `--corpus-in` and refuses to run
        // alongside `-j`, both already true here. It also **depends on `--timeout` staying
        // unconditional**: Crucible reads `--coverage` with no timeout as its coverage-ONLY mode,
        // which replays the corpus and fuzzes nothing. Make the budget conditional and every
        // campaign silently stops testing.
        let mut args = vec![
            "-C", &dir, "run", program, fname, "--release",
            "--corpus-in", CORPUS_DIR, "--corpus-out", CORPUS_DIR, "--crashes-out", CRASHES_DIR,
            "--timeout", &timeout, "--coverage",
        ];
        if target.exploration == Exploration::UntilFirstFinding {
            args.push("--stop-on-crash");
        }
        let started = Instant::now();
        let ran = ws.run("crucible", args, &files);
        // However the campaign ended. Crucible writes into the shared harness dir, so a file left
        // behind — by a run killed after its periodic flush, say — would be published as the *next*
        // component's coverage.
        coverage::preserve(&ws.dir, &hspec.dir(), REPORT_ROOT, fname);
        let located = match ran {
            Ok(out) => {
                let combined = format!("{}\n{}", out.stdout, out.stderr);
                let found = findings(&combined, &ws.dir, program, fname);
                // Order matters: a fuzz finding and a clean run both mean the harness BUILT, so
                // classify those first — only a *non-zero* exit with build markers is a real
                // build failure. This keeps `error[...]`-looking runtime/log text in a clean
                // (exit 0) fuzz run from being misread as a build failure.
                let concluded = if !found.is_empty() {
                    // Each crash refutes ONE invariant — pin BAD to the property each finding names
                    // (every assertion is tagged `[<title>]`), and let `exploration` say what the
                    // checks nothing named get. A title belonging to another component is left to
                    // the component that owns it; one nobody in the run owns marks all BAD (never
                    // hide it).
                    attribute_findings(target, &input.run_props, &found)
                } else if out.exit_code == 0 {
                    // Ran to the budget with no violation, so nothing this campaign covers was
                    // refuted.
                    //
                    // KNOWN GAP against the verdict contract on `Backend::validate`: `GOOD` per
                    // check also claims each one was *exercised*, and a campaign reports crashes,
                    // not which tagged assertions it evaluated. So a property whose assertion was
                    // never written, or written where the fuzzer cannot reach it, passes here. The
                    // fix is `fuzz_assert!` recording each evaluation and the campaign reporting the
                    // tally (docs/author-determined-checks.md); until then this is the one place a
                    // Crucible verdict claims more than the run established.
                    target.all(Outcome::Good, None)
                } else if is_build_error(&combined) {
                    // Shared build; re-author the whole spec (docs/rust-applications.md).
                    ValidateOutcome::BuildFailed { errors: build_errors(&out) }
                } else {
                    // Non-zero exit with no build markers and no finding: the campaign built and
                    // ran, so what the verdict owes is why it *stopped* — which the build log's
                    // tail cannot say (see `run_failure`).
                    target.all(Outcome::Error, Some(run_failure(&out)))
                };
                // What the campaign spent, on every row it answered for — a verdict is only worth
                // what the run behind it cost, and the report has nowhere else to say so.
                Campaign::of(&unit_name(input), budget_s, started.elapsed(), &combined)
                    .annotate(target, concluded)
            }
            Err(e) => target.all(Outcome::Error, Some(e)),
        };
        in_section(target, located)
    }

    fn sandbox_grants(&self, _args: &AppArgs) -> SandboxGrants {
        // Read-only grants beyond the launcher's discovered Rust toolchain: the crucible checkout
        // (path deps) and the `crucible` binary's dir. Was Python's `crucible_sandbox` extra_ro.
        let mut extra_ro = Vec::new();
        if let Ok(repo) = std::env::var("CRUCIBLE_REPO") {
            extra_ro.push(repo);
        }
        if let Some(dir) = toolchain::which_dir("crucible") {
            extra_ro.push(dir);
        }
        SandboxGrants { extra_ro, extra_env: Vec::new() }
    }

    fn workspace_prep(&self, input: &AuthorInput) -> WorkspacePrep {
        // Place a deps-only harness manifest (preflight feature) so warming has a manifest and the
        // setup dry-run can select a feature; per-run builds overwrite it with their own feature.
        // Then warm the harness crate's deps and build the program `.so` (the host runs both with
        // the shared helpers — fetch unconfined, build confined+offline).
        //
        // This is also where the crate-vs-IDL decision is made, ONCE: the program's own Anchor
        // version decides it (`crate_dep_usable`), or the operator forces the IDL path by supplying
        // one. Later callouts don't re-derive it — they read the `idl` fact the host reports after
        // placing the file, so the whole run renders one consistent crate.
        let program = &input.program;
        let cr = SolanaSourceUnit::from_input(input);
        let forced = input.args.text("program_idl").is_some();
        let dest = HarnessSpec::new(program, cr.clone(), String::new()).idl_dest();
        let idl_dest = (forced || !crate_dep_usable(&cr)).then_some(dest);
        // Render the manifest for the mode just decided — warming must fetch the deps the real
        // builds will use (under the IDL path the program's own graph is never resolved at all) —
        // and pin the toolchain, so the fetch resolves with the cargo that will do the building.
        let spec = HarnessSpec::new(program, cr.clone(), idl_dest.clone().unwrap_or_default());
        // What the host's Solana toolchain is asked to do, in the shape the two of them share
        // (`autoprover-solana`). The SDK carries it without a schema, so this is the only place the
        // request's fields are named on this side.
        let request = SolanaPrep {
            // The harness dir AND the program's own crate. Warming feeds the private `CARGO_HOME`
            // that the later *confined, offline* build reads, so anything missing from it is a hard
            // build failure rather than a download.
            //
            // The program's crate has to be named explicitly because only the crate path reaches it
            // by accident: there the harness manifest path-depends on the program, so fetching the
            // harness resolves the program's graph too. Under the IDL path the harness does not
            // depend on the program at all — that is the whole point of it — so nothing warms the
            // program, and `cargo-build-sbf` dies offline on the first dependency it cannot find.
            warm_dirs: vec![spec.dir(), cr.dir.clone()],
            // The `.so` is named after the crate's lib target, not the analysis identifier. Needed
            // under both paths: LiteSVM loads the compiled program either way.
            build_program: Some(cr.lib.clone()),
            idl_dest,
        };
        WorkspacePrep {
            // Pointed at the preflight root, since that gate is the next thing that builds here.
            files: spec.manifest_files(PREFLIGHT_ROOT, &HarnessSpec::features(&[])),
            toolchain_request: ChainData::of(&request).unwrap_or_default(),
        }
    }

    fn crate_root(&self, input: &CrateRootInput) -> BTreeMap<String, String> {
        // The same files the setup gate already built, from the same two halves it was sent — the
        // authored fixture and the unit set. It re-emits them because that gate does not run when the
        // host serves the setup spec from cache, and the per-unit gates need the crate on disk either
        // way. Identical bytes, so this is a no-op write on the path where the gate did run.
        let program = &input.program;
        if program.is_empty() {
            return BTreeMap::new();
        }
        let spec = HarnessSpec::new(
            program,
            SolanaSourceUnit::of(&input.source_unit, program),
            SolanaPrepFacts::of(&input.prep_facts).idl.unwrap_or_default(),
        );
        // Every unit gets a feature, including ones that will later give up: the declaration cannot
        // wait for an outcome, and `finalize` puts an honest `compile_error!` behind the ones that
        // produce nothing rather than leaving a `mod` with no file.
        let features: Vec<String> = input.units.iter().map(feature_of_unit).collect();
        spec.scaffold(input.setup.as_deref().unwrap_or_default(), &features)
    }

    fn finalize(&self, outcomes: &FinalizeInput) -> BTreeMap<String, String> {
        // Only the section files: the crate root and manifest were written once by `crate_root` and
        // are already correct for the whole unit set. What is added here is the body behind each
        // declared feature — an authored suite, or a `compile_error!` saying why there is none.
        let program = &outcomes.program;
        if program.is_empty() {
            return BTreeMap::new();
        }
        let spec = HarnessSpec::new(
            program,
            SolanaSourceUnit::of(&outcomes.source_unit, program),
            SolanaPrepFacts::of(&outcomes.prep_facts).idl.unwrap_or_default(),
        );

        let mut files = BTreeMap::new();
        // The dedupe survives for the case it was written for: two targets that resolved to the same
        // authored source. The first target owns the body; a second file would be a module the crate
        // root's entry for it delegates into without the source ever defining the fn.
        let mut seen: Vec<String> = Vec::new();
        for (feature, text) in delivered_sections(outcomes) {
            if seen.contains(&text) {
                continue;
            }
            seen.push(text.clone());
            files.insert(spec.section_path(&feature), Section::file(&feature, &text));
        }
        for (name, gave_up) in outcomes.gave_up() {
            let feature = feature_of_unit(&gave_up.unit);
            files.insert(
                spec.section_path(&feature),
                Section::gave_up(&feature, name, &gave_up.reason),
            );
        }
        files
    }
}

#[cfg(test)]
mod tests {
    //! What each callout returns for the payload the host actually sends: the per-component report
    //! rows and their shared fuzz target, the prep plan's one crate-vs-IDL decision, and the
    //! sections `finalize` leaves behind.
    use super::*;
    use crate::harness::crucible_repo;
    use crate::layout::harness_dir;
    use crate::testkit::{
        at, component_input, delivered_component, distinct_crate, outcomes, prep_input,
        prep_request, prop, skewed_crate, target_over,
    };

    /// A path inside the `lending` harness crate, as the host receives it (workdir-relative).
    fn at_lending(rel: &str) -> String {
        at("lending", rel)
    }

    #[test]
    fn a_verdict_says_which_components_section_it_came_from() {
        // Two components, each with an invariant its author named the same way — which is what two
        // authors given the same property title do. The report keys a rule row by (file, name), so
        // without a per-section file the second component's verdict is dropped as a duplicate of
        // the first (the 2026-08-12 vault run: 15 checks, 14 rows).
        let props = [prop("vault_authority_immutable", "auth_immutable")];
        let verdicts_for = |feature: &str| {
            let target = target_over(feature, &props);
            let ValidateOutcome::Verdicts { verdicts } =
                in_section(&target, target.all(Outcome::Good, None))
            else {
                panic!("a verdict set")
            };
            verdicts
                .iter()
                .map(|(n, v)| (n.to_string(), v.unit_file.clone()))
                .collect::<Vec<_>>()
        };
        assert_eq!(
            verdicts_for("c_vault_initialization"),
            vec![("c_auth_immutable".to_string(), Some("c_vault_initialization.rs".to_string()))],
        );
        assert_eq!(
            verdicts_for("c_lamport_custody"),
            vec![("c_auth_immutable".to_string(), Some("c_lamport_custody.rs".to_string()))],
        );
    }

    #[test]
    fn every_check_of_a_component_shares_that_components_fuzz_target() {
        // Whatever the author called its checks, they all run in the component's one campaign —
        // one build and one run for the whole property set.
        let app = CrucibleApp;
        let input = component_input(
            "farms", "Farms Integration",
            vec![prop("stake matches position", "stake_matches"), prop("no double stake", "no_dbl")],
        );
        for name in ["c_stake_matches", "whatever_the_author_called_it"] {
            assert_eq!(app.target_for(&input, name).as_deref(), Some("c_farms"));
        }
        // …and a different component gets a different target, so the host runs one campaign each.
        let other = component_input("referrals", "Referrals", vec![prop("fees capped", "fees")]);
        assert_eq!(app.target_for(&other, "c_fees").as_deref(), Some("c_referrals"));
    }

    #[test]
    fn only_a_component_groups_its_checks() {
        // The shared fixture formalizes nothing, and the gate runs before anything is analyzed, so
        // neither has a check to place.
        let app = CrucibleApp;
        let mut input = component_input("farms", "Farms", vec![prop("p", "p")]);
        assert!(app.target_for(&input, "c_p").is_some());
        for authored in [
            Authored::Setup { model: serde_json::Value::Null, units: Vec::new() },
            Authored::Preflight,
        ] {
            input.authored = authored;
            assert!(app.target_for(&input, "c_p").is_none());
        }
    }

    #[test]
    fn workspace_prep_builds_the_lib_artifact_and_warms_the_harness_dir() {
        let request = prep_request(
            &CrucibleApp.workspace_prep(&prep_input(distinct_crate(), serde_json::json!({}))),
        );
        // The harness dir follows the identifier (`crucible run vault`)…
        assert_eq!(request.warm_dirs, vec![harness_dir("vault"), "programs/lend".to_string()]);
        // …but the `.so` to build is the program crate's lib target.
        assert_eq!(request.build_program.as_deref(), Some("example_lending"));
        // A linkable program needs no IDL.
        assert_eq!(request.idl_dest, None);
    }

    #[test]
    fn the_programs_own_crate_is_warmed_even_when_the_harness_does_not_depend_on_it() {
        // The later build is confined and OFFLINE, so it can only use what warming fetched. Under
        // the IDL path the harness has no path dep on the program to drag the program's graph in,
        // and `cargo-build-sbf` fails on the first crate missing from the private CARGO_HOME —
        // which is exactly how the `solana_vault_idl` gate first failed.
        let request = prep_request(
            &CrucibleApp.workspace_prep(&prep_input(skewed_crate(), serde_json::json!({}))),
        );

        assert!(request.idl_dest.is_some(), "this is the IDL path");
        assert!(
            request.warm_dirs.contains(&"programs/lend".to_string()),
            "the program's crate must be warmed itself: {:?}",
            request.warm_dirs,
        );
    }

    #[test]
    fn workspace_prep_asks_for_an_idl_when_the_program_cannot_be_linked() {
        // Anchor 0.29 vs ours ⇒ the crate path is impossible, so the wheel requests an IDL and
        // renders the warming manifest for the IDL path (else warming would resolve — and fail on —
        // the program's own dependency graph).
        let plan = CrucibleApp.workspace_prep(&prep_input(skewed_crate(), serde_json::json!({})));
        assert_eq!(
            prep_request(&plan).idl_dest.as_deref(),
            Some(at("vault", "idls/example_lending.json").as_str())
        );
        if let Some(repo) = crucible_repo() {
            let cargo = &plan.files[&at("vault", "Cargo.toml")];
            assert!(cargo.contains("crucible-idl-gen"), "warming manifest not on the IDL path");
            assert!(!cargo.contains("programs/lend"), "warming manifest still links the program");
            let _ = repo;
        }
        // The `.so` is still built: LiteSVM loads the real program either way.
        assert_eq!(prep_request(&plan).build_program.as_deref(), Some("example_lending"));
        // An operator can force the IDL path for a program that *is* linkable.
        let forced = CrucibleApp.workspace_prep(&prep_input(
            distinct_crate(),
            serde_json::json!({ "program_idl": "/tmp/lend.json" }),
        ));
        assert_eq!(
            prep_request(&forced).idl_dest.as_deref(),
            Some(at("vault", "idls/example_lending.json").as_str())
        );
    }

    #[test]
    fn finalize_emits_one_section_and_one_feature_per_component() {
        // The deliverable is ONE crate holding every component's fn, declaring exactly the
        // features the gated builds selected — read off the hosts' mirrored `targets`, not
        // re-derived from a display name.
        let app = CrucibleApp;
        let outcomes = outcomes(serde_json::json!({
            "program": "lending",
            "setup": "struct Fixture {}",
            "source_unit": { "dir": "programs/lending", "lib": "lending_program",
                             "package": "lending_program", "anchor": "" },
            "prep_facts": {},
            "components": [
                delivered_component(
                    "Withdraw Queue", &["c_withdraw_queue"], "fn c_withdraw_queue() { /* q */ }",
                ),
                delivered_component("Farms", &["c_farms"], "fn c_farms() { /* f */ }"),
                // A component that gave up authored nothing, so it carries its unit instead of
                // targets — the wheel names its (already declared) feature from that.
                { "name": "Referrals", "outcome": { "status": "gave_up",
                  "unit": { "slug": "referrals" }, "reason": "the fixture exposes no referral action" } },
            ],
        }));
        // A section is keyed by the *validation target* its checks ran under — read off the host's
        // mirrored `targets`, never re-derived from a display name.
        let sections = delivered_sections(&outcomes);
        assert_eq!(
            sections.keys().cloned().collect::<Vec<_>>(),
            vec!["c_farms".to_string(), "c_withdraw_queue".to_string()],
            "one section per delivered component, and no fallback c_invariants",
        );

        // `finalize` writes section files only: the crate root and manifest were written once, up
        // front, by `crate_root` — this is the run's last word on what is behind each feature.
        let files = app.finalize(&outcomes);
        assert!(
            files[&at_lending("src/c_withdraw_queue.rs")].contains("fn c_withdraw_queue()"),
            "{files:?}"
        );
        assert!(files[&at_lending("src/c_farms.rs")].contains("fn c_farms()"), "{files:?}");
        assert!(!files.contains_key(&at_lending("src/main.rs")), "{:?}", files.keys());
        assert!(!files.contains_key(&at_lending("Cargo.toml")), "{:?}", files.keys());
        // The one that gave up gets an honest refusal behind its feature, not silence and not a test.
        let referrals = &files[&at_lending("src/c_referrals.rs")];
        assert!(referrals.contains("compile_error!"), "{referrals}");
        assert!(referrals.contains("the fixture exposes no referral action"), "{referrals}");
    }

    #[test]
    fn finalize_writes_a_shared_section_once_even_if_two_targets_map_to_it() {
        // Guards the failure that produced N copies of one fn: two targets, one authored source.
        let app = CrucibleApp;
        let outcomes = outcomes(serde_json::json!({
            "program": "lending",
            "setup": "struct Fixture {}",
            "source_unit": { "dir": "programs/lending", "lib": "lending_program",
                             "package": "lending_program", "anchor": "" },
            "prep_facts": {},
            "components": [
                delivered_component("A", &["c_a", "c_b"], "fn c_a() {}"),
            ],
        }));
        let files = app.finalize(&outcomes);
        assert_eq!(files[&at_lending("src/c_a.rs")].matches("fn c_a()").count(), 1, "{files:?}");
        assert!(!files.contains_key(&at_lending("src/c_b.rs")), "{:?}", files.keys());
    }
}
