"""Errors raised by the Moodle integration.

Messages are deliberately generic: they never carry the token, the request
parameters or Moodle's raw error text, so they are safe to log and to
translate into HTTP responses.
"""


class MoodleError(Exception):
    """Base class, and the fallback for Moodle errors we don't classify.

    `errorcode` is Moodle's short code (`requiredcapability`, ...) when Moodle
    answered with an error object, for an operator's diagnosis."""

    def __init__(self, message: str = "", *, errorcode: str | None = None) -> None:
        super().__init__(message)
        self.errorcode = errorcode


class MoodleUnavailableError(MoodleError):
    """Network failure, timeout, 5xx, or a response that isn't valid JSON."""


class MoodleAuthError(MoodleError):
    """The token is invalid/expired or lacks permission for the function."""


class MoodleCourseNotFoundError(MoodleError):
    """Moodle has no (visible) course with the requested id."""
