"""Declared cache-key derivation rules.

``WorkflowContext.child`` walks the cache tree one typed edge at a time:
a ``CacheKey[Parent, Child]`` names the child slot and carries the
(phantom) evidence that the transition is legal. The key *strings*,
though, were historically minted by ad-hoc helpers — file-local
functions gluing digests together with f-strings, each returning a bare
``CacheKey(...)`` whose type arguments were whatever the annotation
happened to claim. Nothing tied a derivation rule to the edge it
produces, and consumers that re-derive keys (the cache explorers) had
to import private helpers from whichever module each rule happened to
live in.

A :class:`KeyFamily` makes the rule the declared object: one value
pinning the parent type, the child type, and the derivation from the
parameters that identify a member of the family. Calling the family is
the only way to mint that edge's key, so every producer and every
explorer agree on the string and the types by construction.

Conventions:

- Families are declared next to their cache models (usually the module
  of the producing agent) and named ``*_KEY``, like the constant
  ``CacheKey`` slots they generalize.
- Each application gathers its families into a registry module —
  ``composer.pipeline.keys`` for the Auto-Prove driver chain,
  ``composer.spec.source.keys`` for the prover backend's sub-chain — so
  one file shows the whole cache tree. Registries import producers,
  never the reverse.
"""

from dataclasses import dataclass
from typing import Callable

from composer.spec.context import CacheKey


@dataclass(frozen=True)
class KeyFamily[Parent, Child, **P]:
    """The derivation rule for a ``Parent → Child`` cache edge.

    ``parent`` / ``child`` are runtime witnesses that bind the type
    parameters (``CacheKey``'s are phantom, so nothing else could);
    ``derive`` maps the values identifying one member of the family to
    its key string. Calling the family yields the typed key::

        AUTOSETUP_KEY = KeyFamily(ContractSetup, SetupSuccess, _autosetup_key)
        ...
        ctx.child(AUTOSETUP_KEY(app, prover_opts))

    A fixed edge (no parameters) is just a ``CacheKey`` constant; declare
    a family only when there is something to derive.
    """

    parent: type[Parent]
    child: type[Child]
    derive: Callable[P, str]

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> CacheKey[Parent, Child]:
        return CacheKey(self.derive(*args, **kwargs))


@dataclass(frozen=True)
class PolyKeyFamily[Parent, **P]:
    """A :class:`KeyFamily` whose child type is a call-site parameter.

    For the edges the driver traverses generically — the formalization
    result is the *backend's* result type, the analyzed system model is
    the *ecosystem's* — no static declaration can name the child. The
    caller passes the child's runtime witness instead, which is exactly
    what those call sites already hold (``formalizer.formalized_type``,
    ``ecosystem.system_model``), and inference does the rest — no
    ``CacheKey[...]`` respelling, no annotation-steered inference of an
    unparameterized call::

        child_ctx = await ctx.child(
            FORMALIZATION_KEY(formalizer.formalized_type, batch.props)
        )
    """

    parent: type[Parent]
    derive: Callable[P, str]

    def __call__[Child](
        self, child: type[Child], /, *args: P.args, **kwargs: P.kwargs
    ) -> CacheKey[Parent, Child]:
        return CacheKey(self.derive(*args, **kwargs))
