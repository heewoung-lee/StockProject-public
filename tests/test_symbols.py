import tempfile
import unittest
from pathlib import Path

from stockbot.symbols import SymbolDirectory, load_symbol_directory


class SymbolDirectoryTest(unittest.TestCase):
    def test_known_symbol_has_company_label(self):
        directory = SymbolDirectory({"005930": "삼성전자"})

        self.assertEqual("삼성전자", directory.name_for("005930"))
        self.assertEqual("삼성전자 (005930)", directory.label_for("005930"))

    def test_unknown_symbol_falls_back_without_crash(self):
        directory = SymbolDirectory({})

        self.assertEqual("알 수 없음", directory.name_for("999999"))
        self.assertEqual("알 수 없음 (999999)", directory.label_for("999999"))

    def test_loads_csv_symbol_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "symbols.csv"
            path.write_text("symbol,name\n005930,삼성전자\n", encoding="utf-8")

            directory = load_symbol_directory(path)

        self.assertEqual("삼성전자 (005930)", directory.label_for("005930"))

    def test_rejects_missing_required_csv_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "symbols.csv"
            path.write_text("code,company\n005930,삼성전자\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_symbol_directory(path)

    def test_rejects_duplicate_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "symbols.csv"
            path.write_text("symbol,name\n005930,삼성전자\n005930,삼성전자우\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_symbol_directory(path)

    def test_committed_symbol_file_loads_public_stock_names(self):
        path = Path(__file__).resolve().parents[1] / "data" / "symbols.csv"

        directory = load_symbol_directory(path)

        self.assertEqual("삼성전자 (005930)", directory.label_for("005930"))
        self.assertEqual("SK하이닉스 (000660)", directory.label_for("000660"))


if __name__ == "__main__":
    unittest.main()
