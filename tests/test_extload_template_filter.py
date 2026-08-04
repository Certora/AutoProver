"""SummarySetup._filter_template_entries_to_contract keeps only the materialized
methods{} entries that match a real method of the contract (by name, param types, and
return type). This drops the extload template's variants a contract doesn't implement,
which otherwise break CVL: a same-signature/different-return pair makes combineFunctions
throw "un-mergeable signature ...", and a DELETE on an absent method throws "Only
public/external methods are DELETE-able".
"""

from certora_autosetup.setup.setup_summaries import SummarySetup


class _FakeMethodsParser:
    def __init__(self, methods):
        self._methods = methods

    def get_methods_by_originating_contract(self, _contract_name):
        return self._methods


def _summary_setup(methods):
    # Bypass the heavy __init__ (which needs all_methods.json etc.) — the filter only
    # touches self.methods_parser, self.log, and class-level regex/normalizer.
    s = SummarySetup.__new__(SummarySetup)
    s.methods_parser = _FakeMethodsParser(methods)
    s.log = lambda *_a, **_k: None
    return s


# The full extload template as materialized for one contract (lowercase
# extsload/exttload + a same-signature/different-return pair + camelCase extSload/extSloads).
_MATERIALIZED = """\
methods {
    function Reader.extsload(bytes32 slot) external returns (bytes32) => NONDET DELETE;
    function Reader.extsload(bytes32[] slots) external returns (bytes32[] memory) => ArbBytes32(slots) DELETE;
    function Reader.extsload(bytes32 startSlot, uint256 nSlots) external returns (bytes32[] memory) => ArbNBytes32(startSlot, nSlots) DELETE;
    function Reader.extsload(bytes32 startSlot, uint256 nSlots) external returns (bytes memory) => ArbNBytes(startSlot, nSlots) DELETE;
    function Reader.exttload(bytes32 slot) external returns (bytes32) => NONDET DELETE;
    function Reader.exttload(bytes32[] slots) external returns (bytes32[] memory) => ArbBytes32(slots) DELETE;
    function Reader.exttload(bytes32[] slots) external returns (bytes memory) => ArbBytes(slots) DELETE;
    function Reader.extSload(bytes32 slot) external returns (bytes32) => NONDET DELETE;
    function Reader.extSloads(bytes32[] slots) external returns (bytes32[] memory) => ArbBytes32(slots) DELETE;
}
function ArbBytes32(bytes32[] slots) returns bytes32[] { return slots; }
"""

# A contract exposing extSload(bytes32)->bytes32 and extSloads(bytes32[])->bytes32[].
_CAMEL_METHODS = [
    {"name": "extSload", "fullSignature": ["bytes32"], "returns": ["bytes32"]},
    {"name": "extSloads", "fullSignature": ["bytes32[]"], "returns": ["bytes32[]"]},
]


def _entry_lines(spec: str):
    return [ln.strip() for ln in spec.splitlines() if ln.strip().startswith("function Reader.")]


def test_keeps_only_matching_methods() -> None:
    out = _summary_setup(_CAMEL_METHODS)._filter_template_entries_to_contract(_MATERIALIZED, "Reader")
    entries = _entry_lines(out)
    names = [ln.split("Reader.")[1].split("(")[0] for ln in entries]
    assert names == ["extSload", "extSloads"]                 # absent extsload/exttload dropped
    assert "ArbBytes32" in out                                # non-entry (CVL helper) lines preserved
    assert "function ArbBytes32" in out


def test_no_duplicate_signatures_remain() -> None:
    out = _summary_setup(_CAMEL_METHODS)._filter_template_entries_to_contract(_MATERIALIZED, "Reader")
    sigs = [ln.split("=>")[0].split("returns")[0].strip() for ln in _entry_lines(out)]
    assert len(sigs) == len(set(sigs))


def test_keeps_bytes_return_variant_when_that_is_what_the_contract_has() -> None:
    # A contract whose extsload(bytes32,uint256) returns `bytes` keeps the ->bytes entry
    # (not the ->bytes32[] one) — the two-return-variants case resolves per contract.
    methods = [{"name": "extsload", "fullSignature": ["bytes32", "uint256"], "returns": ["bytes"]}]
    out = _summary_setup(methods)._filter_template_entries_to_contract(_MATERIALIZED, "Reader")
    entries = _entry_lines(out)
    assert len(entries) == 1
    assert "returns (bytes memory)" in entries[0]
    assert "bytes32[]" not in entries[0]


def test_unknown_methods_leaves_content_unchanged() -> None:
    # If the contract's methods can't be determined, don't filter (avoid dropping everything).
    out = _summary_setup([])._filter_template_entries_to_contract(_MATERIALIZED, "Reader")
    assert out == _MATERIALIZED
