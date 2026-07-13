#!/usr/bin/env python3
# 記帳 skill 核心程式
# 本次功能與弱模型路由改善：GPT-5.0
#
# 設計原則：每個子指令都直接印出「可原樣顯示」的結果，
# AI 模型只需判斷該呼叫哪個子指令、填哪些參數，不必再自行組字。

import argparse
import csv
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

# Windows 主控台預設 cp950，強制 UTF-8 才能正確輸出中文
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 全新帳本開箱即用的預設科目（順序也是初始報表順序）
DEFAULT_CATEGORIES = [
    "外食費", "買菜金", "民俗節日", "居住費", "管理費", "交通費",
    "車稅險", "保健費", "治裝費", "精進金", "旅遊金", "公關費",
    "孝親費", "奉獻", "歸墊", "雜費",
]

DB_PATH = Path(os.environ.get(
    "BOOK_DB",
    Path(__file__).resolve().parent.parent / "data" / "book.db",
))

SEP = "-------"  # 合計前的分隔線


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    had_categories = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='categories'"
    ).fetchone() is not None
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS entries(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dt TEXT NOT NULL,        -- 'YYYY-MM-DD HH:MM:SS'
                kind TEXT NOT NULL,      -- in=入金 out=支出 adj=校正
                category TEXT,           -- out 用科目名；其餘為 NULL
                note TEXT NOT NULL,      -- 品項/說明
                amount INTEGER NOT NULL) -- out/in 為正；adj 可為負"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS categories(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                position INTEGER NOT NULL)"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS bookkeeping_meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL)"""
        )

        initialized = con.execute(
            "SELECT 1 FROM bookkeeping_meta WHERE key='categories_initialized'"
        ).fetchone() is not None
        category_count = int(con.execute(
            "SELECT COUNT(*) FROM categories"
        ).fetchone()[0])
        if not initialized:
            # 全新 DB 種入原本 16 科。舊版若已有空的 categories 且沒有
            # 支出，視為使用者刻意清空；若仍有支出則視為中斷遷移並修復。
            has_out_entries = con.execute(
                "SELECT 1 FROM entries WHERE kind='out' LIMIT 1"
            ).fetchone() is not None
            if not had_categories or (category_count == 0 and has_out_entries):
                con.executemany(
                    "INSERT OR IGNORE INTO categories(name,position) VALUES(?,?)",
                    [(name, index) for index, name in enumerate(DEFAULT_CATEGORIES)],
                )
            con.execute(
                "INSERT INTO bookkeeping_meta(key,value) VALUES('categories_initialized','1')"
            )

        # 修復舊版或中斷遷移留下的孤兒科目，絕不遺失既有支出。
        next_position = int(con.execute(
            "SELECT COALESCE(MAX(position),-1)+1 FROM categories"
        ).fetchone()[0])
        orphan_names = [row[0] for row in con.execute(
            "SELECT DISTINCT e.category FROM entries e "
            "LEFT JOIN categories c ON c.name=e.category "
            "WHERE e.kind='out' AND e.category IS NOT NULL AND c.name IS NULL "
            "ORDER BY e.id"
        ).fetchall()]
        con.executemany(
            "INSERT INTO categories(name,position) VALUES(?,?)",
            [(name, next_position + index) for index, name in enumerate(orphan_names)],
        )

        con.execute(
            """CREATE TRIGGER IF NOT EXISTS entries_require_category_insert
            BEFORE INSERT ON entries
            WHEN NEW.kind='out' AND (
                NEW.category IS NULL OR
                NOT EXISTS(SELECT 1 FROM categories WHERE name=NEW.category)
            )
            BEGIN
                SELECT RAISE(ABORT, '支出科目不存在');
            END"""
        )
        con.execute(
            """CREATE TRIGGER IF NOT EXISTS entries_require_category_update
            BEFORE UPDATE OF kind,category ON entries
            WHEN NEW.kind='out' AND (
                NEW.category IS NULL OR
                NOT EXISTS(SELECT 1 FROM categories WHERE name=NEW.category)
            )
            BEGIN
                SELECT RAISE(ABORT, '支出科目不存在');
            END"""
        )
        con.execute(
            """CREATE TRIGGER IF NOT EXISTS categories_restrict_delete
            BEFORE DELETE ON categories
            WHEN EXISTS(
                SELECT 1 FROM entries WHERE kind='out' AND category=OLD.name
            )
            BEGIN
                SELECT RAISE(ABORT, '科目仍有帳目使用');
            END"""
        )
        con.execute(
            """CREATE TRIGGER IF NOT EXISTS categories_restrict_name_update
            BEFORE UPDATE OF name ON categories
            WHEN NEW.name<>OLD.name AND EXISTS(
                SELECT 1 FROM entries WHERE kind='out' AND category=OLD.name
            )
            BEGIN
                SELECT RAISE(ABORT, '請同步更名帳目');
            END"""
        )
        con.commit()
    except Exception:
        con.rollback()
        con.close()
        raise
    return con


@contextmanager
def write_transaction(con):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
        con.commit()
    except BaseException:
        con.rollback()
        raise


@contextmanager
def read_transaction(con):
    """讓一個多查詢報表全程看到同一份 SQLite 快照。"""
    con.execute("BEGIN")
    try:
        yield
    finally:
        con.rollback()


def categories(con):
    return [row[0] for row in con.execute(
        "SELECT name FROM categories ORDER BY position,id"
    ).fetchall()]


def clean_category_name(value):
    name = str(value).strip()
    if not name:
        print("錯誤：科目名稱不可為空")
        sys.exit(0)
    return name


def balance(con):
    # out 扣錢，其餘（in、adj）加錢
    row = con.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind='out' THEN -amount ELSE amount END),0) FROM entries"
    ).fetchone()
    return int(row[0])


def today():
    return datetime.now().strftime("%Y-%m-%d")


def normalize_date(value):
    raw = str(value).strip()
    for fmt, output in (
        ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M"),
        ("%Y-%m-%d", "%Y-%m-%d"),
    ):
        try:
            return datetime.strptime(raw, fmt).strftime(output)
        except ValueError:
            pass
    print(f"錯誤：日期時間必須是 YYYY-MM-DD 或 YYYY-MM-DD HH:MM（收到「{value}」）")
    sys.exit(0)


def build_dt(date):
    # 指定日期可精確到 HH:MM；只有日期時補 00:00。
    if date:
        normalized = normalize_date(date)
        return normalized + (":00" if len(normalized) == 16 else " 00:00:00")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def input_prefix(dt, date):
    # 沒指定日期時間時維持簡潔；有指定就照精度顯示。
    if not date:
        return ""
    normalized = normalize_date(date)
    return (dt[:16] if len(normalized) == 16 else dt[:10]) + " "


def matched_prefix(dt, date, force_time=False):
    # 修改／刪除只有在使用者指定日期時顯示日期；保留原紀錄的 HH:MM。
    if not date and not force_time:
        return ""
    explicit_time = bool(date) and len(str(date).strip()) == 16
    return (
        dt[:16] if force_time or explicit_time or dt[11:16] != "00:00" else dt[:10]
    ) + " "


def label_of(kind, category):
    if kind == "in":
        return "入金"
    if kind == "adj":
        return "校正"
    return category


def parse_int(s, name):
    try:
        return int(str(s).replace(",", "").strip())
    except ValueError:
        print(f"錯誤：{name}必須是整數（收到「{s}」）")
        sys.exit(0)


def parse_positive(s, name):
    value = parse_int(s, name)
    if value <= 0:
        print(f"錯誤：{name}必須是正整數（收到「{s}」）")
        sys.exit(0)
    return value


def month_range(month):
    # '2026-07' -> ('2026-07-01', '2026-08-01')；字串比較即可涵蓋整月
    try:
        normalized = datetime.strptime(str(month).strip(), "%Y-%m").strftime("%Y-%m")
    except ValueError:
        print(f"錯誤：月份必須是 YYYY-MM（收到「{month}」）")
        sys.exit(0)
    y, m = (int(x) for x in normalized.split("-"))
    start = f"{y:04d}-{m:02d}-01"
    end = f"{y+1:04d}-01-01" if m == 12 else f"{y:04d}-{m+1:02d}-01"
    return start, end


def selected_months(values):
    raw = values or [datetime.now().strftime("%Y-%m")]
    result = []
    for value in raw:
        start, _ = month_range(value)
        month = start[:7]
        if month not in result:
            result.append(month)
    return result


def selected_periods(months, dates, label):
    if months and dates:
        print(f"錯誤：{label}查詢請選日期或月份，不要混用")
        return None
    if dates:
        result = []
        seen = set()
        for raw_date in dates:
            date = normalize_date(raw_date)
            if len(date) != 10:
                print(f"錯誤：{label}的日期必須是 YYYY-MM-DD")
                return None
            if date in seen:
                continue
            seen.add(date)
            end = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime(
                "%Y-%m-%d"
            )
            result.append((date, date, end))
        return result
    return [
        (month, *month_range(month)) for month in selected_months(months)
    ]


def dwidth(s):
    # 顯示寬度：中日文字元算 2 欄，其餘算 1 欄
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in str(s))


def rpad(s, width):
    return str(s) + " " * max(0, width - dwidth(s))


def clean_find_terms(values):
    terms = []
    for value in values:
        term = str(value).strip()
        if not term:
            print("錯誤：查找文字不可為空")
            sys.exit(0)
        if term not in terms:
            terms.append(term)
    return terms


def find_entries(con, finds, date, amount):
    terms = clean_find_terms(finds)
    alternatives = " OR ".join("instr(note, ?) > 0" for _ in terms)
    q = (
        "SELECT id,dt,kind,category,note,amount FROM entries "
        f"WHERE ({alternatives})"
    )
    params = list(terms)
    if date:
        normalized = normalize_date(date)
        length = 16 if len(normalized) == 16 else 10
        q += f" AND substr(dt,1,{length})=?"
        params.append(normalized)
    if amount is not None:
        q += " AND amount=?"
        params.append(parse_positive(amount, "金額"))
    q += " ORDER BY dt DESC, id DESC"
    return con.execute(q, params).fetchall()


def rowid_entry(con, rowid):
    eid = parse_positive(rowid, "rowid")
    return con.execute(
        "SELECT id,dt,kind,category,note,amount FROM entries WHERE id=?", (eid,)
    ).fetchone()


def selected_rows(con, a, action):
    has_finds = bool(a.find)
    has_rowid = a.rowid is not None
    if has_finds == has_rowid:
        print("錯誤：--find（可重複）與 --rowid 請擇一提供")
        return None
    if has_rowid:
        row = rowid_entry(con, a.rowid)
        if not row:
            print(f"找不到 rowid {a.rowid} 的紀錄，請確認")
            return None
        return [row]
    rows = find_entries(con, a.find, a.date, getattr(a, "amount", None))
    terms = "、".join(clean_find_terms(a.find))
    if not rows:
        print(f"找不到符合「{terms}」的紀錄，請確認")
        return None
    if len(rows) > 1:
        verb = "修改" if action == "edit" else "刪除"
        out = [f"找到 {len(rows)} 筆符合「{terms}」的紀錄，未{verb}任何帳目："]
        for eid, dt, kind, category, note, amount in rows:
            out.append(
                f"rowid {eid} | {dt[:16]} | {label_of(kind, category)} | {note} | {int(amount)}"
            )
        out.append(f"請依上述內容選定 rowid，再執行{verb}。")
        print("\n".join(out))
        return None
    return rows


# ---- 記帳 ----

def cmd_in(con, a):
    amount = parse_positive(a.amount, "金額")
    dt = build_dt(a.date)
    with write_transaction(con):
        con.execute("INSERT INTO entries(dt,kind,category,note,amount) VALUES(?,?,?,?,?)",
                    (dt, "in", None, a.note, amount))
        current = balance(con)
    print(f"{input_prefix(dt, a.date)}入金 ({a.note}) {amount} 餘額 {current}")


def cmd_out(con, a):
    has_amount = a.amount is not None
    has_balance = a.balance is not None
    if has_amount == has_balance:
        print("錯誤：--amount（花費金額）與 --balance（剩餘餘額）請擇一提供")
        return
    if has_balance and a.date:
        print(
            "倒推不支援指定日期或時間。請告訴我這次實際花了多少錢，"
            "或移除日期時間後再用目前餘額倒推。"
        )
        return
    dt = build_dt(a.date)
    with write_transaction(con):
        if a.category not in categories(con):
            print(f"錯誤：沒有「{a.category}」這個科目，請確認科目名稱")
            return
        if has_balance:
            target = parse_int(a.balance, "餘額")
            current = balance(con)
            amount = current - target
            if amount <= 0:
                print(f"錯誤：目前餘額 {current}，剩餘 {target} 無法倒推出花費，請確認")
                return
        else:
            amount = parse_positive(a.amount, "金額")
        con.execute("INSERT INTO entries(dt,kind,category,note,amount) VALUES(?,?,?,?,?)",
                    (dt, "out", a.category, a.note, amount))
        current = balance(con)
    print(f"{input_prefix(dt, a.date)}{a.category} ({a.note}) {amount} 餘額 {current}")


def cmd_edit(con, a):
    with write_transaction(con):
        rows = selected_rows(con, a, "edit")
        if not rows:
            return
        row = rows[0]
        eid, dt, kind, category, note, old = row
        b0 = balance(con)
        new = parse_int(a.to, "金額")
        if kind in ("in", "out") and new <= 0:
            print(f"錯誤：金額必須是正整數（收到「{a.to}」）")
            return
        changed = con.execute(
            "UPDATE entries SET amount=? WHERE id=?", (new, eid)
        ).rowcount
        if changed != 1:
            print(f"找不到 rowid {eid} 的紀錄，請確認")
            return
        b1 = balance(con)
    print(f"{matched_prefix(dt, a.date, bool(a.rowid))}{label_of(kind, category)} ({note}) {old} -> {new} 餘額 {b0} -> {b1}")


def cmd_delete(con, a):
    with write_transaction(con):
        rows = selected_rows(con, a, "delete")
        if not rows:
            return
        row = rows[0]
        eid, dt, kind, category, note, amount = row
        b0 = balance(con)
        changed = con.execute("DELETE FROM entries WHERE id=?", (eid,)).rowcount
        if changed != 1:
            print(f"找不到 rowid {eid} 的紀錄，請確認")
            return
        b1 = balance(con)
    print(f"{matched_prefix(dt, a.date, bool(a.rowid))}刪除 {label_of(kind, category)} ({note}) {amount} 餘額 {b0} -> {b1}")


# ---- 餘額 ----

def cmd_balance(con, a):
    print(f"餘額 {balance(con)}")


def cmd_adjust(con, a):
    # 絕對校正：直接把餘額平移到目標值，不動已記帳的相對金額
    target = parse_int(a.balance, "餘額")
    with write_transaction(con):
        cur = balance(con)
        diff = target - cur
        con.execute("INSERT INTO entries(dt,kind,category,note,amount) VALUES(?,?,?,?,?)",
                    (build_dt(None), "adj", None, "餘額校正", diff))
    print(f"餘額校正 {cur} -> {target}")


# ---- 報表 ----

def cmd_categories(con, a):
    names = categories(con)
    print("\n".join(names) if names else "目前沒有科目")


def cmd_category_add(con, a):
    name = clean_category_name(a.name)
    with write_transaction(con):
        if name in categories(con):
            print(f"錯誤：科目「{name}」已存在")
            return
        position = con.execute(
            "SELECT COALESCE(MAX(position),-1)+1 FROM categories"
        ).fetchone()[0]
        con.execute(
            "INSERT INTO categories(name,position) VALUES(?,?)", (name, position)
        )
    print(f"新增科目 {name}")


def cmd_category_delete(con, a):
    name = clean_category_name(a.name)
    with write_transaction(con):
        if name not in categories(con):
            print(f"錯誤：找不到科目「{name}」")
            return
        used = int(con.execute(
            "SELECT COUNT(*) FROM entries WHERE kind='out' AND category=?", (name,)
        ).fetchone()[0])
        if used:
            print(f"無法刪除科目「{name}」：仍有 {used} 筆帳目使用此科目，請先刪除這些帳目")
            return
        con.execute("DELETE FROM categories WHERE name=?", (name,))
    print(f"刪除科目 {name}")


def cmd_category_clear(con, a):
    with write_transaction(con):
        used = int(con.execute(
            "SELECT COUNT(*) FROM entries WHERE kind='out' AND category IS NOT NULL"
        ).fetchone()[0])
        if used:
            print(f"無法刪除全部科目：仍有 {used} 筆帳目使用科目，請先刪除這些帳目")
            return
        con.execute("DELETE FROM categories")
    print("已刪除全部科目")


def cmd_category_replace(con, a):
    names = [clean_category_name(name) for name in a.name]
    duplicate = next((name for name in names if names.count(name) > 1), None)
    if duplicate:
        print(f"錯誤：科目「{duplicate}」重複，未變更任何科目")
        return
    with write_transaction(con):
        used = [row[0] for row in con.execute(
            "SELECT DISTINCT category FROM entries "
            "WHERE kind='out' AND category IS NOT NULL ORDER BY category"
        ).fetchall() if row[0] not in names]
        if used:
            joined = "、".join(used)
            print(f"無法替換科目：帳目仍在使用「{joined}」，請先刪除這些帳目")
            return
        for position, name in enumerate(names):
            con.execute(
                "INSERT OR IGNORE INTO categories(name,position) VALUES(?,?)",
                (name, position),
            )
            con.execute(
                "UPDATE categories SET position=? WHERE name=?", (position, name)
            )
        placeholders = ",".join("?" for _ in names)
        con.execute(
            f"DELETE FROM categories WHERE name NOT IN ({placeholders})", names
        )
    print(f"已替換科目（共 {len(names)} 個）")


def cmd_category_rename(con, a):
    old = clean_category_name(a.old)
    new = clean_category_name(a.new)
    with write_transaction(con):
        names = categories(con)
        if old not in names:
            print(f"錯誤：找不到科目「{old}」")
            return
        if new != old and new in names:
            print(f"錯誤：科目「{new}」已存在")
            return
        if new != old:
            # 先建立新科目，再搬帳、刪舊科目，讓完整性觸發器全程成立。
            position = con.execute(
                "SELECT position FROM categories WHERE name=?", (old,)
            ).fetchone()[0]
            con.execute(
                "INSERT INTO categories(name,position) VALUES(?,?)", (new, position)
            )
            con.execute(
                "UPDATE entries SET category=? WHERE kind='out' AND category=?",
                (new, old),
            )
            con.execute("DELETE FROM categories WHERE name=?", (old,))
    print(f"科目 {old} -> {new}")


def _report_block(con, title, start, end):
    totals = dict(con.execute(
        "SELECT category, SUM(amount) FROM entries "
        "WHERE kind='out' AND dt>=? AND dt<? GROUP BY category",
        (start, end)).fetchall())
    names = [c for c in categories(con) if totals.get(c)]
    grand = sum(int(totals[n]) for n in names)
    namew = max([dwidth(n) for n in names] + [dwidth("合計")])
    amtw = max([len(str(int(totals[n]))) for n in names] + [len(str(grand))])
    out = [title, "=" * dwidth(title)]
    for n in names:
        out.append(f"{rpad(n, namew)} {str(int(totals[n])).rjust(amtw)}")
    out.append(SEP)
    out.append(f"{rpad('合計', namew)} {str(grand).rjust(amtw)}")
    return "\n".join(out)


def cmd_report(con, a):
    periods = selected_periods(a.month, a.date, "分帳")
    if periods is None:
        return
    with read_transaction(con):
        output = "\n\n".join(
            _report_block(con, title, start, end) for title, start, end in periods
        )
    print(output)


def _cat_lines(con, category, start, end):
    rows = con.execute(
        "SELECT dt, note, amount FROM entries "
        "WHERE kind='out' AND category=? AND dt>=? AND dt<? ORDER BY dt, id",
        (category, start, end)).fetchall()
    lines = [f"{dt[:16]} {note} {int(amount)}" for dt, note, amount in rows]
    total = sum(int(a) for _, _, a in rows)
    return lines, total


def _by_category(con, cats, title, start, end):
    # 依科目分段輸出（category-detail 與 expand 共用）
    out = [title, "=" * dwidth(title)]
    grand = 0
    for index, c in enumerate(cats):
        lines, total = _cat_lines(con, c, start, end)
        grand += total
        if index:
            out.append("")
        out.append(c)
        out.extend(lines)
    out.append(SEP)
    out.append(f"合計 {grand}")
    return "\n".join(out)


def cmd_category_detail(con, a):
    periods = selected_periods(a.month, a.date, "分帳明細")
    if periods is None:
        return
    blocks = []
    with read_transaction(con):
        for title, start, end in periods:
            # 只列出該期間有支出的科目
            have = {r[0] for r in con.execute(
                "SELECT DISTINCT category FROM entries WHERE kind='out' AND dt>=? AND dt<?",
                (start, end)).fetchall()}
            blocks.append(_by_category(
                con, [c for c in categories(con) if c in have], title, start, end
            ))
    print("\n\n".join(blocks))


def cmd_expand(con, a):
    periods = selected_periods(a.month, a.date, "展開")
    if periods is None:
        return
    with read_transaction(con):
        names = categories(con)
        for c in a.category:
            if c not in names:
                print(f"錯誤：沒有「{c}」這個科目，請確認科目名稱")
                return
        blocks = [
            _by_category(con, a.category, title, start, end)
            for title, start, end in periods
        ]
    print("\n\n".join(blocks))  # 保留使用者指定的科目與月份順序


def _detail_date(con, raw_date):
    date = normalize_date(raw_date)
    if len(date) != 10:
        print("錯誤：單日明細的日期必須是 YYYY-MM-DD")
        sys.exit(0)
    rows = con.execute(
        "SELECT dt, category, note, amount FROM entries "
        "WHERE kind='out' AND substr(dt,1,10)=? ORDER BY dt, id", (date,)).fetchall()
    out = [date, "=" * dwidth(date)]
    grand = 0
    for dt, cat, note, amount in rows:
        out.append(f"{dt[11:16]} {cat} ({note}) {int(amount)}")
        grand += int(amount)
    out.append(SEP)
    out.append(f"合計 {grand}")
    return "\n".join(out)


def _detail_month(con, month):
    # 整月依日期排列
    start, end = month_range(month)
    rows = con.execute(
        "SELECT dt, category, note, amount FROM entries "
        "WHERE kind='out' AND dt>=? AND dt<? ORDER BY dt, id", (start, end)).fetchall()
    out = [month, "=" * dwidth(month)]
    grand = 0
    cur_day = None
    for dt, cat, note, amount in rows:
        day = dt[:10]
        if day != cur_day:
            if cur_day is not None:
                out.append("")
            out.append(day)
            cur_day = day
        out.append(f"{dt[11:16]} {cat} ({note}) {int(amount)}")
        grand += int(amount)
    out.append(SEP)
    out.append(f"合計 {grand}")
    return "\n".join(out)


def cmd_detail(con, a):
    if a.date and a.month:
        print("錯誤：明細查詢請選日期或月份，不要混用")
        return
    if a.date:
        dates = []
        for raw_date in a.date:
            date = normalize_date(raw_date)
            if len(date) != 10:
                print("錯誤：單日明細的日期必須是 YYYY-MM-DD")
                return
            if date not in dates:
                dates.append(date)
        with read_transaction(con):
            output = "\n\n".join(_detail_date(con, date) for date in dates)
        print(output)
        return
    months = selected_months(a.month)
    with read_transaction(con):
        output = "\n\n".join(_detail_month(con, month) for month in months)
    print(output)


def cmd_archive(con, a):
    cutoff = normalize_date(a.before)
    if len(cutoff) != 10:
        print("錯誤：封存截止日必須是 YYYY-MM-DD")
        return
    csv_path = None
    try:
        # 從讀取、寫 CSV 到補期初餘額全程鎖住同一本帳，避免重複封存。
        with write_transaction(con):
            rows = con.execute(
                "SELECT dt, kind, category, note, amount FROM entries WHERE dt < ? "
                "ORDER BY dt, CASE WHEN kind='adj' AND note='期初餘額' THEN 0 ELSE 1 END, id",
                (cutoff,)).fetchall()
            if not rows:
                print(f"沒有 {cutoff} 之前的資料可備份")
                return
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            for suffix in range(1000):
                tail = "" if suffix == 0 else f"_{suffix}"
                candidate = DB_PATH.parent / f"backup_{stamp}{tail}.csv"
                try:
                    backup_file = open(
                        candidate, "x", encoding="utf-8-sig", newline=""
                    )
                    csv_path = candidate
                    break
                except FileExistsError:
                    continue
            else:
                raise RuntimeError("無法建立不重複的備份檔名")

            running = 0
            with backup_file:
                w = csv.writer(backup_file)
                w.writerow(["日期", "時間", "科目", "品項", "金額", "餘額"])
                for dt, kind, category, note, amount in rows:
                    running += (-amount if kind == "out" else amount)
                    w.writerow([
                        dt[:10], dt[11:16], label_of(kind, category), note,
                        amount, running,
                    ])
            carry = running  # 被搬走資料的淨額 = 應保留的期初餘額
            con.execute("DELETE FROM entries WHERE dt < ?", (cutoff,))
            if carry != 0:
                con.execute(
                    "INSERT INTO entries(dt,kind,category,note,amount) VALUES(?,?,?,?,?)",
                    (f"{cutoff} 00:00:00", "adj", None, "期初餘額", carry),
                )
            current = balance(con)
    except BaseException:
        # DB 交易若失敗，不留下看似成功但不對應資料庫狀態的 CSV。
        if csv_path is not None and csv_path.exists():
            csv_path.unlink()
        raise
    try:
        shown_path = csv_path.relative_to(Path(__file__).resolve().parent.parent).as_posix()
    except ValueError:
        shown_path = str(csv_path)
    print(f"已備份 {len(rows)} 筆到 {shown_path}，清空後餘額 {current}")


def main():
    p = argparse.ArgumentParser(description="記帳")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("categories")
    sub.add_parser("balance")             # 查目前餘額

    pca = sub.add_parser("category-add")  # 新增科目
    pca.add_argument("--name", required=True)

    pcd = sub.add_parser("category-delete")  # 刪除未使用的科目
    pcd.add_argument("--name", required=True)

    sub.add_parser("category-clear")       # 清空未使用的所有科目

    pcp = sub.add_parser("category-replace")  # 原子替換整份科目清單
    pcp.add_argument("--name", required=True, action="append")

    pcr = sub.add_parser("category-rename")  # 更名並同步歷史帳目
    pcr.add_argument("--from", dest="old", required=True)
    pcr.add_argument("--to", dest="new", required=True)

    pi = sub.add_parser("in")             # 錢變多
    pi.add_argument("--note", required=True)
    pi.add_argument("--amount", required=True)
    pi.add_argument("--date")

    po = sub.add_parser("out")            # 錢變少
    po.add_argument("--category", required=True)
    po.add_argument("--note", required=True)
    po.add_argument("--amount")           # 花費金額
    po.add_argument("--balance")          # 倒推：剩餘餘額
    po.add_argument("--date")

    pe = sub.add_parser("edit")           # 修改金額
    pe.add_argument("--find", action="append")  # 多個關鍵字為 OR
    pe.add_argument("--rowid")            # 多筆搜尋後的精確選擇
    pe.add_argument("--amount")           # 搜尋時可用原金額縮小範圍
    pe.add_argument("--to", required=True)
    pe.add_argument("--date")

    pd = sub.add_parser("delete")         # 刪除
    pd.add_argument("--find", action="append")  # 多個關鍵字為 OR
    pd.add_argument("--rowid")            # 多筆搜尋後的精確選擇
    pd.add_argument("--date")
    pd.add_argument("--amount")

    pj = sub.add_parser("adjust")         # 絕對校正餘額
    pj.add_argument("--balance", required=True)

    pr = sub.add_parser("report")         # 科目統計
    pr.add_argument("--month", action="append")
    pr.add_argument("--date", action="append")

    pcd = sub.add_parser("category-detail")  # 分帳明細（依科目）
    pcd.add_argument("--month", action="append")
    pcd.add_argument("--date", action="append")

    px = sub.add_parser("expand")         # 展開科目（可多個）
    px.add_argument("--category", required=True, action="append")
    px.add_argument("--month", action="append")
    px.add_argument("--date", action="append")

    pdt = sub.add_parser("detail")        # 明細（依日期排列）
    pdt.add_argument("--month", action="append")
    pdt.add_argument("--date", action="append")

    pa = sub.add_parser("archive")        # 舊資料截斷備份
    pa.add_argument("--before", required=True)

    a = p.parse_args()
    con = connect()
    {
        "categories": cmd_categories,
        "category-add": cmd_category_add,
        "category-delete": cmd_category_delete,
        "category-clear": cmd_category_clear,
        "category-replace": cmd_category_replace,
        "category-rename": cmd_category_rename,
        "balance": cmd_balance,
        "in": cmd_in,
        "out": cmd_out,
        "edit": cmd_edit,
        "delete": cmd_delete,
        "adjust": cmd_adjust,
        "report": cmd_report,
        "category-detail": cmd_category_detail,
        "expand": cmd_expand,
        "detail": cmd_detail,
        "archive": cmd_archive,
    }[a.cmd](con, a)
    con.close()


if __name__ == "__main__":
    main()
