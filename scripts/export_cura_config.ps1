<#
.SYNOPSIS
Exports this Windows computer's UltiMaker Cura configuration (printers, profiles, materials, user
plugins and post-processing scripts) into the layout Cura uses on Linux.

.DESCRIPTION
Same as scripts/export_cura_config.sh, for Windows without Git Bash. It creates:

  <Dest>\config\<series>   ->  ~/.config/cura/<series>        (cura.cfg, plugins.json)
  <Dest>\data\<series>     ->  ~/.local/share/cura/<series>   (everything else)

which docker-compose.yml mounts, or which can be copied to those folders of a Linux Cura.
Nothing is modified in the source. Works with Windows PowerShell 5.1 and PowerShell 7.

.PARAMETER Series
Cura series (first two numbers of the version, e.g. 5.13). Default: the newest one found.

.PARAMETER Source
Cura's configuration folder, when it is not %APPDATA%\cura\<series>.

.PARAMETER Dest
Destination folder. Default: .\cura-data

.PARAMETER NewToken
Do not export Remote Web Control's API token: the server generates a new one.

.PARAMETER Zip
Also pack the result into <Dest>.zip, to send it to another computer.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\export_cura_config.ps1 -NewToken -Zip
#>
[CmdletBinding()]
param(
    [string]$Series = "",
    [string]$Source = "",
    [string]$Dest = "cura-data",
    [switch]$NewToken,
    [switch]$Zip
)

$ErrorActionPreference = "Stop"

$base = if ($env:APPDATA) { Join-Path $env:APPDATA "cura" } else { "" }  # Windows only; elsewhere use -Source.
if (-not $Series) {
    if ($Source) {
        $Series = Split-Path -Leaf $Source
    } else {
        # Newest "X.Y" folder, compared as versions (5.9 < 5.13).
        $Series = Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^\d+\.\d+$' } |
            Sort-Object { [version]$_.Name } | Select-Object -Last 1 -ExpandProperty Name
    }
}
if ($Series -notmatch '^\d+\.\d+$') {
    throw "Could not determine the Cura series (e.g. 5.13). Use -Series or -Source."
}
if (-not $Source) { $Source = Join-Path $base $Series }

if (-not (Test-Path -LiteralPath (Join-Path $Source "cura.cfg"))) {
    throw "No Cura $Series configuration found: '$Source\cura.cfg' does not exist. Open that Cura version once, or use -Series / -Source."
}
if (-not (Test-Path -LiteralPath (Join-Path $Source "machine_instances"))) {
    Write-Warning "'$Source' has no printers (machine_instances). Exporting anyway."
}

$Dest = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Dest))
$configDest = Join-Path (Join-Path $Dest "config") $Series
$dataDest = Join-Path (Join-Path $Dest "data") $Series
if ((Test-Path -LiteralPath $configDest) -or (Test-Path -LiteralPath $dataDest)) {
    throw "'$Dest' already has a Cura $Series configuration. Delete it first to export again (it may contain the server's jobs and token)."
}
New-Item -ItemType Directory -Force -Path $configDest, $dataDest | Out-Null

# Preferences -> config
foreach ($name in "cura.cfg", "plugins.json") {
    $file = Join-Path $Source $name
    if (Test-Path -LiteralPath $file) { Copy-Item -LiteralPath $file -Destination $configDest }
}
if ($NewToken) {
    # Drop "token = ..." from the [remotewebcontrol] section: the server generates a new token.
    $cfg = Join-Path $configDest "cura.cfg"
    $section = ""
    $lines = foreach ($line in [System.IO.File]::ReadAllLines($cfg)) {
        if ($line -match '^\[') { $section = $line.Trim() }
        if ($section -eq "[remotewebcontrol]" -and $line -match '^\s*token\s*=') { continue }
        $line
    }
    # UTF-8 without BOM: Cura's configparser would not read a BOM.
    [System.IO.File]::WriteAllLines($cfg, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
}

# Everything else -> data. Left out: logs, backups, caches, preferences (already copied) and
# RemoteWebControl itself (the image provides the plugin; its jobs and token file are per machine).
# -LiteralPath everywhere: names like "Ender-3+Pro+%232.global.cfg" or with [ ] are copied as they are.
$root = (Get-Item -LiteralPath $Source).FullName.TrimEnd('\', '/')
foreach ($file in Get-ChildItem -LiteralPath $root -Recurse -File -Force) {
    $relative = $file.FullName.Substring($root.Length + 1)
    $parts = $relative -split '[\\/]'
    $top = $parts[0]
    $skip = ($parts.Count -eq 1 -and ($top -in "cura.cfg", "plugins.json" -or $top -like "cura.log*" -or
                                      $top -like "cura *.cfg" -or $top -like "*.bak")) -or
            $top -in "cache", "RemoteWebControl" -or
            ($parts.Count -gt 1 -and $top -eq "plugins" -and $parts[1] -eq "RemoteWebControl") -or
            $parts -contains "__pycache__"
    if ($skip) { continue }
    $target = Join-Path $dataDest $relative
    $folder = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Force -Path $folder | Out-Null }
    Copy-Item -LiteralPath $file.FullName -Destination $target
}

$printers = @(Get-ChildItem -LiteralPath (Join-Path $dataDest "machine_instances") -File -ErrorAction SilentlyContinue).Count
$plugins = (Get-ChildItem -LiteralPath (Join-Path $dataDest "plugins") -Directory -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty Name) -join " "
Write-Host "Exported Cura $Series from:"
Write-Host "  $Source"
Write-Host "to:"
Write-Host "  $configDest"
Write-Host "  $dataDest   ($printers printer(s); user plugins: $(if ($plugins) { $plugins } else { 'none' }))"
if ($NewToken) { Write-Host "The API token was not exported: the server will create a new one." }

if ($Zip) {
    # ZipArchive with "/" separators: Compress-Archive in Windows PowerShell 5.1 writes "\", which
    # Linux unzip turns into file names containing backslashes.
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    $zipPath = "$Dest.zip"
    if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath }
    $archive = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($file in Get-ChildItem -LiteralPath $Dest -Recurse -File) {
            $entry = $file.FullName.Substring($Dest.Length + 1).Replace('\', '/')
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $file.FullName, $entry) | Out-Null
        }
    } finally {
        $archive.Dispose()
    }
    Write-Host "Packed into $zipPath (inside: config/$Series and data/$Series)."
}
