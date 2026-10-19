"""Run the Odoo AI IAP service (iap-apps/iap_ai) locally.

`iap` with no arguments starts the service, or shows its status and recent
logs if it is already running. Dev databases are pointed at it with
`iap connect <db>`, which sets their `ai.endpoint` parameter.
"""
import os
import signal
import socket
import subprocess
import time
import urllib.request
from collections import deque
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

HOME = Path.home()
# The IAP service runs on its own Odoo version, independent of the client worktrees
ODOO_DIR = Path(os.getenv("IAP_ODOO_DIR", HOME / "repos/Odoo/wt/saas-19.4"))
APPS_DIR = Path(os.getenv("IAP_APPS_DIR", HOME / "repos/iap-apps"))
PYTHON = Path(os.getenv("IAP_PYTHON", ODOO_DIR / ".venv/bin/python"))
PORT = int(os.getenv("IAP_PORT", "8086"))
DB = os.getenv("IAP_DB", "iap_ai_db")
STATE_DIR = Path(os.getenv("XDG_STATE_HOME", HOME / ".local/state")) / "iap"
PID_FILE = STATE_DIR / "iap.pid"
LOG_FILE = STATE_DIR / "iap.log"
ENDPOINT = f"http://localhost:{PORT}"
API_KEYS = ("OPENAI_API_KEY", "GEMINI_API_KEY")
START_TIMEOUT = 90

app = typer.Typer(
    help="Run the Odoo AI IAP service locally.",
    no_args_is_help=False,
    add_completion=False,
)
console = Console()


# ---------------------------------------------------------------------------
# process state
# ---------------------------------------------------------------------------

def _read_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        PID_FILE.unlink(missing_ok=True)
        return None
    except PermissionError:
        pass
    return pid


def _port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/web/health", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def _uptime(pid: int) -> str:
    res = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True, text=True)
    return res.stdout.strip() or "?"


def _db_exists(name: str) -> bool:
    res = subprocess.run(
        ["psql", "-d", "postgres", "-Atc", f"SELECT 1 FROM pg_database WHERE datname = '{name}'"],
        capture_output=True, text=True,
    )
    return res.stdout.strip() == "1"


def _git_branch(path: Path) -> str:
    res = subprocess.run(["git", "-C", str(path), "branch", "--show-current"], capture_output=True, text=True)
    return res.stdout.strip() or "?"


def _tail(n: int) -> list[str]:
    try:
        with LOG_FILE.open(errors="replace") as f:
            return list(deque(f, maxlen=n))
    except FileNotFoundError:
        return []


# ---------------------------------------------------------------------------
# display
# ---------------------------------------------------------------------------

def _style_line(line: str) -> str:
    line = line.rstrip("\n").replace("[", r"\[")
    for level, style in ((" ERROR ", "red"), (" CRITICAL ", "bold red"), (" WARNING ", "yellow")):
        if level in line:
            return f"[{style}]{line}[/]"
    return f"[dim]{line}[/]" if " INFO " in line else line


def _show_logs(n: int) -> None:
    lines = _tail(n)
    if lines:
        console.print(Panel("\n".join(_style_line(l) for l in lines), title=f"last {len(lines)} log lines", title_align="left", border_style="dim"))


def _show_status(pid: int | None, log_lines: int = 15) -> None:
    healthy = _healthy()
    if pid and healthy:
        state = "[bold green]● running[/]"
    elif pid:
        state = "[bold yellow]● starting / unhealthy[/]"
    elif _port_open():
        state = f"[bold yellow]● port {PORT} busy, not started by iap[/]"
    else:
        state = "[bold red]○ stopped[/]"

    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold")
    t.add_column()
    t.add_row("state", state)
    if pid:
        t.add_row("pid", f"{pid}  (up {_uptime(pid)})")
    t.add_row("endpoint", ENDPOINT)
    t.add_row("database", DB)
    t.add_row("odoo", f"{ODOO_DIR}  [dim]({_git_branch(ODOO_DIR / 'odoo')})[/]")
    t.add_row("iap-apps", f"{APPS_DIR}  [dim]({_git_branch(APPS_DIR)})[/]")
    t.add_row("log", str(LOG_FILE))
    console.print(Panel(t, title="local IAP", title_align="left"))
    if log_lines:
        _show_logs(log_lines)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def _start(update: bool = False) -> None:
    if pid := _read_pid():
        console.print("[yellow]Already running.[/]")
        _show_status(pid)
        return
    if _port_open():
        console.print(f"[red]Port {PORT} is already in use by another process.[/] Stop it, or set IAP_PORT.")
        raise typer.Exit(1)
    for path in (ODOO_DIR / "odoo/odoo-bin", APPS_DIR / "iap_ai", PYTHON):
        if not path.exists():
            console.print(f"[red]Missing {path}[/] (see IAP_ODOO_DIR / IAP_APPS_DIR / IAP_PYTHON)")
            raise typer.Exit(1)
    if not any(os.getenv(k) for k in API_KEYS):
        console.print(f"[yellow]None of {', '.join(API_KEYS)} is set; AI calls will fail.[/]")

    addons = ",".join(str(p) for p in (
        ODOO_DIR / "odoo/addons", ODOO_DIR / "enterprise", APPS_DIR / "iap_ai", APPS_DIR / "iap_common",
    ))
    first_run = not _db_exists(DB)
    cmd = [
        # The Odoo venv provides Odoo's deps; iap_ai's own deps are layered on top for this run only
        "uv", "run", "--no-project", "--python", str(PYTHON),
        "--with-requirements", str(APPS_DIR / "iap_ai/requirements.txt"),
        # iap_common imports it but declares no requirements file
        "--with", "decorator",
        "python", str(ODOO_DIR / "odoo/odoo-bin"),
        "--addons-path", addons,
        "-d", DB,
        "--http-port", str(PORT),
        "--workers=1",
        "--dev=all",
    ]
    if first_run:
        cmd += ["-i", "odoo_ai"]
    elif update:
        cmd += ["-u", "odoo_ai"]

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "ODOO_AI_TEST_MODE": "1"}  # no IAP billing/credit checks
    with LOG_FILE.open("w") as log:
        proc = subprocess.Popen(
            cmd, cwd=ODOO_DIR, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group, so stop() takes the workers down too
        )
    PID_FILE.write_text(str(proc.pid))

    what = "installing odoo_ai in a new database" if first_run else "updating odoo_ai" if update else "starting"
    with console.status(f"[bold]{what}[/] on {ENDPOINT} ..."):
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                PID_FILE.unlink(missing_ok=True)
                console.print(f"[red]IAP exited with code {proc.returncode}.[/]")
                _show_logs(30)
                raise typer.Exit(1)
            if _healthy():
                break
            time.sleep(0.5)
        else:
            console.print(f"[yellow]Not healthy after {START_TIMEOUT}s; still running, check the logs.[/]")
    _show_status(proc.pid, log_lines=0)
    console.print(f"Point a database at it with [bold]iap connect <db>[/].")


def _stop() -> bool:
    pid = _read_pid()
    if not pid:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
        for _ in range(40):
            time.sleep(0.25)
            os.killpg(pid, 0)
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    PID_FILE.unlink(missing_ok=True)
    return True


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Start the IAP service, or show its status and logs if it is already running."""
    if ctx.invoked_subcommand is None:
        _start()


@app.command()
def start(update: bool = typer.Option(False, "--update", "-u", help="Update the odoo_ai module on start.")) -> None:
    """Start the IAP service in the background."""
    _start(update)


@app.command()
def stop() -> None:
    """Stop the IAP service."""
    console.print("[green]Stopped.[/]" if _stop() else "[dim]Not running.[/]")


@app.command()
def restart(update: bool = typer.Option(False, "--update", "-u", help="Update the odoo_ai module on start.")) -> None:
    """Restart the IAP service (e.g. after switching the iap-apps branch)."""
    _stop()
    _start(update)


@app.command()
def status(lines: int = typer.Option(15, "--lines", "-n", help="Log lines to show.")) -> None:
    """Show whether the IAP service is running, with recent logs."""
    _show_status(_read_pid(), log_lines=lines)


@app.command()
def logs(
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep following the log."),
    lines: int = typer.Option(50, "--lines", "-n", help="Log lines to show."),
) -> None:
    """Show the IAP service log."""
    if follow:
        os.execvp("tail", ["tail", "-n", str(lines), "-F", str(LOG_FILE)])
    _show_logs(lines)


@app.command()
def connect(
    db: str = typer.Argument(..., help="Client database to point at the local IAP."),
    reset: bool = typer.Option(False, "--reset", help="Remove the override, back to the public Odoo AI endpoint."),
) -> None:
    """Point a dev database's `ai.endpoint` at the local IAP service."""
    if not _db_exists(db):
        console.print(f"[red]Database {db!r} does not exist.[/]")
        raise typer.Exit(1)
    if reset:
        sql = "DELETE FROM ir_config_parameter WHERE key = 'ai.endpoint'"
    else:
        sql = (
            "INSERT INTO ir_config_parameter (key, value) VALUES ('ai.endpoint', :'endpoint') "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
    res = subprocess.run(
        ["psql", "-d", db, "-v", "ON_ERROR_STOP=1", "-v", f"endpoint={ENDPOINT}", "-q"],
        input=sql, capture_output=True, text=True,
    )
    if res.returncode:
        console.print(f"[red]{res.stderr.strip()}[/]")
        raise typer.Exit(1)
    target = "the public Odoo AI endpoint" if reset else ENDPOINT
    console.print(f"[green]{db}[/] now uses {target} (no restart needed).")
