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
                name="easy", cvl="rule r_easy { assert true; }\n",
                property_rules={"P-easy": ["r_easy"]}, imports=("shared",),
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
                            property_rules={"P-c": ["rc"]}, imports=[], is_run_target=True)
    # re-putting an EXISTING run-target is always allowed (not a new one)
    assert "cap" not in put_msg(name="a", cvl="rule ra { assert true; }\n// edit\n",
                                property_rules={"P-a": ["ra"]}, imports=[], is_run_target=True)
    # a new SHARED buffer (is_run_target=false) is exempt from the cap
    assert "cap" not in put_msg(name="shared", cvl="ghost g(uint) returns uint;\n",
                                property_rules={}, imports=[], is_run_target=False)
