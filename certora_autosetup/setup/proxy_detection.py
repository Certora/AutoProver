#!/usr/bin/env python3
"""
Proxy Pattern Detection Module

This module provides functionality to detect proxy patterns in smart contracts
and find their implementation contracts using a single LLM agent with tools.
"""

import asyncio
import subprocess
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field

from certora_autosetup.utils.llm_util import _load_anthropic_key, _get_cached_client, default_anthropic_model
from certora_autosetup.utils.types import ContractHandle

from certora_autosetup.utils.logger import logger

import anthropic
import anthropic.types


# =============================================================================
# Pydantic Models
# =============================================================================


class ImplementationContract(BaseModel):
    """Represents a discovered implementation contract."""
    contract_name: str = Field(description="Name of the implementation contract")
    source_file: str = Field(description="Path to the source file relative to project root")
    reasoning: str = Field(description="Why this contract was identified as an implementation")


class ProxyAnalysisResult(BaseModel):
    """Combined result of proxy detection and implementation finding."""
    is_proxy: bool = Field(description="Whether the contract uses a proxy pattern")
    proxy_pattern: str = Field(
        description="Type of proxy pattern detected",
        examples=["UUPS", "Transparent", "Beacon", "Diamond", "MinimalProxy", "Custom", "NotAProxy"]
    )
    explanation: str = Field(description="Explanation of why this pattern was detected or not")
    implementation_hint: str | None = Field(
        default=None,
        description="Hint about where to find implementation (storage slot name or variable)"
    )
    implementations: list[ImplementationContract] = Field(
        default_factory=list,
        description="List of discovered implementation contracts (empty if not a proxy)"
    )
    search_completed: bool = Field(
        default=True,
        description="Whether the implementation search completed successfully"
    )
    error_message: str | None = Field(
        default=None,
        description="Error message if search failed"
    )


class ProxyMapping(BaseModel):
    """Maps a proxy contract to its implementations."""
    proxy_contract_name: str = Field(description="Name of the proxy contract")
    proxy_source_file: str = Field(description="Source file of the proxy contract")
    implementations: list[ImplementationContract] = Field(
        default_factory=list,
        description="List of implementation contracts for this proxy"
    )
    proxy_pattern: str = Field(description="Type of proxy pattern")


# =============================================================================
# Tool Definitions for Anthropic API
# =============================================================================


FIND_TOOL: dict[str, Any] = {
    "name": "find_files",
    "description": "Search for files by name pattern in the project directory. Use this to find Solidity files that might contain implementation contracts.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path to search in (relative to project root). Use '.' for project root.",
                "default": "."
            },
            "name_pattern": {
                "type": "string",
                "description": "File name pattern to search for (e.g., '*.sol', '*Implementation*.sol')"
            },
            "type": {
                "type": "string",
                "enum": ["f", "d"],
                "description": "Type of entry to find: 'f' for files, 'd' for directories",
                "default": "f"
            }
        },
        "required": ["name_pattern"]
    }
}


GREP_TOOL: dict[str, Any] = {
    "name": "grep_content",
    "description": "Search for text patterns in files. Use this to find contracts that implement specific interfaces or inherit from base contracts.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression pattern to search for"
            },
            "path": {
                "type": "string",
                "description": "File or directory path to search in (relative to project root). Use '.' for project root.",
                "default": "."
            },
            "options": {
                "type": "string",
                "description": "Grep options: -l (files only), -r (recursive), -i (case insensitive), -n (line numbers), -w (whole words), -E (extended regex). Combine as needed, e.g., '-rli'"
            }
        },
        "required": ["pattern"]
    }
}


SUBMIT_RESULT_TOOL: dict[str, Any] = {
    "name": "submit_result",
    "description": "Submit the final analysis result. Call this when you have completed your analysis. If the contract is NOT a proxy, call immediately with is_proxy=false. If it IS a proxy, call after searching for implementations.",
    "input_schema": ProxyAnalysisResult.model_json_schema()
}


# =============================================================================
# Static System Prompt (Cached)
# =============================================================================

# This prompt is static and will be cached across all calls
SYSTEM_PROMPT_STATIC = """You are an expert smart contract security analyst specializing in proxy and delegation patterns.

Your job is narrow: decide whether the given contract is a **proxy that performs delegatecall to another contract**, and if so, find the contract(s) it delegates to.

## READ THIS FIRST — Proxy vs Implementation

In every upgradeable system there are TWO kinds of contracts. They are NOT the same and only ONE of them is a "proxy" for the purposes of this analysis:

### The PROXY contract (this is what we want to detect)
- Contains code that calls `delegatecall` (directly or via a helper like `_delegate`, `_delegateView`, `Address.functionDelegateCall`, assembly `delegatecall`, etc.)
- Has a `fallback()` and/or `receive()` that forwards calls via delegatecall, OR has individual functions that delegatecall
- Stores or reads an implementation address (often via EIP-1967 storage slots)
- Is typically very small — its job is just to forward calls
- Examples: `ERC1967Proxy`, `TransparentUpgradeableProxy`, `BeaconProxy`, `Clones` (EIP-1167), the diamond contract itself, custom proxy/dispatcher contracts

### The IMPLEMENTATION contract (this is NOT a proxy — return is_proxy=false)
- Contains the actual business logic
- Is the **TARGET** of someone else's delegatecall, but does not itself perform delegatecall to forward calls
- May inherit from `UUPSUpgradeable`, `Initializable`, `OwnableUpgradeable`, `AccessControlUpgradeable`, etc.
- May define `initialize()`, `_authorizeUpgrade()`, `_disableInitializers()`
- May be paired with a separate `ERC1967Proxy` deployed elsewhere
- **None of these properties make it a proxy.** It is the thing the proxy points at.

**If the contract you are analyzing is an implementation contract that gets delegated TO, return `is_proxy: false` with `proxy_pattern: "NotAProxy"`. This is the correct answer even if it inherits `UUPSUpgradeable` and defines `_authorizeUpgrade`.**

The single most important question: **does this contract's own code execute `delegatecall` to forward calls to another contract?** If no, it is not a proxy.

## Common false positives — DO NOT classify these as proxies

- Inherits `UUPSUpgradeable` / `Initializable` / `*Upgradeable` → implementation, not proxy
- Defines `_authorizeUpgrade()` → implementation, not proxy
- Calls `_disableInitializers()` in constructor → implementation, not proxy
- Has an `initialize()` function with `initializer` modifier → implementation, not proxy
- Reads from EIP-1967 slots but never delegatecalls → not a proxy
- Inherits a base contract whose name contains "Upgradeable" → not by itself a proxy

The OZ documentation describes UUPS as "the implementation contains the upgrade logic". The word "proxy" in `UUPSUpgradeable` refers to the *separate* `ERC1967Proxy` contract that points at this implementation — that other contract is the proxy, this one is not.

## What DOES make a contract a proxy

The contract's own source must contain a delegatecall path. Concretely, look for:

1. **Direct delegatecall**: `address.delegatecall(...)`, `assembly { ... delegatecall(...) ... }`
2. **Helper-based delegation**: calls to `_delegate(impl)`, `_delegateView(impl)`, `Address.functionDelegateCall(...)`, `Proxy._delegate`
3. **A `fallback()` / `receive()` whose body delegatecalls** to an implementation (this is the canonical proxy shape)
4. **Per-function delegation**: explicit functions whose body is just `_delegate(...)` or `_delegateView(...)` — common in "split contract" patterns

If none of the above appear in the contract's source (including inherited contracts that you can see), it is NOT a proxy.

## Standard proxy patterns (when the contract IS a proxy)

**Transparent Proxy**: `TransparentUpgradeableProxy` — has fallback that delegatecalls, admin-gated upgrades
**ERC1967 Proxy**: bare `ERC1967Proxy` — fallback delegatecalls to EIP-1967 impl slot
**Beacon Proxy**: fallback reads impl from a beacon then delegatecalls
**Diamond (EIP-2535)**: fallback routes by selector to facets via delegatecall
**Minimal Proxy (EIP-1167)**: clone bytecode, hard-coded delegatecall target

Note: "UUPS" is NOT a pattern that applies to the contract being analyzed unless that contract is itself the ERC1967 proxy. UUPS describes where the upgrade logic lives (in the implementation) — but the actual proxy in a UUPS deployment is still a separate `ERC1967Proxy` contract.

## Custom delegation patterns

**Split Contract / Partial Delegation**: contract has its own logic but also delegatecalls some functions to a sibling "view" or "logic" contract. Indicators: `SplitContractMixin`, `viewImplementation`, per-function `_delegateView(impl)` calls. These ARE proxies (partial proxies) because the contract itself executes delegatecall.

## Tools Available

1. **find_files** — Search for files by name pattern
2. **grep_content** — Search for text patterns in files
3. **submit_result** — Submit your final analysis

## Procedure

1. Scan the contract's source for any delegatecall path (direct, assembly, helper, fallback-based, per-function).
2. Also check the contract's inheritance: if it directly inherits from a well-known proxy base contract whose entire job is to delegatecall, treat that as a delegatecall path. Examples that count: `Proxy` (OZ's abstract base), `ERC1967Proxy`, `TransparentUpgradeableProxy`, `BeaconProxy`, `UpgradeableProxy`, `Clones`, custom bases that clearly exist only to forward calls. Do NOT count `UUPSUpgradeable`, `Initializable`, `OwnableUpgradeable`, `AccessControlUpgradeable`, or any `*Upgradeable` mixin — those are for implementations, not proxies.
3. If neither (1) nor (2) yields a delegatecall path, call `submit_result` with `is_proxy: false`, `proxy_pattern: "NotAProxy"`. Do not search further.
4. If you DO find a delegatecall path, identify the target(s) — check constructor parameters, state variables, imports, parent constructors — and use `find_files` / `grep_content` to locate the implementation contract(s) in the project. Then call `submit_result` with `is_proxy: true` and the implementations list.

When unsure, err toward `is_proxy: false`. A wrong "true" causes downstream tools to treat an implementation as a proxy and apply the wrong setup; a wrong "false" is recoverable.

## Limits

- Maximum 10 tool calls.
- If `is_proxy=false`, you should typically need ZERO tool calls — just analyze the source and submit.
- Only spend tool calls searching for implementations after you have confirmed the contract performs delegatecall."""


# =============================================================================
# Proxy Analysis Agent
# =============================================================================


class ProxyAnalysisAgent:
    """Agent that analyzes contracts for proxy patterns and finds implementations."""

    # Whitelisted grep options for security
    ALLOWED_GREP_OPTIONS = {"-l", "-r", "-i", "-n", "-w", "-E"}

    # Maximum iterations for the agent loop
    MAX_ITERATIONS = 10

    # Command timeout in seconds
    COMMAND_TIMEOUT = 30

    # Maximum output size in characters to prevent token overflow
    MAX_OUTPUT_CHARS = 8000

    def __init__(self, project_root: Path, verbose: bool = False):
        self.project_root = project_root
        self.verbose = verbose

    def _truncate_output(self, output: str) -> str:
        """Truncate output if it exceeds MAX_OUTPUT_CHARS."""
        if len(output) <= self.MAX_OUTPUT_CHARS:
            return output
        truncated = output[:self.MAX_OUTPUT_CHARS]
        # Try to truncate at a newline to keep output clean
        last_newline = truncated.rfind('\n')
        if last_newline > self.MAX_OUTPUT_CHARS // 2:
            truncated = truncated[:last_newline]
        return truncated + f"\n\n[OUTPUT TRUNCATED - showing first {len(truncated)} of {len(output)} chars]"

    def _sanitize_path(self, path: str) -> Path:
        """Sanitize a path to prevent directory traversal attacks."""
        path = path.strip()

        if path.startswith("/"):
            abs_path = Path(path)
        else:
            abs_path = self.project_root / path

        abs_path = abs_path.resolve()

        try:
            abs_path.relative_to(self.project_root.resolve())
        except ValueError:
            raise ValueError(f"Path escapes project root: {path}")

        return abs_path

    def _execute_find_sync(self, name_pattern: str, path: str = ".", file_type: str = "f") -> str:
        """Execute a find command with proper sanitization (sync, run via asyncio.to_thread)."""
        try:
            if file_type not in ("f", "d"):
                file_type = "f"
            search_path = self._sanitize_path(path)

            if not search_path.exists():
                return f"Path does not exist: {path}"

            cmd = [
                "find", str(search_path),
                "-not", "-path", "*/.certora_internal/*",
                "-type", file_type,
                "-name", name_pattern
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.COMMAND_TIMEOUT,
                cwd=str(self.project_root)
            )

            output = result.stdout.strip()
            if result.stderr:
                output += f"\nStderr: {result.stderr.strip()}"

            if not output:
                return "No files found matching pattern"
            return self._truncate_output(output)

        except subprocess.TimeoutExpired:
            return f"Command timed out after {self.COMMAND_TIMEOUT} seconds"
        except ValueError as e:
            return f"Invalid path: {e}"
        except Exception as e:
            return f"Error executing find: {e}"

    async def _execute_find(self, name_pattern: str, path: str = ".", file_type: str = "f") -> str:
        """Execute a find command without blocking the event loop."""
        return await asyncio.to_thread(self._execute_find_sync, name_pattern, path, file_type)

    def _execute_grep_sync(self, pattern: str, options: str | None, path: str = ".") -> str:
        """Execute a grep command with proper sanitization (sync, run via asyncio.to_thread)."""
        try:
            search_path = self._sanitize_path(path)

            if not search_path.exists():
                return f"Path does not exist: {path}"

            allowed_opts = []
            if options:
                for opt in options.split():
                    if opt.startswith("-") and len(opt) > 1:
                        if opt in self.ALLOWED_GREP_OPTIONS:
                            allowed_opts.append(opt)
                        else:
                            combined = opt[1:]
                            valid_combined = []
                            for char in combined:
                                single_opt = f"-{char}"
                                if single_opt in self.ALLOWED_GREP_OPTIONS:
                                    valid_combined.append(char)
                            if valid_combined:
                                allowed_opts.append(f"-{''.join(valid_combined)}")

            cmd = ["grep", "--exclude-dir=.certora_internal", "--include=*.sol"]
            cmd.extend(allowed_opts)
            cmd.append(pattern)
            cmd.append(str(search_path))

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.COMMAND_TIMEOUT,
                cwd=str(self.project_root)
            )

            output = result.stdout.strip()
            if not output:
                return "No matches found"
            return self._truncate_output(output)

        except subprocess.TimeoutExpired:
            return f"Command timed out after {self.COMMAND_TIMEOUT} seconds"
        except ValueError as e:
            return f"Invalid path: {e}"
        except Exception as e:
            return f"Error executing grep: {e}"

    async def _execute_grep(self, pattern: str, options: str | None, path: str = ".") -> str:
        """Execute a grep command without blocking the event loop."""
        return await asyncio.to_thread(self._execute_grep_sync, pattern, options, path)

    async def analyze(
        self,
        contract: ContractHandle,
        contract_source: str,
        api_key: str | None = None
    ) -> ProxyAnalysisResult:
        """Analyze a contract for proxy patterns and find implementations.

        Args:
            contract: The contract to analyze
            contract_source: Source code of the contract
            api_key: Anthropic API key (uses env var if not provided)

        Returns:
            ProxyAnalysisResult with detection and implementation info
        """

        loaded_key = _load_anthropic_key(self.verbose, api_key)
        if not loaded_key:
            logger.warning(f"Proxy detection skipped for {contract.contract_name} - No API key available")
            return ProxyAnalysisResult(
                is_proxy=False,
                proxy_pattern="NotAProxy",
                explanation="No API key available",
                search_completed=False,
                error_message="No API key available"
            )

        client = _get_cached_client(loaded_key, async_client=True)
        if not client:
            logger.warning(f"Proxy detection skipped for {contract.contract_name} - Failed to initialize client")
            return ProxyAnalysisResult(
                is_proxy=False,
                proxy_pattern="NotAProxy",
                explanation="Failed to initialize Anthropic client",
                search_completed=False,
                error_message="Failed to initialize Anthropic client"
            )

        # Build user message with contract-specific info (variable part)
        user_message = f"""Analyze this contract for proxy patterns:

**Contract Name:** {contract.contract_name}
**Source File:** {contract.source_file}

**Source Code:**
```solidity
{contract_source}
```

Reminder: a "proxy" is a contract that itself executes `delegatecall` to forward calls to another contract — either directly in its own code, or by inheriting from a well-known proxy base whose whole job is to delegatecall (`Proxy`, `ERC1967Proxy`, `TransparentUpgradeableProxy`, `BeaconProxy`, etc.). An implementation contract that merely inherits `UUPSUpgradeable` / `Initializable` / other `*Upgradeable` mixins and defines `_authorizeUpgrade` is NOT a proxy — it is the target of some other contract's delegatecall, and you must return `is_proxy: false` for it.

- If this contract has no delegatecall path (neither in its own code nor via inheritance from a known proxy base): immediately call submit_result with is_proxy=false, proxy_pattern="NotAProxy".
- If this contract DOES have a delegatecall path: use tools to find the target implementation(s), then call submit_result with is_proxy=true."""

        # System message with cache control on static content
        system_messages: list[anthropic.types.TextBlockParam] = [
            {
                "type": "text",
                "text": SYSTEM_PROMPT_STATIC,
                "cache_control": {"type": "ephemeral"}  # type: ignore[typeddict-item]
            }
        ]

        messages: list[anthropic.types.MessageParam] = [{"role": "user", "content": user_message}]
        tools: list[anthropic.types.ToolParam] = [
            cast(anthropic.types.ToolParam, FIND_TOOL),
            cast(anthropic.types.ToolParam, GREP_TOOL),
            cast(anthropic.types.ToolParam, SUBMIT_RESULT_TOOL)
        ]

        logger.info(f"Analyzing {contract.contract_name} for proxy patterns...")

        # Agent loop
        for iteration in range(1, self.MAX_ITERATIONS + 1):
            if iteration > 1:
                logger.debug(f"Agent iteration {iteration} for {contract.contract_name}")
            try:
                response = await client.messages.create(
                    model=default_anthropic_model(),
                    max_tokens=4096,
                    system=system_messages,
                    messages=messages,
                    tools=tools
                )

                if response.stop_reason == "end_turn":
                    logger.warning(f"Agent ended without submitting result for {contract.contract_name}")
                    return ProxyAnalysisResult(
                        is_proxy=False,
                        proxy_pattern="NotAProxy",
                        explanation="Agent completed without submitting results",
                        search_completed=False,
                        error_message="Agent completed without calling submit_result"
                    )

                assistant_content = response.content
                messages.append({"role": "assistant", "content": assistant_content})

                tool_results: list[anthropic.types.ToolResultBlockParam] = []
                for block in response.content:
                    if isinstance(block, anthropic.types.ToolUseBlock):
                        tool_name = block.name
                        tool_input = cast(dict[str, Any], block.input)

                        if tool_name == "submit_result":
                            result = ProxyAnalysisResult.model_validate(tool_input)
                            if result.is_proxy:
                                impl_names = [impl.contract_name for impl in result.implementations]
                                logger.info(f"{contract.contract_name}: {result.proxy_pattern} proxy detected, "
                                            f"{len(result.implementations)} implementation(s): {impl_names}")
                            else:
                                logger.info(f"{contract.contract_name}: Not a proxy")
                            logger.info(f"Explanation: {result.explanation}")
                            return result

                        elif tool_name == "find_files":
                            name_pattern = tool_input.get("name_pattern", "*.sol")
                            find_path = tool_input.get("path", ".")
                            find_type = tool_input.get("type", "f")
                            logger.debug(f"find_files: pattern={name_pattern}, path={find_path}")
                            find_result = await self._execute_find(name_pattern, find_path, find_type)
                            lines = find_result.split('\n')
                            preview = lines[:3] if len(lines) > 3 else lines
                            logger.debug(f"find_files result: {len(lines)} lines, preview: {preview}")
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": find_result
                            })

                        elif tool_name == "grep_content":
                            grep_pattern = tool_input.get("pattern", "")
                            grep_path = tool_input.get("path", ".")
                            grep_options = tool_input.get("options")
                            logger.debug(f"grep_content: pattern={grep_pattern}, path={grep_path}, options={grep_options}")
                            grep_result = await self._execute_grep(grep_pattern, grep_options, grep_path)
                            lines = grep_result.split('\n')
                            preview = lines[:3] if len(lines) > 3 else lines
                            logger.debug(f"grep_content result: {len(lines)} lines, preview: {preview}")
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": grep_result
                            })

                if tool_results:
                    messages.append({"role": "user", "content": tool_results})

            except Exception as e:
                logger.error(f"Error during proxy analysis for {contract.contract_name}: {e}")
                return ProxyAnalysisResult(
                    is_proxy=False,
                    proxy_pattern="NotAProxy",
                    explanation=f"Agent error: {e}",
                    search_completed=False,
                    error_message=f"Agent error: {e}"
                )

        logger.warning(f"Max iterations ({self.MAX_ITERATIONS}) reached for {contract.contract_name}")
        return ProxyAnalysisResult(
            is_proxy=False,
            proxy_pattern="NotAProxy",
            explanation=f"Max iterations ({self.MAX_ITERATIONS}) reached",
            search_completed=False,
            error_message=f"Max iterations ({self.MAX_ITERATIONS}) reached without completing analysis"
        )


# =============================================================================
# Main Proxy Detector Class
# =============================================================================


class ProxyDetector:
    """Detects proxy patterns in contracts and finds their implementations."""

    def __init__(self, project_root: Path, verbose: bool = False):
        self.project_root = project_root
        self.verbose = verbose
        self.agent = ProxyAnalysisAgent(project_root, verbose)

    async def analyze_contract(self, contract: ContractHandle) -> ProxyAnalysisResult:
        """Analyze a single contract for proxy patterns.

        Args:
            contract: The contract to analyze

        Returns:
            ProxyAnalysisResult with detection and implementation info
        """
        try:
            contract_path = Path(contract.source_file)
            if not contract_path.is_absolute():
                contract_path = self.project_root / contract_path

            if not contract_path.exists():
                logger.warning(f"Proxy detection skipped for {contract.contract_name} - "
                               f"File not found: {contract.source_file}")
                return ProxyAnalysisResult(
                    is_proxy=False,
                    proxy_pattern="NotAProxy",
                    explanation=f"Contract file not found: {contract.source_file}",
                    search_completed=False,
                    error_message=f"File not found: {contract.source_file}"
                )

            contract_source = contract_path.read_text()
            return await self.agent.analyze(contract, contract_source)

        except Exception as e:
            logger.error(f"Error reading contract {contract.contract_name}: {e}")
            return ProxyAnalysisResult(
                is_proxy=False,
                proxy_pattern="NotAProxy",
                explanation=f"Error reading contract: {e}",
                search_completed=False,
                error_message=str(e)
            )

