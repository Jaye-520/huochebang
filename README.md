# PP-OCRv6 Medium OCR

The `x001.ocr.OCRRecognizer` module extracts text from images with PaddleOCR's
PP-OCRv6 medium detection and recognition models.

Install dependencies with [uv](https://docs.astral.sh/uv/):

```powershell
uv sync
```

On Windows, install the [Microsoft Visual C++ 2015–2022 Redistributable
(x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe) required by PyTorch.

Run OCR on an image:

```powershell
uv run x001-ocr path\to\image.png
```

The first run downloads the model weights. Recognized text is printed to the
console.

The module can also be used from Python:

```python
from x001.ocr import OCRRecognizer

recognizer = OCRRecognizer()
texts = recognizer.extract_text("path/to/image.png")
```

## Android device control with ADB

`ADBController` is pinned to operate the 货车帮司机 app (`com.wlqq`) on the
Xiaomi MIX 3 (serial `7450df55`) out of the box: the device serial, the
UiAutomator input backend, and the launch activity are hardcoded as defaults.
It uses the bundled Windows platform-tools (revision 37.0.1); on other
platforms, install ADB and make it available on `PATH`. Enable USB debugging,
connect the phone, and confirm it is listed by running
`.\src\x001\platform-tools\adb.exe devices`.

```python
from x001.adb import ADBController

device = ADBController()
device.launch()  # 启动货车帮司机首页
device.tap(540, 1200)
device.back()
device.swipe(540, 1500, 540, 500, duration_ms=400)
```

The default `input_backend="uiautomator"` is required for 货车帮司机 because
the app ignores touch events injected by `adb shell input`. UiAutomator
connects lazily and reuses the connection for later commands. All defaults can
still be overridden per call site (`serial=`, `adb_path=`, `input_backend=`);
the plain `adb` backend remains available for other apps. `adb_path` is also
used by UiAutomator through `ADBUTILS_ADB_PATH`.

ADB command failures and timeouts are raised to the caller. Coordinates are in
screen pixels; make sure they match the connected phone's display (1080×2340).

## Development checks

Run the linter, formatter check, and unit tests with:

```powershell
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests -v
```
