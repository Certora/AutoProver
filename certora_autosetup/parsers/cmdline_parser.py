#!/usr/bin/env python3
"""
Command line output parsing utilities.

This module contains functions for parsing command line output from various tools,
particularly for extracting useful information like job URLs from tool output.
"""

from typing import Optional

from certora_autosetup.utils.job_utils import extract_job_url_from_text


def extract_job_url(output: str) -> Optional[str]:
    """Extract job URL from certoraRun output.

    Args:
        output: The stdout/stderr output from certoraRun command

    Returns:
        str: The job URL if found, None otherwise
    """
    return extract_job_url_from_text(output)