"""Directory sweeping for ``--extra-context``.

A ``--extra-context`` entry is either a document or a directory to sweep. The resolved
list feeds the prompt and the bug-analysis cache key, and ``cache-autoprove inputs``
re-runs the same resolver to rebuild that key, so it must be deterministic and
reproducible from the CLI arguments alone. The sweep is deliberately flat.
"""

import pathlib

import pytest

from composer.input.files import discover_documents, resolve_document_paths


def _tree(root: pathlib.Path, *relative: str) -> None:
    for rel in relative:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"contents of {rel}\n")


def test_sweep_is_flat_and_sorted_by_name(tmp_path: pathlib.Path) -> None:
    _tree(tmp_path, "b.md", "a.md", "nested/deep/z.txt", "nested/a.md")
    # Not recursive: nested/ is invisible to the sweep.
    assert [p.name for p in discover_documents(tmp_path)] == ["a.md", "b.md"]


def test_filters_by_suffix_case_insensitively(tmp_path: pathlib.Path) -> None:
    _tree(tmp_path, "notes.MD", "scope.Pdf", "readme.rst", "data.json", "Vault.sol")
    got = {p.name for p in discover_documents(tmp_path)}
    # Source and config are deliberately excluded from a sweep, even though the
    # uploader would happily read them if named explicitly.
    assert got == {"notes.MD", "scope.Pdf", "readme.rst"}


def test_skips_hidden_files(tmp_path: pathlib.Path) -> None:
    _tree(tmp_path, "keep.md", ".hidden.md")
    assert [p.name for p in discover_documents(tmp_path)] == ["keep.md"]


def test_empty_sweep_returns_empty_list(tmp_path: pathlib.Path) -> None:
    _tree(tmp_path, "Vault.sol", "nested/a.md")
    assert discover_documents(tmp_path) == []


def test_non_directory_is_an_error(tmp_path: pathlib.Path) -> None:
    _tree(tmp_path, "a.md")
    with pytest.raises(ValueError, match="not a directory"):
        discover_documents(tmp_path / "a.md")
    with pytest.raises(ValueError, match="not a directory"):
        discover_documents(tmp_path / "nope")


# --- resolve_document_paths ------------------------------------------------------------

def test_entries_keep_their_order_with_sweeps_spliced_in_place(tmp_path: pathlib.Path) -> None:
    _tree(tmp_path, "z.md", "a.md", "swept/m.md", "swept/b.md")
    got = resolve_document_paths([
        str(tmp_path / "z.md"), str(tmp_path / "swept"), str(tmp_path / "a.md"),
    ])
    assert [p.name for p in got] == ["z.md", "b.md", "m.md", "a.md"]


def test_no_entries_is_empty() -> None:
    assert resolve_document_paths(None) == []
    assert resolve_document_paths([]) == []


def test_named_file_is_not_suffix_filtered(tmp_path: pathlib.Path) -> None:
    # A named file bypasses DOCUMENT_SUFFIXES — that filter is a sweep policy only.
    _tree(tmp_path, "Vault.sol")
    assert [p.name for p in resolve_document_paths([str(tmp_path / "Vault.sol")])] == [
        "Vault.sol"
    ]


def test_missing_entry_is_an_error(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="no such file or directory"):
        resolve_document_paths([str(tmp_path / "nope.md")])


def test_directory_with_no_documents_is_an_error(tmp_path: pathlib.Path) -> None:
    _tree(tmp_path, "empty/Vault.sol")
    with pytest.raises(ValueError, match="no supported documents"):
        resolve_document_paths([str(tmp_path / "empty")])
