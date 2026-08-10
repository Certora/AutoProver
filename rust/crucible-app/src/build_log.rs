//! Reading a `crucible run` log: did the harness *build*, and what does the author need to see.
//!
//! The build/ran distinction is what the gating callouts choose their whole shape from — a build
//! failure re-authors the spec, while a run that built produces verdicts — so it is decided here,
//! once, rather than by each callout's own reading of the output.

use autoprover_sdk::sandbox::CommandOutput;

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

/// Did the build fail (as opposed to the harness building and fuzzing)?
///
/// `"Build failed"` is the load-bearing marker and covers more than it looks like: the `crucible`
/// CLI runs the harness build itself and `bail!("Build failed")`s on *any* non-zero `cargo build` —
/// so cargo's pre-compile failures (an unresolvable dependency graph, an unloadable manifest, an
/// unparseable lockfile) arrive here already normalized to that one string, even though none of them
/// prints an `error[` code or a `could not compile` line. The other two markers catch the same
/// failures if they ever reach us without the CLI's wrapper.
pub(crate) fn is_build_error(out: &str) -> bool {
    out.contains("could not compile") || out.contains("error[") || out.contains("Build failed")
}

/// The compiler errors to hand back to the model — extracted diagnostics, else a raw tail.
pub(crate) fn build_errors(out: &CommandOutput) -> String {
    let combined = format!("{}\n{}", out.stdout, out.stderr);
    let d = compiler_diagnostics(&combined);
    if d.is_empty() {
        combined[combined.len().saturating_sub(2000)..].to_string()
    } else {
        d
    }
}

#[cfg(test)]
mod tests {
    //! The distinction `validate` leans on to choose between baking a verdict and re-authoring.
    use super::*;

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
        assert!(!is_build_error(
            "warning: unused variable: `x`\n    Finished `release` profile [optimized] target(s)\n\
             [FUZZ_PULSE] execs:12000 cov:341\n[FUZZ_FINDING] crash:0001 reproduces:true \
             summary:[balance never overflows] expected 100, got 0\n"
        ));
        assert!(!is_build_error(""));
    }
}
