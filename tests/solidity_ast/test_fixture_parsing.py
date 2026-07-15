"""Strict round-trips of real certoraRun --dump_asts fixtures through the typed models.

These are the schema-drift alarm: a solc release that adds a nodeType shows up as an
UnknownNode here, and a new field shows up as a non-empty model_extra — both fail.
"""

from pathlib import Path

import pytest

from certora_autosetup.solidity_ast import (
    AstDump,
    ContractDefinition,
    ErrorDefinition,
    InlineAssembly,
    MemberAccess,
    RevertStatement,
    UncheckedBlock,
    UnknownNode,
    UserDefinedValueTypeDefinition,
    YulBlock,
    find_all,
    walk,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "solidity_ast"
# 0.4/0.5 are below the >= 0.6 floor but must still parse fully typed (lenient-older
# policy); version-specific assertions below gate on this split.
LEGACY_FIXTURES = ["solc_0_4_26", "solc_0_5_17"]
MODERN_FIXTURES = ["solc_0_6_12", "solc_0_7_6", "solc_0_8_30"]
SOLC_FIXTURES = LEGACY_FIXTURES + MODERN_FIXTURES


@pytest.fixture(scope="module", params=SOLC_FIXTURES)
def dump(request: pytest.FixtureRequest) -> AstDump:
    return AstDump.load(FIXTURES / f"{request.param}.asts.json", on_error="raise")


def test_all_sources_parse(dump: AstDump) -> None:
    sources = list(dump.iter_sources())
    assert sources
    for _, source in sources:
        assert source.raw_kind == "solidity"
        assert source.root is not None


def test_no_unknown_nodes(dump: AstDump) -> None:
    for _, _, root in dump.iter_parsed_roots():
        unknown = {n.nodeType for n in walk(root) if isinstance(n, UnknownNode)}
        assert not unknown, f"nodeTypes not covered by the model set: {unknown}"


def test_no_extra_fields(dump: AstDump) -> None:
    for _, _, root in dump.iter_parsed_roots():
        extras = {
            f"{type(n).__name__}.{key}"
            for n in walk(root)
            for key in (n.model_extra or {})
        }
        assert not extras, f"fields present in solc output but missing from models: {extras}"


def test_typed_index_covers_raw_index(dump: AstDump) -> None:
    # The reverse (typed finding MORE nodes than the raw flat map) is expected: the
    # certoraRun flattener does not descend through id-less container objects.
    for _, source in dump.iter_sources():
        raw_ids = {int(i) for i in source.raw if i.lstrip("-").isdigit()}
        missing = raw_ids - set(source.nodes)
        assert not missing, f"{source.source_path}: raw ids unreachable by typed walk: {missing}"


def test_certora_contract_name_stamping(dump: AstDump) -> None:
    stamped = [
        n
        for _, source in dump.iter_sources()
        for n in source.nodes.values()
        if n.certora_contract_name is not None
    ]
    assert stamped
    assert all(isinstance(n.certora_contract_name, str) for n in stamped)


def test_inheritance_semantics(dump: AstDump, request: pytest.FixtureRequest) -> None:
    contracts = {
        c.name: c
        for _, _, root in dump.iter_parsed_roots()
        for c in find_all(root, ContractDefinition)
    }
    diamond = contracts["Diamond"]
    id_to_name = {c.id: c.name for c in contracts.values()}
    linearized = [id_to_name[i] for i in diamond.linearizedBaseContracts]
    assert linearized[0] == "Diamond"
    assert set(linearized[1:]) >= {"Base"}
    assert any(c.contractKind == "interface" for c in contracts.values())
    assert any(c.contractKind == "library" for c in contracts.values())
    if "dump" in request.fixturenames and request.node.callspec.params["dump"] in MODERN_FIXTURES:
        # the `abstract` flag only exists from solc 0.6
        assert any(c.abstract for c in contracts.values())
    else:
        assert not contracts["Base"].fullyImplemented


def test_src_location_points_at_source(dump: AstDump) -> None:
    for _, _, root in dump.iter_parsed_roots():
        source_text = (FIXTURES / "contracts" / root.absolutePath).read_bytes()
        contract = next(find_all(root, ContractDefinition))
        loc = contract.src_location
        snippet = source_text[loc.offset : loc.offset + loc.length]
        assert snippet.split()[0] in (b"contract", b"abstract", b"interface", b"library")


def test_yul_present(dump: AstDump, request: pytest.FixtureRequest) -> None:
    assemblies = [
        a for _, _, root in dump.iter_parsed_roots() for a in find_all(root, InlineAssembly)
    ]
    assert assemblies
    for assembly in assemblies:
        if request.node.callspec.params["dump"] in MODERN_FIXTURES:
            assert isinstance(assembly.AST, YulBlock)
            assert assembly.AST.statements
        else:
            # the <= 0.5 dialect: no Yul tree, assembly source text + keyed refs
            assert assembly.AST is None
            assert assembly.operations
            assert all(isinstance(r, dict) for r in assembly.externalReferences)


def test_08_specific_nodes_present() -> None:
    dump = AstDump.load(FIXTURES / "solc_0_8_30.asts.json", on_error="raise")
    roots = [root for _, _, root in dump.iter_parsed_roots()]
    for node_type in (ErrorDefinition, RevertStatement, UncheckedBlock, UserDefinedValueTypeDefinition):
        assert any(any(find_all(r, node_type)) for r in roots), node_type.__name__
    code_accesses = [
        m
        for r in roots
        for m in find_all(r, MemberAccess)
        if m.memberName == "code"
    ]
    assert code_accesses
