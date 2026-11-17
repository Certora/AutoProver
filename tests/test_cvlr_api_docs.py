"""What the CVLR API producer emits.

Everything here is pure: the walk's output is written by hand as :class:`DocumentedCrate`s, so
these tests are about what the producer does with items, not about rustdoc's schema. Whether the
walk reads real rustdoc right is ``test_every_documented_path_compiles_in_a_scaffolded_project``
at the bottom, which is ``expensive``: it documents the pinned crates and lets cargo judge every
path the corpus offers. See ``docs/cvlr-api-docs-plan.md`` §4.2 and §7.
"""

import re
import shutil
from pathlib import Path

import pytest

from composer.cargo.doctest import Failed, Passed, check_examples
from composer.cargo.metadata import CargoMetadataJson, parse_metadata
from composer.rag.cvlr_api import SURFACE_HEADING, ApiPath
from composer.rag.import_format import ManualBlockKind, RagManifest
from composer.scripts.cvlr_api_docs import (
    CfgGate,
    DocumentedCrate,
    ExampleGate,
    FeatureGate,
    Item,
    ItemKind,
    LocalCheckout,
    Origin,
    Published,
    RustdocUnavailable,
    _leaves,
    _probe_crate,
    _reachable,
    build_manifest,
    cvlr_packages,
    document_family,
    documented_generations,
    gate_of,
    gated_sources,
    manifest_source,
    render_type,
    undocumented_report,
    writable_from,
)
from tests.conftest import CVLR_SOLANA_0_5_1_COMMIT, read_from_commit

_ASSERT_LE = Item(
    name="cvlr_assert_le",
    kind=ItemKind.MACRO,
    crate="cvlr-asserts",
    path="cvlr_asserts::cvlr_assert_le",
    signature="macro_rules! cvlr_assert_le {\n    ($lhs:expr, $rhs:expr) => { ... };\n}",
)
_IS_U128 = Item(
    name="is_u128",
    kind=ItemKind.FUNCTION,
    crate="cvlr-mathint",
    path="cvlr_mathint::is_u128",
    signature="fn is_u128(v: NativeIntU64) -> bool",
    docs="Whether `v` fits in a `u128`.",
    aliases=("cvlr::is_u128", "cvlr::mathint::is_u128"),
)
#: A block rustdoc would build, and one it would not.
_COMPILED_SOURCE = "let layout = 1;\n"
_WITH_EXAMPLE = Item(
    name="fun_acc_infos_with_mem_layout",
    kind=ItemKind.FUNCTION,
    crate="cvlr-solana",
    path="cvlr_solana::fun_acc_infos_with_mem_layout",
    signature="fn fun_acc_infos_with_mem_layout()",
    docs=f"Lead.\n\n```\n{_COMPILED_SOURCE}```\n",
)
_WITH_IGNORED = Item(
    name="cvlr_deserialize_nondet_accounts",
    kind=ItemKind.FUNCTION,
    crate="cvlr-solana",
    path="cvlr_solana::cvlr_deserialize_nondet_accounts",
    signature="fn cvlr_deserialize_nondet_accounts()",
    docs="```ignore\nlet accounts = cvlr_deserialize_nondet_accounts();\n```\n",
)


def _family(origin: Origin = Published()) -> list[DocumentedCrate]:
    return [
        DocumentedCrate("cvlr-asserts", "0.6.2", (_ASSERT_LE,), origin),
        DocumentedCrate("cvlr-mathint", "0.6.2", (_IS_U128,), origin),
        DocumentedCrate("cvlr-solana", "0.5.2", (_WITH_IGNORED, _WITH_EXAMPLE), origin),
    ]


def _passing(crates: list[DocumentedCrate]) -> ExampleGate:
    """The gate's verdict as if every example had compiled, for tests about everything else."""
    return ExampleGate(verdicts={source: Passed() for source in gated_sources(crates)})


def _section(manifest: RagManifest, crate: str, version: str, leaf: str) -> str:
    headers = ApiPath(crate, version, leaf).headers()
    section = next(s for s in manifest.manual_sections if list(s.headers) == headers)
    return "\n".join(b.body for b in section.blocks)


# ---------------------------------------------------------------------------------------------
# an entry


def test_an_alias_reaches_the_entry_body():
    # `cvlr_api_lookup` finds a name only a re-export spells by the entry's own text.
    body = "\n".join(text for _kind, text in _IS_U128.body())
    assert "Also reachable as `cvlr::is_u128`, `cvlr::mathint::is_u128`." in body


def test_an_associated_item_has_no_path_line():
    method = Item(
        name="NativeIntU64::u64_max", kind=ItemKind.FUNCTION, crate="cvlr-mathint", path=None,
        signature="fn u64_max() -> Self",
    )
    assert not any(text.startswith("Path:") for _kind, text in method.body())


def test_a_gate_is_stated_in_the_entry():
    gated = Item(
        name="x", kind=ItemKind.FUNCTION, crate="c", path="c::x", signature="fn x()",
        gate=FeatureGate("std"),
    )
    assert "Behind the `std` cargo feature" in "\n".join(t for _k, t in gated.body())


def test_an_item_only_an_undeclared_crate_spells_is_not_writable():
    declared = {"cvlr", "cvlr_solana"}
    unreachable = Item(
        name="SECTION", kind=ItemKind.CONSTANT, crate="cvlr-rule-manifest",
        path="cvlr_rule_manifest::SECTION", signature="const SECTION: &str",
    )
    assert not writable_from(unreachable, declared)
    assert writable_from(_IS_U128, declared), "the facade alias is writable"
    assert writable_from(Item("T::m", ItemKind.FUNCTION, "cvlr-x", None, "fn m()"), declared)


# ---------------------------------------------------------------------------------------------
# type rendering


@pytest.mark.parametrize(
    "node, expected",
    [
        ({"primitive": "u64"}, "u64"),
        ({"generic": "Self"}, "Self"),
        ({"resolved_path": {"path": "Pubkey", "args": None}}, "Pubkey"),
        ({"borrowed_ref": {"lifetime": "'a", "is_mutable": True, "type": {"primitive": "u8"}}},
         "&'a mut u8"),
        ({"slice": {"primitive": "u8"}}, "[u8]"),
        ({"array": {"type": {"primitive": "u8"}, "len": "32"}}, "[u8; 32]"),
        ({"tuple": []}, "()"),
        ({"tuple": [{"primitive": "u8"}, {"primitive": "bool"}]}, "(u8, bool)"),
        ({"raw_pointer": {"is_mutable": False, "type": {"primitive": "u8"}}}, "*const u8"),
    ],
)
def test_types_render_as_rust(node, expected):
    assert render_type(node) == expected


def test_an_unmodelled_type_elides_rather_than_guessing():
    # The caller is about to write this into Rust that has to compile, so a shape this does not
    # model becomes `_` rather than something plausible and wrong.
    assert render_type({"some_future_kind": {"whatever": 1}}) == "_"
    assert render_type(None) == "_"


# ---------------------------------------------------------------------------------------------
# the manifest


def test_the_signature_is_code_and_the_prose_is_not():
    # The importer holds code aside behind a <code-ref-N> placeholder so it stays out of the
    # keyword-searchable text. A signature mis-tagged as prose would pollute every keyword hit.
    family = _family()
    manifest = build_manifest(family, "test", _passing(family))
    headers = ApiPath("cvlr-asserts", "0.6.2", "cvlr_assert_le").headers()
    section = next(s for s in manifest.manual_sections if list(s.headers) == headers)
    code = [b for b in section.blocks if b.kind is ManualBlockKind.CODE]
    assert [b.body for b in code] == [_ASSERT_LE.signature]


def test_the_surface_lists_every_item_and_says_it_is_closed():
    family = _family()
    manifest = build_manifest(family, "test", _passing(family))
    body = _section(manifest, "cvlr-solana", "0.5.2", SURFACE_HEADING)
    assert "`cvlr_deserialize_nondet_accounts` — function" in body
    assert "`fun_acc_infos_with_mem_layout` — function" in body
    assert "2 items" in body
    assert "a name that is not on it is not in `cvlr-solana` 0.5.2" in body


# ---------------------------------------------------------------------------------------------
# the undocumented report


def test_the_report_lists_exactly_the_items_with_no_prose():
    """The corpus can only carry what the crates carry, and this is the ask against them.

    It has to be a list of *items*, not of doc-comment lines: what an agent hits is a specific
    name with nothing behind it, and the person fixing it needs that name.
    """
    family = _family()
    report = undocumented_report(family, _passing(family))
    assert "`cvlr_assert_le`" in report
    assert "`is_u128`" not in report


def test_the_report_counts_what_it_lists():
    family = _family()
    report = undocumented_report(family, _passing(family))
    assert "1 of 4 items carry no prose documentation." in report


def test_the_report_carries_the_signature_of_each_missing_item():
    # Whoever writes the doc comment needs to see what they are documenting without opening the
    # crate; a bare name would send them looking for it.
    family = _family()
    report = undocumented_report(family, _passing(family))
    assert _ASSERT_LE.signature.splitlines()[0] in report


# ---------------------------------------------------------------------------------------------
# header paths have to be unique
#
# The manual product keys on the header parts (``parts_unique`` in the importer's schema), and a
# crate exporting a macro and the function it expands into is ordinary CVLR: ``cvlr-log`` does it,
# ``cvlr-spec`` does it four times, and ``cvlr-solana-stake`` exports two functions of one name.
# Duplicate leaves would shadow each other, and the ingest would die at the database with a
# constraint error naming no item.


def _item(name: str, kind: ItemKind, path: str) -> Item:
    return Item(name=name, kind=kind, crate="cvlr-log", path=path, signature=f"{kind} {name}")


def test_one_name_for_two_items_is_told_apart_by_kind():
    # Which is the distinction in every real case: the macro and the function it expands into.
    leaves = _leaves([
        _item("cvlr_log", ItemKind.MACRO, "cvlr_log::cvlr_log"),
        _item("cvlr_log", ItemKind.FUNCTION, "cvlr_log::log::cvlr_log"),
    ])
    assert leaves == ["cvlr_log (macro)", "cvlr_log (function)"]


def test_one_name_and_one_kind_fall_back_to_the_defining_path():
    # ``cvlr-solana-stake`` exports `get_minimum_delegation` twice, both functions.
    leaves = _leaves([
        _item("get_minimum_delegation", ItemKind.FUNCTION, "cvlr_solana_stake::get_minimum_delegation"),
        _item("get_minimum_delegation", ItemKind.FUNCTION, "cvlr_solana_stake::tools::get_minimum_delegation"),
    ])
    assert leaves == [
        "cvlr_solana_stake::get_minimum_delegation",
        "cvlr_solana_stake::tools::get_minimum_delegation",
    ]


def test_a_name_that_is_already_unique_is_left_alone():
    """Almost every entry, and the reason the qualifier is not applied uniformly.

    An agent reaches these sections by name; putting a parenthesis in the middle of every header
    in the corpus to accommodate a few collisions would make the common case read worse.
    """
    items = [_item("cvlr_log", ItemKind.MACRO, "cvlr_log::cvlr_log"), _item("log_u64", ItemKind.FUNCTION, "p")]
    assert _leaves(items) == ["cvlr_log", "log_u64"]


def test_indistinguishable_items_are_refused_rather_than_dropped():
    # Same name, same kind, same path: there is nothing left to key on, and silently keeping one
    # of the two is the outcome nobody could detect.
    with pytest.raises(RustdocUnavailable, match="overwrite each other"):
        _leaves([_item("x", ItemKind.FUNCTION, "p::x"), _item("x", ItemKind.FUNCTION, "p::x")])


# ---------------------------------------------------------------------------------------------
# documenting a local checkout


def _checkout(name: str) -> LocalCheckout:
    root = Path("/home/eric/src/cvlr")
    return LocalCheckout(
        root=root, crate_dir=root / name, version="0.6.1", revision="cvlr-v0.6.1-3-g8d9d463-dirty"
    )


def test_a_checkout_enters_as_a_patch_and_not_as_a_dependency(tmp_path):
    """Which is what keeps the pin and the source apart.

    A path *dependency* would let a checkout decide the version too, and a corpus describing a
    release this build does not support is exactly what the pin exists to prevent. As a patch, a
    checkout that has moved off the pin fails at cargo instead.
    """
    from composer.spec.cvlr.reference import SOLANA_V2

    _probe_crate(tmp_path, SOLANA_V2, {"cvlr-asserts": _checkout("cvlr-asserts")})
    manifest = (tmp_path / "Cargo.toml").read_text()
    deps, patch = manifest.split("[patch.crates-io]")
    assert 'cvlr = "=0.6.2"' in deps
    assert "path" not in deps
    assert 'cvlr-asserts = { path = "/home/eric/src/cvlr/cvlr-asserts" }' in patch


def test_a_probe_with_no_checkouts_patches_only_the_unpublished_releases(tmp_path):
    """The corpus documents the release a run builds, and for an unpublished one that is its
    commit, not whatever crates.io has under an older version."""
    from composer.spec.cvlr.reference import SOLANA_V2

    unpublished = read_from_commit(SOLANA_V2, CVLR_SOLANA_0_5_1_COMMIT)

    _probe_crate(tmp_path, unpublished, {})
    _, patch = (tmp_path / "Cargo.toml").read_text().split("[patch.crates-io]")
    assert {line.split(" = ")[0] for line in patch.strip().splitlines()} == {
        p.name for p in unpublished.from_commits()
    }
    for p in unpublished.from_commits():
        assert p.commit.patch_line(p.name) in patch


def test_a_checkout_replaces_the_commit_for_the_crates_it_defines(tmp_path):
    from composer.spec.cvlr.reference import SOLANA_V2

    unpublished = read_from_commit(SOLANA_V2, CVLR_SOLANA_0_5_1_COMMIT)

    root = Path("/home/eric/src/cvlr-solana")
    checkout = LocalCheckout(
        root=root, crate_dir=root / "cvlr-solana", version="0.5.1", revision="v0.5.1"
    )
    _probe_crate(tmp_path, unpublished, {"cvlr-solana": checkout})
    _, patch = (tmp_path / "Cargo.toml").read_text().split("[patch.crates-io]")
    assert 'cvlr-solana = { path = "/home/eric/src/cvlr-solana/cvlr-solana" }' in patch
    assert "cvlr-spl-token = { git" in patch
    assert patch.count("cvlr-solana = ") == 1


def test_a_checkout_that_defines_no_cvlr_crate_is_refused(tmp_path):
    """The one failure a caller cannot see: the build finishes, describing the published releases.

    It is the same argument as :meth:`ChainReference.withholding`'s — a setting whose only effect
    is "changed nothing" is indistinguishable from one that worked.
    """
    metadata = CargoMetadataJson.model_validate({
        "packages": [{
            "id": "program", "name": "solana-program", "version": "2.0.0",
            "manifest_path": f"{tmp_path}/Cargo.toml",
        }],
        "workspace_root": str(tmp_path),
        "workspace_members": ["program"],
    })
    with pytest.raises(RustdocUnavailable, match="defines no CVLR crate"):
        cvlr_packages(parse_metadata(metadata))


def test_a_package_resolves_to_its_own_directory_not_the_workspace_root():
    # What `[patch]` has to name. A workspace root would patch in the wrong crate, or nothing.
    metadata = CargoMetadataJson.model_validate({
        "packages": [
            {
                "id": "asserts", "name": "cvlr-asserts", "version": "0.6.2",
                "manifest_path": "/src/cvlr/cvlr-asserts/Cargo.toml",
            },
            {
                "id": "serde", "name": "serde", "version": "1.0.0",
                "manifest_path": "/src/cvlr/serde/Cargo.toml",
            },
        ],
        "workspace_root": "/src/cvlr",
        "workspace_members": ["asserts", "serde"],
    })
    (found,) = cvlr_packages(parse_metadata(metadata))
    assert (found.name, found.root) == ("cvlr-asserts", Path("/src/cvlr/cvlr-asserts"))


def test_a_checkout_corpus_is_indistinguishable_from_a_published_one():
    """The rows must not mention where they came from, and this is the reason.

    A checkout is documented in order to be *tried*: to see how an author agent works with
    documentation still in review. A caveat no released corpus carries would make the agent's
    behaviour differ from what the released corpus produces.

    Provenance is recorded where no agent reads it — the manifest's source line and the log.
    """
    published = _family()
    checkout = _family(origin=_checkout("cvlr-asserts"))
    built = build_manifest(checkout, "s", _passing(checkout))
    assert built == build_manifest(published, "s", _passing(published))
    assert "/home/eric/src" not in built.model_dump_json()


def test_the_manifest_source_names_every_checkout_it_read():
    """What someone looking at an ingested corpus and wondering why it disagrees with crates.io has.

    Grouped by checkout rather than listed per crate: fifteen crates from two trees is two facts.
    It is also the only durable trace, since the rows carry none.
    """
    from composer.spec.cvlr.reference import SOLANA_V2

    unpublished = read_from_commit(SOLANA_V2, CVLR_SOLANA_0_5_1_COMMIT)

    assert "checkout" not in manifest_source(unpublished, {})
    commit = unpublished.from_commits()[0].commit
    assert f"{commit.repo} @ {commit.rev[:12]}" in manifest_source(unpublished, {})
    source = manifest_source(
        unpublished, {"cvlr-asserts": _checkout("cvlr-asserts"), "cvlr-log": _checkout("cvlr-log")}
    )
    assert "cvlr 0.6.2" in source
    assert source.count("/home/eric/src/cvlr @ cvlr-v0.6.1-3-g8d9d463-dirty") == 1
    assert "cvlr-asserts, cvlr-log" in source


def test_the_undocumented_report_records_that_it_measured_a_checkout():
    # The list is meant to shrink, and it shrinks in a checkout first. A report that did not say
    # which tree it counted would read as a measurement of the published crates.
    family = _family(origin=_checkout("cvlr-asserts"))
    report = undocumented_report(family, _passing(family))
    assert "local checkouts" in report
    assert "/home/eric/src/cvlr @ cvlr-v0.6.1-3-g8d9d463-dirty" in report


# ---------------------------------------------------------------------------------------------
# the path a caller writes
#
# rustdoc's `paths` gives the path an item is *defined* at, and the CVLR house style is a private
# `mod` plus a public re-export at the crate root: `mod layout;` + `pub use layout::{...}`. The
# definition path then names a private module, and an author who writes it gets
# `error[E0603]: module `layout` is private`.


def test_a_path_through_a_private_module_is_not_writable():
    public = {"cvlr_solana::pubkey": True, "cvlr_solana::layout": False}
    assert _reachable("cvlr_solana::cvlr_deserialize_nondet_accounts", public)
    assert _reachable("cvlr_solana::pubkey::pubkey_new", public)
    assert not _reachable("cvlr_solana::layout::cvlr_deserialize_nondet_accounts", public)


def test_a_checkout_of_one_release_line_reaches_only_its_generation():
    """Patched into the other generation's probe, an SDK 3 cvlr-solana checkout fails at cargo."""
    from composer.scripts.cvlr_api_docs import sources_for
    from composer.spec.cvlr.reference import SOLANA_V2, SOLANA_V3

    root = Path("/home/eric/src/cvlr-solana")
    sdk3 = LocalCheckout(
        root=root,
        crate_dir=root / "cvlr-solana",
        version=SOLANA_V3.binding.version,
        revision="x",
    )
    core = _checkout("cvlr-asserts")
    sources = {"cvlr-solana": sdk3, "cvlr-asserts": core}
    assert sources_for(SOLANA_V3, sources) == sources
    assert sources_for(SOLANA_V2, sources) == {"cvlr-asserts": core}


# ---------------------------------------------------------------------------------------------
# the examples
#
# The verdicts come from `cargo check` (tests/test_cargo_doctest.py runs it for real); these pin
# what the corpus does with a verdict, which needs no cargo.


def test_only_blocks_rustdoc_would_build_are_sent_to_the_compiler():
    gated = gated_sources(_family())
    assert gated == {_COMPILED_SOURCE: ["cvlr-solana::fun_acc_infos_with_mem_layout"]}


_PROSE = "Lead.\n\n```rust\n# use cvlr::prelude::*;\nlet x = 1;\n```\n\nAfter.\n"


def test_a_compiled_example_is_labelled_and_shows_what_was_compiled():
    gate = ExampleGate(verdicts={"use cvlr::prelude::*;\nlet x = 1;\n": Passed()})
    rendered = gate.render(_PROSE)
    assert (
        "_Compiled against the pinned crates:_\n```rust\nuse cvlr::prelude::*;\nlet x = 1;\n```\n"
        in rendered
    )
    assert rendered.startswith("Lead.\n\n") and rendered.endswith("\n\nAfter.\n")


def test_a_failed_example_is_removed_and_the_removal_said():
    gate = ExampleGate(verdicts={"use cvlr::prelude::*;\nlet x = 1;\n": Failed("error[E0425]")})
    rendered = gate.render(_PROSE)
    assert "let x = 1" not in rendered
    assert "did not compile against the pinned crates" in rendered
    assert rendered.endswith("\n\nAfter.\n")


@pytest.mark.parametrize("info", ["ignore", "rust,ignore", "text", "compile_fail"])
def test_a_block_that_was_not_compiled_says_so(info):
    docs = f"```{info}\nlet x = ;\n```\n"
    rendered = ExampleGate(verdicts={}).render(docs)
    assert f"_Not compiled (marked `{info}` upstream)" in rendered
    assert f"```{info}\nlet x = ;\n```" in rendered
    assert "_Compiled" not in rendered


def test_an_example_with_no_verdict_is_refused_rather_than_shown():
    from composer.cargo.doctest import GateUnavailable

    with pytest.raises(GateUnavailable):
        ExampleGate(verdicts={}).render("```\nlet x = 1;\n```\n")


def test_the_corpus_carries_the_gate_s_labels():
    family = _family()
    manifest = build_manifest(
        family, "test", ExampleGate(verdicts={_COMPILED_SOURCE: Failed("error")})
    )
    prose = _section(manifest, "cvlr-solana", "0.5.2", "fun_acc_infos_with_mem_layout")
    assert "let layout" not in prose
    assert "did not compile" in prose
    ignored = _section(manifest, "cvlr-solana", "0.5.2", "cvlr_deserialize_nondet_accounts")
    assert "_Not compiled (marked `ignore` upstream)" in ignored


def test_the_report_names_each_dropped_example_and_where_it_was():
    family = _family()
    gate = ExampleGate(
        verdicts={_COMPILED_SOURCE: Failed("\nerror[E0433]: failed to resolve\n  --> x.rs")},
        owners=gated_sources(family),
    )
    report = undocumented_report(family, gate)
    assert "Examples that do not compile — 1 of 1" in report
    assert (
        "- `cvlr-solana::fun_acc_infos_with_mem_layout`: `error[E0433]: failed to resolve`"
        in report
    )
    assert "Examples that do not compile" not in undocumented_report(family, _passing(family))


# ---------------------------------------------------------------------------------------------
# what an item's attributes say


@pytest.mark.parametrize(
    ("attr", "gate"),
    [
        ('#[cfg(feature = "std")]', FeatureGate("std")),
        ('#[doc(cfg(feature = "std"))]', FeatureGate("std")),
        ({"other": '#[cfg(feature = "std")]'}, FeatureGate("std")),
        # Naming one of these features would tell the reader the wrong thing to turn on.
        ('#[cfg(any(feature = "a", feature = "b"))]', CfgGate('any(feature = "a", feature = "b")')),
        ('#[cfg(all(feature = "a", unix))]', CfgGate('all(feature = "a", unix)')),
        # Not a gate on the item existing.
        ('#[cfg_attr(feature = "a", derive(Debug))]', None),
        ("macro_export", None),
    ],
)
def test_a_cfg_is_reported_as_the_gate_it_is(attr, gate):
    assert gate_of([attr]) == gate





# ---------------------------------------------------------------------------------------------
# against the pinned crates
#
# The corpus's central promise is the path: an entry says "write this", and an author does. The
# producer cannot check that against itself, so this asks cargo. Each Solana generation is
# documented for real, and every path and alias it offers is compiled as a `use` in a crate with
# exactly the reference set's dependencies, which is what a scaffolded project declares.


def _candidates(item: Item) -> list[str]:
    return [p for p in (item.path, *item.aliases) if p is not None]


@pytest.mark.expensive
def test_every_documented_path_compiles_in_a_scaffolded_project(tmp_path):
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    for n, reference in enumerate(documented_generations("solana")):
        probe = tmp_path / f"generation-{n}"
        probe.mkdir()
        crates = document_family(reference, probe, fetch=True)
        project = tmp_path / f"project-{n}"
        _probe_crate(project, reference, {})
        shutil.copyfile(probe / "Cargo.lock", project / "Cargo.lock")

        # An associated item is reached through its type, and a gated one needs a feature the
        # scaffold does not turn on.
        items = [i for c in crates for i in c.items if i.path is not None and i.gate is None]
        paths = sorted({p for item in items for p in _candidates(item)})
        verdicts = dict(zip(paths, check_examples(project, [f"use {p};\n" for p in paths])))

        unwritable = [
            f"{item.crate}::{item.name}: {_candidates(item)}"
            for item in items
            if not any(isinstance(verdicts[p], Passed) for p in _candidates(item))
        ]
        assert not unwritable, f"{reference.line()}: no path compiles for\n" + "\n".join(unwritable)

        # `render_type` writes `_` for a type shape it does not model; the pinned crates have none.
        elided = [
            f"{i.crate}::{i.name}: {i.signature}"
            for c in crates for i in c.items
            if i.kind is not ItemKind.MACRO and re.search(r"(?<![\w'])_(?!\w)", i.signature)
        ]
        assert not elided, f"{reference.line()}: types rendered as `_`:\n" + "\n".join(elided)

        by_name = {(i.crate, i.name): i for c in crates for i in c.items}
        # A derive is written as a derive, which rustdoc's index does not say on its own.
        assert by_name["cvlr-derive", "Nondet"].signature == "#[derive(Nondet)]"
        # An inherent method has no path of its own, and is emitted under its type.
        u64_max = by_name["cvlr-mathint", "NativeIntU64::u64_max"]
        assert u64_max.path is None and u64_max.signature == "fn u64_max() -> Self"
        # A macro carries its matchers and not its expansion.
        assert_le = by_name["cvlr-asserts", "cvlr_assert_le"].signature
        assert assert_le.startswith("macro_rules! cvlr_assert_le") and "{ ... }" in assert_le
