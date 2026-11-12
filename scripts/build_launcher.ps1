$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$uvCommand = Get-Command uv -ErrorAction Stop
$dist = Join-Path $root "dist"
$work = Join-Path $root "build\launcher"
$source = Join-Path $root "src\x001\launcher.py"
$uvBinary = "$($uvCommand.Source);."

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
        --add-binary $uvBinary `
        $source
}
finally {
    Pop-Location
}
