#!/usr/bin/env python3
"""
Automatic Setup for Contract Summaries

This script detects if certain functions are called in the project contracts
and sets up appropriate summaries for verification.

Currently supported summaries:
- Math.mulDiv from OpenZeppelin Math.sol
- toString from OpenZeppelin ShortStrings.sol
- toString from OpenZeppelin Strings.sol
"""


import argparse
import functools
import json
import logging
import logging.handlers
import os
import re
import shutil
import asyncio
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, TypedDict, Coroutine, Literal, Annotated
from pydantic import BaseModel, Field, ValidationError, Discriminator

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.contract_utils import split_contract_spec
from certora_autosetup.cache.cache_fs import cache_path, get_fs
from certora_autosetup.utils.constants import (
    ANTHROPIC_API_KEY_ENV,
    DIR_CERTORA_INTERNAL,
    DIR_LLM_CACHE,
    PATH_ALL_METHODS_JSON,
    SUMMARIES_SUBDIR,
)
from certora_autosetup.utils.llm_util import (
    call_llm_structured,
    call_llm_async_structured_cached,
    default_anthropic_model,
    is_local_backend,
)

from certora_autosetup.setup.solidity_utils import DEPENDENCIES, find_all_library_files as util_find_all_library_files
from certora_autosetup.setup.solidity_utils import find_all_solidity_files as util_find_all_solidity_files
from certora_autosetup.setup.solidity_utils import walk_files_by_suffix

# Import method parser
from certora_autosetup.parsers.method_parser import MethodParser
from certora_autosetup.parsers.spec_imports import parse_imports_from_spec
from certora_autosetup.setup.summary_resolver import resolve_summary_specs
from certora_autosetup.setup.signature_types import InheritanceGraph


# CVL grammar keyword terminals that cannot double as an identifier. A Solidity parameter whose name
# equals one of these is lexed as that keyword inside a methods{} entry, which is a syntax error; such
# names are suffixed with "_" before emission (see _cvl_safe_param_name).
#
# This deliberately EXCLUDES the terminals listed under the `usable_keywords` production in cvl.cup
# (exists, forall, sum, usum, using, as, import, use, builtin, override, sig, description, invariant,
# preserved, weak, strong, onTransactionBoundary, old, hook, unresolved). The grammar accepts those
# wherever an identifier is expected, so a parameter named after one parses fine and must NOT be
# mangled. Note: uppercase "UNRESOLVED" is a distinct summary keyword and remains reserved.
CVL_RESERVED_WORDS = frozenset({
    "ALL", "ALWAYS", "ASSERT_FALSE", "AUTO", "CONSTANT", "Create", "DELETE", "DISPATCH", "DISPATCHER",
    "HAVOC_ALL", "HAVOC_ECF", "NONDET", "PER_CALLEE_CONSTANT", "STORAGE", "Sload", "Sstore", "Tload",
    "Tstore", "UNRESOLVED", "assert", "assuming", "at", "axiom", "default", "definition", "else",
    "event", "expect", "fallback", "false", "filtered", "function", "ghost", "good_description",
    "havoc", "if", "in", "indexed", "lastReverted", "lastStorage", "links", "mapping", "methods",
    "new", "norevert", "persistent", "require", "requireInvariant", "reset_storage", "return",
    "returns", "revert", "rule", "satisfy", "sort", "true", "void", "with", "withrevert", "xor",
})


def _cvl_safe_param_name(name: str) -> str:
    """The parameter name with a trailing "_" if it equals a CVL reserved word, otherwise unchanged."""
    return f"{name}_" if name in CVL_RESERVED_WORDS else name


try:
    from dotenv import load_dotenv

    # Load .env from current working directory
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        api_key = os.getenv(ANTHROPIC_API_KEY_ENV)
        if api_key:
            print(f"✓ Using {ANTHROPIC_API_KEY_ENV} from environment variables")
        else:
            print(f"ERROR: .env file not found in current directory: {Path.cwd()}")
            print(f"       and {ANTHROPIC_API_KEY_ENV} not found in environment variables")
            print("Please either:")
            print(f"  1. Create a .env file in the current directory with {ANTHROPIC_API_KEY_ENV}=...")
            print(f"  2. Export {ANTHROPIC_API_KEY_ENV} as an environment variable")
            sys.exit(1)
    else:
        loaded = load_dotenv(env_path)
        if not loaded:
            print(f"ERROR: Failed to load .env file from: {env_path}")
            sys.exit(1)

        print(f"✓ Loaded .env from {env_path}")
except ImportError:
    print("ERROR: python-dotenv is required. Install with: pip install python-dotenv")
    sys.exit(1)

from certora_autosetup.parsers.type_analyzer import TypeAnalyzer, TypeCategory

class CacheStats(TypedDict):
    hits: int
    misses: int

class MatchAnalysis(BaseModel):
    """
    Result of your analysis on whether a contract method matches the given criteria.
    """
    is_match: bool = Field(description="True if the method matches the described criteria, False otherwise")
    explanation: str = Field(description="A BRIEF (no more than one sentence) describing why the method does or does not match")

class PrimitiveType(BaseModel):
    ty: Literal["primitive"]
    ty_name: Literal[
        "uint8", "uint16", "uint24", "uint32", "uint40", "uint48", "uint56", "uint64",
        "uint72", "uint80", "uint88", "uint96", "uint104", "uint112", "uint120", "uint128",
        "uint136", "uint144", "uint152", "uint160", "uint168", "uint176", "uint184", "uint192",
        "uint200", "uint208", "uint216", "uint224", "uint232", "uint240", "uint248", "uint256",
        "int8", "int16", "int24", "int32", "int40", "int48", "int56", "int64",
        "int72", "int80", "int88", "int96", "int104", "int112", "int120", "int128",
        "int136", "int144", "int152", "int160", "int168", "int176", "int184", "int192",
        "int200", "int208", "int216", "int224", "int232", "int240", "int248", "int256",
        "bytes1", "bytes2", "bytes3", "bytes4", "bytes5", "bytes6", "bytes7", "bytes8",
        "bytes9", "bytes10", "bytes11", "bytes12", "bytes13", "bytes14", "bytes15", "bytes16",
        "bytes17", "bytes18", "bytes19", "bytes20", "bytes21", "bytes22", "bytes23", "bytes24",
        "bytes25", "bytes26", "bytes27", "bytes28", "bytes29", "bytes30", "bytes31", "bytes32",
        "bool", "address"
    ]

class QualifiedType(BaseModel):
    """
    A contract-qualified type like Contract.TypeName (for UDVTs, enums, etc.)
    """
    ty: Literal["qualified"]
    contract_name: str = Field(description="The contract that defines this type")
    type_name: str = Field(description="The name of the type within the contract")

class BuiltInArrayType(BaseModel):
    ty: Literal["primitive_array"]
    nm: Literal["bytes", "string"] = Field(description="The name of the built in array type")

class AggregateArrayType(BaseModel):
    ty: Literal["aggregate_array"]
    base: "SolidityType" = Field(description="The type of elements of the dynamic array")

class StaticArrayType(BaseModel):
    ty: Literal["static_array"]
    base: "SolidityType"  = Field(description="The type of elements of the static array")
    n_elems: int = Field(description="The number of elements in this static array")

ReferenceBase = Annotated[BuiltInArrayType | AggregateArrayType | StaticArrayType, Discriminator("ty")]

class ReferenceType(BaseModel):
    ty: Literal["ref"]
    value: ReferenceBase = Field(description="The reference type used as a parameter")
    location: Literal["calldata", "memory", "storage"] = Field(description="The location required")

SolidityType = Annotated[BuiltInArrayType | AggregateArrayType | PrimitiveType | QualifiedType | StaticArrayType, Discriminator("ty")]

ParameterType = Annotated[ReferenceType | PrimitiveType | QualifiedType, Discriminator("ty")]

def _pprint_solidity_type(t: SolidityType) -> str:
    match t.ty:
        case "primitive_array":
            return t.nm
        case "primitive":
            return t.ty_name
        case "qualified":
            return f"{t.contract_name}.{t.type_name}"
        case "aggregate_array":
            return _pprint_solidity_type(t.base) + "[]"
        case "static_array":
            return _pprint_solidity_type(t.base) + f"[{t.n_elems}]"

def _pprint_type(t: ParameterType) -> str:
    match t.ty:
        case "primitive":
            return t.ty_name
        case "qualified":
            return f"{t.contract_name}.{t.type_name}"
        case "ref":
            return _pprint_solidity_type(t.value)

class TypeAndName(BaseModel):
    """
    A formal parameter with a type and a name.
    """
    ty: ParameterType = Field(description="The type of the parameter.")
    name: str = Field(description="The name of the parameter")

class DecimalSummary(BaseModel):
    """
    Used to indicate that a method performs a decimal conversion and how to summarize it.
    """
    ty: Literal["decimal"]
    method_name: str = Field(description="The name of the analyzed method (without any parameters or return information)")
    param_list: list[TypeAndName] = Field(description="The method parameters for the summary")
    amount_parameter: str = Field(description="The name of the parameter in `param_list` that is being converted between decimals")
    return_type : PrimitiveType = Field(description="The return type of the analyzed function.")
    cvl_function_name: str = Field(description="The name of the function to use for the generated summary. Must be a valid, appropriately named CVL identifier.")

    explanation: str = Field(description="A BRIEF (no more than one sentence) justifying why this method is a decimal conversion")

    @property
    def summary_line(self) -> str:
        params = ", ".join([ f"{_pprint_type(p.ty)} {_cvl_safe_param_name(p.name)}" for p in self.param_list ])
        return f"function _.{self.method_name}({params}) internal => {self.cvl_function_name}({_cvl_safe_param_name(self.amount_parameter)}) expect {self.return_type.ty_name};"

    @property
    def cvl_function(self) -> str:
        param_ty : ParameterType | None = None
        for p in self.param_list:
            if p.name == self.amount_parameter:
                param_ty = p.ty
        if param_ty is None or not isinstance(param_ty, PrimitiveType):
            raise ValueError("Invalid analysis, amount param doesn't appear in list?")
        return f"function {self.cvl_function_name}({param_ty.ty_name} amount) returns {self.return_type.ty_name} {{ return amount; }}"

class InvalidSummary(BaseModel):
    """
    Used to indicate that the method is not a decimal conversion and why
    """
    ty: Literal["failed"]
    explanation: str = Field(description="A BREIF description of why the summary generation failed")

class DecimalAnalysisResult(BaseModel):
    """
    Communicate the result of the decimal conversion analysis.

    Only one of the result fields should be set.
    """
    res_failed: InvalidSummary | None = Field(description="The field used to communicate an unsuccesful result")
    res_success: DecimalSummary | None = Field(description="The field used to communicate a successful result")

class NondetSummary(BaseModel):
    """
    Used to generate a NONDET summary for a method.
    """
    ty: Literal["nondet"]
    contract_name: str = Field(description="The name of the contract containing the method")
    method_name: str = Field(description="The name of the method (without parameters or return info)")
    param_list: list[TypeAndName] = Field(description="The method parameters with their types")
    return_type: list[ParameterType] | None = Field(description="The return type(s) of the method, or None if void")
    visibility: Literal["internal", "external"] = Field(description="'internal' for internal/private/public methods, 'external' for external methods")
    explanation: str = Field(description="A BRIEF (one sentence) explanation of why this method should be summarized as NONDET")

    @property
    def summary_line(self) -> str:
        params = ", ".join([f"{_pprint_type(p.ty)} {_cvl_safe_param_name(p.name)}" for p in self.param_list])
        if self.return_type is not None:
            return_types = ", ".join([
                _pprint_type(ty) for ty in self.return_type
            ])
            return_str = f" returns ({return_types})"
        else:
            return_str = ""
        return f"function {self.contract_name}.{self.method_name}({params}) {self.visibility}{return_str} => NONDET;"

class NondetAnalysisResult(BaseModel):
    """
    Communicate the result of the NONDET summary analysis.

    Only one of the result fields should be set.
    """
    res_failed: InvalidSummary | None = Field(description="Set this if the method cannot be summarized (e.g., has struct parameters)")
    res_success: NondetSummary | None = Field(description="Set this if the method can be summarized as NONDET")

class RecipeType(str, Enum):
    PRICE_COMPUTATION = "price_computation"
    NEW_CONTRACT = "new_contract"
    DECIMAL_CONVERSION = "decimal_conversion"
    NONLINEAR_OPERATIONS = "nonlinear_operations"
    INLINE_ASSEMBLY = "inline_assembly"
    CUSTOM = "custom"


# ERC4626 asset<->share conversions are internal/view and read vault state
# (totalSupply/totalAssets), so they pass the DECIMAL_CONVERSION property filter and
# look like unit conversions to the LLM. They are exchange rates, not stateless decimal
# rescales: summarizing them as the identity `return amount` is unsound. Skip the
# standard selectors deterministically (they keep these names when inherited).
ERC4626_EXCHANGE_RATE_METHODS = frozenset(
    {
        "convertToShares",
        "convertToAssets",
        "_convertToShares",
        "_convertToAssets",
        "previewDeposit",
        "previewMint",
        "previewWithdraw",
        "previewRedeem",
    }
)


@dataclass
class Recipe:
    """Recipe for LLM-based function analysis."""

    recipe_type: RecipeType
    characteristic: str  # Description of function characteristic to look for
    properties: Dict[
        str, Any
    ]  # Required method properties (visibility, stateMutability, etc.)
    summary_type: str  # How to summarize matching methods (e.g., "NONDET")


class SummarySetup:
    def __init__(self, verbose: int = 0, inheritance_graph: InheritanceGraph | None = None):
        self.verbose = verbose
        self.inheritance_graph: InheritanceGraph = inheritance_graph or InheritanceGraph()
        self.script_dir = Path(__file__).parent
        # Bundled summaries folder is shipped at certora/specs/summaries/ in the package.
        self.summaries_dir = self.script_dir.parent / "certora" / SUMMARIES_SUBDIR
        self.certora_dir = Path.cwd() / "certora"
        # User-side output dir for generated summaries. Created once here so every
        # downstream method can assume it exists without re-mkdir-ing.
        self.user_summaries_dir = self.certora_dir / SUMMARIES_SUBDIR
        self.user_summaries_dir.mkdir(parents=True, exist_ok=True)

        # LLM log path
        self.llm_log_path = Path(".certora_internal/llm.log")

        # Configure logger
        logger.set_verbosity(verbose)
        self.component = "SummarySetup"

        # Set up LLM logger with rotation
        self.llm_logger = self._setup_llm_logger()

        # Load function signatures from JSON file
        self.function_summaries = self._load_function_summaries()

        # Pre-compute Solidity file sets to avoid repeated calls
        self.solidity_files_with_dependencies = self.find_all_solidity_files(
            include_dependencies=True
        )
        self.solidity_files_no_dependencies = self.find_all_solidity_files(
            include_dependencies=False
        )
        self.solidity_files_libraries = self.find_all_library_files(
            include_test_files=False, include_dependencies=False
        )

        # CVL helper functions emitted alongside DECIMAL_CONVERSION_IDENTITY summaries,
        # keyed by function name to dedupe across calls.
        self._cvl_functions: Dict[str, str] = {}

        # Per-summarized-contract method accumulator, populated by analyze_contract().
        # The (contractName, methodName) keys of every method in this dict serve as
        # the dedup set ("already emitted") across analyze_contract calls.
        self._methods_per_contract: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        # Curated function-summary keys matched for every contract that has entered the
        # scene (initial main + additional_contracts, plus any added by call resolution).
        # Populated by ``on_contracts_entered_scene``; consulted by autosetup's
        # ``_summarized_library_names`` to figure out which libraries have a curated
        # summary attached and therefore should be added to the scene.
        self.matched_functions: Set[str] = set()

        # Accumulated import lines for the base aggregator spec. Rewritten (sorted) by
        # ``_rewrite_aggregator`` after every batch of contracts entering the scene, so the
        # initial scope and call-resolution batches all funnel through one writer.
        self._aggregator_imports: Set[str] = set()

        # Run configuration captured by ``configure`` so ``on_contracts_entered_scene`` applies it
        # identically to the initial scene and to call-resolution batches.
        self.main_contract: str = ""
        self.additional_names: List[str] = []
        self._enable_llm: bool = False
        self._custom_recipe: Optional[str] = None
        self._llm_contract_files: List[str] = []

        # Initialize TypeAnalyzer for resolving user-defined types in function declarations
        self.type_analyzer: TypeAnalyzer = self._init_type_analyzer()

    @functools.cached_property
    def methods_parser(self) -> MethodParser:
        """Parsed ``all_methods.json``, loaded once and reused.

        ``all_methods.json`` is written once by the build (``generate_all_methods_json``) and is
        immutable afterward, so the parse is cached rather than repeated per match/recipe call.
        Access only after confirming the file exists.
        """
        return MethodParser(str(PATH_ALL_METHODS_JSON))

    def _init_type_analyzer(self) -> TypeAnalyzer:
        """Initialize TypeAnalyzer. Fatal error if initialization fails."""
        analyzer = TypeAnalyzer()
        if not analyzer.types_json.exists():
            raise FileNotFoundError(f"TypeAnalyzer: types JSON not found at {analyzer.types_json}")
        if not analyzer.parse_all():
            raise RuntimeError("TypeAnalyzer failed to parse type data")
        self.log("TypeAnalyzer initialized successfully", "DEBUG")
        return analyzer

    def log(self, message: str, level: str = "INFO"):
        """Log messages using centralized logger."""
        logger.log(message, level, self.component)

    @functools.cached_property
    def _udt_context(self) -> str:
        """LLM-prompt-formatted UDT context. Loaded lazily on first access and
        cached for the lifetime of this ``SummarySetup`` instance — UDT data
        comes from ``all_user_defined_types.json`` which doesn't change within
        a single run."""
        return self._format_udt_context_for_llm(self._load_user_defined_types())

    def _load_user_defined_types(self) -> dict[str, list[dict]]:
        """Load user-defined types from all_user_defined_types.json.

        Returns:
            Dict with keys 'udvts' and 'enums', each containing a list of type info dicts.
        """
        types_file = Path(".certora_internal/all_user_defined_types.json")
        result: dict[str, list[dict]] = {"udvts": [], "enums": []}

        if not types_file.exists():
            self.log("all_user_defined_types.json not found", "WARNING")
            return result

        try:
            with open(types_file, "r") as f:
                all_types = json.load(f)

            for type_info in all_types:
                category = type_info.get("typeCategory", "")
                if category == "UserDefinedValueType":
                    result["udvts"].append(type_info)
                elif category == "UserDefinedEnum":
                    result["enums"].append(type_info)

            return result
        except Exception as e:
            self.log(f"Error loading user-defined types: {e}", "WARNING")
            return result

    def _format_udt_context_for_llm(self, udt_data: dict[str, list[dict]]) -> str:
        """Format user-defined types into a simple list of qualified names for LLM prompts."""
        qualified_names = set()

        for t in udt_data["udvts"]:
            qname = t.get("qualifiedName", "")
            if qname:
                qualified_names.add(qname)

        for t in udt_data["enums"]:
            qname = t.get("qualifiedName", "")
            if qname:
                qualified_names.add(qname)

        if not qualified_names:
            return "No user-defined types found."

        lines = ["Available user-defined types for CVL:"]
        for name in sorted(qualified_names):
            lines.append(f"  {name}")

        return "\n".join(lines)

    def _setup_llm_logger(self) -> logging.Logger:
        """Set up a rotating file logger for LLM interactions."""
        llm_logger = logging.getLogger("llm_interactions")
        llm_logger.setLevel(logging.INFO)

        # Don't add handlers if they already exist (avoid duplicates)
        if not llm_logger.handlers:
            self.llm_log_path.parent.mkdir(parents=True, exist_ok=True)

            # Create rotating file handler (20MB max, keep 1 backup)
            handler = logging.handlers.RotatingFileHandler(
                filename=str(self.llm_log_path),
                maxBytes=20 * 1024 * 1024,  # 20MB
                backupCount=1,
                encoding="utf-8",
            )

            # Simple format - we'll format the messages ourselves
            formatter = logging.Formatter("%(message)s")
            handler.setFormatter(formatter)

            llm_logger.addHandler(handler)

        return llm_logger

    def _load_function_summaries(self) -> Dict[str, Dict[str, Any]]:
        """Load function summaries configuration from JSON file."""
        config_file = Path(__file__).parent / "function_summaries.json"

        try:
            with open(config_file, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Function summaries config file not found: {config_file}\n"
                f"Please ensure function_summaries.json exists in the setup/ directory."
            )
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Invalid JSON in function summaries config file: {config_file}\n"
                f"Error: {e}"
            )

    def parse_call_graph_dot_file(
        self, dot_file: str
    ) -> Tuple[Dict[str, Set[str]], Set[str]]:
        """
        Parse a call-graph DOT file and extract the graph structure.
        Call-graph files use quoted node names: "contract1" -> "contract2"

        Returns:
            Tuple of (adjacency_list, nodes) where:
            - adjacency_list is a dict mapping source nodes to sets of target nodes
            - nodes is a set of all nodes in the graph
        """
        adjacency_list: dict = defaultdict(set)
        nodes: set = set()

        if not os.path.exists(dot_file):
            self.log(f"Call-graph DOT file not found: {dot_file}", "WARNING")
            return adjacency_list, nodes

        try:
            with open(dot_file, "r") as f:
                content = f.read()

            # Find all edges (format: "node1" -> "node2")
            edge_pattern = r'"([^"]+)"\s*->\s*"([^"]+)"'
            for match in re.finditer(edge_pattern, content):
                source, target = match.groups()
                adjacency_list[source].add(target)
                nodes.add(source)
                nodes.add(target)

            # Also find standalone nodes (format: "node"[attributes])
            node_pattern = r'"([^"]+)"\[.*?\]'
            for match in re.finditer(node_pattern, content):
                node = match.group(1)
                nodes.add(node)

            self.log(
                f"Parsed call-graph DOT file: {len(nodes)} nodes, {sum(len(targets) for targets in adjacency_list.values())} edges"
            )

        except Exception as e:
            self.log(f"Error parsing call-graph DOT file {dot_file}: {e}", "WARNING")

        return adjacency_list, nodes

    def find_reachable_nodes(
        self, graph: Dict[str, Set[str]], start_nodes: Set[str]
    ) -> Set[str]:
        """Find all nodes reachable from the given start nodes using BFS."""
        reachable = set()
        queue = deque(start_nodes)

        while queue:
            node = queue.popleft()
            if node in reachable:
                continue
            reachable.add(node)

            # Add all neighbors to the queue
            if node in graph:
                for neighbor in graph[node]:
                    if neighbor not in reachable:
                        queue.append(neighbor)

        return reachable

    def extract_method_names(self, nodes: Set[str]) -> Set[str]:
        """Extract method names from node labels.

        DOT node labels often contain contract and method info like:
        - "Contract.method()"
        - "method()"
        - "Contract::method"
        """
        method_names = set()

        for node in nodes:
            # Remove parameters and clean up
            clean_node = re.sub(r"\([^)]*\)", "", node)

            # Handle Contract.method or Contract::method format
            if "." in clean_node or "::" in clean_node:
                parts = re.split(r"[.:]", clean_node)
                if parts:
                    method_name = parts[-1].strip()
                    if method_name:
                        method_names.add(method_name)
            else:
                # Just the method name (no prefix or separator)
                method_name = clean_node.strip()
                if method_name:
                    method_names.add(method_name)

        return method_names

    def analyze_call_graph(
        self, call_graph_file: str, target_functions: Set[str]
    ) -> Set[str]:
        """Analyze call graph to check which target functions are reachable.

        Args:
            call_graph_file: Path to the call graph DOT file
            target_functions: Set of function names to look for (e.g., {'toString', 'mulDiv'})

        Returns:
            Set of target functions that are reachable from root nodes
        """
        if not call_graph_file or not os.path.exists(call_graph_file):
            return set()

        self.log(f"Analyzing call graph: {call_graph_file}")
        graph, nodes = self.parse_call_graph_dot_file(call_graph_file)

        # Find root nodes (nodes with no incoming edges)
        all_targets = set()
        for targets in graph.values():
            all_targets.update(targets)
        root_nodes = nodes - all_targets

        if not root_nodes:
            # If no clear roots, consider all public/external functions as roots
            # Look for typical public function patterns
            root_nodes = {
                n
                for n in nodes
                if not n.startswith("_") and not n.startswith("internal")
            }

        self.log(f"Found {len(root_nodes)} root nodes in call graph")

        # Find all reachable nodes from roots
        reachable = self.find_reachable_nodes(graph, root_nodes)

        # Extract method names from reachable nodes
        reachable_methods = self.extract_method_names(reachable)

        # Check which target functions are reachable
        found_functions = target_functions & reachable_methods

        if found_functions:
            self.log(
                f"Found reachable target functions in call graph: {found_functions}",
                "SUCCESS",
            )

        return found_functions

    def find_all_solidity_files(
        self, include_test_files: bool = False, include_dependencies: bool = False
    ) -> List[str]:
        """Find all Solidity files in the current project.

        Args:
            include_test_files: Whether to include test (.t.sol) and script (.s.sol) files
            include_dependencies: Whether to include files in dependency directories
        """
        return util_find_all_solidity_files(
            include_test_files=include_test_files,
            include_dependencies=include_dependencies,
            verbose=self.verbose >= 1,
            log_func=self.log,
        )

    def find_all_library_files(
        self, include_test_files: bool = False, include_dependencies: bool = False
    ) -> List[str]:
        """Find all Solidity files that contain library definitions.

        Args:
            include_test_files: Whether to include test files
            include_dependencies: Whether to include dependency files

        Returns:
            List of file paths containing library definitions
        """
        return util_find_all_library_files(
            include_test_files=include_test_files,
            include_dependencies=include_dependencies,
            verbose=self.verbose >= 1,
            log_func=self.log,
        )

    def copy_summaries_folder(self, matched_function_keys: Iterable[str]) -> Path:
        """Copy only the bundled summary files referenced by matched curated keys
        into ``certora/specs/summaries/``.

        For each key in ``matched_function_keys`` we resolve the entry in
        ``function_summaries.json`` and copy its ``summary_file`` (a ``.spec`` or
        ``.template.spec``) plus any ``additional_contracts`` files (e.g. harness
        ``.sol`` files). Unmatched library specs aren't copied so the user's
        ``certora/specs/summaries/`` stays focused on what's actually used.
        """
        source_summaries = self.summaries_dir
        target_summaries = self.user_summaries_dir

        if not source_summaries.exists():
            self.log(
                f"Source summaries folder not found at {source_summaries}", "WARNING"
            )
            return target_summaries

        # Seed the closure with the matched summary_files and any additional_contracts.
        # Templates ARE included in the closure (so we follow their `import` lines to
        # the helpers they need, e.g. OZ_Math.template.spec → ../Math.spec) but they
        # are NOT copied to the user's dir — _materialize_template reads them
        # directly from the package.
        seed: Set[Path] = set()
        for key in matched_function_keys:
            info = self.function_summaries.get(key)
            if not info:
                continue
            seed.add(Path(info["summary_file"]).relative_to(SUMMARIES_SUBDIR))
            for ac in info.get("additional_contracts", []):
                seed.add(Path(ac).relative_to(SUMMARIES_SUBDIR))

        # Walk transitive imports per seed via parse_imports_from_spec(recursive=True),
        # then keep only entries that resolved inside the bundled tree.
        package_root = source_summaries.resolve()
        closure: Set[Path] = set(seed)
        for rel in seed:
            src = source_summaries / rel
            if not src.exists() or not src.is_file() or src.suffix != ".spec":
                continue
            for imported in parse_imports_from_spec(src, recursive=True):
                try:
                    closure.add(imported.resolve().relative_to(package_root))
                except ValueError:
                    pass  # imports outside the bundled tree aren't ours to copy

        copied = 0
        for rel in sorted(closure):
            # Templates are read from the package source by _materialize_template;
            # never copy them to the user's dir.
            if str(rel).endswith(".template.spec"):
                continue
            src = source_summaries / rel
            dst = target_summaries / rel
            if not src.exists():
                self.log(f"Bundled summary file missing: {src}", "WARNING")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            copied += 1

        self.log(f"Copied {copied} curated summary file(s) to {target_summaries}")
        return target_summaries

    def _find_rounding_enum_value(self, contract_name) -> Optional[str]:
        """Find the correct Rounding enum value from all_user_defined_types.json.

        Returns:
            Either "Up" or "Ceil" depending on what's found in the Rounding enum
        """
        try:
            user_types_file = Path(".certora_internal/all_user_defined_types.json")
            if not user_types_file.exists():
                self.log("Warning: all_user_defined_types.json not found", "WARNING")
                return None

            with open(user_types_file, "r") as f:
                user_types = json.load(f)

            # Find the Rounding enum
            for type_info in user_types:
                if (
                    type_info.get("typeCategory") == "UserDefinedEnum"
                    and type_info.get("typeName") == "Rounding"
                    and type_info.get("main_contract") == contract_name
                ):
                    enum_members = type_info.get("enumMembers", [])
                    self.log(f"Found Rounding enum with members: {enum_members}")

                    # Extract member names from the objects
                    member_names = []
                    for member in enum_members:
                        if isinstance(member, dict) and "name" in member:
                            member_names.append(member["name"])
                        elif isinstance(member, str):
                            member_names.append(member)

                    # Check for Up or Ceil
                    if "Ceil" in member_names:
                        return "Ceil"
                    elif "Up" in member_names:
                        return "Up"
                    else:
                        self.log(
                            f"Warning: Rounding enum found but contains neither 'Up' nor 'Ceil'. Members: {member_names}",
                            "WARNING",
                        )
                        return None  # Default fallback

            self.log("Warning: Rounding enum not found in user types", "WARNING")
            return None

        except Exception as e:
            raise Exception(f"Error reading user types file: {e}")

    def process_template_in_place(
        self,
        template_file: Path,
        template_result_file: Path,
        contract_name: str,
    ) -> None:
        """Substitute ``$CONTRACT_NAME$`` (and OZ_Math's rounding placeholders) in the
        bundled template at ``template_file`` and write the result to
        ``template_result_file``."""
        # Read template content
        template_content = template_file.read_text()

        # Replace placeholders
        processed_content = template_content.replace("$CONTRACT_NAME$", contract_name)

        # Special handling for OZ_Math.template.spec
        if template_file.name == "OZ_Math.template.spec":
            rounding_value = self._find_rounding_enum_value(contract_name)
            if rounding_value is None:
                processed_content = processed_content.replace(
                    "$COMMENT_IF_NO_ROUNDING$", "//"
                )
                processed_content = processed_content.replace(
                    "$COMMENT_BLOCK_START_IF_NO_ROUNDING$", "/*"
                )
                processed_content = processed_content.replace(
                    "$COMMENT_BLOCK_END_IF_NO_ROUNDING$", "*/"
                )
            else:
                processed_content = processed_content.replace(
                    "$UINT_ROUND_UP$", rounding_value
                )
                processed_content = processed_content.replace(
                    "$COMMENT_IF_NO_ROUNDING$", ""
                )
                processed_content = processed_content.replace(
                    "$COMMENT_BLOCK_START_IF_NO_ROUNDING$", ""
                )
                processed_content = processed_content.replace(
                    "$COMMENT_BLOCK_END_IF_NO_ROUNDING$", ""
                )
            self.log(
                f"Processed OZ_Math template with Rounding value: {rounding_value}"
            )

        # Create output file
        output_file = template_result_file
        output_file.write_text(processed_content)

        self.log(
            f"Processed template {template_file.name} with contract name: {contract_name}"
        )

    @staticmethod
    def _versioned_template_relpath(rel_under_summaries: Path, main_contract: str) -> Path:
        """Summaries-dir-relative path of a curated summary, mapping a ``.template.spec`` to its
        ``{base}-{main_contract}.spec`` name and returning any other path unchanged."""
        stem = rel_under_summaries.stem
        if not stem.endswith(".template"):
            return rel_under_summaries
        return rel_under_summaries.parent / f"{stem[: -len('.template')]}-{main_contract}.spec"

    def _materialize_template(self, template_path: str, main_contract: str) -> str:
        """Substitute ``$CONTRACT_NAME$`` in a bundled template and write the versioned
        spec into the user's ``certora/specs/summaries/`` tree.

        Templates are read directly from the package source (``self.summaries_dir``)
        and never copied to the user's project — only the substituted output lands
        there, named ``{base}-{main_contract}.spec``. The summary aggregator imports this
        versioned path directly.

        Args:
            template_path: Project-relative template path as recorded in
                ``function_summaries.json`` (e.g.
                ``"specs/summaries/OpenZeppelin/OZ_Math.template.spec"``).
            main_contract: Substituted in for ``$CONTRACT_NAME$`` (and used to detect
                the rounding-enum value for OZ_Math's special-case fields).

        Returns:
            The project-relative path of the written versioned spec (e.g.
            ``"specs/summaries/OpenZeppelin/OZ_Math-MyContract.spec"``).
        """
        rel_under_summaries = Path(template_path).relative_to(SUMMARIES_SUBDIR)
        src = self.summaries_dir / rel_under_summaries

        versioned_rel_under = self._versioned_template_relpath(rel_under_summaries, main_contract)

        dst = self.user_summaries_dir / versioned_rel_under
        dst.parent.mkdir(parents=True, exist_ok=True)

        self.process_template_in_place(src, dst, main_contract)

        return str(SUMMARIES_SUBDIR / versioned_rel_under)

    def curated_summary_import_path(self, func_name: str, main_contract: str) -> str:
        """Aggregator-relative import path for a matched curated-summary key.

        A ``.template.spec`` entry is materialized into its ``{base}-{main_contract}.spec`` form first.
        """
        import_path: str = self.function_summaries[func_name]["summary_file"]
        if import_path.endswith(".template.spec"):
            import_path = self._materialize_template(import_path, main_contract)
        return os.path.relpath(self.certora_dir / import_path, self.user_summaries_dir)

    def _add_aggregator_imports(self, imports: Iterable[str]) -> List[str]:
        """Record aggregator-relative import paths for the base summary aggregator.

        Paths are accumulated (not written) here; ``_rewrite_aggregator`` re-emits the
        file sorted from the full set. Returns the paths that were newly added (for
        logging); already-present paths are ignored so repeated registration is idempotent.
        """
        added = [imp for imp in imports if imp not in self._aggregator_imports]
        self._aggregator_imports.update(added)
        return added

    def _rewrite_aggregator(self, main_contract: str) -> Path:
        """(Re-)write the base summary aggregator at
        ``certora/specs/summaries/{main_contract}_base_summaries.spec`` from the
        accumulated import set.

        The aggregator imports one line per accumulated path: curated bundled specs
        (e.g. ``"OpenZeppelin/OZ_Math.spec"``, possibly a template materialized to a
        ``{base}-{main}.spec`` versioned name) and per-contract LLM specs
        (``"{C}_summaries.spec"``). All paths are relative to the aggregator's directory
        and emitted sorted, so the file is deterministic regardless of the order in which
        contracts entered the scene.
        """
        aggregator_path = self.user_summaries_dir / f"{main_contract}_base_summaries.spec"
        lines = [
            f"// Auto-generated base summaries for {main_contract}",
            "// Generated by setup_summaries.py",
            "",
        ]
        if self._aggregator_imports:
            for imp in sorted(self._aggregator_imports):
                lines.append(f'import "{imp}";')
        else:
            lines.append(f"// No summaries needed for {main_contract}")

        aggregator_path.write_text("\n".join(lines) + "\n")
        self.log(
            f"Wrote {aggregator_path} with {len(self._aggregator_imports)} import(s)",
            "SUCCESS" if self._aggregator_imports else "INFO",
        )
        return aggregator_path

    def prune_emitted_specs(self, main_contract: str) -> None:
        """Comment out methods{} entries that don't resolve in the compiled scene, across ALL emitted
        summary specs. Curated/dedicated library specs (in function_summaries.json key order) take
        precedence over LLM-generated ones for duplicate (receiver, name, param_types) ownership.

        Global by design — it needs every spec at once for that precedence — so it runs after summary
        emission and before a prover submission, not inline per contract. Union templates and curated
        specs added lazily during call resolution otherwise leave entries that fail CVL typechecking.

        Each spec path is passed to the resolver at most once: several function_summaries keys can
        share one summary_file (e.g. the SafeERC20 keys), and resolving the same file twice makes the
        second pass treat the file's own kept entries as duplicates owned by the first and drop them.
        """
        if not PATH_ALL_METHODS_JSON.exists():
            return
        ordered_specs: List[Path] = []
        seen: Set[Path] = set()

        def _add(p: Path) -> None:
            if p not in seen:
                seen.add(p)
                ordered_specs.append(p)

        for key in self.function_summaries:  # function_summaries.json key order = curated precedence
            if key not in self.matched_functions:
                continue
            rel_under = Path(self.function_summaries[key]["summary_file"]).relative_to(SUMMARIES_SUBDIR)
            rel_under = self._versioned_template_relpath(rel_under, main_contract)
            _add(self.user_summaries_dir / rel_under)
        # Then any other emitted spec (LLM per-contract, call resolution) — lower dedup precedence.
        for spec in walk_files_by_suffix(self.user_summaries_dir, ".spec"):
            _add(spec)
        resolve_summary_specs(ordered_specs, PATH_ALL_METHODS_JSON, log=self.log)

    def should_process_file(self, file_path: str) -> bool:
        """Check if a file should be processed (not a dependency or internal file).

        Uses the same logic as other functionalities for consistency.
        """
        # Convert to Path for easier manipulation
        path = Path(file_path)
        path_str = str(path)

        # Skip .certora_internal directory
        if ".certora_internal" in path.parts:
            return False

        # Skip dependency directories
        if any(pattern in path_str for pattern in DEPENDENCIES):
            return False

        # Skip test and script files if not explicitly included
        if path.name.endswith(".t.sol") or path.name.endswith(".s.sol"):
            return False

        return True

    def _make_decimal_summary_call(
        self,
        prompt: str,
        model: str,
        max_tokens: int,
        max_retries: int = 10,
        log_to_file: bool = False,
        log_path: Path | None = None,
    ) -> Optional[DecimalAnalysisResult]:
        """Make an API call with exponential backoff retry for rate limits, with custom model and token settings."""
        # Convert Path to string if provided
        log_path_str = str(log_path) if log_path else None

        return call_llm_structured(
            prompt=prompt,
            ty=DecimalAnalysisResult,
            model=model,
            max_tokens=max_tokens,
            temperature=0.0,
            max_retries=max_retries,
            log_to_file=log_to_file,
            log_path=log_path_str,
            verbose=False,
        )

    async def _make_match_analysis_call(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        max_retries: int = 10,
        log_to_file: bool = False,
        log_path: Path | None = None,
    ) -> Optional[MatchAnalysis]:
        """Make an API call with exponential backoff retry for rate limits, with custom model and token settings."""
        # Convert Path to string if provided
        log_path_str = str(log_path) if log_path else None

        return await call_llm_async_structured_cached(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            ty=MatchAnalysis,
            model=model,
            max_tokens=max_tokens,
            temperature=0.0,
            max_retries=max_retries,
            log_to_file=log_to_file,
            log_path=log_path_str,
            verbose=False,
        )

    def _make_nondet_summary_call(
        self,
        prompt: str,
        model: str,
        max_tokens: int,
        max_retries: int = 10,
        log_to_file: bool = False,
        log_path: Path | None = None,
    ) -> Optional[NondetAnalysisResult]:
        """Make an API call for NONDET summary generation."""
        log_path_str = str(log_path) if log_path else None

        return call_llm_structured(
            prompt=prompt,
            ty=NondetAnalysisResult,
            model=model,
            max_tokens=max_tokens,
            temperature=0.0,
            max_retries=max_retries,
            log_to_file=log_to_file,
            log_path=log_path_str,
            verbose=False,
        )

    def _generate_nondet_summary(
        self,
        method: Dict[str, Any],
        udt_context: str,
    ) -> Optional[str]:
        """Generate a NONDET summary for a method using LLM.

        Args:
            method: Method info dict from all_methods.json
            udt_context: Formatted string listing available user-defined types

        Returns:
            The summary line string, or None if generation failed
        """
        contract_name = method["contractName"]
        method_name = method["name"]
        visibility = method["visibility"]

        # Find the contract file
        ch = self.inheritance_graph.find_handle_by_name(contract_name=contract_name)
        contract_file : str | None
        if ch is None or (contract_file := ch.source_file) is None:
            self.log(f"Could not find source file for {contract_name}", "WARNING")
            return None

        try:
            with open(contract_file, "r") as f:
                contract_code = f.read()
        except Exception as e:
            self.log(f"Could not read {contract_file}: {e}", "WARNING")
            return None

        # Extract the function signature area
        pattern = rf"function\s+{re.escape(method_name)}\s*\([^)]*\)[^{{]*"
        match = re.search(pattern, contract_code)

        if not match:
            self.log(f"Could not find function {method_name} in {contract_file}", "WARNING")
            return None

        function_sig = match.group(0)

        # CVL visibility
        cvl_visibility = "internal" if visibility in ["public", "private", "internal"] else "external"

        prompt = f"""Generate a CVL NONDET summary for this Solidity function.

{udt_context}

Contract: {contract_name}
Function signature from source:
```solidity
{function_sig}
```

Generate a NondetSummary with:
- contract_name: "{contract_name}"
- method_name: "{method_name}"
- param_list: the parameters with their CVL types
- return_type: the return type(s) (or null if void)
- visibility: "{cvl_visibility}"

For any user-defined types in the parameters or return type, use the qualified name from the list above.
If the function cannot be summarized (e.g., struct parameters), return an InvalidSummary.

"""

        method_info = f"{contract_name}.{method_name}"

        # Check cache first
        cached = self._load_from_cache(prompt, default_anthropic_model(), method_info)
        if cached is not None:
            try:
                response = NondetAnalysisResult.model_validate_json(cached)
                if response.res_success:
                    return response.res_success.summary_line
            except Exception:
                pass  # Cache invalid, will regenerate

        try:
            response = self._make_nondet_summary_call(
                prompt,
                default_anthropic_model(),
                max_tokens=1500,
                log_to_file=True,
                log_path=self.llm_log_path,
            )
        except Exception as e:
            self.log(f"LLM call failed for {method_info}: {e}", "WARNING")
            return None

        if not response:
            return None

        # Save to cache
        self._save_to_cache(prompt, default_anthropic_model(), response.model_dump_json())

        if response.res_failed:
            self.log(
                f"Cannot summarize {method_info}: {response.res_failed.explanation}",
                "WARNING"
            )
            return None

        result = response.res_success
        if not result:
            self.log(f"No result for {method_info}", "WARNING")
            return None

        return result.summary_line

    def _generate_function_declaration(self, method: Dict[str, Any], is_wildcard: bool) -> str:
        """Generate a CVL function declaration from structured method data.

        Uses parameter types, names, locations, and return types from the method dict
        (populated from Certora build data via all_methods.json).

        Returns:
            CVL function declaration string.
        """
        contract_name = method["contractName"] if not is_wildcard else "_"
        method_name = method["name"]

        # Map Solidity visibility to CVL visibility
        solidity_visibility = method["visibility"]
        cvl_visibility = "internal" if solidity_visibility in ("public", "private", "internal") else "external"

        def classify_solidity_type(sol_type: str) -> tuple[str, str]:
            """
            Classify a Solidity type and return the CVL type.
            Returns (cvl_type, classification) where classification is 'primitive', 'contract', 'struct', 'enum', or 'unknown'.
            """
            type_info = self.type_analyzer._resolve_type_from_string(sol_type)
            match type_info.category:
                case TypeCategory.PRIMITIVE:
                    return type_info.cvl_name, "primitive"
                case TypeCategory.CONTRACT:
                    return "address", "contract"
                case TypeCategory.BYTES:
                    return "bytes", "primitive"
                case TypeCategory.STRING:
                    return "string", "primitive"
                case TypeCategory.ARRAY:
                    return type_info.cvl_name, "primitive"
                case TypeCategory.STRUCT:
                    return type_info.cvl_name, "struct"
                case TypeCategory.ENUM:
                    return type_info.cvl_name, "enum"
                case _:
                    return sol_type, "unknown"

        def is_array_type(sol_type: str) -> bool:
            """Check if a type is any kind of array (dynamic, static, or nested)."""
            return bool(re.search(r"\[\d*\]", sol_type))

        # Extract structured data from the method dict
        param_types = method.get("fullSignature", [])
        param_names = method.get("paramNames", [])
        locations = method.get("location", [])
        return_types = method.get("returns", [])
        return_locations = method.get("returnLocations", [])

        # Build CVL parameter list
        params = []
        for i, param_type in enumerate(param_types):
            param_name = param_names[i] if i < len(param_names) and param_names[i] else f""
            param_name = _cvl_safe_param_name(param_name)
            location = locations[i] if i < len(locations) else ""

            cvl_type, classification = classify_solidity_type(param_type)

            if classification == "primitive":
                # For internal functions, preserve memory/calldata location for reference types
                needs_location = cvl_type in ["bytes", "string"] or is_array_type(cvl_type)
                if cvl_visibility == "internal" and needs_location and location in ("memory", "calldata"):
                    params.append(f"{cvl_type} {location} {param_name}")
                else:
                    params.append(f"{cvl_type} {param_name}")
            elif classification == "contract":
                params.append(f"{cvl_type} {param_name}")
            elif classification == "struct":
                # Structs are reference types - need location for internal functions
                if cvl_visibility == "internal" and location in ("memory", "calldata"):
                    params.append(f"{cvl_type} {location} {param_name}")
                else:
                    params.append(f"{cvl_type} {param_name}")
            elif classification == "enum":
                params.append(f"{cvl_type} {param_name}")
            else:
                params.append(f"... /* {param_type} {param_name} - needs manual fix */")

        # Build CVL return type list
        returns = []
        for i, ret_type in enumerate(return_types):
            cvl_type, classification = classify_solidity_type(ret_type)
            ret_location = return_locations[i] if i < len(return_locations) else ""

            if classification in ("primitive", "contract", "struct", "enum"):
                # For internal functions, preserve memory/calldata location for reference types
                needs_location = cvl_type in ["bytes", "string"] or is_array_type(cvl_type) or classification == "struct"
                if cvl_visibility == "internal" and needs_location and ret_location in ("memory", "calldata"):
                    returns.append(f"{cvl_type} {ret_location}")
                else:
                    returns.append(cvl_type)
            else:
                returns.append(f"... /* {ret_type} - needs manual fix */")

        param_list = ", ".join(params) if params else ""

        # Build the declaration — wildcard declarations must not include a `returns` clause
        if returns and not is_wildcard:
            return_list = ", ".join(returns)
            declaration = f"function {contract_name}.{method_name}({param_list}) {cvl_visibility} returns ({return_list})"
        else:
            declaration = f"function {contract_name}.{method_name}({param_list}) {cvl_visibility}"

        return declaration

    def _get_cache_key(self, prompt: str, model: str) -> str:
        """Generate a cache key from prompt and model."""
        import hashlib

        # Use hash to create a consistent key from prompt + model
        content = f"{model}::{prompt}"
        return hashlib.sha256(content.encode()).hexdigest()

    def _load_from_cache(
        self, prompt: str, model: str, method_info: str | None = None
    ) -> Optional[str]:
        """Load cached response if available."""
        fs = get_fs()
        cache_dir = cache_path(DIR_CERTORA_INTERNAL, DIR_LLM_CACHE)
        if not fs.exists(cache_dir):
            return None

        cache_key = self._get_cache_key(prompt, model)
        cache_file = cache_dir + f"/{cache_key}.json"

        if fs.exists(cache_file):
            try:
                with fs.open(cache_file, "r") as f:
                    cache_data = json.load(f)
                    if cache_data.get("model") == model:
                        if method_info:
                            self.log(
                                f"Cache hit for {method_info} using {model}", "DEBUG"
                            )
                        else:
                            self.log(f"Cache hit for {model} query", "DEBUG")
                        return cache_data.get("response")
            except Exception as e:
                raise Exception(f"Error reading cache: {e}")

        return None

    def _save_to_cache(self, prompt: str, model: str, response: str):
        """Save response to cache."""
        fs = get_fs()
        cache_dir = cache_path(DIR_CERTORA_INTERNAL, DIR_LLM_CACHE)
        fs.mkdirs(cache_dir, exist_ok=True)

        cache_key = self._get_cache_key(prompt, model)
        cache_file = cache_dir + f"/{cache_key}.json"

        try:
            cache_data = {
                "model": model,
                "prompt": prompt[:500],  # Store first 500 chars for debugging
                "response": response,
                "timestamp": datetime.now().isoformat(),
            }
            with fs.open(cache_file, "w") as f:
                json.dump(cache_data, f, indent=2)
        except Exception as e:
            self.log(f"Error saving to cache: {e}", "DEBUG")

    async def _call_llm_model(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        log_path: Path,
        model_name: str,
        cache_stats: CacheStats | None = None,
        method_info: str | None = None,
    ) -> MatchAnalysis:
        """Call a specific LLM model and return parsed result, using cache when available."""
        # Check cache first (key is derived from the full prompt content)
        prompt = system_prompt + user_prompt
        cached_answer = self._load_from_cache(prompt, model, method_info)

        answer: MatchAnalysis | None = None
        try:
            if cached_answer is not None:
                answer = MatchAnalysis.model_validate_json(cached_answer)
                if cache_stats is not None:
                    cache_stats["hits"] += 1
                with open(log_path, "a") as f:
                    f.write(f"{model_name} Response (cached): {cached_answer}\n")
        except ValidationError:
            pass

        if answer is None:
            # Make API call
            if cache_stats is not None:
                cache_stats["misses"] += 1
            try:
                # Use the existing retry mechanism for rate limits
                answer = await self._make_match_analysis_call(
                    system_prompt, user_prompt, model, max_tokens, log_to_file=True, log_path=log_path
                )

                if answer is None:
                    # Rate limit or other error exceeded max retries
                    self.log(f"{model_name} analysis failed after retries", "WARNING")
                    return MatchAnalysis(is_match= False, explanation="")

                # Save to cache
                self._save_to_cache(prompt, model, answer.model_dump_json())

                with open(log_path, "a") as f:
                    f.write(f"{model_name} Response: {answer}\n")

            except Exception as e:
                self.log(f"{model_name} analysis failed: {e}", "WARNING")
                with open(log_path, "a") as f:
                    f.write(f"{model_name} Error: {e}\n")
                return MatchAnalysis(is_match =False, explanation="")

        # Parse the answer (whether from cache or API)
        return answer

    def _get_method_signature(self, method: Dict[str, Any], contract_code: str) -> str:
        """Extract the full method signature from contract code."""
        method_name = method["name"]
        expected_param_count = method.get("paramCount", 0)

        # Try to find the method signature in the contract code
        import re

        # Pattern to match function declaration with parameters
        pattern = rf"function\s+{re.escape(method_name)}\s*\(([^)]*)\)"

        # Find all matches for potential overloaded functions
        matches = list(re.finditer(pattern, contract_code))

        # If we have multiple matches, find the one with matching parameter count
        for match in matches:
            params = match.group(1).strip()

            # Count parameters in this match
            if params:
                # Split by comma but be careful with nested parentheses (for tuples, arrays, etc.)
                param_count = 1  # At least one parameter if params is not empty
                paren_depth = 0
                for char in params:
                    if char == "(" or char == "[":
                        paren_depth += 1
                    elif char == ")" or char == "]":
                        paren_depth -= 1
                    elif char == "," and paren_depth == 0:
                        param_count += 1
            else:
                param_count = 0

            # Check if this match has the expected parameter count
            if param_count != expected_param_count:
                continue  # Try next match

            # Clean up the parameters (remove variable names, keep only types)
            if params:
                # Simple cleanup - this could be more sophisticated
                param_parts = []
                for param in params.split(","):
                    param = param.strip()
                    # Take just the type part (first word(s) before variable name)
                    words = param.split()
                    if words:
                        # Handle types like "uint256", "address payable", etc.
                        if len(words) > 1 and words[1] in [
                            "memory",
                            "storage",
                            "calldata",
                            "payable",
                        ]:
                            param_parts.append(f"{words[0]} {words[1]}")
                        else:
                            param_parts.append(words[0])
                return f"{method_name}({', '.join(param_parts)})"
            else:
                return f"{method_name}()"

        # Fallback to just the method name if no matching signature found
        return method_name

    def _create_system_prompt(
        self,
        contract_code: str,
        contract_file: str,
        recipe: Recipe,
    ) -> str:
        """Create system prompt containing contract code and recipe framing. Shared across all methods in the same contract+recipe."""
        if recipe.recipe_type == RecipeType.NEW_CONTRACT:
            return f"""You are analyzing a Solidity contract to determine if specific methods create new contracts.

Contract file: {contract_file}

Contract code:
```solidity
{contract_code}
```

Look specifically for:
1. Use of the 'new' keyword followed by a contract name
2. Contract instantiation patterns
3. Factory pattern implementations

For each method you are asked about, set is_match to true if the method creates new contracts, false otherwise. Provide a single-sentence explanation."""
        else:
            return f"""You are analyzing a Solidity contract to determine if specific methods {recipe.characteristic}.

Contract file: {contract_file}

Contract code:
```solidity
{contract_code}
```

For each method you are asked about, set is_match to true if the method {recipe.characteristic}, false otherwise. Provide a single-sentence explanation."""

    def _create_user_prompt(
        self,
        method: Dict[str, Any],
        contract_code: str,
        recipe: Recipe,
    ) -> str:
        """Create user prompt containing method-specific details. Varies per method."""
        method_signature = self._get_method_signature(method, contract_code)

        if recipe.recipe_type == RecipeType.NEW_CONTRACT:
            return f"""Analyze the method '{method_signature}' and determine if it creates new contracts using the 'new' keyword.

Method signature: {method_signature}
- Visibility: {method["visibility"]}
- State Mutability: {method["stateMutability"]}"""
        else:
            return f"""Analyze the method '{method_signature}' and determine if it {recipe.characteristic}.

Method signature: {method_signature}
- Visibility: {method["visibility"]}
- State Mutability: {method["stateMutability"]}"""

    async def _analyze_method_with_llm(
        self,
        method: dict,
        contract_code: str,
        recipe: Recipe,
        cache_stats: CacheStats,
        llm_log_path: Path,
        method_full_name: str,
        system_prompt: str,
    ) -> Optional[dict]:
        user_prompt = self._create_user_prompt(method, contract_code, recipe)

        # Local LLM: single pass (both stages would hit the same model)
        if is_local_backend():
            result = await self._call_llm_model(
                default_anthropic_model(),
                system_prompt,
                user_prompt,
                500,
                llm_log_path,
                "Local",
                cache_stats,
                method_full_name,
            )
            if result.is_match:
                method_with_summary = method.copy()
                method_with_summary["_summary_type"] = recipe.summary_type
                method_with_summary["_recipe_characteristic"] = recipe.characteristic
                method_with_summary["_ai_explanation"] = result.explanation
                self.log(f"✓ Local LLM match: {method_full_name}")
                with open(self.llm_log_path, "a") as f:
                    f.write(f"Final Decision: MATCH (local LLM)\n")
                    f.write(f"Explanation: {result.explanation}\n\n")
                return method_with_summary
            else:
                with open(self.llm_log_path, "a") as f:
                    f.write("Final Decision: NO MATCH (local LLM)\n\n")
            return None

        # Stage 1: Fast filtering with Haiku
        haiku_result = await self._call_llm_model(
            "claude-haiku-4-5-20251001",
            system_prompt,
            user_prompt,
            200,
            llm_log_path,
            "Haiku",
            cache_stats,
            method_full_name,
        )

        if haiku_result.is_match:
            self.log(
                f"Haiku: {method_full_name} potentially matches - checking with Sonnet"
            )

            # Stage 2: Confirmation with Sonnet
            sonnet_result = await self._call_llm_model(
                default_anthropic_model(),
                system_prompt,
                user_prompt,
                500,
                llm_log_path,
                "Sonnet",
                cache_stats,
                method_full_name,
            )

            if sonnet_result.is_match:
                # Store method with Sonnet's explanation (more detailed and accurate)
                method_with_summary = method.copy()
                method_with_summary["_summary_type"] = recipe.summary_type
                method_with_summary["_recipe_characteristic"] = (
                    recipe.characteristic
                )
                method_with_summary["_ai_explanation"] = sonnet_result.explanation
                self.log(
                    f"✓ Both LLMs agree: {method_full_name} matches criteria"
                )

                with open(self.llm_log_path, "a") as f:
                    f.write("Final Decision: MATCH (both models agree)\n")
                    f.write(
                        f"Using Sonnet's explanation: {sonnet_result.explanation}\n\n"
                    )
                return method_with_summary

            else:
                self.log(
                    f"✗ Disagreement: Haiku said yes, Sonnet said no for {method_full_name}"
                )
                with open(self.llm_log_path, "a") as f:
                    f.write("Final Decision: NO MATCH (Sonnet disagreed)\n\n")
        else:
            with open(self.llm_log_path, "a") as f:
                f.write("Haiku Decision: NO MATCH (skipping Sonnet)\n\n")
        return None


    async def _analyze_method_with_llm_gated(
        self,
        method: dict,
        contract_code: str,
        recipe: Recipe,
        cache_stats: CacheStats,
        llm_log_path: Path,
        method_full_name: str,
        system_prompt: str,
        processed: asyncio.Queue[None | Literal[True]],
        sem: asyncio.Semaphore,
    ) -> Optional[dict]:
        async with sem:
            l = await self._analyze_method_with_llm(
                method, contract_code, recipe, cache_stats, llm_log_path, method_full_name,
                system_prompt,
            )
            await processed.put(None)
            return l

    async def analyze_with_llm(
        self,
        recipe: Recipe,
        contract_files: Set[str],
        methods_to_skip: Set[Tuple[str, str]] | None = None,
        main_contract: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Analyze methods using LLM based on a recipe.

        Args:
            recipe: Recipe containing characteristic, properties, and summary_type
            contract_files: Set of Solidity files to analyze
            methods_to_skip: Set of (contract, method) pairs to skip (already marked for
                summarization by non-LLM step or matched by previous LLM recipes)
            main_contract: Optional main contract name to filter methods by originatingContract

        Returns:
            List of methods that match the criteria, excluding methods in methods_to_skip
        """
        if methods_to_skip is None:
            methods_to_skip = set()

        # Load method information
        methods_file = PATH_ALL_METHODS_JSON
        if not methods_file.exists():
            self.log(
                "all_methods.json not found. Run orchestrator with compilation analysis first.",
                "ERROR",
            )
            return []

        mp = self.methods_parser

        # Filter methods by properties and originatingContract
        all_methods = mp.get_all_methods()
        filtered_methods = []

        for method in all_methods:
            # Filter by originating contract if main_contract is specified
            if main_contract and method.get("originatingContract") != main_contract:
                continue
            method_key = (method["contractName"], method["name"])

            # Check if method matches required properties
            matches = True
            for prop, value in recipe.properties.items():
                if prop in method:
                    # Handle both single values and lists of values
                    if isinstance(value, list):
                        if method[prop] not in value:
                            matches = False
                            break
                    else:
                        if method[prop] != value:
                            matches = False
                            break

            if matches:
                # Skip methods with storage location parameters (internal-only, can't generate valid summaries)
                if any(loc == "storage" for loc in method.get("location", [])):
                    continue

                # Skip known ERC4626 exchange-rate methods for the decimal-conversion recipe:
                # they read vault state, so the identity summary would be unsound (see constant).
                if (
                    recipe.recipe_type == RecipeType.DECIMAL_CONVERSION
                    and method["name"] in ERC4626_EXCHANGE_RATE_METHODS
                ):
                    self.log(
                        f"Skipping ERC4626 exchange-rate method (not a decimal conversion): {method['name']}"
                    )
                    continue

                # Skip if this method was already matched by a previous recipe
                if method_key not in methods_to_skip:
                    filtered_methods.append(method)

        if not filtered_methods:
            self.log(f"No methods found matching properties: {recipe.properties}")
            return []

        self.log(
            f"Found {len(filtered_methods)} methods matching property filters (after duplicate filtering)"
        )

        self.log(f"Will send {len(filtered_methods)} methods to LLM for analysis")

        # Log session start to .certora_internal/llm.log
        llm_log_path = Path(".certora_internal/llm.log")
        llm_log_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.llm_log_path, "a") as f:
            f.write(f"\n{'=' * 80}\n")
            f.write(f"LLM Analysis Session: {datetime.now().isoformat()}\n")
            f.write(f"Recipe: {recipe.characteristic}\n")
            f.write(f"Properties: {recipe.properties}\n")
            f.write(f"{'=' * 80}\n\n")

        processed_count = 0
        cache_stats : CacheStats = {"hits": 0, "misses": 0}

        # Combine contract files and library files for processing
        all_files_to_process = set(contract_files) | set(self.solidity_files_libraries)

        # Process each contract/library file that contains filtered methods

        sem = asyncio.Semaphore(2)

        q : asyncio.Queue[None | Literal[True]] = asyncio.Queue()

        async def monitor_loop():
            processed = 0
            while True:
                d = await q.get()
                if d is None:
                    processed+=1
                    if processed % 50 == 0:
                        self.log(
                            f"LLM Progress: Processed {processed_count} methods so far..."
                        )
                else:
                    # other option is the poison pill `True`, we're done
                    return

        tasks : list[Coroutine[Any, Any, dict | None]] = []

        async def task_runner() -> list[dict | None]:
            try:
                return await asyncio.gather(*tasks)
            finally:
                # kill the queue
                await q.put(True)

        for contract_file in all_files_to_process:
            # Use consistent filtering logic
            if not self.should_process_file(contract_file):
                continue

            # Check if this file has any of our filtered methods
            contract_name = Path(contract_file).stem
            file_methods = [
                m for m in filtered_methods if m["contractName"] == contract_name
            ]

            if not file_methods:
                continue

            # Read the contract file
            try:
                with open(contract_file, "r") as f:
                    contract_code = f.read()
            except Exception as e:
                raise Exception(f"Error reading {contract_file}: {e}")

            # Compute system prompt once per contract file (shared across all methods)
            system_prompt = self._create_system_prompt(contract_code, contract_file, recipe)

            # Analyze each method with two-stage LLM approach
            for method in file_methods:
                # Get method signature for better logging
                method_signature = self._get_method_signature(method, contract_code)
                method_full_name = f"{contract_name}.{method_signature}"

                task = self._analyze_method_with_llm_gated(
                    method=method,
                    cache_stats=cache_stats,
                    contract_code=contract_code,
                    llm_log_path=llm_log_path,
                    method_full_name=method_full_name,
                    recipe=recipe,
                    system_prompt=system_prompt,
                    processed=q,
                    sem=sem,
                )
                tasks.append(task)

        l, _ = await asyncio.gather(
            task_runner(),
            monitor_loop(),
        )

        matching_methods = [ m for m in l if m is not None]
        cache_hits = cache_stats["hits"]
        cache_misses = cache_stats["misses"]

        # Log summary and final progress
        if processed_count > 0:
            total_calls = cache_hits + cache_misses
            hit_rate = (cache_hits / total_calls * 100) if total_calls > 0 else 0
            self.log(
                f"LLM Analysis Complete: Processed {processed_count} methods, found {len(matching_methods)} matches"
            )
            self.log(
                f"Cache statistics: {cache_hits} hits, {cache_misses} misses ({hit_rate:.1f}% hit rate)"
            )

        with open(self.llm_log_path, "a") as f:
            f.write(f"Total processed: {processed_count}\n")
            f.write(f"Total matches: {len(matching_methods)}\n")
            f.write(f"{'=' * 80}\n")

        return matching_methods

    def _get_function_signature(self, method: Dict[str, Any], is_wildcard: bool) -> str:
        """Get function signature for a method."""
        return self._generate_function_declaration(method, is_wildcard)

    def _generate_decimal_conversion_summary(
        self, method: Dict[str, Any], contract_files: List[str]
    ) -> tuple[str, str] | None:
        """Use LLM to generate decimal conversion summary and CVL function."""
        try:
            # Get function source code
            contract_name = method["contractName"]
            method_name = method["name"]

            # Find the contract file
            # TODO: look up by ContractHandle (file, name). The stem heuristic
            # silently misses contracts whose name doesn't match the file basename
            # (e.g. a Helper contract declared inside Token.sol).
            contract_file = None
            for file_path in contract_files:
                if Path(file_path).stem == contract_name:
                    contract_file = file_path
                    break

            if not contract_file:
                return None

            try:
                with open(contract_file, "r") as f:
                    contract_code = f.read()
            except:
                return None

            # Extract the function implementation
            import re

            pattern = rf"function\s+{re.escape(method_name)}\s*\([^)]*\).*?(?=function\s+\w+|contract\s+\w+|interface\s+\w+|library\s+\w+|$)"
            match = re.search(pattern, contract_code, re.DOTALL)

            if not match:
                return None

            function_code = match.group(0)

            # Get function signature
            func_sig = self._get_function_signature(method, is_wildcard=True)

            # TODO 1: unify with the regular function declaration generation logic xml,
            # TODO 2: Do not use wildcard
            # Create LLM prompt
            prompt = f"""
            You are analyzing a Solidity function for decimal/precision conversion and need to generate a CVL summary.

            CVL Instructions:
            - Function summaries can call CVL functions with specific parameters
            - For wildcard summaries: function _.methodName($PARAMETERS$) internal => cvl_function_name(specific_parameter) expect <return_type>;
            - The "expect <return_type>" clause is REQUIRED for wildcard summaries that call CVL functions
            - CVL functions are defined outside the methods block

            Function signature: {func_sig}
            Function implementation:
            ```solidity
            {function_code}
            ```

            This function converts amounts between different decimal precisions. I need you to:

            1. Deduce the parameters names and types.
            2. Identify which parameter represents the input amount to be converted. It has to be a primitive numeric type (e.g., uint256, int, etc.) of the summarized function. It MUST appear in the parameter list. If multiple such parameters exist, choose the one that is most likely to be the amount (e.g., named `amount`, `value`, `qty`, etc.). If no such parameter exists, return an "InvalidSummary".
            3. Determine the return type of the function being summarized. Common types are uint256, uint128, int256, etc. If uncertain, use uint256.

            The type of the amount parameter must must be a subtype (according to Solidity) of the return type. If you cannot follow these instructions, use InvalidSummary
            """

            # Call LLM using existing infrastructure

            try:
                # Use Claude Opus 4.5 for complex decimal conversion analysis
                opus_model = "claude-opus-4-5-20251101"
                response = self._make_decimal_summary_call(
                    prompt,
                    opus_model,
                    max_tokens=2000,
                    log_to_file=True,
                    log_path=self.llm_log_path,
                )
            except Exception as e:
                raise Exception(f"Failed to call LLM API: {e}")

            if not response:
                return None

            # Debug: log the raw response
            if self.verbose:
                self.log(
                    f"Raw LLM response for decimal conversion: {response.model_dump_json()}",
                    "DEBUG",
                )

            if response.res_failed:
                self.log(
                    f"Failed to summarize decimal conversion: {response.res_failed.explanation}",
                    "WARNING"
                )
                return None

            result = response.res_success
            if not result:
                self.log(
                    f"No result set for {method_name}",
                    "WARNING"
                )
                return None

            # Try to extract JSON from the response (sometimes LLM adds extra text)
            summary_line = result.summary_line.strip()
            cvl_function = result.cvl_function.strip()
            return (summary_line, cvl_function)
        except Exception as e:
            raise Exception(f"Error generating decimal conversion summary: {e}")

    def _already_emitted_keys(self) -> Set[Tuple[str, str]]:
        """The (contractName, methodName) keys for everything already accumulated
        in ``self._methods_per_contract`` — i.e. the methods that have already been
        materialized into a per-contract spec at some point in this run."""
        return {
            (c_name, method["name"])
            for c_name, methods in self._methods_per_contract.items()
            for method in methods
        }

    def _emit_per_contract_summaries(self, new_methods: List[Dict[str, Any]]) -> Set[str]:
        """Group new matches by ``contractName`` and (re-)write the corresponding
        ``certora/specs/summaries/{C}_summaries.spec`` files.

        Each call accumulates into ``self._methods_per_contract`` and rewrites the
        affected per-contract spec files with the full method set seen so far.
        Methods already present (same ``(contractName, methodName)``) are skipped.

        Returns the set of ``contractName`` values whose specs were (re-)written.
        """
        if not new_methods:
            return set()

        already = self._already_emitted_keys()
        contracts_touched: Set[str] = set()
        for method in new_methods:
            key = (method["contractName"], method["name"])
            if key in already:
                continue
            already.add(key)
            self._methods_per_contract[method["contractName"]].append(method)
            contracts_touched.add(method["contractName"])

        if not contracts_touched:
            return set()

        contract_files = self.solidity_files_no_dependencies

        for c_name in contracts_touched:
            spec_path = self.user_summaries_dir / f"{c_name}_summaries.spec"
            content = self._build_spec_content_for_contract(
                c_name, self._methods_per_contract[c_name], contract_files, self._udt_context
            )
            spec_path.write_text(content)
            self.log(f"Wrote LLM summaries for {c_name} -> {spec_path}")

        # Helpers (decimal-conversion CVL functions) are scope-independent and shared
        # across per-contract specs to avoid duplicate-definition errors at typecheck.
        self._emit_helpers_spec()

        return contracts_touched

    def _build_spec_content_for_contract(
        self,
        contract_name: str,
        methods: List[Dict[str, Any]],
        contract_files: List[str],
        udt_context: str,
    ) -> str:
        """Build the CVL spec content for one summarized contract.

        If any method actually references a CVL helper (DECIMAL_CONVERSION_IDENTITY
        successfully generates one), the resulting spec includes ``import "./_helpers.spec";``
        at the top so it can be typechecked stand-alone. The helpers file is rewritten
        by ``_emit_helpers_spec`` right after every per-contract spec write, so the
        import always resolves.
        """
        helpers_referenced: Set[str] = set()
        header = [
            f"// LLM-based summaries for {contract_name}",
            "// Auto-generated by setup_summaries.py",
            "",
        ]
        body: List[str] = ["methods {"]

        by_summary_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for method in methods:
            by_summary_type[method.get("_summary_type", "NONDET")].append(method)

        for summary_type in sorted(by_summary_type.keys()):
            type_methods = by_summary_type[summary_type]
            body.append(f"    // {summary_type} summaries")
            body.append("")
            for method in sorted(type_methods, key=lambda m: m["name"]):
                self._append_method_summary(
                    body, method, summary_type, contract_files, udt_context, helpers_referenced
                )
                body.append("")
            body.append("")

        body.append("}")
        body.append("")

        if helpers_referenced:
            header.extend(['import "./_helpers.spec";', ""])
        return "\n".join(header + body)

    def _returns_reference_type(self, method: Dict[str, Any]) -> bool:
        """Whether any of the method's returns is a reference type (string/bytes/array/struct/mapping),
        determined from the per-return data location recorded in the build JSON.
        """
        return any(loc in ("memory", "calldata", "storage") for loc in method.get("returnLocations", []))

    def _append_method_summary(
        self,
        content: List[str],
        method: Dict[str, Any],
        summary_type: str,
        contract_files: List[str],
        udt_context: str,
        helpers_referenced: Set[str],
    ) -> None:
        """Render one method's CVL summary (comment block + summary line) into ``content``.

        Adds any CVL helper function name actually emitted by this method (currently
        only DECIMAL_CONVERSION_IDENTITY) to ``helpers_referenced`` so the caller
        knows whether to import ``_helpers.spec`` at the top of the per-contract spec.
        """
        contract = method["contractName"]
        name = method["name"]
        recipe_characteristic = method.get("_recipe_characteristic", "Unknown recipe")
        ai_explanation = method.get("_ai_explanation", "No explanation provided")

        comment_lines = [f"Recipe: {recipe_characteristic}"]
        if ai_explanation and ai_explanation != "No explanation provided":
            comment_lines.append(f"AI Analysis: {ai_explanation}")

        func_declaration = self._generate_function_declaration(method, is_wildcard=False)
        has_ellipsis = "..." in func_declaration and "/* " in func_declaration
        if has_ellipsis:
            comment_lines.append(
                "TODO: Manual fix needed - replace ellipsis with proper parameter types"
            )

        content.append("    /*")
        for line in comment_lines:
            content.append(f"     * {line}")
        content.append("     */")

        if summary_type.upper() == "NONDET":
            if self._returns_reference_type(method):
                sig = self._get_function_signature(method, is_wildcard=False)
                content.append(
                    f"    // AUTO-DISABLED (NONDET unsound for reference types): {sig}"
                )
            elif method["stateMutability"] in ["view", "pure"]:
                if has_ellipsis and (nondet_summary := self._generate_nondet_summary(
                    method, udt_context
                )) is not None:
                    content.append(f"    {nondet_summary}")
                else:
                    content.append(f"    {func_declaration} => NONDET;")
            else:
                self.log(
                    f"Warning: Cannot use NONDET for non-view method {contract}.{name}",
                    "WARNING",
                )
        elif summary_type.upper() == "HAVOC_ALL_DELETE":
            if method["visibility"] == "external":
                content.append(f"    {func_declaration} => HAVOC_ALL DELETE;")
            else:
                self.log(
                    f"Warning: HAVOC_ALL_DELETE should only be used for external methods, "
                    f"but {contract}.{name} is {method['visibility']}",
                    "WARNING",
                )
        elif summary_type.upper() == "DECIMAL_CONVERSION_IDENTITY":
            if method["stateMutability"] in ["view", "pure"]:
                summary_and_function = self._generate_decimal_conversion_summary(method, contract_files)
                if summary_and_function:
                    summary_line, cvl_function = summary_and_function
                    content.append(
                        "    // WARNING: This summary assumes no decimal conversion (identity function)"
                    )
                    content.append(
                        "    // This is UNSOUND and very limiting - should be generalized in the future"
                    )
                    content.append(f"    {summary_line}")
                    func_name_match = re.search(r"function\s+(\w+)\s*\(", cvl_function)
                    if func_name_match:
                        helper_name = func_name_match.group(1)
                        self._cvl_functions[helper_name] = cvl_function
                        helpers_referenced.add(helper_name)
                else:
                    sig = self._get_function_signature(method, is_wildcard=False)
                    content.append(f"    // Excluded from decimal conversion analysis: {sig}")
            else:
                self.log(
                    f"Warning: DECIMAL_CONVERSION_IDENTITY should only be used for view/pure methods, "
                    f"but {contract}.{name} is {method['stateMutability']}",
                    "WARNING",
                )
        else:
            self.log(f"Unknown summary type: {summary_type}", "WARNING")

    def _emit_helpers_spec(self) -> None:
        """Rewrite ``_helpers.spec`` from scratch with every accumulated decimal-conversion
        CVL function in ``self._cvl_functions``.

        Called at the end of every ``_emit_per_contract_summaries`` invocation, so the
        file's content grows monotonically as more decimal-conversion methods are
        discovered across iterations. Each call overwrites the whole file — we don't
        append — which is fine because ``self._cvl_functions`` is the source of truth
        and contains all helpers from this run.
        """
        if not self._cvl_functions:
            return
        helpers_path = self.user_summaries_dir / "_helpers.spec"
        lines = [
            "// CVL helper functions for LLM-generated summaries",
            "// Auto-generated by setup_summaries.py",
            "",
        ]
        for cvl_function in self._cvl_functions.values():
            lines.append(cvl_function)
            lines.append("")
        helpers_path.write_text("\n".join(lines))

    def _build_recipes(self, custom_recipe: Optional[str]) -> List[Recipe]:
        """Return the recipes to run in deterministic (alphabetical) order."""
        if custom_recipe:
            try:
                recipe_data = json.loads(custom_recipe)
            except Exception as e:
                raise Exception(f"Error parsing custom recipe: {e}")
            self.log(f"Using custom recipe: {recipe_data['characteristic']}")
            recipes = [
                Recipe(
                    recipe_type=RecipeType.CUSTOM,
                    characteristic=recipe_data["characteristic"],
                    properties=recipe_data["properties"],
                    summary_type=recipe_data.get("summary_type", "NONDET"),
                )
            ]
        else:
            recipes = [
                Recipe(
                    recipe_type=RecipeType.PRICE_COMPUTATION,
                    characteristic="computes a price",
                    properties={"stateMutability": "view"},
                    summary_type="NONDET",
                ),
                Recipe(
                    recipe_type=RecipeType.NEW_CONTRACT,
                    characteristic='creates a new contract using the "new" keyword',
                    properties={"visibility": "external"},
                    summary_type="HAVOC_ALL_DELETE",
                ),
                Recipe(
                    recipe_type=RecipeType.DECIMAL_CONVERSION,
                    characteristic='converts an amount from one precision or decimals to another precision or decimals, e.g., converting between token units. The conversion factor MUST be a constant or derived solely from a token\'s `decimals()`. DO NOT INCLUDE methods whose result depends on contract state such as `totalSupply`, `totalAssets`, token balances, reserves, or any share<->asset exchange rate, nor methods that fetch a rate or a price, i.e. that rely on any kind of external information besides a call to "decimals" of e.g. an ERC20 token. Good example: `normalizeDecimals`. Bad example: `fetchRate` that contains a call to an oracle and then convert that rate. Bad example: ERC4626 `convertToShares`/`convertToAssets`/`previewDeposit`, whose result depends on the vault\'s `totalSupply`/`totalAssets` and is therefore an exchange rate, not a decimal conversion. We want the underlying conversion logic, not the oracle interaction.',
                    properties={
                        "visibility": "internal",
                        "stateMutability": ["view", "pure"],
                    },
                    summary_type="DECIMAL_CONVERSION_IDENTITY",
                ),
                Recipe(
                    recipe_type=RecipeType.NONLINEAR_OPERATIONS,
                    characteristic="contains more than 3 non-linear operations (multiplication, division, exponentiation, or modulo)",
                    properties={"visibility": "internal", "stateMutability": "pure"},
                    summary_type="NONDET",
                ),
                Recipe(
                    recipe_type=RecipeType.INLINE_ASSEMBLY,
                    characteristic="contains inline assembly blocks with `mload` or `mstore` instructions",
                    properties={"visibility": "internal"},
                    summary_type="NONDET",
                ),
            ]
        return sorted(recipes, key=lambda r: r.characteristic)

    async def analyze_contract(
        self,
        contract_name: str,
        contract_files: List[str],
        methods_to_skip: Optional[Set[Tuple[str, str]]] = None,
        custom_recipe: Optional[str] = None,
    ) -> bool:
        """Run all LLM recipes for one compilation unit and emit per-summarized-contract specs.

        Filters methods by ``originatingContract == contract_name``. Each match is appended
        to ``self._methods_per_contract[match["contractName"]]`` and the corresponding
        ``certora/specs/summaries/{contractName}_summaries.spec`` file is (re-)written with
        the full set of methods seen so far for that contract — methods already in
        ``self._emitted_methods`` are skipped, so calling ``analyze_contract`` repeatedly
        across iterations doesn't duplicate summaries even when compilation units overlap.

        Args:
            contract_name: Compilation unit to analyze (matched against ``originatingContract``).
            contract_files: Solidity source files passed to ``analyze_with_llm``.
            methods_to_skip: ``(contractName, methodName)`` pairs already handled by the
                non-LLM step or otherwise not to be reanalyzed. Always merged with
                ``self._emitted_methods`` so prior analyses aren't redone.
            custom_recipe: Optional JSON recipe string; bypasses the default recipe set.

        Returns:
            True if at least one per-contract spec was (re-)written.
        """
        recipes_sorted = self._build_recipes(custom_recipe)

        self.log(f"Running LLM analysis for compilation unit: {contract_name}")

        skip: Set[Tuple[str, str]] = set(methods_to_skip) if methods_to_skip else set()
        skip.update(self._already_emitted_keys())

        all_matches: List[Dict[str, Any]] = []
        seen: Set[Tuple[str, str]] = set()
        for recipe in recipes_sorted:
            self.log(f"  Recipe '{recipe.characteristic}'")
            matches = await self.analyze_with_llm(recipe, set(contract_files), skip, contract_name)
            if not matches:
                continue
            self.log(f"    {len(matches)} match(es)")
            for method in matches:
                key = (method["contractName"], method["name"])
                self.log(f"      - {key[0]}.{key[1]}")
                skip.add(key)
                if key in seen:
                    continue
                seen.add(key)
                all_matches.append(method)

        if not all_matches:
            return False

        contracts_written = self._emit_per_contract_summaries(all_matches)
        return bool(contracts_written)

    @staticmethod
    def _library_matches(library_names: List[str], method: Dict[str, Any]) -> bool:
        """Whether ``method`` belongs to any contract in ``library_names``.

        Matched against both the defining contract (reliable for inherited methods) and the build
        data's ``contractName``.
        """
        owners = (method.get("definingContract"), method.get("contractName"))
        return any(name in owners for name in library_names)

    def match_summaries_from_all_methods(
        self, main_contract: str
    ) -> Tuple[Set[str], Set[Tuple[str, str]]]:
        """Match function summaries against methods from all_methods.json.

        Args:
            main_contract: Main contract name to analyze.

        Returns:
            Tuple of (matched_function_keys, matched_method_tuples):
            - matched_function_keys: Set of matched function-summary keys.
            - matched_method_tuples: Set of (contractName, method_name) tuples
              already marked for summarization (used by LLM analysis to avoid
              duplicate work).
        """
        methods_file = PATH_ALL_METHODS_JSON
        if not methods_file.exists():
            self.log("all_methods.json not found", "ERROR")
            return set(), set()

        mp = self.methods_parser

        self.log(f"Matching summaries for {main_contract}...")
        # Filter to only methods that originate from this contract (compilation unit)
        contract_methods = mp.get_methods_by_originating_contract(main_contract)
        matched_functions: Set[str] = set()
        matched_method_tuples: Set[Tuple[str, str]] = set()

        for func_name, func_info in self.function_summaries.items():
            self.log(f"Checking for {func_info['description']} usage...", "DEBUG")

            # Match by name (disjunctive - match any name in list)
            if "names" in func_info:
                for method in contract_methods:
                    if method["name"] in func_info["names"]:
                        # Optional: require the method to belong to one of library_names
                        if func_info.get("library_names"):
                            if self._library_matches(func_info["library_names"], method):
                                self.log(
                                    f"✓ Matched {func_info['description']} by name in {main_contract}"
                                )
                                matched_functions.add(func_name)
                                matched_method_tuples.add((method["contractName"], method["name"]))
                                break
                        else:
                            self.log(
                                f"✓ Matched {func_info['description']} by name in {main_contract}"
                            )
                            matched_functions.add(func_name)
                            matched_method_tuples.add((method["contractName"], method["name"]))
                            break

            # Match by signature (disjunctive - match any signature in list)
            if "signatures" in func_info and func_name not in matched_functions:
                for method in contract_methods:
                    method_sig = f"{method['name']}({','.join(method['fullSignature'])})"
                    if method_sig in func_info["signatures"]:
                        # Optional: require the method to belong to one of library_names
                        if func_info.get("library_names"):
                            if self._library_matches(func_info["library_names"], method):
                                self.log(
                                    f"✓ Matched {func_info['description']} by signature in {main_contract}: {method_sig}"
                                )
                                matched_functions.add(func_name)
                                matched_method_tuples.add((method["contractName"], method["name"]))
                                break
                        else:
                            self.log(
                                f"✓ Matched {func_info['description']} by signature in {main_contract}: {method_sig}"
                            )
                            matched_functions.add(func_name)
                            matched_method_tuples.add((method["contractName"], method["name"]))
                            break

        self.log(f"Found {len(matched_functions)} summaries for {main_contract} ({len(matched_method_tuples)} method tuples)")
        return matched_functions, matched_method_tuples

    async def on_contracts_entered_scene(self, contract_names: List[str], main_contract: str) -> None:
        """Summarize a batch of contracts that have entered the verification scene — used for both
        the initial scene and each batch added during call resolution.

        Curated matching runs first so the LLM step can skip methods a curated summary already
        covers; the base aggregator and the prune pass are refreshed once per batch.

        ``main_contract`` only determines the aggregator filename — ``contract_names`` are the
        contracts to summarize. LLM analysis honors the ``_enable_llm`` / ``_custom_recipe`` /
        ``_llm_contract_files`` settings recorded by ``configure``. The affected per-contract specs
        and the aggregator are re-written on each call, so manual edits to autosetup-managed specs
        are overwritten.
        """
        contract_names = list(dict.fromkeys(contract_names))  # de-dup, preserve order
        if not contract_names:
            return

        # TODO(precision): matching and LLM analysis below are scoped to each contract's whole
        # compilation unit, not to the methods actually reachable from the main contract. A contract
        # pulled in as a candidate link/dispatch target therefore gets curated + LLM summaries for
        # functions the main contract never calls (e.g. a contract linked at an interface-typed field
        # whose extra methods are unused) — the prover reports those as "unused". This is sound and
        # the prover cost is negligible, but it spends LLM calls on unreachable methods. Scoping to the
        # reachable set (a transitive closure over resolved call edges, or the prover's reachability
        # report) would avoid the wasted LLM work, at the cost of computing reachability.

        # 1. Curated summaries first — match per contract, accumulating both the matched keys
        #    and the (contractName, methodName) tuples the LLM step must not re-summarize.
        curated_keys: Set[str] = set()
        per_contract_skip: Dict[str, Set[Tuple[str, str]]] = {}
        for name in contract_names:
            matched, tuples = self.match_summaries_from_all_methods(name)
            curated_keys |= matched
            per_contract_skip[name] = tuples
        if curated_keys:
            # Publish for downstream consumers (autosetup's library-scene filter) and for
            # prune_emitted_specs's curated-over-LLM dedup precedence.
            self.matched_functions |= curated_keys
            self.copy_summaries_folder(curated_keys)
            registered = self._add_aggregator_imports(
                self.curated_summary_import_path(key, main_contract) for key in curated_keys
            )
            if registered:
                self.log(f"Aggregator: registered curated specs {registered}")

        # 2. LLM analysis per contract, skipping curated-covered methods.
        if self._enable_llm:
            for name in contract_names:
                await self.analyze_contract(
                    name,
                    self._llm_contract_files,
                    methods_to_skip=per_contract_skip.get(name),
                    custom_recipe=self._custom_recipe,
                )

        # 3. Import the per-contract LLM specs that landed on disk.
        self._add_aggregator_imports(
            f"{name}_summaries.spec"
            for name in contract_names
            if (self.user_summaries_dir / f"{name}_summaries.spec").exists()
        )

        # 4. Refresh the aggregator (sorted, from accumulated state) and prune methods{} entries
        #    that don't resolve in the compiled scene across all emitted specs (curated + LLM).
        self._rewrite_aggregator(main_contract)
        self.prune_emitted_specs(main_contract)

    def configure(
        self,
        main_contract: str,
        contract_files: List[str] | None = None,
        additional_contracts: Optional[List[str]] = None,
        include_test_files: bool = False,
        include_dependencies: bool = False,
        enable_llm: bool = False,
        custom_recipe: Optional[str] = None,
    ) -> bool:
        """Capture the configuration for a summarization run; perform no summarization.

        Resolves the source file list (discovering all Solidity files when ``contract_files`` is
        empty), parses the additional-contract names into ``self.additional_names``, and records the
        main contract and the LLM settings on the instance for ``on_contracts_entered_scene`` to use.

        Returns True once configured, or False if no Solidity sources are found.

        Args:
            main_contract: Name of the main contract being verified.
            contract_files: List of contract files to analyze.
            additional_contracts: ``--additional-contracts`` strings (each
                ``path/to/Foo.sol`` or ``path/to/Foo.sol:Foo``); seeded into the initial
                scene alongside the main contract.
            include_test_files: Include test files in analysis.
            include_dependencies: Include dependency files in analysis.
            enable_llm: Enable LLM-based method analysis.
            custom_recipe: Optional custom LLM recipe JSON string.
        """
        self.log("=== AUTOMATIC SUMMARY SETUP ===")

        # If no contract files provided, find all Solidity files
        if not contract_files:
            self.log("Searching for all Solidity files in the project...")
            contract_files = self.find_all_solidity_files(
                include_test_files=include_test_files,
                include_dependencies=include_dependencies,
            )

            if not contract_files:
                self.log("No Solidity files found in the project", "WARNING")
                return False

        self.main_contract = main_contract
        self.additional_names = [split_contract_spec(ac)[1] for ac in (additional_contracts or [])]

        self.log(f"Main contract for analysis: {main_contract}")
        if self.additional_names:
            self.log(f"Additional contracts for analysis: {self.additional_names}")

        # LLM settings + the (dependency-inclusive) source list, applied by on_contracts_entered_scene
        # so the initial scene and later call-resolution batches share identical config.
        self._enable_llm = enable_llm
        self._custom_recipe = custom_recipe
        self._llm_contract_files = contract_files
        if not enable_llm and self.verbose:
            self.log("LLM analysis not enabled (use --enable-llm flag)", "INFO")
        return True


def main():
    parser = argparse.ArgumentParser(
        description="Automatic setup for contract summaries in Certora verification"
    )

    parser.add_argument(
        "contract_files",
        nargs="*",  # Changed from '+' to '*' to make it optional
        help="Solidity contract files to analyze (if not provided, analyzes all .sol files in project)",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Enable verbose output (-v for basic, -vv for extra details)",
    )

    parser.add_argument(
        "--include-test-files",
        action="store_true",
        help="Include test (.t.sol) and script (.s.sol) files in analysis (default: excluded)",
    )

    parser.add_argument(
        "--include-dependencies",
        action="store_true",
        help="Include files in dependency directories (node_modules, lib, forge-std) in analysis (default: excluded)",
    )

    parser.add_argument(
        "--main-contract",
        type=str,
        required=True,
        help="Main contract name being verified",
    )

    parser.add_argument(
        "--enable-llm",
        action="store_true",
        help="Enable LLM-based method analysis (requires ANTHROPIC_API_KEY in .env)",
    )

    parser.add_argument(
        "--force-llm-regenerate",
        action="store_true",
        help="Force regeneration of LLM summaries even if they already exist",
    )

    parser.add_argument(
        "--skip-non-llm",
        action="store_true",
        help="Skip all non-LLM processing (only run LLM analysis)",
    )

    parser.add_argument(
        "--llm-recipe", type=str, help="Custom recipe for LLM analysis (JSON format)"
    )

    args = parser.parse_args()

    # Validate contract files if provided
    if args.contract_files:
        for contract_file in args.contract_files:
            if not contract_file.endswith(".sol"):
                print(f"Error: {contract_file} is not a Solidity file", file=sys.stderr)
                sys.exit(1)
            if not Path(contract_file).exists():
                print(f"Error: {contract_file} does not exist", file=sys.stderr)
                sys.exit(1)

    # Run the setup
    setup = SummarySetup(verbose=args.verbose)

    # If skip-non-llm is set, only run LLM analysis (no curated matching, no summary aggregator).
    if args.skip_non_llm:
        if not args.enable_llm:
            print("Error: --skip-non-llm requires --enable-llm")
            sys.exit(1)
        setup.log("Running LLM analysis only (skipping non-LLM processing)")
        success = asyncio.run(
            setup.analyze_contract(
                contract_name=args.main_contract,
                contract_files=args.contract_files if args.contract_files else [],
                custom_recipe=args.llm_recipe,
            )
        )
    else:
        success = setup.configure(
            main_contract=args.main_contract,
            contract_files=args.contract_files if args.contract_files else None,
            include_test_files=args.include_test_files,
            include_dependencies=args.include_dependencies,
            enable_llm=args.enable_llm,
            custom_recipe=args.llm_recipe,
        )
        if success:
            asyncio.run(
                setup.on_contracts_entered_scene(
                    [args.main_contract] + setup.additional_names, args.main_contract
                )
            )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
