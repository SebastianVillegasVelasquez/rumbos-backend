from app.schemas.base import ApiModel


class Scene(ApiModel):
    id: str
    zone_count: int


class SceneList(ApiModel):
    items: list[Scene]
