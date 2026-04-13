"""Shared exception types for devloop."""


class DevloopError(Exception):
    """Base exception for devloop failures."""


class ConfigError(DevloopError):
    """Raised when the configuration is invalid."""


class ClipboardError(DevloopError):
    """Raised when clipboard access fails."""


class SessionError(DevloopError):
    """Raised when session state cannot be read or written."""


class ProtocolError(DevloopError):
    """Raised when the LLM protocol block is invalid."""


class RetrievalError(DevloopError):
    """Raised when repository retrieval fails."""


class GitError(DevloopError):
    """Raised when a Git operation fails."""


class PatchApplyError(DevloopError):
    """Raised when a patch is unsafe or cannot be applied."""

