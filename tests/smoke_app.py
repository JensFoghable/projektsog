"""Smoke test for the app agent (read-only, temporary LOCALAPPDATA, nothing visible).

1. Runs a tiny server-only script under pythonw.exe (no stdout/stderr, like the real app;
   fake collaborators) and exercises it over HTTP: /api/status, the static UI, error paths
   that make the stock http.server write to stderr, and the SSE stream.
2. Loads the real UI (projektsog/web) through the server in an invisible headless Edge with a
   temporary profile and checks that the server's Content-Security-Policy blocks nothing.
3. Parses install.ps1/uninstall.ps1 with the PowerShell parser (they are NOT run), checks the
   UTF-8 BOM and that the Danish literals survive Windows PowerShell 5.1's decoding.
4. Compiles the installer's shortcut code and creates a .lnk in a temp dir (not the Start
   menu), then reads back its AppUserModelID.

Run from the repo root:  python tests/smoke_app.py
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(REPO, "projektsog", "web")
PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
NO_WINDOW = subprocess.CREATE_NO_WINDOW


# -- fake collaborators (shapes as in SPEC §8/§9/§13, enough for the UI to render) ------------

class SmokeIndexer:
    def status(self) -> dict:
        return {"hostname": "SMOKE", "version": "1.0.0", "sources_total": 1,
                "sources_online": 1, "sources_offline": 0, "sources_excluded": 0,
                "sources_ready": 1, "sources_included_online": 1, "entries": 0, "files": 0,
                "dirs": 0, "projects": 0, "scanning": [], "queued": 0, "last_scan_end": None,
                "initial_scan_done": True, "worker": {"running": True, "restarts": 0},
                "db_size": 0}

    def list_sources(self) -> list: return []
    def hosts(self) -> list: return []
    def recent_projects(self, limit: int = 30) -> list: return []

    def search(self, q: str, **kwargs) -> dict:
        return {"query": q, "tokens": [], "took_ms": 0, "total": 0, "truncated": False,
                "results": []}


class SmokeBridge:
    def state(self) -> dict:
        return {"enabled": True, "running": False, "connected": False, "error": None,
                "project": None, "database": None, "clip_count": 0, "updated": None,
                "folders": [], "other_dirs": [], "suggestions": [], "primary": None,
                "offline_clips": 0, "offline_disks": []}


class SmokeController:
    def hotkey_status(self) -> dict:
        return {"spec": "shift+space", "label": "Shift+Mellemrum", "enabled": True,
                "active": False, "mode": None}

    def get_run_at_login(self) -> bool:
        return False


SERVER_SCRIPT = r'''
import json, os, sys, time, traceback
repo, result_path, stop_path, web_dir = sys.argv[1:5]
report = {"stdout_is_none": sys.stdout is None, "stderr_is_none": sys.stderr is None}
try:
    sys.path.insert(0, repo)
    from projektsog.config import Config
    from projektsog.events import EventBus
    from projektsog.server import Server
    from tests.smoke_app import SmokeBridge, SmokeController, SmokeIndexer

    bus = EventBus()
    server = Server(Config(), bus, SmokeIndexer(), SmokeBridge(), SmokeController(),
                    web_dir=web_dir, sse_heartbeat_s=0.5)
    report["port"] = server.start(0)
    with open(result_path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(report, fh)
    os.replace(result_path + ".tmp", result_path)
    deadline = time.monotonic() + 120
    while not os.path.exists(stop_path) and time.monotonic() < deadline:
        bus.publish("status", {"tick": time.time()})
        time.sleep(0.2)
    server.stop()
    with open(result_path + ".done", "w", encoding="utf-8") as fh:
        fh.write("stopped")
except BaseException:
    with open(result_path + ".error", "w", encoding="utf-8") as fh:
        fh.write(traceback.format_exc())
    raise
'''

PS_CHECK = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$result = [ordered]@{}
foreach ($file in $args) {
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($file, [ref] $null, [ref] $errors)
    $strings = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true) | ForEach-Object { $_.Value })
    $result[(Split-Path $file -Leaf)] = [ordered]@{
        errors = @($errors | ForEach-Object { "line $($_.Extent.StartLineNumber): $($_.Message)" })
        strings = $strings
    }
}
$result | ConvertTo-Json -Depth 4 -Compress
'''

SHORTCUT_CHECK = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$install, $outDir = $args
$ast = [System.Management.Automation.Language.Parser]::ParseFile($install, [ref] $null, [ref] $null)
$source = ($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$ShellLinkSource' }, $true) | Select-Object -First 1).Right.Expression.Value
Add-Type -TypeDefinition $source -Language CSharp
$name = 'Smoke.lnk'
$lnk = Join-Path $outDir $name
('Projektsog.Install.ShellLink' -as [type])::Create($lnk, "$env:WINDIR\System32\notepad.exe", '"C:\x\Projekts' + [char]0xF8 + 'g.pyw"', $outDir, '', 'smoke', 'Projektsog.App')
$item = (New-Object -ComObject Shell.Application).NameSpace($outDir).ParseName($name)
[ordered]@{ aumid = $item.ExtendedProperty('System.AppUserModel.ID'); psversion = "$($PSVersionTable.PSVersion)" } | ConvertTo-Json -Compress
'''

EXPECTED_LITERALS = {
    "install.ps1": ["Projektsøg kører – tryk Shift+Mellemrum hvor som helst",
                    "Genstart DaVinci Resolve for at se ‘Projektsøg’ under Workspace ▸ Scripts ▸ Utility",
                    "Projektsøg.pyw", "Projektsog.App", "Projektsøg - Åbn projektmappe.py"],
    "uninstall.ps1": ["Projektsøg", "Projektsøg.lnk", "Projektsøg - Åbn projektmappe.py",
                      "Projektsoeg - Aabn projektmappe.py"],
}


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        print(f"  [{'OK' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
        if not ok:
            self.failures += 1
        return ok


def http_request(port: int, method: str, path: str, *, host: str | None = None,
                 headers: dict[str, str] | None = None, body: bytes | None = None
                 ) -> tuple[int, http.client.HTTPMessage, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"127.0.0.1:{port}")
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        response = conn.getresponse()
        return response.status, response.headers, response.read()
    finally:
        conn.close()


def wait_for_file(path: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def web_dir_for(tmp: str) -> str:
    """The real UI when it exists, else a one-line page."""
    if os.path.isfile(os.path.join(WEB_DIR, "index.html")):
        return WEB_DIR
    web_dir = os.path.join(tmp, "web")
    os.makedirs(web_dir, exist_ok=True)
    with open(os.path.join(web_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write("<!doctype html><title>Projektsøg</title>")
    return web_dir


def smoke_pythonw_server(report: Report, tmp: str) -> None:
    print("Server under pythonw.exe")
    if not report.check(os.path.isfile(PYTHONW), "pythonw.exe found", PYTHONW):
        return
    script = os.path.join(tmp, "pythonw_server.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(SERVER_SCRIPT)
    result_path, stop_path = os.path.join(tmp, "result.json"), os.path.join(tmp, "stop")
    env = dict(os.environ, LOCALAPPDATA=os.path.join(tmp, "localappdata"))
    started = time.monotonic()
    # No stdio redirection: the child gets no standard handles, exactly like a Start-menu launch.
    proc = subprocess.Popen([PYTHONW, script, REPO, result_path, stop_path, web_dir_for(tmp)],
                            env=env, cwd=tmp, creationflags=NO_WINDOW)
    try:
        if not wait_for_file(result_path, 15):
            error_file = result_path + ".error"
            detail = open(error_file, encoding="utf-8").read() if os.path.exists(error_file) else ""
            report.check(False, "server started", detail or f"exit code {proc.poll()}")
            return
        with open(result_path, encoding="utf-8") as fh:
            info = json.load(fh)
        port = info["port"]
        report.check(info["stderr_is_none"] and info["stdout_is_none"],
                     "child really has no stdout/stderr (pythonw conditions)",
                     f"stdout None={info['stdout_is_none']}, stderr None={info['stderr_is_none']}")
        report.check(True, "server started", f"port {port} after {time.monotonic() - started:.2f} s")

        status, headers, body = http_request(port, "GET", "/api/status")
        data = json.loads(body) if status == 200 else {}
        report.check(status == 200 and data.get("hostname") == "SMOKE" and "resolve" in data
                     and data.get("hotkey", {}).get("label") == "Shift+Mellemrum",
                     "GET /api/status", f"HTTP {status}, {len(data)} keys incl. resolve+hotkey")
        timings = []
        for _ in range(20):
            t0 = time.perf_counter()
            http_request(port, "GET", "/api/status")
            timings.append((time.perf_counter() - t0) * 1000)
        report.check(statistics.median(timings) < 50, "/api/status latency",
                     f"median {statistics.median(timings):.1f} ms, max {max(timings):.1f} ms "
                     "(new connection per request)")

        status, headers, body = http_request(port, "GET", "/")
        report.check(status == 200 and headers.get("Content-Type") == "text/html; charset=utf-8"
                     and headers.get("Cache-Control") == "no-store"
                     and "default-src 'self'" in headers.get("Content-Security-Policy", "")
                     and "Projektsøg".encode() in body,
                     "GET / (index.html, no-store, CSP)", f"HTTP {status}, {len(body)} bytes")
        for path, local in (("/app.js", ("web", "app.js")), ("/style.css", ("web", "style.css")),
                            ("/assets/icon.png", ("assets", "icon.png")),
                            ("/favicon.ico", ("assets", "icon.ico"))):
            if os.path.isfile(os.path.join(REPO, "projektsog", *local)):
                status, headers, body = http_request(port, "GET", path)
                report.check(status == 200 and headers.get("Cache-Control") == "no-store",
                             f"GET {path}", f"HTTP {status}, {headers.get('Content-Type')}, "
                                            f"{len(body)} bytes")

        # Paths where the stock http.server writes to stderr (None here): must not break.
        status, _, _ = http_request(port, "GET", "/api/nope")
        report.check(status == 404, "unknown API path -> 404", f"HTTP {status}")
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(f"BREW /api/status HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            reply = sock.recv(4096)
        report.check(reply.startswith(b"HTTP/1.1 501"), "unsupported method -> stdlib 501",
                     reply.split(b"\r\n", 1)[0].decode("latin-1"))
        status, _, _ = http_request(port, "POST", "/api/scan", body=b"{}")
        report.check(status == 403, "POST without X-Projektsog -> 403", f"HTTP {status}")
        status, _, _ = http_request(port, "GET", "/api/status", host="evil.example")
        report.check(status == 403, "foreign Host header -> 403", f"HTTP {status}")

        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            data = b""
            deadline = time.monotonic() + 5
            while b"event: status\ndata: " not in data and time.monotonic() < deadline:
                data += sock.recv(4096)
        report.check(b"text/event-stream" in data and b"event: status\ndata: " in data,
                     "SSE stream delivers events", f"{len(data)} bytes received")

        status, _, _ = http_request(port, "GET", "/api/status")
        report.check(status == 200, "still serving after the error paths", f"HTTP {status}")
    finally:
        with open(stop_path, "w", encoding="utf-8") as fh:
            fh.write("stop")
        stop_started = time.monotonic()
        try:
            code = proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = None
        report.check(code == 0 and os.path.exists(result_path + ".done"),
                     "clean stop", f"exit code {code}, {time.monotonic() - stop_started:.2f} s")


def find_edge() -> str | None:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        path = os.path.join(base or "", "Microsoft", "Edge", "Application", "msedge.exe")
        if base and os.path.isfile(path):
            return path
    return None


def kill_edge_using(profile: str) -> None:
    """Headless Edge exits by itself; make sure nothing with our temp profile lingers."""
    command = ("Get-CimInstance Win32_Process -Filter \"Name = 'msedge.exe'\" | Where-Object "
               "{ $_.CommandLine -and $_.CommandLine.Contains($env:SMOKE_PROFILE) } | "
               "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction "
               "SilentlyContinue }")
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                   env=dict(os.environ, SMOKE_PROFILE=profile), capture_output=True,
                   timeout=60, creationflags=NO_WINDOW)


def smoke_ui_csp(report: Report, tmp: str) -> None:
    print("Real UI under the server's Content-Security-Policy (invisible headless Edge)")
    edge = find_edge()
    if not os.path.isfile(os.path.join(WEB_DIR, "index.html")) or edge is None:
        report.check(True, "skipped", "projektsog/web or Edge not present")
        return
    sys.path.insert(0, REPO)
    from projektsog.config import Config
    from projektsog.events import EventBus
    from projektsog.server import Server
    server = Server(Config(path=os.path.join(tmp, "csp-config.json")), EventBus(),
                    SmokeIndexer(), SmokeBridge(), SmokeController(), web_dir=WEB_DIR)
    port = server.start(0)
    profile = os.path.join(tmp, "edge-profile")
    try:
        # --timeout stops loading after 6 s so --dump-dom returns despite the open SSE stream.
        proc = subprocess.run(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", f"--user-data-dir={profile}",
             "--enable-logging=stderr", "--v=0", "--timeout=6000", "--dump-dom",
             f"http://127.0.0.1:{port}/?panel=settings"],
            capture_output=True, timeout=60, creationflags=NO_WINDOW)
        dom = proc.stdout.decode("utf-8", "replace")
        log_lines = proc.stderr.decode("utf-8", "replace").splitlines()
    except subprocess.TimeoutExpired:
        report.check(False, "headless Edge finished", "timed out after 60 s")
        return
    finally:
        server.stop()
        kill_edge_using(profile)
    violations = [line for line in log_lines
                  if "Content Security Policy" in line or "Refused to" in line]
    report.check(not violations, "no CSP violations",
                 "; ".join(v[-200:] for v in violations[:3]) or f"{len(log_lines)} log lines checked")
    report.check("Søg efter projekt" in dom and "<title>Projektsøg</title>" in dom,
                 "UI rendered (search field + title)", f"{len(dom)} bytes of DOM")


def run_powershell(exe: str, name: str, script_text: str, args: list[str],
                   tmp: str) -> tuple[int, str, str]:
    """Run a checker script; it prints UTF-8 JSON (the console code page is bypassed)."""
    script = os.path.join(tmp, name)
    with open(script, "w", encoding="utf-8-sig") as fh:
        fh.write(script_text)
    proc = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", script, *args], capture_output=True, timeout=180,
                          creationflags=NO_WINDOW)
    return (proc.returncode, proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def smoke_install_scripts(report: Report, tmp: str) -> None:
    print("install.ps1 / uninstall.ps1 (parsed only, never run)")
    files = [os.path.join(REPO, name) for name in ("install.ps1", "uninstall.ps1")]
    for path in files:
        with open(path, "rb") as fh:
            head = fh.read(3)
        report.check(head == b"\xef\xbb\xbf", f"{os.path.basename(path)} starts with a UTF-8 BOM")
    shells = [exe for exe in ("powershell.exe", "pwsh.exe") if shutil.which(exe)]
    for exe in shells:
        code, out, err = run_powershell(exe, "parse_check.ps1", PS_CHECK, files, tmp)
        try:
            parsed = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            report.check(False, f"{exe} parser", (err or out).strip()[:300])
            continue
        for name, result in parsed.items():
            report.check(not result["errors"], f"{exe}: {name} parses",
                         "; ".join(result["errors"]) or "0 errors")
            missing = [s for s in EXPECTED_LITERALS[name]
                       if not any(s in value for value in result["strings"])]
            report.check(not missing, f"{exe}: {name} Danish literals intact",
                         f"missing {missing}" if missing else "")
    if "powershell.exe" in shells:
        out_dir = os.path.join(tmp, "shortcut")
        os.makedirs(out_dir)
        code, out, err = run_powershell("powershell.exe", "shortcut_check.ps1", SHORTCUT_CHECK,
                                        [files[0], out_dir], tmp)
        try:
            result = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            result = {}
        report.check(result.get("aumid") == "Projektsog.App",
                     "installer shortcut code compiles and sets the AppUserModelID",
                     f"PS {result.get('psversion')}, AUMID {result.get('aumid')!r} (temp .lnk)"
                     if result else (err or out).strip()[:300])


def main() -> int:
    sys.stdout.reconfigure(errors="replace")    # a legacy console code page lacks some glyphs
    report = Report()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="projektsog-smoke-app-",
                                     ignore_cleanup_errors=True) as tmp:
        os.environ["LOCALAPPDATA"] = os.path.join(tmp, "localappdata")
        smoke_pythonw_server(report, tmp)
        smoke_ui_csp(report, tmp)
        smoke_install_scripts(report, tmp)
    print(f"{'FAILED' if report.failures else 'PASSED'}: {report.failures} failure(s) "
          f"in {time.monotonic() - started:.1f} s")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
