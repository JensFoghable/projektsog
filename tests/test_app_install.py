"""install.ps1 / uninstall.ps1: stopping a running Projektsøg never ends another program
(INST-1), and only the one Resolve menu script is installed (known issue 4).

The scripts are never run – they would install or uninstall for real. A PowerShell harness
loads their functions (and a few named constants) from the parsed script and calls them with
test paths. The real process sweep is replaced by a stub, because it would end any Projektsøg
process of this session. The processes that stop logic acts on are dummy Python processes
started by this test (hidden, killed in cleanup); one of them runs the real server so that
Request-Quit is checked against projektsog/server.py.
"""

from __future__ import annotations

import ctypes
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from ctypes import wintypes

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = {name: os.path.join(REPO, name) for name in ("install.ps1", "uninstall.ps1")}
POWERSHELL = shutil.which("powershell.exe")
NO_WINDOW = subprocess.CREATE_NO_WINDOW
MENU_SCRIPT = "Projektsøg - Åbn projektmappe.py"
ASCII_MENU_SCRIPT = "Projektsoeg - Aabn projektmappe.py"
_saved_env: dict[str, str | None] = {}
_tmp: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    _saved_env["LOCALAPPDATA"] = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = _tmp.name


def tearDownModule() -> None:
    if _saved_env.get("LOCALAPPDATA") is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = _saved_env["LOCALAPPDATA"]
    _tmp.cleanup()


HARNESS = r'''
param([string] $PlanPath)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3.0
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
$ast = [System.Management.Automation.Language.Parser]::ParseFile($plan.script, [ref] $null, [ref] $null)
$functions = [ordered]@{}
foreach ($node in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    $functions[$node.Name] = $node.Extent.Text
    . ([ScriptBlock]::Create($node.Extent.Text))
}
foreach ($node in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $false)) {
    if (@($plan.constants) -contains $node.Left.Extent.Text.TrimStart('$')) {
        . ([ScriptBlock]::Create($node.Extent.Text))
    }
}
$realSweep = ${function:Get-LeftoverProcesses}
function Get-LeftoverProcesses { return @() }       # a test never sweeps real processes
foreach ($property in $plan.variables.PSObject.Properties) {
    Set-Variable -Name $property.Name -Value $property.Value -Scope Script
}
$result = [ordered]@{}
switch ($plan.action) {
    'functions' { $result['functions'] = $functions }
    'stop' {
        # An app started as administrator, seen from a normal PowerShell: Windows hides only
        # its command line (R2-INST-1). Everything else about these processes is real.
        $hidden = @(foreach ($case in $plan.cases) {
            if ($case.PSObject.Properties.Name -contains 'hidden_pid') { [int] $case.hidden_pid } })
        function Get-CimInstance {
            param([string] $ClassName, [string] $Filter, $ErrorAction)
            foreach ($p in @(CimCmdlets\Get-CimInstance -ClassName $ClassName -Filter $Filter -ErrorAction SilentlyContinue)) {
                if ($hidden -notcontains [int] $p.ProcessId) { $p; continue }
                [pscustomobject]@{ ProcessId = $p.ProcessId; Name = $p.Name; SessionId = $p.SessionId
                                   CreationDate = $p.CreationDate; CommandLine = $null }
            }
        }
        $kills = New-Object System.Collections.Generic.List[int]
        function Stop-Process {             # recorded; it only ever reaches this test's dummies
            param([int] $Id, [switch] $Force, $ErrorAction)
            $kills.Add($Id)
            Microsoft.PowerShell.Management\Stop-Process -Id $Id -Force -ErrorAction SilentlyContinue
        }
        foreach ($case in $plan.cases) {
            $script:InstanceFile = $case.instance_file
            $kills.Clear()
            $wait = 8000
            if ($case.PSObject.Properties.Name -contains 'wait_ms') { $wait = [int] $case.wait_ms }
            $output = @(Stop-Projektsog -QuitWaitMs $wait 6>&1)
            $result[$case.id] = [ordered]@{
                stopped = @($output | Where-Object { $_ -is [bool] })
                file_left = Test-Path -LiteralPath $case.instance_file
                kills = @($kills)
                said = (@($output | Where-Object { $_ -isnot [bool] } | ForEach-Object { "$_" }) -join ' ').Trim()
            }
        }
    }
    'fake-processes' {
        $own = Get-CurrentSessionId
        $created = (Get-Date).AddMinutes(-5)
        $fakeProcesses = @(foreach ($f in $plan.fake_processes) {
            $session = $own
            if (-not $f.own_session) { $session = $own + 1 }
            [pscustomobject]@{ ProcessId = [int] $f.pid; Name = [string] $f.name;
                               CommandLine = [string] $f.cmd; SessionId = $session;
                               CreationDate = $created }
        })
        function Get-CimInstance {          # honours the two WQL filter forms the scripts use
            param([string] $ClassName, [string] $Filter, $ErrorAction)
            if ($Filter -match '^ProcessId = (\d+)$') {
                $wanted = [int] $Matches[1]
                return $fakeProcesses | Where-Object { $_.ProcessId -eq $wanted }
            }
            $names = @([regex]::Matches($Filter, "Name = '([^']+)'") | ForEach-Object { $_.Groups[1].Value })
            return $fakeProcesses | Where-Object { $names -contains $_.Name }
        }
        $result['swept'] = @(& $realSweep | ForEach-Object { $_.ProcessId })
        Set-Content -LiteralPath $InstanceFile -Value '{}'
        $result['verified'] = @($fakeProcesses | Where-Object {
            Get-InstanceProcess ([pscustomobject]@{ Pid = $_.ProcessId; Port = 1 }) } |
            ForEach-Object { $_.ProcessId })
        $result['unreadable'] = @($fakeProcesses | Where-Object {
            Get-InstanceProcess ([pscustomobject]@{ Pid = $_.ProcessId; Port = 1 }) -Unreadable } |
            ForEach-Object { $_.ProcessId })
    }
    'install-resolve' { $result['installed'] = Install-ResolveScripts }
    'remove-resolve' { Remove-ResolveScripts }
}
'RESULT ' + (ConvertTo-Json $result -Compress -Depth 5)
'''

# -- dummy processes ------------------------------------------------------------------------
SLEEPER = "import time; time.sleep(120)"
PARENT_OF_SLEEPER = (
    "import subprocess, sys, time; "
    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], "
    "creationflags=0x08000000); print(c.pid, flush=True); time.sleep(120)")
# A stand-in app: the real server; POST /api/quit (with the token) makes it exit with code 0.
QUITTABLE_APP = r"""
import os, sys, threading
sys.path.insert(0, sys.argv[1])
from projektsog.config import Config
from projektsog.events import EventBus
from projektsog.server import Server

class Controller:
    exiting = threading.Event()

    def request_exit(self):
        self.exiting.set()
        threading.Timer(0.2, os._exit, (0,)).start()

server = Server(Config(path=sys.argv[2]), EventBus(), None, None, Controller(),
                web_dir=sys.argv[3], assets_dir=sys.argv[3])
print(server.start(0), flush=True)
threading.Event().wait()
"""
# The same, hung: it answers POST /api/quit (and notes it in the file argv[4]) but stays.
STUCK_APP = QUITTABLE_APP.replace(
    "        self.exiting.set()\n        threading.Timer(0.2, os._exit, (0,)).start()\n",
    "        with open(sys.argv[4], 'a', encoding='utf-8') as fh:\n"
    "            fh.write('quit\\n')\n")
assert STUCK_APP != QUITTABLE_APP

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
_kernel32.TerminateProcess.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.CloseHandle.restype = wintypes.BOOL
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


class _ProcessHandle:
    """A handle to a process this test did not start directly (it cannot be confused with a
    later process that reuses the PID)."""

    def __init__(self, pid: int) -> None:
        self.handle = _kernel32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def alive(self) -> bool:
        code = wintypes.DWORD()
        return bool(_kernel32.GetExitCodeProcess(self.handle, ctypes.byref(code))
                    and code.value == _STILL_ACTIVE)

    def close(self) -> None:
        if self.handle:
            if self.alive():
                _kernel32.TerminateProcess(self.handle, 1)
            _kernel32.CloseHandle(self.handle)
            self.handle = None


class _QuitRecorder(http.server.ThreadingHTTPServer):
    """Where a stale instance.json points: records every request, answers 500."""

    daemon_threads = True

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str | None]] = []
        recorder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                recorder.requests.append((self.command, self.path,
                                          self.headers.get("X-Projektsog")))
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *args: object) -> None:
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def close(self) -> None:
        self.shutdown()
        self.server_close()


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is not available")
class InstallScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.harness = os.path.join(self.tmp, "harness.ps1")
        with open(self.harness, "w", encoding="utf-8-sig") as fh:
            fh.write(HARNESS)

    # -- helpers ----------------------------------------------------------------------------
    def run_harness(self, script: str, action: str, **plan: object) -> dict:
        plan.update(script=SCRIPTS[script], action=action)
        plan.setdefault("variables", {})
        plan.setdefault("constants", [])
        plan_path = os.path.join(self.tmp, f"plan-{uuid.uuid4().hex}.json")
        with open(plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, ensure_ascii=False)
        proc = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                               "Bypass", "-File", self.harness, plan_path],
                              capture_output=True, timeout=120, creationflags=NO_WINDOW)
        out = proc.stdout.decode("utf-8", "replace")
        results = [line for line in out.splitlines() if line.startswith("RESULT ")]
        if proc.returncode != 0 or not results:
            self.fail(f"{script} {action}: exit {proc.returncode}\n{out}\n"
                      f"{proc.stderr.decode('utf-8', 'replace')}")
        return json.loads(results[-1][len("RESULT "):])

    def spawn(self, code: str, *extra: str, reads_line: bool = False
              ) -> tuple[subprocess.Popen, str | None]:
        proc = subprocess.Popen([sys.executable, "-c", code, *extra], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=NO_WINDOW)
        self.addCleanup(self._reap, proc)
        line = proc.stdout.readline().decode("ascii", "replace").strip() if reads_line else None
        return proc, line

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.wait(10)
        proc.stdout.close()

    def recorder(self) -> _QuitRecorder:
        server = _QuitRecorder()
        self.addCleanup(server.close)
        return server

    def instance_file(self, name: str, pid: int, port: int) -> str:
        folder = os.path.join(self.tmp, name)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "instance.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pid": pid, "port": port}, fh)
        return path

    def stop_variables(self) -> dict:
        return {"AppName": "Projektsøg", "EdgeProfile": os.path.join(self.tmp, "edge-profile")}

    # -- INST-1 -------------------------------------------------------------------------------
    def test_stop_only_ever_ends_a_verified_projektsog(self) -> None:
        web_dir = os.path.join(self.tmp, "web")
        os.makedirs(web_dir)
        for script in SCRIPTS:
            with self.subTest(script=script):
                cases, expect = [], {}
                # 1. Stale file, the PID now belongs to another program: never touched.
                foreign, _ = self.spawn(SLEEPER)
                foreign_quits = self.recorder()
                cases.append({"id": "foreign", "instance_file": self.instance_file(
                    f"{script}-foreign", foreign.pid, foreign_quits.server_address[1])})
                # 2. Looks like ours but started after instance.json was written: PID reuse.
                reused, _ = self.spawn(SLEEPER, "-m", "projektsog")
                reused_quits = self.recorder()
                path = self.instance_file(f"{script}-reused", reused.pid,
                                          reused_quits.server_address[1])
                an_hour_ago = time.time() - 3600
                os.utime(path, (an_hour_ago, an_hour_ago))
                cases.append({"id": "reused", "instance_file": path})
                # 3. A helper process is never "the instance".
                helper, _ = self.spawn(SLEEPER, "-m", "projektsog.scanworker")
                helper_quits = self.recorder()
                cases.append({"id": "helper", "instance_file": self.instance_file(
                    f"{script}-helper", helper.pid, helper_quits.server_address[1])})
                # 4. Ours but hung (quit fails): asked first, then only that process is ended.
                hung, child_pid = self.spawn(PARENT_OF_SLEEPER, "-m", "projektsog",
                                             reads_line=True)
                child = _ProcessHandle(int(child_pid))
                self.addCleanup(child.close)
                hung_quits = self.recorder()
                cases.append({"id": "hung", "instance_file": self.instance_file(
                    f"{script}-hung", hung.pid, hung_quits.server_address[1])})
                # 5. Ours and healthy: POST /api/quit to the real server ends it (exit code 0).
                app_proc, port = self.spawn(
                    QUITTABLE_APP, REPO, os.path.join(self.tmp, f"{script}-config.json"),
                    web_dir, "-m", "projektsog", reads_line=True)
                cases.append({"id": "healthy", "instance_file": self.instance_file(
                    f"{script}-healthy", app_proc.pid, int(port))})

                result = self.run_harness(script, "stop", cases=cases,
                                          variables=self.stop_variables())

                for case in cases:                              # stopped, files gone
                    self.assertEqual(result[case["id"]]["stopped"], [True], case["id"])
                    self.assertIs(result[case["id"]]["file_left"], False, case["id"])
                self.assertIsNone(foreign.poll(), "an unrelated process was ended")
                self.assertIsNone(reused.poll(), "a process that reused the PID was ended")
                self.assertIsNone(helper.poll(), "a helper was taken for the instance")
                for recorder in (foreign_quits, reused_quits, helper_quits):
                    self.assertEqual(recorder.requests, [])      # not even asked to quit
                self.assertEqual(hung_quits.requests, [("POST", "/api/quit", "1")])
                self.assertIsNotNone(hung.wait(10))
                self.assertTrue(child.alive(), "a whole process tree was ended")
                self.assertEqual(app_proc.wait(10), 0, "the healthy app was not asked to quit")
                self.assertEqual({case["id"]: result[case["id"]]["kills"] for case in cases},
                                 {"foreign": [], "reused": [], "helper": [], "hung": [hung.pid],
                                  "healthy": []})

    def test_an_app_started_as_administrator_is_asked_to_quit_but_never_killed(self) -> None:
        # R2-INST-1: a normal PowerShell cannot read an elevated process's command line. It is
        # asked via POST /api/quit – only when that very PID listens on the port – and never
        # ended by force; if it stays, instance.json stays too and the script stops.
        web_dir = os.path.join(self.tmp, "web")
        os.makedirs(web_dir)
        for script in SCRIPTS:
            with self.subTest(script=script):
                cases = []
                # 1. It quits when asked: the script carries on (instance.json removed).
                app_proc, port = self.spawn(
                    QUITTABLE_APP, REPO, os.path.join(self.tmp, f"{script}-a.json"), web_dir,
                    reads_line=True)
                cases.append({"id": "quits", "hidden_pid": app_proc.pid, "instance_file":
                              self.instance_file(f"{script}-quits", app_proc.pid, int(port))})
                # 2. Hung: asked, stays – nothing is killed, the script must stop.
                quit_log = os.path.join(self.tmp, f"{script}-quit.log")
                stuck, stuck_port = self.spawn(
                    STUCK_APP, REPO, os.path.join(self.tmp, f"{script}-b.json"), web_dir,
                    quit_log, reads_line=True)
                cases.append({"id": "stays", "hidden_pid": stuck.pid, "wait_ms": 1500,
                              "instance_file": self.instance_file(f"{script}-stays", stuck.pid,
                                                                  int(stuck_port))})
                # 3. Another process listens on the port in instance.json: not even asked.
                sleeper, _ = self.spawn(SLEEPER)
                other = self.recorder()
                cases.append({"id": "other-listener", "hidden_pid": sleeper.pid,
                              "instance_file": self.instance_file(
                                  f"{script}-other", sleeper.pid, other.server_address[1])})

                result = self.run_harness(script, "stop", cases=cases,
                                          variables=self.stop_variables())

                quits, stays, other_case = (result[c["id"]] for c in cases)
                self.assertEqual((quits["stopped"], quits["file_left"], quits["kills"]),
                                 ([True], False, []))
                self.assertEqual(app_proc.wait(10), 0, "the elevated app was not asked to quit")
                for case in (stays, other_case):
                    self.assertEqual((case["stopped"], case["file_left"], case["kills"]),
                                     ([False], True, []))
                    self.assertIn("startet som administrator", case["said"])
                    self.assertIn("ikonet ved uret", case["said"])
                with open(quit_log, encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), "quit\n")          # asked once …
                self.assertIsNone(stuck.poll(), "an unverifiable process was ended")   # … kept
                self.assertEqual(other.requests, [])               # a foreign listener: never
                self.assertIsNone(sleeper.poll())

    def test_a_run_that_could_not_stop_projektsog_changes_nothing(self) -> None:
        # R2-INST-1: install/uninstall end right there (Stop-Projektsog said why) – install
        # must not start a second instance next to the old one, uninstall must not claim
        # success while Projektsøg keeps running.
        for script, first_change in (("install.ps1", "New-AppShortcut $pythonInfo.pythonw"),
                                     ("uninstall.ps1", "Remove-Item -LiteralPath $ShortcutFile")):
            with self.subTest(script=script):
                with open(SCRIPTS[script], encoding="utf-8-sig") as fh:
                    text = fh.read()
                guard = re.search(r"\nif \(-not \(Stop-Projektsog\)\) \{\s*exit 1\b", text)
                self.assertIsNotNone(guard, "the result of Stop-Projektsog is not checked")
                self.assertLess(guard.start(), text.index(first_change))
                self.assertEqual(len(re.findall(r"\bStop-Projektsog\b", text)), 2)  # def + call

    def test_sweep_and_instance_check_rules(self) -> None:
        profile = os.path.join(self.tmp, "Projektsog", "edge-profile")
        processes = [
            # pid, name, own session, command line, swept, may be the instance, may be an
            # instance started as administrator (command line hidden, R2-INST-1)
            (11, "pythonw.exe", True, '"C:\\Py\\pythonw.exe" "C:\\Github\\Search\\Projektsøg.pyw" --background', True, True, False),
            (12, "python.exe", True, '"C:\\Py\\python.exe" -m projektsog --debug', True, True, False),
            (13, "pythonw.exe", True, '"C:\\Py\\pythonw.exe" -m projektsog.scanworker', True, False, False),
            (14, "pythonw.exe", True, '"C:\\Py\\pythonw.exe" -m projektsog.hotkey --child', True, False, False),
            (15, "pythonw.exe", False, '"C:\\Py\\pythonw.exe" "C:\\Github\\Search\\Projektsøg.pyw"', False, False, False),
            (16, "python.exe", True, '"C:\\Py\\python.exe" server.py', False, False, False),
            (17, "python.exe", True, '"C:\\Py\\python.exe" -m projektsogx', False, False, False),
            (18, "msedge.exe", True, f'"msedge.exe" --app=http://127.0.0.1:47811/ --user-data-dir={profile}', True, False, False),
            (19, "msedge.exe", True, '"msedge.exe" --user-data-dir=C:\\Other\\profile', False, False, False),
            (20, "msedge.exe", False, f'"msedge.exe" --user-data-dir={profile}', False, False, False),
            (21, "Resolve.exe", True, '"Resolve.exe" -m projektsog', False, False, False),
            (22, "pythonw.exe", True, None, False, False, True),
            (23, "python.exe", True, "", False, False, True),
            (24, "pythonw.exe", False, None, False, False, False),
            (25, "msedge.exe", True, None, False, False, False),
            (26, "Resolve.exe", True, None, False, False, False),
        ]
        fakes = [{"pid": pid, "name": name, "own_session": own, "cmd": cmd}
                 for pid, name, own, cmd, *_ in processes]
        for script in SCRIPTS:
            with self.subTest(script=script):
                result = self.run_harness(
                    script, "fake-processes", fake_processes=fakes,
                    variables={"EdgeProfile": profile, "AppName": "Projektsøg",
                               "InstanceFile": os.path.join(self.tmp, f"{script}.json")})
                self.assertEqual(sorted(result["swept"]),
                                 [p[0] for p in processes if p[4]])
                self.assertEqual(sorted(result["verified"]),
                                 [p[0] for p in processes if p[5]])
                self.assertEqual(sorted(result["unreadable"]),
                                 [p[0] for p in processes if p[6]])

    def test_both_scripts_share_the_stop_code_and_never_kill_trees(self) -> None:
        shared = ("Read-InstanceFile", "Request-Quit", "Get-CurrentSessionId",
                  "Test-OwnCommandLine", "Get-InstanceProcess", "Test-ProcessAlive",
                  "Wait-ProcessExit", "Test-PortOwner", "Get-LeftoverProcesses",
                  "Stop-Projektsog")
        texts = {script: self.run_harness(script, "functions")["functions"]
                 for script in SCRIPTS}
        for name in shared:
            with self.subTest(function=name):
                self.assertIn(name, texts["install.ps1"])
                self.assertEqual(texts["install.ps1"][name], texts["uninstall.ps1"][name])
        for path in SCRIPTS.values():
            with open(path, encoding="utf-8-sig") as fh:
                text = fh.read().lower()
            self.assertNotIn("taskkill", text)

    # -- known issue 4 ------------------------------------------------------------------------
    def test_only_the_unicode_menu_script_is_installed_and_both_are_removed(self) -> None:
        repo = os.path.join(self.tmp, "repo")
        support = os.path.join(self.tmp, "Resolve")
        utility = os.path.join(support, "Support", "Fusion", "Scripts", "Utility")
        data = os.path.join(self.tmp, "data")
        manifest = os.path.join(data, "resolve-scripts.txt")
        for folder in (os.path.join(repo, "resolve_scripts"), utility, data):
            os.makedirs(folder)
        for name in (MENU_SCRIPT, ASCII_MENU_SCRIPT):
            with open(os.path.join(repo, "resolve_scripts", name), "w", encoding="utf-8") as fh:
                fh.write(f"# new {name}\n")
            with open(os.path.join(utility, name), "w", encoding="utf-8") as fh:
                fh.write("# installed by an earlier version\n")
        with open(os.path.join(utility, "AutoSubs V2.lua"), "w", encoding="utf-8") as fh:
            fh.write("-- the user's own script\n")
        with open(manifest, "w", encoding="utf-8-sig") as fh:     # as an earlier install wrote
            fh.write(f"{MENU_SCRIPT}\n{ASCII_MENU_SCRIPT}\n")
        variables = {"Repo": repo, "ResolveSupport": support, "ResolveUtility": utility,
                     "DataDir": data, "ScriptManifest": manifest}

        result = self.run_harness("install.ps1", "install-resolve", variables=variables,
                                  constants=["ResolveScript"])
        self.assertIs(result["installed"], True)
        self.assertEqual(sorted(os.listdir(utility)), ["AutoSubs V2.lua", MENU_SCRIPT])
        with open(os.path.join(utility, MENU_SCRIPT), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), f"# new {MENU_SCRIPT}\n")
        with open(manifest, encoding="utf-8-sig") as fh:
            self.assertEqual([line for line in fh.read().splitlines() if line], [MENU_SCRIPT])

        # The README's fallback: the user copies the ASCII-named script by hand.
        shutil.copy(os.path.join(repo, "resolve_scripts", ASCII_MENU_SCRIPT), utility)
        os.remove(os.path.join(repo, "resolve_scripts", ASCII_MENU_SCRIPT))
        self.run_harness("uninstall.ps1", "remove-resolve", variables=variables,
                         constants=["ResolveScripts"])
        self.assertEqual(os.listdir(utility), ["AutoSubs V2.lua"])
        self.assertFalse(os.path.exists(manifest))


if __name__ == "__main__":
    unittest.main()
