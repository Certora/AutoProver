#!/usr/bin/env python3
"""
Logging Mixin - Provides contextual logging capabilities.

This mixin provides a unified way to add context (like contract names, phases, etc.)
to log messages across the autosetup codebase.
"""

import logging
from typing import Optional


class LoggingMixin:
    """
    Mixin class that provides contextual logging capabilities.

    Classes that inherit from this mixin get access to _log_with_context() method
    which can add context information to logger names for better log organization.

    Classes using this mixin must provide a 'logger' attribute of type logging.Logger.
    """

    # Declare that inheriting classes must provide a logger attribute
    logger: logging.Logger

    def _log_with_context(
        self,
        level: str,
        message: str,
        context: Optional[str] = None
    ) -> None:
        """
        Log a message with optional context added to the logger name.

        Args:
            level: Log level ('debug', 'info', 'warning', 'error', 'critical')
            message: Message to log
            context: Optional context to add to logger name (e.g., contract name, phase)

        Examples:
            self._log_with_context("info", "Processing started")
            # Logs: autosetup.module - INFO - Processing started

            self._log_with_context("info", "Processing started", context="StandardERC20")
            # Logs: autosetup.module.StandardERC20 - INFO - Processing started
        """
        if context:
            context_logger = logging.getLogger(f"{self.logger.name}.{context}")
            getattr(context_logger, level)(message)
        else:
            getattr(self.logger, level)(message)
    
    def _log_debug(self, message: str, context: Optional[str] = None) -> None:
        """Convenience method for debug logging."""
        self._log_with_context("debug", message, context)
    
    def _log_info(self, message: str, context: Optional[str] = None) -> None:
        """Convenience method for info logging."""
        self._log_with_context("info", message, context)
    
    def _log_warning(self, message: str, context: Optional[str] = None) -> None:
        """Convenience method for warning logging."""
        self._log_with_context("warning", message, context)
    
    def _log_error(self, message: str, context: Optional[str] = None) -> None:
        """Convenience method for error logging."""
        self._log_with_context("error", message, context)
    
    def _log_critical(self, message: str, context: Optional[str] = None) -> None:
        """Convenience method for critical logging."""
        self._log_with_context("critical", message, context)