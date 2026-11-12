import argparse
from pathlib import Path


class OCRRecognizer:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract text from an image.")
    parser.add_argument("image", type=Path, help="Path to the input image")
    args = parser.parse_args()

    if not args.image.is_file():
        parser.error(f"image file does not exist: {args.image}")

    texts = OCRRecognizer().extract_text(args.image)
    if texts:
        print("\n".join(texts))
    else:
        print("No text detected.")


if __name__ == "__main__":
    main()
