class RunRefusal(Exception):
    """The run cannot go ahead with what it was given, and says why in words written for the person
    who started it — a project this build cannot verify, say, rather than a fault of the run's own.

    The message reaches that person as it is, in the report the run leaves in place of its own
    (:mod:`composer.pipeline.run_failure`), so it must name the problem and what would fix it, and
    nothing a reader outside the team should not see. The other ways a run can end are reported
    without text: a cancel or a keyboard interrupt as an interruption, a spent budget as which
    budget ran out, and any other exception as an internal error.
    """
