"""Shared fixtures for smtool unit tests — fast, deterministic, GENERIC (a fictional CUT `C`;
no LLM, no prover, no compiled scene, no customer contract)."""
import pytest

from smtool.ir import ToolInput, FunctionSpec, Param as P
from smtool.project import Project


@pytest.fixture
def spec():
    """FunctionSpec.of with terse (type, name) param tuples: spec('f', [('uint256','x')], ['uint256'])."""
    def _spec(name, params=(), returns=(), mutability="nonpayable", **modeling):
        return FunctionSpec.of(name, [P(t, n) for t, n in params], list(returns), mutability, **modeling)
    return _spec


@pytest.fixture
def project():
    """Build a Project over the fictional CUT `C` from a set of FunctionSpecs (skeleton; no fills)."""
    def _project(*funcs, cut="C"):
        return Project.from_input(ToolInput(cut=cut, functions=list(funcs)))
    return _project
