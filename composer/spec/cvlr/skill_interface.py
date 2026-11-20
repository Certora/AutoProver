"""What the ``certora-autoprover-solana`` and ``certora-autoprover-soroban`` skills depend on in
this package.

The skills (``Certora/autoprover-skill``, ``plugins/certora-autoprover-solana`` and
``plugins/certora-autoprover-soroban``) each pin a commit of this repository and name these
commands, flags and files in their own text. Nothing in this repository reads that text, so a
rename here would pass CI and break a skill at its next pin bump. The tests marked
``skill_interface`` check the code against this module instead.

The names are written out rather than imported from the code they describe: an edit that changes
one of them has to change this file too, where a reviewer sees that the skills are affected.
"""

#: The console script.
COMMAND = "certora-cvlr"

#: Each subcommand the skills run, with the flags they pass. ``--token-model`` and the ``--chain``
#: flags are the Soroban skill's; everything else both skills pass.
SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    "toolchain": ("--chain",),
    "setup": ("--package", "--component", "--no-check", "--token-model"),
    "check": ("--package", "--component"),
    "prove": (
        "--package",
        "--component",
        "--rule",
        "--exclude-rule",
        "--loop-iter",
        "--optimistic-loop",
        "--msg",
        "--unsat-core",
    ),
    "wait": ("--for", "--chain"),
    "vacuity": ("--rule",),
    "cex": ("--rule", "--chain"),
}

#: What ``wait`` prints first: the job is still running, or it finished and the verdicts follow,
#: one ``  <STATUS> <rule>`` line each, or it failed.
WAIT_STATES = ("RUNNING", "FINISHED", "FAILED")

#: Where setup writes the reference text, relative to the project root.
REFERENCE_DIR = ".certora_internal/cvlr/reference"

#: The reference files the Solana skill reads, relative to :data:`REFERENCE_DIR`.
REFERENCE_FILES = (
    "README.md",
    "cvlr.md",
    "properties.md",
    "author.md",
    "judge.md",
    "invariants.md",
    "vacuity.md",
    "recipes/index.md",
)

#: The reference files the Soroban skill reads. No recipes: the Soroban reference ships none, as
#: :func:`composer.spec.cvlr.skill.reference_documents` says.
SOROBAN_REFERENCE_FILES = (
    "README.md",
    "cvlr.md",
    "properties.md",
    "author.md",
    "judge.md",
    "invariants.md",
    "vacuity.md",
)

#: Sections of ``author.md`` the Solana skill sends the agent to by heading.
AUTHOR_SECTIONS = (
    "## State invariants: a base case and one step per writer",
    "## Reaching a handler on an Anchor program",
    "## The loop ladder: when a loop will not finish",
    "## When a rule should fail",
)

#: Sections of the Soroban ``author.md`` the Soroban skill sends the agent to by heading. The
#: invariant method has no author section on Soroban; it is the reference's ``invariants.md``.
SOROBAN_AUTHOR_SECTIONS = (
    "## Storage, and what a rule may say about it",
    "## Authorization",
    "## Loops are bounded, and the bound is not assumed away",
    "## When a rule should fail",
)

#: The harness layout setup writes, relative to the package root.
SPECS_DIR = "src/certora/specs"
SHARED_INVARIANTS_FILE = "src/certora/specs/invariants.rs"
#: The component setup always declares, which holds the shared invariants' base cases and steps.
INVARIANT_RULES = "invariant_rules"
#: A component's points-to summaries, with its module name in place of ``{module}``.
SUMMARY_LAYER = "src/certora/summaries/{module}.txt"
#: A component's cargo feature, with its module name in place of ``{module}``.
UNIT_FEATURE = "certora_unit_{module}"
#: The feature every verification build enables.
HARNESS_FEATURE = "certora"
