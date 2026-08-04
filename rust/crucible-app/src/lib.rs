//! The **Crucible** application — AutoProver's Solana verification backend, which
//! authors [Crucible](https://github.com/asymmetric-research/crucible) fuzz harnesses
//! and gates them with the local `crucible` CLI. Pairs with the shared `solana`
//! ecosystem front half (see `docs/crucible-application.md`).
//!
//! A passive [`Backend`] (`docs/rust-backend-api.md`): it supplies the descriptor,
//! toolchain precondition checks, the per-invariant `units`, the authoring prompts
//! (fixture + tests), and the two gating callouts — `compile` (a `crucible … --dry-run`
//! build) and `validate` (one `crucible … --mode explore` fuzz run per unit) — which run
//! the toolchain through the shared `run_confined` launcher. Python owns the loop.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use autoprover_sdk::{
    run_confined, AppDescriptor, ArgDefault, ArgSpec, ArtifactLayout, AuthorInput, Backend,
    CommandOutput, CompileResult, CoreSlot, DeliverableMode, EventKind, Failure, FailureKind,
    Outcome, PhaseSpec, PreflightSpec, ProgramCrate, Prompt, Sandbox, SandboxGrants, SetupSpec,
    Unit, ValidateOutcome, Verdict, WorkspacePrep,
};

use askama::Template;

// The crucible/solana/anchor stack a harness pins (docs/crucible-application.md §6.1). Hardcoded
// for now to the combination the installed toolchain matches (was Python's `CrucibleHarness`).
const ANCHOR_VERSION: &str = "1.0.1";
const SOLANA_VERSION: &str = "3.0";
const LIBAFL_VERSION: &str = "0.15.1";

/// The toolchain the harness crate is built with — the `crucible` CLI forces this channel for the
/// harness build (`try_cargo_build`), so the crate pins it too (see `rust_toolchain.j2`).
const HARNESS_TOOLCHAIN: &str = "stable";

/// Fallback harness-fn name, used when the host sends a unit with no `slug` — i.e. a single-unit
/// host, where a collision is impossible by construction. A multi-unit host (the Solana ecosystem's
/// per-component units) always supplies one; see [`harness_fn`].
const DEFAULT_HARNESS_FN: &str = "c_invariants";

/// The unit's human label, for the authoring/judge prompts. Falls back to the program name for a
/// host that sends no component (a single-unit host).
fn unit_name(input: &AuthorInput) -> String {
    match input.component.get("name").and_then(|v| v.as_str()) {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => input.program.clone(),
    }
}

/// The `#[invariant_test]` fn holding ALL of one component's properties — one harness fn, one
/// build, one fuzz run per component (docs/crucible-component-units.md §8.1). The Crucible macro
/// self-gates `main()` by fn name == feature, so this doubles as the Cargo feature and the
/// `crucible run <program> <feature>` target selector.
///
/// Named from the unit's own slug, which the host puts on `AuthorInput.component` (Solana's
/// `SolanaComponentInstance.feature_json()["slug"]`). Deliberately *not* re-derived here by
/// slugifying the display name: that would put the same slug rule in two languages and let the
/// deliverable's feature names drift from the report's. What this *does* own is spelling that slug
/// as a Rust identifier — see [`ident_of`].
fn harness_fn(input: &AuthorInput) -> String {
    match input.component.get("slug").and_then(|v| v.as_str()) {
        Some(s) if !s.is_empty() => format!("c_{}", ident_of(s)),
        _ => DEFAULT_HARNESS_FN.to_string(),
    }
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

/// The delivered crate's `harness fn -> authored section` map, from the host's outcome set.
///
/// Keyed by the *validation target* each component ran under (the host mirrors those in as
/// `targets`), so the crate declares exactly the features the gated builds selected. Never keyed
/// off `property_units`: those are report **rows**, one per property, and none of them is a feature
/// that gates anything — keying on them is what once wrote the harness fn once per property.
/// A `BTreeMap` keeps the emitted crate stable and sorted.
///
/// Split out of `finalize` so the assembly rule is testable without a crucible checkout (the
/// manifest half of `finalize`'s output only materializes when `$CRUCIBLE_REPO` is set).
fn delivered_sections(outcomes: &serde_json::Value) -> BTreeMap<String, String> {
    let mut sections: BTreeMap<String, String> = BTreeMap::new();
    let Some(comps) = outcomes.get("components").and_then(|v| v.as_array()) else {
        return sections;
    };
    for c in comps {
        if !c.get("delivered").and_then(|v| v.as_bool()).unwrap_or(false) {
            continue;
        }
        let text = c.get("artifact_text").and_then(|v| v.as_str()).unwrap_or_default().trim();
        let targets: Vec<String> = c
            .get("targets")
            .and_then(|v| v.as_array())
            .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
            .unwrap_or_default();
        if targets.is_empty() {
            // A host that sends no targets ran one unnamed target; use the fallback fn.
            sections.insert(DEFAULT_HARNESS_FN.to_string(), text.to_string());
        } else {
            for t in targets {
                sections.insert(t, text.to_string());
            }
        }
    }
    sections
}

// --- askama templates ---------------------------------------------------------------------
// Each struct binds a `.j2` file under `templates/` (the same convention as composer/templates/
// *.j2, here for the Rust side). They replace the former inline string consts and `format!`
// literals: `render()` fills the holes. `escape = "none"` because these render prompts and
// Rust/TOML source — NOT HTML — so no entity escaping. Whitespace is preserved (see askama.toml).

/// Backend-guidance prose injected into the property-extraction prompt. Crucible is a fuzzer,
/// so — like Foundry — refutations are valuable but universals can't be *proven*.
#[derive(Template)]
#[template(path = "backend_guidance.j2", escape = "none")]
struct BackendGuidance;

/// Concise Crucible harness API reference for the fixture-authoring prompt (§7.5 cheat-sheet).
/// `crate_id` is the program crate's **lib** name — the fixture's `use <id>::*` and the `.so` it
/// loads — which is not the analysis identifier (see `ProgramCrate`). `idl` says those items are
/// IDL-generated rather than the program crate's own, which narrows what the fixture may reach for.
#[derive(Template)]
#[template(path = "harness_cheat_sheet.j2", escape = "none")]
struct HarnessCheatSheet<'a> {
    crate_id: &'a str,
    idl: bool,
}

/// A complete, compiling worked example (a different `escrow` program) to pattern-match.
#[derive(Template)]
#[template(path = "example_fixture.j2", escape = "none")]
struct ExampleFixture;

/// Cheat-sheet for authoring the one `#[invariant_test]` fn holding a component's properties.
/// Carries `harness_fn` because the fn's name IS the Cargo feature and fuzz target — a cheat sheet
/// naming a different fn than the instruction does is how the author ends up writing an fn no
/// build selects.
#[derive(Template)]
#[template(path = "test_cheat_sheet.j2", escape = "none")]
struct TestCheatSheet<'a> {
    harness_fn: &'a str,
}

/// Reviewer persona for the `judge_prompt` turn (peer of Foundry's judge system prompt).
#[derive(Template)]
#[template(path = "judge_system.j2", escape = "none")]
struct JudgeSystem;

/// A `#[invariant_test]` probe appended by the host to validate the fixture via `--dry-run`.
#[derive(Template)]
#[template(path = "probe_fn.j2", escape = "none")]
struct ProbeFn;

/// The wheel-authored fixture the **preflight** gate builds (`docs/crucible-application.md` §5.0) —
/// no LLM involved. `crate_id` names both the module the program's types come from and the `.so`
/// basename, exactly as in an authored fixture, so the preflight proves the same two paths resolve.
#[derive(Template)]
#[template(path = "skeleton_fixture.j2", escape = "none")]
struct SkeletonFixture<'a> {
    crate_id: &'a str,
}

/// The pinned `[dependencies]` block for the harness crate. `idl` selects where the program's types
/// come from (see [`ProgramTypes`]): the generator crate, or a path dep on the program itself —
/// `package`/`crate_dir` being its Cargo name and its directory *relative to the project root*,
/// which the template joins to `../../` (the harness crate sits at `fuzz/<program>/`).
#[derive(Template)]
#[template(path = "cargo_deps.j2", escape = "none")]
struct CargoDeps<'a> {
    cf: &'a str,
    ctc: &'a str,
    idl: bool,
    idl_gen: &'a str,
    package: &'a str,
    crate_dir: &'a str,
    anchor_version: &'a str,
    libafl_version: &'a str,
    solana_version: &'a str,
}

/// The `declare_fuzz_program!` invocation that opens an IDL-path harness's `main.rs`.
#[derive(Template)]
#[template(path = "idl_prelude.j2", escape = "none")]
struct IdlPrelude<'a> {
    module: &'a str,
    path: &'a str,
}

/// The harness crate's `rust-toolchain.toml` — see the template for why it has one.
#[derive(Template)]
#[template(path = "rust_toolchain.j2", escape = "none")]
struct RustToolchain<'a> {
    channel: &'a str,
}

/// The harness `Cargo.toml` skeleton (`deps` + `feats` are pre-rendered strings).
#[derive(Template)]
#[template(path = "cargo_toml.j2", escape = "none")]
struct CargoToml<'a> {
    program: &'a str,
    deps: &'a str,
    feats: &'a str,
}

/// Re-author suffix after a failed build / dry-run (leads with extracted compiler errors).
#[derive(Template)]
#[template(path = "revise_compile.j2", escape = "none")]
struct ReviseCompile<'a> {
    errors: &'a str,
    prev_src: &'a str,
    tail: &'a str,
}

/// Re-author suffix after a security reviewer rejected a compiling suite.
#[derive(Template)]
#[template(path = "revise_judge.j2", escape = "none")]
struct ReviseJudge<'a> {
    feedback: &'a str,
    prev_src: &'a str,
}

/// The fixture-authoring prompt (the `setup` phase). `listed`/`n` are the properties the fixture
/// must make checkable — the host authors it *after* extraction precisely so they can be here.
#[derive(Template)]
#[template(path = "author_setup.j2", escape = "none")]
struct AuthorSetup<'a> {
    program: &'a str,
    n: usize,
    listed: &'a str,
    cheat: &'a str,
    example: &'a str,
    facts: &'a str,
    model: &'a str,
    revise: &'a str,
}

/// The invariant-suite authoring prompt (per component).
#[derive(Template)]
#[template(path = "author_component.j2", escape = "none")]
struct AuthorComponent<'a> {
    harness_fn: &'a str,
    unit: &'a str,
    program: &'a str,
    n: usize,
    first: &'a str,
    listed: &'a str,
    component: &'a str,
    cheat: &'a str,
    fixture: &'a str,
    revise: &'a str,
}

/// The judge instruction (embeds `judge_guidance.j2` via `{% include %}`).
#[derive(Template)]
#[template(path = "judge_instruction.j2", escape = "none")]
struct JudgeInstruction<'a> {
    program: &'a str,
    harness_fn: &'a str,
    listed: &'a str,
    component: &'a str,
    fixture: &'a str,
    spec: &'a str,
}

/// One instruction's mined Anchor facts (a row in `api_facts.j2`).
struct IxFact {
    name: String,
    pascal: String,
    args: Vec<String>,
    accounts: Vec<String>,
}

/// The "API facts" block mined from the analyzed model (crate id, ids, types, instructions).
#[derive(Template)]
#[template(path = "api_facts.j2", escape = "none")]
struct ApiFacts<'a> {
    crate_id: &'a str,
    analysis_id: Option<&'a str>,
    program_id: String,
    account_types: Vec<String>,
    instructions: Vec<IxFact>,
}

/// The crucible checkout that resolves the harness crate's path deps (`$CRUCIBLE_REPO`). Read
/// here so crate rendering is fully wheel-owned; `validate_preconditions` guarantees it is set.
fn crucible_repo() -> Option<PathBuf> {
    std::env::var("CRUCIBLE_REPO").ok().map(PathBuf::from)
}

/// Can the harness depend on the program's crate directly?
///
/// Only when the program's Anchor major matches the one this wheel links. Anchor's generated
/// `InstructionData` / `ToAccountMetas` impls belong to the exact `anchor-lang` crate that produced
/// them, so a program on another major can never satisfy `crucible-test-context`'s trait bounds —
/// and its transitive Solana stack usually cannot even co-resolve with ours (Solana 1.17's
/// `solana-frozen-abi` pins `ahash =0.8.5`, while `libafl 0.15` needs `^0.8.11`). No version
/// pinning fixes either; the IDL path exists for exactly this case.
///
/// An unknown requirement (no `anchor-lang`, or a git/path dep) keeps the crate path: that is the
/// historical behaviour, and the compiler reports it precisely if it turns out to be wrong.
fn crate_dep_usable(cr: &ProgramCrate) -> bool {
    match (cr.anchor_compat(), autoprover_sdk::anchor_compat_key(ANCHOR_VERSION)) {
        (Some(theirs), Some(ours)) => theirs == ours,
        _ => true,
    }
}

/// Where the harness gets the program's Anchor types from — the one axis the crate rendering turns
/// on (`docs/crucible-application.md` §6.1).
enum ProgramTypes {
    /// A path dependency on the program's crate: the real types, requiring a matching Anchor major.
    Crate,
    /// Generated from the program's IDL by `crucible-idl-gen`, at this harness-crate-relative path.
    /// The harness does not depend on the program at all, so the program's own toolchain never
    /// enters the harness build — the only way to fuzz a program built against another stack.
    Idl(String),
}

/// Everything the harness crate's rendering needs, derived once per callout from an [`AuthorInput`]
/// (or, in `finalize`, from the outcome set): which program it targets, and where its types come
/// from.
struct HarnessSpec {
    /// The analysis identifier. Names the harness crate itself — `fuzz/<program>/`, package
    /// `<program>_fuzz`, and the selector `crucible run <program>` resolves — nothing else.
    program: String,
    /// The program under test's crate, [`ProgramCrate::resolved`] so every part is populated. Used
    /// for the path dep under [`ProgramTypes::Crate`]; its `lib` is the module holding the
    /// program's types under *either* mode, so the authored fixture's `use <id>::*` is identical.
    cr: ProgramCrate,
    types: ProgramTypes,
}

impl HarnessSpec {
    /// The spec for `input`. The mode follows the host's report — a non-empty `idl` context key
    /// means the host placed an IDL at that (workdir-relative) path because `workspace_prep` asked
    /// for one, so the types are generated. See [`CrucibleApp::workspace_prep`], which makes the
    /// decision.
    fn of(input: &AuthorInput) -> Self {
        Self::new(&input.program, input.program_crate.resolved(&input.program), ctx_str(input, "idl"))
    }

    /// `idl_at` is the IDL's workdir-relative path, or empty for the crate path.
    fn new(program: &str, cr: ProgramCrate, idl_at: String) -> Self {
        let types = if idl_at.is_empty() {
            ProgramTypes::Crate
        } else {
            // `declare_fuzz_program!` resolves its path against the harness crate's manifest dir,
            // so make the host's workdir-relative report crate-relative.
            let prefix = format!("fuzz/{program}/");
            ProgramTypes::Idl(idl_at.strip_prefix(&prefix).unwrap_or(&idl_at).to_string())
        };
        Self { program: program.to_string(), cr, types }
    }

    /// The module the program's `instruction`/`accounts`/state types live under — the crate's lib
    /// name in both modes (the generated module is named for it), so prompts don't branch.
    fn crate_id(&self) -> &str {
        &self.cr.lib
    }

    /// Are the program's types generated from its IDL (rather than its crate)?
    fn is_idl(&self) -> bool {
        matches!(self.types, ProgramTypes::Idl(_))
    }

    /// Where the host should place the IDL when this spec's mode needs one: inside the harness
    /// crate, so the delivered crate carries the IDL it was built against.
    fn idl_dest(&self) -> String {
        format!("fuzz/{}/idls/{}.json", self.program, self.cr.lib)
    }

    /// The `[dependencies]` block — the pinned crucible/solana/anchor stack plus *either* the
    /// program crate as a path dep *or* `crucible-idl-gen` and the three crates its generated code
    /// references: `bytemuck` (zero-copy state casts), `ctor` (schema registration) and `fixed`
    /// (`I80F48` conversions, emitted for any `Wrapped*I80F48`-shaped IDL type — which every
    /// fixed-point lending program has).
    fn deps(&self, repo: &Path) -> String {
        let crates = repo.join("crates");
        let cf = crates.join("crucible-fuzzer").display().to_string();
        let ctc = crates.join("crucible-test-context").display().to_string();
        let idl_gen = crates.join("crucible-idl-gen").display().to_string();
        CargoDeps {
            cf: &cf,
            ctc: &ctc,
            idl: self.is_idl(),
            idl_gen: &idl_gen,
            package: &self.cr.package,
            crate_dir: &self.cr.dir,
            anchor_version: ANCHOR_VERSION,
            libafl_version: LIBAFL_VERSION,
            solana_version: SOLANA_VERSION,
        }
        .render()
        .expect("render cargo_deps")
    }

    /// The harness `Cargo.toml`: one `[[bin]]` (`invariant_test`) selected by a per-component Cargo
    /// feature. `features` are inert (`f = []`) — Crucible's macro self-gates `main()` by fn name ==
    /// feature — so a build only needs the feature it selects declared (was `CrucibleHarness`).
    fn cargo_toml(&self, repo: &Path, features: &[String]) -> String {
        let feats = if features.is_empty() {
            "# (no components yet)".to_string()
        } else {
            features.iter().map(|f| format!("{f} = []")).collect::<Vec<_>>().join("\n")
        };
        let deps = self.deps(repo);
        CargoToml { program: &self.program, deps: &deps, feats: &feats }
            .render()
            .expect("render cargo_toml")
    }

    /// `main.rs` for `spec`: under the IDL path the generated module is declared here rather than
    /// by the model — the host owns the crate's scaffolding, and the authored fixture only ever
    /// writes `use <crate_id>::*`.
    fn main_rs(&self, authored: &str) -> String {
        match &self.types {
            ProgramTypes::Crate => authored.to_string(),
            ProgramTypes::Idl(path) => {
                let decl = IdlPrelude { module: self.crate_id(), path }
                    .render()
                    .expect("render idl_prelude");
                format!("{decl}\n{authored}")
            }
        }
    }

    /// The crate's build files: a `Cargo.toml` declaring exactly `features` (materialized per run —
    /// with `serialize_toolchain` there is no concurrent writer, so no shared-manifest race and no
    /// cumulative feature reservation) plus the `rust-toolchain.toml` that pins which cargo resolves
    /// this directory. Both are needed before dependency warming, which is why they are separable
    /// from the authored `src/main.rs`.
    fn manifest_files(&self, features: &[String]) -> BTreeMap<String, String> {
        let mut files = BTreeMap::new();
        files.insert(
            format!("fuzz/{}/rust-toolchain.toml", self.program),
            RustToolchain { channel: HARNESS_TOOLCHAIN }.render().expect("render rust_toolchain"),
        );
        if let Some(repo) = crucible_repo() {
            files.insert(
                format!("fuzz/{}/Cargo.toml", self.program),
                self.cargo_toml(&repo, features),
            );
        }
        files
    }

    /// The crate's on-disk files for a confined build: the build files plus `src/main.rs`.
    fn files(&self, authored: &str, features: &[String]) -> BTreeMap<String, String> {
        let mut files = self.manifest_files(features);
        files.insert(format!("fuzz/{}/src/main.rs", self.program), self.main_rs(authored));
        files
    }
}

/// The program under test's crate as declared in the entry point's `args` blob (the same value the
/// host puts on every `AuthorInput`), or an empty one when the host resolved none.
fn arg_program_crate(args: &serde_json::Value) -> ProgramCrate {
    args.get("program_crate")
        .and_then(|v| serde_json::from_value(v.clone()).ok())
        .unwrap_or_default()
}

/// The analysis identifier from `args` — the `Name` half of `main_contract` (`path:Name`).
fn arg_program(args: &serde_json::Value) -> &str {
    args.get("main_contract")
        .and_then(|v| v.as_str())
        .and_then(|s| s.split_once(':'))
        .map(|(_, name)| name)
        .unwrap_or_default()
}

/// The directory of `bin` on `$PATH` (for a read-only sandbox grant), if found.
fn which_dir(bin: &str) -> Option<String> {
    let path = std::env::var("PATH").ok()?;
    std::env::split_paths(&path)
        .find(|dir| dir.join(bin).is_file())
        .map(|dir| dir.display().to_string())
}

/// The compiled binaries a Crucible run needs on `PATH`. Checked up-front so a run
/// fails fast with an actionable message rather than deep in the build phase.
const REQUIRED_BINARIES: &[&str] = &["crucible", "cargo-build-sbf", "anchor"];

/// Is `bin` an executable file reachable via `$PATH`? A pure filesystem scan — we do
/// not *run* anything here (validate_preconditions must stay a cheap, sync check).
fn on_path(bin: &str) -> bool {
    let Ok(path) = std::env::var("PATH") else {
        return false;
    };
    std::env::split_paths(&path).any(|dir| dir.join(bin).is_file())
}


/// Extract just the rustc error diagnostics from a (possibly long) cargo build log so
/// the revise prompt leads with the actual errors instead of pages of "Compiling …".
/// Keeps each `error[..]`/`error:` block with its `-->`/`|`/`=` context; drops warnings
/// and progress. Returns "" if there are no error lines.
fn compiler_diagnostics(out: &str) -> String {
    let mut kept: Vec<&str> = Vec::new();
    let mut in_err = false;
    for line in out.lines() {
        let t = line.trim_start();
        if t.starts_with("error[") || t.starts_with("error:") {
            in_err = true;
            kept.push(line);
        } else if in_err {
            if line.is_empty()
                || line.starts_with(' ')
                || t.starts_with("-->")
                || t.starts_with('|')
                || t.starts_with('=')
            {
                kept.push(line);
            } else {
                in_err = false;
            }
        }
    }
    while kept.last().is_some_and(|l| l.trim().is_empty()) {
        kept.pop();
    }
    let joined = kept.join("\n");
    // Cap so a pathological error count can't blow up the prompt.
    joined[..joined.len().min(4000)].to_string()
}

/// snake_case → PascalCase — Anchor's `instruction`/`accounts` struct naming.
fn to_pascal(snake: &str) -> String {
    snake
        .split('_')
        .filter(|s| !s.is_empty())
        .map(|w| {
            let mut c = w.chars();
            match c.next() {
                Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
                None => String::new(),
            }
        })
        .collect()
}

/// A concise, high-signal "API facts" block mined from the analyzed model so the author
/// need not dig through the full JSON (or rediscover Anchor names by exploring): the crate
/// id, declare_id, state types, and each instruction's snake→Pascal name + args + accounts.
/// Returns "" if the model shape isn't recognized.
fn api_facts(analyzed: &serde_json::Value, program: &str, crate_id: &str) -> String {
    let components = match analyzed.get("components").and_then(|c| c.as_array()) {
        Some(c) => c,
        None => return String::new(),
    };
    let is_prog = |c: &&serde_json::Value| c.get("instructions").is_some_and(|i| i.is_array());
    let prog = components
        .iter()
        .find(|c| {
            is_prog(c)
                && (c.get("program_identifier").and_then(|v| v.as_str()) == Some(program)
                    || c.get("name").and_then(|v| v.as_str()) == Some(program))
        })
        .or_else(|| components.iter().find(is_prog));
    let prog = match prog {
        Some(p) => p,
        None => return String::new(),
    };

    let str_of = |v: Option<&serde_json::Value>| v.and_then(|x| x.as_str()).unwrap_or("?").to_string();
    // The crate id is the dependency's actual *lib* name (`ProgramCrate::lib`), NOT the analysis's
    // `program_identifier` — which may be the `#[program] pub mod` name, or just the label the user
    // passed — either of which would mis-resolve `use <id>::*`. Surface the module name as a note
    // only when it differs (the template renders it iff `analysis_id` is `Some`).
    let analysis_raw = str_of(prog.get("program_identifier"));
    let analysis_id: Option<String> =
        (analysis_raw != crate_id && analysis_raw != "?").then_some(analysis_raw);
    let program_id = prog
        .get("program_id")
        .and_then(|v| v.as_str())
        .unwrap_or("(not declared)")
        .to_string();
    let account_types: Vec<String> = prog
        .get("account_types")
        .and_then(|v| v.as_array())
        .map(|types| types.iter().filter_map(|t| t.as_str().map(String::from)).collect())
        .unwrap_or_default();
    let instructions: Vec<IxFact> = prog
        .get("instructions")
        .and_then(|v| v.as_array())
        .map(|ixs| {
            ixs.iter()
                .map(|ix| {
                    let name = str_of(ix.get("name"));
                    let pascal = to_pascal(&name);
                    let args = ix
                        .get("args")
                        .and_then(|v| v.as_array())
                        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
                        .unwrap_or_default();
                    let accounts = ix
                        .get("accounts")
                        .and_then(|v| v.as_array())
                        .map(|a| {
                            a.iter()
                                .filter_map(|x| x.get("name").and_then(|n| n.as_str()).map(String::from))
                                .collect()
                        })
                        .unwrap_or_default();
                    IxFact { name, pascal, args, accounts }
                })
                .collect()
        })
        .unwrap_or_default();
    ApiFacts {
        crate_id,
        analysis_id: analysis_id.as_deref(),
        program_id,
        account_types,
        instructions,
    }
    .render()
    .expect("render api_facts")
}

/// The "previous attempt failed, fix it" suffix shared by both authoring loops: lead with
/// the *extracted* compiler errors, then the prior source, then a trimmed raw-log tail.
fn revise_suffix(prev_src: &str, raw: &str) -> String {
    let errors = compiler_diagnostics(raw);
    let tail = &raw[raw.len().saturating_sub(2500)..];
    ReviseCompile { errors: &errors, prev_src, tail }
        .render()
        .expect("render revise_compile")
}

/// The "previous attempt was rejected by the reviewer" suffix. Unlike `revise_suffix`, the
/// draft *compiled* — so frame it as review feedback to address, not compiler errors to fix
/// (otherwise the author thrashes hunting for build errors that do not exist).
fn judge_revise_suffix(prev_src: &str, feedback: &str) -> String {
    ReviseJudge { feedback, prev_src }
        .render()
        .expect("render revise_judge")
}

/// Dispatch the re-author suffix on which gate rejected the prior attempt.
fn revise_for(f: &Failure) -> String {
    match f.kind {
        FailureKind::Judge => judge_revise_suffix(&f.draft, &f.errors),
        FailureKind::Compile => revise_suffix(&f.draft, &f.errors),
    }
}



/// Did the build fail (as opposed to the harness building and fuzzing)?
///
/// `"Build failed"` is the load-bearing marker and covers more than it looks like: the `crucible`
/// CLI runs the harness build itself and `bail!("Build failed")`s on *any* non-zero `cargo build` —
/// so cargo's pre-compile failures (an unresolvable dependency graph, an unloadable manifest, an
/// unparseable lockfile) arrive here already normalized to that one string, even though none of them
/// prints an `error[` code or a `could not compile` line. The other two markers catch the same
/// failures if they ever reach us without the CLI's wrapper.
fn is_build_error(out: &str) -> bool {
    out.contains("could not compile") || out.contains("error[") || out.contains("Build failed")
}

/// One action in a crash's reproducing sequence, as Crucible records it in
/// `crash_<id>.meta.json`. `success` is whether the *instruction* succeeded, which is the field
/// that makes a finding triageable: a violation whose action failed cannot have been caused by
/// the state transition the property is scoped to.
#[derive(serde::Deserialize)]
struct CrashAction {
    name: String,
    #[serde(default)]
    success: bool,
    #[serde(default)]
    error_code: Option<i64>,
}

impl std::fmt::Display for CrashAction {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // The code is shown only on failure: Crucible sometimes reports `success: true` *with* an
        // `error_code` (seen on the klend run), and rendering that as `OK(3007)` reads like a bug
        // in this formatter rather than the upstream inconsistency it is.
        match (self.success, self.error_code) {
            (false, Some(c)) => write!(f, "{} -> FAIL({c})", self.name),
            (false, None) => write!(f, "{} -> FAIL", self.name),
            (true, _) => write!(f, "{} -> OK", self.name),
        }
    }
}

/// The reproducing sequence Crucible writes next to a crash payload. `iteration` is the fuzzer
/// iteration the violation fired on: `0` means nothing had been mutated yet, so the violation is
/// in the *post-setup fixture state*.
#[derive(serde::Deserialize)]
struct CrashRepro {
    #[serde(default)]
    iteration: u64,
    #[serde(default)]
    actions: Vec<CrashAction>,
}

/// Why a counterexample looks like a *harness* bug rather than a program bug — the two smells
/// that are decidable from the crash metadata alone. Absent when the violation followed a
/// successful state transition, i.e. when the finding looks genuine and deserves a human.
///
/// An enum rather than a bool-plus-string: each variant carries exactly the evidence that
/// justifies it, so a reader gets the specific smell instead of an unexplained "suspect".
enum HarnessSuspicion {
    /// Fired on iteration 0 — before the fuzzer changed anything. The fixture's own initial
    /// state violates the invariant, so no program behaviour is implicated.
    InitialState,
    /// Fired on an action that did not succeed. A property scoped to "after a successful X"
    /// cannot be refuted by an X that reverted; the assertion is running outside its
    /// precondition (or over state the failed action never created).
    ViolatingActionFailed(String),
}

impl std::fmt::Display for HarnessSuspicion {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InitialState => write!(
                f,
                "the violation fired on iteration 0, before the fuzzer changed any state — so \
                 the post-setup FIXTURE state already violates this invariant, and no program \
                 behaviour is implicated. Assert only after the action the property is scoped \
                 to, or exclude the fixture state the property does not cover."
            ),
            Self::ViolatingActionFailed(a) => write!(
                f,
                "the action it fired on did not succeed ({a}) — the state transition this \
                 property is scoped to never happened, so the assertion is running outside its \
                 precondition (or over state that action failed to create). Gate the assertion \
                 on the action succeeding, and skip uninitialized accounts."
            ),
        }
    }
}

/// Where Crucible may have left `crash_<id>.meta.json`, relative to the run's workdir: its
/// default flat output dir, then the per-test layout `tmin`/`show` use.
fn crash_meta_paths(workdir: &Path, program: &str, unit: &str, crash_id: &str) -> Vec<PathBuf> {
    let file = format!("{crash_id}.meta.json");
    vec![
        workdir.join("output").join(&file),
        workdir.join("fuzz").join(program).join("crashes").join(unit).join(&file),
    ]
}

impl CrashRepro {
    /// Read the sequence recorded for `crash_id`. Best-effort by design — a finding must still
    /// report when the metadata is missing or in a shape we don't know; it just reports without
    /// its sequence rather than failing the run.
    fn read(workdir: &Path, program: &str, unit: &str, crash_id: &str) -> Option<Self> {
        crash_meta_paths(workdir, program, unit, crash_id)
            .iter()
            .find_map(|p| std::fs::read_to_string(p).ok())
            .and_then(|s| serde_json::from_str(&s).ok())
    }

    /// The smell, if the metadata shows one. `iteration == 0` is checked first because it is the
    /// stronger statement: nothing was mutated at all, so the sequence is irrelevant.
    fn suspicion(&self) -> Option<HarnessSuspicion> {
        if self.iteration == 0 {
            return Some(HarnessSuspicion::InitialState);
        }
        // The violation fires on the LAST executed action — Crucible stops the sequence there.
        match self.actions.last() {
            Some(a) if !a.success => Some(HarnessSuspicion::ViolatingActionFailed(a.to_string())),
            _ => None,
        }
    }

    /// The sequence as report text. Kept off the first line so the live console view stays
    /// one line per verdict (see `composer.rustapp.adapter`).
    fn render_sequence(&self) -> String {
        let head = format!(
            "reproducing sequence (iteration {}, {} action(s)):",
            self.iteration,
            self.actions.len()
        );
        let body = self
            .actions
            .iter()
            .enumerate()
            .map(|(i, a)| format!("  {}. {a}", i + 1))
            .collect::<Vec<_>>()
            .join("\n");
        if body.is_empty() { head } else { format!("{head}\n{body}") }
    }
}

/// Pull the human-readable finding out of a `crucible run` log so a `BAD` verdict explains
/// itself, and enrich it with the crash's reproducing sequence and any harness-bug smell.
/// Crucible prints `[FUZZ_FINDING] crash:<id> reproduces:<bool> summary:<msg>`, where `<msg>` is
/// the failed `fuzz_assert_*` message.
///
/// The **first line** is the summary plus, when present, a `SUSPECT HARNESS BUG` marker — the
/// signal that decides whether a human should look at all. The sequence follows on later lines.
/// Both reach the report as the rule's `message`; the console shows the first line.
fn finding_detail(out: &str, workdir: &Path, program: &str, unit: &str) -> Option<String> {
    let line = out.lines().find(|l| l.contains("[FUZZ_FINDING]"))?.trim();
    let Some((head, summary)) = line.split_once("summary:") else {
        return Some(line.to_string());
    };
    if summary.trim().is_empty() {
        return Some(line.to_string());
    }
    let crash = head.split_whitespace().find_map(|t| t.strip_prefix("crash:")).unwrap_or("?");
    let mut detail = format!("crash {crash}: {}", summary.trim());

    let Some(repro) = CrashRepro::read(workdir, program, unit, crash) else {
        return Some(detail);
    };
    if let Some(smell) = repro.suspicion() {
        detail.push_str(&format!(" — SUSPECT HARNESS BUG (likely not a program bug): {smell}"));
    }
    detail.push('\n');
    detail.push_str(&repro.render_sequence());
    Some(detail)
}

/// Attribute a shared-target counterexample across the covered report units. Crucible tags each
/// assertion message with its property title (`[<title>]`), so the finding names the invariant it
/// refutes: that unit gets `BAD` (carrying the finding); the rest held over the explored space, so
/// `GOOD`. If nothing can be attributed (no tagged title matched), mark them all `BAD` rather than
/// silently pass a real counterexample. This is the backend's own attribution — the host never
/// parses the finding.
fn attribute_finding(covered: &[Unit], detail: Option<String>) -> ValidateOutcome {
    let d = detail.clone().unwrap_or_default();
    let named: std::collections::HashSet<&str> = covered
        .iter()
        .filter(|u| !u.property.is_empty() && d.contains(&u.property))
        .map(|u| u.unit.as_str())
        .collect();
    let all_bad = named.is_empty();
    ValidateOutcome::Verdicts {
        verdicts: covered
            .iter()
            .map(|u| {
                if all_bad || named.contains(u.unit.as_str()) {
                    let mut v = Verdict::with_outcome(Outcome::Bad);
                    v.detail = detail.clone();
                    (u.unit.clone(), v)
                } else {
                    (u.unit.clone(), Verdict::with_outcome(Outcome::Good))
                }
            })
            .collect(),
    }
}

// ===========================================================================
// Backend glue: small pure helpers shared by the callouts.
// ===========================================================================

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

/// A string field from the input's `context` blob (e.g. the shared fixture source).
fn ctx_str(input: &AuthorInput, key: &str) -> String {
    input.context.get(key).and_then(|v| v.as_str()).unwrap_or_default().to_string()
}

/// A u64 field from the input's `context` blob, with a default.
fn ctx_u64(input: &AuthorInput, key: &str, default: u64) -> u64 {
    input.context.get(key).and_then(|v| v.as_u64()).unwrap_or(default)
}

/// The compiler errors to hand back to the model — extracted diagnostics, else a raw tail.
fn build_errors(out: &CommandOutput) -> String {
    let combined = format!("{}\n{}", out.stdout, out.stderr);
    let d = compiler_diagnostics(&combined);
    if d.is_empty() {
        combined[combined.len().saturating_sub(2000)..].to_string()
    } else {
        d
    }
}

// ===========================================================================
// The backend.
// ===========================================================================

struct CrucibleApp;

impl Backend for CrucibleApp {
    fn descriptor(&self) -> AppDescriptor {
        AppDescriptor {
            name: "crucible".to_string(),
            header_text: "Crucible — Solana fuzzing backend | AutoProver".to_string(),
            // Selects the shared `solana` ecosystem front half (system model + prompts).
            ecosystem: "solana".to_string(),
            backend_tag: "crucible".to_string(),
            backend_guidance: BackendGuidance.render().expect("render backend_guidance"),
            analysis_key: "crucible-solana-analysis".to_string(),
            phases: vec![
                // UI-only phase: discover the design doc when one isn't supplied (§host).
                PhaseSpec { key: "discover_design_doc".into(), label: "Design Doc Discovery".into(), order: 0, core_slot: None },
                // UI-only phase: the program `.so` + IDL + the skeleton harness build (§5.0). It
                // runs CONCURRENTLY with the two phases below it — the order is the declaration's,
                // which is what the frontend lists sections by, not a claim about sequencing.
                PhaseSpec { key: "preflight".into(), label: "Build Preflight".into(), order: 1, core_slot: None },
                PhaseSpec { key: "analysis".into(), label: "System Analysis".into(), order: 2, core_slot: Some(CoreSlot::Analysis) },
                PhaseSpec { key: "extraction".into(), label: "Property Extraction".into(), order: 3, core_slot: Some(CoreSlot::Extraction) },
                // UI-only phase: author the shared fixture, once every property is known (§5.2).
                PhaseSpec { key: "harness_fixture".into(), label: "Harness Fixture".into(), order: 4, core_slot: None },
                PhaseSpec { key: "formalization".into(), label: "Harness Authoring".into(), order: 5, core_slot: Some(CoreSlot::Formalization) },
                PhaseSpec { key: "report".into(), label: "Report".into(), order: 6, core_slot: Some(CoreSlot::Report) },
            ],
            // Only `--fuzz-timeout` is wired through to `crucible run`. Other tuning knobs
            // (parallel cores, stateful mode, a version pin) are deliberately omitted until
            // they're actually threaded to the fuzz command — an inert flag is worse than none.
            args: vec![
                ArgSpec {
                    flag: "--fuzz-timeout".to_string(),
                    help: "Per-test fuzzing budget in seconds (`crucible run --timeout`).".to_string(),
                    default: ArgDefault::Int { value: Some(60) },
                    required: false,
                },
                // The IDL path is chosen automatically for a program whose Anchor major this
                // harness can't link (§6.1), but only if an IDL can be produced — which needs the
                // *program's* `anchor` CLI. This flag supplies one instead (any Anchor IDL format;
                // the generator converts legacy IDLs itself), and also forces the IDL path for a
                // program that would otherwise be linked directly.
                ArgSpec {
                    flag: "--program-idl".to_string(),
                    help: "Path to the program's IDL JSON. Generates the harness's types from it \
                           instead of depending on the program crate — required for a program \
                           built against a different Anchor/Solana stack than Crucible's."
                        .to_string(),
                    default: ArgDefault::Str { value: None },
                    required: false,
                },
            ],
            rag_db_default: Some("crucible_kb".to_string()),
            event_kinds: vec![
                EventKind::log("fuzz_pulse", "Fuzzing"),
                EventKind::log("fuzz_finding", "Finding"),
                EventKind::log("build_output", "Build"),
                // The reviewer (judge) turn's accept/reject on a compiled test suite.
                EventKind::log("judge", "Review"),
                // The per-invariant verdict — surfaced as a persistent callout + toast.
                EventKind::notice("verdict", "Verdict"),
            ],
            // Metadata (properties.json / commentary / property→tests map) lands under
            // `certora/crucible/` — the split Foundry uses — while the crate deliverable is the
            // one file under `fuzz/<program>/` (deliverable_primary + the finalize render).
            artifact_layout: ArtifactLayout {
                deliverable_dir: "certora/crucible".into(),
                internal_dir: ".certora_internal/crucible".into(),
                report_dir: "certora/crucible/reports".into(),
                artifact_dir: "certora/crucible/harnesses".into(),
                artifact_prefix: "harness".into(),
                artifact_extension: "rs".into(),
                property_suffix: "property_tests".into(),
                deliverable_primary: Some("fuzz/{program}/src/main.rs".into()),
            },
            // The whole toolchain surface gated once, up front, against a skeleton this wheel
            // authors itself — a dependency or codegen problem is not something a fixture author
            // can fix, so it must not first appear as compiler errors in its draft.
            preflight: Some(PreflightSpec {
                phase_key: "preflight".into(),
                label: "Build Preflight".into(),
            }),
            // A shared fixture authored once (the setup step), one crate assembled by finalize
            // (callout), all toolchain runs serialized on the one crate/target, confined by
            // default (untrusted native builds), and "instruction" as the unit noun.
            setup: Some(SetupSpec {
                phase_key: "harness_fixture".into(),
                label: "Harness Fixture".into(),
                context_key: "fixture".into(),
            }),
            deliverable_mode: DeliverableMode::Callout,
            serialize_toolchain: true,
            confine_by_default: true,
            // Units are the Solana ecosystem's `ProgramComponent`s, so the SDK default noun is
            // right — this used to say "instruction", from the long-gone per-instruction units.
            component_noun: None,
        }
    }

    fn validate_preconditions(&self, args: &serde_json::Value) -> Result<(), String> {
        let mut problems: Vec<String> = Vec::new();

        let missing: Vec<&str> = REQUIRED_BINARIES
            .iter()
            .copied()
            .filter(|b| !on_path(b))
            .collect();
        if !missing.is_empty() {
            problems.push(format!(
                "required tool(s) not found on PATH: {}. Install the Solana toolchain \
                 (solana-cli / cargo-build-sbf), Anchor, and the crucible CLI \
                 (`cargo install --path crates/crucible-fuzz-cli`).",
                missing.join(", ")
            ));
        }

        // The target must be a buildable Cargo/Anchor workspace (cf. foundry's
        // foundry.toml precondition). We only check structure here; the actual build
        // happens in the build phase.
        if let Some(root) = args.get("project_root").and_then(|v| v.as_str()) {
            if !Path::new(root).join("Cargo.toml").is_file() {
                problems.push(format!(
                    "{root}/Cargo.toml not found — Crucible needs a buildable Cargo/Anchor \
                     workspace containing the program's crate."
                ));
            }
            // The harness declares the program under test as a path dep, so its crate must exist:
            // check it here rather than let a wrong directory surface as a confusing "failed to
            // load manifest for dependency" deep in an offline build.
            let cr = arg_program_crate(args).resolved(arg_program(args));
            let manifest = Path::new(root).join(&cr.dir).join("Cargo.toml");
            if !manifest.is_file() {
                problems.push(format!(
                    "no Cargo crate for the program under test at {}/Cargo.toml — Crucible \
                     declares it as a path dependency of the harness. Point the main-contract \
                     path at a source file inside the program's crate.",
                    cr.dir
                ));
            }
        } else {
            problems.push("no project_root in args".to_string());
        }

        // The crucible checkout resolves the harness crate's path deps (§6.1). Was
        // `resolve_crucible_repo` in Python; now the wheel owns it (it renders the deps).
        match std::env::var("CRUCIBLE_REPO") {
            Ok(repo) if Path::new(&repo).join("crates/crucible-fuzzer").is_dir() => {}
            Ok(repo) => problems.push(format!(
                "$CRUCIBLE_REPO={repo} has no crates/crucible-fuzzer — set it to a local crucible \
                 clone (the harness deps resolve against it)."
            )),
            Err(_) => problems.push(
                "$CRUCIBLE_REPO is not set — point it at a local crucible clone (must contain \
                 crates/crucible-fuzzer); the harness crate's path deps resolve against it."
                    .to_string(),
            ),
        }

        if problems.is_empty() {
            Ok(())
        } else {
            Err(problems.join("\n"))
        }
    }

    fn units(&self, input: &AuthorInput) -> Vec<Unit> {
        // The setup fixture has no report units. One component's properties all live in ONE
        // harness fn ([`harness_fn`]) — a single build + fuzz run per component
        // (docs/crucible-component-units.md §8.1) — but each property is still its own report row,
        // mapping to that shared fuzz target. The host runs each distinct target once and
        // attributes a counterexample to the offending property via the finding message.
        if input.kind == "setup" {
            return Vec::new();
        }
        input
            .props
            .iter()
            .enumerate()
            .map(|(i, p)| {
                let slug = if p.slug.is_empty() { format!("inv{i}") } else { p.slug.clone() };
                // Report row = c_<prop slug> (one per property); fuzz target = the component's fn.
                Unit {
                    property: p.title.clone(),
                    unit: format!("c_{slug}"),
                    target: Some(harness_fn(input)),
                }
            })
            .collect()
    }

    fn author_prompt(&self, input: &AuthorInput, failure: Option<&Failure>) -> Prompt {
        let program = &input.program;
        let revise = failure.map(revise_for).unwrap_or_default();
        let instruction = if input.kind == "setup" {
            // Author the shared fixture from the analyzed model (carried in `component`).
            let analyzed = &input.component;
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
                revise: &revise,
            }
            .render()
            .expect("render author_setup")
        } else {
            // Author ONE #[invariant_test] fn holding all of THIS component's properties.
            let hf = harness_fn(input);
            let listed = listed_props(input);
            let component = serde_json::to_string_pretty(&input.component)
                .unwrap_or_else(|_| input.component.to_string());
            let fixture = ctx_str(input, "fixture");
            let cheat = TestCheatSheet { harness_fn: &hf }.render().expect("render test_cheat_sheet");
            AuthorComponent {
                harness_fn: &hf,
                unit: &unit_name(input),
                program,
                n: input.props.len(),
                first: input.props.first().map(|p| p.title.as_str()).unwrap_or("property"),
                listed: &listed,
                component: &component,
                cheat: &cheat,
                fixture: &fixture,
                revise: &revise,
            }
            .render()
            .expect("render author_component")
        };
        Prompt { system: None, instruction }
    }

    fn judge_prompt(&self, input: &AuthorInput, spec: &str) -> Option<Prompt> {
        // The shared fixture is scaffolding, not test evidence — the compile/dry-run gate
        // already vets it, and there is no property to judge it against. Judge only the
        // per-component test suites (the peer of Foundry's feedback judge).
        if input.kind == "setup" {
            return None;
        }
        let program = &input.program;
        let listed = listed_props(input);
        let component = serde_json::to_string_pretty(&input.component)
            .unwrap_or_else(|_| input.component.to_string());
        let fixture = ctx_str(input, "fixture");
        let instruction = JudgeInstruction {
            program,
            harness_fn: &harness_fn(input),
            listed: &listed,
            component: &component,
            fixture: &fixture,
            spec,
        }
        .render()
        .expect("render judge_instruction");
        let system = JudgeSystem.render().expect("render judge_system");
        Some(Prompt { system: Some(system), instruction })
    }

    fn compile(
        &self,
        input: &AuthorInput,
        spec: &str,
        workdir: &Path,
        sandbox: &Sandbox,
    ) -> CompileResult {
        let program = &input.program;
        let hspec = HarnessSpec::of(input);
        let probe = || ProbeFn.render().expect("render probe_fn");
        // Preflight: the wheel's OWN skeleton — `spec` is empty, nothing has been authored yet.
        // Setup: dry-run the authored fixture behind that same probe test. Component: fixture + the
        // authored tests, dry-run behind that component's harness feature (which gates `main`).
        let (main_rs, feature) = match input.kind.as_str() {
            "preflight" => {
                let skeleton = SkeletonFixture { crate_id: hspec.crate_id() }
                    .render()
                    .expect("render skeleton_fixture");
                (format!("{skeleton}{}", probe()), "c_probe".to_string())
            }
            "setup" => (format!("{spec}{}", probe()), "c_probe".to_string()),
            _ => {
                let fixture = ctx_str(input, "fixture");
                (format!("{fixture}\n\n{spec}"), harness_fn(input))
            }
        };
        let files = hspec.files(&main_rs, std::slice::from_ref(&feature));
        let args = vec![
            "run".to_string(),
            program.clone(),
            feature,
            "--release".to_string(),
            "--dry-run".to_string(),
        ];
        match run_confined(sandbox, "crucible", &args, &files, workdir) {
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
        unit: &str,
        workdir: &Path,
        sandbox: &Sandbox,
    ) -> ValidateOutcome {
        let program = &input.program;
        let fixture = ctx_str(input, "fixture");
        let timeout = ctx_u64(input, "fuzz_timeout", 30);
        let files = HarnessSpec::of(input)
            .files(&format!("{fixture}\n\n{spec}"), std::slice::from_ref(&unit.to_string()));
        let args = vec![
            "run".to_string(),
            program.clone(),
            unit.to_string(),
            "--release".to_string(),
            "--mode".to_string(),
            "explore".to_string(),
            "--timeout".to_string(),
            timeout.to_string(),
        ];
        // The report units this fuzz target covers (Crucible: a component's properties share its fn).
        // The backend owns attribution — it maps ONE run to a verdict per covered unit.
        let covered: Vec<Unit> =
            self.units(input).into_iter().filter(|u| u.target_or_unit() == unit).collect();
        let all = |o: Outcome, detail: Option<String>| -> ValidateOutcome {
            ValidateOutcome::Verdicts {
                verdicts: covered
                    .iter()
                    .map(|u| {
                        let mut v = Verdict::with_outcome(o);
                        v.detail = detail.clone();
                        (u.unit.clone(), v)
                    })
                    .collect(),
            }
        };
        match run_confined(sandbox, "crucible", &args, &files, workdir) {
            Ok(out) => {
                let combined = format!("{}\n{}", out.stdout, out.stderr);
                // Order matters: a fuzz finding and a clean run both mean the harness BUILT, so
                // classify those first — only a *non-zero* exit with build markers is a real
                // build failure. This keeps `error[...]`-looking runtime/log text in a clean
                // (exit 0) fuzz run from being misread as a build failure.
                if combined.contains("[FUZZ_FINDING]") {
                    // A crash refutes ONE invariant — pin BAD to the property the finding names
                    // (each assertion is tagged `[<title>]`), holding the rest GOOD over the
                    // explored space. If it can't be attributed, mark all BAD (never hide it).
                    attribute_finding(
                        &covered,
                        finding_detail(&combined, workdir, program, unit),
                    )
                } else if out.exit_code == 0 {
                    all(Outcome::Good, None) // ran to the budget with no violation = every invariant held
                } else if is_build_error(&combined) {
                    // Shared build; re-author the whole spec (docs/rust-backend-api.md).
                    ValidateOutcome::BuildFailed { errors: build_errors(&out) }
                } else {
                    // Non-zero exit with no build markers and no finding — capture the tail.
                    all(Outcome::Error, Some(build_errors(&out)))
                }
            }
            Err(e) => all(Outcome::Error, Some(e)),
        }
    }

    fn sandbox_grants(&self, _args: &serde_json::Value) -> SandboxGrants {
        // Read-only grants beyond the launcher's discovered Rust toolchain: the crucible checkout
        // (path deps) and the `crucible` binary's dir. Was Python's `crucible_sandbox` extra_ro.
        let mut extra_ro = Vec::new();
        if let Ok(repo) = std::env::var("CRUCIBLE_REPO") {
            extra_ro.push(repo);
        }
        if let Some(dir) = which_dir("crucible") {
            extra_ro.push(dir);
        }
        SandboxGrants { extra_ro, extra_env: Vec::new() }
    }

    fn workspace_prep(&self, input: &AuthorInput) -> WorkspacePrep {
        // Place a deps-only harness manifest (probe feature) so warming has a manifest and the
        // setup dry-run can select a feature; per-run builds overwrite it with their own feature.
        // Then warm the harness crate's deps and build the program `.so` (the host runs both with
        // the shared helpers — fetch unconfined, build confined+offline).
        //
        // This is also where the crate-vs-IDL decision is made, ONCE: the program's own Anchor
        // version decides it (`crate_dep_usable`), or the operator forces the IDL path by supplying
        // one. Later callouts don't re-derive it — they read the `idl` context key the host sets
        // after placing the file, so the whole run renders one consistent crate.
        let program = &input.program;
        let cr = input.program_crate.resolved(program);
        let forced = !ctx_str(input, "program_idl").is_empty();
        let dest = HarnessSpec::new(program, cr.clone(), String::new()).idl_dest();
        let idl_dest = (forced || !crate_dep_usable(&cr)).then_some(dest);
        // Render the manifest for the mode just decided — warming must fetch the deps the real
        // builds will use (under the IDL path the program's own graph is never resolved at all) —
        // and pin the toolchain, so the fetch resolves with the cargo that will do the building.
        let spec = HarnessSpec::new(program, cr.clone(), idl_dest.clone().unwrap_or_default());
        WorkspacePrep {
            files: spec.manifest_files(std::slice::from_ref(&"c_probe".to_string())),
            warm_dirs: vec![format!("fuzz/{program}")],
            // The `.so` is named after the crate's lib target, not the analysis identifier. Needed
            // under both paths: LiteSVM loads the compiled program either way.
            build_program: Some(cr.lib),
            idl_dest,
        }
    }

    fn finalize(&self, outcomes: &serde_json::Value) -> BTreeMap<String, String> {
        // Assemble the one deliverable crate: the shared fixture + every delivered invariant's
        // test section, keyed by its feature (was Python's `CrucibleHarness`/`CrucibleArtifactStore`).
        let program = outcomes.get("program").and_then(|v| v.as_str()).unwrap_or_default();
        let fixture = outcomes.get("setup").and_then(|v| v.as_str()).unwrap_or_default();
        // The host mirrors `AuthorInput.program_crate` and the placed `idl` into the outcome set —
        // the delivered crate must be the same one the gated builds used, or it won't compile for
        // the user.
        let cr = outcomes
            .get("program_crate")
            .and_then(|v| serde_json::from_value::<ProgramCrate>(v.clone()).ok())
            .unwrap_or_default();
        let idl = outcomes.get("idl").and_then(|v| v.as_str()).unwrap_or_default();
        let spec = HarnessSpec::new(program, cr.resolved(program), idl.to_string());

        let sections = delivered_sections(outcomes);
        if program.is_empty() || sections.is_empty() {
            return BTreeMap::new();
        }

        // Emit each distinct section once. Normally every component contributes its own fn and the
        // sections already differ; the guard is for two targets that resolved to the same authored
        // source, which would otherwise write that harness fn twice and fail to compile. (The gated
        // builds never see this: `compile`/`validate` assemble fixture + spec directly, and only
        // this render folds the whole outcome set into one crate.)
        let mut body_parts: Vec<&str> = Vec::new();
        for text in sections.values() {
            if !body_parts.contains(&text.as_str()) {
                body_parts.push(text);
            }
        }
        let body = body_parts.join("\n\n");
        // Declare exactly the features that gate a delivered `main()` — one per component, which is
        // what `crucible run <program> <fn>` selects.
        let features: Vec<String> = sections.keys().cloned().collect();
        let authored = format!(
            "{}\n\n{}{}",
            fixture.trim_end(),
            body,
            if body.is_empty() { "" } else { "\n" }
        );
        spec.files(&authored, &features)
    }
}

autoprover_sdk::export_app!(crucible_app, CrucibleApp);

#[cfg(test)]
mod template_parity {
    //! Guards the askama migration: the build-critical crate files must render byte-identically
    //! to the former `format!` output (else the harness crate won't compile), and the static
    //! prose templates must preserve their bytes. Prompts are checked for template residue only.
    use super::*;

    /// The expected `[dependencies]` block, spelled out independently of the template (originally
    /// the `crucible_deps` `format!` body, kept as the oracle across the askama migration). The
    /// program's types come either from its crate or from `crucible-idl-gen` — never both.
    fn expected_deps(spec: &HarnessSpec, repo: &Path) -> String {
        let crates = repo.join("crates");
        let (idl_deps, program_dep) = if spec.is_idl() {
            (
                format!(
                    "bytemuck = \"1.14\"\n\
                     crucible-idl-gen = {{ path = \"{}\" }}\n\
                     ctor = \"0.6\"\n\
                     ctrlc = \"3.4\"\n\
                     fixed = \"1\"\n",
                    crates.join("crucible-idl-gen").display()
                ),
                String::new(),
            )
        } else {
            (
                "ctrlc = \"3.4\"\n".to_string(),
                format!(
                    "{} = {{ path = \"../../{}\", features = [\"no-entrypoint\"] }}\n",
                    spec.cr.package, spec.cr.dir
                ),
            )
        };
        format!(
            "crucible-fuzzer = {{ path = \"{cf}\" }}\n\
             crucible-test-context = {{ path = \"{ctc}\" }}\n\
             anchor-lang = \"{ANCHOR_VERSION}\"\n\
             arbitrary = {{ version = \"1\", features = [\"derive\"] }}\n\
             {idl_deps}\
             libafl = {{ version = \"{LIBAFL_VERSION}\", features = [\"std\", \"cli\", \"prelude\"] }}\n\
             libafl_bolts = {{ version = \"{LIBAFL_VERSION}\", features = [\"std\"] }}\n\
             {program_dep}solana-keypair = \"{SOLANA_VERSION}\"\n\
             solana-pubkey = \"{SOLANA_VERSION}\"\n\
             solana-signer = \"{SOLANA_VERSION}\"",
            cf = crates.join("crucible-fuzzer").display(),
            ctc = crates.join("crucible-test-context").display(),
        )
    }

    /// The expected harness manifest, spelled out independently of the template.
    fn expected_cargo_toml(spec: &HarnessSpec, repo: &Path, features: &[String]) -> String {
        let feats = if features.is_empty() {
            "# (no components yet)".to_string()
        } else {
            features.iter().map(|f| format!("{f} = []")).collect::<Vec<_>>().join("\n")
        };
        format!(
            "[package]\n\
             name = \"{program}_fuzz\"\n\
             version = \"0.1.0\"\n\
             edition = \"2021\"\n\
             \n\
             [workspace]\n\
             \n\
             [dependencies]\n\
             {deps}\n\
             \n\
             [[bin]]\n\
             name = \"invariant_test\"\n\
             path = \"src/main.rs\"\n\
             \n\
             [features]\n\
             {feats}\n",
            program = spec.program,
            deps = expected_deps(spec, repo),
        )
    }

    /// A crate whose directory, package and lib names all differ from the analysis identifier —
    /// the lend shape (`programs/lend`, package `example_lending`), which is what the
    /// `programs/<program>` convention got wrong. `anchor` is ours, so it stays linkable.
    fn distinct_crate() -> ProgramCrate {
        ProgramCrate {
            dir: "programs/lend".into(),
            package: "example-lending".into(),
            lib: "example_lending".into(),
            anchor: ANCHOR_VERSION.into(),
        }
    }

    /// The same crate on an Anchor major this harness cannot link — lend's real one.
    fn skewed_crate() -> ProgramCrate {
        ProgramCrate { anchor: "0.29.0".into(), ..distinct_crate() }
    }

    fn spec_of(cr: ProgramCrate, idl_at: &str) -> HarnessSpec {
        HarnessSpec::new("vault", cr, idl_at.to_string())
    }

    fn prep_input(cr: ProgramCrate, context: serde_json::Value) -> AuthorInput {
        AuthorInput {
            kind: "setup".into(),
            program: "vault".into(),
            program_crate: cr,
            component: serde_json::Value::Null,
            props: vec![],
            context,
        }
    }

    #[test]
    fn crate_files_render_the_expected_manifest() {
        let repo = Path::new("/home/user/crucible");
        let specs = [
            spec_of(ProgramCrate::default().resolved("vault"), ""),
            spec_of(distinct_crate(), ""),
            spec_of(skewed_crate(), "fuzz/vault/idls/example_lending.json"),
        ];
        for spec in &specs {
            assert_eq!(spec.deps(repo), expected_deps(spec, repo));
            // empty features
            assert_eq!(spec.cargo_toml(repo, &[]), expected_cargo_toml(spec, repo, &[]));
            // one and several features
            for feats in
                [vec!["c_invariants".to_string()], vec!["c_probe".into(), "c_invariants".into()]]
            {
                assert_eq!(
                    spec.cargo_toml(repo, &feats),
                    expected_cargo_toml(spec, repo, &feats),
                    "cargo_toml mismatch for features {feats:?}",
                );
            }
        }
    }

    #[test]
    fn the_program_dep_points_at_the_resolved_crate_not_the_analysis_id() {
        let repo = Path::new("/home/user/crucible");
        let deps = spec_of(distinct_crate(), "").deps(repo);
        // Keyed by the Cargo package name, pointing at the real directory — both independent of
        // the `vault` identifier the harness crate itself is named after.
        assert!(
            deps.contains(
                "example-lending = { path = \"../../programs/lend\", features = [\"no-entrypoint\"] }"
            ),
            "unexpected program dep in:\n{deps}"
        );
        // The pre-`program_crate` convention still holds when the host resolved nothing.
        let legacy = spec_of(ProgramCrate::default().resolved("vault"), "").deps(repo);
        assert!(
            legacy.contains(
                "vault = { path = \"../../programs/vault\", features = [\"no-entrypoint\"] }"
            ),
            "unexpected fallback dep in:\n{legacy}"
        );
    }

    #[test]
    fn the_idl_path_drops_the_program_dep_and_declares_the_generated_module() {
        let repo = Path::new("/home/user/crucible");
        let spec = spec_of(skewed_crate(), "fuzz/vault/idls/example_lending.json");
        let deps = spec.deps(repo);
        // Nothing about the program under test is in the graph — that is the whole point: its
        // Anchor/Solana stack can neither co-resolve with ours nor satisfy our trait bounds.
        assert!(!deps.contains("programs/lend"), "program still a dependency:\n{deps}");
        assert!(!deps.contains("no-entrypoint"), "program still a dependency:\n{deps}");
        for needle in ["crucible-idl-gen = { path =", "bytemuck = \"1.14\"", "ctor = \"0.6\""] {
            assert!(deps.contains(needle), "missing {needle:?} in:\n{deps}");
        }
        // The generated module is declared for the model, keyed to the crate's lib name so the
        // authored fixture's `use <id>::*` reads the same as on the crate path. The macro resolves
        // its path against the harness crate, so the host's workdir-relative report is stripped.
        let main_rs = spec.main_rs("use example_lending::*;\n");
        assert!(
            main_rs.contains(
                "crucible_idl_gen::declare_fuzz_program!(example_lending = \"idls/example_lending.json\");"
            ),
            "unexpected prelude in:\n{main_rs}"
        );
        assert!(main_rs.ends_with("use example_lending::*;\n"), "authored source not kept verbatim");
        // The crate path adds nothing to what the model wrote.
        assert_eq!(spec_of(distinct_crate(), "").main_rs("fn x() {}"), "fn x() {}");
    }

    #[test]
    fn the_harness_crate_pins_its_own_toolchain() {
        // rustup picks a toolchain by directory and the harness crate lives *inside* the target
        // project, so without this file the project's `rust-toolchain.toml` would decide which cargo
        // warms the deps — a different one from the `crucible` build's, and often too old to parse a
        // dependency's manifest at all.
        let spec = spec_of(distinct_crate(), "");
        let pin = &spec.manifest_files(&[])["fuzz/vault/rust-toolchain.toml"];
        assert!(pin.contains(&format!("channel = \"{HARNESS_TOOLCHAIN}\"")), "unexpected pin:\n{pin}");
        // Emitted with the crate under every path: warming (manifest only) and the deliverable.
        assert!(spec.files("fn main() {}", &[]).contains_key("fuzz/vault/rust-toolchain.toml"));
        let plan = CrucibleApp.workspace_prep(&prep_input(distinct_crate(), serde_json::Value::Null));
        assert!(plan.files.contains_key("fuzz/vault/rust-toolchain.toml"));
    }

    #[test]
    fn workspace_prep_builds_the_lib_artifact_and_warms_the_harness_dir() {
        let plan = CrucibleApp.workspace_prep(&prep_input(distinct_crate(), serde_json::Value::Null));
        // The harness dir follows the identifier (`crucible run vault`)…
        assert_eq!(plan.warm_dirs, vec!["fuzz/vault".to_string()]);
        // …but the `.so` to build is the program crate's lib target.
        assert_eq!(plan.build_program.as_deref(), Some("example_lending"));
        // A linkable program needs no IDL.
        assert_eq!(plan.idl_dest, None);
    }

    #[test]
    fn workspace_prep_asks_for_an_idl_when_the_program_cannot_be_linked() {
        // Anchor 0.29 vs ours ⇒ the crate path is impossible, so the wheel requests an IDL and
        // renders the warming manifest for the IDL path (else warming would resolve — and fail on —
        // the program's own dependency graph).
        let plan = CrucibleApp.workspace_prep(&prep_input(skewed_crate(), serde_json::Value::Null));
        assert_eq!(plan.idl_dest.as_deref(), Some("fuzz/vault/idls/example_lending.json"));
        if let Some(repo) = crucible_repo() {
            let cargo = &plan.files["fuzz/vault/Cargo.toml"];
            assert!(cargo.contains("crucible-idl-gen"), "warming manifest not on the IDL path");
            assert!(!cargo.contains("programs/lend"), "warming manifest still links the program");
            let _ = repo;
        }
        // The `.so` is still built: LiteSVM loads the real program either way.
        assert_eq!(plan.build_program.as_deref(), Some("example_lending"));
        // An operator can force the IDL path for a program that *is* linkable.
        let forced = CrucibleApp.workspace_prep(&prep_input(
            distinct_crate(),
            serde_json::json!({ "program_idl": "/tmp/lend.json" }),
        ));
        assert_eq!(forced.idl_dest.as_deref(), Some("fuzz/vault/idls/example_lending.json"));
    }

    #[test]
    fn crate_dep_usability_tracks_the_anchor_compatibility_unit() {
        let with = |anchor: &str| ProgramCrate { anchor: anchor.into(), ..distinct_crate() };
        // Same major as ours (1.0.1) — patch/minor differences are compatible.
        for ok in [ANCHOR_VERSION, "1.0.0", "^1.2", "=1.9.9"] {
            assert!(crate_dep_usable(&with(ok)), "{ok} should be linkable");
        }
        // A 0.x release is its own compatibility unit, so every one of them is out.
        for bad in ["0.29.0", "0.31", "^0.30.1", "2.0.0"] {
            assert!(!crate_dep_usable(&with(bad)), "{bad} should NOT be linkable");
        }
        // Unknown (not an Anchor program, or a git dep) keeps the historical crate path.
        for unknown in ["", "*", "workspace"] {
            assert!(crate_dep_usable(&with(unknown)), "{unknown:?} should fall back to the crate");
        }
    }

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
        eq(ProbeFn.render().unwrap(), include_str!("../templates/probe_fn.j2"));
    }

    #[test]
    fn harness_cheat_sheet_substitutes_the_crate_id_and_has_no_placeholder() {
        // The cheat sheet's `use`/`.so` are the crate's lib name, not the analysis identifier.
        let out = HarnessCheatSheet { crate_id: "example_lending", idl: false }.render().unwrap();
        assert!(out.contains("use example_lending::*;"), "crate id not substituted:\n{out}");
        assert!(
            out.contains("\"../../target/deploy/example_lending.so\""),
            ".so path not substituted:\n{out}"
        );
        assert!(!out.contains("<program>"), "leftover <program> placeholder");
        assert!(!out.contains("{{"), "leftover askama expression");
        // On the crate path the fixture may use the program's own items, so say nothing about IDLs.
        assert!(!out.contains("GENERATED"), "crate path mentions IDL generation:\n{out}");

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
    fn api_facts_renders_from_the_analyzed_model() {
        let model = serde_json::json!({
            "components": [{
                "name": "vault",
                "program_identifier": "vault_program",
                "program_id": "Vau1t111",
                "account_types": ["VaultState"],
                "instructions": [
                    {"name": "deposit", "args": ["amount"],
                     "accounts": [{"name": "vault"}, {"name": "depositor"}]},
                ],
            }],
        });
        let out = api_facts(&model, "vault", "vault");
        for needle in [
            "crate id (for `use <id>::*`): vault",
            "pub mod vault_program",                  // module-name note (differs from crate id)
            "declare_id / program id: Vau1t111",
            "state/account types: VaultState",
            "- deposit → instruction::Deposit, accounts::Deposit; args: [amount]; accounts: [vault, depositor]",
        ] {
            assert!(out.contains(needle), "api_facts missing {needle:?} in:\n{out}");
        }
        assert!(!out.contains("{{") && !out.contains("{%"), "template residue in api_facts");
        // The component is still found by the analysis identifier, but the crate id rendered for
        // `use <id>::*` is the dependency's lib name (here they differ, as in lend).
        let out = api_facts(&model, "vault", "example_lending");
        assert!(out.contains("crate id (for `use <id>::*`): example_lending"), "in:\n{out}");
        // Unrecognized model shape → empty (unchanged contract).
        assert_eq!(api_facts(&serde_json::json!({}), "vault", "vault"), "");
    }

    fn assert_no_residue(s: &str) {
        for t in ["{{", "{%", "{#"] {
            assert!(!s.contains(t), "template residue {t:?} in:\n{s}");
        }
    }

    #[test]
    fn prompt_templates_render_end_to_end() {
        use autoprover_sdk::Property;

        let app = CrucibleApp;
        let component = serde_json::json!({ "instructions": [{ "name": "deposit" }] });
        let prop = Property {
            title: "no overflow".into(),
            sort: "invariant".into(),
            description: "balance never overflows".into(),
            slug: "no_overflow".into(),
        };

        // setup branch + a compile failure (exercises author_setup.j2 + revise_compile.j2). The
        // fixture is authored with the properties in hand — that is the whole point of the host
        // deferring it until extraction has run — so they are part of this input too.
        let setup = AuthorInput {
            kind: "setup".into(),
            program: "vault".into(),
            program_crate: ProgramCrate::default(),
            component: component.clone(),
            props: vec![prop.clone()],
            context: serde_json::Value::Null,
        };
        let compile_fail = Failure {
            draft: "prior fixture src".into(),
            errors: "error[E0433]: failed to resolve".into(),
            kind: FailureKind::Compile,
        };
        // Prose templates are wrapped to 120, so a phrase can span a newline — compare with
        // whitespace collapsed so the checks are wrap-insensitive.
        let norm = |s: &str| s.split_whitespace().collect::<Vec<_>>().join(" ");
        let has = |hay: &str, needle: &str| assert!(
            norm(hay).contains(&norm(needle)), "missing {needle:?} in:\n{hay}"
        );

        let p = app.author_prompt(&setup, Some(&compile_fail));
        assert_no_residue(&p.instruction);
        has(&p.instruction, "FIXTURE (only) for the Solana program `vault`");
        has(&p.instruction, "The previous attempt FAILED");
        has(&p.instruction, "error[E0433]");
        // The properties the fixture must make checkable, and the design rules that follow from
        // them: an action per instruction they touch (including negative attempts), enough actors,
        // and configuration the fuzzer can actually cross.
        has(&p.instruction, "Design it for these 1 properties");
        has(&p.instruction, "- [invariant] no overflow: balance never overflows");
        has(&p.instruction, "One `action_*` per instruction those properties exercise");
        has(&p.instruction, "Negative attempts are actions too");
        has(&p.instruction, "Never set a limit, cap or threshold to `u64::MAX`");

        // component branch + a judge failure (exercises author_component.j2 + revise_judge.j2).
        let comp = AuthorInput {
            kind: "component".into(),
            program: "vault".into(),
            program_crate: ProgramCrate::default(),
            component: component.clone(),
            props: vec![prop],
            context: serde_json::json!({ "fixture": "struct Fixture { ctx: TestContext }" }),
        };
        let judge_fail = Failure {
            draft: "prior tests".into(),
            errors: "Criterion 1: vacuous assertion".into(),
            kind: FailureKind::Judge,
        };
        let p = app.author_prompt(&comp, Some(&judge_fail));
        assert_no_residue(&p.instruction);
        has(&p.instruction, "named EXACTLY `c_invariants`");
        has(&p.instruction, "`\"[no overflow] ...\"`");
        has(&p.instruction, "reviewer REJECTED");
        // What the component turn may add to the fixture, what it may not, and the honest way out
        // when the fixture can't reach a property (the alternative being a vacuous assertion).
        has(&p.instruction, "put extra `impl Fixture { ... }` blocks (plain, NOT `#[fuzz_fixture]`)");
        has(&p.instruction, "Do not add `action_*` methods or a second `#[fuzz_fixture]` block");
        has(&p.instruction, "Do not send instructions from the test");
        has(&p.instruction, "do not fake it");
        has(&p.instruction, "// UNCOVERABLE:");

        // judge prompt (exercises judge_instruction.j2 + the judge_guidance.j2 include + system).
        let jp = app.judge_prompt(&comp, "fn c_invariants(f: &mut Fixture) {}").expect("component judge");
        assert_no_residue(&jp.instruction);
        has(&jp.instruction, "Evaluate the Crucible fuzz-test suite");
        has(&jp.instruction, "Criterion 1");
        has(jp.system.as_deref().unwrap(), "senior Solana security engineer");
        // setup has no judge turn.
        assert!(app.judge_prompt(&setup, "x").is_none());
    }

    // --- per-component harness fns (docs/crucible-component-units.md §8.1) -------------------

    fn component_input(slug: &str, name: &str, props: Vec<autoprover_sdk::Property>) -> AuthorInput {
        AuthorInput {
            kind: "component".into(),
            program: "lending".into(),
            program_crate: ProgramCrate::default(),
            component: serde_json::json!({
                "name": name, "slug": slug,
                "instructions": [{ "name": "deposit" }],
            }),
            props,
            context: serde_json::json!({ "fixture": "struct Fixture { ctx: TestContext }" }),
        }
    }

    fn prop(title: &str, slug: &str) -> autoprover_sdk::Property {
        autoprover_sdk::Property {
            title: title.into(), sort: "invariant".into(),
            description: "d".into(), slug: slug.into(),
        }
    }

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
        assert_eq!(app.units(&unit)[0].target_or_unit(), "c_withdraw_queue");

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
        input.component = serde_json::json!({ "instructions": [] });
        assert_eq!(harness_fn(&input), DEFAULT_HARNESS_FN);
        assert_eq!(unit_name(&input), "lending");
    }

    #[test]
    fn every_property_of_a_component_shares_that_components_fuzz_target() {
        // Report rows stay per-property (attribution); the fuzz target is the component's one fn.
        let app = CrucibleApp;
        let input = component_input(
            "farms", "Farms Integration",
            vec![prop("stake matches position", "stake_matches"), prop("no double stake", "no_dbl")],
        );
        let units = app.units(&input);
        assert_eq!(
            units.iter().map(|u| u.unit.as_str()).collect::<Vec<_>>(),
            vec!["c_stake_matches", "c_no_dbl"],
        );
        for u in &units {
            assert_eq!(u.target_or_unit(), "c_farms");
        }
        // …and a different component gets a different target, so the host runs one campaign each.
        let other = component_input("referrals", "Referrals", vec![prop("fees capped", "fees")]);
        assert_eq!(app.units(&other)[0].target_or_unit(), "c_referrals");
    }

    #[test]
    fn setup_has_no_report_units() {
        let app = CrucibleApp;
        let mut input = component_input("farms", "Farms", vec![prop("p", "p")]);
        input.kind = "setup".into();
        assert!(app.units(&input).is_empty());
    }

    /// A resolver failure reaches us through the `crucible` CLI, which normalizes *any* failed
    /// `cargo build` to `bail!("Build failed")` — so it classifies as a build failure (re-author the
    /// shared spec) and not as an `ERROR` verdict per unit, even though cargo's own text carries
    /// neither an `error[` code nor a "could not compile" line. Verbatim output from forcing the
    /// crate-dep path on a real Anchor 0.29 / Solana 1.17 program against Crucible's stack.
    #[test]
    fn a_resolver_failure_through_the_cli_is_a_build_error_not_a_verdict() {
        let out = "\
error: failed to select a version for `solana-program`.
    ... required by package `kamino_lending v1.23.0 (/tmp/klend/programs/klend)`
    ... which satisfies path dependency `kamino_lending` of package `kamino_lending_fuzz v0.1.0`
Error: Build failed
";
        assert!(is_build_error(out));
        // The compile-failure family, and the CLI's bare wrapper on its own.
        assert!(is_build_error("error[E0432]: unresolved import `vault::instruction`"));
        assert!(is_build_error("error: could not compile `vault_fuzz` (bin \"invariant_test\")"));
        assert!(is_build_error("Build failed"));
    }

    #[test]
    fn a_fuzz_run_that_built_is_never_a_build_error() {
        // The distinction `validate` leans on to choose between baking a verdict and re-authoring.
        assert!(!is_build_error(
            "warning: unused variable: `x`\n    Finished `release` profile [optimized] target(s)\n\
             [FUZZ_PULSE] execs:12000 cov:341\n[FUZZ_FINDING] crash:0001 reproduces:true \
             summary:[balance never overflows] expected 100, got 0\n"
        ));
        assert!(!is_build_error(""));
    }

    #[test]
    fn the_author_and_judge_prompts_name_the_components_own_fn() {
        let app = CrucibleApp;
        let input = component_input("withdraw_queue", "Withdraw Queue", vec![prop("fifo", "fifo")]);
        let norm = |s: &str| s.split_whitespace().collect::<Vec<_>>().join(" ");

        let p = app.author_prompt(&input, None);
        assert_no_residue(&p.instruction);
        assert!(norm(&p.instruction).contains("named EXACTLY `c_withdraw_queue`"));
        assert!(norm(&p.instruction).contains("**Withdraw Queue** component"));
        // The interleaving warning that replaces the whole-program framing.
        assert!(norm(&p.instruction).contains("drives the WHOLE program, not just this component"));
        // The embedded cheat sheet must name the SAME fn as the instruction. It used to hardcode
        // `c_invariants`, which — once the fn became per-component — told the author to write an
        // fn no build selects, in the same prompt that told it to write `c_withdraw_queue`.
        assert!(
            norm(&p.instruction).contains("fn c_withdraw_queue(fixture: &mut Fixture)"),
            "cheat sheet does not name the component's fn:\n{}", p.instruction,
        );
        assert!(!p.instruction.contains("c_invariants"), "stale fn name in:\n{}", p.instruction);

        let jp = app.judge_prompt(&input, "fn c_withdraw_queue() {}").expect("judge");
        assert_no_residue(&jp.instruction);
        assert!(norm(&jp.instruction).contains("c_withdraw_queue"));
    }

    #[test]
    fn the_author_is_told_to_interpolate_the_operands_into_every_assertion_message() {
        // A custom message REPLACES `fuzz_assert*`'s built-in operand dump, so a message without
        // the values yields an untriageable counterexample. On the klend run this is why
        // "total supply exceeds deposit_limit" needed an hour and a rebuild to explain.
        let app = CrucibleApp;
        let input = component_input("withdraw_queue", "Withdraw Queue", vec![prop("fifo", "fifo")]);
        let s = app.author_prompt(&input, None).instruction;
        let norm = s.split_whitespace().collect::<Vec<_>>().join(" ");

        assert!(norm.contains("INTERPOLATE THE OPERAND VALUES"), "{norm}");
        // The *reason* has to be in the prompt, not just the rule — the macro behaviour is the
        // non-obvious part, and a rule without it reads as style advice.
        assert!(norm.contains("operand dump is DISCARDED"), "{norm}");
        // And the worked contrast, so "include the values" is unambiguous.
        assert!(norm.contains("total_supply={} exceeds deposit_limit={}"), "{norm}");
    }

    #[test]
    fn the_judge_rejects_untriageable_messages_and_precondition_scope_errors() {
        // The two harness defects that produced BOTH of klend's false counterexamples. Catching
        // them at the judge is cheaper than reporting them as findings a human must triage.
        let app = CrucibleApp;
        let input = component_input("withdraw_queue", "Withdraw Queue", vec![prop("fifo", "fifo")]);
        let jp = app.judge_prompt(&input, "fn c_withdraw_queue() {}").expect("judge");
        let norm = jp.instruction.split_whitespace().collect::<Vec<_>>().join(" ");

        assert!(norm.contains("Diagnosable failure messages"), "{norm}");
        assert!(norm.contains("Precondition scope"), "{norm}");
        // The zeroed-account trap: an Option-returning read is not a guard, because a zeroed
        // account deserializes into a default struct rather than failing.
        assert!(norm.contains("DEFAULT struct"), "{norm}");
        assert!(norm.contains("iteration 0"), "{norm}");
    }

    #[test]
    fn finalize_emits_one_section_and_one_feature_per_component() {
        // The deliverable is ONE crate holding every component's fn, declaring exactly the
        // features the gated builds selected — read off the hosts' mirrored `targets`, not
        // re-derived from a display name.
        let app = CrucibleApp;
        let outcomes = serde_json::json!({
            "program": "lending",
            "setup": "struct Fixture {}",
            "program_crate": { "dir": "programs/lending", "lib": "lending_program", "package": "lending_program" },
            "idl": "",
            "components": [
                { "name": "Withdraw Queue", "delivered": true, "targets": ["c_withdraw_queue"],
                  "artifact_text": "fn c_withdraw_queue() { /* q */ }" },
                { "name": "Farms", "delivered": true, "targets": ["c_farms"],
                  "artifact_text": "fn c_farms() { /* f */ }" },
                // A component that gave up contributes nothing — not an empty fn, not a feature.
                { "name": "Referrals", "delivered": false },
            ],
        });
        // The features the crate declares ARE the section keys — one per delivered component,
        // read off the host's targets. (Asserted here rather than via the rendered Cargo.toml,
        // which only materializes when a crucible checkout is on `$CRUCIBLE_REPO`.)
        let sections = delivered_sections(&outcomes);
        assert_eq!(
            sections.keys().cloned().collect::<Vec<_>>(),
            vec!["c_farms".to_string(), "c_withdraw_queue".to_string()],
            "one feature per delivered component, and no fallback c_invariants",
        );

        let files = app.finalize(&outcomes);
        let main_rs = files.get("fuzz/lending/src/main.rs").expect("main.rs");
        assert!(main_rs.contains("fn c_withdraw_queue()"), "{main_rs}");
        assert!(main_rs.contains("fn c_farms()"), "{main_rs}");
        assert!(main_rs.contains("struct Fixture {}"), "the shared fixture is included");
        assert!(!main_rs.contains("Referrals"));
        if let Some(cargo) = files.get("fuzz/lending/Cargo.toml") {
            assert!(cargo.contains("c_withdraw_queue = []"), "{cargo}");
            assert!(cargo.contains("c_farms = []"), "{cargo}");
            assert!(!cargo.contains("c_invariants = []"), "{cargo}");
        }
    }

    #[test]
    fn finalize_writes_a_shared_section_once_even_if_two_targets_map_to_it() {
        // Guards the failure that produced N copies of one fn: two targets, one authored source.
        let app = CrucibleApp;
        let outcomes = serde_json::json!({
            "program": "lending",
            "setup": "struct Fixture {}",
            "program_crate": { "dir": "programs/lending", "lib": "lending_program", "package": "lending_program" },
            "idl": "",
            "components": [
                { "name": "A", "delivered": true, "targets": ["c_a", "c_b"],
                  "artifact_text": "fn c_a() {}" },
            ],
        });
        let main_rs = app.finalize(&outcomes);
        let main_rs = main_rs.get("fuzz/lending/src/main.rs").expect("main.rs");
        assert_eq!(main_rs.matches("fn c_a()").count(), 1, "{main_rs}");
    }
}

#[cfg(test)]
mod crash_triage {
    //! A `BAD` verdict must carry enough to triage it without rebuilding the harness: the
    //! reproducing sequence, and a flag when the metadata alone shows the counterexample is a
    //! harness defect rather than a program bug.
    //!
    //! The two `meta.json` bodies below are the REAL ones from the klend run of 2026-08-03, where
    //! both counterexamples turned out to be harness bugs and manual triage cost an hour. They are
    //! the regression cases.
    use super::*;

    /// `crash_ad707f0d6fef8cca` — `deposit_limit_not_exceeded`. Fired at iteration 0 on an action
    /// that failed: the fixture seeded 1000 liquidity into reserves left at `deposit_limit = 0`,
    /// so the invariant was false before the fuzzer did anything.
    const KLEND_INITIAL_STATE: &str = r#"{
        "test_name": "c_liquidity_supply_ctoken_exchange",
        "iteration": 0,
        "seed": 1785812846177550995,
        "actions": [
            {"name": "mark_deleveraging_unauthorized", "params": {}, "success": false,
             "error_code": 3007}
        ]
    }"#;

    /// `crash_492173e388336c5d` — `reserve_lending_market_immutable`, after `tmin`. The violating
    /// `clone_reserve_config` FAILED, so reserve C was never initialized and its zeroed
    /// `lending_market` (all-zero pubkey) was compared against the real market key.
    const KLEND_ACTION_FAILED: &str = r#"{
        "test_name": "c_reserve_lifecycle_configuration",
        "iteration": 4211,
        "actions": [
            {"name": "update_lending_market_owner", "params": {}, "success": true},
            {"name": "clone_reserve_config", "params": {}, "success": false, "error_code": 3002}
        ]
    }"#;

    fn repro(json: &str) -> CrashRepro {
        serde_json::from_str(json).expect("parse crash meta")
    }

    /// A workdir with `output/<crash>.meta.json` in it. Named per-test so cases can't collide.
    fn workdir_with_meta(tag: &str, crash_id: &str, meta: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("crucible_triage_{tag}"));
        let out = dir.join("output");
        std::fs::create_dir_all(&out).expect("mkdir");
        std::fs::write(out.join(format!("{crash_id}.meta.json")), meta).expect("write meta");
        dir
    }

    fn finding_line(crash_id: &str, summary: &str) -> String {
        format!("[FUZZ_FINDING] crash:{crash_id} reproduces:true summary:{summary}")
    }

    #[test]
    fn iteration_zero_is_the_fixtures_own_state_not_a_program_bug() {
        let s = repro(KLEND_INITIAL_STATE).suspicion();
        assert!(matches!(s, Some(HarnessSuspicion::InitialState)), "expected InitialState");
    }

    #[test]
    fn a_violation_on_a_failed_action_is_outside_the_propertys_precondition() {
        match repro(KLEND_ACTION_FAILED).suspicion() {
            Some(HarnessSuspicion::ViolatingActionFailed(a)) => {
                // The action string carries the evidence: which action, and its error code.
                assert_eq!(a, "clone_reserve_config -> FAIL(3002)");
            }
            other => panic!("expected ViolatingActionFailed, got {}", other.is_some()),
        }
    }

    #[test]
    fn a_violation_after_a_successful_action_is_left_alone() {
        // The case that must NOT be flagged — a real counterexample deserves a human, and crying
        // "suspect harness" over every finding would make the flag worthless.
        let genuine = r#"{"iteration": 91, "actions": [
            {"name": "deposit_reserve_liquidity", "params": {}, "success": true}
        ]}"#;
        assert!(repro(genuine).suspicion().is_none());
    }

    #[test]
    fn an_empty_sequence_is_not_flagged_as_a_failed_action() {
        // `actions: []` must not panic or invent a smell; iteration 0 still flags on its own.
        let empty = r#"{"iteration": 5, "actions": []}"#;
        assert!(repro(empty).suspicion().is_none());
        let empty_at_zero = r#"{"iteration": 0, "actions": []}"#;
        assert!(matches!(
            repro(empty_at_zero).suspicion(),
            Some(HarnessSuspicion::InitialState)
        ));
    }

    #[test]
    fn an_error_code_on_a_successful_action_is_not_rendered() {
        // Crucible emitted `success: true` alongside `error_code: 3007` on the klend run. Showing
        // it would read as a formatter bug; the code only carries meaning on a failure.
        let contradictory = r#"{"iteration": 3, "actions": [
            {"name": "flash_borrow_and_repay", "params": {}, "success": true, "error_code": 3007}
        ]}"#;
        assert!(repro(contradictory).render_sequence().ends_with("1. flash_borrow_and_repay -> OK"));
    }

    #[test]
    fn the_sequence_renders_one_numbered_action_per_line() {
        let r = repro(KLEND_ACTION_FAILED).render_sequence();
        assert_eq!(
            r,
            "reproducing sequence (iteration 4211, 2 action(s)):\n  \
             1. update_lending_market_owner -> OK\n  2. clone_reserve_config -> FAIL(3002)"
        );
    }

    #[test]
    fn the_detail_leads_with_the_summary_and_the_suspicion_then_the_sequence() {
        let wd = workdir_with_meta("lead", "crash_ad70", KLEND_INITIAL_STATE);
        let out = finding_line("crash_ad70", "[deposit_limit_not_exceeded] total supply exceeds");
        let detail = finding_detail(&out, &wd, "kamino_lending", "c_liquidity_supply_ctoken_exchange")
            .expect("detail");

        let (first, rest) = detail.split_once('\n').expect("multi-line detail");
        // The console shows only this line, so the deciding signal has to be on it.
        assert!(first.starts_with("crash crash_ad70: [deposit_limit_not_exceeded]"), "{first}");
        assert!(first.contains("SUSPECT HARNESS BUG"), "{first}");
        assert!(first.contains("iteration 0"), "{first}");
        // …and the sequence follows, for the report.
        assert!(rest.starts_with("reproducing sequence (iteration 0, 1 action(s)):"), "{rest}");
        assert!(rest.contains("mark_deleveraging_unauthorized -> FAIL(3007)"), "{rest}");
    }

    #[test]
    fn a_genuine_finding_gets_its_sequence_but_no_suspicion_marker() {
        let meta = r#"{"iteration": 91, "actions": [
            {"name": "deposit_reserve_liquidity", "params": {}, "success": true}
        ]}"#;
        let wd = workdir_with_meta("genuine", "crash_beef", meta);
        let out = finding_line("crash_beef", "[conservation] vault drifted");
        let detail = finding_detail(&out, &wd, "p", "c_u").expect("detail");

        assert!(!detail.contains("SUSPECT HARNESS BUG"), "{detail}");
        assert!(detail.contains("reproducing sequence (iteration 91, 1 action(s)):"), "{detail}");
    }

    #[test]
    fn a_finding_still_reports_when_the_metadata_is_missing() {
        // Best-effort enrichment: no crash file (or an unreadable one) must not lose the finding,
        // and must not change the one-line shape the report relied on before this existed.
        let out = finding_line("0001", "[balance never overflows] expected 100, got 0");
        let missing = std::env::temp_dir().join("crucible_triage_does_not_exist");
        let detail = finding_detail(&out, &missing, "p", "c_u").expect("detail");
        assert_eq!(detail, "crash 0001: [balance never overflows] expected 100, got 0");
    }

    #[test]
    fn unparsable_metadata_degrades_to_the_bare_finding() {
        let wd = workdir_with_meta("garbage", "crash_bad", "{ not json");
        let out = finding_line("crash_bad", "[p] boom");
        assert_eq!(
            finding_detail(&out, &wd, "p", "c_u").expect("detail"),
            "crash crash_bad: [p] boom"
        );
    }

    #[test]
    fn the_per_test_crashes_layout_is_also_searched() {
        // `crucible tmin`/`show` keep crashes under fuzz/<program>/crashes/<test>/ rather than the
        // flat output dir, so a minimized crash is still found.
        let dir = std::env::temp_dir().join("crucible_triage_layout");
        let nested = dir.join("fuzz").join("kamino_lending").join("crashes").join("c_unit");
        std::fs::create_dir_all(&nested).expect("mkdir");
        std::fs::write(nested.join("crash_x.meta.json"), KLEND_INITIAL_STATE).expect("write");

        let detail =
            finding_detail(&finding_line("crash_x", "[p] boom"), &dir, "kamino_lending", "c_unit")
                .expect("detail");
        assert!(detail.contains("SUSPECT HARNESS BUG"), "{detail}");
    }

    #[test]
    fn a_marker_line_without_a_summary_is_passed_through_unchanged() {
        let raw = "[FUZZ_FINDING] crash:0001 reproduces:true";
        let missing = std::env::temp_dir().join("crucible_triage_nosummary");
        assert_eq!(finding_detail(raw, &missing, "p", "c_u").as_deref(), Some(raw));
    }
}

