"""Organization/scope knowledge: data/organization.md (free text) and data/employees.json (name + position)."""
import json
import threading

from . import storage

ORG_FILE = "organization.md"
EMP_FILE = "employees.json"
_lock = threading.Lock()


# ---- organization.md ----

def read_organization() -> str:
    return storage.read_text(ORG_FILE)


def write_organization(content: str) -> str:
    """Replace organization.md (previous version backed up). Returns the backup name."""
    return storage.write_text_with_backup(ORG_FILE, content, "organization")


def list_org_versions() -> list[str]:
    return storage.list_backups("organization")


def read_org_version(name: str) -> str:
    return storage.read_backup("organization", name)


# ---- employees ----

def _load() -> list[dict]:
    raw = storage.read_text(EMP_FILE)
    try:
        return json.loads(raw).get("employees", []) if raw else []
    except json.JSONDecodeError:
        return []


def _save(employees: list[dict]) -> None:
    storage.write_text(EMP_FILE, json.dumps({"employees": employees}, indent=2, ensure_ascii=False) + "\n")


def _clean(value, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{field} too long (max {limit} chars)")
    return value


def _find(employees: list[dict], name: str) -> dict:
    for e in employees:
        if e["name"].lower() == str(name).strip().lower():
            return e
    raise ValueError(f"No employee named '{name}'")


def list_employees() -> list[dict]:
    with _lock:
        return _load()


def add_employee(name: str, position: str) -> dict:
    name, position = _clean(name, "name", 80), _clean(position, "position", 100)
    with _lock:
        employees = _load()
        if any(e["name"].lower() == name.lower() for e in employees):
            raise ValueError(f"Employee '{name}' already exists")
        emp = {"name": name, "position": position}
        employees.append(emp)
        _save(employees)
        return emp


def update_employee(name: str, new_name: str | None = None, position: str | None = None) -> dict:
    """Change an employee's name and/or position. A rename also updates their project assignments."""
    with _lock:
        employees = _load()
        emp = _find(employees, name)
        old = emp["name"]
        if new_name is not None:
            new_name = _clean(new_name, "name", 80)
            if new_name.lower() != old.lower() and any(e["name"].lower() == new_name.lower() for e in employees):
                raise ValueError(f"Employee '{new_name}' already exists")
            emp["name"] = new_name
        if position is not None:
            emp["position"] = _clean(position, "position", 100)
        _save(employees)
    if emp["name"] != old:
        storage.rename_assignee(old, emp["name"])
    return emp


def remove_employee(name: str) -> dict:
    """Remove from the employee list. Their project assignments are left alone (reported by overview)."""
    with _lock:
        employees = _load()
        emp = _find(employees, name)
        _save([e for e in employees if e is not emp])
    still = [p["name"] for p in storage.list_projects(assignee=emp["name"]) if p.get("status") != "done"]
    return {"removed": emp, "still_assigned_to_unfinished": still}


# ---- views ----

def overview() -> dict:
    """Employees with the projects they are assigned to, plus assignees who are not in the employee list."""
    projects = storage.list_projects()
    by_id = {p["id"]: p for p in projects}
    employees = list_employees()

    def entries(person: str) -> list[dict]:
        out = []
        for p in projects:
            if person.lower() in [a.lower() for a in p.get("assignees", [])]:
                parent = by_id.get(p.get("parent_id"))
                out.append({"id": p["id"], "name": p["name"], "status": p.get("status", "proposed"),
                            "parent": parent["name"] if parent else None,
                            "estimated_hours": p.get("estimated_hours", 0), "hours_logged": p.get("hours_logged", 0)})
        order = {"in_progress": 0, "blocked": 1, "planned": 2, "proposed": 3, "done": 4}
        return sorted(out, key=lambda e: order.get(e["status"], 5))

    def remaining(es: list[dict]) -> float:
        return sum(max(0, e["estimated_hours"] - e["hours_logged"]) for e in es if e["status"] != "done")

    rows = []
    for e in employees:
        es = entries(e["name"])
        rows.append({**e, "projects": es, "remaining_hours": remaining(es)})
    known = {e["name"].lower() for e in employees}
    unlisted = sorted({a for p in projects for a in p.get("assignees", []) if a.lower() not in known}, key=str.lower)
    return {"employees": rows,
            "unlisted": [{"name": n, "projects": entries(n), "remaining_hours": remaining(entries(n))} for n in unlisted]}


def context_text() -> str:
    """Appended to every agent's system prompt so all agents know the organization and its scope."""
    org = read_organization().strip() or "(not filled in yet)"
    employees = list_employees()
    people = "\n".join(f"- {e['name']} — {e['position']}" for e in employees) or "(none recorded)"
    return ("\n\n---\n# Organization & scope knowledge\n"
            "(Maintained in organization.md and employees.json. The project manager can update them.)\n\n"
            f"{org}\n\n## Employees\n{people}")
