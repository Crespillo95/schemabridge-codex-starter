$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$candidates = if ($env:SCHEMABRIDGE_PYTHON) {
    @(@{ Command = $env:SCHEMABRIDGE_PYTHON; Arguments = @() })
} else {
    @(
        @{ Command = "py"; Arguments = @("-3.13") },
        @{ Command = "py"; Arguments = @("-3.12") },
        @{ Command = "py"; Arguments = @("-3.11") },
        @{ Command = "python"; Arguments = @() }
    )
}

$selected = $null
foreach ($candidate in $candidates) {
    if (-not (Get-Command $candidate.Command -ErrorAction SilentlyContinue)) {
        continue
    }

    $candidateArguments = @($candidate.Arguments)
    & $candidate.Command @candidateArguments -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $selected = $candidate
        break
    }
}

if ($null -eq $selected) {
    throw "Python >=3.11,<3.14 was not found. Set SCHEMABRIDGE_PYTHON to a supported interpreter."
}

$pythonCommand = $selected.Command
$pythonArguments = @($selected.Arguments)
& $pythonCommand @pythonArguments -V
$pythonExecutable = (& $pythonCommand @pythonArguments -c "import sys; print(sys.executable)").Trim()
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw "The selected Python executable could not be resolved."
}

$uv = Get-Command "uv" -ErrorAction SilentlyContinue
if ($null -eq $uv) {
    throw "uv 0.11.30 is required for a frozen bootstrap."
}
$uvVersion = (& $uv.Source --version).Trim()
if ($uvVersion -ne "uv 0.11.30") {
    throw "uv must be exactly 0.11.30."
}

& $uv.Source lock --check --no-python-downloads
if ($LASTEXITCODE -ne 0) {
    throw "The frozen dependency lock is not current."
}
& $uv.Source sync --frozen --all-extras --all-groups --python $pythonExecutable --no-python-downloads
if ($LASTEXITCODE -ne 0) {
    throw "Frozen bootstrap failed."
}
& .venv\Scripts\schemabridge.exe version
& .venv\Scripts\schemabridge.exe doctor

Write-Host "`nBootstrap complete. Next: run make check."
