//! Keeping each campaign's coverage file.
//!
//! `--coverage` makes Crucible write an LCOV file *as the campaign runs*, so coverage is a
//! by-product of validation rather than a second pass over the corpus. It costs about 2% of
//! throughput (measured on klend: 380.5 vs 389.8 exec/s over equal 180s budgets), because edge and
//! branch tracking is already on to steer the fuzzer — what `--coverage` adds is the per-PC hit map
//! LCOV needs.
//!
//! Crucible ignores `--lcov-out` for a live campaign (only its coverage-only mode reads that path)
//! and writes `coverage.lcov` into the cwd it spawns the harness binary in, which is the harness
//! dir. Every component shares that one crate, so each campaign overwrites the last and a finished
//! run would hold only the final component's coverage. This moves each file out under its
//! component's name the moment its campaign ends.

use std::path::{Path, PathBuf};

/// What Crucible names it, in the harness dir.
const WRITTEN_AS: &str = "coverage.lcov";

/// Move this campaign's coverage file to a per-component path under `report_dir`, returning where
/// it landed — or `None` when the campaign produced none.
///
/// The source is removed whether or not the copy succeeded. A campaign can end *without* Crucible
/// writing at all — `--stop-on-crash` exits the process with no final write, so only what its
/// periodic writer last flushed exists — and a file left in place would be picked up and published
/// under the *next* component's name.
pub(crate) fn preserve(
    workdir: &Path,
    harness_dir: &str,
    report_dir: &str,
    feature: &str,
) -> Option<PathBuf> {
    let written = workdir.join(harness_dir).join(WRITTEN_AS);
    if !written.is_file() {
        return None;
    }
    let dest = workdir.join(report_dir).join("coverage").join(format!("{feature}.lcov"));
    let kept = dest
        .parent()
        .map(|d| std::fs::create_dir_all(d).is_ok())
        .unwrap_or(false)
        && std::fs::copy(&written, &dest).is_ok();
    let _ = std::fs::remove_file(&written);
    kept.then_some(dest)
}

#[cfg(test)]
mod tests {
    use super::*;

    const HARNESS: &str = "fuzz/lending";
    const REPORTS: &str = "certora/crucible/reports";

    /// A workdir with a harness dir, optionally holding the file a campaign would have left.
    fn workdir(tag: &str, lcov: Option<&str>) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("crucible_coverage_{tag}"));
        let _ = std::fs::remove_dir_all(&dir);
        let harness = dir.join(HARNESS);
        std::fs::create_dir_all(&harness).expect("mkdir");
        if let Some(text) = lcov {
            std::fs::write(harness.join(WRITTEN_AS), text).expect("write lcov");
        }
        dir
    }

    #[test]
    fn the_campaigns_coverage_is_kept_under_its_components_name() {
        let dir = workdir("kept", Some("SF:program_abc.bpf\nLF:100\nLH:12\n"));

        let at = preserve(&dir, HARNESS, REPORTS, "c_oracle").expect("preserved");

        assert_eq!(at, dir.join(REPORTS).join("coverage/c_oracle.lcov"));
        assert!(std::fs::read_to_string(&at).expect("read").contains("LH:12"));
    }

    #[test]
    fn the_file_does_not_survive_to_be_claimed_by_the_next_component() {
        // Every component fuzzes the same crate, so Crucible writes to the same path each time.
        // Leaving it would publish one campaign's coverage under another campaign's name.
        let dir = workdir("moved", Some("SF:x\n"));

        preserve(&dir, HARNESS, REPORTS, "c_first").expect("preserved");

        assert!(!dir.join(HARNESS).join(WRITTEN_AS).exists(), "the source is gone");
        assert_eq!(
            preserve(&dir, HARNESS, REPORTS, "c_second"),
            None,
            "a component whose campaign wrote nothing reports nothing",
        );
    }

    #[test]
    fn a_campaign_that_wrote_nothing_is_not_an_error() {
        // `--stop-on-crash` exits without a final write; that is a quiet absence, not a failure —
        // the verdicts it produced are still the run's answer.
        let dir = workdir("absent", None);

        assert_eq!(preserve(&dir, HARNESS, REPORTS, "c_oracle"), None);
    }
}
