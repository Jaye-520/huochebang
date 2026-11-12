$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
}
function Wait-Operation {
    param($Operation, [Type]$ResultType)

    $task = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.GetAwaiter().GetResult()
}
$language = [Windows.Globalization.Language]::new('zh-Hans-CN')
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) {
    throw 'Windows OCR requires the zh-Hans-CN OCR language pack.'
}
[Console]::WriteLine('ready')
while ($null -ne ($request = [Console]::ReadLine())) {
    try {
        $path = ConvertFrom-Json -InputObject $request
        $memory = [IO.MemoryStream]::new([IO.File]::ReadAllBytes($path))
        $stream = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($memory)
        try {
            $decoder = Wait-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
            $bitmap = Wait-Operation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
            try {
                $result = Wait-Operation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
                $lines = @(
                    foreach ($line in $result.Lines) {
                        ,@(
                            foreach ($word in $line.Words) {
                                $box = $word.BoundingRect
                                ,@($word.Text, [int]$box.X, [int]$box.Y, [int]$box.Right, [int]$box.Bottom)
                            }
                        )
                    }
                )
                [Console]::WriteLine((ConvertTo-Json -Compress -Depth 5 -InputObject @{lines = $lines}))
            } finally {
                $bitmap.Dispose()
            }
        } finally {
            $stream.Dispose()
            $memory.Dispose()
        }
    } catch {
        [Console]::WriteLine((ConvertTo-Json -Compress -InputObject @{error = $_.ToString()}))
    }
}
