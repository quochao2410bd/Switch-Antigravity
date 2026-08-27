<#
.SYNOPSIS
    Safe read-only probe for AGM (Antigravity Manager) installation and environment.
.DESCRIPTION
    Checks AGM binary availability, version, data directories, master key presence,
    SQLite database state, and Windows Credential Manager target.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'

Write-Output "=== AGM Environment Detection ==="

# 1. Check AGM binary on PATH and candidate locations
$agmCmd = Get-Command agm -ErrorAction SilentlyContinue
$agmPath = ""
if ($agmCmd) {
    $agmPath = $agmCmd.Source
} else {
    $candidates = @(
        "$HOME\.local\bin\agm.exe",
        "$HOME\bin\agm.exe",
        "$HOME\go\bin\agm.exe",
        "$HOME\.cargo\bin\agm.exe",
        "$env:LOCALAPPDATA\Programs\agm\agm.exe",
        "C:\Program Files\agm\agm.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) {
            $agmPath = $c
            break
        }
    }
}

if ($agmPath) {
    Write-Output "[+] AGM Binary Found: $agmPath"
    try {
        $versionOutput = & $agmPath --help 2>&1
        Write-Output "[+] AGM CLI Invocation: OK"
    } catch {
        Write-Output "[-] AGM CLI Invocation Failed: $_"
    }
} else {
    Write-Output "[-] AGM Binary: NOT FOUND on PATH or common search locations"
}

# 2. Check Data Directories
$agentDir = if ($env:AGM_DATA_DIR) { $env:AGM_DATA_DIR } elseif ($env:ANTIGRAVITY_AGENT_DIR) { $env:ANTIGRAVITY_AGENT_DIR } else { "$HOME\.antigravity-agent" }
Write-Output "[*] Data Directory Target: $agentDir"
if (Test-Path $agentDir) {
    Write-Output "    Directory exists: YES"
    $mkPath = Join-Path $agentDir ".mk"
    $dbPath = Join-Path $agentDir "cloud_accounts.db"
    $aliasesPath = Join-Path $agentDir "aliases.json"

    if (Test-Path $mkPath) {
        $mkItem = Get-Item $mkPath
        Write-Output "    Master Key (.mk): PRESENT (Size: $($mkItem.Length) bytes)"
    } else {
        Write-Output "    Master Key (.mk): MISSING"
    }

    if (Test-Path $dbPath) {
        $dbItem = Get-Item $dbPath
        Write-Output "    Database (cloud_accounts.db): PRESENT (Size: $($dbItem.Length) bytes)"
    } else {
        Write-Output "    Database (cloud_accounts.db): MISSING"
    }

    if (Test-Path $aliasesPath) {
        Write-Output "    Aliases (aliases.json): PRESENT"
    } else {
        Write-Output "    Aliases (aliases.json): NONE"
    }
} else {
    Write-Output "    Directory exists: NO (Store not yet initialized)"
}

# 3. Check Windows Credential Manager target for Antigravity
Write-Output "[*] Checking Windows Credential Manager for 'gemini:antigravity'..."
$cmdkeyOut = cmdkey /list | Out-String
if ($cmdkeyOut -match 'target=gemini:antigravity') {
    Write-Output "[+] Credential Manager Target 'gemini:antigravity': PRESENT"
} else {
    Write-Output "[-] Credential Manager Target 'gemini:antigravity': NOT FOUND"
}

# 4. Check Antigravity Desktop Installation
Write-Output "[*] Checking Antigravity Desktop Installation..."
$desktopExe = "$env:LOCALAPPDATA\Programs\antigravity\Antigravity.exe"
$desktopRoaming = "$env:APPDATA\Antigravity"

if (Test-Path $desktopExe) {
    Write-Output "[+] Antigravity Desktop Executable: $desktopExe"
} else {
    Write-Output "[-] Antigravity Desktop Executable: NOT FOUND at default path"
}

if (Test-Path $desktopRoaming) {
    Write-Output "[+] Antigravity User Data: $desktopRoaming"
} else {
    Write-Output "[-] Antigravity User Data: NOT FOUND"
}

Write-Output "=== Detection Complete ==="
