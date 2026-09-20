from __future__ import annotations

import unittest

from quant_engine.stock_ml.factors import _pit_eligible


class PointInTimeUniverseTest(unittest.TestCase):
    def test_listing_age_is_evaluated_per_trade_date(self):
        status = {"is_st": 0, "is_suspended": 0, "list_status": "L"}
        self.assertFalse(_pit_eligible("20260401", "20260101", 120, status, True))
        self.assertTrue(_pit_eligible("20260515", "20260101", 120, status, True))

    def test_historical_status_controls_eligibility(self):
        self.assertFalse(_pit_eligible(
            "20260515", "20200101", 120,
            {"is_st": 1, "is_suspended": 0, "list_status": "L"}, True, "正常名称"))
        self.assertFalse(_pit_eligible(
            "20260515", "20200101", 120,
            {"is_st": 0, "is_suspended": 1, "list_status": "L"}, True, "正常名称"))
        self.assertTrue(_pit_eligible(
            "20260515", "20200101", 120,
            {"is_st": 0, "is_suspended": 0, "list_status": "L"}, True, "当前ST名称不应回看"))

    def test_missing_pit_status_is_conservatively_excluded(self):
        self.assertFalse(_pit_eligible("20260515", "20200101", 120, None, True))


if __name__ == "__main__":
    unittest.main()
