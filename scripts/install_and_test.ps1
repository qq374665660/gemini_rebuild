param(
    [switch]$SkipVenv,
    [switch]$AllowPandasPatch
)

$ErrorActionPreference = "Stop"

$rootDir = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $rootDir

if (-not $SkipVenv) {
    if (-not (Test-Path ".venv")) {
        python -m venv .venv
    }
    . .venv\Scripts\Activate.ps1
}

python -m pip install --upgrade pip

try {
    python -m pip install -r requirements.txt
} catch {
    if ($AllowPandasPatch) {
        python -m pip install "pandas>=2.2.2,<2.3"
        python -m pip install -r requirements.txt --no-deps
    } else {
        Write-Host "Failed to install requirements. Use -AllowPandasPatch to allow pandas patch version."
        exit 1
    }
}

python manage.py migrate
python manage.py test core
