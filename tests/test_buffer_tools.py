"""Smoke + java-free logic tests for the multi-buffer authoring tools."""

from langchain_core.tools import BaseTool

from composer.spec.source.buffer_tools import (
    WithBuffers,
    delete_buffer,
    edit_buffer,
    get_buffer,
    list_buffers,
    put_buffer,
)
from composer.spec.source.spec_buffers import NamedBuffer


def _state():
    return {
        "buffers": {
            "shared": NamedBuffer(name="shared", cvl="ghost g(uint) returns uint;\n", is_run_target=False),
            "easy": NamedBuffer(
                name="easy", cvl='import "shared.spec";\nrule r_easy { assert true; }\n',
                property_rules={"P-easy": ["r_easy"]},
            ),
        }
    }


def test_all_factories_build():
    for factory in (put_buffer, edit_buffer, get_buffer, list_buffers, delete_buffer):
        assert isinstance(factory(WithBuffers), BaseTool)


def test_get_buffer_returns_text_or_message():
    tool = get_buffer(WithBuffers)
    assert "r_easy" in tool.invoke({"state": _state(), "name": "easy"})
    assert "No buffer" in tool.invoke({"state": _state(), "name": "nope"})


def test_list_buffers_reports_kind_imports_and_rulecount():
    out = list_buffers(WithBuffers).invoke({"state": _state()})
    assert "easy (run-target, 1 rules" in out
    assert "imports ['shared']" in out
    assert "shared (shared, 0 rules)" in out


def test_list_buffers_empty():
    assert "No spec buffers" in list_buffers(WithBuffers).invoke({"state": {"buffers": {}}})


def _invoke_msg(tool, tool_name, state, **args) -> str:
    res = tool.invoke({"name": tool_name, "args": {"state": state, **args}, "id": "t", "type": "tool_call"})
    if hasattr(res, "content"):
        return str(res.content)
    msgs = res.update.get("messages", []) if hasattr(res, "update") else []
    return str(msgs[0].content) if msgs else ""


def test_put_buffer_warns_on_cross_buffer_duplicate_of_this_buffer(monkeypatch):
    """Writing a run-target buffer whose declaration already lives in another run-target is flagged at
    authoring time (before proving), naming only the current buffer's duplicates."""
    monkeypatch.setattr("composer.spec.source.buffer_tools.cvl_syntax_error", lambda *a, **k: None)
    state = {"buffers": {
        "easy": NamedBuffer(
            name="easy", cvl="ghost dup(uint) returns uint;\nrule r_easy { assert true; }\n",
            property_rules={"P-easy": ["r_easy"]},
        ),
    }}
    msg = _invoke_msg(put_buffer(WithBuffers), "put_buffer", state,
                      name="hard", cvl="ghost dup(uint) returns uint;\nrule r_hard { assert true; }\n",
                      is_run_target=True, imports=[], property_rules={"P-hard": ["r_hard"]})
    assert "NOTE" in msg and "ghost dup(uint) returns uint;" in msg and "easy" in msg


def test_put_buffer_no_warning_without_duplicate(monkeypatch):
    """A buffer that duplicates nothing is accepted with no note."""
    monkeypatch.setattr("composer.spec.source.buffer_tools.cvl_syntax_error", lambda *a, **k: None)
    msg = _invoke_msg(put_buffer(WithBuffers), "put_buffer", _state(),
                      name="hard", cvl="rule r_hard { assert true; }\n",
                      is_run_target=True, imports=[], property_rules={"P-hard": ["r_hard"]})
    assert msg == "Accepted"


def test_edit_buffer_warns_when_edit_introduces_duplicate(monkeypatch):
    """Editing a buffer to add a declaration another run-target already has is flagged at edit time."""
    monkeypatch.setattr("composer.spec.source.buffer_tools.cvl_syntax_error", lambda *a, **k: None)
    state = {"buffers": {
        "easy": NamedBuffer(
            name="easy", cvl="ghost dup(uint) returns uint;\nrule r_easy { assert true; }\n",
            property_rules={"P-easy": ["r_easy"]},
        ),
        "hard": NamedBuffer(
            name="hard", cvl="rule r_hard { assert true; }\n", property_rules={"P-hard": ["r_hard"]},
        ),
    }}
    msg = _invoke_msg(edit_buffer(WithBuffers), "edit_buffer", state, name="hard",
                      old_string="rule r_hard", new_string="ghost dup(uint) returns uint;\nrule r_hard")
    assert "NOTE" in msg and "easy" in msg


def test_put_buffer_enforces_run_target_cap(monkeypatch):
    # put_buffer validates via the CVL typechecker jar (absent in CI) before the cap check; bypass it.
    monkeypatch.setattr("composer.spec.source.buffer_tools.cvl_syntax_error", lambda *a, **k: None)
    monkeypatch.setenv("AUTOPROVER_MAX_SPEC_BUFFERS", "2")
    tool = put_buffer(WithBuffers)
    state = {"buffers": {
        "a": NamedBuffer(name="a", cvl="rule ra { assert true; }\n", property_rules={"P-a": ["ra"]}),
        "b": NamedBuffer(name="b", cvl="rule rb { assert true; }\n", property_rules={"P-b": ["rb"]}),
    }}

    def put_msg(**args) -> str:
        # Invoke via the tool-call form so InjectedToolCallId is supplied; normalize str/Command result.
        res = tool.invoke({"name": "put_buffer", "args": {"state": state, **args},
                           "id": "t", "type": "tool_call"})
        if hasattr(res, "content"):
            return str(res.content)
        msgs = res.update.get("messages", []) if hasattr(res, "update") else []
        return str(msgs[0].content) if msgs else ""

    # a third NEW run-target exceeds the cap of 2 -> rejected
    assert "cap" in put_msg(name="c", cvl="rule rc { assert true; }\n",
                            property_rules={"P-c": ["rc"]}, is_run_target=True)
    # re-putting an EXISTING run-target is always allowed (not a new one)
    assert "cap" not in put_msg(name="a", cvl="rule ra { assert true; }\n// edit\n",
                                property_rules={"P-a": ["ra"]}, is_run_target=True)
    # a new SHARED buffer (is_run_target=false) is exempt from the cap
    assert "cap" not in put_msg(name="shared", cvl="ghost g(uint) returns uint;\n",
                                property_rules={}, is_run_target=False)
