"""
Centralized logging utility for PreAudit tools.

This module provides a thread-safe, consistent logging interface
that ensures messages appear in the correct order.
"""

import os
import sys
import threading
from datetime import datetime
from typing import Optional
from collections import deque
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler

from certora_autosetup.utils.constants import ALL_LOGS_IN_STDOUT_ENV, DIR_CERTORA_INTERNAL, DIR_PREAUDIT_DEBUG


def _suppress_keyring_warnings():
    """Suppress 'Keyring not available' warnings from certora-cloud-cli.

    Sets log level to ERROR for certora_login and keyring loggers to suppress
    noisy credential messages like "Keyring not available" and "Reading credentials".
    """
    logging.getLogger("certora_login").setLevel(logging.ERROR)
    logging.getLogger("keyring").setLevel(logging.ERROR)


_suppress_keyring_warnings()


class Logger:
    """Thread-safe logger with consistent output ordering."""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    @property
    def muted(self) -> bool:
        if ALL_LOGS_IN_STDOUT_ENV in os.environ:
            return False
        return self._muted

    @muted.setter
    def muted(self, value: bool) -> None:
        self._muted = value

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self.verbose = 0
        self._muted = False  # When True, suppress all console output (file logging still works)
        self.use_colors = True
        self.use_emojis = True
        self._log_queue = deque()
        self._flush_lock = threading.Lock()

        # Set up file logging with rotation
        self._setup_file_logging()
        
        # Color codes for terminal output
        self.color_codes = {
            "INFO": "\033[0m",      # Default
            "SUCCESS": "\033[92m",  # Green
            "WARNING": "\033[93m",  # Yellow
            "ERROR": "\033[91m",    # Red
            "DEBUG": "\033[94m",    # Blue
            "CRITICAL": "\033[95m", # Magenta
        }
        
        # Emoji prefixes
        self.emojis = {
            "ERROR": "❌",
            "WARNING": "⚠️", 
            "INFO": "ℹ️",
            "SUCCESS": "✅",
            "DEBUG": "🐛",
            "CRITICAL": "🔴"
        }

    def _setup_file_logging(self, log_name: str = "orchestrator", force_flush: bool = False):
        """Set up rotating file handler for logging.

        Args:
            log_name: Name of the log file (without .log extension)
            force_flush: If True, force immediate flush after every log write
        """
        # Create .certora_internal directory if it doesn't exist
        log_dir = Path(".certora_internal")
        log_dir.mkdir(exist_ok=True)

        # Set up the file logger
        self.file_logger = logging.getLogger(log_name)
        self.file_logger.setLevel(logging.DEBUG)

        # Remove any existing handlers to avoid duplicates
        self.file_logger.handlers.clear()

        # Create rotating file handler (max 10MB per file, keep 5 files)
        log_file = log_dir / f"{log_name}.log"
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5
        )

        # Force immediate flush if requested (important for Flask auto-reload)
        if force_flush:
            stream = file_handler.stream
            if stream is not None:
                file_handler.setStream(stream)  # Ensure stream exists
            original_emit = file_handler.emit
            def emit_with_flush(record: logging.LogRecord) -> None:
                original_emit(record)
                s = file_handler.stream
                if s is not None:
                    s.flush()
            file_handler.emit = emit_with_flush  # type: ignore[method-assign]

        # Set up formatter for file output (no colors/emojis)
        file_formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)

        # Add handler to logger
        self.file_logger.addHandler(file_handler)

        # Prevent propagation to root logger
        self.file_logger.propagate = False

    def setup_job_logging(self, job_id: str):
        """
        Set up job-specific logging for reporter runs.

        Creates a log file named reporter_<job_id>_<timestamp>.log in
        .certora_internal/preaudit_debug/ directory.

        Args:
            job_id: The Certora job ID
        """
        # Create debug directory
        debug_dir = Path(DIR_CERTORA_INTERNAL) / DIR_PREAUDIT_DEBUG
        debug_dir.mkdir(parents=True, exist_ok=True)

        # Generate timestamp for log file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_name = f"reporter_{job_id}_{timestamp}"

        # Remove existing handlers to avoid duplicate logging
        self.file_logger.handlers.clear()

        # Create file handler (no rotation for job-specific logs)
        log_file = debug_dir / f"{log_name}.log"
        file_handler = logging.FileHandler(log_file)

        # Set up formatter (no colors/emojis for file output)
        file_formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)

        # Add handler to logger
        self.file_logger.addHandler(file_handler)

        # Store log file path for reference
        self.current_log_file = log_file

    def set_verbosity(self, verbose: int):
        """Set the verbosity level (0=normal, 1=verbose, 2=debug)."""
        self.verbose = verbose
    
    def log(self, message: str, level: str = "INFO", component: Optional[str] = None):
        """Log a message with proper formatting and ordering."""
        # Check verbosity level for DEBUG messages
        if level == "DEBUG" and self.verbose < 2:
            self._log_to_file(message, level, component)
            return

        # If muted, only log to file (skip console output)
        if self.muted:
            self._log_to_file(message, level, component)
            return

        with self._flush_lock:
            self._format_and_output(message, level, component)
    
    def _format_message(self, message: str, level: str, component: Optional[str]) -> str:
        """Format a message with timestamp, thread ID, component, and level."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        thread_id = threading.get_ident()

        parts = [f"[{timestamp}]"]
        parts.append(f"[T{thread_id}]")

        if component:
            parts.append(f"[{component}]")

        # Add level with color if enabled
        if self.use_colors and sys.stdout.isatty():
            color = self.color_codes.get(level, "\033[0m")
            reset = "\033[0m"
            parts.append(f"{color}{level}{reset}:")
        else:
            parts.append(f"{level}:")

        # Add emoji if enabled
        if self.use_emojis and level in self.emojis:
            parts.append(self.emojis[level])

        parts.append(message)
        return " ".join(parts)

    def _format_and_output(self, message: str, level: str, component: Optional[str]):
        """Format and immediately output the message."""
        formatted_message = self._format_message(message, level, component)

        # Output to appropriate stream with immediate flush
        if level in ["ERROR", "CRITICAL"]:
            print(formatted_message, file=sys.stderr, flush=True)
        else:
            print(formatted_message, file=sys.stdout, flush=True)

        # Also log to file (without colors/emojis)
        self._log_to_file(message, level, component)

    def _log_to_file(self, message: str, level: str, component: Optional[str]):
        """Log message to rotating file."""
        try:
            # Get thread ID
            thread_id = threading.get_ident()

            # Build message for file (no colors/emojis)
            file_message = f"[T{thread_id}] {message}"
            if component:
                file_message = f"[{component}] {file_message}"

            # Map our custom levels to standard logging levels
            level_mapping = {
                "DEBUG": logging.DEBUG,
                "INFO": logging.INFO,
                "SUCCESS": logging.INFO,
                "WARNING": logging.WARNING,
                "ERROR": logging.ERROR,
                "CRITICAL": logging.CRITICAL
            }

            log_level = level_mapping.get(level, logging.INFO)
            self.file_logger.log(log_level, file_message)

        except Exception as e:
            # Don't let file logging errors break the application
            pass
    
    def debug(self, message: str, component: Optional[str] = None):
        """Log a debug message."""
        self.log(message, "DEBUG", component)
    
    def info(self, message: str, component: Optional[str] = None):
        """Log an info message."""
        self.log(message, "INFO", component)
    
    def warning(self, message: str, component: Optional[str] = None):
        """Log a warning message."""
        self.log(message, "WARNING", component)
    
    def error(self, message: str, component: Optional[str] = None):
        """Log an error message."""
        self.log(message, "ERROR", component)
    
    def success(self, message: str, component: Optional[str] = None):
        """Log a success message."""
        self.log(message, "SUCCESS", component)
    
    def critical(self, message: str, component: Optional[str] = None):
        """Log a critical message."""
        self.log(message, "CRITICAL", component)

    def log_or_write(self, msg: str, progress=None, level: str = "INFO", component: Optional[str] = None):
        """Log message via tqdm.write() if progress bar exists, else use logger.

        Args:
            msg: The message to log
            progress: Optional tqdm progress bar instance
            level: Log level ("INFO", "WARNING", "ERROR", "DEBUG", "SUCCESS", "CRITICAL")
            component: Optional component name for log prefix (e.g., "AnalysisCache")
        """
        level_upper = level.upper()

        if progress:
            if not self.muted:
                formatted_msg = self._format_message(msg, level_upper, component)
                progress.write(formatted_msg)
            # Always log to file
            self._log_to_file(msg, level_upper, component)
        else:
            # Use regular logging (which includes formatting and mute check)
            self.log(msg, level_upper, component)


# Global logger instance
logger = Logger()


def log_with_contract(component: str, level: str, contract_name: str, message: str) -> None:
    """Log a message prefixed with [contract_name]."""
    logger.log(f"[{contract_name}] {message}", level.upper(), component)