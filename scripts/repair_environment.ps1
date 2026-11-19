param(
    [Parameter(Mandatory = $true)]
    [string]$BasePython
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$environmentPath = [IO.Path]::GetFullPath((Join-Path $projectRoot '.venv'))
$backupRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot '.finagent\environment_backups'))
$backupPath = Join-Path $backupRoot ('venv_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
$workspacePrefix = $projectRoot.TrimEnd('\') + '\'
foreach ($taskPath in @($environmentPath, $backupRoot, $backupPath)) {
    if (-not $taskPath.StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Environment or backup path is outside the project.'
    }
}
$baseExecutable = (Resolve-Path -LiteralPath $BasePython).Path
& $baseExecutable -c 'import sys; assert sys.version_info >= (3, 11); print("Base Python:", sys.executable, sys.version.split()[0])'
if ($LASTEXITCODE -ne 0) { throw 'Base Python cannot run or is older than 3.11.' }
if (Test-Path -LiteralPath $environmentPath) {
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    Move-Item -LiteralPath $environmentPath -Destination $backupPath
    Write-Host "Previous environment preserved: $backupPath"
}
& $baseExecutable -m venv $environmentPath
if ($LASTEXITCODE -ne 0) { throw 'Environment creation failed. The old environment remains in the backup directory.' }
$environmentPython = Join-Path $environmentPath 'Scripts\python.exe'
& $environmentPython -m pip install -e "${projectRoot}[pdf,calendar]"
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Retry using .venv\Scripts\python.exe -m pip install -e ".[pdf,calendar]" from the project root.' }
& (Join-Path $environmentPath 'Scripts\finagent.exe') --help
if ($LASTEXITCODE -ne 0) { throw 'The generated console launcher failed its startup check.' }
Write-Host 'Environment repaired. Reactivate .venv in your terminal, then run finagent chat.'
