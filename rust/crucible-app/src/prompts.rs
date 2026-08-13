//! The turns' prompts: the fixture-authoring instruction, the per-component suite instruction, and
//! the reviewer's persona + instruction.
//!
//! Everything a prompt says about *names* comes from [`crate::layout`], so what the author is asked
//! to write and what the generated crate root calls it cannot disagree.

use askama::Template;

use autoprover_sdk::authoring::{AuthorInput, Authored, Prompt};

use crate::facts::api_facts;
use crate::harness::HarnessSpec;
use crate::layout::unit_name;
use crate::templates::{
    AuthorComponent, AuthorSetup, ExampleFixture, HarnessCheatSheet, JudgeInstruction, JudgeSystem,
    TestCheatSheet,
};

/// The input's properties as prompt lines — `- [sort] title: description`, the form every prompt
/// that lists them uses.
fn listed_props(input: &AuthorInput) -> String {
    input
        .props
        .iter()
        .map(|p| format!("- [{}] {}: {}", p.sort, p.title, p.description))
        .collect::<Vec<_>>()
        .join("\n")
}

/// The compiled shared fixture every component's tests build on, or empty before it exists.
fn fixture_of(input: &AuthorInput) -> String {
    input.setup.clone().unwrap_or_default()
}

/// The authoring prompt for whichever half of the harness this callout is for.
pub(crate) fn author_prompt(input: &AuthorInput) -> Prompt {
    let program = &input.program;
    let instruction = match &input.authored {
        // Nothing is authored for the gate — the wheel supplies its own skeleton — so the host
        // never asks for this prompt. Say so rather than rendering a prompt about no unit.
        Authored::Preflight => "ERROR: the preflight gate authors nothing".to_string(),
        // Author the shared fixture from the analyzed model.
        Authored::Setup { model: analyzed, .. } => {
            let model =
                serde_json::to_string_pretty(analyzed).unwrap_or_else(|_| analyzed.to_string());
            // The fixture's `use <id>::*` and the `.so` it loads are the crate's lib name — which
            // is also the IDL-generated module's name, so the fixture reads the same either way.
            let spec = HarnessSpec::of(input);
            let crate_id = spec.crate_id();
            let cheat = HarnessCheatSheet { crate_id, idl: spec.is_idl() }
                .render()
                .expect("render harness_cheat_sheet");
            let example = ExampleFixture.render().expect("render example_fixture");
            let facts = api_facts(analyzed, program, crate_id);
            AuthorSetup {
                program,
                n: input.props.len(),
                listed: &listed_props(input),
                cheat: &cheat,
                example: &example,
                facts: &facts,
                model: &model,
            }
            .render()
            .expect("render author_setup")
        }
        // Author ONE invariant fn holding all of THIS component's properties.
        Authored::Component { unit } => {
            let listed = listed_props(input);
            let component =
                serde_json::to_string_pretty(unit).unwrap_or_else(|_| unit.to_string());
            let fixture = fixture_of(input);
            let cheat = TestCheatSheet.render().expect("render test_cheat_sheet");
            AuthorComponent {
                unit: &unit_name(input),
                program,
                n: input.props.len(),
                first: input.props.first().map(|p| p.title.as_str()).unwrap_or("property"),
                listed: &listed,
                component: &component,
                cheat: &cheat,
                fixture: &fixture,
            }
            .render()
            .expect("render author_component")
        }
    };
    Prompt { system: None, instruction }
}

/// The reviewer persona for the review turn.
pub(crate) fn judge_system() -> String {
    JudgeSystem.render().expect("render judge_system")
}

/// The review turn's instruction: this component's properties, the shared fixture, and the suite
/// under review.
pub(crate) fn judge_instruction(input: &AuthorInput, spec: &str) -> String {
    let program = &input.program;
    let listed = listed_props(input);
    let component = input
        .unit()
        .map(|u| serde_json::to_string_pretty(u).unwrap_or_else(|_| u.to_string()))
        .unwrap_or_default();
    let fixture = fixture_of(input);
    JudgeInstruction {
        program,
        listed: &listed,
        component: &component,
        fixture: &fixture,
        spec,
    }
    .render()
    .expect("render judge_instruction")
}

#[cfg(test)]
mod tests {
    //! Every prompt renders end to end with no hole left unfilled, and each says the things whose
    //! absence produced a bad run: the constant fn name, interpolated assertion messages, and the
    //! two harness defects the judge is there to catch.
    use super::*;
    use crate::app::CrucibleApp;
    use crate::layout::SECTION_FN;
    use crate::testkit::{component_input, prep_input, prop};
    use autoprover_sdk::authoring::{Property, PropertyKind};
    use autoprover_sdk::Backend;
    use autoprover_solana::SolanaSourceUnit;

    fn assert_no_residue(s: &str) {
        for t in ["{{", "{%", "{#"] {
            assert!(!s.contains(t), "template residue {t:?} in:\n{s}");
        }
    }

    #[test]
    fn prompt_templates_render_end_to_end() {
        let app = CrucibleApp;
        let component = serde_json::json!({ "instructions": [{ "name": "deposit" }] });
        let prop = Property {
            component: "Deposits".into(),
            title: "no overflow".into(),
            sort: PropertyKind::Invariant,
            description: "balance never overflows".into(),
            slug: "no_overflow".into(),
        };

        // setup branch (exercises author_setup.j2). The fixture is authored with the properties in
        // hand — that is the whole point of the host deferring it until extraction has run — so
        // they are part of this input too.
        let setup = AuthorInput {
            authored: Authored::Setup { model: component.clone(), units: Vec::new() },
            props: vec![prop.clone()],
            ..prep_input(SolanaSourceUnit::default(), serde_json::json!({}))
        };
        // Prose templates are wrapped to 120, so a phrase can span a newline — compare with
        // whitespace collapsed so the checks are wrap-insensitive.
        let norm = |s: &str| s.split_whitespace().collect::<Vec<_>>().join(" ");
        let has = |hay: &str, needle: &str| assert!(
            norm(hay).contains(&norm(needle)), "missing {needle:?} in:\n{hay}"
        );

        let p = app.author_prompt(&setup);
        assert_no_residue(&p.instruction);
        has(&p.instruction, "FIXTURE (only) for the Solana program `vault`");
        // The properties the fixture must make checkable, and the design rules that follow from
        // them: an action per instruction they touch (including negative attempts), enough actors,
        // and configuration the fuzzer can actually cross.
        has(&p.instruction, "Design it for these 1 properties");
        has(&p.instruction, "- [invariant] no overflow: balance never overflows");
        has(&p.instruction, "One `action_*` per instruction those properties exercise");
        has(&p.instruction, "Negative attempts are actions too");
        // …that such an action RECORDS the outcome rather than asserting on it. A tagged assertion
        // in the fixture fires in every component's campaign, and the components that do not own
        // the title cannot place the counterexample
        // (docs/crucible-cross-component-attribution.md §4.2).
        has(&p.instruction, "the action ATTEMPTS, it never JUDGES");
        has(&p.instruction, "Never assert a property in the fixture");
        has(&p.instruction, "compiled into **every** component's fuzz target");
        // …and that it reports `true`. A negative action returning `false` is read by Crucible as a
        // dead-end and ENDS the action sequence, so every draw of it truncates the campaign — and
        // any violation on it is auto-labelled a suspected harness bug, which is backwards for an
        // action whose purpose is the rejection. Both were observed in the 2026-08-07 e2e run,
        // where the fixture's five negative actions all returned `false`.
        has(&p.instruction, "A negative action must `return true`");
        has(&p.instruction, "ends the action sequence there");
        has(&p.instruction, "Never set a limit, cap or threshold to `u64::MAX`");

        // component branch (exercises author_component.j2).
        let comp = AuthorInput {
            authored: Authored::Component { unit: component.clone() },
            props: vec![prop],
            setup: Some("struct Fixture { ctx: TestContext }".into()),
            ..prep_input(SolanaSourceUnit::default(), serde_json::json!({}))
        };
        let p = app.author_prompt(&comp);
        assert_no_residue(&p.instruction);
        // The unit carries no slug, so the *feature* falls back to `DEFAULT_HARNESS_FN` — but the
        // prompt asks for the constant fn either way, the fallback being a wheel-side name now.
        has(&p.instruction, &format!("named EXACTLY `{SECTION_FN}`"));
        has(&p.instruction, "`\"[no overflow] ...\"`");
        // What the component turn may add to the fixture, what it may not, and the honest way out
        // when the fixture can't reach a property (the alternative being a vacuous assertion).
        has(&p.instruction, "put extra `impl Fixture { ... }` blocks (plain, NOT `#[fuzz_fixture]`)");
        has(&p.instruction, "Do not add `action_*` methods or a second `#[fuzz_fixture]` block");
        has(&p.instruction, "Do not send instructions from the test");
        has(&p.instruction, "do not fake it");
        has(&p.instruction, "// UNCOVERABLE:");
        // The other half of §4.2: the fixture only records a negative attempt's outcome, so the
        // assertion for a "must be rejected" property is this component's. Without this the split
        // moves the check nowhere and silently drops it.
        has(&p.instruction, "checked HERE, not in the fixture");
        has(&p.instruction, "fixture.accepted.init_without_signer");

        // judge prompt (exercises judge_instruction.j2 + the judge_guidance.j2 include + system).
        let reviewer = app.judge(&comp).expect("component judge");
        let ji = app.judge_instruction(&comp, "fn c_invariants(f: &mut Fixture) {}");
        assert_no_residue(&ji);
        has(&ji, "Evaluate the Crucible fuzz-test suite");
        has(&ji, "Criterion 1");
        has(reviewer.system.as_deref().unwrap(), "senior Solana security engineer");
        // setup has no judge turn.
        assert!(app.judge(&setup).is_none());
    }

    #[test]
    fn the_author_and_judge_prompts_ask_for_the_constant_fn_name() {
        let app = CrucibleApp;
        let input = component_input("withdraw_queue", "Withdraw Queue", vec![prop("fifo", "fifo")]);
        let norm = |s: &str| s.split_whitespace().collect::<Vec<_>>().join(" ");

        let p = app.author_prompt(&input);
        assert_no_residue(&p.instruction);
        assert!(norm(&p.instruction).contains(&format!("named EXACTLY `{SECTION_FN}`")));
        // The declared unit is the tagged assertion, not the one fn that holds them all. Both
        // authors of the 2026-08-11 vault run read an earlier wording ("which harness function
        // verifies which property") as "map every property onto `invariants`", which made one
        // counterexample fail all ten properties that check covered.
        assert!(norm(&p.instruction).contains("one TAGGED ASSERTION, not the"), "{}", p.instruction);
        assert!(
            norm(&p.instruction).contains("Never map several properties onto one invariant"),
            "{}", p.instruction,
        );
        assert!(norm(&p.instruction).contains("**Withdraw Queue** component"));
        // The interleaving warning that replaces the whole-program framing.
        assert!(norm(&p.instruction).contains("drives the WHOLE program, not just this component"));
        // The embedded cheat sheet names the same fn as the instruction — trivially now, because
        // both name a constant. This used to be the live hazard: two places carrying a per-component
        // name that could disagree, and a disagreement told the author to write an fn no build
        // selects. The name a build selects is `c_<slug>`, and it belongs to the wheel-generated
        // entry, so it must NOT reach the prompt at all.
        assert!(
            norm(&p.instruction).contains(&format!("fn {SECTION_FN}(fixture: &mut Fixture)")),
            "cheat sheet does not ask for the constant fn:\n{}", p.instruction,
        );
        assert!(
            !p.instruction.contains("c_withdraw_queue"),
            "the generated entry's name leaked into the prompt:\n{}", p.instruction,
        );
        assert!(!p.instruction.contains("c_invariants"), "stale fn name in:\n{}", p.instruction);

        let ji = app.judge_instruction(&input, &format!("fn {SECTION_FN}() {{}}"));
        assert_no_residue(&ji);
        assert!(norm(&ji).contains(SECTION_FN));
        assert!(!ji.contains("c_withdraw_queue"), "generated name leaked into the judge:\n{ji}");
    }

    #[test]
    fn the_author_is_told_to_interpolate_the_operands_into_every_assertion_message() {
        // A custom message REPLACES `fuzz_assert*`'s built-in operand dump, so a message without
        // the values yields an untriageable counterexample. On the klend run this is why
        // "total supply exceeds deposit_limit" needed an hour and a rebuild to explain.
        let app = CrucibleApp;
        let input = component_input("withdraw_queue", "Withdraw Queue", vec![prop("fifo", "fifo")]);
        let s = app.author_prompt(&input).instruction;
        let norm = s.split_whitespace().collect::<Vec<_>>().join(" ");

        assert!(norm.contains("INTERPOLATE THE OPERAND VALUES"), "{norm}");
        // The *reason* has to be in the prompt, not just the rule — the macro behaviour is the
        // non-obvious part, and a rule without it reads as style advice.
        assert!(norm.contains("operand dump is DISCARDED"), "{norm}");
        // And the worked contrast, so "include the values" is unambiguous.
        assert!(norm.contains("total_supply={} exceeds deposit_limit={}"), "{norm}");
    }

    #[test]
    fn both_authors_are_told_a_panic_on_fuzzed_input_costs_the_whole_run() {
        // The klend run that produced 84 uninformative ERROR verdicts across three components:
        // one unchecked `find_program_address` in the shared fixture aborted every campaign that
        // drew the input, and the input then sat in the shared corpus poisoning later components.
        // Both halves have to be in the prompt — the `try_` rule alone reads as style advice, and
        // an author who does not know a panic is invisible to the fuzzer will keep reaching for
        // the unchecked form.
        let app = CrucibleApp;
        let setup = AuthorInput {
            authored: Authored::Setup {
                model: serde_json::json!({}).into(),
                units: Vec::new(),
            },
            props: vec![prop("fifo", "fifo")],
            ..prep_input(SolanaSourceUnit::default(), serde_json::json!({}))
        };
        let fixture = app.author_prompt(&setup).instruction;
        let norm = |s: &str| s.split_whitespace().collect::<Vec<_>>().join(" ");

        let f = norm(&fixture);
        assert!(f.contains("try_find_program_address"), "no guarded derivation in:\n{f}");
        assert!(f.contains("outside the fuzz target"), "the panic's invisibility is missing:\n{f}");
        assert!(f.contains("shared corpus"), "the blast radius is missing:\n{f}");
        // `setup()` is exempt, and saying so keeps the rule from being read as "never panic".
        assert!(f.contains("In `setup()` itself, panicking is still fine"), "{f}");

        // The judge reviews suites, not the fixture (`judge` returns None for setup), so the same
        // defect reaches it only through a component — where it is this author's to fix.
        let comp = component_input("withdraw_queue", "Withdraw Queue", vec![prop("fifo", "fifo")]);
        let j = norm(&app.judge_instruction(&comp, "fn c_fifo(f: &mut Fixture) {}"));
        assert!(j.contains("Panics are not findings"), "no panic criterion in the judge:\n{j}");
        assert!(j.contains("report it under C8"), "fixture-gap routing missing:\n{j}");
    }

    #[test]
    fn the_judge_rejects_untriageable_messages_and_precondition_scope_errors() {
        // The two harness defects that produced BOTH of klend's false counterexamples. Catching
        // them at the judge is cheaper than reporting them as findings a human must triage.
        let app = CrucibleApp;
        let input = component_input("withdraw_queue", "Withdraw Queue", vec![prop("fifo", "fifo")]);
        let ji = app.judge_instruction(&input, "fn c_withdraw_queue() {}");
        let norm = ji.split_whitespace().collect::<Vec<_>>().join(" ");

        assert!(norm.contains("Diagnosable failure messages"), "{norm}");
        assert!(norm.contains("Precondition scope"), "{norm}");
        // The zeroed-account trap: an Option-returning read is not a guard, because a zeroed
        // account deserializes into a default struct rather than failing.
        assert!(norm.contains("DEFAULT struct"), "{norm}");
        assert!(norm.contains("iteration 0"), "{norm}");
        // A rejection the fixture recorded but this suite never asserts on is an uncovered
        // property, not a covered one — the judge is the only thing that sees both halves.
        assert!(norm.contains("The assertion must be in THIS suite"), "{norm}");
    }

    #[test]
    fn the_property_author_is_told_the_shape_of_the_checker_that_will_read_its_output() {
        // 33 of klend's 411 proposed properties were declined as unformalizable, and four shapes
        // account for all of them (docs/crucible-uncheckable-properties.md). Each is decided when
        // the property is *worded*, one phase before anyone tries to check it — and this prose is
        // the only thing that reaches that phase, since extraction runs before the fixture exists.
        let guidance = CrucibleApp.descriptor().backend_guidance;
        let norm = guidance.split_whitespace().collect::<Vec<_>>().join(" ");
        let has = |needle: &str| {
            let n = needle.split_whitespace().collect::<Vec<_>>().join(" ");
            assert!(norm.contains(&n), "missing {needle:?} in:\n{guidance}");
        };

        // The premise the four rules follow from: a predicate over accounts, between actions.
        has("evaluated *between* actions");
        has("cannot see which instruction just ran");
        // 10 of the 33 — a delta across one call, with the standing relation left unstated.
        has("A per-call delta is not checkable");
        // 9 — a rejection or liveness claim, refuted by a revert that leaves no residue. The
        // harness is built FROM these properties, so naming the attempt is what creates the action
        // that records it; this is the one rule the author can act on but not verify.
        has("**\"Must be rejected\" needs the exact attempt.**");
        has("becomes an action that makes the attempt and records whether it was accepted");
        // 8 — an assertion over harness-supplied values, which no implementation can fail.
        has("holds under every possible implementation");
        // 5 — the deciding quantity is computed and never stored.
        has("Name the account fields that decide it");
        // …and the two that are worth knowing rather than avoiding.
        has("panic and a returned `Err` are indistinguishable");
        has("perturbs and restores before it ends");

        // None of it may become a reason to withhold a property: a fuzzer cannot prove, and the
        // catalogue of what it cannot check is exactly what a reader of the report needs.
        has("state universal safety properties and invariants freely");
    }

    #[test]
    fn a_source_confirmed_defect_is_published_as_a_finding_rather_than_skipped() {
        // klend filed two confirmed bugs — reasons opening "KNOWN VULNERABILITY" and "The bug is
        // real (confirmed in source: …)" — through `record_skip`, so the report showed them under
        // "Formalization gaps" beside 31 genuine ones. They are not gaps; a gap is the absence of a
        // claim about the program, and these are claims.
        let app = CrucibleApp;
        let input = component_input("flash_loans", "Flash Loans", vec![prop("gating", "gating")]);
        let norm = |s: &str| s.split_whitespace().collect::<Vec<_>>().join(" ");
        let author = norm(&app.author_prompt(&input).instruction);

        assert!(author.contains("is a FINDING, not a gap"), "{author}");
        assert!(author.contains("// FINDING:"), "{author}");
        assert!(author.contains("mark it `expect_check_failure`"), "{author}");
        // The two rules that keep it from becoming a channel for guesses.
        assert!(author.contains("name the **source evidence**"), "{author}");
        assert!(author.contains("never use it for a suspicion you have not confirmed"), "{author}");

        // The judge has to know too, or it rejects the exact handling the author was told to use:
        // a `// FINDING:` fn asserts nothing, which is Criterion 1 on its face.
        let judge = norm(&app.judge_instruction(&input, "fn c_gating() {}"));
        assert!(judge.contains("Do not read it as a C1 vacuous assertion"), "{judge}");
        // …but it is not a free pass. The judge is pointed at the two ways the claim fails.
        assert!(judge.contains("point at specific source"), "{judge}");
        assert!(judge.contains("say which sequence"), "{judge}");
    }
}
