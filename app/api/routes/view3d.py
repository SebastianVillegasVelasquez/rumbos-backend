"""The 3D scenes the backend knows about (a code registry, see `app.view3d`)."""

from fastapi import APIRouter

from app.schemas.view3d import Scene, SceneList
from app.view3d import scenes

router = APIRouter(prefix="/view3d", tags=["view3d"])


@router.get("/scenes", response_model=SceneList)
async def list_scenes() -> SceneList:
    return SceneList(
        items=[
            Scene(id=scene, zone_count=scenes.SCENES[scene])
            for scene in scenes.known_scenes()
        ]
    )
