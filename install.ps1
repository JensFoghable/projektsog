#Requires -Version 5.1
<#
.SYNOPSIS
    Installerer Projektsøg for den aktuelle Windows-bruger.

.DESCRIPTION
    - opretter genvejen "Projektsøg" i Start-menuen (med ikon og AppUserModelID)
    - slår "Start med Windows" til (spring over med -NoAutostart)
    - kopierer DaVinci Resolve-scriptet til Workspace > Scripts > Utility
    - registrerer Claude-sessionernes Resolve-kø (koe.py installer), så "Byg nu" i Klippe virker
      og robotterne kan se, når en session bygger - koe.py findes i Davinci-mappen ved siden af
      Projektsøg-mappen eller i C:\Github\Davinci (eller angiv -Koe)
    - gemmer listen over computere, hvis -Hosts er angivet
    - starter Projektsøg i baggrunden
    Kræver ikke administratorrettigheder. Kør scriptet igen for at opdatere en installation.

.PARAMETER NoAutostart
    Projektsøg starter ikke automatisk, når du logger på Windows.

.PARAMETER Python
    Sti til python.exe (3.14 eller nyere), hvis den ikke findes automatisk.

.PARAMETER Koe
    Sti til Resolve-køens koe.py, hvis den ikke ligger i Davinci\resolve-koe ved siden af
    Projektsøg-mappen eller i C:\Github\Davinci\resolve-koe.

.PARAMETER Hosts
    Andre computere, hvis delte mapper Projektsøg skal søge i - navnene adskilt af komma uden
    mellemrum, f.eks. -Hosts STUDIO-PC,KLIPPER-PC. De føjes til listen over computere (samme
    liste som Indstillinger > Placeringer > Tilføj computer); computere, der allerede står på
    listen, bliver stående. Pc'en selv springes altid over, så den samme liste kan bruges på
    alle pc'erne. Uden -Hosts ændres listen ikke (efter en ny installation er den tom).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Hosts STUDIO-PC,KLIPPER-PC
#>
[CmdletBinding()]
param(
    [switch] $NoAutostart,
    [string] $Python,
    [string[]] $Hosts,
    [string] $Koe
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$AppName        = 'Projektsøg'
$AppUserModelId = 'Projektsog.App'
$Repo           = $PSScriptRoot
$Launcher       = Join-Path $Repo 'Projektsøg.pyw'
$IconFile       = Join-Path $Repo 'projektsog\assets\icon.ico'
$DataDir        = Join-Path $env:LOCALAPPDATA 'Projektsog'
$InstanceFile   = Join-Path $DataDir 'instance.json'
$EdgeProfile    = Join-Path $DataDir 'edge-profile'
$ScriptManifest = Join-Path $DataDir 'resolve-scripts.txt'
$ResolveScript  = 'Projektsøg - Åbn projektmappe.py'    # the one menu entry that is installed
$RunKey         = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$StartupApprovedKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
$ShortcutFile   = Join-Path ([Environment]::GetFolderPath('Programs')) 'Projektsøg.lnk'
$ResolveSupport = Join-Path $env:APPDATA 'Blackmagic Design\DaVinci Resolve'
$ResolveUtility = Join-Path $ResolveSupport 'Support\Fusion\Scripts\Utility'
# "-Hosts A,B" through "powershell -File" arrives as the one string "A,B"; the names are split
# (commas, semicolons, spaces) by $HostsScript.
$HostsGiven     = $PSBoundParameters.ContainsKey('Hosts')

# Asks a Python interpreter for its version and - when new enough - the exact autostart command
# the app itself uses (projektsog.app.RUN_COMMAND), or why the app cannot be loaded. The reply
# is ASCII-only JSON, so no console code page can mangle the "ø" in the paths. The script is
# sent on stdin, which avoids native-argument quoting problems.
$PythonProbe = @'
import json, sys
info = {"version": list(sys.version_info[:2]), "executable": sys.executable}
if sys.version_info >= (3, 14):
    sys.path.insert(0, sys.argv[1])
    try:
        from projektsog.app import RUN_COMMAND, pythonw_path
        info.update(pythonw=pythonw_path(), run_command=RUN_COMMAND)
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(info))
'@

# Adds computers to the app's own list (Indstillinger > Placeringer > Tilføj computer). Python
# does the work: the same name check as the settings page (Indexer._valid_host: upper case, name,
# FQDN or IPv4) and Config.update(), which validates and saves config.json atomically as UTF-8
# without BOM - PowerShell never writes config.json (Windows PowerShell 5.1 adds a BOM, and the
# app could not read the file). argv: repo, "check" | "save", the names as base64 UTF-8 text
# (no native-argument quoting). "check" changes nothing. The reply is ASCII-only JSON:
# {"hosts": [...]} (+ "added", "all" for "save") or {"error": "..."}.
$HostsScript = @'
import base64, json, re, sys
sys.path.insert(0, sys.argv[1])
reply = {}
try:
    from projektsog.config import Config
    from projektsog.indexer import Indexer
    text = base64.b64decode(sys.argv[3] if len(sys.argv) > 3 else "").decode("utf-8")
    hosts = []
    for name in re.split(r"[\s,;]+", text):
        if name:
            host = Indexer._valid_host(name)
            if host not in hosts:
                hosts.append(host)
    if not hosts:
        raise ValueError("Ingen computernavne angivet")
    reply["hosts"] = hosts
    if sys.argv[2] == "save":
        cfg = Config()
        current = [str(h) for h in cfg.get("hosts") or []]
        known = {h.strip().strip("\\").upper() for h in current}
        added = [h for h in hosts if h not in known]
        if added:
            cfg.update({"hosts": current + added})
        reply.update(added=added, all=cfg.get("hosts"))
except ValueError as exc:
    reply = {"error": str(exc)}
except Exception as exc:
    reply = {"error": f"{type(exc).__name__}: {exc}"}
print(json.dumps(reply))
'@

# Start-menu shortcut with System.AppUserModel.ID (not possible through WScript.Shell).
# C# 5 so that Windows PowerShell 5.1 can compile it.
$ShellLinkSource = @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace Projektsog.Install
{
    public static class ShellLink
    {
        [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
        private class CShellLink { }

        [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
         Guid("000214F9-0000-0000-C000-000000000046")]
        private interface IShellLinkW
        {
            void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder file, int maxPath,
                         IntPtr findData, uint flags);
            void GetIDList(out IntPtr idList);
            void SetIDList(IntPtr idList);
            void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder name,
                                int maxName);
            void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string name);
            void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder dir,
                                     int maxPath);
            void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string dir);
            void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder args,
                              int maxPath);
            void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
            void GetHotkey(out ushort hotkey);
            void SetHotkey(ushort hotkey);
            void GetShowCmd(out int showCmd);
            void SetShowCmd(int showCmd);
            void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder iconPath,
                                 int maxPath, out int iconIndex);
            void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string iconPath, int iconIndex);
            void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string relativePath,
                                 uint reserved);
            void Resolve(IntPtr hwnd, uint flags);
            void SetPath([MarshalAs(UnmanagedType.LPWStr)] string file);
        }

        [StructLayout(LayoutKind.Sequential, Pack = 4)]
        private struct PropertyKey
        {
            public Guid FormatId;
            public uint PropertyId;
        }

        // A PROPVARIANT holding a VT_LPWSTR, the only variant type used here.
        [StructLayout(LayoutKind.Explicit, Size = 24)]
        private struct PropVariant
        {
            [FieldOffset(0)] public ushort ValueType;
            [FieldOffset(8)] public IntPtr Pointer;
        }

        [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
         Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
        private interface IPropertyStore
        {
            void GetCount(out uint count);
            void GetAt(uint index, out PropertyKey key);
            void GetValue(ref PropertyKey key, out PropVariant value);
            void SetValue(ref PropertyKey key, ref PropVariant value);
            void Commit();
        }

        private const ushort VT_LPWSTR = 31;

        public static void Create(string path, string target, string arguments,
                                  string workingDirectory, string iconPath, string description,
                                  string appUserModelId)
        {
            IShellLinkW link = (IShellLinkW)new CShellLink();
            try
            {
                link.SetPath(target);
                link.SetArguments(arguments);
                link.SetWorkingDirectory(workingDirectory);
                link.SetDescription(description);
                if (!String.IsNullOrEmpty(iconPath))
                {
                    link.SetIconLocation(iconPath, 0);
                }

                // System.AppUserModel.ID = {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, 5
                PropertyKey key = new PropertyKey();
                key.FormatId = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
                key.PropertyId = 5;
                PropVariant value = new PropVariant();
                value.ValueType = VT_LPWSTR;
                value.Pointer = Marshal.StringToCoTaskMemUni(appUserModelId);
                try
                {
                    IPropertyStore store = (IPropertyStore)link;
                    store.SetValue(ref key, ref value);
                    store.Commit();
                }
                finally
                {
                    Marshal.FreeCoTaskMem(value.Pointer);
                }
                ((IPersistFile)link).Save(path, true);
            }
            finally
            {
                Marshal.ReleaseComObject(link);
            }
        }
    }
}
'@

function Write-Step([string] $Text) {
    Write-Host ''
    Write-Host $Text -ForegroundColor Cyan
}

function Stop-WithError([string] $Text) {
    Write-Host ''
    Write-Host $Text -ForegroundColor Red
    exit 1
}

function Get-PythonInfo([string] $Command) {
    # Native stderr must not become a terminating error under 'Stop' (Windows PowerShell 5.1).
    $ErrorActionPreference = 'Continue'
    try {
        $output = $PythonProbe | & $Command - $Repo 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $output) { return $null }
        $info = ($output | Select-Object -Last 1) | ConvertFrom-Json
        $fields = $info.PSObject.Properties.Name
        if ($fields -notcontains 'run_command' -and $fields -notcontains 'error') {
            return $null        # older than 3.14
        }
        return $info
    } catch {
        return $null
    }
}

function Invoke-HostsScript([string] $Executable, [string] $Mode, [string[]] $Names) {
    # $HostsScript's reply, or $null when Python did not answer.
    $ErrorActionPreference = 'Continue'
    try {
        $encoded = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes(($Names -join "`n")))
        $output = $HostsScript | & $Executable - $Repo $Mode $encoded 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $output) { return $null }
        return ($output | Select-Object -Last 1) | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($Python) { $candidates.Add($Python) }
    foreach ($name in 'py', 'python', 'python3') {
        if (Get-Command $name -CommandType Application -ErrorAction SilentlyContinue) {
            $candidates.Add($name)
        }
    }
    $patterns = @(
        (Join-Path $env:LOCALAPPDATA 'Python\pythoncore-3.*\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe'),
        (Join-Path $env:ProgramFiles 'Python3*\python.exe'))
    foreach ($pattern in $patterns) {
        Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            ForEach-Object { $candidates.Add($_.FullName) }
    }
    foreach ($candidate in $candidates) {
        $info = Get-PythonInfo $candidate
        if ($info) { return $info }
    }
    return $null
}

function Test-Edge {
    $folders = @(${env:ProgramFiles(x86)}, $env:ProgramFiles, $env:LOCALAPPDATA) | Where-Object { $_ }
    foreach ($folder in $folders) {
        if (Test-Path -LiteralPath (Join-Path $folder 'Microsoft\Edge\Application\msedge.exe')) {
            return $true
        }
    }
    return $false
}

function Read-InstanceFile {
    if (-not (Test-Path -LiteralPath $InstanceFile)) { return $null }
    try {
        $info = Get-Content -LiteralPath $InstanceFile -Raw -Encoding UTF8 | ConvertFrom-Json
        return [pscustomobject]@{ Pid = [int] $info.pid; Port = [int] $info.port }
    } catch {
        return $null
    }
}

function Request-Quit([int] $Port) {
    # POST /api/quit lets the app close its window, helpers and index cleanly.
    try {
        $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/api/quit")
        $request.Method = 'POST'
        $request.Proxy = $null
        $request.Timeout = 5000
        $request.ContentType = 'application/json'
        $request.Headers.Add('X-Projektsog', '1')
        $body = [System.Text.Encoding]::ASCII.GetBytes('{}')
        $request.ContentLength = $body.Length
        $stream = $request.GetRequestStream()
        $stream.Write($body, 0, $body.Length)
        $stream.Close()
        $request.GetResponse().Close()
        return $true
    } catch {
        return $false
    }
}

function Get-CurrentSessionId {
    return (Get-Process -Id $PID).SessionId
}

function Test-OwnCommandLine([string] $CommandLine, [switch] $MainProcess) {
    # Projektsøg itself ("Projektsøg.pyw", "-m projektsog") and - unless -MainProcess - its
    # helper processes ("-m projektsog.hotkey", "-m projektsog.scanworker").
    if (-not $CommandLine) { return $false }
    if ($CommandLine -like '*Projektsøg.pyw*') { return $true }
    if ($MainProcess) { return $CommandLine -match '-m\s+projektsog(\s|$)' }
    return $CommandLine -match '-m\s+projektsog(\.|\s|$)'
}

function Get-InstanceProcess($Instance, [switch] $Unreadable) {
    # instance.json outlives crashes, logoffs and Task Manager kills, and Windows reuses PIDs.
    # So its PID only counts when it verifiably is Projektsøg: python.exe/pythonw.exe running
    # Projektsøg.pyw or "-m projektsog" in this Windows session, started before instance.json
    # was written. Any other process with that PID is never touched.
    # -Unreadable: instead the same process whose command line Windows does not show - the app
    # runs as administrator and this PowerShell does not. It cannot be verified, only asked.
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $([int] $Instance.Pid)" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $process) { return $null }
    if (@('python.exe', 'pythonw.exe') -notcontains $process.Name) { return $null }
    if ($Unreadable) {
        if ($process.CommandLine) { return $null }
    } elseif (-not (Test-OwnCommandLine $process.CommandLine -MainProcess)) {
        return $null
    }
    if ($process.SessionId -ne (Get-CurrentSessionId)) { return $null }
    $file = Get-Item -LiteralPath $InstanceFile -ErrorAction SilentlyContinue
    if (-not $file -or -not $process.CreationDate -or
            $process.CreationDate.ToUniversalTime() -gt $file.LastWriteTimeUtc.AddSeconds(2)) {
        return $null
    }
    return $process
}

function Test-ProcessAlive($Process) {
    # Is the process verified by Get-InstanceProcess still running (same PID, same start time)?
    $now = Get-CimInstance Win32_Process -Filter "ProcessId = $([int] $Process.ProcessId)" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    return [bool] ($now -and $now.CreationDate -eq $Process.CreationDate)
}

function Wait-ProcessExit($Process, [int] $Milliseconds) {
    $deadline = (Get-Date).AddMilliseconds($Milliseconds)
    while ((Test-ProcessAlive $Process) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 200
    }
}

function Test-PortOwner([int] $Port, [int] $ProcessId) {
    # Is that process the one listening on 127.0.0.1:$Port, i.e. the server a request to that
    # port reaches? Windows shows who owns a socket even when it hides the command line.
    try {
        $listeners = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction Stop)
    } catch {
        return $false
    }
    return [bool] ($listeners | Where-Object { $_.OwningProcess -eq $ProcessId })
}

function Get-LeftoverProcesses {
    # Our Python processes (app, hotkey helper, scan worker) and the Edge window that uses the
    # app's private profile, in this Windows session - nothing else.
    $session = Get-CurrentSessionId
    $pythonFilter = "Name = 'pythonw.exe' OR Name = 'python.exe'"
    $python = Get-CimInstance Win32_Process -Filter $pythonFilter -ErrorAction SilentlyContinue |
        Where-Object { $_.SessionId -eq $session -and (Test-OwnCommandLine $_.CommandLine) }
    $edge = Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.SessionId -eq $session -and $_.CommandLine -and
                       $_.CommandLine.IndexOf($EdgeProfile, [StringComparison]::OrdinalIgnoreCase) -ge 0 }
    return @($python) + @($edge) | Where-Object { $_ }
}

function Stop-Projektsog([int] $QuitWaitMs = 8000) {
    # Returns $false - and leaves instance.json alone, so the running app keeps working - when
    # a running Projektsøg could not be stopped; the caller must not carry on then.
    $instance = Read-InstanceFile
    $process = $null
    $unreadable = $null
    if ($instance) {
        $process = Get-InstanceProcess $instance
        if (-not $process) { $unreadable = Get-InstanceProcess $instance -Unreadable }
    }
    if ($process) {
        Write-Host "Stopper den kørende $AppName ..."
        # First ask: POST /api/quit closes the window, the helper processes and the index cleanly.
        if (Request-Quit $instance.Port) {
            $running = Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
            if ($running) { [void] $running.WaitForExit($QuitWaitMs) }
        }
        # Hung: end exactly the verified process - never a process tree. Its helpers exit by
        # themselves without it, and the sweep below catches any that do not.
        if (Test-ProcessAlive $process) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
    } elseif ($unreadable) {
        # Started as administrator: it cannot be verified from here, so it is never ended by
        # force. POST /api/quit works across that boundary - sent only when that very process
        # is the one listening on the port.
        Write-Host "Stopper den kørende $AppName ..."
        if ((Test-PortOwner $instance.Port $unreadable.ProcessId) -and (Request-Quit $instance.Port)) {
            Wait-ProcessExit $unreadable $QuitWaitMs
        }
        if (Test-ProcessAlive $unreadable) {
            Write-Host ''
            Write-Host ("Den kørende $AppName er startet som administrator og kunne ikke stoppes herfra. " +
                        "Afslut den fra ikonet ved uret (højreklik ▸ Afslut) – eller log af og på igen – " +
                        'og kør så scriptet igen.') -ForegroundColor Red
            return $false
        }
    }
    $deadline = (Get-Date).AddSeconds(5)
    while (@(Get-LeftoverProcesses).Count -gt 0 -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    foreach ($leftover in @(Get-LeftoverProcesses)) {
        Stop-Process -Id $leftover.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $InstanceFile) {
        Remove-Item -LiteralPath $InstanceFile -Force -ErrorAction SilentlyContinue
    }
    return $true
}

function New-AppShortcut([string] $Pythonw) {
    if (-not ('Projektsog.Install.ShellLink' -as [type])) {
        Add-Type -TypeDefinition $ShellLinkSource -Language CSharp
    }
    $icon = ''
    if (Test-Path -LiteralPath $IconFile) {
        $icon = $IconFile
    } else {
        Write-Warning "Ikonet $IconFile mangler - genvejen får Pythons ikon."
    }
    $shellLink = 'Projektsog.Install.ShellLink' -as [type]
    $shellLink::Create($ShortcutFile, $Pythonw, ('"{0}"' -f $Launcher), $Repo, $icon,
                       'Find projektmapper overalt med Shift+Mellemrum', $AppUserModelId)
}

function Remove-RunValues([switch] $KeepOwn) {
    # The app's own value is named exactly "Projektsøg"; older or hand-made entries that start
    # the app another way would start it twice.
    $key = Get-Item -LiteralPath $RunKey -ErrorAction SilentlyContinue
    if (-not $key) { return }
    foreach ($name in $key.GetValueNames()) {
        if (-not $name) { continue }
        $data = [string] $key.GetValue($name)
        $ours = $name -eq $AppName -or $data -like '*Projektsøg.pyw*' -or
                $data -match '-m\s+projektsog(\s|$)'
        if ($ours -and -not ($KeepOwn -and $name -eq $AppName)) {
            Remove-ItemProperty -LiteralPath $RunKey -Name $name -ErrorAction SilentlyContinue
        }
    }
}

function Install-ResolveScripts {
    # Only "Projektsøg - Åbn projektmappe.py" - one menu entry. resolve_scripts\ also holds an
    # ASCII-named copy for Resolve versions that do not list the name (README, Fejlfinding).
    $source = Join-Path (Join-Path $Repo 'resolve_scripts') $ResolveScript
    if (-not (Test-Path -LiteralPath $source)) {
        Write-Warning "DaVinci Resolve-scriptet mangler: $source"
        return $false
    }
    if (-not (Test-Path -LiteralPath $ResolveSupport)) {
        Write-Host 'DaVinci Resolve er ikke installeret for denne bruger - springer over.'
        return $false
    }
    New-Item -ItemType Directory -Path $ResolveUtility -Force | Out-Null
    # Scripts an earlier version installed under other names (only files we put there).
    $previous = @()
    if (Test-Path -LiteralPath $ScriptManifest) {
        $previous = @(Get-Content -LiteralPath $ScriptManifest -Encoding UTF8 | Where-Object { $_ })
    }
    foreach ($name in $previous) {
        if ($name -ne $ResolveScript) {
            Remove-Item -LiteralPath (Join-Path $ResolveUtility $name) -Force -ErrorAction SilentlyContinue
        }
    }
    Copy-Item -LiteralPath $source -Destination (Join-Path $ResolveUtility $ResolveScript) -Force
    Write-Host "  $ResolveScript"
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
    Set-Content -LiteralPath $ScriptManifest -Value @($ResolveScript) -Encoding UTF8
    return $true
}

function Find-ResolveQueue {
    # The Claude sessions' Resolve queue lives in the Davinci folder (not in this repo): beside the
    # Projektsøg folder, or in C:\Github\Davinci - or wherever -Koe says.
    if ($Koe) {
        if (Test-Path -LiteralPath $Koe -PathType Leaf) { return (Resolve-Path -LiteralPath $Koe).Path }
        Write-Warning "koe.py findes ikke: $Koe"
        return $null
    }
    $candidates = @(
        (Join-Path (Split-Path -Parent $Repo) 'Davinci\resolve-koe\koe.py'),
        'C:\Github\Davinci\resolve-koe\koe.py',
        (Join-Path $env:USERPROFILE 'Github\Davinci\resolve-koe\koe.py'),
        (Join-Path $env:USERPROFILE 'Documents\GitHub\Davinci\resolve-koe\koe.py'))
    foreach ($path in $candidates) {
        if (Test-Path -LiteralPath $path -PathType Leaf) { return $path }
    }
    return $null
}

function Install-ResolveQueue([string] $PythonExe) {
    # "koe.py installer" registers the resolvekoe: links (HKCU, no admin): the "Byg nu" buttons
    # open them, and Projektsøg finds the queue's state through them (SPEC §21.2).
    $queue = Find-ResolveQueue
    if (-not $queue) {
        Write-Host ('  Resolve-køen (Davinci\resolve-koe\koe.py) blev ikke fundet - springer over. ' +
                    'Angiv den med -Koe "C:\sti\til\koe.py".')
        return $false
    }
    $output = @(& $PythonExe $queue installer 2>&1 | ForEach-Object { "$_" })
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Resolve-køen kunne ikke registreres: $(($output -join ' ').Trim())"
        return $false
    }
    Write-Host "  $queue"
    return $true
}

function Start-Projektsog([string] $Pythonw) {
    $start = @{
        FilePath         = $Pythonw
        ArgumentList     = @(('"{0}"' -f $Launcher), '--background')
        WorkingDirectory = $Repo
    }
    Start-Process @start
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
        $instance = Read-InstanceFile
        if ($instance -and (Get-Process -Id $instance.Pid -ErrorAction SilentlyContinue)) {
            return $true
        }
    }
    return $false
}

# --------------------------------------------------------------------------------------------

Write-Host "Installerer $AppName fra $Repo"
$principal = New-Object Security.Principal.WindowsPrincipal ([Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning ('PowerShell kører som administrator, så Projektsøg startes også som administrator ' +
                   'indtil næste login. Kør hellere install.ps1 i et almindeligt PowerShell-vindue.')
}
if (-not (Test-Path -LiteralPath $Launcher)) {
    Stop-WithError "Filen $Launcher mangler. Kør install.ps1 fra Projektsøg-mappen."
}

Write-Step 'Finder Python 3.14 ...'
$pythonInfo = Find-Python
if (-not $pythonInfo) {
    Stop-WithError ('Python 3.14 eller nyere blev ikke fundet. Installér Python fra ' +
                    'https://www.python.org/downloads/ og kør install.ps1 igen, eller angiv ' +
                    'stien: powershell -ExecutionPolicy Bypass -File .\install.ps1 ' +
                    '-Python "C:\sti\til\python.exe"')
}
Write-Host "  Python $($pythonInfo.version -join '.'): $($pythonInfo.executable)"
if ($pythonInfo.PSObject.Properties.Name -contains 'error') {
    Stop-WithError "Projektsøg kunne ikke indlæses med denne Python: $($pythonInfo.error)"
}
if (-not (Test-Edge)) {
    Write-Warning 'Microsoft Edge blev ikke fundet. Projektsøg viser sit vindue med Edge - installér det fra https://www.microsoft.com/edge'
}
if ($HostsGiven) {
    # Checked before anything is changed: a mistyped name stops the script right here.
    $hostsCheck = Invoke-HostsScript $pythonInfo.executable 'check' $Hosts
    if (-not $hostsCheck) {
        Stop-WithError 'Computernavnene i -Hosts kunne ikke kontrolleres med Python.'
    }
    if ($hostsCheck.PSObject.Properties.Name -contains 'error') {
        Stop-WithError ("-Hosts: $($hostsCheck.error). Skriv computernes navne adskilt af " +
                        'komma, f.eks. -Hosts STUDIO-PC,KLIPPER-PC')
    }
}

Write-Step "Stopper en eventuel kørende $AppName ..."
if (-not (Stop-Projektsog)) {
    exit 1      # (it said why) nothing is changed while the old version still runs
}

Write-Step 'Opretter genvejen i Start-menuen ...'
New-AppShortcut $pythonInfo.pythonw
Write-Host "  $ShortcutFile"

Write-Step 'Start med Windows ...'
if ($NoAutostart) {
    Remove-RunValues
    Write-Host '  Fra (-NoAutostart)'
} else {
    Remove-RunValues -KeepOwn
    $runValue = @{
        Path         = $RunKey
        Name         = $AppName
        Value        = $pythonInfo.run_command
        PropertyType = 'String'
        Force        = $true
    }
    New-ItemProperty @runValue | Out-Null
    Write-Host '  Til'
}
# Task Manager's own on/off flag for the entry: an old "Deaktiveret" would keep a fresh "Til"
# from running (the app's own switch forgets it the same way).
Remove-ItemProperty -LiteralPath $StartupApprovedKey -Name $AppName -ErrorAction SilentlyContinue

Write-Step 'Kopierer DaVinci Resolve-script ...'
$resolveScriptsInstalled = Install-ResolveScripts

Write-Step 'Registrerer Resolve-køen (Byg nu og robotterne) ...'
$queueInstalled = Install-ResolveQueue $pythonInfo.executable

if ($HostsGiven) {
    # Only now: the stopped app can no longer overwrite config.json with its own copy.
    Write-Step 'Gemmer listen over computere ...'
    $hostsSaved = Invoke-HostsScript $pythonInfo.executable 'save' $Hosts
    if ($hostsSaved -and $hostsSaved.PSObject.Properties.Name -notcontains 'error') {
        if (@($hostsSaved.added).Count -gt 0) {
            Write-Host "  Tilføjet: $(@($hostsSaved.added) -join ', ')"
        } else {
            Write-Host '  (stod allerede på listen)'
        }
        Write-Host "  Computere: $(@($hostsSaved.all) -join ', ')"
    } else {
        $why = 'Python svarede ikke'
        if ($hostsSaved) { $why = $hostsSaved.error }
        Write-Warning ("Listen over computere kunne ikke gemmes ($why). Tilføj computerne under " +
                       'Indstillinger ▸ Placeringer ▸ Tilføj computer.')
    }
}

Write-Step "Starter $AppName ..."
if (Start-Projektsog $pythonInfo.pythonw) {
    Write-Host ''
    Write-Host 'Projektsøg kører – tryk Shift+Mellemrum hvor som helst' -ForegroundColor Green
    if ($resolveScriptsInstalled) {
        # Double quotes: PowerShell would treat ‘ ’ as single-quote delimiters.
        Write-Host "Genstart DaVinci Resolve for at se ‘Projektsøg’ under Workspace ▸ Scripts ▸ Utility"
    }
} else {
    Write-Warning ("$AppName ser ikke ud til at være startet. Se logfilerne i " +
                   (Join-Path $DataDir 'logs'))
    exit 1
}
