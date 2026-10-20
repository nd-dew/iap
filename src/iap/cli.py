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
DB = os.getenv("IAP_DB", "iap")
STATE_DIR = Path(os.getenv("XDG_STATE_HOME", HOME / ".local/state")) / "iap"
PID_FILE = STATE_DIR / "iap.pid"
LOG_FILE = STATE_DIR / "iap.log"
ENDPOINT = f"http://localhost:{PORT}"
REPOS = {
    name: path
    for name, path in (("odoo", ODOO_DIR / "odoo"), ("enterprise", ODOO_DIR / "enterprise"), ("iap-apps", APPS_DIR))
    if path.is_dir()
}
# Provider credentials read by iap_ai: OpenAI, and the Vertex / Gemini Enterprise service account
API_KEYS = ("OPENAI_API_KEY", "GEAP_SERVICE_ACCOUNT")
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


def _git(path: Path, *args: str) -> str | None:
    res = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else None


def _ago(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def _repo_state(path: Path) -> str:
    """Branch, and how it compares to its upstream as of the last fetch."""
    branch = _git(path, "branch", "--show-current") or "?"
    upstream = _git(path, "rev-parse", "--abbrev-ref", "@{u}")
    if not upstream:
        return f"{branch}  [dim](no upstream)[/]"
    ahead, behind = map(int, (_git(path, "rev-list", "--left-right", "--count", "HEAD...@{u}") or "0 0").split())
    if behind:
        state = f"[yellow]{behind} behind {upstream}[/]"
    else:
        state = f"[green]up to date with {upstream}[/]"
    if ahead:
        state += f", [cyan]{ahead} local commit{'s' * (ahead > 1)}[/]"
    if _git(path, "status", "--porcelain", "--untracked-files=no"):
        state += ", [cyan]uncommitted changes[/]"
    fetch_head = _git(path, "rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD")
    try:
        fetched = f"fetched {_ago(time.time() - Path(fetch_head).stat().st_mtime)}"
    except (TypeError, OSError):
        fetched = "never fetched"
    return f"{state}  [dim]({fetched})[/]"


def _fetch(paths: list[Path]) -> None:
    """Fetch only the upstream branch of each repo, in parallel."""
    procs = []
    for path in paths:
        upstream = _git(path, "rev-parse", "--abbrev-ref", "@{u}")
        if upstream:
            remote, _, branch = upstream.partition("/")
            procs.append((path, subprocess.Popen(
                ["git", "-C", str(path), "fetch", "--quiet", remote, branch],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )))
    with console.status("[bold]fetching upstream ..."):
        for path, proc in procs:
            if proc.wait():
                console.print(f"[red]fetch failed in {path}:[/] {proc.stderr.read().strip()}")


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


def _client_rows() -> str:
    dbs = _client_databases()
    if not dbs:
        return "[dim]no local database has the ai module installed[/]"
    last_used = _last_used()
    width = max(map(len, dbs))
    rows = []
    for name, (endpoint, uuid) in sorted(dbs.items(), key=lambda d: (d[1][0] != ENDPOINT, d[0])):
        if endpoint == ENDPOINT:
            age = last_used.get(uuid)
            used = f"last used {_ago(age)}" if age is not None else "never used"
            rows.append(f"[green]●[/] {name:<{width}}  [dim]{used}[/]")
        elif endpoint:
            rows.append(f"[yellow]→[/] {name:<{width}}  [dim]{endpoint}[/]")
        else:
            rows.append(f"[dim]○ {name:<{width}}  public endpoint[/]")
    return "\n".join(rows)


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
    t.add_row("log", str(LOG_FILE))
    t.add_row()
    t.add_row("client dbs", _client_rows())
    t.add_row()
    for name, path in REPOS.items():
        t.add_row(name, f"{path}\n{_repo_state(path)}")
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
    if missing := [k for k in API_KEYS if not os.getenv(k)]:
        console.print(f"[yellow]{', '.join(missing)} not set; calls to that provider will fail.[/]")

    # Enterprise isn't needed by odoo_ai; it's only used if present
    addons = ",".join(str(p) for p in (
        ODOO_DIR / "odoo/addons", ODOO_DIR / "enterprise", APPS_DIR / "iap_ai", APPS_DIR / "iap_common",
    ) if p.is_dir())
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
def status(
    lines: int = typer.Option(15, "--lines", "-n", help="Log lines to show."),
    fetch: bool = typer.Option(False, "--fetch", "-f", help="Fetch upstream first, for fresh 'behind' counts."),
) -> None:
    """Show whether the IAP service is running, how its code compares to upstream, and recent logs."""
    if fetch:
        _fetch(list(REPOS.values()))
    _show_status(_read_pid(), log_lines=lines)


@app.command()
def fetch() -> None:
    """Fetch upstream for the Odoo and iap-apps repos the service runs on, and show the status."""
    _fetch(list(REPOS.values()))
    _show_status(_read_pid(), log_lines=0)


@app.command()
def logs(
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep following the log."),
    lines: int = typer.Option(50, "--lines", "-n", help="Log lines to show."),
) -> None:
    """Show the IAP service log."""
    if follow:
        os.execvp("tail", ["tail", "-n", str(lines), "-F", str(LOG_FILE)])
    _show_logs(lines)


def _client_databases() -> dict[str, tuple[str, str]]:
    """Local databases with the `ai` module installed (other than the IAP's own): {name: (ai.endpoint, database.uuid)}."""
    res = subprocess.run(
        ["psql", "-d", "postgres", "-Atc", "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY datname"],
        capture_output=True, text=True,
    )
    dbs = {}
    for name in res.stdout.split():
        if name in ("postgres", DB):
            continue
        check = subprocess.run(
            ["psql", "-d", name, "-AtF", "\t", "-c",
             "SELECT (SELECT value FROM ir_config_parameter WHERE key = 'ai.endpoint'),"
             " (SELECT value FROM ir_config_parameter WHERE key = 'database.uuid')"
             " FROM ir_module_module WHERE name = 'ai' AND state = 'installed'"],
            capture_output=True, text=True,
        )
        if check.stdout.strip():  # non-Odoo databases just fail the query
            endpoint, _, uuid = check.stdout.rstrip("\n").partition("\t")
            dbs[name] = (endpoint, uuid)
    return dbs


def _last_used() -> dict[str, float]:
    """Seconds since each client dbuuid last sent a completion job to the IAP (sync calls and embeddings leave no trace)."""
    res = subprocess.run(
        ["psql", "-d", DB, "-AtF", "\t", "-c",
         "SELECT dbuuid, EXTRACT(EPOCH FROM now() AT TIME ZONE 'UTC' - max(create_date)) FROM odoo_ai_job GROUP BY dbuuid"],
        capture_output=True, text=True,
    )
    return {uuid: float(age) for uuid, age in (l.split("\t") for l in res.stdout.splitlines())}


def _current_endpoint(db: str) -> str:
    res = subprocess.run(
        ["psql", "-d", db, "-Atc", "SELECT value FROM ir_config_parameter WHERE key = 'ai.endpoint'"],
        capture_output=True, text=True,
    )
    return res.stdout.strip()


def _set_endpoint(db: str, reset: bool) -> str | None:
    """Set (or remove) `ai.endpoint` in a database; returns an error message on failure."""
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
    return res.stderr.strip() if res.returncode else None


@app.command()
def connect(
    db: str = typer.Argument(None, help="Client database to point at the local IAP."),
    all_: bool = typer.Option(False, "--all", "-a", help="Every local database with the ai module installed."),
    reset: bool = typer.Option(False, "--reset", help="Remove the override, back to the public Odoo AI endpoint."),
    force: bool = typer.Option(False, "--force", help="With --all, also change databases set to another endpoint."),
) -> None:
    """Point a dev database's `ai.endpoint` at the local IAP service."""
    if all_ == bool(db):
        console.print("[red]Pass either a database name or --all.[/]")
        raise typer.Exit(1)
    if db and not _db_exists(db):
        console.print(f"[red]Database {db!r} does not exist.[/]")
        raise typer.Exit(1)
    dbs = [db] if db else list(_client_databases())
    if not dbs:
        console.print("[dim]No local database has the ai module installed.[/]")
        return
    target = "the public Odoo AI endpoint" if reset else ENDPOINT
    failed = False
    for name in dbs:
        current = _current_endpoint(name)
        if all_ and not force and current and current != ENDPOINT:
            # Deliberately pointed elsewhere (e.g. ai.test.odoo.com); leave it alone
            console.print(f"[yellow]{name}[/] skipped: uses {current} [dim](--force to change)[/]")
            continue
        if error := _set_endpoint(name, reset):
            console.print(f"[red]{name}: {error}[/]")
            failed = True
        else:
            console.print(f"[green]{name}[/] now uses {target}")
    console.print("[dim]No restart needed.[/]")
    if failed:
        raise typer.Exit(1)
