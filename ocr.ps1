# OCR PNGs with the built-in Windows OCR engine; prints one JSON object per text line:
# {"text": "...", "x": left, "y": top, "h": height}
#   ocr.ps1 -Path file.png      one image
#   ocr.ps1 -Server             read image paths from stdin, one per line; after each image
#                               print the lines followed by ##END## (keeps the engine warm)
param([string]$Path, [switch]$Server)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime] | Out-Null

$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) {
    $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op))
    $task.Wait() | Out-Null
    $task.Result
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()

function Read-Image([string]$p) {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($p)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        foreach ($line in $result.Lines) {
            $w = ($line.Words | Select-Object -First 1).BoundingRect
            [pscustomobject]@{ text = $line.Text; x = [int]$w.X; y = [int]$w.Y; h = [int]$w.Height } | ConvertTo-Json -Compress
        }
    } finally { $stream.Dispose() }
}

if ($Server) {
    while ($null -ne ($p = [Console]::In.ReadLine())) {
        try { Read-Image $p } catch { }
        "##END##"
        [Console]::Out.Flush()
    }
} else {
    Read-Image (Resolve-Path $Path).Path
}
