import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from x001.engines import HybridEngine
from x001.ocr import OCRRecognizer


class OCRRecognizerTests(unittest.TestCase):
    def test_selects_ppocrv5_mobile_with_openvino_cpu(self):
        from rapidocr.utils.typings import EngineType, ModelType, OCRVersion

        with patch("rapidocr.RapidOCR") as rapid:
            OCRRecognizer()

        params = rapid.call_args.kwargs["params"]
        self.assertIs(params["Det.engine_type"], EngineType.OPENVINO)
        self.assertIs(params["Det.model_type"], ModelType.MOBILE)
        self.assertIs(params["Det.ocr_version"], OCRVersion.PPOCRV5)
        self.assertIs(params["Rec.engine_type"], EngineType.OPENVINO)
        self.assertIs(params["Rec.model_type"], ModelType.MOBILE)
        self.assertIs(params["Rec.ocr_version"], OCRVersion.PPOCRV5)
        self.assertFalse(params["Global.use_cls"])

    def test_locate_text_maps_boxes_and_filters_low_scores(self):
        recognizer = object.__new__(OCRRecognizer)
        recognizer._ocr = Mock(
            return_value=SimpleNamespace(
                boxes=np.asarray(
                    [
                        [[1.2, 2.1], [9.9, 2.3], [9.2, 8.7], [1.3, 9.0]],
                        [[12, 4], [18, 4], [18, 9], [12, 9]],
                    ]
                ),
                txts=("33-35吨", "noise"),
                scores=np.asarray([0.95, 0.2]),
            )
        )
        image = np.zeros((20, 30, 3), dtype=np.uint8)
        image[0, 0] = (1, 2, 3)

        self.assertEqual(recognizer.locate_text(image), [("33-35吨", 1, 2, 9, 9)])
        received = recognizer._ocr.call_args.args[0]
        np.testing.assert_array_equal(received[0, 0], (3, 2, 1))


class HybridEngineTests(unittest.TestCase):
    def test_loads_secondary_engine_only_when_requested(self):
        engine = object.__new__(HybridEngine)
        engine._secondary = None
        image = Image.new("RGB", (20, 20), "white")
        lines = [("33-35吨", 1, 2, 10, 12)]

        with patch("x001.engines.RapidEngine") as rapid:
            rapid.return_value.locate.return_value = lines
            self.assertEqual(engine.locate_secondary(image), lines)

        rapid.assert_called_once_with()
        rapid.return_value.locate.assert_called_once_with(image)


if __name__ == "__main__":
    unittest.main()
