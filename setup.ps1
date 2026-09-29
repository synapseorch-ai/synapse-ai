# Install or upgrade Synapse (Windows).
#
#   irm https://github.com/synapseorch-ai/synapse-ai/releases/latest/download/setup.ps1 | iex
#
# This only fetches two things quietly - uv, and the installer that ships with
# the same release - and hands over to it. The installer draws the steps, asks
# its questions and ends with the link; everything else goes to
# %USERPROFILE%\.synapse\logs\install.log.
#
# For testing: $env:SYNAPSE_INSTALLER = a local synapse-install.pyz,
# $env:SYNAPSE_PACKAGE = a local wheel, $env:SYNAPSE_HOME = another home.
#
# No `exit` anywhere: under `irm | iex` it would close the user's window.

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
            Invoke-WebRequest -UseBasicParsing -Uri "https://astral.sh/uv/$uvVersion/install.ps1" -OutFile $installer
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
            Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile "$pyz.part"
            $expected = ((Invoke-WebRequest -UseBasicParsing -Uri "$url.sha256").Content -split '\s+')[0]
            $actual = (Get-FileHash -Algorithm SHA256 -Path "$pyz.part").Hash
            if ($actual -ne $expected.ToUpper()) {
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

    # Hand over. The installer reads answers from the console itself.
    $ErrorActionPreference = 'Continue'
    & $uv run --no-project --quiet --python 3.12 python $pyz --uv $uv @Arguments
    return $LASTEXITCODE
}

$synapseExit = Install-Synapse -Arguments $args
