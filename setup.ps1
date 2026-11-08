# Install or upgrade Synapse (Windows).
#
#   irm https://github.com/synapseorch-ai/synapse-ai/releases/latest/download/setup.ps1 | iex
#
# This only fetches two things quietly - uv, and the installer that ships with
# the same release - and hands over to it. The installer draws the steps, asks
# its questions and ends with the link; everything else goes to
# %USERPROFILE%\.synapse\logs\install.log.
#
# `irm | iex` passes no arguments, so every option is an environment variable
# set before that line: $env:SYNAPSE_YES='1' (ask nothing), SYNAPSE_NO_START,
# SYNAPSE_NO_BROWSER, SYNAPSE_INSTALL_VERSION='2.0.0', SYNAPSE_WITH='knowledge'.
#
# For testing: $env:SYNAPSE_INSTALLER = a local synapse-install.pyz,
# $env:SYNAPSE_PACKAGE = a local wheel, $env:SYNAPSE_HOME = another home.
#
# No `exit` anywhere: under `irm | iex` it would close the user's window.
#
# Every download has -TimeoutSec 30 (PowerShell 5.1 and 7): Windows gives up on an
# address that drops the connection after about 21 s and .NET tries the next one,
# and the cap keeps a stalled request from holding the install (F1445). The
# installer is about 66 KB.

# The installer's published sha256, as uppercase hex. Read from a file, not from
# Invoke-WebRequest's .Content: GitHub serves release assets as
# application/octet-stream, which makes .Content a byte array, and splitting that
# gave "51" (F833).
function Get-PublishedSha256 {
    param([string]$Uri, [string]$Path)
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri $Uri -OutFile $Path
    $text = [System.IO.File]::ReadAllText($Path)
    Remove-Item $Path
    return (($text.Trim() -split '\s+')[0]).ToUpper()
}

function Install-Synapse {
    param([string[]]$Arguments)

    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    $release = 'https://github.com/synapseorch-ai/synapse-ai/releases/latest/download'
    $url = if ($env:SYNAPSE_INSTALLER_URL) { $env:SYNAPSE_INSTALLER_URL } else { "$release/synapse-install.pyz" }
    $uvVersion = '0.11.8'
    $synapseHome = if ($env:SYNAPSE_HOME) { $env:SYNAPSE_HOME } else { Join-Path $env:USERPROFILE '.synapse' }
    $runtime = Join-Path $synapseHome 'runtime'
    $logs = Join-Path $synapseHome 'logs'
    $log = Join-Path $logs 'install.log'
    New-Item -ItemType Directory -Force -Path $runtime, $logs | Out-Null
    Add-Content -Path $log -Value "`n===== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') setup.ps1 ====="

    # One line while there is nothing to draw yet; the installer's first frame clears it.
    Write-Host -NoNewline "`n  Getting the Synapse installer..."

    try {
        # uv: one already on this computer, or a private copy that edits nothing on PATH.
        $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
        $private = Join-Path $runtime 'uv\uv.exe'
        if (-not $uv -and (Test-Path $private)) { $uv = $private }
        if (-not $uv) {
            $installer = Join-Path $runtime 'uv-install.ps1'
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri "https://astral.sh/uv/$uvVersion/install.ps1" -OutFile $installer
            $env:UV_UNMANAGED_INSTALL = Join-Path $runtime 'uv'
            # Native programs write progress to stderr; under 'Stop', Windows
            # PowerShell 5.1 turns that into an error. Their exit code decides.
            $ErrorActionPreference = 'Continue'
            & powershell -NoProfile -ExecutionPolicy Bypass -File $installer *>> $log
            $ErrorActionPreference = 'Stop'
            Remove-Item Env:\UV_UNMANAGED_INSTALL
            Remove-Item $installer -ErrorAction SilentlyContinue
            if (-not (Test-Path $private)) { throw "Could not install uv (details in $log)." }
            $uv = $private
        }

        # The installer: this release's, checked against its published checksum.
        $pyz = $env:SYNAPSE_INSTALLER
        if (-not $pyz) {
            $pyz = Join-Path $runtime 'synapse-install.pyz'
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri $url -OutFile "$pyz.part"
            $expected = Get-PublishedSha256 -Uri "$url.sha256" -Path "$pyz.sha256"
            $actual = (Get-FileHash -Algorithm SHA256 -Path "$pyz.part").Hash
            if ($actual -ne $expected) {
                Remove-Item "$pyz.part"
                throw 'The installer did not match its published checksum. Nothing was installed.'
            }
            Move-Item -Force "$pyz.part" $pyz
        }

        $ErrorActionPreference = 'Continue'
        & $uv python install 3.12 --quiet *>> $log
        $ErrorActionPreference = 'Stop'
        if ($LASTEXITCODE -ne 0) { throw "uv could not get Python (details in $log)." }
    }
    catch {
        Write-Host "`r                                        "
        Write-Host "  $($_.Exception.Message)"
        Write-Host ''
        return 1
    }

    # Hand over. Start-Process, not `&`: inside a function whose result is
    # assigned, `&` would capture the installer's output instead of showing
    # it, and the installer would see no console to draw on or read keys from.
    $argv = @('run', '--no-project', '--quiet', '--python', '3.12', 'python', $pyz, '--uv', $uv)
    if ($Arguments) { $argv += $Arguments }
    $quoted = $argv | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }
    $process = Start-Process -FilePath $uv -ArgumentList $quoted -NoNewWindow -Wait -PassThru
    return $process.ExitCode
}

$synapseExit = Install-Synapse -Arguments $args
