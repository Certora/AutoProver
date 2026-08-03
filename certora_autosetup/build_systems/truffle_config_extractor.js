#!/usr/bin/env node
/**
 * Truffle Config Extractor
 *
 * Loads truffle-config.js / truffle.js and prints the compiler-relevant fields as JSON.
 *
 * Unlike the Hardhat extractor this never requires the build system itself: a Truffle
 * config is a plain CommonJS module, so evaluating it is enough — a project whose
 * node_modules holds no `truffle` still yields its pinned solc. Nested requires inside
 * the config (`module.exports = require('<shared-truffle-config-pkg>')` is a common
 * Truffle-era layout) resolve against the config file's own directory, which is where
 * the real settings then live.
 */

const path = require('path');

const configPath = process.argv[2];

try {
    if (!configPath) {
        throw new Error('usage: truffle_config_extractor.js <path-to-truffle-config>');
    }

    const config = require(path.resolve(configPath)) || {};

    console.log(JSON.stringify({
        // Truffle v5: { compilers: { solc: { version, settings: { optimizer, evmVersion } } } }
        compilers: config.compilers || {},
        // Truffle v4: a top-level `solc` block holding only settings — the compiler
        // version was bundled with truffle itself, so there is nothing to pin there.
        solc: config.solc || {},
        contracts_directory: config.contracts_directory || null,
        contracts_build_directory: config.contracts_build_directory || null,
    }));
} catch (error) {
    // A config that cannot be evaluated (typically `require`-ing a dependency that was
    // never installed) is reported as data, not as a non-zero exit: the caller falls back
    // to Truffle's defaults rather than aborting the run, and the reason reaches the log.
    // Node appends a multi-line "Require stack" to resolution failures; the first line
    // names the missing module, which is the whole diagnosis.
    console.log(JSON.stringify({ error: String(error.message).split('\n')[0] }));
}
