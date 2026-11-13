param(
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$uvCommand = Get-Command uv -ErrorAction Stop
$dist = if ($OutputDirectory) {
    $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory)
} else {
    Join-Path $root "dist"
}
$work = Join-Path $root "build\launcher"
$source = Join-Path $root "src\x001\launcher.py"
$sourcePath = Join-Path $root "src"
$icon = Join-Path $root "src\x001\assets\x001.ico"
$uvBinary = "$($uvCommand.Source);."
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
    uvx --python 3.14 --from pyinstaller pyinstaller `
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
