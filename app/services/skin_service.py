import uuid

from app.enums import AssetKind
from app.exceptions import (
    SkinAssetNotFoundError,
    SkinAssetSizeMismatchError,
    SkinAssetWrongKindError,
    SkinIsBuiltinError,
    SkinNotFoundError,
)
from app.repositories.protocols import AssetRepository, SkinRepository
from app.schemas.skin import (
    MAX_SKINS_LISTED,
    ImageSkin,
    ProceduralSkin,
    SkinCreate,
    SkinList,
    SkinRead,
    SkinUpdate,
)


class SkinService:
    """Skins and the rules for what an image skin may reference.

    The shape of a config is checked by its Pydantic model; what needs the
    database (do the assets exist, are they bubble images, do the states share
    one artboard size) is checked here.
    """

    def __init__(self, skins: SkinRepository, assets: AssetRepository) -> None:
        self._skins = skins
        self._assets = assets

    async def list_skins(self) -> SkinList:
        return SkinList(items=await self._skins.list(MAX_SKINS_LISTED))

    async def create_skin(self, data: SkinCreate) -> SkinRead:
        await self.validate_config(data.config)
        return await self._skins.create(data.name, data.config)

    async def update_skin(self, skin_id: uuid.UUID, data: SkinUpdate) -> SkinRead:
        """Applies only the fields present in `data`."""
        current = await self._require(skin_id)
        if current.builtin:
            raise SkinIsBuiltinError(skin_id)
        if data.config is not None:
            await self.validate_config(data.config)
        updated = await self._skins.update(skin_id, data.name, data.config)
        if updated is None:  # deleted between the lookup and the write
            raise SkinNotFoundError(skin_id)
        return updated

    async def delete_skin(self, skin_id: uuid.UUID) -> None:
        current = await self._require(skin_id)
        if current.builtin:
            raise SkinIsBuiltinError(skin_id)
        if not await self._skins.delete(skin_id):
            raise SkinNotFoundError(skin_id)

    async def validate_config(self, config: ProceduralSkin | ImageSkin) -> None:
        """For image skins, the referenced assets must exist, be bubble images,
        and all have exactly the same width and height (artists draw every
        state on one artboard; different sizes make the states "jump")."""
        if not isinstance(config, ImageSkin):
            return
        referenced = config.states.referenced()
        found = await self._assets.get_many(set(referenced.values()))

        missing = {s: a for s, a in referenced.items() if a not in found}
        if missing:
            raise SkinAssetNotFoundError(missing)
        wrong = {
            s: a for s, a in referenced.items() if found[a].kind is not AssetKind.BUBBLE
        }
        if wrong:
            raise SkinAssetWrongKindError(wrong)
        sizes = [(s, a, found[a].width, found[a].height) for s, a in referenced.items()]
        if len({(width, height) for _, _, width, height in sizes}) > 1:
            raise SkinAssetSizeMismatchError(sizes)

    async def _require(self, skin_id: uuid.UUID) -> SkinRead:
        skin = await self._skins.get_by_id(skin_id)
        if skin is None:
            raise SkinNotFoundError(skin_id)
        return skin
