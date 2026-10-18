"""Two Moodle sites that use the same course ids and activity ids never see,
change or collide with each other, and neither does a second course of one
site. Everything goes through the real API with real bearer tokens."""

import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.enums import Role
from tests import images, skin_configs
from tests.asset_helpers import png_asset, upload
from tests.conftest import Box
from tests.editor_client import EditorClient
from tests.tenant_helpers import insert_tenant, insert_token
from tests.test_publications_api import add_bubble, publish

COURSE = 7
ACTIVITY = 100
SECTION = 3

MakeClient = Callable[[str | None], EditorClient]


class Site:
    """One tenant, with an editor and a student of `COURSE`."""

    def __init__(
        self, tenant_id: uuid.UUID, editor: EditorClient, student: EditorClient
    ):
        self.tenant_id = tenant_id
        self.editor = editor
        self.student = student


@pytest.fixture
async def sites(
    engine: AsyncEngine, make_client: MakeClient, box: Box, storage: object
) -> tuple[Site, Site]:
    built = []
    for name in ("alpha", "beta"):
        tenant_id = await insert_tenant(engine, name)
        built.append(
            Site(
                tenant_id,
                make_client(await insert_token(engine, tenant_id, COURSE, Role.EDITOR)),
                make_client(
                    await insert_token(engine, tenant_id, COURSE, Role.STUDENT)
                ),
            )
        )
    return built[0], built[1]


async def _map(client: httpx.AsyncClient, **fields: Any) -> dict[str, Any]:
    r = await client.post(
        "/course-maps",
        json={
            "title": "Mapa",
            "moodleCourseId": COURSE,
            "moodleSectionId": SECTION,
            "imageUrl": "http://i/x.png",
            **fields,
        },
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def test_the_same_course_section_and_activity_coexist(
    sites: tuple[Site, Site],
) -> None:
    alpha, beta = sites
    map_a = await _map(alpha.editor)
    map_b = await _map(beta.editor)  # same course, same section: no clash
    bubble_a = await add_bubble(alpha.editor, map_a["id"], ACTIVITY)
    bubble_b = await add_bubble(beta.editor, map_b["id"], ACTIVITY)  # same activity

    assert map_a["id"] != map_b["id"]
    assert bubble_a["id"] != bubble_b["id"]
    # And inside one tenant the rules still hold.
    again = await alpha.editor.post(
        "/course-maps",
        json={
            "title": "Otro",
            "moodleCourseId": COURSE,
            "moodleSectionId": SECTION,
            "imageUrl": "http://i/x.png",
        },
    )
    assert again.status_code == 409
    dup = await alpha.editor.post(
        f"/course-maps/{map_a['id']}/bubbles",
        json={"activityId": ACTIVITY, "x": 0.5, "y": 0.5},
    )
    assert dup.status_code == 409


async def test_listings_show_only_the_callers_data(sites: tuple[Site, Site]) -> None:
    alpha, beta = sites
    await _map(alpha.editor, title="de alpha")
    await _map(beta.editor, title="de beta")

    for site, title in ((alpha, "de alpha"), (beta, "de beta")):
        listed = (await site.editor.get("/course-maps")).json()
        assert [m["title"] for m in listed["items"]] == [title]
        assert listed["total"] == 1
        # Asking for a course by id does not widen it either.
        by_course = (
            await site.editor.get("/course-maps", params={"moodleCourseId": COURSE})
        ).json()
        assert [m["title"] for m in by_course["items"]] == [title]


async def test_foreign_maps_and_bubbles_are_404_for_every_verb(
    sites: tuple[Site, Site],
) -> None:
    alpha, beta = sites
    mine = await _map(alpha.editor)
    bubble = await add_bubble(alpha.editor, mine["id"], ACTIVITY)
    map_path = f"/course-maps/{mine['id']}"
    bubble_path = f"{map_path}/bubbles/{bubble['id']}"
    intruder = beta.editor

    attempts = [
        await intruder.get(map_path),
        await intruder.patch(
            map_path, json={"title": "x"}, headers={"If-Match": '"1"'}
        ),
        await intruder.delete(map_path),
        await intruder.put(
            f"{map_path}/appearance",
            json={"settings": mine["settings"], "defaultSkinId": None, "skinRules": []},
            headers={"If-Match": '"1"'},
        ),
        await intruder.put(
            f"{map_path}/view3d",
            content=b"null",
            headers={"If-Match": '"1"', "Content-Type": "application/json"},
        ),
        await intruder.post(
            f"{map_path}/bubbles", json={"activityId": 5, "x": 0.1, "y": 0.1}
        ),
        await intruder.patch(
            bubble_path, json={"x": 0.2, "y": 0.2}, headers={"If-Match": '"1"'}
        ),
        await intruder.delete(bubble_path, headers={"If-Match": '"1"'}),
        await intruder.put(
            f"{map_path}/bubbles/order", json={"bubbleIds": [bubble["id"]]}
        ),
        await intruder.get(f"{map_path}/publication-state"),
        await intruder.get(f"{map_path}/publications"),
        await intruder.post(f"{map_path}/publish", json={"draftHash": "0" * 64}),
        await intruder.post(f"{map_path}/discard-changes", headers={"If-Match": '"1"'}),
    ]

    assert [r.status_code for r in attempts] == [404] * len(attempts), [
        (r.request.method, r.request.url.path, r.status_code) for r in attempts
    ]
    # Nothing was touched.
    assert (await alpha.editor.get(map_path)).json()["title"] == "Mapa"
    assert len((await alpha.editor.get(map_path)).json()["bubbles"]) == 1


async def test_reordering_maps_needs_a_course_you_can_see(
    sites: tuple[Site, Site],
) -> None:
    alpha, _ = sites
    r = await alpha.editor.put(
        "/course-maps/order", json={"moodleCourseId": 99, "mapIds": []}
    )
    assert r.status_code == 404


async def test_a_map_for_another_course_cannot_be_created(
    sites: tuple[Site, Site],
) -> None:
    alpha, _ = sites
    r = await alpha.editor.post(
        "/course-maps",
        json={"title": "x", "moodleCourseId": COURSE + 1, "imageUrl": "http://i/x.png"},
    )
    assert r.status_code == 404


async def test_another_course_of_the_same_tenant_is_equally_invisible(
    engine: AsyncEngine, make_client: MakeClient, sites: tuple[Site, Site]
) -> None:
    alpha, _ = sites
    mine = await _map(alpha.editor)
    other_course = make_client(
        await insert_token(engine, alpha.tenant_id, COURSE + 1, Role.EDITOR)
    )

    assert (await other_course.get(f"/course-maps/{mine['id']}")).status_code == 404
    assert (await other_course.get("/course-maps")).json()["items"] == []
    own = await _map(other_course, moodleCourseId=COURSE + 1, title="curso 8")
    assert (await alpha.editor.get(f"/course-maps/{own['id']}")).status_code == 404


async def test_published_maps_are_visible_to_the_courses_students_only(
    engine: AsyncEngine, make_client: MakeClient, sites: tuple[Site, Site]
) -> None:
    alpha, beta = sites
    mine = await _map(alpha.editor)
    await add_bubble(alpha.editor, mine["id"], ACTIVITY)
    await publish(alpha.editor, mine["id"])
    path = f"/published/course-maps/{mine['id']}"

    assert (await alpha.student.get(path)).status_code == 200
    assert (await alpha.editor.get(path)).status_code == 200
    assert (await beta.student.get(path)).status_code == 404
    assert (await beta.editor.get(path)).status_code == 404
    other_course = make_client(
        await insert_token(engine, alpha.tenant_id, COURSE + 1, Role.STUDENT)
    )
    assert (await other_course.get(path)).status_code == 404
    for site in (beta, alpha):
        listed = (await site.student.get("/published/course-maps")).json()
        assert listed["total"] == (1 if site is alpha else 0)


# --- assets ---------------------------------------------------------------------


async def test_assets_are_per_tenant_even_for_identical_bytes(
    sites: tuple[Site, Site],
) -> None:
    alpha, beta = sites
    data = images.png((40, 40))
    first = await upload(alpha.editor, data)
    second = await upload(beta.editor, data)  # same bytes, other tenant: its own asset
    again = await upload(alpha.editor, data)

    assert (first.status_code, second.status_code, again.status_code) == (201, 201, 200)
    assert first.json()["id"] != second.json()["id"]
    assert again.json()["id"] == first.json()["id"]


async def test_the_library_lists_and_edits_only_own_assets(
    sites: tuple[Site, Site],
) -> None:
    alpha, beta = sites
    asset = await png_asset(alpha.editor, 1)

    assert (await beta.editor.get("/assets")).json()["total"] == 0
    assert (await alpha.editor.get("/assets")).json()["total"] == 1
    assert (
        await beta.editor.patch(f"/assets/{asset['id']}", json={"title": "mine"})
    ).status_code == 404
    assert (await beta.editor.delete(f"/assets/{asset['id']}")).status_code == 404
    assert (await alpha.editor.get("/assets")).json()["items"][0]["title"] != "mine"
    assert (await alpha.editor.delete(f"/assets/{asset['id']}")).status_code == 204


async def test_an_asset_is_fetchable_by_its_id_without_a_token(
    sites: tuple[Site, Site], make_client: MakeClient
) -> None:
    """Documented: an <img> cannot send a header, the unguessable id is the key."""
    alpha, _ = sites
    asset = await png_asset(alpha.editor, 2)
    anonymous = make_client(None)

    for url in (asset["url"], asset["thumbUrl"] or asset["url"]):
        r = await anonymous.get(url)
        assert r.status_code == 200, url
    assert (await anonymous.get(f"/assets/{uuid.uuid4()}")).status_code == 404


async def test_a_foreign_asset_cannot_be_used_as_a_background(
    sites: tuple[Site, Site],
) -> None:
    alpha, beta = sites
    background = await png_asset(alpha.editor, 3, "background")

    r = await beta.editor.post(
        "/course-maps",
        json={"title": "x", "moodleCourseId": COURSE, "imageUrl": background["url"]},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "background_asset_not_found"


async def test_quotas_are_per_tenant(
    engine: AsyncEngine, make_client: MakeClient, box: Box, storage: object
) -> None:
    roomy = await insert_tenant(engine, "roomy", quota=10**9)
    roomy_editor = make_client(await insert_token(engine, roomy, COURSE, Role.EDITOR))
    data = images.png((30, 30))
    stored = (await upload(roomy_editor, data)).json()["bytes"]

    # Room for exactly that one file.
    small = await insert_tenant(engine, "small", quota=stored + 10)
    small_editor = make_client(await insert_token(engine, small, COURSE, Role.EDITOR))
    assert (await upload(small_editor, data)).status_code == 201
    over = await upload(small_editor, images.png((31, 31), color=(9, 9, 9)))
    assert over.status_code == 507
    # The other tenant is unaffected by the first one's usage, whatever it holds.
    for n in range(3):
        r = await upload(roomy_editor, images.png((32 + n, 30), color=(n, 5, 5)))
        assert r.status_code == 201


# --- skins ----------------------------------------------------------------------


async def test_custom_skins_are_private_and_builtins_are_shared(
    engine: AsyncEngine, sites: tuple[Site, Site]
) -> None:
    from app.models import Skin
    from app.tenancy.scope import SYSTEM
    from tests.sessions import new_session

    async with new_session(engine, SYSTEM) as session:
        session.add(
            Skin(
                name="Orbe",
                kind="procedural",
                config=skin_configs.procedural(),
                is_builtin=True,
                is_default=True,
            )
        )
        await session.commit()
    alpha, beta = sites
    created = await alpha.editor.post(
        "/skins", json={"name": "de alpha", "config": skin_configs.procedural()}
    )
    assert created.status_code == 201
    skin_id = created.json()["id"]

    names_alpha = [
        s["name"] for s in (await alpha.editor.get("/skins")).json()["items"]
    ]
    names_beta = [s["name"] for s in (await beta.editor.get("/skins")).json()["items"]]
    assert names_alpha == ["Orbe", "de alpha"]
    assert names_beta == ["Orbe"]
    assert (
        await beta.editor.patch(f"/skins/{skin_id}", json={"name": "x"})
    ).status_code == 404
    assert (await beta.editor.delete(f"/skins/{skin_id}")).status_code == 404

    # A map of the other tenant cannot adopt it either.
    mine = await _map(beta.editor)
    r = await beta.editor.put(
        f"/course-maps/{mine['id']}/appearance",
        json={"settings": mine["settings"], "defaultSkinId": skin_id, "skinRules": []},
    )
    assert r.status_code == 422
    # Built-in skins cannot be changed by anybody.
    builtin = next(
        s for s in (await beta.editor.get("/skins")).json()["items"] if s["builtin"]
    )
    assert (
        await beta.editor.patch(f"/skins/{builtin['id']}", json={"name": "x"})
    ).status_code == 403
    assert (await beta.editor.delete(f"/skins/{builtin['id']}")).status_code == 403
