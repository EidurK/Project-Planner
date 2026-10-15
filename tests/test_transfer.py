import json
import zipfile

import pytest

from app import storage, subagents, transfer


@pytest.fixture
def data(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setattr(storage, "DATA_DIR", d)
    storage.seed_defaults()
    return d


def make_zip(path, contents, manifest=True, **manifest_over):
    with zipfile.ZipFile(path, "w") as zf:
        if manifest:
            m = {"format": transfer.FORMAT, "version": 1, "files": sorted(contents)} | manifest_over
            zf.writestr("manifest.json", json.dumps(m))
        for name, content in contents.items():
            zf.writestr(name, content)
    return path


def good_files():
    return {"prompt.md": "PM prompt", "projects.json": json.dumps({"projects": [
        {"id": "p1", "name": "Plant", "status": "planned"},
        {"id": "p2", "name": "Mine", "parent_id": "p1", "depends_on": ["p1"], "assignees": ["Sam"]},
    ]}), "employees.json": json.dumps({"employees": [{"name": "Sam", "position": "Lead"}]}),
        "organization.md": "We build.", "agents/planner.md": "PLAN", "agents/reviewer.md": "REV",
        "agents/gatherer.md": "GATH"}


def test_roundtrip_with_custom_subagent(data, tmp_path):
    storage.write_text("prompt.md", "My main prompt")
    storage.write_text("projects.json", json.dumps({"projects": [{"id": "p1", "name": "A", "status": "done"}]}))
    storage.write_text("organization.md", "Org text")
    subagents._manage("create_subagent", {"name": "risk_analyst", "description": "d", "prompt": "RISK",
                                          "tools": ["list_projects"]})
    z = tmp_path / "x.zip"
    summary = transfer.export_zip(z)
    assert summary["custom_agents"] == ["risk_analyst"]
    names = set(zipfile.ZipFile(z).namelist())
    assert {"manifest.json", "prompt.md", "projects.json", "organization.md", "employees.json",
            "agents/registry.json", "agents/risk_analyst.md", "agents/planner.md"} <= names
    assert not any("secret" in n or "settings" in n or "history" in n for n in names)

    # a friend's fresh install, with their own key and settings that must survive
    friend = tmp_path / "friend"
    storage.DATA_DIR = friend
    storage.seed_defaults()
    storage.write_text("secrets.json", '{"api_key": "mine"}')
    storage.write_text("settings.json", '{"model": "claude-opus-5-5"}')
    storage.write_text("agents/stale_custom.md", "old")
    storage.write_text("prompt.md", "friend prompt")
    result = transfer.import_zip(z)
    assert storage.read_text("prompt.md") == "My main prompt"
    assert json.loads(storage.read_text("projects.json"))["projects"][0]["name"] == "A"
    assert storage.read_text("organization.md") == "Org text"
    assert storage.read_text("agents/risk_analyst.md") == "RISK"
    assert not (friend / "agents" / "stale_custom.md").exists()
    assert "risk_analyst" in subagents.agents()
    assert storage.read_text("secrets.json") == '{"api_key": "mine"}'
    assert storage.read_text("settings.json") == '{"model": "claude-opus-5-5"}'
    # what was replaced is recoverable
    backup = friend / "history" / result["backup"].split("/")[-1]
    assert (backup / "prompt.md").read_text() == "friend prompt"
    assert (backup / "agents" / "stale_custom.md").read_text() == "old"


def test_good_archive_validates(tmp_path):
    parsed = transfer.parse(transfer.read_zip(make_zip(tmp_path / "g.zip", good_files())))
    assert parsed["summary"]["projects"] == 2
    p2 = json.loads(parsed["files"]["projects.json"])["projects"][1]
    assert p2["priority"] == "medium" and p2["estimated_hours"] == 0  # defaults filled in


@pytest.mark.parametrize("mutate, message", [
    (lambda f: f.pop("prompt.md"), "prompt.md is missing"),
    (lambda f: f.pop("projects.json"), "projects.json is missing"),
    (lambda f: f.update({"prompt.md": "  "}), "empty"),
    (lambda f: f.update({"prompt.md": "x" * 30_000}), "too long"),
    (lambda f: f.update({"evil.sh": "rm -rf"}), "Unexpected file"),
    (lambda f: f.update({"../escape.md": "x"}), "Unexpected file"),
    (lambda f: f.update({"agents/UPPER.md": "x"}), "Unexpected file"),
    (lambda f: f.update({"agents/ghost.md": "x"}), "not listed"),
    (lambda f: f.update({"projects.json": "not json"}), "not valid JSON"),
    (lambda f: f.update({"projects.json": '{"nope": []}'}), "'projects'"),
    (lambda f: f.update({"projects.json": '{"projects": [{"id": "p1", "name": "A", "status": "weird"}]}'}), "status"),
    (lambda f: f.update({"projects.json": '{"projects": [{"id": "x", "name": "A"}]}'}), "id must look like"),
    (lambda f: f.update({"projects.json": '{"projects": [{"id": "p1", "name": "A"}, {"id": "p1", "name": "B"}]}'}), "duplicate id"),
    (lambda f: f.update({"projects.json": '{"projects": [{"id": "p1", "name": "A", "estimated_hours": -3}]}'}), "estimated_hours"),
    (lambda f: f.update({"projects.json": '{"projects": [{"id": "p1", "name": "A", "due_date": "soon"}]}'}), "due_date"),
    (lambda f: f.update({"projects.json": '{"projects": [{"id": "p1", "name": "A", "depends_on": ["p9"]}]}'}), "does not exist"),
    (lambda f: f.update({"projects.json": '{"projects": [{"id": "p1", "name": "A", "depends_on": ["p1"]}]}'}), "itself"),
    (lambda f: f.update({"projects.json": json.dumps({"projects": [
        {"id": "p1", "name": "A", "depends_on": ["p2"]}, {"id": "p2", "name": "B", "depends_on": ["p1"]}]})}), "loop"),
    (lambda f: f.update({"projects.json": json.dumps({"projects": [
        {"id": "p1", "name": "A", "parent_id": "p2"}, {"id": "p2", "name": "B", "parent_id": "p1"}]})}), "loop"),
    (lambda f: f.update({"employees.json": '{"employees": [{"name": "Sam"}]}'}), "name and a position"),
    (lambda f: f.update({"employees.json": json.dumps({"employees": [
        {"name": "Sam", "position": "a"}, {"name": "sam", "position": "b"}]})}), "duplicate employee"),
    (lambda f: f.update({"agents/registry.json": json.dumps({"agents": {"bot": {"description": "d", "tools": ["delete_project"]}}}),
                         "agents/bot.md": "x"}), "tools must be"),
    (lambda f: f.update({"agents/registry.json": json.dumps({"agents": {"reviewer": {"description": "d"}}})}), "reserved"),
    (lambda f: f.update({"agents/registry.json": json.dumps({"agents": {"bot": {"description": "d"}}})}), "bot.md is missing"),
])
def test_invalid_archives_are_rejected(tmp_path, mutate, message):
    files = good_files()
    mutate(files)
    with pytest.raises(transfer.InvalidExport, match=message):
        transfer.parse(transfer.read_zip(make_zip(tmp_path / "bad.zip", files)))


def test_manifest_problems(tmp_path):
    def check(msg, **kw):
        with pytest.raises(transfer.InvalidExport, match=msg):
            transfer.parse(transfer.read_zip(make_zip(tmp_path / "m.zip", good_files(), **kw)))

    check("manifest.json is missing", manifest=False)
    check("wrong manifest format", format="something-else")
    check("version 2", version=2)
    check("no valid version", version="1")
    check("does not match", files=["prompt.md"])


def test_not_a_zip_and_missing_file(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_text("this is not a zip")
    with pytest.raises(transfer.InvalidExport, match="not a valid zip"):
        transfer.read_zip(bad)
    with pytest.raises(transfer.InvalidExport, match="not found"):
        transfer.read_zip(tmp_path / "nope.zip")


def test_size_and_encryption_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(transfer, "MAX_ENTRY_BYTES", 100)
    files = good_files() | {"prompt.md": "x" * 500}
    with pytest.raises(transfer.InvalidExport, match="too large"):
        transfer.read_zip(make_zip(tmp_path / "big.zip", files))
    monkeypatch.setattr(transfer, "MAX_ENTRIES", 2)
    monkeypatch.setattr(transfer, "MAX_ENTRY_BYTES", 10**6)
    with pytest.raises(transfer.InvalidExport, match="Too many"):
        transfer.read_zip(make_zip(tmp_path / "many.zip", good_files()))


def test_failed_import_changes_nothing(data, tmp_path):
    storage.write_text("prompt.md", "keep me")
    files = good_files() | {"projects.json": '{"projects": [{"id": "p1", "name": "A", "status": "weird"}]}'}
    with pytest.raises(transfer.InvalidExport):
        transfer.import_zip(make_zip(tmp_path / "bad.zip", files))
    assert storage.read_text("prompt.md") == "keep me"
    assert not (data / "history").exists() or not list((data / "history").glob("import-backup-*"))


def test_builtin_prompts_missing_from_archive_fall_back_to_defaults(data, tmp_path):
    files = good_files()
    for k in ("agents/planner.md", "agents/reviewer.md", "agents/gatherer.md"):
        files.pop(k)
    transfer.import_zip(make_zip(tmp_path / "min.zip", files))
    assert storage.read_text("agents/planner.md").strip()  # restored from defaults/


def test_export_refuses_data_that_would_not_import(data, tmp_path):
    storage.write_text("projects.json", '{"projects": [{"id": "p1", "name": "A", "status": "weird"}]}')
    with pytest.raises(transfer.InvalidExport):
        transfer.export_zip(tmp_path / "x.zip")
    assert not (tmp_path / "x.zip").exists()
