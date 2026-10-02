import pytest
from pydantic import ValidationError

from app.enums import BubbleIcon, BubbleStatus
from app.schemas.bubble import BubbleCreate, BubbleUpdate


def test_bubble_create_defaults() -> None:
    bubble = BubbleCreate(activity_id=1, x=0.1, y=0.9)
    assert bubble.icon is None
    assert bubble.status is BubbleStatus.LOCKED


@pytest.mark.parametrize("bad", [-0.01, 1.01])
def test_bubble_create_rejects_out_of_range_position(bad: float) -> None:
    with pytest.raises(ValidationError):
        BubbleCreate(activity_id=1, x=bad, y=0.5)


def test_bubble_create_rejects_unknown_icon_and_status() -> None:
    with pytest.raises(ValidationError):
        BubbleCreate.model_validate(
            {"activity_id": 1, "x": 0, "y": 0, "icon": "rocket"}
        )
    with pytest.raises(ValidationError):
        BubbleCreate.model_validate(
            {"activity_id": 1, "x": 0, "y": 0, "status": "done"}
        )


def test_bubble_update_is_partial() -> None:
    update = BubbleUpdate.model_validate({"status": "complete"})
    assert update.model_fields_set == {"status"}


def test_bubble_update_distinguishes_null_icon_from_missing() -> None:
    assert "icon" in BubbleUpdate.model_validate({"icon": None}).model_fields_set
    assert (
        "icon" not in BubbleUpdate.model_validate({"status": "locked"}).model_fields_set
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"x": 0.5},
        {"y": 0.5},
        {"x": None, "y": None},
        {"status": None},
        {"x": 2, "y": 0},
    ],
)
def test_bubble_update_rejects_invalid_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BubbleUpdate.model_validate(payload)


def test_bubble_update_accepts_position_and_icon() -> None:
    update = BubbleUpdate.model_validate({"x": 0.2, "y": 0.3, "icon": "star"})
    assert update.icon is BubbleIcon.STAR


def test_schemas_accept_camel_and_snake_and_serialize_camel() -> None:
    camel = BubbleCreate.model_validate({"activityId": 1, "x": 0, "y": 0})
    snake = BubbleCreate(activity_id=1, x=0, y=0)
    assert camel == snake
    assert set(snake.model_dump(by_alias=True)) == {
        "activityId",
        "x",
        "y",
        "icon",
        "status",
    }
