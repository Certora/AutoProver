#!/usr/bin/env node
/**
 * Hardhat Config Extractor
 *
 * Extracts relevant configuration from hardhat.config.js using the
 * Hardhat Runtime Environment (HRE) API.
 *
 * Based on: https://v2.hardhat.org/hardhat-runner/docs/advanced/hardhat-runtime-environment
 */

const Module = require('module');
const fs = require('fs');
const path = require('path');

try {
    // Ensure Node.js looks for modules in the project directory (cwd)
    // This allows require("hardhat") to find the locally installed hardhat package
    const projectRoot = process.cwd();
    const originalPaths = module.paths;
    module.paths = Module._nodeModulePaths(projectRoot);

    // Check if hardhat.config.ts exists (TypeScript config)
    const tsConfigPath = path.join(projectRoot, 'hardhat.config.ts');
    const hasTypeScriptConfig = fs.existsSync(tsConfigPath);

    // If TypeScript config exists, try to register ts-node
    if (hasTypeScriptConfig) {
        try {
            // Register ts-node with transpileOnly to skip type checking
            // This allows loading TypeScript configs even if the project has type errors
            require('ts-node').register({
                transpileOnly: true,
                compilerOptions: {
                    module: 'commonjs'
                }
            });
        } catch (tsError) {
            // ts-node not available, output error and empty object
            console.error('TypeScript config detected but ts-node not available:', tsError.message);
            console.log('{}');
            process.exit(0);
        }
    }

    // Load the Hardhat Runtime Environment
    // This must be run from the project directory containing hardhat.config.js/ts
    const hre = require("hardhat");

    // Access the resolved configuration
    const config = hre.config;

    // Extract relevant settings
    const output = {
        solidity: config.solidity || {},
        paths: config.paths || {},
        // hre.config is the *resolved* config: when the user config has no
        // `solidity` entry, hardhat fills in its own built-in default compiler
        // (0.7.3), which says nothing about what the project's sources need.
        // Flag that case so the caller can ignore the resolved version.
        solidityImplicitDefault: hre.userConfig.solidity === undefined
    };

    // Output as JSON
    console.log(JSON.stringify(output));

    // Restore original module paths
    module.paths = originalPaths;
} catch (error) {
    // If we can't load Hardhat or the config, output empty object
    // This allows graceful fallback to defaults
    console.error('Error loading Hardhat config:', error.message);
    console.log('{}');
}
