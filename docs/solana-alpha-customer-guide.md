# Solana AutoProver alpha: what to send and what comes back

The Solana AutoProver reads a Solana program and its design document, proposes properties the
program should have, writes Certora Verification Language for Rust (CVLR) rules for them, and checks
each rule with the Certora Solana Prover. This is an alpha: a Certora engineer starts each run and
watches it.

## What to send

- **The project, as it is in your repository.** A zip of the workspace root (the directory with the
  root `Cargo.toml`), or the repository and commit. Do not build it first: the run builds the program
  itself, and fetches its dependencies from crates.io and GitHub.
- **Which program to verify.** Its source file relative to the workspace root, for example
  `programs/vault/src/lib.rs`, and the program's name, usually its crate name with `-` written as
  `_`. If the crate that owns that file is not the one to build, name the cargo package too.
- **A design document**, if you have one: what the program is for, its roles, and what must always
  hold. It is optional, but the properties are much better with it. A threat model is welcome too.

### What the program must look like

- **The Solana SDK at 2.x (`solana-program` 2.x) or 3.x (the `solana-*` 3.x crates).** Programs on
  `solana-program` 1.x are refused.
- **Anchor, if used, at 0.26.0 to 0.29.0, 0.30.1, 0.31.1, 0.32.0 or 0.32.1.** The run builds against
  Certora's fork of that Anchor release, which the Prover can analyze. Any other version stops the run
  with a message naming these.
- **No CVLR dependency of a different line already in the project.** The run adds the CVLR crates
  that match your SDK generation.

Your `rust-toolchain.toml` is not used: the run builds with its own toolchain.

## What comes back

Everything is under `certora/cvlr/`:

- `report.json` and an HTML report: the properties, the rules that check each one, each rule's
  verdict with a link to its Prover job, the properties that were skipped and why, and findings for
  rules that failed.
- `harness.patch`: the rules and the wiring they need (a `certora` cargo feature and a module in your
  crate). `git apply` it to your project at the commit you sent.
- `components/<module>.patch`, for each part of the program whose rules needed changes to the program
  itself: the rules plus those changes. Apply one of these instead of `harness.patch` to rebuild that
  part, on a clean checkout, one at a time.
- `confs/<module>.conf`, one per part: the Prover configuration it was verified with. After applying
  the patch, run `certoraSolanaProver <path to the conf>` from the package directory to check those
  rules again.

## Known limitations

- **Coverage varies from property to property.** Some properties cannot be stated or checked within
  the Prover's limits. Each skipped property is listed with its reason.
- **Edits to the program are claims to review.** Where a rule needed a change to the program (making
  a function reachable, replacing a function with a simpler stand-in), the report gives the change
  and the argument that it does not change what the rule checks. Review those changes; the argument
  is the tool's, and a stand-in's description is not re-checked when the stand-in changes later.
- **A run cut short by its budget delivers a partial result.** Those parts are listed separately,
  their drafts end in `.unverified`, and their verdicts are not claims.
- **The rules run against the Prover's development release and pre-release CVLR crates.** A rule
  that verifies today may need re-running when those are released.
