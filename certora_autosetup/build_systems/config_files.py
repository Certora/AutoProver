"""The filenames each build system anchors a project on.

A directory holding one of these is a project root, which is all some callers need to know:
``utils/remappings._projects_under`` recognises the other projects under a run root without
knowing what to do with any of them. That module is imported by the managers themselves, so
it cannot import them back — hence a leaf module both sides read the names from, rather than
a second copy of the list.
"""

FOUNDRY_CONFIG_FILENAMES = ("foundry.toml",)

HARDHAT_CONFIG_FILENAMES = ("hardhat.config.js", "hardhat.config.ts")

# `truffle.js` is the v4 spelling, `truffle-config.js` everything since.
TRUFFLE_CONFIG_FILENAMES = ("truffle-config.js", "truffle.js")

BUILD_CONFIG_FILENAMES = (
    FOUNDRY_CONFIG_FILENAMES + HARDHAT_CONFIG_FILENAMES + TRUFFLE_CONFIG_FILENAMES
)
