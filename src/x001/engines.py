"""采集进程使用的 Windows 和 PaddleOCR 识别后端。"""

import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import numpy as np
from PIL import Image

from .ocr import OCRRecognizer
from .vision import Line


class WindowsOCR:
    """复用一个 PowerShell 进程调用系统简体中文 OCR。"""

    def __init__(self) -> None:
        self.temp = TemporaryDirectory(prefix="x001-ocr-")
        self.path = Path(self.temp.name) / "screen.png"
        script = Path(__file__).with_name("windows_ocr.ps1")
        try:
            self.process = subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except BaseException:
            self.temp.cleanup()
            raise
        try:
            if self.process.stdout.readline().strip() != "ready":
                raise RuntimeError("Windows OCR could not initialize zh-Hans-CN")
        except BaseException:
            self.close()
            raise

    def locate(self, image: Image.Image) -> list[Line]:
        image.save(self.path, compress_level=1)
        self.process.stdin.write(json.dumps(str(self.path)) + "\n")
        self.process.stdin.flush()
        response = self.process.stdout.readline()
        if not response:
            raise RuntimeError("Windows OCR stopped unexpectedly")
        result = json.loads(response)
        if "error" in result:
            raise RuntimeError(result["error"])

        lines = []
        for words in result["lines"]:
            segment = []
            for word in words:
                if segment and word[1] - segment[-1][3] > max(22, image.width // 32):
                    lines.append(_merge(segment))
                    segment = []
                segment.append(word)
            if segment:
                lines.append(_merge(segment))
        return lines

    def close(self) -> None:
        try:
            try:
                self.process.stdin.close()
            finally:
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        finally:
            self.process.stdout.close()
            self.temp.cleanup()


def _merge(words: list[Line]) -> Line:
    return (
        "".join(word[0] for word in words),
        min(word[1] for word in words),
        min(word[2] for word in words),
        max(word[3] for word in words),
        max(word[4] for word in words),
    )


class PaddleEngine:
    """将识别框转换回输入图片的像素坐标。"""

    def __init__(self) -> None:
        import torch

        torch.set_num_threads(min(4, os.cpu_count() or 1))
        torch.set_num_interop_threads(1)
        self.ocr = OCRRecognizer()

    def locate(self, image: Image.Image) -> list[Line]:
        scale = min(1, 864 / image.width)
        resized = image.resize(
            (round(image.width * scale), round(image.height * scale)),
            Image.Resampling.LANCZOS,
        )
        return [
            (
                s,
                round(x / scale),
                round(y / scale),
                round(x2 / scale),
                round(y2 / scale),
            )
            for s, x, y, x2, y2 in self.ocr.locate_text(np.asarray(resized))
        ]

    def close(self) -> None:
        pass


def open_engine(backend: str) -> WindowsOCR | PaddleEngine:
    """启动指定后端，或按平台选择可用的后端。"""
    start = monotonic()
    if os.name == "nt" and backend in ("auto", "windows"):
        try:
            engine = WindowsOCR()
        except OSError, RuntimeError:
            if backend == "windows":
                raise
        else:
            print(
                f"OCR Windows zh-Hans-CN ready in {monotonic() - start:.2f}s",
                flush=True,
            )
            return engine
    if backend == "windows":
        raise RuntimeError("Windows OCR requires Windows and the zh-Hans-CN OCR pack")
    engine = PaddleEngine()
    print(f"OCR PP-OCRv6 Medium ready in {monotonic() - start:.2f}s", flush=True)
    return engine
