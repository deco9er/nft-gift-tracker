$ErrorActionPreference = 'Stop'

$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$sharedPython = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'
$entryPoint = Join-Path $PSScriptRoot 'parser\catalog_bot.py'

if (Test-Path -LiteralPath $projectPython -PathType Leaf) {
    $python = $projectPython
} elseif (Test-Path -LiteralPath $sharedPython -PathType Leaf) {
    $python = $sharedPython
} else {
    $python = (Get-Command python -CommandType Application).Source
}

& $python $entryPoint --login
exit $LASTEXITCODE
