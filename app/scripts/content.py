"""Operator tool that publishes reviewed trainer content.

    python -m app.scripts.content publish --pack en/A1 \\
        --activities en-a1-1.1-dialogue en-a1-1.1-grammar \\
        --forms a1-form-a --reviewer "Name Surname" [--confirm]

`--activities` and `--forms` take ids, or `all` for everything that is
`reviewed`. Only content a person has already moved to `reviewed` can be
published: a `draft` is refused, so publishing is never the first look.

Without `--confirm` it is a dry run: it lists what would change and writes
nothing. With `--confirm` it sets `status: published` (and `reviewed_by` /
`reviewed_at` from `--reviewer` and now) in the content files, flips the pack
itself to `published` (the audio route serves only the audio of a published
pack), lints the whole pack, and if the linter finds an error (for example a
published item whose audio file is missing) puts every file back as it was and
exits 1. On success it appends one line to `<content-dir>/publication-audit.jsonl`.

The change is to files in the repository: commit it and deploy. Nothing runs
against the database, and it never touches `TRAINER_SERVE_DRAFTS`.
"""

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.trainer.lint import lint
from app.trainer.pack import Pack, PackError, load_pack

Out = Callable[[str], None]
AUDIT_FILE = "publication-audit.jsonl"
_PACK_KEY = re.compile(r"^[a-z]{2,3}/[A-Z0-9]{1,4}$")
_STATUS_LINE = re.compile(r'^(  "status": )"(?:draft|reviewed)"', re.MULTILINE)


@dataclass
class Plan:
    pack: Pack
    activities: list[str] = field(default_factory=list)
    forms: list[str] = field(default_factory=list)
    pack_flips: bool = False
    problems: list[str] = field(default_factory=list)


def _pick(
    wanted: list[str], available: dict[str, str], kind: str, problems: list[str]
) -> list[str]:
    """`available` maps id -> status. `all` means every `reviewed` one."""
    if wanted == ["all"]:
        return sorted(i for i, status in available.items() if status == "reviewed")
    chosen: list[str] = []
    for item_id in wanted:
        status = available.get(item_id)
        if status is None:
            problems.append(f"{kind} {item_id}: not in the pack")
        elif status == "published":
            problems.append(f"{kind} {item_id}: already published")
        elif status != "reviewed":
            problems.append(
                f"{kind} {item_id}: is {status}; a person must review it first "
                "(set its status to reviewed)"
            )
        else:
            chosen.append(item_id)
    return chosen


def make_plan(pack: Pack, activities: list[str], forms: list[str]) -> Plan:
    plan = Plan(pack)
    plan.activities = _pick(
        activities,
        {a.id: a.status for a in pack.activities.values()},
        "activity",
        plan.problems,
    )
    plan.forms = _pick(
        forms,
        {f.form_id: f.status for f in pack.forms.values()},
        "form",
        plan.problems,
    )
    if not plan.problems and not (plan.activities or plan.forms):
        plan.problems.append("nothing to publish: no reviewed item matches")
    plan.pack_flips = bool(plan.activities or plan.forms) and (
        pack.meta.status != "published"
    )
    return plan


def _rewrite_json(path: Path, reviewer: str, now: datetime) -> None:
    raw = path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    data = json.loads(raw)
    data["status"] = "published"
    data["reviewed_by"] = reviewer
    data["reviewed_at"] = now.isoformat().replace("+00:00", "Z")
    text = json.dumps(data, indent=2, ensure_ascii=False).replace("\n", newline)
    path.write_bytes((text + newline).encode("utf-8"))


def _rewrite_pack_status(path: Path) -> None:
    raw = path.read_bytes().decode("utf-8")
    text, count = _STATUS_LINE.subn(r'\1"published"', raw)
    if count != 1:
        raise PackError([f"{path.name}: cannot find the top-level status line"])
    path.write_bytes(text.encode("utf-8"))


def apply_plan(plan: Plan, reviewer: str, now: datetime) -> list[tuple[Path, bytes]]:
    """Writes the change and returns the original bytes of every file touched."""
    root = plan.pack.root
    targets = [root / "activities" / f"{i}.json" for i in plan.activities]
    targets += [root / "assessments" / f"{i}.json" for i in plan.forms]
    saved: list[tuple[Path, bytes]] = []
    for path in targets:
        saved.append((path, path.read_bytes()))
        _rewrite_json(path, reviewer, now)
    if plan.pack_flips:
        meta = root / "pack.json"
        saved.append((meta, meta.read_bytes()))
        _rewrite_pack_status(meta)
    return saved


def restore(saved: list[tuple[Path, bytes]]) -> None:
    for path, original in saved:
        path.write_bytes(original)


def _publish(args: argparse.Namespace, now: datetime, out: Out) -> int:
    reviewer = args.reviewer.strip()
    if not reviewer:
        out("--reviewer must name the person who reviewed the content")
        return 2
    if not _PACK_KEY.match(args.pack):
        out("--pack must look like en/A1")
        return 2
    base = Path(args.content_dir)
    root = base / args.pack
    try:
        pack = load_pack(root)
    except PackError as exc:
        out("The pack does not load:")
        for problem in exc.problems:
            out(f"  {problem}")
        return 1
    plan = make_plan(pack, args.activities or [], args.forms or [])
    if plan.problems:
        out("Nothing was changed:")
        for problem in plan.problems:
            out(f"  {problem}")
        return 1

    out(f"Pack {args.pack} (version {pack.content_version}), reviewer: {reviewer}")
    out("These change from reviewed to published:")
    for activity_id in plan.activities:
        out(f"  activity {activity_id}")
    for form_id in plan.forms:
        out(f"  form {form_id}")
    if plan.pack_flips:
        out(f"  pack.json: {pack.meta.status} -> published (its audio is served)")
    if not args.confirm:
        out("Dry run: nothing was written. Run again with --confirm to publish.")
        return 0

    saved = apply_plan(plan, reviewer, now)
    try:
        report = lint(load_pack(root))
    except PackError as exc:
        failures = exc.problems
    else:
        failures = [] if report.ok else report.errors
    if failures:
        restore(saved)
        out("The linter refuses the result; every file was put back. Fix:")
        for problem in failures:
            out(f"  {problem}")
        return 1

    after = load_pack(root).content_version
    entry = {
        "at": now.isoformat().replace("+00:00", "Z"),
        "action": "publish",
        "pack": args.pack,
        "activities": plan.activities,
        "forms": plan.forms,
        "pack_status_flipped": plan.pack_flips,
        "reviewer": reviewer,
        "content_version_before": pack.content_version,
        "content_version_after": after,
    }
    with (base / AUDIT_FILE).open("a", encoding="utf-8") as audit:
        audit.write(json.dumps(entry, ensure_ascii=False) + "\n")
    out(f"Published. Content version {pack.content_version} -> {after}.")
    out(f"Audit line appended to {base / AUDIT_FILE}. Commit the changes and deploy.")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    now: datetime | None = None,
    out: Out = print,
) -> int:
    parser = argparse.ArgumentParser(prog="app.scripts.content")
    sub = parser.add_subparsers(dest="command", required=True)
    publish = sub.add_parser("publish", help="publish reviewed content")
    publish.add_argument("--pack", required=True, help="language/level, e.g. en/A1")
    publish.add_argument("--activities", nargs="+", help="activity ids, or 'all'")
    publish.add_argument("--forms", nargs="+", help="exam form ids, or 'all'")
    publish.add_argument("--reviewer", required=True, help="who reviewed the content")
    publish.add_argument(
        "--confirm", action="store_true", help="write; without it, a dry run"
    )
    publish.add_argument(
        "--content-dir",
        default=os.environ.get("TRAINER_CONTENT_DIR", "content/trainer"),
    )
    args = parser.parse_args(argv)
    return _publish(args, now or datetime.now(UTC), out)


if __name__ == "__main__":
    sys.exit(main())
