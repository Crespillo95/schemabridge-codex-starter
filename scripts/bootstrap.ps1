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
& $pythonCommand @pythonArguments -m venv --clear .venv
& .venv\Scripts\python.exe -m pip install --upgrade pip
& .venv\Scripts\python.exe -m pip install -e ".[dev,postgres,sql,ui,llm]"
& .venv\Scripts\schemabridge.exe version
& .venv\Scripts\schemabridge.exe doctor

Write-Host "`nBootstrap complete. Next: run make check."
