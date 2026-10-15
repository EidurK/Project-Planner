"""Export / import of a whole setup as one zip: all prompts, the project data and the organization data.

Never included: the API key, the chosen model/settings, or the history of old versions.
Import validates the whole archive before it changes anything, and backs up what it replaces.
"""
import copy
import json
import re
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from . import storage, subagents

FORMAT = "pm-agent-export"
VERSION = 1

MAX_ENTRIES = 100
MAX_ENTRY_BYTES = 2_000_000
MAX_TOTAL_BYTES = 10_000_000

MAIN_PROMPT = "prompt.md"
PROJECTS = "projects.json"
ORGANIZATION = "organization.md"
EMPLOYEES = "employees.json"
REGISTRY = subagents.REGISTRY  # agents/registry.json
BUILTIN_AGENT_FILES = {f"agents/{a['file']}" for a in subagents.BUILTIN.values()}
AGENT_FILE_RE = re.compile(r"^agents/[a-z][a-z0-9_]*\.md$")

PROJECT_DEFAULTS = {"description": "", "status": "proposed", "assignees": [], "estimated_hours": 0,
                    "hours_logged": 0, "priority": "medium", "due_date": None, "notes": "",
                    "parent_id": None, "depends_on": []}


class InvalidExport(ValueError):
    """The archive is not a valid export. The message says why."""


# ---------------------------------------------------------------- validation of the pieces

def _text(name: str, raw: bytes, *, allow_empty: bool = False) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidExport(f"{name} is not valid UTF-8 text") from None
    if not allow_empty and not text.strip():
        raise InvalidExport(f"{name} is empty")
    if len(text) > storage.MAX_PROMPT_CHARS:
        raise InvalidExport(f"{name} is too long (max {storage.MAX_PROMPT_CHARS} characters)")
    return text


def _json(name: str, raw: bytes, key: str) -> list | dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise InvalidExport(f"{name} is not valid JSON ({e})") from None
    if not isinstance(data, dict) or key not in data:
        raise InvalidExport(f"{name} must be a JSON object with a '{key}' entry")
    return data[key]


def validate_projects(items) -> list[dict]:
    if not isinstance(items, list):
        raise InvalidExport("projects.json: 'projects' must be a list")
    out, ids = [], set()
    for i, raw in enumerate(items, 1):
        if not isinstance(raw, dict):
            raise InvalidExport(f"projects.json: entry {i} is not an object")
        p = dict(raw)  # keep the original key order; only fill in what is missing
        for k, v in PROJECT_DEFAULTS.items():
            p.setdefault(k, copy.deepcopy(v))
        pid = p.get("id")
        where = f"projects.json: project {pid!r}" if isinstance(pid, str) else f"projects.json: entry {i}"
        if not isinstance(pid, str) or not re.fullmatch(r"p\d+", pid):
            raise InvalidExport(f"{where}: id must look like p1, p2, ...")
        if pid in ids:
            raise InvalidExport(f"{where}: duplicate id")
        ids.add(pid)
        if not isinstance(p.get("name"), str) or not p["name"].strip():
            raise InvalidExport(f"{where}: name is required")
        if not isinstance(p["description"], str) or not isinstance(p["notes"], str):
            raise InvalidExport(f"{where}: description and notes must be text")
        if p["status"] not in storage.STATUSES:
            raise InvalidExport(f"{where}: status must be one of {list(storage.STATUSES)}")
        if p["priority"] not in storage.PRIORITIES:
            raise InvalidExport(f"{where}: priority must be one of {list(storage.PRIORITIES)}")
        for k in ("assignees", "depends_on"):
            if not isinstance(p[k], list) or not all(isinstance(x, str) for x in p[k]):
                raise InvalidExport(f"{where}: {k} must be a list of text")
        for k in ("estimated_hours", "hours_logged"):
            v = p[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise InvalidExport(f"{where}: {k} must be a non-negative number")
        if p["due_date"] is not None:
            try:
                datetime.strptime(p["due_date"], "%Y-%m-%d")
            except (TypeError, ValueError):
                raise InvalidExport(f"{where}: due_date must be YYYY-MM-DD or null") from None
        if p["parent_id"] is not None and not isinstance(p["parent_id"], str):
            raise InvalidExport(f"{where}: parent_id must be a project id or null")
        p.setdefault("updated_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        out.append(p)
    by_id = {p["id"]: p for p in out}
    for p in out:
        where = f"projects.json: project {p['id']}"
        if p["parent_id"] is not None and p["parent_id"] not in by_id:
            raise InvalidExport(f"{where}: parent_id {p['parent_id']} does not exist")
        seen, cur = {p["id"]}, p["parent_id"]
        while cur:
            if cur in seen:
                raise InvalidExport(f"{where}: parent_id forms a loop")
            seen.add(cur)
            cur = by_id[cur]["parent_id"]
        for d in p["depends_on"]:
            if d == p["id"]:
                raise InvalidExport(f"{where}: depends on itself")
            if d not in by_id:
                raise InvalidExport(f"{where}: depends_on {d} does not exist")
            if storage._has_path(by_id, d, p["id"]):
                raise InvalidExport(f"{where}: dependency loop through {d}")
    return out


def validate_employees(items) -> list[dict]:
    if not isinstance(items, list):
        raise InvalidExport("employees.json: 'employees' must be a list")
    out, names = [], set()
    for i, e in enumerate(items, 1):
        if (not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"].strip()
                or not isinstance(e.get("position"), str) or not e["position"].strip()):
            raise InvalidExport(f"employees.json: entry {i} needs a name and a position (both text)")
        name, position = e["name"].strip(), e["position"].strip()
        if len(name) > 80 or len(position) > 100:
            raise InvalidExport(f"employees.json: entry {i} is too long")
        if name.lower() in names:
            raise InvalidExport(f"employees.json: duplicate employee '{name}'")
        names.add(name.lower())
        out.append({"name": name, "position": position})
    return out


def validate_registry(agents) -> dict:
    if not isinstance(agents, dict):
        raise InvalidExport("agents/registry.json: 'agents' must be an object")
    out = {}
    for name, spec in agents.items():
        where = f"agents/registry.json: subagent {name!r}"
        if not subagents.NAME_RE.match(name) or name in subagents.RESERVED:
            raise InvalidExport(f"{where}: invalid or reserved name")
        if not isinstance(spec, dict) or not isinstance(spec.get("description"), str) or not spec["description"].strip():
            raise InvalidExport(f"{where}: description is required")
        tools = spec.get("tools", [])
        if not isinstance(tools, list) or any(t not in subagents.ALLOWED_TOOLS for t in tools):
            raise InvalidExport(f"{where}: tools must be a list drawn from {subagents.ALLOWED_TOOLS}")
        turns = spec.get("max_turns", 15)
        if isinstance(turns, bool) or not isinstance(turns, int) or not 1 <= turns <= 40:
            raise InvalidExport(f"{where}: max_turns must be a whole number from 1 to 40")
        out[name] = {"description": spec["description"].strip(), "tools": list(dict.fromkeys(tools)), "max_turns": turns}
    return out


# ---------------------------------------------------------------- the archive

def build_files(data_dir: Path | None = None) -> dict[str, str]:
    """Collect the files that go into an export (relative path -> text), validating them like an import would."""
    root = data_dir or storage.DATA_DIR
    files: dict[str, str] = {}

    def read(rel: str) -> str:
        p = root / rel
        return p.read_text(encoding="utf-8") if p.exists() else ""

    files[MAIN_PROMPT] = read(MAIN_PROMPT)
    for rel in sorted(BUILTIN_AGENT_FILES):
        files[rel] = read(rel)
    reg_raw = read(REGISTRY)
    if reg_raw:
        files[REGISTRY] = reg_raw
        try:
            custom = json.loads(reg_raw).get("agents", {})
        except (json.JSONDecodeError, AttributeError):
            raise InvalidExport("Your data/agents/registry.json is damaged, so it can't be exported") from None
        for name in custom:
            files[f"agents/{name}.md"] = read(f"agents/{name}.md")
    files[PROJECTS] = read(PROJECTS) or '{"projects": []}'
    files[ORGANIZATION] = read(ORGANIZATION)
    files[EMPLOYEES] = read(EMPLOYEES) or '{"employees": []}'
    parse({k: v.encode("utf-8") for k, v in files.items()}, require_manifest=False)  # export only what imports cleanly
    return files


def parse(contents: dict[str, bytes], *, require_manifest: bool = True) -> dict:
    """Validate the decoded archive contents. Returns {'files': {rel: text}, 'summary': {...}}. Raises InvalidExport."""
    allowed = {MAIN_PROMPT, PROJECTS, ORGANIZATION, EMPLOYEES, REGISTRY} | BUILTIN_AGENT_FILES | {"manifest.json"}
    for name in contents:
        if name not in allowed and not AGENT_FILE_RE.match(name):
            raise InvalidExport(f"Unexpected file in archive: {name}")

    if require_manifest:
        if "manifest.json" not in contents:
            raise InvalidExport("manifest.json is missing, so this is not an export from this app")
        try:
            manifest = json.loads(contents["manifest.json"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise InvalidExport("manifest.json is not valid JSON") from None
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise InvalidExport("This zip is not an export from this app (wrong manifest format)")
        version = manifest.get("version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise InvalidExport("manifest.json has no valid version")
        if version > VERSION:
            raise InvalidExport(f"This export is version {version}; this app only understands up to version {VERSION}. "
                               "Update the app and try again.")
        listed = manifest.get("files")
        if not isinstance(listed, list) or set(listed) != set(contents) - {"manifest.json"}:
            raise InvalidExport("manifest.json does not match the files in the archive (incomplete or modified zip)")

    for required in (MAIN_PROMPT, PROJECTS):
        if required not in contents:
            raise InvalidExport(f"{required} is missing from the archive")

    files: dict[str, str] = {}
    files[MAIN_PROMPT] = _text(MAIN_PROMPT, contents[MAIN_PROMPT])
    projects = validate_projects(_json(PROJECTS, contents[PROJECTS], "projects"))
    files[PROJECTS] = json.dumps({"projects": projects}, indent=2, ensure_ascii=False) + "\n"

    employees = []
    if EMPLOYEES in contents:
        employees = validate_employees(_json(EMPLOYEES, contents[EMPLOYEES], "employees"))
    files[EMPLOYEES] = json.dumps({"employees": employees}, indent=2, ensure_ascii=False) + "\n"
    if ORGANIZATION in contents:
        files[ORGANIZATION] = _text(ORGANIZATION, contents[ORGANIZATION], allow_empty=True)

    registry = validate_registry(_json(REGISTRY, contents[REGISTRY], "agents")) if REGISTRY in contents else {}
    if registry:
        files[REGISTRY] = json.dumps({"agents": registry}, indent=2) + "\n"
    expected_agent_files = BUILTIN_AGENT_FILES | {f"agents/{n}.md" for n in registry}
    for rel in sorted(expected_agent_files):
        if rel in contents:
            files[rel] = _text(rel, contents[rel])
        elif rel not in BUILTIN_AGENT_FILES:
            raise InvalidExport(f"{rel} is missing (needed by custom subagent '{Path(rel).stem}')")
    for rel in contents:
        if AGENT_FILE_RE.match(rel) and rel not in expected_agent_files:
            raise InvalidExport(f"{rel} belongs to a subagent that is not listed in agents/registry.json")

    return {"files": files, "summary": {"projects": len(projects), "employees": len(employees),
                                        "custom_agents": sorted(registry),
                                        "agent_prompts": sum(1 for r in files if r.startswith("agents/") and r.endswith(".md"))}}


def export_zip(path: Path) -> dict:
    """Write the export to `path`. Returns a summary."""
    storage.seed_defaults()
    files = build_files()
    summary = parse({k: v.encode("utf-8") for k, v in files.items()}, require_manifest=False)["summary"]
    manifest = {"format": FORMAT, "version": VERSION, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "files": sorted(files), "summary": summary}
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        for rel in sorted(files):
            zf.writestr(rel, files[rel])
    return summary


def read_zip(path: Path) -> dict[str, bytes]:
    """Safely read an archive into memory (size/entry limits, no odd paths, no encryption)."""
    if not path.is_file():
        raise InvalidExport(f"File not found: {path}")
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise InvalidExport(f"{path.name} is not a valid zip file") from None
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ENTRIES:
            raise InvalidExport(f"Too many files in the archive (max {MAX_ENTRIES})")
        if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES or any(i.file_size > MAX_ENTRY_BYTES for i in infos):
            raise InvalidExport("The archive is too large to be an export from this app")
        contents: dict[str, bytes] = {}
        for i in infos:
            if i.flag_bits & 0x1:
                raise InvalidExport("Encrypted zip files are not supported")
            if i.filename in contents:
                raise InvalidExport(f"Duplicate entry in archive: {i.filename}")
            try:
                contents[i.filename] = zf.read(i)
            except (zipfile.BadZipFile, RuntimeError, OSError) as e:
                raise InvalidExport(f"The archive is damaged ({e})") from None
        return contents


def import_zip(path: Path) -> dict:
    """Validate `path`, then replace the current prompts/projects/organization with its contents.

    The replaced files are copied to data/history/import-backup-<time>/ first. Returns a summary
    (including 'backup'). Raises InvalidExport before touching anything if the archive is invalid.
    """
    parsed = parse(read_zip(path))
    files = parsed["files"]
    root = storage.DATA_DIR
    root.mkdir(parents=True, exist_ok=True)

    # 1. back up everything that is about to be replaced
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = root / "history" / f"import-backup-{stamp}"
    current = [MAIN_PROMPT, PROJECTS, ORGANIZATION, EMPLOYEES] + sorted(
        str(p.relative_to(root)).replace("\\", "/") for p in (root / "agents").glob("*") if p.is_file())
    for rel in current:
        src = root / rel
        if src.exists():
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            (backup / rel).write_bytes(src.read_bytes())

    # 2. replace: the old custom subagents go (they are in the backup), the imported ones come in
    for p in (root / "agents").glob("*"):
        if p.is_file():
            p.unlink()
    for rel, text in files.items():
        storage.write_text(rel, text)
    if ORGANIZATION not in files:
        storage.write_text(ORGANIZATION, (storage.DEFAULTS_DIR / ORGANIZATION).read_text(encoding="utf-8"))
    storage.seed_defaults()  # fills built-in prompts the archive did not contain
    return {**parsed["summary"], "backup": str(backup)}


def default_export_name() -> str:
    return f"pm-agent-export-{date.today().isoformat()}.zip"
