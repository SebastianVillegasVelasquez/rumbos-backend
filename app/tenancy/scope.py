"""Tenant (and course) scoping, enforced below the repositories.

A session carries a `Scope` in `session.info`. From then on every ORM statement
it runs that touches a tenant-owned table is rewritten to include the scope's
filter, and every new tenant-owned row is stamped with the scope's tenant. So
no repository query, present or future, can read or change another tenant's
data by forgetting a `WHERE`: the rows are simply not visible. A session
without a scope that touches those tables fails loudly instead of leaking.

What each table sees (`t` the scope's tenant, `c` its course, when it has one):

- `course_maps`, `bubbles`: `tenant_id = t` and `moodle_course_id = c`;
- `course_map_skin_rules`, `course_map_view3d_bindings`, `publications`: those
  of the maps visible by the rule above;
- `assets`: `tenant_id = t` (a tenant's library is shared by its courses);
- `skins`: `tenant_id = t` or NULL (built-ins), and only `tenant_id = t` to
  change or delete one.

A statement may opt out of the course filter with the execution option
`tenant_wide=True`; only the asset usage checks do, because an asset is used by
maps of every course of the tenant.

`SYSTEM` (no tenant) switches the filter off, for the few things that are not
a tenant's business: serving an image by its unguessable id, and the
maintenance scripts.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import event, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria
from sqlalchemy.orm.util import LoaderCriteriaOption

from app.exceptions import CourseMapNotFoundError
from app.models import (
    Asset,
    Bubble,
    CourseMap,
    CourseMapSkinRule,
    CourseMapView3dBinding,
    Publication,
    Skin,
)

SCOPE_KEY = "rumbos.scope"
TENANT_WIDE = "tenant_wide"

# Tables whose rows carry a `tenant_id`, stamped when they are created.
_STAMPED = (CourseMap, Bubble, Asset, Skin)
# Tables that hang off a map and have no `tenant_id` of their own.
_VIA_MAP = (CourseMapSkinRule, CourseMapView3dBinding, Publication)
_SCOPED = frozenset((*_STAMPED, *_VIA_MAP))


@dataclass(frozen=True)
class Scope:
    """`tenant_id` None is the system scope (no filtering at all); `course_id`
    None means every course of the tenant."""

    tenant_id: uuid.UUID | None
    course_id: int | None = None


SYSTEM = Scope(None)


class ScopeError(RuntimeError):
    """The scope and what a session tries to do disagree (a bug, never a 4xx)."""


class ScopeMissingError(ScopeError):
    """A session touched tenant-owned tables without a scope."""


def bind_scope(session: AsyncSession | Session, scope: Scope) -> None:
    session.info[SCOPE_KEY] = scope


def scope_of(session: AsyncSession | Session) -> Scope | None:
    scope = session.info.get(SCOPE_KEY)
    return scope if isinstance(scope, Scope) else None


def _criteria(scope: Scope, state: ORMExecuteState) -> list[LoaderCriteriaOption]:
    """The loader criteria that make `state`'s statement see only `scope`."""
    tid, course = scope.tenant_id, scope.course_id
    wide = bool(state.execution_options.get(TENANT_WIDE, False))
    writing = state.is_update or state.is_delete
    options: list[LoaderCriteriaOption] = []

    if course is None or wide:
        options.append(
            with_loader_criteria(
                CourseMap, lambda c: c.tenant_id == tid, include_aliases=True
            )
        )
        options.append(
            with_loader_criteria(
                Bubble, lambda c: c.tenant_id == tid, include_aliases=True
            )
        )
        options.append(
            with_loader_criteria(
                CourseMapSkinRule,
                lambda c: c.course_map_id.in_(
                    select(CourseMap.id).where(CourseMap.tenant_id == tid)
                ),
                include_aliases=True,
            )
        )
        options.append(
            with_loader_criteria(
                CourseMapView3dBinding,
                lambda c: c.course_map_id.in_(
                    select(CourseMap.id).where(CourseMap.tenant_id == tid)
                ),
                include_aliases=True,
            )
        )
        options.append(
            with_loader_criteria(
                Publication,
                lambda c: c.course_map_id.in_(
                    select(CourseMap.id).where(CourseMap.tenant_id == tid)
                ),
                include_aliases=True,
            )
        )
    else:
        options.append(
            with_loader_criteria(
                CourseMap,
                lambda c: (c.tenant_id == tid) & (c.moodle_course_id == course),
                include_aliases=True,
            )
        )
        options.append(
            with_loader_criteria(
                Bubble,
                lambda c: (c.tenant_id == tid) & (c.moodle_course_id == course),
                include_aliases=True,
            )
        )
        for model in (CourseMapSkinRule, CourseMapView3dBinding, Publication):
            options.append(_via_course(model, tid, course))

    options.append(
        with_loader_criteria(Asset, lambda c: c.tenant_id == tid, include_aliases=True)
    )
    if writing:
        options.append(
            with_loader_criteria(
                Skin, lambda c: c.tenant_id == tid, include_aliases=True
            )
        )
    else:
        options.append(
            with_loader_criteria(
                Skin,
                lambda c: or_(c.tenant_id == tid, c.tenant_id.is_(None)),
                include_aliases=True,
            )
        )
    return options


def _via_course(
    model: type[CourseMapSkinRule] | type[CourseMapView3dBinding] | type[Publication],
    tid: uuid.UUID | None,
    course: int,
) -> LoaderCriteriaOption:
    return with_loader_criteria(
        model,
        lambda c: c.course_map_id.in_(
            select(CourseMap.id).where(
                CourseMap.tenant_id == tid, CourseMap.moodle_course_id == course
            )
        ),
        include_aliases=True,
    )


@event.listens_for(Session, "do_orm_execute")
def _apply_scope(state: ORMExecuteState) -> None:
    if state.is_insert or state.is_column_load or state.is_relationship_load:
        return
    touched = {mapper.class_ for mapper in state.all_mappers} & _SCOPED
    scope = state.session.info.get(SCOPE_KEY)
    if scope is None:
        if touched:
            names = ", ".join(sorted(cls.__name__ for cls in touched))
            raise ScopeMissingError(f"session has no scope but touches {names}")
        return
    if scope.tenant_id is None:
        return
    # Every scoped table is covered, not only the `touched` ones: a subquery or
    # a join can bring in a table `all_mappers` does not list at the top level.
    # Criteria for a table the statement does not use are inert.
    state.statement = state.statement.options(*_criteria(scope, state))


@event.listens_for(Session, "before_flush")
def _stamp_new_rows(session: Session, flush_context: object, instances: object) -> None:
    for obj in session.new:
        if not isinstance(obj, _STAMPED):
            continue
        scope = session.info.get(SCOPE_KEY)
        if scope is None:
            raise ScopeMissingError(
                f"session has no scope but creates a {type(obj).__name__}"
            )
        if scope.tenant_id is None:
            if obj.tenant_id is None and not isinstance(obj, Skin):
                raise ScopeMissingError(
                    f"a {type(obj).__name__} needs a tenant (system scope given)"
                )
            continue
        if obj.tenant_id is None:
            obj.tenant_id = scope.tenant_id
        elif obj.tenant_id != scope.tenant_id:
            raise ScopeError(
                f"a {type(obj).__name__} of another tenant cannot be created here"
            )
        if (
            scope.course_id is not None
            and isinstance(obj, CourseMap | Bubble)
            and obj.moodle_course_id != scope.course_id
        ):
            # A course the principal cannot see: the same answer as for any
            # other foreign resource.
            raise CourseMapNotFoundError(obj.moodle_course_id)
