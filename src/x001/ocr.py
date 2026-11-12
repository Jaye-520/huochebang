"""使用 PaddleOCR 识别本地图片。"""

import argparse
from pathlib import Path

from numpy.typing import NDArray


class OCRRecognizer:
    """复用 PP-OCRv6 Medium 模型进行文字检测和识别。"""

    def __init__(self) -> None:
        from paddleocr import PaddleOCR

        self._ocr = PaddleOCR(
            text_detection_model_name="PP-OCRv6_medium_det",
            text_recognition_model_name="PP-OCRv6_medium_rec",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            engine="transformers",
        )

    def extract_text(self, image_path: str | Path) -> list[str]:
        image = Path(image_path)
        if not image.is_file():
            raise FileNotFoundError(f"Image file does not exist: {image}")

        texts: list[str] = []
        for result in self._ocr.predict(str(image)):
            texts.extend(result["rec_texts"])
        return texts

    def locate_text(self, image: NDArray) -> list[tuple[str, int, int, int, int]]:
        """返回 RGB 图片中每段文字及其像素边界。"""
        lines = []
        for result in self._ocr.predict(image[:, :, ::-1].copy()):
            for text, polygon in zip(
                result["rec_texts"], result["rec_polys"], strict=True
            ):
                x, y = polygon.T
                lines.append((text, int(min(x)), int(min(y)), int(max(x)), int(max(y))))
        return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="识别本地图片中的文字")
    parser.add_argument("image", type=Path, help="待识别的图片路径")
    args = parser.parse_args()

    if not args.image.is_file():
        parser.error(f"图片文件不存在 {args.image}")

    texts = OCRRecognizer().extract_text(args.image)
    if texts:
        print("\n".join(texts))
    else:
        print("未识别到文字。")


if __name__ == "__main__":
    main()
