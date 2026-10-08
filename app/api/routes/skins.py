import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.repositories.sqlalchemy.asset_repository import SqlAlchemyAssetRepository
from app.repositories.sqlalchemy.skin_repository import SqlAlchemySkinRepository
from app.schemas.skin import SkinCreate, SkinList, SkinRead, SkinUpdate
from app.services.skin_service import SkinService

router = APIRouter(prefix="/skins", tags=["skins"])


def get_skin_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SkinService:
    return SkinService(
        SqlAlchemySkinRepository(session), SqlAlchemyAssetRepository(session)
    )


ServiceDep = Annotated[SkinService, Depends(get_skin_service)]


@router.get("", response_model=SkinList)
async def list_skins(service: ServiceDep) -> SkinList:
    return await service.list_skins()


@router.post("", response_model=SkinRead, status_code=status.HTTP_201_CREATED)
async def create_skin(data: SkinCreate, service: ServiceDep) -> SkinRead:
    return await service.create_skin(data)


@router.patch("/{skin_id}", response_model=SkinRead)
async def update_skin(
    skin_id: uuid.UUID, data: SkinUpdate, service: ServiceDep
) -> SkinRead:
    return await service.update_skin(skin_id, data)


@router.delete("/{skin_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skin(skin_id: uuid.UUID, service: ServiceDep) -> Response:
    await service.delete_skin(skin_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
