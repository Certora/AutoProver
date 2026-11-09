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

In this repository, A1–A4 and I are built: `certora-cvlr setup`, `check`, `prove`, `wait` and
`vacuity` (`composer/cli/certora_cvlr.py`, `composer/spec/cvlr/skill.py`), the interface module
`composer/spec/cvlr/skill_interface.py`, and the `skill_interface` tests in
`tests/test_cvlr_skill.py`. S1, the first `SKILL.md`, is next. Nothing is tagged yet.

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

**Structural invariants: one shared module, proved first, assumed everywhere.** CVLR has no
inductive `invariant`, and the CVL skill's Phase 5 is built on one: `invariants.spec` is proved
before any component, changed only at the barrier, imported by every component spec and used
through `requireInvariant`. The Solana skill keeps that shape and fills it with the pipeline's own
recipe, the "State invariants" section of `cvlr_property_generation_system_prompt.j2` and
`cvlr_invariant_guidance.j2` (U37):

- **What an invariant may be about** is the guidance's answer. One account's fields, or accounts
  every writer is given together, tied by a constraint such as `has_one`. A relationship over every
  account of a type has no rule, because no handler sees them all, so Phase 4 restates it as what
  each operation does to the accounts it touches. A fixed-size array is stated per element, at a
  nondet index assumed in bounds.
- **Where it lives.** The predicates in `src/certora/specs/invariants.rs`, compiled whenever the
  `certora` feature is on; the rules that prove them in `invariant_rules.rs`, a component of their
  own that setup always declares, so that a component's job does not check them again. Each component's module stays behind a feature of its own, as in the pipeline, so parallel
  generators cannot break each other's builds; every one of them can still `use` the shared module.
  The shared module is compiled with every component, so a trait impl it makes (`impl Nondet for
  Pool`) is one no component may repeat (`E0119`). The pipeline's units never meet, which is why
  they can each make their own; here the shared module owns the impls its invariants need, and
  components use them.
- **What proves it.** Each invariant is a `pub fn` returning `bool`. Its doc comment lists every
  writer of a field it reads, found in the code and followed up to the handlers, across the whole
  program and not one component. The base case runs each instruction that creates the state, from
  arbitrary inputs, and asserts the invariant: that is the answer to an instruction that creates an
  account having no "before". One step rule per writer assumes the invariant, runs the writer and
  asserts it, generated with `cvlr_invariant_rules!` or a local macro. A handler that may create
  or modify the same account (`init_if_needed`, `realloc`) is both a base case and a step.
- **The gate.** Phase 5 closes when the judge has accepted the module and a full run shows every
  base case and step VERIFIED. None is ever marked expected-to-fail: a component that assumed an
  invariant whose step was marked would assume exactly what failed to prove. A failing step whose
  pre-state nothing produces strengthens the invariant, and the new conjunct gets its own steps.
- **How a component uses it.** `cvlr_assume!(<predicate>(..))` on the rule's pre-state is the
  counterpart of `requireInvariant`. The rule's commentary names the step rules behind it, and the
  judge rejects an assumption of a predicate that is not in the shared module, or whose steps did
  not all verify. An assumption the module cannot supply follows the pipeline's remedies in order:
  a request for a new invariant at the barrier, then a justification rule, then a bare
  `cvlr_assume!` that says what it gives up.
- **The barrier.** Only the orchestrator edits `invariants.rs`, between generation and judging,
  and then re-runs the Phase 5 gate, exactly as the CVL skill treats `invariants.spec`.

The pipeline has the recipe but not the sharing: its units build in isolation, so an invariant one
unit proves cannot be assumed by another (`cvlr-todo.md` U56). Its hoisting step, which moves a
property several components state into one shared unit (`composer/spec/hoist.py`), becomes a step
at the end of the skill's Phase 4. The orchestrator already sees every component's property file,
so merging duplicates into a protocol component, checked with `cvlr_rules!` over every handler,
needs no model call of its own. The recipe has not yet been exercised by a pipeline run, so the
skill's first evals are also its first test.

**One command, `certora-cvlr`, with a subcommand per step.** One console script is one name for
the skill to pin and the interface test to check; `setup` is A1, and A2 and A3 become
subcommands beside it. A4 is not a subcommand: `setup` writes the reference text into the project
(`.certora_internal/cvlr/reference/`) and prints the path, rendered for the CVLR versions the
project resolves.

**Builds are unconfined unless asked.** The pipeline confines a build by default because it compiles
a project nobody at the keyboard chose to build. Here the user's agent already builds the project
with the user's permissions, and the launcher is a Linux binary `uv tool install` does not provide.
`COMPOSER_SANDBOX_PROVIDER` still opts in.

## Work in this repository

**A1 — Set up a project.** A console script over `preflight.select_package`, `plan_scaffold` +
`apply`, the fork plan, and the compile gate. It prints what it edited, and exits non-zero with
the `Blocked` reason when it refuses. It submits nothing. It also declares the harness's modules:
`invariants` under the `certora` feature, and one module and feature per component the agent names
(`harness.declare_modules` writes `specs/mod.rs`, `scaffold.declare_unit_features` the features),
so the skill never edits either by hand. Today `declare_modules` gates every module on a unit
feature; the ungated `invariants` module is new, and U56 needs it too.

**A2 — Build and submit.** Three subcommands:

- `check --component M` type-checks one component for the SBF target in seconds, the pipeline
  author's `cargo_check`.
- `prove --component M` builds with `cargo certora-sbf`, writes the build script and conf outside
  the working tree, submits, and prints the job link without waiting. Rule filters and the
  author-tunable settings (`--loop-iter`, `--optimistic-loop WHY`) are flags. A component's
  points-to summaries are a file the agent edits, `src/certora/summaries/<module>.txt`, composed
  into the component's conf on every prove. From the build until the job is accepted nothing else
  may build the crate, so concurrent proves of one workspace queue on a file lock; a generator may
  edit its own module meanwhile, because another component's module is behind a disabled `cfg`.
- `wait <link> --for SECONDS` polls the job for at most that long and prints `RUNNING <status>`, or
  `FINISHED` with one line per rule and the Prover's report, or `FAILED` with the reason. The
  skill's bounded foreground loop is this one command repeated; there is no detached process to
  manage. `run_prover` is split into `submit_prover_job` and `collect_prover_results` for it.

**A3 — Explain a result.** No model call in AutoProver: the skill's agent does the reading the
pipeline's analyst does, from the same instructions (`vacuity.md` in the reference).

- `prove --unsat-core --rule R` is the pipeline's `explain_vacuity` rerun: the vacuity check off,
  unsat cores on. `vacuity <link> --rule R` then prints whether the rule's assertion is in the core
  (`vacuity.core_finding`, decided without a model) and the core itself. Contradictory assumptions
  the Prover settles before solving, such as two bounds on one value, write no core; `vacuity` says
  so rather than leaving an empty answer.
- `wait` appends the Prover's warnings about functions it treated as external
  (`externals.external_warnings`, the note `verify_rules` gives), and `prove` says which summary
  directives match no function in the build, as `verify_rules` does.

**A4 — The reference text, shipped.** Rendered by `setup` from the sources the pipeline's agents
read, so there is no second copy to drift: the baseline facts (`cvlr_baseline_facts.md`), the
property guidance (`guidance.py`), the author's and judge's system prompts (rendered without a
worked example), the invariant guidance, and the recipes, one file per retrieval ID. A README maps
each pipeline tool those texts name to what the skill's agent does instead. The skill names the
directory `setup` prints rather than a path inside the package; the CVL skill finds
`hardhat_config_extractor.js` by its path inside the package, which is the coupling to avoid. The
skill sends the agent to sections of `author.md` by heading, so those headings are part of the
interface.

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
targets as external actors; setup is A1; Phase 4 ends by merging duplicate protocol-wide
properties; Phase 5 is the shared invariants module above; the prover loop is A2 and A3; the report
is unchanged.

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
