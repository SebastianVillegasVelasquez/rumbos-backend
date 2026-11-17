"""Every content pack of the deployment, loaded once at start-up."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from app.trainer.pack import Pack, PackError, load_pack


@dataclass(frozen=True)
class Catalog:
    # (language code, level) -> pack
    packs: Mapping[tuple[str, str], Pack]

    def pack(self, language: str, level: str) -> Pack | None:
        return self.packs.get((language, level))

    def form(self, form_id: str) -> tuple[Pack, str] | None:
        for pack in self.packs.values():
            if form_id in pack.forms:
                return pack, form_id
        return None


def load_catalog(base: Path) -> Catalog:
    """Loads every `<language>/<level>/` pack under `base`. Any bad pack stops
    the load with all problems listed; nothing is served half-loaded."""
    packs: dict[tuple[str, str], Pack] = {}
    problems: list[str] = []
    for meta_path in sorted(base.glob("*/*/pack.json")):
        try:
            pack = load_pack(meta_path.parent)
        except PackError as exc:
            problems += [
                f"{meta_path.parent.relative_to(base)}: {p}" for p in exc.problems
            ]
            continue
        key = (pack.meta.language.code, pack.meta.level)
        if key in packs:
            problems.append(f"two packs for {key}")
        packs[key] = pack
    if problems:
        raise PackError(problems)
    return Catalog(MappingProxyType(packs))
