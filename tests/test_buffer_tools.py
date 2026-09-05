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
