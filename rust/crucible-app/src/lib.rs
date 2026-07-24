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
    PhaseSpec, Prompt, Sandbox, SandboxGrants, SetupSpec, Unit, ValidateOutcome, Verdict,
    WorkspacePrep,
};

use askama::Template;

// The crucible/solana/anchor stack a harness pins (docs/crucible-application.md §6.1). Hardcoded
// for now to the combination the installed toolchain matches (was Python's `CrucibleHarness`).
const ANCHOR_VERSION: &str = "1.0.1";
const SOLANA_VERSION: &str = "3.0";
const LIBAFL_VERSION: &str = "0.15.1";

/// The single `#[invariant_test]` fn that holds ALL of a program's invariants — one harness,
/// one build, one fuzz run (the Crucible macro self-gates `main()` by fn name == feature, so this
/// is also the feature/target selector). See docs/crucible-unit-granularity.md §3.
const SINGLE_HARNESS_FN: &str = "c_invariants";

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
#[derive(Template)]
#[template(path = "harness_cheat_sheet.j2", escape = "none")]
struct HarnessCheatSheet<'a> {
    program: &'a str,
}

/// A complete, compiling worked example (a different `escrow` program) to pattern-match.
#[derive(Template)]
#[template(path = "example_fixture.j2", escape = "none")]
struct ExampleFixture;

/// Cheat-sheet for authoring the single `#[invariant_test]` fn holding all invariants.
#[derive(Template)]
#[template(path = "test_cheat_sheet.j2", escape = "none")]
struct TestCheatSheet;

/// Reviewer persona for the `judge_prompt` turn (peer of Foundry's judge system prompt).
#[derive(Template)]
#[template(path = "judge_system.j2", escape = "none")]
struct JudgeSystem;

/// A `#[invariant_test]` probe appended by the host to validate the fixture via `--dry-run`.
#[derive(Template)]
#[template(path = "probe_fn.j2", escape = "none")]
struct ProbeFn;

/// The pinned `[dependencies]` block for the harness crate.
#[derive(Template)]
#[template(path = "cargo_deps.j2", escape = "none")]
struct CargoDeps<'a> {
    cf: &'a str,
    ctc: &'a str,
    program: &'a str,
    anchor_version: &'a str,
    libafl_version: &'a str,
    solana_version: &'a str,
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

/// The fixture-authoring prompt (the `setup` phase).
#[derive(Template)]
#[template(path = "author_setup.j2", escape = "none")]
struct AuthorSetup<'a> {
    program: &'a str,
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

/// The `[dependencies]` block for the harness crate — the pinned crucible/solana/anchor stack
/// plus the program-under-test as a path dep (was `CrucibleDep::render_deps`).
fn crucible_deps(program: &str, repo: &Path) -> String {
    let crates = repo.join("crates");
    let cf = crates.join("crucible-fuzzer").display().to_string();
    let ctc = crates.join("crucible-test-context").display().to_string();
    CargoDeps {
        cf: &cf,
        ctc: &ctc,
        program,
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
fn render_cargo_toml(program: &str, repo: &Path, features: &[String]) -> String {
    let feats = if features.is_empty() {
        "# (no components yet)".to_string()
    } else {
        features.iter().map(|f| format!("{f} = []")).collect::<Vec<_>>().join("\n")
    };
    let deps = crucible_deps(program, repo);
    CargoToml { program, deps: &deps, feats: &feats }
        .render()
        .expect("render cargo_toml")
}

/// The crate's on-disk files for a confined build: `src/main.rs` plus a `Cargo.toml` declaring
/// exactly `features` (materialized per run — with `serialize_toolchain` there is no concurrent
/// writer, so no shared-manifest race and no cumulative feature reservation).
fn crate_files(program: &str, main_rs: String, features: &[String]) -> BTreeMap<String, String> {
    let mut files = BTreeMap::new();
    files.insert(format!("fuzz/{program}/src/main.rs"), main_rs);
    if let Some(repo) = crucible_repo() {
        files.insert(
            format!("fuzz/{program}/Cargo.toml"),
            render_cargo_toml(program, &repo, features),
        );
    }
    files
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
fn api_facts(analyzed: &serde_json::Value, program: &str) -> String {
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
    // The crate id is the harness's actual Cargo dependency name — the program name
    // (== CrucibleDep's crate), NOT the analysis's `program_identifier`, which may be the
    // `#[program] pub mod` name and would mis-resolve `use <id>::*`. Surface the module name as
    // a note only when it differs (the template renders it iff `analysis_id` is `Some`).
    let analysis_raw = str_of(prog.get("program_identifier"));
    let analysis_id: Option<String> =
        (analysis_raw != program && analysis_raw != "?").then_some(analysis_raw);
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
        crate_id: program,
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
fn is_build_error(out: &str) -> bool {
    out.contains("could not compile") || out.contains("error[") || out.contains("Build failed")
}

/// Pull the human-readable finding out of a `crucible run` log so a `BAD` verdict explains
/// itself. Crucible prints `[FUZZ_FINDING] crash:<id> reproduces:<bool> summary:<msg>`, where
/// `<msg>` is the failed `fuzz_assert_*` message (with the actual vs expected values). Returns
/// `crash <id>: <msg>`, or the raw marker line if the summary can't be parsed, or None.
fn finding_detail(out: &str) -> Option<String> {
    let line = out.lines().find(|l| l.contains("[FUZZ_FINDING]"))?.trim();
    match line.split_once("summary:") {
        Some((head, summary)) if !summary.trim().is_empty() => {
            let crash =
                head.split_whitespace().find_map(|t| t.strip_prefix("crash:")).unwrap_or("?");
            Some(format!("crash {crash}: {}", summary.trim()))
        }
        _ => Some(line.to_string()),
    }
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
                    let mut v = Verdict::with_outcome("BAD");
                    v.detail = detail.clone();
                    (u.unit.clone(), v)
                } else {
                    (u.unit.clone(), Verdict::with_outcome("GOOD"))
                }
            })
            .collect(),
    }
}

// ===========================================================================
// Backend glue: small pure helpers shared by the callouts.
// ===========================================================================

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
                PhaseSpec { key: "analysis".into(), label: "System Analysis".into(), order: 1, core_slot: Some(CoreSlot::Analysis) },
                PhaseSpec { key: "extraction".into(), label: "Property Extraction".into(), order: 2, core_slot: Some(CoreSlot::Extraction) },
                // UI-only phase: build the program `.so` + IDL + shared fixture (§5.1).
                PhaseSpec { key: "build_harness".into(), label: "Build Harness".into(), order: 3, core_slot: None },
                PhaseSpec { key: "formalization".into(), label: "Harness Authoring".into(), order: 4, core_slot: Some(CoreSlot::Formalization) },
                PhaseSpec { key: "report".into(), label: "Report".into(), order: 5, core_slot: Some(CoreSlot::Report) },
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
            // A shared fixture authored once (the setup step), one crate assembled by finalize
            // (callout), all toolchain runs serialized on the one crate/target, confined by
            // default (untrusted native builds), and "instruction" as the unit noun.
            setup: Some(SetupSpec {
                phase_key: "build_harness".into(),
                label: "Build Harness".into(),
                context_key: "fixture".into(),
            }),
            deliverable_mode: DeliverableMode::Callout,
            serialize_toolchain: true,
            confine_by_default: true,
            component_noun: Some("instruction".into()),
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
                     workspace with the program under programs/<name>/."
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
        // The setup fixture has no report units. A component's invariants all live in ONE harness
        // fn (`SINGLE_HARNESS_FN`) — a single build + fuzz run covering every property
        // (docs/crucible-unit-granularity.md §3) — but each property is still its own report row,
        // mapping to that shared fuzz target. The host runs the shared target once and attributes
        // a counterexample to the offending property via the finding message.
        if input.kind == "setup" {
            return Vec::new();
        }
        input
            .props
            .iter()
            .enumerate()
            .map(|(i, p)| {
                let slug = if p.slug.is_empty() { format!("inv{i}") } else { p.slug.clone() };
                // Report row = c_<slug> (one per property); fuzz target = the shared c_invariants.
                Unit {
                    property: p.title.clone(),
                    unit: format!("c_{slug}"),
                    target: Some(SINGLE_HARNESS_FN.to_string()),
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
            let cheat = HarnessCheatSheet { program }.render().expect("render harness_cheat_sheet");
            let example = ExampleFixture.render().expect("render example_fixture");
            let facts = api_facts(analyzed, program);
            AuthorSetup {
                program,
                cheat: &cheat,
                example: &example,
                facts: &facts,
                model: &model,
                revise: &revise,
            }
            .render()
            .expect("render author_setup")
        } else {
            // Author ONE #[invariant_test] fn holding ALL invariants (single build + run).
            let listed = input
                .props
                .iter()
                .map(|p| format!("- [{}] {}: {}", p.sort, p.title, p.description))
                .collect::<Vec<_>>()
                .join("\n");
            let component = serde_json::to_string_pretty(&input.component)
                .unwrap_or_else(|_| input.component.to_string());
            let fixture = ctx_str(input, "fixture");
            let cheat = TestCheatSheet.render().expect("render test_cheat_sheet");
            AuthorComponent {
                harness_fn: SINGLE_HARNESS_FN,
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
        let listed = input
            .props
            .iter()
            .map(|p| format!("- [{}] {}: {}", p.sort, p.title, p.description))
            .collect::<Vec<_>>()
            .join("\n");
        let component = serde_json::to_string_pretty(&input.component)
            .unwrap_or_else(|_| input.component.to_string());
        let fixture = ctx_str(input, "fixture");
        let instruction = JudgeInstruction {
            program,
            harness_fn: SINGLE_HARNESS_FN,
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
        // Setup: dry-run the fixture behind a probe test. Component: fixture + the authored tests,
        // dry-run behind the shared harness feature `c_invariants` (which gates `main`).
        let (main_rs, feature) = if input.kind == "setup" {
            let probe = ProbeFn.render().expect("render probe_fn");
            (format!("{spec}{probe}"), "c_probe".to_string())
        } else {
            let fixture = ctx_str(input, "fixture");
            (format!("{fixture}\n\n{spec}"), SINGLE_HARNESS_FN.to_string())
        };
        let files = crate_files(program, main_rs, std::slice::from_ref(&feature));
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
        let files = crate_files(program, format!("{fixture}\n\n{spec}"), std::slice::from_ref(&unit.to_string()));
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
        // The report units this fuzz target covers (Crucible: every invariant shares `c_invariants`).
        // The backend owns attribution — it maps ONE run to a verdict per covered unit.
        let covered: Vec<Unit> =
            self.units(input).into_iter().filter(|u| u.target_or_unit() == unit).collect();
        let all = |o: &str, detail: Option<String>| -> ValidateOutcome {
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
                    attribute_finding(&covered, finding_detail(&combined))
                } else if out.exit_code == 0 {
                    all("GOOD", None) // ran to the budget with no violation = every invariant held
                } else if is_build_error(&combined) {
                    // Shared build; re-author the whole spec (docs/rust-backend-api.md).
                    ValidateOutcome::BuildFailed { errors: build_errors(&out) }
                } else {
                    // Non-zero exit with no build markers and no finding — capture the tail.
                    all("ERROR", Some(build_errors(&out)))
                }
            }
            Err(e) => all("ERROR", Some(e)),
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
        let program = &input.program;
        let mut files = BTreeMap::new();
        if let Some(repo) = crucible_repo() {
            files.insert(
                format!("fuzz/{program}/Cargo.toml"),
                render_cargo_toml(program, &repo, std::slice::from_ref(&"c_probe".to_string())),
            );
        }
        WorkspacePrep {
            files,
            warm_dirs: vec![format!("fuzz/{program}")],
            build_program: Some(program.clone()),
        }
    }

    fn finalize(&self, outcomes: &serde_json::Value) -> BTreeMap<String, String> {
        // Assemble the one deliverable crate: the shared fixture + every delivered invariant's
        // test section, keyed by its feature (was Python's `CrucibleHarness`/`CrucibleArtifactStore`).
        let program = outcomes.get("program").and_then(|v| v.as_str()).unwrap_or_default();
        let fixture = outcomes.get("setup").and_then(|v| v.as_str()).unwrap_or_default();

        // feature -> test section (BTreeMap keeps a stable, sorted crate — matches the old store).
        let mut sections: BTreeMap<String, String> = BTreeMap::new();
        if let Some(comps) = outcomes.get("components").and_then(|v| v.as_array()) {
            for c in comps {
                if !c.get("delivered").and_then(|v| v.as_bool()).unwrap_or(false) {
                    continue;
                }
                let text = c.get("artifact_text").and_then(|v| v.as_str()).unwrap_or_default();
                // property_units: [[title, [feature, ...]], ...]
                if let Some(pu) = c.get("property_units").and_then(|v| v.as_array()) {
                    for entry in pu {
                        if let Some(units) = entry.get(1).and_then(|v| v.as_array()) {
                            for u in units.iter().filter_map(|v| v.as_str()) {
                                sections.insert(u.to_string(), text.trim().to_string());
                            }
                        }
                    }
                }
            }
        }
        if program.is_empty() || sections.is_empty() {
            return BTreeMap::new();
        }

        let features: Vec<String> = sections.keys().cloned().collect();
        let body = features.iter().map(|f| sections[f].clone()).collect::<Vec<_>>().join("\n\n");
        let main_rs = format!(
            "{}\n\n{}{}",
            fixture.trim_end(),
            body,
            if body.is_empty() { "" } else { "\n" }
        );
        let mut files = BTreeMap::new();
        files.insert(format!("fuzz/{program}/src/main.rs"), main_rs);
        if let Some(repo) = crucible_repo() {
            files.insert(
                format!("fuzz/{program}/Cargo.toml"),
                render_cargo_toml(program, &repo, &features),
            );
        }
        files
    }
}

autoprover_sdk::export_app!(crucible_app, CrucibleApp);

#[cfg(test)]
mod template_parity {
    //! Guards the askama migration: the build-critical crate files must render byte-identically
    //! to the former `format!` output (else the harness crate won't compile), and the static
    //! prose templates must preserve their bytes. Prompts are checked for template residue only.
    use super::*;

    /// The OLD `crucible_deps` format! body — kept here verbatim as the parity oracle.
    fn expected_deps(program: &str, repo: &Path) -> String {
        let crates = repo.join("crates");
        format!(
            "crucible-fuzzer = {{ path = \"{cf}\" }}\n\
             crucible-test-context = {{ path = \"{ctc}\" }}\n\
             anchor-lang = \"{ANCHOR_VERSION}\"\n\
             arbitrary = {{ version = \"1\", features = [\"derive\"] }}\n\
             ctrlc = \"3.4\"\n\
             libafl = {{ version = \"{LIBAFL_VERSION}\", features = [\"std\", \"cli\", \"prelude\"] }}\n\
             libafl_bolts = {{ version = \"{LIBAFL_VERSION}\", features = [\"std\"] }}\n\
             {program} = {{ path = \"../../programs/{program}\", features = [\"no-entrypoint\"] }}\n\
             solana-keypair = \"{SOLANA_VERSION}\"\n\
             solana-pubkey = \"{SOLANA_VERSION}\"\n\
             solana-signer = \"{SOLANA_VERSION}\"",
            cf = crates.join("crucible-fuzzer").display(),
            ctc = crates.join("crucible-test-context").display(),
        )
    }

    /// The OLD `render_cargo_toml` format! body — the parity oracle.
    fn expected_cargo_toml(program: &str, repo: &Path, features: &[String]) -> String {
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
            deps = expected_deps(program, repo),
        )
    }

    #[test]
    fn crate_files_are_byte_identical_to_the_old_format() {
        let repo = Path::new("/home/user/crucible");
        assert_eq!(crucible_deps("vault", repo), expected_deps("vault", repo));
        // empty features
        assert_eq!(
            render_cargo_toml("vault", repo, &[]),
            expected_cargo_toml("vault", repo, &[]),
        );
        // one and several features
        for feats in [vec!["c_invariants".to_string()], vec!["c_probe".into(), "c_invariants".into()]] {
            assert_eq!(
                render_cargo_toml("vault", repo, &feats),
                expected_cargo_toml("vault", repo, &feats),
                "cargo_toml mismatch for features {feats:?}",
            );
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
        eq(TestCheatSheet.render().unwrap(), include_str!("../templates/test_cheat_sheet.j2"));
        eq(JudgeSystem.render().unwrap(), include_str!("../templates/judge_system.j2"));
        eq(ProbeFn.render().unwrap(), include_str!("../templates/probe_fn.j2"));
    }

    #[test]
    fn harness_cheat_sheet_substitutes_program_and_has_no_placeholder() {
        let out = HarnessCheatSheet { program: "vault" }.render().unwrap();
        assert!(out.contains("use vault::*;"), "program not substituted:\n{out}");
        assert!(!out.contains("<program>"), "leftover <program> placeholder");
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
        let out = api_facts(&model, "vault");
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
        // Unrecognized model shape → empty (unchanged contract).
        assert_eq!(api_facts(&serde_json::json!({}), "vault"), "");
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

        // setup branch + a compile failure (exercises author_setup.j2 + revise_compile.j2).
        let setup = AuthorInput {
            kind: "setup".into(),
            program: "vault".into(),
            component: component.clone(),
            props: vec![],
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

        // component branch + a judge failure (exercises author_component.j2 + revise_judge.j2).
        let comp = AuthorInput {
            kind: "component".into(),
            program: "vault".into(),
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

        // judge prompt (exercises judge_instruction.j2 + the judge_guidance.j2 include + system).
        let jp = app.judge_prompt(&comp, "fn c_invariants(f: &mut Fixture) {}").expect("component judge");
        assert_no_residue(&jp.instruction);
        has(&jp.instruction, "Evaluate the Crucible fuzz-test suite");
        has(&jp.instruction, "Criterion 1");
        has(jp.system.as_deref().unwrap(), "senior Solana security engineer");
        // setup has no judge turn.
        assert!(app.judge_prompt(&setup, "x").is_none());
    }
}
