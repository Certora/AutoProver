"""The descriptor-driven RAG corpus registry (``composer.tools.rag_env``).

A wheel names a corpus by tag (``rag_db_default``); a tag is usable only when *both* halves are
registered — its connection in ``composer.rag.db.KNOWLEDGE_BASES`` and its search-tool factory in
``rag_env._FACTORIES``. The two failure modes are deliberately opposite, and these pin that:

* an **unregistered tag** is a repo/wheel bug — it raises, at descriptor load, before the run
  spends anything;
* an **unavailable corpus** (DB down, embedding model missing) is an environment condition — the
  run continues with no RAG surface, because a search aid must never fail a run.

Tests that need a corpus register a stub rather than leaning on the real one, so they pin the seam
and not ``crucible_kb``'s particulars. That stub is also the executable spec for adding a real
corpus: two entries, in two maps.
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


def test_the_message_names_the_registered_corpora_it_knows_about():
    assert "known: ['crucible_kb']" in str(
        pytest.raises(ValueError, rag_env.validate_rag_db, "no_such_kb").value
    )


def test_the_message_says_so_when_nothing_is_registered_at_all(monkeypatch: pytest.MonkeyPatch):
    # With an empty registry the "known: []" wording would read as a lookup failure, so the message
    # switches. Emptied explicitly — this used to be the branch's resting state.
    monkeypatch.setattr(rag_env, "_FACTORIES", {})
    monkeypatch.setattr(rag_db, "KNOWLEDGE_BASES", {})
    assert "none is registered yet" in str(
        pytest.raises(ValueError, rag_env.validate_rag_db, "no_such_kb").value
    )


def test_the_crucible_corpus_is_registered_in_both_halves():
    # The wheel declares `rag_db_default='crucible_kb'`; both halves must be present or
    # `build_application` refuses to load the descriptor at all.
    assert rag_env.validate_rag_db("crucible_kb") is None


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
