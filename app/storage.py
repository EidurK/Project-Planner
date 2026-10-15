"""File access for the agent. Only the two data files are ever touched."""
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

DEFAULTS_DIR = Path(__file__).resolve().parent.parent / "defaults"
DATA_DIR = Path(os.environ.get("PM_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
MAX_PROMPT_CHARS = 20_000
STATUSES = ("proposed", "planned", "in_progress", "blocked", "done")
PRIORITIES = ("low", "medium", "high")

_lock = threading.Lock()


def _projects_path() -> Path:
    return DATA_DIR / "projects.json"


def _history_dir() -> Path:
    return DATA_DIR / "history"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---- prompts (the main prompt and subagent prompts share the same read/backup logic) ----

def read_text(rel: str) -> str:
    path = DATA_DIR / rel
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write_text_with_backup(rel: str, content: str, prefix: str) -> str:
    """Back up the current file to history/<prefix>-<stamp>.md, then replace it. Returns the backup name."""
    if not content or not content.strip():
        raise ValueError("Prompt cannot be empty")
    if len(content) > MAX_PROMPT_CHARS:
        raise ValueError(f"Prompt too long (max {MAX_PROMPT_CHARS} chars)")
    with _lock:
        backup = ""
        old = read_text(rel)
        if old:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = f"{prefix}-{stamp}.md"
            _atomic_write(_history_dir() / backup, old)
        _atomic_write(DATA_DIR / rel, content)
        return backup


def write_text(rel: str, content: str) -> None:
    with _lock:
        _atomic_write(DATA_DIR / rel, content)


def delete_text(rel: str) -> None:
    with _lock:
        (DATA_DIR / rel).unlink(missing_ok=True)


def list_backups(prefix: str) -> list[str]:
    d = _history_dir()
    return sorted((p.name for p in d.glob(f"{prefix}-*.md")), reverse=True) if d.exists() else []


def read_backup(prefix: str, name: str) -> str:
    # name must be one of the listed backups (no path traversal)
    if name not in list_backups(prefix):
        raise ValueError(f"Unknown prompt version: {name}")
    return (_history_dir() / name).read_text(encoding="utf-8")


def read_prompt() -> str:
    return read_text("prompt.md")


def write_prompt(content: str) -> str:
    return write_text_with_backup("prompt.md", content, "prompt")


def list_prompt_versions() -> list[str]:
    return list_backups("prompt")


def read_prompt_version(name: str) -> str:
    return read_backup("prompt", name)


# ---- projects ----

def _load() -> dict:
    p = _projects_path()
    if not p.exists():
        return {"projects": []}
    return json.loads(p.read_text(encoding="utf-8"))


def _save(db: dict) -> None:
    _atomic_write(_projects_path(), json.dumps(db, indent=2, ensure_ascii=False) + "\n")


def _clean(fields: dict, partial: bool) -> dict:
    out = {}
    if "name" in fields:
        name = str(fields["name"]).strip()
        if not name:
            raise ValueError("name cannot be empty")
        out["name"] = name
    elif not partial:
        raise ValueError("name is required")
    if "description" in fields:
        out["description"] = str(fields["description"])
    if "notes" in fields:
        out["notes"] = str(fields["notes"])
    if "status" in fields:
        if fields["status"] not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        out["status"] = fields["status"]
    if "priority" in fields:
        if fields["priority"] not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")
        out["priority"] = fields["priority"]
    if "assignees" in fields:
        a = fields["assignees"]
        if not isinstance(a, list) or not all(isinstance(x, str) for x in a):
            raise ValueError("assignees must be a list of strings")
        out["assignees"] = [x.strip() for x in a if x.strip()]
    for key in ("estimated_hours", "hours_logged"):
        if key in fields:
            v = fields[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f"{key} must be a non-negative number")
            out[key] = v
    if "parent_id" in fields:
        out["parent_id"] = fields["parent_id"] or None
    if "depends_on" in fields:
        d = fields["depends_on"]
        if not isinstance(d, list) or not all(isinstance(x, str) for x in d):
            raise ValueError("depends_on must be a list of project ids")
        out["depends_on"] = list(dict.fromkeys(d))
    if "due_date" in fields:
        v = fields["due_date"]
        if v:
            try:
                datetime.strptime(v, "%Y-%m-%d")
            except (TypeError, ValueError):
                raise ValueError("due_date must be YYYY-MM-DD") from None
        out["due_date"] = v or None
    return out


def _has_path(by_id: dict, start: str, target: str) -> bool:
    """True if `target` is reachable from `start` by following depends_on."""
    seen, stack = set(), [start]
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen or cur not in by_id:
            continue
        seen.add(cur)
        stack.extend(by_id[cur].get("depends_on", []))
    return False


def _validate_refs(db: dict, pid: str, clean: dict) -> None:
    by_id = {p["id"]: p for p in db["projects"]}
    parent = clean.get("parent_id")
    if parent:
        if parent == pid:
            raise ValueError("A project cannot be its own parent")
        if parent not in by_id:
            raise ValueError(f"Unknown parent_id {parent}")
        # no parent cycles
        cur = by_id[parent].get("parent_id")
        while cur:
            if cur == pid:
                raise ValueError("parent_id would create a parent cycle")
            cur = by_id.get(cur, {}).get("parent_id")
    deps = clean.get("depends_on", [])
    for d in deps:
        if d == pid:
            raise ValueError("A project cannot depend on itself")
        if d not in by_id:
            raise ValueError(f"Unknown id in depends_on: {d}")
        if _has_path(by_id, d, pid):
            raise ValueError(f"depends_on {d} would create a dependency cycle")


def check_plan(ids: list[str] | None = None) -> list[str]:
    """Deterministic sanity checks. Returns a list of problem descriptions."""
    with _lock:
        projects = _load()["projects"]
    by_id = {p["id"]: p for p in projects}
    scope = [by_id[i] for i in ids if i in by_id] if ids else projects
    problems = []
    for i in ids or []:
        if i not in by_id:
            problems.append(f"{i}: no such project")
    for p in scope:
        label = f"{p['id']} ({p['name']})"
        if not p.get("estimated_hours"):
            problems.append(f"{label}: no hour estimate")
        if not p.get("assignees"):
            problems.append(f"{label}: nobody assigned")
        for d in p.get("depends_on", []):
            dep = by_id.get(d)
            if dep is None:
                problems.append(f"{label}: depends on missing project {d}")
            elif _has_path(by_id, d, p["id"]):
                problems.append(f"{label}: dependency cycle via {d}")
        children = [c for c in projects if c.get("parent_id") == p["id"]]
        if children:
            total = sum(c.get("estimated_hours", 0) for c in children)
            est = p.get("estimated_hours", 0)
            if est and abs(total - est) > 0.25 * est:
                problems.append(f"{label}: subtasks sum to {total}h but parent estimate is {est}h")
        if p.get("parent_id") and p["parent_id"] not in by_id:
            problems.append(f"{label}: parent {p['parent_id']} is missing")
    return problems


def list_projects(status: str | None = None, assignee: str | None = None) -> list[dict]:
    with _lock:
        projects = _load()["projects"]
    if status:
        projects = [p for p in projects if p.get("status") == status]
    if assignee:
        projects = [p for p in projects if assignee.lower() in [a.lower() for a in p.get("assignees", [])]]
    return projects


def get_project(project_id: str) -> dict:
    for p in list_projects():
        if p["id"] == project_id:
            return p
    raise ValueError(f"No project with id {project_id}")


def add_project(fields: dict) -> dict:
    clean = _clean(fields, partial=False)
    with _lock:
        db = _load()
        nums = [int(p["id"][1:]) for p in db["projects"] if p["id"][1:].isdigit()]
        project = {
            "id": f"p{max(nums, default=0) + 1}",
            "description": "",
            "status": "proposed",
            "assignees": [],
            "estimated_hours": 0,
            "hours_logged": 0,
            "priority": "medium",
            "due_date": None,
            "notes": "",
            "parent_id": None,
            "depends_on": [],
            **clean,
            "updated_at": _now(),
        }
        _validate_refs(db, project["id"], project)
        db["projects"].append(project)
        _save(db)
        return project


def update_project(project_id: str, fields: dict) -> dict:
    clean = _clean(fields, partial=True)
    with _lock:
        db = _load()
        for p in db["projects"]:
            if p["id"] == project_id:
                _validate_refs(db, project_id, clean)
                p.update(clean)
                p["updated_at"] = _now()
                _save(db)
                return p
    raise ValueError(f"No project with id {project_id}")


def delete_project(project_id: str, replaced_by: str | None = None) -> dict:
    """Delete a project (backing up the whole file first).

    Things that depended on it, and its subtasks, are re-pointed to `replaced_by`
    if given (e.g. the surviving duplicate), otherwise the links are dropped.
    """
    with _lock:
        db = _load()
        by_id = {p["id"]: p for p in db["projects"]}
        if project_id not in by_id:
            raise ValueError(f"No project with id {project_id}")
        if replaced_by is not None:
            if replaced_by == project_id or replaced_by not in by_id:
                raise ValueError(f"replaced_by must be another existing project, got {replaced_by}")
        removed = by_id[project_id]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        _atomic_write(_history_dir() / f"projects-{stamp}.json", json.dumps(db, indent=2, ensure_ascii=False) + "\n")
        db["projects"] = [p for p in db["projects"] if p["id"] != project_id]
        for other in db["projects"]:
            deps = other.get("depends_on", [])
            if project_id in deps:
                deps.remove(project_id)
                if replaced_by and replaced_by != other["id"] and replaced_by not in deps:
                    deps.append(replaced_by)
            if other.get("parent_id") == project_id:
                other["parent_id"] = replaced_by
        # re-pointing could (rarely) create a cycle; drop the offending links rather than corrupt the plan
        by_id = {p["id"]: p for p in db["projects"]}
        for other in db["projects"]:
            other["depends_on"] = [d for d in other.get("depends_on", []) if not _has_path(by_id, d, other["id"])]
        _save(db)
        return removed


def rename_assignee(old: str, new: str) -> int:
    """Rename a person in every project's assignees. Returns how many projects changed."""
    with _lock:
        db = _load()
        n = 0
        for p in db["projects"]:
            names = p.get("assignees", [])
            if any(a.lower() == old.lower() for a in names):
                p["assignees"] = list(dict.fromkeys(new if a.lower() == old.lower() else a for a in names))
                p["updated_at"] = _now()
                n += 1
        if n:
            _save(db)
        return n


def seed_defaults() -> list[str]:
    """Copy starter files from defaults/ into the data dir for anything missing. Never overwrites."""
    copied = []
    for src in sorted(DEFAULTS_DIR.rglob("*")):
        if src.is_file():
            rel = src.relative_to(DEFAULTS_DIR)
            dest = DATA_DIR / rel
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(src.read_bytes())
                copied.append(str(rel))
    return copied
