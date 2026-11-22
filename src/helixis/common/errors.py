"""Exception hierarchy for Helixis."""


class HelixisError(Exception):
    """Base exception for all Helixis errors."""


class LLMError(HelixisError):
    """LLM call failure (rate limit, timeout, context overflow)."""


class ToolExecutionError(HelixisError):
    """A tool call failed during execution."""

    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        super().__init__(f"Tool '{tool_name}' failed: {message}")


class BrowserError(HelixisError):
    """Browser automation failure."""


class ConfirmationDeniedError(HelixisError):
    """User denied a confirmation request."""

    def __init__(self, action: str):
        self.action = action
        super().__init__(f"User denied action: {action}")


class SessionNotFoundError(HelixisError):
    """Agent session not found."""


class ConfigurationError(HelixisError):
    """Invalid configuration."""
