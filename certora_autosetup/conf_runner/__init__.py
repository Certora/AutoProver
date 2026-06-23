"""
Conf Runner package — execute Certora verification configurations and generate reports.

Conf execution component: given a set of .conf files, submits them
to the Certora Prover (cloud or local), handles callbacks (multi-assert, difficult retry),
and generates reports including the results JSON.

Will eventually become its own repository.
"""

from certora_autosetup.conf_runner.conf_runner import ConfRunner
from certora_autosetup.conf_runner.types import ConfRunnerConfig

__all__ = [
    "ConfRunner",
    "ConfRunnerConfig",
]
