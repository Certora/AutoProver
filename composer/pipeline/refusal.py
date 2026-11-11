class RunRefusal(Exception):
    """The run cannot go ahead with what it was given, and says why in words written for the person
    who started it — a project this build cannot verify, say, rather than a fault of the run's own.

    The message reaches that person as it is, in the report the run leaves in place of its own
    (:mod:`composer.pipeline.run_failure`), so it must name the problem and what would fix it, and
    nothing a reader outside the team should not see. Any other exception that ends a run is
    reported as an internal error, with no text at all.
    """
