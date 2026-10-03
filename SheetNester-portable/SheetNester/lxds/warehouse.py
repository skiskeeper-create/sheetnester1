# -*- coding: utf-8 -*-
"""Виртуальный склад материалов (ТЗ разд. 9). Локальная SQLite, только stdlib.

Учётные единицы (ТЗ 9.2): листы и остатки (обрезки). Движения (приход/списание/инвентаризация)
пишутся в movements — история для инвентаризации и отчёта (ТЗ 9.3). Потокобезопасность: один
коннект + lock (сервер многопоточный). Остаток в MVP — прямоугольник (геометрия контура — опция,
поле geometry зарезервировано, ТЗ 9.2).
"""
import csv
import io
import sqlite3
import threading
import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS sheets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  material TEXT NOT NULL, thickness REAL NOT NULL,
  length REAL NOT NULL, width REAL NOT NULL,
  qty INTEGER NOT NULL DEFAULT 1 CHECK(qty >= 0),
  price_sheet REAL DEFAULT 0, price_kg REAL DEFAULT 0,
  location TEXT DEFAULT '', received TEXT DEFAULT '', batch TEXT DEFAULT '', note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS remnants(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  material TEXT NOT NULL, thickness REAL NOT NULL,
  length REAL NOT NULL, width REAL NOT NULL,
  qty INTEGER NOT NULL DEFAULT 1 CHECK(qty >= 0),
  source TEXT DEFAULT '', created TEXT DEFAULT '', geometry TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS movements(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, kind TEXT NOT NULL, item TEXT NOT NULL, item_id INTEGER,
  delta INTEGER NOT NULL, reason TEXT DEFAULT '', balance INTEGER NOT NULL
);
"""

COLS_SHEET = ("material", "thickness", "length", "width", "qty", "price_sheet", "price_kg",
              "location", "received", "batch", "note")


class Warehouse:
    """Склад. path — файл SQLite (например приложение/data/warehouse.db)."""

    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def _ts(self):
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _move(self, kind, item, item_id, delta, reason, balance):
        self.db.execute("INSERT INTO movements(ts, kind, item, item_id, delta, reason, balance)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (self._ts(), kind, item, item_id, delta, reason, balance))

    # ---------------------------------------------------------------- листы
    def add_sheet(self, **kw):
        """Приход листа(ов). Обязательные: material, thickness, length, width; qty по умолчанию 1."""
        kw = {k: kw.get(k) for k in COLS_SHEET}
        if kw["qty"] is None:
            kw["qty"] = 1
        if not kw["received"]:
            kw["received"] = self._ts()[:10]
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO sheets(material, thickness, length, width, qty, price_sheet, price_kg,"
                " location, received, batch, note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (kw["material"], float(kw["thickness"]), float(kw["length"]), float(kw["width"]),
                 int(kw["qty"]), float(kw["price_sheet"] or 0), float(kw["price_kg"] or 0),
                 kw["location"] or "", kw["received"], kw["batch"] or "", kw["note"] or ""))
            self._move("приход", "лист", cur.lastrowid, int(kw["qty"]), kw["note"] or "", int(kw["qty"]))
            self.db.commit()
            return cur.lastrowid

    def list_sheets(self, material=None, thickness=None):
        q, args = "SELECT * FROM sheets WHERE qty >= 0", []
        if material:
            q += " AND material = ?"
            args.append(material)
        if thickness is not None:
            q += " AND thickness = ?"
            args.append(float(thickness))
        q += " ORDER BY material, thickness, length DESC"
        with self.lock:
            return [dict(r) for r in self.db.execute(q, args)]

    def list_remnants(self, material=None, thickness=None):
        q, args = "SELECT * FROM remnants WHERE qty > 0", []
        if material:
            q += " AND material = ?"
            args.append(material)
        if thickness is not None:
            q += " AND thickness = ?"
            args.append(float(thickness))
        q += " ORDER BY material, thickness, length DESC"
        with self.lock:
            return [dict(r) for r in self.db.execute(q, args)]

    def write_off(self, table, row_id, qty, reason=""):
        """Списание (таблица 'sheets'|'remnants'). Остаток не может уйти в минус. -> balance."""
        if table not in ("sheets", "remnants"):
            raise ValueError("table должен быть 'sheets' или 'remnants'")
        item = "лист" if table == "sheets" else "остаток"
        with self.lock:
            row = self.db.execute("SELECT qty FROM %s WHERE id=?" % table, (row_id,)).fetchone()
            if row is None:
                raise ValueError("позиция не найдена")
            if int(qty) <= 0:
                raise ValueError("количество должно быть > 0")
            if row["qty"] < int(qty):
                raise ValueError("недостаточно на складе: есть %d, запрошено %d" % (row["qty"], qty))
            bal = row["qty"] - int(qty)
            self.db.execute("UPDATE %s SET qty=? WHERE id=?" % table, (bal, row_id))
            self._move("списание", item, row_id, -int(qty), reason, bal)
            self.db.commit()
            return bal

    def adjust(self, table, row_id, new_qty, reason="инвентаризация"):
        """Инвентаризация: прямая корректировка остатка с записью движения (ТЗ 9.3)."""
        if table not in ("sheets", "remnants"):
            raise ValueError("table должен быть 'sheets' или 'remnants'")
        item = "лист" if table == "sheets" else "остаток"
        with self.lock:
            row = self.db.execute("SELECT qty FROM %s WHERE id=?" % table, (row_id,)).fetchone()
            if row is None:
                raise ValueError("позиция не найдена")
            new_qty = int(new_qty)
            self.db.execute("UPDATE %s SET qty=? WHERE id=?" % table, (new_qty, row_id))
            self._move("инвентаризация", item, row_id, new_qty - row["qty"], reason, new_qty)
            self.db.commit()
            return new_qty

    # ---------------------------------------------------------------- поиск и расход
    def available(self, material, thickness, min_l, min_w):
        """Сколько листов (не остатков) подходят по материалу/толщине и минимальному размеру.
        Лист со склада можно перевернуть: подходит, если (L>=min_l и W>=min_w) или (L>=min_w и W>=min_l)."""
        with self.lock:
            r = self.db.execute(
                "SELECT COALESCE(SUM(qty),0) s, COUNT(*) k FROM sheets"
                " WHERE material=? AND thickness=? AND qty>0 AND"
                " ((length>=? AND width>=?) OR (length>=? AND width>=?))",
                (material, float(thickness), float(min_l), float(min_w), float(min_w), float(min_l))).fetchone()
            return dict(qty=r["s"], positions=r["k"])

    def consume(self, material, thickness, count, sheet_l, sheet_w, used=None, source=""):
        """Авто-расход при раскладке (ТЗ 9.3/9.5): списать count подходящих листов (сначала старые
        по дате прихода; лист можно перевернуть — ориентация не важна), при used=(ext_w, ext_h) — габарит
        занятой области на листе — создать прямоугольный остаток из большей из двух полос (справа или сверху
        от занятой области; остаток записывается в ориентации раскройного листа). -> dict(spent, remnant_id).
        Если подходящих листов меньше count — списывается сколько есть (spent < requested)."""
        spent, remnant_id = 0, None
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM sheets WHERE material=? AND thickness=? AND qty>0 AND"
                " ((length>=? AND width>=?) OR (length>=? AND width>=?))"
                " ORDER BY received, id",
                (material, float(thickness), float(sheet_l), float(sheet_w),
                 float(sheet_w), float(sheet_l))).fetchall()
            need = int(count)
            for r in rows:
                if need <= 0:
                    break
                take = min(need, r["qty"])
                bal = r["qty"] - take
                self.db.execute("UPDATE sheets SET qty=? WHERE id=?", (bal, r["id"]))
                self._move("расход (раскладка)", "лист", r["id"], -take, source, bal)
                spent += take
                need -= take
            if used and spent:
                ew, eh = float(used[0]), float(used[1])
                right = (sheet_l - ew) * sheet_w       # полоса справа
                top = sheet_l * (sheet_w - eh)         # полоса сверху
                if right >= top and right > 0:
                    rl, rw = sheet_l - ew, sheet_w
                elif top > 0:
                    rl, rw = sheet_l, sheet_w - eh
                else:
                    rl = rw = 0
                if rl > 50 and rw > 50:                # мелочь не храним
                    cur = self.db.execute(
                        "INSERT INTO remnants(material, thickness, length, width, qty, source, created)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (material, float(thickness), rl, rw, spent, source or "раскладка", self._ts()[:10]))
                    self._move("остаток", "остаток", cur.lastrowid, spent, source, spent)
                    remnant_id = cur.lastrowid
            self.db.commit()
        return dict(spent=spent, requested=int(count), remnant_id=remnant_id)

    # ---------------------------------------------------------------- импорт / история / отчёт
    def import_csv(self, text):
        """Массовый приход из CSV (ТЗ 9.3): колонки material/thickness/length/width/qty/price_sheet/
        price_kg/location/received/batch — в любом порядке, разделитель ; или ,. -> (ok, errors)."""
        text = text.strip()
        if not text:
            return 0, ["пустой файл"]
        delim = ";" if ";" in text.splitlines()[0] else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delim)
        ok, errors = 0, []
        for i, row in enumerate(reader, 2):
            try:
                kw = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
                self.add_sheet(**kw)
                ok += 1
            except Exception as e:
                errors.append("строка %d: %s" % (i, e))
        return ok, errors

    def history(self, limit=200):
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM movements ORDER BY id DESC LIMIT ?", (int(limit),))]

    def report(self):
        """Остатки на складе (ТЗ 9.3): по материалам/толщинам — листы и обрезки."""
        with self.lock:
            sheets = [dict(r) for r in self.db.execute(
                "SELECT material, thickness, length, width, SUM(qty) qty FROM sheets WHERE qty>0"
                " GROUP BY material, thickness, length, width ORDER BY material, thickness")]
            remnants = [dict(r) for r in self.db.execute(
                "SELECT material, thickness, length, width, SUM(qty) qty FROM remnants WHERE qty>0"
                " GROUP BY material, thickness, length, width ORDER BY material, thickness")]
            return dict(sheets=sheets, remnants=remnants)
