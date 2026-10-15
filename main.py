#!/usr/bin/env python3
"""Start the Project Manager Agent:  python main.py

On first run this creates a private virtual environment (.venv) and installs the dependencies, then starts the
web app and opens it in your browser. Needs Python 3.10+ and an internet connection for the first run.

Share a setup with a friend:
  python main.py --export [FILE.zip]   save all prompts, projects and organization data to a zip
  python main.py --import FILE.zip     load such a zip (your current data is backed up first)

Options:  --port N   --host ADDR   --no-browser   --yes
"""
import argparse
import hashlib
import os
import socket
import subprocess
import sys
import threading
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = VENV / ".requirements-hash"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def in_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV.resolve()


def bootstrap(argv: list[str]) -> int:
    """Create the venv / install requirements if needed, then re-run this script inside the venv."""
    if not venv_python().exists():
        print("First run: creating a virtual environment in .venv ...")
        venv.create(VENV, with_pip=True)
    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    if not STAMP.exists() or STAMP.read_text() != digest:
        print("Installing dependencies (this can take a minute) ...")
        result = subprocess.run([str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check",
                                 "-q", "-r", str(REQUIREMENTS)])
        if result.returncode != 0:
            print("\nInstalling the dependencies failed. Check your internet connection and try again.")
            return 1
        STAMP.write_text(digest)
    try:
        return subprocess.call([str(venv_python()), str(Path(__file__).resolve()), *argv])
    except KeyboardInterrupt:
        return 0


def free_port(host: str, start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1" if host in ("0.0.0.0", "") else host, port)) != 0:
                return port
    raise SystemExit(f"No free port found between {start} and {start + 19}. Use --port to pick one.")


def serve(args: argparse.Namespace) -> int:
    import uvicorn

    os.chdir(ROOT)
    port = free_port(args.host, args.port)
    if port != args.port:
        print(f"Port {args.port} is busy, using {port} instead.")
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{port}"
    print(f"\nProject Manager Agent is running at {url}\nPress Ctrl+C to stop.\n")
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    try:
        uvicorn.run("app.main:app", host=args.host, port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    return 0


def do_export(target: str, overwrite: bool) -> int:
    from app import transfer

    path = Path(target or transfer.default_export_name()).expanduser()
    if path.exists() and not overwrite:
        print(f"{path} already exists. Choose another name or add --yes to overwrite it.")
        return 1
    try:
        summary = transfer.export_zip(path)
    except transfer.InvalidExport as e:
        print(f"Export failed: {e}")
        return 1
    print(f"Exported {summary['projects']} projects, {summary['employees']} employees and "
          f"{summary['agent_prompts'] + 1} prompts"
          + (f" (custom subagents: {', '.join(summary['custom_agents'])})" if summary["custom_agents"] else "")
          + f" to {path.resolve()}\nThe export contains no API key.")
    return 0


def do_import(source: str, assume_yes: bool) -> int:
    from app import storage, transfer

    path = Path(source).expanduser()
    try:
        parsed = transfer.parse(transfer.read_zip(path))  # validate first, change nothing yet
    except transfer.InvalidExport as e:
        print(f"Import failed: {e}\nNothing was changed.")
        return 1
    s = parsed["summary"]
    print(f"{path.name} is a valid export: {s['projects']} projects, {s['employees']} employees, "
          f"{s['agent_prompts'] + 1} prompts"
          + (f", custom subagents: {', '.join(s['custom_agents'])}" if s["custom_agents"] else "") + ".")
    if not assume_yes:
        if not sys.stdin.isatty():
            print("This would replace your current projects, prompts and organization data. "
                  "Re-run with --yes to confirm.")
            return 1
        print("This REPLACES your current projects, prompts and organization data (a backup is saved first).")
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled. Nothing was changed.")
            return 1
    try:
        result = transfer.import_zip(path)
    except (transfer.InvalidExport, OSError) as e:
        print(f"Import failed: {e}")
        return 1
    print(f"Imported. Your previous data was backed up to {result['backup']}\n"
          "Your API key and settings were not changed. Start the app with: python main.py")
    return 0


def main() -> int:
    if sys.version_info < (3, 10):
        print(f"Python 3.10 or newer is required (you have {sys.version.split()[0]}).")
        return 1
    sys.stdout.reconfigure(line_buffering=True)  # show progress immediately, even when output is piped
    parser = argparse.ArgumentParser(description="Run the Project Manager Agent web app.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--export", nargs="?", const="", metavar="FILE",
                       help="save prompts, projects and organization data to a zip (default name: pm-agent-export-DATE.zip)")
    group.add_argument("--import", dest="import_file", metavar="FILE",
                       help="load a zip made with --export (validated first; your current data is backed up)")
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation (import) / allow overwriting (export)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (default 127.0.0.1 = only this computer; see README before changing)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = parser.parse_args()
    if not in_venv():
        return bootstrap(sys.argv[1:])
    if args.export is not None:
        return do_export(args.export, args.yes)
    if args.import_file:
        return do_import(args.import_file, args.yes)
    return serve(args)


if __name__ == "__main__":
    sys.exit(main())
