# ruff: noqa: F811
"""A map cannot be saved with an exam form of another language or level."""

from typing import Any

import httpx

from tests.test_publications_api import make_map
from tests.test_trainer_settings import client  # noqa: F401
from tests.test_welcome_api import put_settings


def exams(form_id: str, **over: Any) -> dict[str, Any]:
    return {
        "enabled": True,
        "checkIn": {"formId": form_id, "maxAttempts": 1, "timeLimitMin": 60},
        "passPercent": 80,
        "passOperator": "gt",
        "exemption": {"enabled": True},
        **over,
    }


def trainer(level: str, enabled: bool = True) -> dict[str, Any]:
    return {"enabled": enabled, "language": "en", "level": level}


async def test_a_form_of_another_level_is_a_422(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    body = {"trainer": trainer("A1"), "assessments": exams("a2-form-a")}
    r = await put_settings(client, created["id"], body)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "trainer_settings_invalid"
    assert detail["errors"] == [
        {
            "path": "assessments.check_in.form_id",
            "rule": "form_level_mismatch",
            "message_key": "trainer_settings.form_level_mismatch",
        }
    ]
    assert detail["problems"][0]["code"] == "form_level_mismatch"
    # No value of the request is echoed back.
    assert "a2-form-a" not in str(detail)


async def test_the_final_exam_is_checked_too(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    body = {
        "trainer": trainer("A2"),
        "assessments": exams(
            "a2-form-a", final={"formId": "a1-form-a", "maxAttempts": 1}
        ),
    }
    r = await put_settings(client, created["id"], body)
    assert r.status_code == 422
    (error,) = r.json()["detail"]["errors"]
    assert error["path"] == "assessments.final.form_id"
    assert error["rule"] == "form_level_mismatch"


async def test_a_form_of_the_same_level_still_saves(
    client: httpx.AsyncClient,
) -> None:
    created = await make_map(client)
    for level, form in (
        ("A1", "a1-form-a"),
        ("A2", "a2-form-a"),
        ("B1", "b1-form-a"),
    ):
        body = {"trainer": trainer(level), "assessments": exams(form)}
        r = await put_settings(client, created["id"], body)
        assert r.status_code == 200, r.text


async def test_without_a_trainer_the_level_is_not_a_choice_so_nothing_is_refused(
    client: httpx.AsyncClient,
) -> None:
    # `trainer.level` defaults to A1 even when the editor never set it, so with
    # the trainer off it cannot be told apart from a deliberate A1.
    created = await make_map(client)
    r = await put_settings(client, created["id"], {"assessments": exams("a2-form-a")})
    assert r.status_code == 200, r.text
