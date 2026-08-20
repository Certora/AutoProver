//! What must already be installed for a run to be possible, checked before the run starts.
//!
//! A pure filesystem/env inspection: nothing here *runs* a tool, because
//! [`Backend::validate_preconditions`](autoprover_sdk::Backend::validate_preconditions) must stay a
//! cheap, synchronous check.

use std::path::Path;

use autoprover_sdk::args::AppArgs;
use autoprover_solana::SolanaSourceUnit;

/// The compiled binaries a Crucible run needs on `PATH`. Checked up-front so a run
/// fails fast with an actionable message rather than deep in the build phase.
const REQUIRED_BINARIES: &[&str] = &["crucible", "cargo-build-sbf", "anchor"];

/// The directory of `bin` on `$PATH` (for a read-only sandbox grant), if found.
pub(crate) fn which_dir(bin: &str) -> Option<String> {
    let path = std::env::var("PATH").ok()?;
    std::env::split_paths(&path)
        .find(|dir| dir.join(bin).is_file())
        .map(|dir| dir.display().to_string())
}

/// Is `bin` an executable file reachable via `$PATH`? A pure filesystem scan — we do
/// not *run* anything here (validate_preconditions must stay a cheap, sync check).
fn on_path(bin: &str) -> bool {
    let Ok(path) = std::env::var("PATH") else {
        return false;
    };
    std::env::split_paths(&path).any(|dir| dir.join(bin).is_file())
}

/// Everything that must hold before a run starts, reported together: the toolchain on `PATH`, a
/// buildable workspace holding the program's crate, and the crucible checkout the harness's path
/// deps resolve against.
///
/// All problems are collected rather than returned one at a time, so an operator fixes their
/// environment in one pass.
pub(crate) fn preconditions(args: &AppArgs) -> Result<(), String> {
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
    let root = &args.project_root;
    if !root.join("Cargo.toml").is_file() {
        problems.push(format!(
            "{}/Cargo.toml not found — Crucible needs a buildable Cargo/Anchor \
             workspace containing the program's crate.",
            root.display()
        ));
    }
    // The harness declares the program under test as a path dep, so its crate must exist:
    // check it here rather than let a wrong directory surface as a confusing "failed to
    // load manifest for dependency" deep in an offline build.
    let cr = SolanaSourceUnit::of(&args.source_unit, &args.program);
    if !root.join(&cr.dir).join("Cargo.toml").is_file() {
        problems.push(format!(
            "no Cargo crate for the program under test at {}/Cargo.toml — Crucible \
             declares it as a path dependency of the harness. Point the main-contract \
             path at a source file inside the program's crate.",
            cr.dir
        ));
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
