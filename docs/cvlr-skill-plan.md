# A Solana skill: CVLR verification driven by the user's own agent

The `certora-autoprover` skill
([Certora/autoprover-skill](https://github.com/Certora/autoprover-skill)) verifies a Solidity
contract with whatever coding agent the user already runs. The agent does the judgment work: the
component model, the properties, the spec, reading counterexamples. A pinned
AutoProver install does the mechanical work, through `certora-autosetup`. This document plans the
same thing for Solana programs: a `certora-autoprover-solana` skill whose rules are CVLR and whose
mechanical steps come from the CVLR backend in this repository.

It is not the CVLR pipeline (`console-solana`). The pipeline runs its own agents, needs an
Anthropic key and a Postgres knowledge base, and owns the whole run. The skill needs none of that
from the user. What it needs from AutoProver is a set of commands and a body of reference text,
both pinned to one commit.

**Where it stands, 2026-11-09.** Two branches in the skill repository, neither pushed:

- `eric/plugins-layout` moves the existing plugin under `plugins/certora-autoprover/` so the
  `certora-skills` marketplace can hold a second plugin, generalizes its CI to every plugin, and
  bumps to 0.6.7. Upgrades from 0.6.6 were tested against Claude Code from a local git server, and
  read from the source of `npx skills` and Codex.
- `eric/solana-skill`, on top of it, is empty. The Solana skill is built there, and the layout
  change is proposed upstream only once the skill works.

Nothing has been written in this repository yet.

## Settled

**Same repository, a second plugin.** The two skills share their method: the phases, the
property sorts and classes, the generator/judge loop, the waiting rules, the report. An installed
plugin is a copy of its own directory and nothing else, so shared text is copied into each skill,
and one repository is where a CI check can keep the copies equal. That check waits until the
Solana skill works; until then the copies may diverge freely.

**Commands for the mechanical work, not prose.** Each step below already exists in
`composer/spec/cvlr/` and `composer/cargo/`, behind the pipeline. Written as instructions, each one
is a page an agent gets wrong some of the time, and the failures are quiet: a build against
crates.io Anchor fails `[3006]` on every handler, a missing feature forward compiles the wrong
module, a CVLR pin from another platform generation fails late and names nothing.

**What is method and what is fact.** The skill repository holds the method. The CVLR facts — the
API, the rule anatomy, the pitfalls, the docs map — change with the backend, often weekly, so
AutoProver ships them in the package and the skill reads them from the installed tool. They are
then always the version that matches the commands. The CVL skill restates its facts by hand,
including about fifteen `file:line` citations into this repository, and every pin bump re-checks
them by hand. The Solana skill does neither.

**The user's crate is edited, behind a feature, and the edits are reported.** The harness lives
in the program crate: `src/certora/`, a `certora` feature, CVLR dependencies, the `[patch]` tables
for Certora's forks. That is what `scaffold.py` writes today. The CVL skill's only source edits
are comment annotations, so the report lists every edit, the way the CVL skill lists
`source_edits.json`.

**Scope of the first version.** Anchor and plain `solana-program` programs on the current
reference set (`reference.py`). A program on another platform generation is refused with the
scaffold's own `Blocked` message, not adapted. Pinocchio and older generations come later.

**Evals.** A small Anchor program with a planted bug, and `test_scenarios/solana_vault_idl`, the
Anchor vault the CVLR tape already drives and the one scenario in the tree the scaffold accepts:
the counterparts of the CVL skill's Counter and Answer.

## Open

**Structural invariants.** CVLR has no inductive `invariant`, and the CVL skill's Phase 5 is built
on one. The proposal: each structural invariant becomes a well-formedness predicate over an
account's state, assumed by other rules the way CVL uses `requireInvariant`, plus one rule per
instruction that assumes it before and asserts it after. Whether that is sound enough to lean on
— an instruction that creates the account has no "before" — is the first thing to settle, because
Phase 5 and the judge's criteria follow from it.

**Command names and boundaries.** Below they are named by what they do.

## Work in this repository

**A1 — Set up a project.** A console script over `preflight.select_package`, `plan_scaffold` +
`apply`, the fork plan, and the compile gate. It prints what it edited, and exits non-zero with
the `Blocked` reason when it refuses. It submits nothing.

**A2 — Build and submit.** Over `prover.prepare_submission` / `run_submission`: build with
`cargo certora-sbf`, write the build script and conf outside the working tree, submit, and print
the job link. Rule filters and the author-tunable conf settings (`conf.py`) are flags. Waiting for
the job stays with the skill, as in the CVL skill's foreground loop.

**A3 — Explain a result.** Over `vacuity.py` (why a rule passed vacuously, from its unsat core)
and `externals.py` (functions the Prover treated as external). The output is text the agent reads.

**A4 — The reference text, shipped.** CVLR writing guidance, pitfalls, the worked example and the
docs map, as package data under one directory, drawn from what the pipeline's prompts already say
(`guidance.py`, `example.py`, the templates). A command prints that directory's path, so the skill
names a command rather than the package layout. The CVL skill finds `hardhat_config_extractor.js`
by its path inside the package, which is the coupling to avoid.

**I — The skill's interface, declared and tested.** Nothing in this repository knows the CVL skill
exists: renaming one of `certora-autosetup`'s flags would pass CI and break the skill at its next
pin bump. For the Solana skill:

1. One module lists what the skill depends on: the commands of A1–A4, their flags, the files they
   write, and the reference files of A4.
2. A pytest marker, `skill_interface`, registered in `pyproject.toml` and not `expensive`, runs
   each command against a small fixture up to the point of submission. A change that breaks the
   interface fails here, in front of whoever made it.
3. Every commit the skill pins is tagged (`skill/certora-autoprover-solana/<version>`), so the SHA
   stays reachable when its branch moves on. `uv tool install …@<sha>` needs that.

## Work in the skill repository

**S1 — `SKILL.md`.** The CVL skill's phases, adapted: preflight checks the Rust and SBF toolchain
instead of solc; the component model is programs, instructions, accounts and PDAs, with CPI
targets as external actors; setup is A1; the prover loop is A2 and A3; the report is unchanged.

**S2 — Property classes.** The CVL skill's list, plus the classes Solana adds: signer, owner and
PDA checks, CPI targets, close and realloc, rent.

**S3 — The pin check.** On a change to `AUTOPROVER_REF`, CI installs `ai-composer` at the pin and
checks that every command and flag the skill names exists, and that the reference directory holds
every file the skill reads. Minutes, no cloud jobs.

**S4 — Evals.** The two fixtures above, run by hand on every pin bump. The Prover's share is cheap;
the agent's tokens are the cost.

## Order

A1, A4 and I go first and together: the interface test is written against the first command, not
added later. The first `SKILL.md` draft can then run setup against a real project while A2 and A3
are built. The skill repository's pin points at a tagged commit on `feature/solana` until the
commands reach master. Items I and S3 apply to the CVL skill as well; that repository belongs to
its maintainers, so the pattern is offered to them once it has worked here.
