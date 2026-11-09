"""What the ``certora-autoprover-solana`` skill depends on in this package.

The skill (``Certora/autoprover-skill``, ``plugins/certora-autoprover-solana``) pins a commit of
this repository and names these commands, flags and files in its own text. Nothing in this
repository reads that text, so a rename here would pass CI and break the skill at its next pin
bump. The tests marked ``skill_interface`` check the code against this module instead.

The names are written out rather than imported from the code they describe: an edit that changes
one of them has to change this file too, where a reviewer sees that the skill is affected.
"""

#: The console script.
COMMAND = "certora-cvlr"

#: Each subcommand the skill runs, with the flags it passes.
SUBCOMMANDS: dict[str, tuple[str, ...]] = {
    "setup": ("--package", "--component", "--no-check"),
}

#: Where setup writes the reference text, relative to the project root.
REFERENCE_DIR = ".certora_internal/cvlr/reference"

#: The reference files the skill reads, relative to :data:`REFERENCE_DIR`.
REFERENCE_FILES = (
    "README.md",
    "cvlr.md",
    "properties.md",
    "author.md",
    "judge.md",
    "invariants.md",
    "recipes/index.md",
)

#: Sections of ``author.md`` the skill sends the agent to by heading.
AUTHOR_SECTIONS = (
    "## State invariants: a base case and one step per writer",
    "## Reaching a handler on an Anchor program",
    "## The loop ladder: when a loop will not finish",
    "## When a rule should fail",
)

#: The harness layout setup writes, relative to the package root.
SPECS_DIR = "src/certora/specs"
SHARED_INVARIANTS_FILE = "src/certora/specs/invariants.rs"
#: A component's cargo feature, with its module name in place of ``{module}``.
UNIT_FEATURE = "certora_unit_{module}"
#: The feature every verification build enables.
HARNESS_FEATURE = "certora"
