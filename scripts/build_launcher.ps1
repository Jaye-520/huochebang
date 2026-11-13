param(
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$uvPath = (Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1).Source
if (-not $uvPath) {
    $defaultUvPath = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path -LiteralPath $defaultUvPath -PathType Leaf) {
        $uvPath = $defaultUvPath
    } else {
        throw "uv.exe not found. Run 'winget install --id astral-sh.uv -e', reopen PowerShell, and run 'uv --version' before retrying. See https://docs.astral.sh/uv/getting-started/installation/"
    }
}
$dist = if ($OutputDirectory) {
    $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory)
} else {
    Join-Path $root "dist"
}
$work = Join-Path $root "build\launcher"
$source = Join-Path $root "src\x001\launcher.py"
$sourcePath = Join-Path $root "src"
$icon = Join-Path $root "src\x001\assets\x001.ico"
$uvBinary = "$uvPath;."
$target = Join-Path $dist "X001.exe"
if (Test-Path -LiteralPath $target -PathType Leaf) {
    try {
        $handle = [System.IO.File]::Open(
            $target, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        $handle.Dispose()
    }
    catch {
        throw "Cannot overwrite $target. Close X001 or pass -OutputDirectory. $($_.Exception.Message)"
    }
}

Push-Location $root
try {
    & $uvPath tool run --python 3.14 --from pyinstaller pyinstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name X001 `
        --distpath $dist `
        --workpath $work `
        --specpath $work `
        --paths $sourcePath `
        --icon $icon `
        --add-binary $uvBinary `
        $source
    if ($LASTEXITCODE -ne 0) {
        throw "Launcher build failed (exit code $LASTEXITCODE)."
    }
}
finally {
    Pop-Location
}
