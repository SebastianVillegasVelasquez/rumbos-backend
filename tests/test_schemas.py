import pytest
from pydantic import ValidationError

from app.enums import BubbleIcon, BubbleStatus
from app.schemas.bubble import BubbleCreate, BubbleUpdate
from app.schemas.course_map import CourseMapCreate, CourseMapUpdate


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


@pytest.mark.parametrize(
    "image_url",
    [
        "https://cdn.example.com/maps/a.webp",
        "http://localhost:8000/a.png",
        "/static/maps/a.webp",
        "/",
    ],
)
def test_image_url_accepts_http_urls_and_site_paths(image_url: str) -> None:
    created = CourseMapCreate(title="T", moodle_course_id=1, image_url=image_url)
    assert created.image_url == image_url


@pytest.mark.parametrize(
    "image_url",
    [
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "file:///etc/passwd",
        "ftp://host/a.png",
        "//evil.example/a.png",
        r"/\evil.example/a.png",
        "relative/path.png",
        "http://",
        "https:///a.png",
        "https://host/a b.png",
        "",
        "/" + "a" * 2048,
    ],
)
def test_image_url_rejects_everything_else(image_url: str) -> None:
    with pytest.raises(ValidationError):
        CourseMapCreate(title="T", moodle_course_id=1, image_url=image_url)


def test_image_url_accepts_exactly_the_max_length() -> None:
    url = "/" + "a" * 2047
    assert CourseMapCreate(title="T", moodle_course_id=1, image_url=url)


def test_title_is_trimmed_and_bounded() -> None:
    assert (
        CourseMapCreate(title="  Ruta  ", moodle_course_id=1, image_url="/a").title
        == "Ruta"
    )
    for bad in ["", "   ", "x" * 121]:
        with pytest.raises(ValidationError):
            CourseMapCreate(title=bad, moodle_course_id=1, image_url="/a")
    assert CourseMapCreate(title="x" * 120, moodle_course_id=1, image_url="/a")


def test_course_map_update_requires_at_least_one_non_null_field() -> None:
    assert CourseMapUpdate.model_validate({"title": "New"}).model_fields_set == {
        "title"
    }
    for bad in [{}, {"title": None}, {"imageUrl": None}, {"imageUrl": "javascript:1"}]:
        with pytest.raises(ValidationError):
            CourseMapUpdate.model_validate(bad)
