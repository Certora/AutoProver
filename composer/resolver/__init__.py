"""The timeout resolver's workspace: a prover run's submitted snapshot, rebuilt
locally, and the adapters that let the DZ chain run against it outside the
pipeline.

The pipeline hands the DZ plugin a :class:`~composer.spec.source.plugin.CVLAuthorState`
whose prover runner and edit store close over the CVL author's live state. Here
the same state is built from a fetched run instead: :mod:`.workspace` fetches
and describes the run, :mod:`.runner` verifies against its canonical
configuration, :mod:`.preflight` compiles it once before any search,
:mod:`.artifacts` and :mod:`.patches` turn the agent's products into files, and
:mod:`.author_state` assembles the state the chain consumes.
"""
