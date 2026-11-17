"""FastAPI access to the content catalog and the trainer settings."""

from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.trainer.catalog import Catalog, load_catalog


def get_catalog(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> Catalog:
    """The catalog loaded at start-up (see the lifespan), or, where the app
    runs without a lifespan (tests), loaded on first use. A bad pack raises
    `PackError`: nothing is ever served half-loaded."""
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is None:
        catalog = load_catalog(Path(settings.trainer_content_dir))
        request.app.state.catalog = catalog
    assert isinstance(catalog, Catalog)
    return catalog


CatalogDep = Annotated[Catalog, Depends(get_catalog)]
