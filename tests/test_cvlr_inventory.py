"""The public-item extraction the crate reference's completeness gate is built on.

An inventory that misses an item is a hole the gate can never demand, and an inventory that invents
one is a gate failure nobody can satisfy. Since the extraction is regex over Rust rather than a
parse, those two failure modes have to be pinned by example — so every shape here was observed in
the CVLR reference set, and three of them are shapes a naive line scan gets wrong.

The three worth knowing about, because each hides a widely-used name:

* ``impl_bin_assert!(cvlr_assert_le, <=, $)`` — a macro that generates macros. The exported name
  exists only as an *argument*, and four of these emit twenty-four names in ``cvlr-asserts`` alone.
* ``pub use super::log::cvlr_log as clog`` — the name every project writes is an alias whose
  definition carries a different one.
* ``#[cfg(test)] mod tests { pub fn … }`` — indistinguishable from API on any single line.
"""

from composer.scripts.cvlr_crate_reference import (
    CORPUS_ROOT,
    EXPANSION_SECTION,
    NO_EXAMPLE,
    Module,
    ReferenceEntry,
    _entry_group,
    _entry_section,
    _expansion_section,
    build_manifest,
)
from composer.spec.cvlr.inventory import ExpansionPair, Item, items_in, uncovered


def _names(source: str, kind: str | None = None) -> set[str]:
    return {i.name for i in items_in("c-1.0.0", "c-1.0.0/src/lib.rs", source) if kind in (None, i.kind)}


def test_the_function_shapes_the_reference_set_actually_uses():
    source = """
pub fn plain() {}
pub const fn constant() -> u64 { 0 }
pub unsafe fn dangerous() {}
pub(crate) fn narrow() {}
fn private() {}
"""
    assert _names(source, "fn") == {"plain", "constant", "dangerous", "narrow"}
    assert "private" not in _names(source)


def test_an_unexported_macro_is_not_api():
    """A ``macro_rules!`` without ``#[macro_export]`` is unreachable from a target's code, and
    documenting one invites exactly the call that will not compile."""
    source = """
#[macro_export]
macro_rules! reachable { () => {}; }

macro_rules! internal { () => {}; }
"""
    assert _names(source, "macro") == {"reachable"}


def test_a_macro_that_generates_macros_exports_its_arguments():
    """``cvlr_assert_eq`` appears nowhere in the source except as an argument. Miss this and the gate
    never asks for the comparison assertions — the family an authoring agent reaches for most."""
    source = """
macro_rules! impl_bin_assert {
    ($name: ident, $pred: tt, $dollar: tt) => {
        #[macro_export]
        macro_rules! $name {
            ($lhs: expr, $rhs: expr) => {{ }};
        }
        pub use $name;
    };
}

impl_bin_assert!(cvlr_assert_eq, ==, $);
impl_bin_assert!(cvlr_assert_le, <=, $);
"""
    assert _names(source, "macro") == {"cvlr_assert_eq", "cvlr_assert_le"}


def test_an_ordinary_macro_invocation_is_not_a_definition():
    """The generator rule keys on a macro whose body exports; a macro that merely *runs* at module
    level defines nothing, and treating its first argument as an export would invent names."""
    source = """
#[macro_export]
macro_rules! not_a_generator { ($x: ident) => { }; }

not_a_generator!(some_argument);
"""
    assert _names(source, "macro") == {"not_a_generator"}


def test_a_renamed_reexport_is_the_name_callers_write():
    """``clog!`` and ``#[cvlr::predicate]`` are the two most-used entry points in the library and
    both are aliases. A reference documenting only the definition's name leaves an agent unable to
    look up the spelling every project uses."""
    source = """
pub use super::log::cvlr_log as clog;
pub use macros::cvlr_predicate as predicate;
"""
    items = {i.name: i for i in items_in("c-1.0.0", "c-1.0.0/src/lib.rs", source)}
    assert set(items) == {"clog", "predicate"}
    assert items["clog"].owner == "super::log::cvlr_log"


def test_a_glob_reexport_names_nothing_to_document():
    assert _names("pub use cvlr_asserts::*;\n") == set()


def test_test_code_is_not_api():
    source = """
pub fn real() {}

#[cfg(test)]
mod tests {
    pub fn helper() {}
    pub struct Fixture;
}

pub fn also_real() {}
"""
    assert _names(source) == {"real", "also_real"}


def test_a_doc_comment_mentioning_an_item_is_not_an_item():
    """``cvlr-macros`` documents its own usage in doc comments, complete with ``pub fn`` lines."""
    source = """
/// Usage:
/// ```
/// pub fn predicate_name(c: &Ctx) {}
/// ```
pub fn cvlr_predicate() {}
"""
    assert _names(source, "fn") == {"cvlr_predicate"}


def test_a_method_records_the_type_it_belongs_to():
    """``nondet`` is defined many times over in ``cvlr-nondet``; the bare name cannot say which one a
    generated entry is about."""
    source = """
impl Nondet for NativeIntU64 {
    pub fn nondet() -> Self { Self }
}
"""
    items = list(items_in("c-1.0.0", "c-1.0.0/src/lib.rs", source))
    assert [(i.name, i.owner, i.qualified) for i in items] == [
        ("nondet", "NativeIntU64", "NativeIntU64::nondet")
    ]


def test_a_trait_impl_names_the_implementing_type_not_the_trait():
    """The reader wants "which type has this method", not "which trait declared it"."""
    source = "impl<T> CvlrLog for Wrapper<T> {\n    pub fn log(&self) {}\n}\n"
    items = list(items_in("c-1.0.0", "c-1.0.0/src/lib.rs", source))
    assert items[0].owner == "Wrapper"


def test_the_proc_macro_crates_export_the_name_they_declare():
    """A derive's name is declared in the attribute, not taken from the function it sits on — the
    function is called ``derive_nondet`` and nobody writes that."""
    source = """
#[proc_macro_derive(Nondet)]
pub fn derive_nondet(input: TokenStream) -> TokenStream { input }

#[proc_macro_attribute]
pub fn rule(attr: TokenStream, item: TokenStream) -> TokenStream { item }

#[proc_macro]
pub fn cvlr_spec(input: TokenStream) -> TokenStream { input }
"""
    found = {i.name: i.kind for i in items_in("c-1.0.0", "c-1.0.0/src/lib.rs", source)}
    assert found == {"Nondet": "derive", "rule": "attribute", "cvlr_spec": "macro"}


# --------------------------------------------------------------------------------------------
# the completeness gate
# --------------------------------------------------------------------------------------------


def _item(name: str) -> Item:
    return Item("c-1.0.0", "c-1.0.0/src/lib.rs", 1, "fn", name)


def test_a_family_named_in_one_line_counts_as_covered():
    """The whole point of grouping: one entry may cover twenty mechanical variants, and a stricter
    check than "is the name mentioned" would fail exactly the entries that do it well."""
    entry = "The comparison assertions — cvlr_assert_eq, cvlr_assert_ne, cvlr_assert_le — all …"
    items = [_item("cvlr_assert_eq"), _item("cvlr_assert_ne"), _item("cvlr_assert_le")]
    assert uncovered(items, entry) == ()


def test_an_unmentioned_symbol_is_reported_once_in_source_order():
    items = [_item("covered"), _item("missing_a"), _item("missing_b"), _item("missing_a")]
    assert uncovered(items, "covered is documented") == ("missing_a", "missing_b")


# --------------------------------------------------------------------------------------------
# what the producer renders
# --------------------------------------------------------------------------------------------


def _entry(**overrides: object) -> ReferenceEntry:
    base = {
        "title": "Nondeterministic scalars",
        "symbols": ["nondet", "nondet_with"],
        "summary": "Produces an unconstrained value of the target type.",
        "signature": "pub fn nondet<T: Nondet>() -> T",
        "example": "let x: u64 = nondet();",
        "notes": "",
    }
    return ReferenceEntry.model_validate({**base, **overrides})


def _module(**overrides: object) -> Module:
    base = {
        "crate": "cvlr-nondet-0.6.1",
        "path": "cvlr-nondet-0.6.1/src/core.rs",
        "items": (),
        "source": "",
        "expansions": (),
    }
    return Module(**{**base, **overrides})  # type: ignore[arg-type]


def test_an_entry_with_a_verified_example_renders_it_as_code():
    section = _entry_section(_module(), _entry())
    kinds = [b.kind.value for b in section.blocks]
    assert "code" in kinds
    assert not [b for b in section.blocks if NO_EXAMPLE in b.body]


def test_an_entry_whose_example_never_compiled_keeps_its_prose_and_says_so():
    """Coverage and exemplifiability are not the same thing. ``cvlr_rules!`` is the construct the
    published methodology recommends most and the hardest to show in a self-contained snippet, so an
    all-or-nothing gate would lose exactly the entries a reader most needs. The prose is derived from
    the source and stays true; only the Rust nobody could compile is withheld."""
    section = _entry_section(_module(), _entry(example=""))
    bodies = [b.body for b in section.blocks]
    assert any("Produces an unconstrained value" in b for b in bodies)
    assert NO_EXAMPLE in bodies
    assert "code" not in [b.kind.value for b in section.blocks]


def test_a_missing_example_is_distinguishable_from_a_needless_one():
    """A reader who sees no example has to be able to tell "this needs none" from "none could be
    produced" — only the second is a reason to go and read the crate source."""
    assert "read the crate's own tests" in NO_EXAMPLE


def test_the_symbol_list_rides_with_the_prose_in_the_vector_index():
    """A vector chunk that is a bare list of identifiers retrieves for everything and means nothing,
    so the names join the sentence that gives them context."""
    group = _entry_group(_module(), _entry())
    paragraph = next(b for b in group.blocks if b.kind.value == "paragraph")
    assert "nondet_with" in paragraph.body and "unconstrained value" in paragraph.body


def test_an_expansion_pair_is_quoted_rather_than_summarised():
    """The one question §5.4 says a corpus cannot answer, answered exactly by the crate itself."""
    section = _expansion_section(
        ExpansionPair(
            crate="cvlr-asserts-0.6.1",
            name="test_add_loc",
            invocation="add_loc!();",
            expansion='::cvlr_asserts::log::add_loc("<FILE>", 0u32);',
        )
    )
    code = [b.body for b in section.blocks if b.kind.value == "code"]
    assert code == ["add_loc!();", '::cvlr_asserts::log::add_loc("<FILE>", 0u32);']
    assert section.headers == [CORPUS_ROOT, EXPANSION_SECTION, "cvlr-asserts-0.6.1: test_add_loc"]


def test_entries_and_expansions_share_the_corpus_root_but_not_the_shelf():
    manifest = build_manifest([(_module(), _entry())], (), source="test")
    assert manifest.knowledge_base == "cvlr_kb"
    assert manifest.manual_sections[0].headers[0] == CORPUS_ROOT
    assert manifest.manual_sections[0].headers[1] == "cvlr-nondet-0.6.1"
