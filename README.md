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

Enable USB debugging on the Android phone, connect it to the computer, and
confirm it is listed by `adb devices`. Then use `ADBController` to tap screen
coordinates, go back, or swipe:

```python
from x001.adb import ADBController

device = ADBController()
device.tap(540, 1200)
device.back()
device.swipe(540, 1500, 540, 500, duration_ms=400)
```

If more than one device is connected, pass its serial from `adb devices`:

```python
device = ADBController(serial="device-serial")
```

ADB command failures and timeouts are raised to the caller. Coordinates are in
screen pixels; make sure they match the connected phone's display.
