"""Smoke + java-free logic tests for the multi-buffer authoring tools."""

from langchain_core.tools import BaseTool

from composer.spec.source.buffer_tools import (
    WithBuffers,
    delete_buffer,
    edit_buffer,
    get_buffer,
    list_buffers,
    put_buffer,
    remap_buffer,
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
    for factory in (put_buffer, edit_buffer, remap_buffer, get_buffer, list_buffers, delete_buffer):
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


def test_remap_buffer_replaces_mapping_and_keeps_cvl():
    """remap_buffer swaps a run-target buffer's property->rule mapping, leaving the CVL untouched."""
    cvl = "rule a { assert true; }\nrule b { assert true; }\n"
    state = {"buffers": {"easy": NamedBuffer(name="easy", cvl=cvl, property_rules={"P": ["a"]})}}
    res = remap_buffer(WithBuffers).invoke(
        {"name": "remap_buffer", "id": "t", "type": "tool_call",
         "args": {"state": state, "name": "easy", "property_rules": {"P": ["a", "b"]}}},
    )
    buf = res.update["buffers"]["easy"]
    assert buf.property_rules == {"P": ["a", "b"]}
    assert buf.cvl == cvl


def test_remap_buffer_rejects_missing_shared_and_empty():
    tool = remap_buffer(WithBuffers)
    assert "No buffer" in _invoke_msg(tool, "remap_buffer", {"buffers": {}},
                                      name="x", property_rules={"P": ["r"]})
    shared = {"buffers": {"s": NamedBuffer(name="s", cvl="ghost g(uint) returns uint;\n",
                                           is_run_target=False)}}
    assert "shared" in _invoke_msg(tool, "remap_buffer", shared, name="s", property_rules={"P": ["r"]})
    rt = {"buffers": {"e": NamedBuffer(name="e", cvl="rule r { assert true; }\n",
                                       property_rules={"P": ["r"]})}}
    assert "empty mapping" in _invoke_msg(tool, "remap_buffer", rt, name="e", property_rules={})


def test_put_buffer_rejects_names_that_escape_the_component_dir(monkeypatch):
    # A buffer's name is its on-disk stem under certora/specs/<component>/; a name that climbs out with
    # '..' or is absolute is rejected, keeping the shared summaries and other components untouchable. A
    # plain subdir stays inside the component dir and is accepted.
    monkeypatch.setattr("composer.spec.source.buffer_tools.cvl_syntax_error", lambda *a, **k: None)
    tool = put_buffer(WithBuffers)

    def put_msg(name: str) -> str:
        res = tool.invoke({"name": "put_buffer",
                           "args": {"state": {"buffers": {}}, "name": name,
                                    "cvl": "rule r { assert true; }\n"},
                           "id": "t", "type": "tool_call"})
        if hasattr(res, "content"):
            return str(res.content)
        msgs = res.update.get("messages", []) if hasattr(res, "update") else []
        return str(msgs[0].content) if msgs else ""

    assert "must stay within" in put_msg("../summaries/custom_summaries")
    assert "must stay within" in put_msg("../other_component/base")
    assert "must stay within" in put_msg("/etc/passwd")
    assert put_msg("sub/helper") == "Accepted"
    assert put_msg("base") == "Accepted"


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
