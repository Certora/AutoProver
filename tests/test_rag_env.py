"""The descriptor-driven RAG corpus registry (``composer.tools.rag_env``).

A wheel names a corpus by tag (``rag_db_default``); a tag is usable only when *both* halves are
registered — its connection in ``composer.rag.db.KNOWLEDGE_BASES`` and its search-tool factory in
``rag_env._FACTORIES``. The two failure modes are deliberately opposite, and these pin that:

* an **unregistered tag** is a repo/wheel bug — it raises, at descriptor load, before the run
  spends anything;
* an **unavailable corpus** (DB down, embedding model missing) is an environment condition — the
  run continues with no RAG surface, because a search aid must never fail a run.

``cvlr_kb`` is registered for real; tests that need an *arbitrary* corpus still register a stub,
which doubles as the executable spec for adding one: two entries, in two maps.
"""

import pytest

from composer.rag import db as rag_db
from composer.tools import rag_env


def _register(monkeypatch: pytest.MonkeyPatch, tag: str, factory) -> None:
    """Register both halves of a corpus for one test — the module-level tables are the registry,
    so a test corpus goes in the same way a real one would."""
    monkeypatch.setitem(rag_db.KNOWLEDGE_BASES, tag, "postgresql://stub/rag_db")
    monkeypatch.setitem(rag_env._FACTORIES, tag, factory)


def test_a_wheel_that_declares_no_corpus_is_fine():
    assert rag_env.validate_rag_db(None) is None


def test_an_unregistered_tag_raises_and_says_where_to_register_it():
    with pytest.raises(ValueError, match="not a registered RAG corpus") as e:
        rag_env.validate_rag_db("no_such_kb")
    msg = str(e.value)
    assert "KNOWLEDGE_BASES" in msg and "rag_env" in msg


def test_a_really_registered_corpus_validates():
    # Not a stub: `cvlr_kb` is registered in both maps for real, and this is the guard against a
    # half-registration landing (the trap the next test describes) during a refactor.
    assert rag_env.validate_rag_db("cvlr_kb") is None


def test_the_registered_corpora_are_listed_when_a_tag_is_unknown(monkeypatch: pytest.MonkeyPatch):
    msg = str(pytest.raises(ValueError, rag_env.validate_rag_db, "no_such_kb").value)
    assert "known: ['cvlr_kb']" in msg


def test_the_message_says_so_when_nothing_is_registered_at_all(monkeypatch: pytest.MonkeyPatch):
    # "known: []" would read as a lookup failure against a populated registry rather than as an
    # empty one, so the empty case gets its own wording. Emptying the maps is how that case is
    # reached now that a corpus is registered.
    monkeypatch.setattr(rag_db, "KNOWLEDGE_BASES", {})
    monkeypatch.setattr(rag_env, "_FACTORIES", {})
    assert "none is registered yet" in str(
        pytest.raises(ValueError, rag_env.validate_rag_db, "no_such_kb").value
    )


def test_half_a_registration_is_not_a_corpus(monkeypatch: pytest.MonkeyPatch):
    # Tools but no connection: nothing to search. A half-registration must fail validation, not
    # validate and then silently produce no tools.
    monkeypatch.setitem(rag_env._FACTORIES, "tools_only", lambda _db: ())
    with pytest.raises(ValueError, match="not a registered RAG corpus"):
        rag_env.validate_rag_db("tools_only")

    # …and a connection with no tools factory is just as unusable.
    monkeypatch.setitem(rag_db.KNOWLEDGE_BASES, "conn_only", "postgresql://stub/rag_db")
    with pytest.raises(ValueError, match="not a registered RAG corpus"):
        rag_env.validate_rag_db("conn_only")


def test_a_registered_tag_validates(monkeypatch: pytest.MonkeyPatch):
    _register(monkeypatch, "stub_kb", lambda _db: ())
    assert rag_env.validate_rag_db("stub_kb") is None


def test_building_tools_for_an_unregistered_tag_raises_rather_than_degrading():
    # Not caught by the degrade path: nothing about the environment would make that corpus appear.
    with pytest.raises(ValueError, match="not a registered RAG corpus"):
        rag_env.build_rag_tools("no_such_kb")


def test_an_unavailable_corpus_degrades_to_no_rag(monkeypatch: pytest.MonkeyPatch, caplog):
    def explodes(_db):
        raise RuntimeError("connection refused")

    _register(monkeypatch, "stub_kb", explodes)
    # Stub the embedder: loading a real sentence-transformers model costs seconds, and this test is
    # about what happens *after* the corpus is opened. Without this the factory below is never
    # reached on a machine with no model installed, and the test would pass for the wrong reason.
    monkeypatch.setattr("composer.rag.models.get_model", lambda: None)
    with caplog.at_level("WARNING"):
        assert rag_env.build_rag_tools("stub_kb") == ()
    assert "unavailable" in caplog.text
    assert "connection refused" in caplog.text  # the factory's failure, not the embedder's


def test_the_cvlr_factory_binds_its_three_tools():
    """The registered factory is only useful if it actually produces tools: a tag can validate
    (both maps populated) while its tools module fails to import or its bindings are wrong, and
    ``build_rag_tools`` would swallow that as an "unavailable corpus" and degrade silently. Binding
    is lazy, so no database is needed to check the wiring."""
    factory = rag_env._FACTORIES["cvlr_kb"]
    names = {t.name for t in factory(object())}  # type: ignore[arg-type]  # bind() is lazy
    assert names == {"cvlr_get_section", "cvlr_manual_search", "cvlr_keyword_search"}
