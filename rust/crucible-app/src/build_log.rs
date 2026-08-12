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
        last_bytes(&combined, 2000).to_string()
    } else {
        d
    }
}

/// The last `n` bytes of `s`, rounded outward to a character boundary.
///
/// Slicing a `String` at `len - n` panics when that index lands inside a multi-byte character, and
/// this reads *fuzzer* output — box-drawing rules, `─`, `✓` — so the index is not hypothetically
/// mid-character.
fn last_bytes(s: &str, n: usize) -> &str {
    let mut at = s.len().saturating_sub(n);
    while at < s.len() && !s.is_char_boundary(at) {
        at += 1;
    }
    &s[at..]
}

/// Is this line the campaign saying it is *still running*, rather than saying anything about how it
/// ended? The fuzzer prints one per pulse, so they are the bulk of any campaign log — and the last
/// thing in it.
fn is_progress(line: &str) -> bool {
    let t = line.trim();
    t.is_empty()
        || t.contains("[FUZZ_PULSE]")
        || (t.contains("exec/sec:") && t.contains("corpus:"))
}

/// Why a campaign that *built* exited non-zero — for a verdict that has to say something.
///
/// Deliberately not [`build_errors`]: there are no compiler diagnostics to find in a fuzz log, so
/// that function falls back to a raw tail, and the tail of a campaign is always its periodic
/// progress line. That line reports throughput and says nothing about why the run stopped — an
/// author handed it revises against a number, and every retry is uninformed in the same way.
///
/// So: lead with the exit status (often the only fact there is — a harness that dies outside the
/// fuzz target leaves no crash artifact and no message), then the last lines from each stream that
/// are *not* progress, labelled by stream because the two carry different halves of the story.
pub(crate) fn run_failure(out: &CommandOutput) -> String {
    let tail = |s: &str| -> String {
        let kept: Vec<&str> = s.lines().filter(|l| !is_progress(l)).collect();
        let from = kept.len().saturating_sub(20);
        kept[from..].join("\n")
    };
    let mut parts = vec![format!("campaign exited with status {}", out.exit_code)];
    for (name, text) in [("stdout", tail(&out.stdout)), ("stderr", tail(&out.stderr))] {
        if !text.trim().is_empty() {
            parts.push(format!("--- {name} (progress lines removed) ---\n{}", last_bytes(&text, 1500)));
        }
    }
    if parts.len() == 1 {
        parts.push(
            "The campaign printed nothing but progress. It ended without a crash, a panic or a \
             diagnostic — the harness most likely died outside the fuzz target, where the fuzzer \
             cannot record it."
                .to_string(),
        );
    }
    parts.join("\n")
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

    /// The campaign log that produced 84 uninformative `ERROR` verdicts across three klend
    /// components: it ran, it stopped, and every byte of the tail is throughput.
    fn silent_campaign() -> CommandOutput {
        let pulses = (0..200)
            .map(|i| {
                format!(
                    "[FUZZ_PULSE] run time: {i}s, clients: 1, corpus: {i}, crashes: 0, \
                     executions: {i}, exec/sec: 74.18, edges: 8447/76548 (11.0%)"
                )
            })
            .collect::<Vec<_>>()
            .join("\n");
        CommandOutput { exit_code: 101, stdout: pulses, stderr: String::new() }
    }

    #[test]
    fn a_campaign_that_says_only_how_fast_it_ran_still_reports_that_it_failed() {
        let out = silent_campaign();
        // What the author used to get: a progress line, sliced mid-word, with no exit status.
        assert!(build_errors(&out).contains("exec/sec"));

        let why = run_failure(&out);
        assert!(why.contains("status 101"), "no exit status in:\n{why}");
        assert!(!why.contains("exec/sec"), "progress survived into:\n{why}");
        assert!(why.contains("outside the fuzz target"), "no account of the silence in:\n{why}");
    }

    #[test]
    fn a_campaign_that_did_say_something_leads_with_it_rather_than_the_pulses() {
        let mut out = silent_campaign();
        out.stderr = "thread 'main' panicked at src/main.rs:41:9:\nindex out of bounds".into();
        let why = run_failure(&out);
        assert!(why.contains("panicked at src/main.rs:41:9"), "panic lost in:\n{why}");
        assert!(why.contains("stderr"), "stream not named in:\n{why}");
    }

    /// `build_errors` sliced `combined` at `len - 2000` outright, and a campaign log is full of
    /// box-drawing and `✓` — so the index landing mid-character panicked the *wheel*, turning a
    /// component's failure into the run's.
    #[test]
    fn a_tail_that_starts_inside_a_character_does_not_panic() {
        let out = CommandOutput {
            exit_code: 1,
            stdout: "─".repeat(2000),
            stderr: "✓ done".into(),
        };
        assert!(!build_errors(&out).is_empty());
        assert!(run_failure(&out).contains("status 1"));
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
