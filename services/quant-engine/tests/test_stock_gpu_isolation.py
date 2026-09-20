import pickle
import tempfile
import unittest
from pathlib import Path

from quant_engine.stock_ml import _train_ranker_isolated


class StockGpuIsolationTest(unittest.TestCase):
    def test_ranker_worker_returns_model_and_metadata(self):
        rows = []
        for day_index in range(45):
            day = f"{20260000 + day_index:08d}"
            for stock_index in range(12):
                rows.append({
                    "trade_date": day,
                    "code": f"{stock_index:06d}.SZ",
                    "momentum20": stock_index + day_index * 0.01,
                    "momentum60": 12 - stock_index + day_index * 0.02,
                    "forward_3": (stock_index - 6) * 0.001 + day_index * 0.00001,
                })

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            frame_path = root_path / "frame.pkl"
            output_path = root_path / "output"
            output_path.mkdir()
            with frame_path.open("wb") as fh:
                pickle.dump(rows, fh, protocol=pickle.HIGHEST_PROTOCOL)
            result = _train_ranker_isolated(
                frame_path, "forward_3",
                {"deviceType": "cpu", "featuresOverride": ["momentum20", "momentum60"],
                 "validationDays": 8, "testDays": 8, "nEstimators": 10,
                 "earlyStopping": 3, "labelGrades": 5},
                output_path, lambda: False,
            )

            self.assertEqual(result["effectiveDevice"], "cpu")
            self.assertGreater(result["trainRows"], 0)
            self.assertTrue(Path(result["modelPath"]).exists())


if __name__ == "__main__":
    unittest.main()
