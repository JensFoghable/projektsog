#Requires -Version 5.1
<#
.SYNOPSIS
    Afinstallerer Projektsøg for den aktuelle Windows-bruger.

.DESCRIPTION
    Stopper Projektsøg og fjerner genvejen i Start-menuen, "Start med Windows" og
    DaVinci Resolve-scriptet. Indeks, indstillinger og logfiler bevares, medmindre
    -RemoveData angives. Selve programmappen slettes ikke.

.PARAMETER RemoveData
    Slet også indeks, indstillinger og logfiler (%LOCALAPPDATA%\Projektsog).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
#>
[CmdletBinding()]
param(
    [switch] $RemoveData
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

$AppName        = 'Projektsøg'
$Repo           = $PSScriptRoot
$DataDir        = Join-Path $env:LOCALAPPDATA 'Projektsog'
$InstanceFile   = Join-Path $DataDir 'instance.json'
$EdgeProfile    = Join-Path $DataDir 'edge-profile'
$ScriptManifest = Join-Path $DataDir 'resolve-scripts.txt'
# Every name a version of install.ps1 has used for the Resolve menu script (also the ASCII
# copy the README's troubleshooting section tells the user to put there by hand).
$ResolveScripts = @('Projektsøg - Åbn projektmappe.py', 'Projektsoeg - Aabn projektmappe.py')
$RunKey         = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$StartupApprovedKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
$ShortcutFile   = Join-Path ([Environment]::GetFolderPath('Programs')) 'Projektsøg.lnk'
$ResolveUtility = Join-Path $env:APPDATA 'Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility'

function Write-Step([string] $Text) {
    Write-Host ''
    Write-Host $Text -ForegroundColor Cyan
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

function Remove-RunValues {
    # The app's own value "Projektsøg" and any other entry that starts the app.
    $key = Get-Item -LiteralPath $RunKey -ErrorAction SilentlyContinue
    if (-not $key) { return }
    foreach ($name in $key.GetValueNames()) {
        if (-not $name) { continue }
        $data = [string] $key.GetValue($name)
        if ($name -eq $AppName -or $data -like '*Projektsøg.pyw*' -or
                $data -match '-m\s+projektsog(\s|$)') {
            Remove-ItemProperty -LiteralPath $RunKey -Name $name -ErrorAction SilentlyContinue
            Write-Host "  $name"
        }
    }
    # Task Manager's on/off flag for the entry.
    Remove-ItemProperty -LiteralPath $StartupApprovedKey -Name $AppName -ErrorAction SilentlyContinue
}

function Remove-ResolveScripts {
    # Only files Projektsøg put there: the names install.ps1 recorded, both names of the menu
    # script and the current scripts.
    $names = @($ResolveScripts)
    if (Test-Path -LiteralPath $ScriptManifest) {
        $names += @(Get-Content -LiteralPath $ScriptManifest -Encoding UTF8 | Where-Object { $_ })
    }
    $sourceDir = Join-Path $Repo 'resolve_scripts'
    $names += @(Get-ChildItem -LiteralPath $sourceDir -Filter '*.py' -File -ErrorAction SilentlyContinue |
                ForEach-Object { $_.Name })
    $removed = 0
    foreach ($name in @($names | Sort-Object -Unique)) {
        $file = Join-Path $ResolveUtility $name
        if (Test-Path -LiteralPath $file) {
            Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $file) {
                Write-Warning "Kunne ikke fjerne $file"
            } else {
                Write-Host "  $name"
                $removed++
            }
        }
    }
    if ($removed -eq 0) { Write-Host '  (intet at fjerne)' }
    if (Test-Path -LiteralPath $ScriptManifest) {
        Remove-Item -LiteralPath $ScriptManifest -Force
    }
}

# --------------------------------------------------------------------------------------------

Write-Host "Afinstallerer $AppName"

Write-Step "Stopper $AppName ..."
if (-not (Stop-Projektsog)) {
    exit 1      # (it said why) nothing is removed while Projektsøg still runs
}

Write-Step 'Fjerner genvejen i Start-menuen ...'
if (Test-Path -LiteralPath $ShortcutFile) {
    Remove-Item -LiteralPath $ShortcutFile -Force
    Write-Host "  $ShortcutFile"
} else {
    Write-Host '  (ingen genvej)'
}

Write-Step 'Fjerner Start med Windows ...'
Remove-RunValues

Write-Step 'Fjerner DaVinci Resolve-script ...'
Remove-ResolveScripts

if ($RemoveData) {
    Write-Step 'Sletter indeks, indstillinger og logfiler ...'
    if (Test-Path -LiteralPath $DataDir) {
        try {
            Remove-Item -LiteralPath $DataDir -Recurse -Force
            Write-Host "  $DataDir"
        } catch {
            Write-Warning "Nogle filer i $DataDir kunne ikke slettes: $($_.Exception.Message)"
        }
    }
} else {
    Write-Host ''
    Write-Host "Indeks og indstillinger er bevaret i $DataDir (slet dem med -RemoveData)."
}

Write-Host ''
Write-Host "$AppName er afinstalleret. Mappen $Repo kan nu slettes." -ForegroundColor Green
