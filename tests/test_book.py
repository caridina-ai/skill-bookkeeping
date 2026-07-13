import csv
import importlib.util
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BOOK = ROOT / "scripts" / "book.py"
DEFAULT_CATEGORIES = [
    "外食費", "買菜金", "民俗節日", "居住費", "管理費", "交通費",
    "車稅險", "保健費", "治裝費", "精進金", "旅遊金", "公關費",
    "孝親費", "奉獻", "歸墊", "雜費",
]


class BookCLITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "book.db"

    def tearDown(self):
        self.tmp.cleanup()

    def run_book(self, *args):
        env = self.book_env()
        result = subprocess.run(
            [sys.executable, str(BOOK), *map(str, args)],
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout.rstrip("\n")

    def book_env(self):
        env = os.environ.copy()
        env["BOOK_DB"] = str(self.db)
        env["PYTHONUTF8"] = "1"
        return env

    def popen_book(self, *args):
        return subprocess.Popen(
            [sys.executable, str(BOOK), *map(str, args)],
            cwd=ROOT,
            env=self.book_env(),
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_fresh_database_has_original_16_categories(self):
        self.assertEqual(self.run_book("categories").splitlines(), DEFAULT_CATEGORIES)
        for index, category in enumerate(DEFAULT_CATEGORIES, start=1):
            output = self.run_book(
                "out", "--category", category, "--note", f"測試{index}",
                "--amount", "1", "--date", "2026-01-01",
            )
            self.assertTrue(output.startswith(f"2026-01-01 {category} (測試{index}) 1"))
        with sqlite3.connect(self.db) as con:
            rows = con.execute(
                "SELECT name FROM categories ORDER BY position,id"
            ).fetchall()
        con.close()
        self.assertEqual([row[0] for row in rows], DEFAULT_CATEGORIES)

    def test_starting_balance_is_absolute_adjustment_not_income(self):
        self.run_book(
            "out", "--category", "買菜金", "--note", "買菜", "--amount", "183",
            "--date", "2026-07-01 10:12",
        )
        self.run_book(
            "out", "--category", "外食費", "--note", "午餐", "--amount", "390",
            "--date", "2026-07-01 12:20",
        )
        self.assertEqual(
            self.run_book("adjust", "--balance", "2000"),
            "餘額校正 -573 -> 2000",
        )
        self.assertEqual(self.run_book("balance"), "餘額 2000")
        con = sqlite3.connect(self.db)
        try:
            row = con.execute(
                "SELECT kind, note, amount FROM entries ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(row, ("adj", "餘額校正", 2573))

    def test_category_lifecycle_and_empty_list(self):
        self.assertEqual(self.run_book("category-clear"), "已刪除全部科目")
        self.assertEqual(self.run_book("categories"), "目前沒有科目")
        self.assertEqual(
            self.run_book("category-add", "--name", "餐飲"), "新增科目 餐飲"
        )
        self.assertEqual(
            self.run_book("category-add", "--name", "醫療"), "新增科目 醫療"
        )
        self.assertEqual(
            self.run_book("category-add", "--name", "餐飲"),
            "錯誤：科目「餐飲」已存在",
        )
        self.assertEqual(
            self.run_book(
                "category-rename", "--from", "醫療", "--to", "健康"
            ),
            "科目 醫療 -> 健康",
        )
        self.assertEqual(
            self.run_book("category-delete", "--name", "健康"), "刪除科目 健康"
        )
        self.assertEqual(self.run_book("categories"), "餐飲")

    def test_category_replace_is_atomic_and_keeps_user_order(self):
        self.assertEqual(
            self.run_book(
                "category-replace", "--name", "餐飲", "--name", "買菜",
                "--name", "交通", "--name", "醫療",
            ),
            "已替換科目（共 4 個）",
        )
        self.assertEqual(
            self.run_book("categories").splitlines(), ["餐飲", "買菜", "交通", "醫療"]
        )
        self.assertEqual(
            self.run_book(
                "category-replace", "--name", "新科", "--name", "新科"
            ),
            "錯誤：科目「新科」重複，未變更任何科目",
        )
        self.assertEqual(
            self.run_book("categories").splitlines(), ["餐飲", "買菜", "交通", "醫療"]
        )
        self.run_book(
            "out", "--category", "餐飲", "--note", "午餐", "--amount", "100"
        )
        self.assertEqual(
            self.run_book(
                "category-replace", "--name", "買菜", "--name", "交通"
            ),
            "無法替換科目：帳目仍在使用「餐飲」，請先刪除這些帳目",
        )
        self.assertEqual(
            self.run_book("categories").splitlines(), ["餐飲", "買菜", "交通", "醫療"]
        )

    def test_category_replace_rolls_back_if_an_insert_fails_midway(self):
        before = self.run_book("categories").splitlines()
        with sqlite3.connect(self.db) as con:
            con.execute(
                "CREATE TRIGGER reject_bad_category BEFORE INSERT ON categories "
                "WHEN NEW.name='故障科目' BEGIN "
                "SELECT RAISE(ABORT, '模擬插入失敗'); END"
            )
        con.close()
        result = subprocess.run(
            [
                sys.executable, str(BOOK), "category-replace",
                "--name", "新科目", "--name", "故障科目",
            ],
            cwd=ROOT,
            env=self.book_env(),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.run_book("categories").splitlines(), before)

    def test_used_category_cannot_be_deleted_or_cleared_but_can_be_renamed(self):
        self.run_book("category-clear")
        self.run_book("category-add", "--name", "餐飲")
        self.assertEqual(
            self.run_book(
                "out", "--category", "餐飲", "--note", "午餐", "--amount", "120",
                "--date", "2026-07-09 12:30",
            ),
            "2026-07-09 12:30 餐飲 (午餐) 120 餘額 -120",
        )
        self.assertEqual(
            self.run_book("category-delete", "--name", "餐飲"),
            "無法刪除科目「餐飲」：仍有 1 筆帳目使用此科目，請先刪除這些帳目",
        )
        self.assertEqual(
            self.run_book("category-clear"),
            "無法刪除全部科目：仍有 1 筆帳目使用科目，請先刪除這些帳目",
        )
        self.assertEqual(
            self.run_book(
                "category-rename", "--from", "餐飲", "--to", "伙食"
            ),
            "科目 餐飲 -> 伙食",
        )
        detail = self.run_book("detail", "--date", "2026-07-09")
        self.assertIn("12:30 伙食 (午餐) 120", detail)
        self.assertEqual(self.run_book("categories"), "伙食")

    def test_time_is_preserved_for_record_edit_delete_and_detail(self):
        self.assertEqual(
            self.run_book(
                "in", "--note", "現金", "--amount", "1000", "--date",
                "2026-07-09 08:05",
            ),
            "2026-07-09 08:05 入金 (現金) 1000 餘額 1000",
        )
        self.assertEqual(
            self.run_book(
                "out", "--category", "保健費", "--note", "看牙", "--amount", "350",
                "--date", "2026-07-09 14:55",
            ),
            "2026-07-09 14:55 保健費 (看牙) 350 餘額 650",
        )
        self.assertEqual(
            self.run_book(
                "edit", "--find", "看牙", "--to", "300", "--date",
                "2026-07-09 14:55",
            ),
            "2026-07-09 14:55 保健費 (看牙) 350 -> 300 餘額 650 -> 700",
        )
        detail = self.run_book("detail", "--date", "2026-07-09")
        self.assertIn("14:55 保健費 (看牙) 300", detail)
        self.assertNotIn("入金", detail)
        self.assertEqual(
            self.run_book(
                "delete", "--find", "現金", "--date", "2026-07-09 08:05"
            ),
            "2026-07-09 08:05 刪除 入金 (現金) 1000 餘額 700 -> -300",
        )

    def test_explicit_midnight_remains_visible_when_editing_and_deleting(self):
        self.run_book(
            "in", "--note", "期初", "--amount", "500", "--date", "2026-07-01"
        )
        self.run_book(
            "out", "--category", "外食費", "--note", "午夜餐", "--amount", "100",
            "--date", "2026-07-09 00:00",
        )
        self.assertEqual(
            self.run_book(
                "edit", "--find", "午夜餐", "--to", "120", "--date",
                "2026-07-09 00:00",
            ),
            "2026-07-09 00:00 外食費 (午夜餐) 100 -> 120 餘額 400 -> 380",
        )
        self.assertEqual(
            self.run_book(
                "delete", "--find", "午夜餐", "--date", "2026-07-09 00:00"
            ),
            "2026-07-09 00:00 刪除 外食費 (午夜餐) 120 餘額 380 -> 500",
        )

    def test_edit_and_delete_accept_or_search_terms(self):
        self.run_book(
            "in", "--note", "期初", "--amount", "1000", "--date", "2026-07-01"
        )
        self.run_book(
            "out", "--category", "居住費", "--note", "七月台灣自來水帳單",
            "--amount", "230", "--date", "2026-07-10 11:20",
        )
        self.run_book(
            "out", "--category", "居住費", "--note", "六月自來水帳單",
            "--amount", "410", "--date", "2026-06-10 09:15",
        )
        self.assertEqual(
            self.run_book(
                "edit", "--find", "水費", "--find", "水電", "--find", "自來水",
                "--amount", "230", "--to", "250",
            ),
            "居住費 (七月台灣自來水帳單) 230 -> 250 餘額 360 -> 340",
        )
        self.assertEqual(
            self.run_book(
                "delete", "--find", "瓦斯", "--find", "自來水", "--amount", "250"
            ),
            "刪除 居住費 (七月台灣自來水帳單) 250 餘額 340 -> 590",
        )
        self.assertEqual(
            self.run_book(
                "edit", "--find", "水費", "--find", "費用", "--to", "200"
            ),
            "找不到符合「水費、費用」的紀錄，請確認",
        )

    def test_multiple_matches_require_rowid_before_edit_or_delete(self):
        self.run_book(
            "in", "--note", "期初", "--amount", "1000", "--date", "2026-07-01"
        )
        self.run_book(
            "out", "--category", "居住費", "--note", "七月水費", "--amount", "100",
            "--date", "2026-07-02 09:00",
        )
        self.run_book(
            "out", "--category", "居住費", "--note", "七月水電", "--amount", "200",
            "--date", "2026-07-03 10:00",
        )
        with sqlite3.connect(self.db) as con:
            ids = dict(con.execute("SELECT note,id FROM entries WHERE kind='out'").fetchall())
        con.close()

        matches = self.run_book(
            "edit", "--find", "水費", "--find", "水電", "--to", "110"
        )
        self.assertIn("找到 2 筆符合「水費、水電」的紀錄，未修改任何帳目：", matches)
        self.assertIn(f"rowid {ids['七月水費']} | 2026-07-02 09:00 | 居住費 | 七月水費 | 100", matches)
        self.assertIn(f"rowid {ids['七月水電']} | 2026-07-03 10:00 | 居住費 | 七月水電 | 200", matches)
        self.assertTrue(matches.endswith("請依上述內容選定 rowid，再執行修改。"))
        self.assertEqual(self.run_book("balance"), "餘額 700")

        self.assertEqual(
            self.run_book(
                "edit", "--rowid", str(ids["七月水費"]), "--to", "110"
            ),
            "2026-07-02 09:00 居住費 (七月水費) 100 -> 110 餘額 700 -> 690",
        )
        delete_matches = self.run_book(
            "delete", "--find", "水費", "--find", "水電"
        )
        self.assertIn("找到 2 筆符合「水費、水電」的紀錄，未刪除任何帳目：", delete_matches)
        self.assertTrue(delete_matches.endswith("請依上述內容選定 rowid，再執行刪除。"))
        self.assertEqual(self.run_book("balance"), "餘額 690")
        self.assertEqual(
            self.run_book("delete", "--rowid", str(ids["七月水電"])),
            "2026-07-03 10:00 刪除 居住費 (七月水電) 200 餘額 690 -> 890",
        )
        with sqlite3.connect(self.db) as con:
            remaining = con.execute(
                "SELECT note,amount FROM entries WHERE kind='out' ORDER BY id"
            ).fetchall()
        con.close()
        self.assertEqual(remaining, [("七月水費", 110)])

    def test_report_category_detail_expand_and_adjust_outputs(self):
        self.run_book(
            "in", "--note", "期初", "--amount", "1000", "--date", "2026-07-01"
        )
        self.run_book(
            "out", "--category", "外食費", "--note", "冰紅茶", "--amount", "25",
            "--date", "2026-07-02 09:10",
        )
        self.run_book(
            "out", "--category", "保健費", "--note", "牙醫", "--amount", "300",
            "--date", "2026-07-03 14:55",
        )
        report = self.run_book("report", "--month", "2026-07")
        self.assertIn("外食費", report)
        self.assertIn("保健費", report)
        self.assertIn("合計", report)
        self.assertTrue(report.endswith("325"))
        category_detail = self.run_book("category-detail", "--month", "2026-07")
        self.assertIn("2026-07-02 09:10 冰紅茶 25", category_detail)
        expanded = self.run_book(
            "expand", "--category", "保健費", "--month", "2026-07"
        )
        self.assertIn("2026-07-03 14:55 牙醫 300", expanded)
        self.assertEqual(
            self.run_book("adjust", "--balance", "1883"),
            "餘額校正 675 -> 1883",
        )
        self.assertEqual(self.run_book("balance"), "餘額 1883")

    def test_multiple_months_and_multiple_dates(self):
        records = [
            ("2026-06-30 08:00", "外食費", "六月早餐", "50"),
            ("2026-07-09 09:00", "外食費", "七月早餐", "60"),
            ("2026-07-11 18:30", "保健費", "七月藥品", "70"),
        ]
        for date, category, note, amount in records:
            self.run_book(
                "out", "--category", category, "--note", note, "--amount", amount,
                "--date", date,
            )
        report = self.run_book(
            "report", "--month", "2026-06", "--month", "2026-07"
        )
        self.assertEqual(report.count("2026-06\n"), 1)
        self.assertEqual(report.count("2026-07\n"), 1)
        report_blocks = report.split("\n\n")
        self.assertTrue(report_blocks[0].endswith("50"))
        self.assertTrue(report_blocks[1].endswith("130"))
        report_dates = self.run_book(
            "report", "--date", "2026-07-09", "--date", "2026-07-11"
        )
        self.assertIn("外食費 60", report_dates)
        self.assertIn("保健費 70", report_dates)
        date_blocks = report_dates.split("\n\n")
        self.assertTrue(date_blocks[0].endswith("60"))
        self.assertTrue(date_blocks[1].endswith("70"))
        category_detail = self.run_book(
            "category-detail", "--month", "2026-06", "--month", "2026-07"
        )
        self.assertIn("六月早餐 50", category_detail)
        self.assertIn("七月藥品 70", category_detail)
        category_detail_dates = self.run_book(
            "category-detail", "--date", "2026-07-09", "--date", "2026-07-11"
        )
        self.assertIn("2026-07-09 09:00 七月早餐 60", category_detail_dates)
        self.assertIn("2026-07-11 18:30 七月藥品 70", category_detail_dates)
        expanded = self.run_book(
            "expand", "--category", "保健費", "--category", "外食費",
            "--month", "2026-06", "--month", "2026-07",
        )
        self.assertLess(expanded.find("保健費"), expanded.find("外食費"))
        self.assertEqual(expanded.count("2026-06\n"), 1)
        self.assertEqual(expanded.count("2026-07\n"), 1)
        expanded_dates = self.run_book(
            "expand", "--category", "保健費", "--category", "外食費",
            "--date", "2026-07-09", "--date", "2026-07-11",
        )
        self.assertEqual(expanded_dates.count("2026-07-09\n"), 1)
        self.assertEqual(expanded_dates.count("2026-07-11\n"), 1)
        self.assertIn("七月早餐 60", expanded_dates)
        self.assertIn("七月藥品 70", expanded_dates)
        dates = self.run_book(
            "detail", "--date", "2026-07-09", "--date", "2026-07-11"
        )
        self.assertIn("09:00 外食費 (七月早餐) 60", dates)
        self.assertIn("18:30 保健費 (七月藥品) 70", dates)
        months = self.run_book(
            "detail", "--month", "2026-06", "--month", "2026-07"
        )
        self.assertIn("2026-06-30", months)
        self.assertIn("2026-07-11", months)
        self.assertEqual(
            self.run_book(
                "detail", "--month", "2026-07", "--date", "2026-07-09"
            ),
            "錯誤：明細查詢請選日期或月份，不要混用",
        )

    def test_report_headers_are_followed_immediately_by_content(self):
        records = [
            ("2026-07-01 08:00", "外食費", "早餐", "50"),
            ("2026-07-02 10:00", "保健費", "藥品", "70"),
        ]
        for date, category, note, amount in records:
            self.run_book(
                "out", "--category", category, "--note", note, "--amount", amount,
                "--date", date,
            )

        outputs = [
            self.run_book("report", "--month", "2026-07"),
            self.run_book("report", "--date", "2026-07-01"),
            self.run_book("category-detail", "--month", "2026-07"),
            self.run_book("category-detail", "--date", "2026-07-01"),
            self.run_book(
                "expand", "--category", "外食費", "--category", "保健費",
                "--month", "2026-07",
            ),
            self.run_book(
                "expand", "--category", "外食費", "--date", "2026-07-01",
            ),
            self.run_book("detail", "--month", "2026-07"),
            self.run_book("detail", "--date", "2026-07-01"),
            self.run_book("category-detail", "--month", "2026-08"),
            self.run_book("detail", "--month", "2026-08"),
        ]
        for output in outputs:
            lines = output.splitlines()
            for index, line in enumerate(lines[:-1]):
                if line and set(line) == {"="}:
                    self.assertNotEqual(
                        lines[index + 1], "", f"標題分隔線後不應空行：\n{output}"
                    )
            for index, line in enumerate(lines):
                if line.startswith("合計"):
                    self.assertGreater(index, 0)
                    self.assertEqual(
                        lines[index - 1], "-------", f"合計前應使用分隔線：\n{output}"
                    )
            self.assertNotIn("\n\n\n", output)

        category_detail = outputs[2]
        self.assertIn("=======\n外食費", category_detail)
        self.assertIn("早餐 50\n\n保健費", category_detail)
        detail = outputs[6]
        self.assertIn("=======\n2026-07-01", detail)
        self.assertIn("(早餐) 50\n\n2026-07-02", detail)
        self.assertEqual(outputs[8], "2026-08\n=======\n-------\n合計 0")
        self.assertEqual(outputs[9], "2026-08\n=======\n-------\n合計 0")

    def test_report_command_names_match_user_terms_without_timeline_alias(self):
        help_result = subprocess.run(
            [sys.executable, str(BOOK), "--help"],
            cwd=ROOT,
            env=self.book_env(),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("category-detail", help_result.stdout)
        self.assertIn("detail", help_result.stdout)
        self.assertNotIn("timeline", help_result.stdout)

        old_command = subprocess.run(
            [sys.executable, str(BOOK), "timeline", "--month", "2026-07"],
            cwd=ROOT,
            env=self.book_env(),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(old_command.returncode, 0)
        self.assertIn("invalid choice", old_command.stderr)

    def test_invalid_values_do_not_change_data(self):
        self.assertEqual(
            self.run_book(
                "out", "--category", "外食費", "--note", "錯誤支出",
                "--amount", "-10",
            ),
            "錯誤：金額必須是正整數（收到「-10」）",
        )
        self.assertEqual(
            self.run_book(
                "in", "--note", "錯誤入金", "--amount", "0"
            ),
            "錯誤：金額必須是正整數（收到「0」）",
        )
        self.assertEqual(
            self.run_book(
                "out", "--category", "外食費", "--note", "壞日期", "--amount", "10",
                "--date", "2026-02-30 12:00",
            ),
            "錯誤：日期時間必須是 YYYY-MM-DD 或 YYYY-MM-DD HH:MM（收到「2026-02-30 12:00」）",
        )
        self.assertEqual(
            self.run_book("report", "--month", "2026-13"),
            "錯誤：月份必須是 YYYY-MM（收到「2026-13」）",
        )
        self.assertEqual(
            self.run_book("archive", "--before", "不是日期"),
            "錯誤：日期時間必須是 YYYY-MM-DD 或 YYYY-MM-DD HH:MM（收到「不是日期」）",
        )
        self.assertEqual(
            self.run_book(
                "out", "--category", "買菜金", "--note", "買菜", "--balance", "10",
                "--date", "2026-07-10 09:54",
            ),
            "倒推不支援指定日期或時間。請告訴我這次實際花了多少錢，或移除日期時間後再用目前餘額倒推。",
        )
        self.assertEqual(self.run_book("balance"), "餘額 0")
        self.assertEqual(list(self.db.parent.glob("backup_*.csv")), [])

    def test_old_database_migration_keeps_defaults_and_legacy_category(self):
        with sqlite3.connect(self.db) as con:
            con.execute(
                "CREATE TABLE entries("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, dt TEXT NOT NULL, "
                "kind TEXT NOT NULL, category TEXT, note TEXT NOT NULL, "
                "amount INTEGER NOT NULL)"
            )
            con.execute(
                "INSERT INTO entries(dt,kind,category,note,amount) "
                "VALUES('2026-01-01 00:00:00','out','舊自訂','舊帳',10)"
            )
        con.close()
        names = self.run_book("categories").splitlines()
        self.assertEqual(names[:16], DEFAULT_CATEGORIES)
        self.assertEqual(names[16:], ["舊自訂"])
        self.assertEqual(self.run_book("categories").splitlines(), names)

    def test_partial_migration_with_empty_categories_repairs_orphans(self):
        with sqlite3.connect(self.db) as con:
            con.execute(
                "CREATE TABLE entries("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, dt TEXT NOT NULL, "
                "kind TEXT NOT NULL, category TEXT, note TEXT NOT NULL, "
                "amount INTEGER NOT NULL)"
            )
            con.execute(
                "CREATE TABLE categories("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, "
                "position INTEGER NOT NULL)"
            )
            con.execute(
                "INSERT INTO entries(dt,kind,category,note,amount) "
                "VALUES('2026-01-01 00:00:00','out','中斷遷移科目','舊帳',10)"
            )
        con.close()
        names = self.run_book("categories").splitlines()
        self.assertEqual(names[:16], DEFAULT_CATEGORIES)
        self.assertEqual(names[16:], ["中斷遷移科目"])
        with sqlite3.connect(self.db) as con:
            orphan_count = con.execute(
                "SELECT COUNT(*) FROM entries e LEFT JOIN categories c "
                "ON c.name=e.category WHERE e.kind='out' AND c.name IS NULL"
            ).fetchone()[0]
        con.close()
        self.assertEqual(orphan_count, 0)

    def test_old_empty_category_list_without_meta_stays_empty(self):
        con = sqlite3.connect(self.db)
        try:
            con.execute(
                "CREATE TABLE entries("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, dt TEXT NOT NULL, "
                "kind TEXT NOT NULL, category TEXT, note TEXT NOT NULL, "
                "amount INTEGER NOT NULL)"
            )
            con.execute(
                "CREATE TABLE categories("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, "
                "position INTEGER NOT NULL)"
            )
            con.commit()
        finally:
            con.close()
        self.assertEqual(self.run_book("categories"), "目前沒有科目")
        self.assertEqual(self.run_book("categories"), "目前沒有科目")

    def test_report_uses_one_snapshot_during_concurrent_category_rename(self):
        self.run_book(
            "out", "--category", "外食費", "--note", "午餐", "--amount", "100",
            "--date", "2026-07-01 12:00",
        )
        con = sqlite3.connect(self.db)
        try:
            mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(mode.lower(), "wal")
        spec = importlib.util.spec_from_file_location("book_snapshot_test", BOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.DB_PATH = self.db

        reached_categories = threading.Event()
        allow_categories = threading.Event()
        real_categories = module.categories

        def paused_categories(con):
            reached_categories.set()
            if not allow_categories.wait(5):
                raise TimeoutError("測試等待科目查詢逾時")
            return real_categories(con)

        module.categories = paused_categories
        report_result = {}

        def run_report():
            con = module.connect()
            output = io.StringIO()
            try:
                with redirect_stdout(output):
                    module.cmd_report(
                        con, SimpleNamespace(month=["2026-07"], date=None)
                    )
                report_result["output"] = output.getvalue()
            except BaseException as exc:
                report_result["error"] = exc
            finally:
                con.close()

        reporter = threading.Thread(target=run_report)
        reporter.start()
        self.assertTrue(reached_categories.wait(5), "報表未進入第二次查詢")
        writer = self.popen_book(
            "category-rename", "--from", "外食費", "--to", "餐飲"
        )
        try:
            stdout, stderr = writer.communicate(timeout=5)
            self.assertEqual(writer.returncode, 0, stderr)
            self.assertEqual(stdout.rstrip("\n"), "科目 外食費 -> 餐飲")
            fresh = sqlite3.connect(self.db)
            try:
                names = [row[0] for row in fresh.execute(
                    "SELECT name FROM categories ORDER BY position,id"
                ).fetchall()]
            finally:
                fresh.close()
            self.assertIn("餐飲", names)
            self.assertNotIn("外食費", names)
        finally:
            allow_categories.set()
        reporter.join(5)
        self.assertFalse(reporter.is_alive(), "報表執行逾時")
        self.assertNotIn("error", report_result)
        self.assertIn("外食費 100", report_result["output"])
        self.assertIn("合計   100", report_result["output"])

    def test_archive_preserves_balance_and_categories(self):
        self.run_book(
            "in", "--note", "期初", "--amount", "1000", "--date", "2025-01-01"
        )
        self.run_book(
            "out", "--category", "外食費", "--note", "早餐", "--amount", "60",
            "--date", "2025-01-02 08:00",
        )
        before = self.run_book("balance")
        output = self.run_book("archive", "--before", "2026-01-01")
        self.assertIn("已備份 2 筆到", output)
        self.assertTrue(output.endswith("清空後餘額 940"))
        self.assertEqual(self.run_book("balance"), before)
        self.assertEqual(self.run_book("categories").splitlines(), DEFAULT_CATEGORIES)
        backups = list(self.db.parent.glob("backup_*.csv"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].read_bytes().startswith(b"\xef\xbb\xbf"))
        with open(backups[0], encoding="utf-8-sig", newline="") as backup_file:
            rows = list(csv.reader(backup_file))
        self.assertEqual(
            rows,
            [
                ["日期", "時間", "科目", "品項", "金額", "餘額"],
                ["2025-01-01", "00:00", "入金", "期初", "1000", "1000"],
                ["2025-01-02", "08:00", "外食費", "早餐", "60", "940"],
            ],
        )

    def test_two_archives_never_overwrite_and_concurrent_archive_is_single(self):
        self.run_book(
            "in", "--note", "舊期初", "--amount", "1000", "--date", "2025-01-01"
        )
        self.run_book(
            "out", "--category", "外食費", "--note", "舊早餐", "--amount", "100",
            "--date", "2025-01-02 08:00",
        )
        p1 = self.popen_book("archive", "--before", "2026-01-01")
        p2 = self.popen_book("archive", "--before", "2026-01-01")
        out1, err1 = p1.communicate(timeout=30)
        out2, err2 = p2.communicate(timeout=30)
        self.assertEqual((p1.returncode, p2.returncode), (0, 0), (err1, err2))
        self.assertEqual(err1 + err2, "")
        outputs = [out1.strip(), out2.strip()]
        self.assertEqual(sum(text.startswith("已備份 2 筆到") for text in outputs), 1)
        self.assertEqual(sum(text == "沒有 2026-01-01 之前的資料可備份" for text in outputs), 1)
        self.assertEqual(self.run_book("balance"), "餘額 900")
        first_backups = list(self.db.parent.glob("backup_*.csv"))
        self.assertEqual(len(first_backups), 1)

        self.run_book(
            "out", "--category", "外食費", "--note", "新早餐", "--amount", "50",
            "--date", "2026-02-01 08:00",
        )
        self.run_book("archive", "--before", "2027-01-01")
        backups = list(self.db.parent.glob("backup_*.csv"))
        self.assertEqual(len(backups), 2)
        self.assertEqual(len({path.name for path in backups}), 2)
        self.assertTrue(all(len(path.stem.split("_")[-1]) == 6 for path in backups))
        self.assertEqual(self.run_book("balance"), "餘額 850")

    def test_concurrent_out_and_category_delete_cannot_create_orphan(self):
        self.run_book("category-replace", "--name", "餐飲")
        p1 = self.popen_book(
            "out", "--category", "餐飲", "--note", "午餐", "--amount", "100"
        )
        p2 = self.popen_book("category-delete", "--name", "餐飲")
        out1, err1 = p1.communicate(timeout=30)
        out2, err2 = p2.communicate(timeout=30)
        self.assertEqual((p1.returncode, p2.returncode), (0, 0), (err1, err2))
        self.assertEqual(err1 + err2, "")
        with sqlite3.connect(self.db) as con:
            orphan_count = con.execute(
                "SELECT COUNT(*) FROM entries e LEFT JOIN categories c "
                "ON c.name=e.category WHERE e.kind='out' AND c.name IS NULL"
            ).fetchone()[0]
            out_count = con.execute(
                "SELECT COUNT(*) FROM entries WHERE kind='out'"
            ).fetchone()[0]
            category_count = con.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        con.close()
        self.assertEqual(orphan_count, 0)
        self.assertIn((out_count, category_count), {(1, 1), (0, 0)})
        if out_count:
            self.assertIn("餘額 -100", out1)
            self.assertIn("無法刪除科目", out2)
        else:
            self.assertIn("錯誤：沒有「餐飲」這個科目", out1)
            self.assertEqual(out2.strip(), "刪除科目 餐飲")

    def test_concurrent_edit_and_delete_never_report_a_stale_update(self):
        self.run_book(
            "in", "--note", "期初", "--amount", "500", "--date", "2026-07-01"
        )
        self.run_book(
            "out", "--category", "外食費", "--note", "午餐", "--amount", "100"
        )
        edit = self.popen_book("edit", "--find", "午餐", "--to", "110")
        delete = self.popen_book("delete", "--find", "午餐")
        edit_out, edit_err = edit.communicate(timeout=30)
        delete_out, delete_err = delete.communicate(timeout=30)
        self.assertEqual((edit.returncode, delete.returncode), (0, 0))
        self.assertEqual(edit_err + delete_err, "")
        if "100 -> 110" in edit_out:
            self.assertIn("(午餐) 110", delete_out)
        else:
            self.assertIn("找不到符合「午餐」", edit_out)
            self.assertIn("(午餐) 100", delete_out)
        with sqlite3.connect(self.db) as con:
            count = con.execute(
                "SELECT COUNT(*) FROM entries WHERE note='午餐'"
            ).fetchone()[0]
        con.close()
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
