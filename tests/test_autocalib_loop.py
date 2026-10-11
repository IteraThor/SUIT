import unittest
import os
import tempfile
from tools.agentic_autocalib_loop import CalibrationLedger, LedgerEntry


class TestCalibrationLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.ledger = CalibrationLedger(self.tmp.name)

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    def test_record_and_load_entry(self):
        entry = LedgerEntry(
            iteration=1,
            hypothesis="Test gamma 460 with quantile tone matching",
            parameters={"exposure": 300, "gain": 40, "contrast": 30, "sharpness": 80, "gamma": 460},
            approved=True,
            composite_gain=85.0,
            notes="Zero highlight clipping, clean Hough lines",
        )
        self.ledger.record(entry)
        entries = self.ledger.load_all()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].iteration, 1)
        self.assertTrue(entries[0].approved)


if __name__ == "__main__":
    unittest.main()
