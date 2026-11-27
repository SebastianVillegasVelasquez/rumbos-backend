import logging
from typing import Any

import httpx
from pydantic import SecretStr, TypeAdapter, ValidationError

from app.moodle.exceptions import (
    MoodleAuthError,
    MoodleCourseNotFoundError,
    MoodleError,
    MoodleUnavailableError,
)
from app.moodle.schemas import (
    MoodleCompletionStatuses,
    MoodleSection,
    MoodleSiteInfo,
)

logger = logging.getLogger(__name__)

_REST_PATH = "/webservice/rest/server.php"

# Moodle `errorcode` values. Classification of codes we haven't seen on a real
# server is best-effort; anything unknown becomes a generic `MoodleError`.
_AUTH_CODES = frozenset({"invalidtoken", "accessexception", "webservicesnotenabled"})
_NOT_FOUND_CODES = frozenset({"dml_missing_record_exception", "invalidcourseid"})

_SITE_INFO = TypeAdapter(MoodleSiteInfo)
_SECTIONS = TypeAdapter(list[MoodleSection])
_COMPLETION = TypeAdapter(MoodleCompletionStatuses)


class HttpMoodleClient:
    """`MoodleClient` over Moodle's REST web service protocol.

    Takes a shared `httpx.AsyncClient` (owned by the app lifespan, which
    configures the timeouts). The token travels only in the POST body, and is
    never logged or put in an exception message.
    """

    def __init__(
        self, http: httpx.AsyncClient, base_url: str, token: SecretStr
    ) -> None:
        self._http = http
        self._url = base_url.rstrip("/") + _REST_PATH
        self._token = token

    async def get_site_info(self) -> MoodleSiteInfo:
        data = await self._call("core_webservice_get_site_info")
        return self._parse(_SITE_INFO, data)

    async def get_course_contents(self, course_id: int) -> list[MoodleSection]:
        data = await self._call("core_course_get_contents", courseid=course_id)
        return self._parse(_SECTIONS, data)

    async def get_activities_completion_status(
        self, course_id: int, moodle_user_id: int
    ) -> MoodleCompletionStatuses:
        """One user's completion in one course, as the service account may read
        it. Moodle leaves out the activities that account cannot see. Used by
        the operator's check only: the gate has no completion provider yet."""
        data = await self._call(
            "core_completion_get_activities_completion_status",
            courseid=course_id,
            userid=moodle_user_id,
        )
        return self._parse(_COMPLETION, data)

    async def _call(self, wsfunction: str, **params: Any) -> Any:
        form = {
            "wstoken": self._token.get_secret_value(),
            "wsfunction": wsfunction,
            "moodlewsrestformat": "json",
            **params,
        }
        try:
            response = await self._http.post(self._url, data=form)
        except httpx.TimeoutException:
            logger.warning("Moodle call %s timed out", wsfunction)
            raise MoodleUnavailableError("Moodle request timed out") from None
        except httpx.HTTPError as exc:
            # str(exc) can embed the request URL; log only the type.
            logger.warning("Moodle call %s failed: %s", wsfunction, type(exc).__name__)
            raise MoodleUnavailableError("Moodle is unreachable") from None

        if response.status_code >= 500:
            raise MoodleUnavailableError(f"Moodle returned HTTP {response.status_code}")
        if response.status_code >= 400:
            raise MoodleError(f"Moodle returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise MoodleUnavailableError(
                "Moodle returned a non-JSON response"
            ) from None

        # Moodle reports failures with HTTP 200 and an error object.
        if isinstance(body, dict) and ("exception" in body or "errorcode" in body):
            self._raise_for_error(wsfunction, body)
        return body

    @staticmethod
    def _raise_for_error(wsfunction: str, body: dict[str, Any]) -> None:
        code = str(body.get("errorcode", ""))
        # Log the code only: `message`/`debuginfo` are Moodle internals.
        logger.warning("Moodle call %s failed with errorcode=%s", wsfunction, code)
        if code in _AUTH_CODES:
            raise MoodleAuthError(
                "Moodle rejected the credentials or permissions", errorcode=code
            )
        if code in _NOT_FOUND_CODES:
            raise MoodleCourseNotFoundError("Moodle course not found", errorcode=code)
        raise MoodleError(f"Moodle error: {code or 'unknown'}", errorcode=code or None)

    @staticmethod
    def _parse[T](adapter: TypeAdapter[T], data: Any) -> T:
        try:
            return adapter.validate_python(data)
        except ValidationError:
            raise MoodleError("Unexpected response shape from Moodle") from None
