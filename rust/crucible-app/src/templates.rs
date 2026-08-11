//! The bindings for the `.j2` files under `templates/`.
//!
//! Each struct binds one file (the same convention as composer/templates/*.j2, here for the Rust
//! side). They replace the former inline string consts and `format!` literals: `render()` fills the
//! holes. `escape = "none"` because these render prompts and Rust/TOML source — NOT HTML — so no
//! entity escaping. Whitespace is preserved (see askama.toml).
//!
//! Nothing here decides anything: what goes in the holes belongs to the module that owns the
//! decision ([`crate::harness`] for the crate's files, [`crate::prompts`] for a turn's prompt).

use askama::Template;

/// Backend-guidance prose injected into the property-extraction prompt. Crucible is a fuzzer,
/// so — like Foundry — refutations are valuable but universals can't be *proven*.
#[derive(Template)]
#[template(path = "backend_guidance.j2", escape = "none")]
pub(crate) struct BackendGuidance;

/// Concise Crucible harness API reference for the fixture-authoring prompt (§7.5 cheat-sheet).
/// `crate_id` is the program crate's **lib** name — the fixture's `use <id>::*` and the `.so` it
/// loads — which is not the analysis identifier (see `SolanaSourceUnit`). `idl` says those items are
/// IDL-generated rather than the program crate's own, which narrows what the fixture may reach for.
#[derive(Template)]
#[template(path = "harness_cheat_sheet.j2", escape = "none")]
pub(crate) struct HarnessCheatSheet<'a> {
    pub(crate) crate_id: &'a str,
    pub(crate) idl: bool,
}

/// A complete, compiling worked example (a different `escrow` program) to pattern-match.
#[derive(Template)]
#[template(path = "example_fixture.j2", escape = "none")]
pub(crate) struct ExampleFixture;

/// Cheat-sheet for authoring the one fn holding a component's properties. Carries nothing: the fn
/// is [`SECTION_FN`](crate::layout::SECTION_FN) in every component, so there is no longer a
/// per-component name for this and the instruction to disagree about.
#[derive(Template)]
#[template(path = "test_cheat_sheet.j2", escape = "none")]
pub(crate) struct TestCheatSheet;

/// Reviewer persona for the review turn (peer of Foundry's judge system prompt).
#[derive(Template)]
#[template(path = "judge_system.j2", escape = "none")]
pub(crate) struct JudgeSystem;

/// How a crate root declares its targets — the `#[cfg]`/`#[invariant_test]` mechanism every entry
/// below it uses. Emitted **once** per root: it explains the layout, not any one target, and one
/// copy per feature was the same paragraph N+1 times in a file a user reads.
///
/// It also declares `PROGRAM_SO`, the `.so` the fixture loads. That path is relative to the harness
/// crate ([`to_project_root`](crate::layout::to_project_root)) and so is the wheel's to spell, not
/// the author's: the fixture prompt names the constant and never counts `../`. Same division as
/// [`SECTION_FN`](crate::layout::SECTION_FN) — the model writes a constant, the wheel owns what
/// varies.
#[derive(Template)]
#[template(path = "root_layout.j2", escape = "none")]
pub(crate) struct RootLayout<'a> {
    pub(crate) program_so: &'a str,
}

/// The crate root's entry for [`PREFLIGHT_FEATURE`](crate::layout::PREFLIGHT_FEATURE) — gated and
/// generated exactly like a component's ([`Section::entry`](crate::section::Section::entry)), but
/// with its body inline: it validates the fixture via `--dry-run` without asserting anything about
/// the program, so there is no authored section to delegate to.
#[derive(Template)]
#[template(path = "preflight_entry.j2", escape = "none")]
pub(crate) struct PreflightEntry<'a> {
    pub(crate) program: &'a str,
    pub(crate) feature: &'a str,
}

/// The crate root's declaration of one component's section: the feature-gated `mod` plus the
/// generated `#[invariant_test]` entry that delegates into it. Why the `#[cfg]` (not the module) is
/// what isolates sections, and why the entry cannot live inside it, is in [`RootLayout`] — said once
/// for the root rather than once per target.
#[derive(Template)]
#[template(path = "section_entry.j2", escape = "none")]
pub(crate) struct SectionEntry<'a> {
    /// The component's Cargo feature (`c_<slug>`), which is also the crate-root entry fn, the
    /// module, and the module file's stem — one name for the whole concept.
    pub(crate) feature: &'a str,
    pub(crate) section_fn: &'a str,
}

/// The module file holding one component's authored tests.
#[derive(Template)]
#[template(path = "section_file.j2", escape = "none")]
pub(crate) struct SectionFile<'a> {
    pub(crate) feature: &'a str,
    pub(crate) section_fn: &'a str,
    pub(crate) body: &'a str,
}

/// The module file standing in for a component formalization gave up on — see
/// [`Section::gave_up`](crate::section::Section::gave_up).
#[derive(Template)]
#[template(path = "gave_up_section.j2", escape = "none")]
pub(crate) struct GaveUpSection<'a> {
    pub(crate) feature: &'a str,
    pub(crate) unit: &'a str,
    pub(crate) reason: &'a str,
}

/// The wheel-authored fixture the **preflight** gate builds (`docs/crucible-application.md` §5.0) —
/// no LLM involved. `crate_id` names both the module the program's types come from and the `.so`
/// basename, exactly as in an authored fixture, so the preflight proves the same two paths resolve.
#[derive(Template)]
#[template(path = "skeleton_fixture.j2", escape = "none")]
pub(crate) struct SkeletonFixture<'a> {
    pub(crate) crate_id: &'a str,
}

/// The pinned `[dependencies]` block for the harness crate. `idl` selects where the program's types
/// come from (see [`ProgramTypes`](crate::harness::ProgramTypes)): the generator crate, or a path
/// dep on the program itself — `package`/`crate_dir` being its Cargo name and its directory
/// *relative to the project root*, which the template joins to `to_root`
/// ([`to_project_root`](crate::layout::to_project_root)).
#[derive(Template)]
#[template(path = "cargo_deps.j2", escape = "none")]
pub(crate) struct CargoDeps<'a> {
    pub(crate) cf: &'a str,
    pub(crate) ctc: &'a str,
    pub(crate) idl: bool,
    pub(crate) idl_gen: &'a str,
    pub(crate) package: &'a str,
    pub(crate) crate_dir: &'a str,
    pub(crate) to_root: &'a str,
    pub(crate) anchor_version: &'a str,
    pub(crate) libafl_version: &'a str,
    pub(crate) solana_version: &'a str,
}

/// The `declare_fuzz_program!` invocation that opens an IDL-path harness's `main.rs`.
#[derive(Template)]
#[template(path = "idl_prelude.j2", escape = "none")]
pub(crate) struct IdlPrelude<'a> {
    pub(crate) module: &'a str,
    pub(crate) path: &'a str,
}

/// The harness crate's `rust-toolchain.toml` — see the template for why it has one.
#[derive(Template)]
#[template(path = "rust_toolchain.j2", escape = "none")]
pub(crate) struct RustToolchain<'a> {
    pub(crate) channel: &'a str,
}

/// The harness crate's `.gitignore`. It sits under the deliverable dir
/// ([`HARNESS_ROOT`](crate::layout::HARNESS_ROOT)), which a user commits — and a run fills it with
/// build output that must not be committed with it.
#[derive(Template)]
#[template(path = "harness_gitignore.j2", escape = "none")]
pub(crate) struct HarnessGitignore;

/// The harness `Cargo.toml` skeleton (`deps` + `feats` are pre-rendered strings). `bin_path` is the
/// crate root the one `[[bin]]` builds — see [`PREFLIGHT_ROOT`](crate::layout::PREFLIGHT_ROOT) for
/// why that varies while its name does not.
#[derive(Template)]
#[template(path = "cargo_toml.j2", escape = "none")]
pub(crate) struct CargoToml<'a> {
    pub(crate) program: &'a str,
    pub(crate) deps: &'a str,
    pub(crate) feats: &'a str,
    pub(crate) bin_path: &'a str,
}

/// The fixture-authoring prompt (the `setup` phase). `listed`/`n` are the properties the fixture
/// must make checkable — the host authors it *after* extraction precisely so they can be here.
#[derive(Template)]
#[template(path = "author_setup.j2", escape = "none")]
pub(crate) struct AuthorSetup<'a> {
    pub(crate) program: &'a str,
    pub(crate) n: usize,
    pub(crate) listed: &'a str,
    pub(crate) cheat: &'a str,
    pub(crate) example: &'a str,
    pub(crate) facts: &'a str,
    pub(crate) model: &'a str,
}

/// The invariant-suite authoring prompt (per component).
#[derive(Template)]
#[template(path = "author_component.j2", escape = "none")]
pub(crate) struct AuthorComponent<'a> {
    pub(crate) unit: &'a str,
    pub(crate) program: &'a str,
    pub(crate) n: usize,
    pub(crate) first: &'a str,
    pub(crate) listed: &'a str,
    pub(crate) component: &'a str,
    pub(crate) cheat: &'a str,
    pub(crate) fixture: &'a str,
}

/// The judge instruction (embeds `judge_guidance.j2` via `{% include %}`).
#[derive(Template)]
#[template(path = "judge_instruction.j2", escape = "none")]
pub(crate) struct JudgeInstruction<'a> {
    pub(crate) program: &'a str,
    pub(crate) listed: &'a str,
    pub(crate) component: &'a str,
    pub(crate) fixture: &'a str,
    pub(crate) spec: &'a str,
}

/// One instruction's mined Anchor facts (a row in `api_facts.j2`).
pub(crate) struct IxFact {
    pub(crate) name: String,
    pub(crate) pascal: String,
    pub(crate) args: Vec<String>,
    pub(crate) accounts: Vec<String>,
}

/// The "API facts" block mined from the analyzed model (crate id, ids, types, instructions).
#[derive(Template)]
#[template(path = "api_facts.j2", escape = "none")]
pub(crate) struct ApiFacts<'a> {
    pub(crate) crate_id: &'a str,
    pub(crate) analysis_id: Option<&'a str>,
    pub(crate) program_id: String,
    pub(crate) account_types: Vec<String>,
    pub(crate) instructions: Vec<IxFact>,
}

#[cfg(test)]
mod tests {
    //! The static prose must survive verbatim, and an interpolated template must leave no hole
    //! unfilled — a placeholder reaching a prompt is a silent instruction to the model.
    use super::*;

    #[test]
    fn static_templates_preserve_their_bytes() {
        // askama drops exactly one trailing newline from a template file, so every `.j2` carries
        // one extra (see the trailing blank line in each). The content is otherwise preserved
        // verbatim, i.e. `render() + "\n" == file`. Asserting that here pins both facts: the
        // static prose is byte-for-byte what shipped, and the one-newline convention holds.
        let eq = |rendered: String, file: &str| assert_eq!(format!("{rendered}\n"), file);
        eq(BackendGuidance.render().unwrap(), include_str!("../templates/backend_guidance.j2"));
        eq(ExampleFixture.render().unwrap(), include_str!("../templates/example_fixture.j2"));
        eq(JudgeSystem.render().unwrap(), include_str!("../templates/judge_system.j2"));
        eq(HarnessGitignore.render().unwrap(), include_str!("../templates/harness_gitignore.j2"));
        // `preflight_entry.j2` is not among these: it interpolates the program and the feature,
        // because the preflight is a gated crate-root entry like any component's. Nor is
        // `root_layout.j2`, which interpolates the `.so` path (see `crate_root_declares_program_so`).
    }

    #[test]
    fn harness_cheat_sheet_substitutes_the_crate_id_and_has_no_placeholder() {
        // The cheat sheet's `use`/`.so` are the crate's lib name, not the analysis identifier.
        let out = HarnessCheatSheet { crate_id: "example_lending", idl: false }.render().unwrap();
        assert!(out.contains("use example_lending::*;"), "crate id not substituted:\n{out}");
        // The `.so` is named, never spelled: its path is relative to a harness dir the author
        // cannot see, so the crate root declares it and the sheet asks for the constant.
        assert!(out.contains("add_program(&program_id, PROGRAM_SO)"), "no PROGRAM_SO in:\n{out}");
        assert!(!out.contains("target/deploy"), "sheet spells a .so path:\n{out}");
        assert!(!out.contains("<program>"), "leftover <program> placeholder");
        assert!(!out.contains("{{"), "leftover askama expression");
        // On the crate path the fixture may use the program's own items, so say nothing about IDLs.
        assert!(!out.contains("GENERATED"), "crate path mentions IDL generation:\n{out}");
        // The `-> bool` contract, which the sheet is the only place to state: it reports whether the
        // ACTION worked, so a correctly-rejected negative attempt returns `true`. The 2026-08-07 e2e
        // fixture returned `false` from all five of its negative actions, which both truncated every
        // campaign that drew one and got their violations auto-labelled harness bugs.
        assert!(out.contains("did this ACTION do what it was designed to do"), "{out}");
        assert!(out.contains("true   // NOT false"), "{out}");
        assert!(out.contains("STOPS the action sequence there"), "{out}");

        // On the IDL path the same `use` holds, plus what the generated module does NOT carry.
        let out = HarnessCheatSheet { crate_id: "example_lending", idl: true }.render().unwrap();
        assert!(out.contains("use example_lending::*;"), "crate id not substituted:\n{out}");
        for needle in [
            "GENERATED from the program's IDL",
            "do NOT write `declare_fuzz_program!`",
            "example_lending::state::*",
        ] {
            assert!(out.contains(needle), "IDL cheat sheet missing {needle:?} in:\n{out}");
        }
        assert!(!out.contains("{{"), "leftover askama expression");
    }

    #[test]
    fn only_the_idl_path_is_told_how_to_write_an_absent_optional_account() {
        // `crucible-idl-gen` drops a `None` optional from the account list where anchor's own derive
        // emits the program id, so on the IDL path a `None` builds an instruction the program
        // rejects before running (see `crate::optional_accounts`). Saying so on the crate path,
        // where anchor is correct, would teach a workaround for a bug that isn't there.
        let idl = HarnessCheatSheet { crate_id: "example_lending", idl: true }.render().unwrap();
        assert!(idl.contains("`Some(self.program_id)` — NEVER `None`"), "{idl}");

        let crate_path =
            HarnessCheatSheet { crate_id: "example_lending", idl: false }.render().unwrap();
        assert!(!crate_path.contains("NEVER `None`"), "{crate_path}");
    }
}
