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

from composer.spec.cvlr.inventory import Item, items_in, uncovered


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
