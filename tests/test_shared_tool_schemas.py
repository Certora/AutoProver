"""LLM-facing schemas of the shared authoring tools vs the master wording.

The families in ``composer.authoring`` are instantiated with each backend's own prose.
CVL and Foundry instantiations must match the tools those backends shipped on master —
name, description, and non-injected field descriptions. Injected ``state`` / ``tool_call_id``
are ignored.
"""

import inspect

from langchain_core.tools import BaseTool

from composer.authoring.tools import (
    CVL_SKIP_DESCRIPTION, CVL_SKIP_REASON, CVL_UNSKIP_DESCRIPTION,
    FOUNDRY_SKIP_DESCRIPTION, FOUNDRY_SKIP_REASON, FOUNDRY_UNSKIP_DESCRIPTION,
    give_up_tool, skip_tools,
)
from composer.cvl.tools import WithCurrSpec, edit_cvl, edit_cvl_description, get_cvl
from composer.foundry.author import _GIVE_UP_DESCRIPTION as _FOUNDRY_GIVE_UP
from composer.spec.natspec.author import _GIVE_UP_DESCRIPTION as _NATSPEC_GIVE_UP
from composer.spec.source.author import _GIVE_UP_DESCRIPTION as _CVL_GIVE_UP


_INJECTED = frozenset({"state", "tool_call_id"})


def _surface(tool: BaseTool) -> dict:
    schema = tool.tool_call_schema.model_json_schema()
    return {
        "name": tool.name,
        "description": tool.description,
        "fields": {
            name: prop.get("description") or ""
            for name, prop in schema.get("properties", {}).items()
            if name not in _INJECTED
        },
    }


def _expect(name: str, raw_description: str, fields: dict[str, str]) -> dict:
    """``inspect.cleandoc`` is what StructuredTool (and so the model) sees."""
    return {
        "name": name,
        "description": inspect.cleandoc(raw_description),
        "fields": fields,
    }


# Frozen from origin/master. A mismatch means the family params drifted.

_MASTER_GET_CVL_DOC = (
    "\n    Retrive the textual representation of the current specification.\n    "
)
_MASTER_EDIT_CVL_DOC = (
    "\nMake a surgical edit to the current CVL spec instead of re-emitting the whole file.\n"
    "\nProvide `old_string` — an exact span copied from the current spec — and `new_string`\n"
    "to replace it with. `old_string` must occur exactly once; include enough surrounding\n"
    "context to make it unique. Prefer this over `put_cvl_raw` when changing only part of an\n"
    "existing spec (e.g. one line of a failing rule): it is dramatically cheaper than\n"
    "re-sending the entire file.\n"
    "\nThe edited spec is run through the CVL parser exactly like `put_cvl_raw`. If the result\n"
    "fails to parse, the edit is rejected with the parser errors and the buffer is unchanged.\n"
    "\nIMPORTANT: You cannot call this tool multiple times in the same turn. If you need to make\n"
    "multiple edits, you must spread them across distinct turns.\n"
)
_MASTER_CVL_GIVE_UP_DOC = (
    "\n    Call this tool to give up on the CVL generation for this task.\n"
    "\n    This should only ever be called as a LAST RESORT when you have exhausted all other\n"
    "    mechanisms to complete your task.\n    "
)
_MASTER_NATSPEC_GIVE_UP_DOC = (
    "\n    Call this tool to give up on the property generation for this task.\n"
    "\n    This should only ever be called as a LAST RESORT when you have exhausted all other\n"
    "    mechanisms to complete your task\n    "
)
_MASTER_FOUNDRY_GIVE_UP_DOC = (
    "\n    Last-resort exit when you've exhausted other mechanisms to complete\n"
    "    the task. The batch will be reported as failed with your ``reason``.\n    "
)
_MASTER_CVL_SKIP_DOC = (
    "\n    Declare that you are skipping a property from the batch.\n"
    "    You must provide the property's title and a justification.\n"
    "    The feedback judge will evaluate whether your justification is valid.\n"
    "    Only use this after genuinely attempting to formalize the property.\n    "
)
_MASTER_FOUNDRY_SKIP_DOC = (
    "\n    Declare that you are skipping a property from the batch.\n"
    "\n    You must provide the property's title and a justification. Skipping\n"
    "    excludes the property from the publish-time property→test mapping\n"
    "    check; only use after a genuine attempt to formalize.\n    "
)
_MASTER_CVL_UNSKIP_DOC = (
    "\n    Remove a previously declared skip for a property.\n"
    "    Use this if you later find a way to formalize a property you previously skipped.\n    "
)
_MASTER_FOUNDRY_UNSKIP_DOC = (
    "\n    Remove a previously declared skip for a property. Use this if you later\n"
    "    find a way to formalize a property you previously skipped.\n    "
)

_MASTER_GET_CVL = _expect("get_cvl", _MASTER_GET_CVL_DOC, {})
_MASTER_EDIT_CVL = _expect("edit_cvl", _MASTER_EDIT_CVL_DOC, {
    "old_string": (
        "The exact span of the current spec to replace. Must occur exactly once; "
        "include surrounding context to disambiguate."
    ),
    "new_string": "The text to replace `old_string` with.",
})
_MASTER_CVL_GIVE_UP = _expect(
    "give_up", _MASTER_CVL_GIVE_UP_DOC, {"reason": "The reason for giving up on your task"},
)
_MASTER_NATSPEC_GIVE_UP = _expect(
    "give_up", _MASTER_NATSPEC_GIVE_UP_DOC, {"reason": "The reason for giving up on your task"},
)
_MASTER_FOUNDRY_GIVE_UP = _expect(
    "give_up", _MASTER_FOUNDRY_GIVE_UP_DOC, {"reason": "Why you are giving up on this batch"},
)
_MASTER_CVL_SKIP = _expect("record_skip", _MASTER_CVL_SKIP_DOC, {
    "property_title": "The snake_case title of the property from the batch listing",
    "reason": "Justification for why this property cannot be formalized",
})
_MASTER_FOUNDRY_SKIP = _expect("record_skip", _MASTER_FOUNDRY_SKIP_DOC, {
    "property_title": "The snake_case title of the property from the batch listing",
    "reason": "Justification for why this property cannot be formalized as a foundry test",
})
_MASTER_CVL_UNSKIP = _expect("unskip_property", _MASTER_CVL_UNSKIP_DOC, {
    "property_title": "The snake_case title of the property to un-skip",
})
_MASTER_FOUNDRY_UNSKIP = _expect("unskip_property", _MASTER_FOUNDRY_UNSKIP_DOC, {
    "property_title": "The snake_case title of the property to un-skip",
})


def test_constants_are_the_master_wording():
    assert _CVL_GIVE_UP == _MASTER_CVL_GIVE_UP_DOC
    assert _NATSPEC_GIVE_UP == _MASTER_NATSPEC_GIVE_UP_DOC
    assert _FOUNDRY_GIVE_UP == _MASTER_FOUNDRY_GIVE_UP_DOC
    assert CVL_SKIP_DESCRIPTION == _MASTER_CVL_SKIP_DOC
    assert FOUNDRY_SKIP_DESCRIPTION == _MASTER_FOUNDRY_SKIP_DOC
    assert CVL_UNSKIP_DESCRIPTION == _MASTER_CVL_UNSKIP_DOC
    assert FOUNDRY_UNSKIP_DESCRIPTION == _MASTER_FOUNDRY_UNSKIP_DOC
    assert edit_cvl_description == _MASTER_EDIT_CVL_DOC


def test_get_cvl_matches_master():
    assert _surface(get_cvl(WithCurrSpec)) == _MASTER_GET_CVL


def test_edit_cvl_matches_master():
    assert _surface(edit_cvl(WithCurrSpec)) == _MASTER_EDIT_CVL


def test_give_up_matches_master():
    assert _surface(give_up_tool(
        name="give_up", description=_CVL_GIVE_UP, label="CVL generation",
    )) == _MASTER_CVL_GIVE_UP
    assert _surface(give_up_tool(
        name="give_up", description=_NATSPEC_GIVE_UP, label="property generation",
    )) == _MASTER_NATSPEC_GIVE_UP
    assert _surface(give_up_tool(
        name="give_up", description=_FOUNDRY_GIVE_UP, label="foundry-test generation",
        reason_description="Why you are giving up on this batch",
    )) == _MASTER_FOUNDRY_GIVE_UP


def test_skip_tools_match_master():
    cvl_skip, cvl_unskip = skip_tools(
        [],
        skip_description=CVL_SKIP_DESCRIPTION,
        skip_reason=CVL_SKIP_REASON,
        unskip_description=CVL_UNSKIP_DESCRIPTION,
    )
    assert _surface(cvl_skip) == _MASTER_CVL_SKIP
    assert _surface(cvl_unskip) == _MASTER_CVL_UNSKIP

    foundry_skip, foundry_unskip = skip_tools(
        [],
        skip_description=FOUNDRY_SKIP_DESCRIPTION,
        skip_reason=FOUNDRY_SKIP_REASON,
        unskip_description=FOUNDRY_UNSKIP_DESCRIPTION,
    )
    assert _surface(foundry_skip) == _MASTER_FOUNDRY_SKIP
    assert _surface(foundry_unskip) == _MASTER_FOUNDRY_UNSKIP
