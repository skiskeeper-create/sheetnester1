# -*- coding: utf-8 -*-
"""Автотесты SheetNester (README: `python test_*.py`).

Проверяют сборку без станка и без CypCut: импорт DXF, нестинг, запись .lxds (параметры режима,
в т.ч. GasType), склад, мосты/врезки, артикулы, round-trip геометрии, серверный API (сквозной).

Запуск из папки приложения:  python test_autotests.py [-v]   (нужен ezdxf, numpy; pytest не обязателен)
"""
import base64
import json
import math
import re
import sys
import threading
import time
import unittest
import zipfile
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP / "lxds"))

import bcmp            # noqa: E402
import bridges         # noqa: E402
import dxf_import      # noqa: E402
import geometry        # noqa: E402
import geomops as go   # noqa: E402
import leads           # noqa: E402
import model           # noqa: E402
import modes           # noqa: E402
import nesting         # noqa: E402
import ocr             # noqa: E402
import reader          # noqa: E402
import warehouse       # noqa: E402
import writer          # noqa: E402

SAMPLES = APP / "DXF samples"
TEMPLATE = APP / "data" / "template.lxds"


def _rect(w, h):
    """Замкнутый прямоугольник w×h с углом в (0,0) — LwPolyline."""
    pts = [(0, 0, 0.0), (w, 0, 0.0), (w, h, 0.0), (0, h, 0.0)]
    return ("LwPoly", True, pts)


def _square_part(name="sq", side=50.0):
    geo = _rect(side, side)
    outline = model.Shape(model.LAYER_OUTLINE, model.CH_CUT, geo)
    hole = model.Shape(model.LAYER_HOLE, model.CH_CUT,
                       ("Circle", (side / 2.0, side / 2.0), 5.0, math.pi / 4, 0.0))
    return model.Part(name, [outline, hole], 0)


class _SilentHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def server_bind(self):                 # подавляем вывод ошибок обработчиков в консоль тестов
        import socketserver
        old = socketserver.StreamRequestHandler.handle_error
        socketserver.StreamRequestHandler.handle_error = lambda self, *a: None
        try:
            ThreadingHTTPServer.server_bind(self)
        finally:
            socketserver.StreamRequestHandler.handle_error = old


# ---------------------------------------------------------------------------------------------------------
class TestModes(unittest.TestCase):
    """Справочник режимов и конвертация в CamParams (ТЗ 7.5)."""

    def test_gas_type_codes(self):
        self.assertEqual(modes.GAS_TYPE, {"O2": "2", "N2": "3", "Воздух": "6"})

    def test_find_mode(self):
        m = modes.find_mode("Сталь", "20")               # строка '20' == число 20.0
        self.assertIsNotNone(m)
        self.assertEqual(m["gas"], "O2")
        self.assertIsNone(modes.find_mode("Сталь", 13))  # нет такой толщины
        self.assertIsNone(modes.find_mode("Титан", 5))

    def test_camparams_units(self):
        p = modes.mode_to_camparams(modes.find_mode("Сталь", 20))
        self.assertEqual(p["WorkSpeed"], "10")           # 0.6 м/мин * 50/3 = 10 мм/с
        self.assertEqual(p["Pierce1/TimeStay"], "6000")  # 6 с -> мс
        self.assertEqual(p["Cut/GasPressure"], "2.5")
        self.assertEqual(p["Cut/GasType"], "2")          # O2
        self.assertEqual(p["MaterialName"], "Carbon steel-O2-20mm.fsm")
        p = modes.mode_to_camparams(modes.find_mode("Нержавейка", 2))
        self.assertEqual(p["Cut/GasType"], "6")          # Воздух
        p = modes.mode_to_camparams(modes.find_mode("Нержавейка", 8))
        self.assertEqual(p["Cut/GasType"], "3")          # N2 — НЕ подтверждён образцами!

    def test_modes_monotonic(self):
        """Скорость по толщине невозрастающая, давление непостоянна скачками — sanity для интерполятов
        Сталь 14/18 мм (согласованы с технологом не были)."""
        for mat in ("Сталь", "Нержавейка", "Алюминий"):
            ths = sorted(t for (m, t) in modes.MODES if m == mat)
            speeds = [modes.MODES[(mat, t)]["speed_m_min"] for t in ths]
            for a, b in zip(speeds, speeds[1:]):
                self.assertGreaterEqual(a, b, "%s: скорость падает с толщиной" % mat)
        s14 = modes.find_mode("Сталь", 14)["speed_m_min"]
        s18 = modes.find_mode("Сталь", 18)["speed_m_min"]
        self.assertTrue(0.8 <= s14 <= 1.0, "Сталь 14: вне диапазона 3Kw Carbon Steel (0.8-1)")
        self.assertTrue(0.6 <= s18 <= 0.7, "Сталь 18: вне диапазона 3Kw Carbon Steel (0.6-0.7)")


# ---------------------------------------------------------------------------------------------------------
class TestGasTypeInLxds(unittest.TestCase):
    """Проверка GasType=3 (азот) на файлах, собранных этим же кодом: патчер должен ставить код газа
    во все каналы реза шаблона; значение 3 нигде не теряется и не подменяется."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = APP / "раскрой" / "_test_gastype"
        cls.tmp.mkdir(parents=True, exist_ok=True)
        part = _square_part("Нерж_8")
        cls.files = {}
        for mat, th, want in (("Нержавейка", 8, "3"), ("Сталь", 20, "2"), ("Нержавейка", 2, "6")):
            mode = modes.find_mode(mat, th)
            n = model.Nest(300, 200, parts=[part],
                           placements=[writer.place_part(part, 0, 10, 10)], thickness=th, material=mat)
            path = cls.tmp / ("%s_%gмм.lxds" % (mat, th))
            writer.write_lxds(n, path, template=TEMPLATE, mode=mode)
            cls.files[(mat, th)] = (path, want)

    def _tech(self, path):
        with zipfile.ZipFile(path) as z:
            return z.read("Technical/content.xml").decode("utf-8", "replace")

    def test_patch_sets_expected_code_in_every_cut_channel(self):
        for (mat, th), (path, want) in self.files.items():
            tech = self._tech(path)
            chans = re.findall(r'<Channel ChannelPort="\d+">.*?</Channel>', tech, re.S)
            cut_ch = [c for c in chans if "<Cut " in c]
            self.assertGreaterEqual(len(cut_ch), 1, "%s %s: нет каналов реза" % (mat, th))
            codes = set()
            for c in cut_ch:
                m = re.search(r'<Cut [^>]*\bGasType="([^"]*)"', c)
                self.assertIsNotNone(m, "%s %s: в канале реза нет Cut@GasType" % (mat, th))
                codes.add(m.group(1))
            self.assertEqual(codes, {want}, "%s %sмм: ожидаем GasType=%s во всех каналах реза" % (mat, th, want))

    def test_n2_case_details(self):
        """Кейс азота (Нержавейка 8 мм): код 3 стоит именно в Cut, прокол — как в шаблоне (не патчится),
        Note/MaterialName переименованы под N2."""
        path, _ = self.files[("Нержавейка", 8)]
        tech = self._tech(path)
        m = re.search(r'<Channel ChannelPort="1"[^>]*>(.*?)<Channel ChannelPort="2"', tech, re.S)
        ch1 = m.group(1)
        self.assertIn('GasType="3"', re.search(r"<Cut [^>]*>", ch1).group(0))
        self.assertIn("stainless steel-N2-8mm.fsm", ch1)
        self.assertIn("Gas: N2", ch1)
        tmpl = self._tech(TEMPLATE)
        p_t = re.search(r'<Pierce1 [^>]*GasType="([^"]*)"', tmpl).group(1)
        p_c = re.search(r'<Pierce1 [^>]*GasType="([^"]*)"', ch1).group(1)
        self.assertEqual(p_t, p_c, "Pierce1@GasType должен остаться из шаблона (по замыслу авторов)")

    def test_no_mode_keeps_template(self):
        """Без режима файл пишется с параметрами шаблона (патча нет)."""
        part = _square_part("БезРежима")
        n = model.Nest(300, 200, parts=[part], placements=[writer.place_part(part, 0, 10, 10)], thickness=99)
        path = self.tmp / "no_mode.lxds"
        writer.write_lxds(n, path, template=TEMPLATE, mode=None)
        with zipfile.ZipFile(path) as z:
            got = z.read("Technical/content.xml")
        with zipfile.ZipFile(TEMPLATE) as z:
            want = z.read("Technical/content.xml")
        self.assertEqual(got, want, "без режима Technical обязан совпадать с шаблоном")

    def test_workspeed_pierce_synced_with_gas(self):
        """Все параметры канала 1 меняются синхронно с газом (regression на _patch_technical)."""
        for (mat, th), (path, want) in self.files.items():
            mode = modes.find_mode(mat, th)
            tech = self._tech(path)
            ch1 = re.search(r'<Channel ChannelPort="1"[^>]*>(.*?)<Channel ChannelPort="2"', tech, re.S).group(1)
            cp = modes.mode_to_camparams(mode)
            self.assertIn('WorkSpeed="%s"' % cp["WorkSpeed"], ch1)
            self.assertIn('TimeStay="%s"' % cp["Pierce1/TimeStay"], ch1)
            self.assertIn(cp["MaterialName"], ch1)
            self.assertIn('GasType="%s"' % want, re.search(r"<Cut [^>]*>", ch1).group(0))


# ---------------------------------------------------------------------------------------------------------
class TestDxfImport(unittest.TestCase):
    def test_parse_filename(self):
        m = dxf_import.parse_filename("7-4057 16мм  2шт.dxf")
        self.assertEqual(m, {"article": "7-4057", "thickness": 16.0, "qty": 2})
        m = dxf_import.parse_filename("3-S301 20мм 12шт.dxf")
        self.assertEqual((m["article"], m["thickness"], m["qty"]), ("3-S301", 20.0, 12))
        m = dxf_import.parse_filename("без-метаданных")
        self.assertIsNone(m["thickness"])

    def test_samples_load(self):
        files = sorted(SAMPLES.glob("*.dxf"))
        self.assertGreaterEqual(len(files), 5, "нет примеров DXF")
        ok = 0
        for f in files:
            part, rep = dxf_import.load_part(f)
            if rep.has_outline and part is not None and part.outline >= 0:
                ok += 1
                m = nesting.part_metrics(part)
                self.assertGreater(m["cut_m"], 0)
                self.assertGreater(m["area"], 0)
        self.assertEqual(ok, len(files), "не все примеры импортируются с внешним контуром")


# ---------------------------------------------------------------------------------------------------------
class TestNestingAndWriter(unittest.TestCase):
    def test_small_parts_fit_and_lxds_valid(self):
        tmp = APP / "раскрой" / "_test_nest"
        tmp.mkdir(parents=True, exist_ok=True)
        entries = [("a", "A50", _square_part("A50", 50), 6), ("b", "B30", _square_part("B30", 30), 4)]
        r = nesting.run_group(entries, 300, 200, 20, tmp, "тест_20мм", TEMPLATE,
                              mode=modes.find_mode("Сталь", 20))
        self.assertEqual(r["unplaced"], [], "детали не должны оставаться за листом")
        total = sum(s["count"] for s in r["sheets"])
        self.assertEqual(total, 10)
        for s in r["sheets"]:
            shapes, blocks = reader.load(s["file"])          # round-trip: записанное читается парсером
            self.assertTrue(shapes, "в .lxds нет фигур")
            self.assertIn(model.LAYER_OUTLINE, {sh["attrs"].get("Layer") for sh in shapes.values()})
            self.assertTrue(blocks, "в .lxds нет блоков")
            self.assertGreater(s["utilization"], 0.2)
            self.assertLessEqual(s["ext"][1][0], 300 + 1e-6)
            self.assertLessEqual(s["ext"][1][1], 200 + 1e-6)

    def test_unplaceable_reported(self):
        tmp = APP / "раскрой" / "_test_nest"
        tmp.mkdir(parents=True, exist_ok=True)
        big = _square_part("BIG", 500)
        r = nesting.run_group([("z", "BIG", big, 1)], 300, 200, 20, tmp, "тест_big", TEMPLATE)
        self.assertEqual(len(r["unplaced"]), 1)
        self.assertEqual(r["sheets"], [])


# ---------------------------------------------------------------------------------------------------------
class TestWarehouse(unittest.TestCase):
    def test_lifecycle(self):
        wh = warehouse.Warehouse(APP / "раскрой" / "_test_wh.db")
        wh.add_sheet(material="Сталь", thickness=20, length=1500, width=3000, qty=2, price_sheet=1000)
        av = wh.available("Сталь", 20, 1500, 3000)
        self.assertEqual(av["qty"], 2)
        wh.consume("Сталь", 20, 1, 1500, 3000, source="тест")
        self.assertEqual(wh.available("Сталь", 20, 1500, 3000)["qty"], 1)
        self.assertEqual(wh.available("Сталь", 20, 1600, 3000)["qty"], 0, "лист меньше запроса недоступен")
        hist = wh.history()
        self.assertTrue(any("тест" in str(h) for h in hist), "consume должен оставить запись в истории")


# ---------------------------------------------------------------------------------------------------------
class TestBridgesLeads(unittest.TestCase):
    def test_corner_bridges_on_rectangle(self):
        bez = go.to_bezier(_rect(100, 60))
        for mode_name in ("all", "diagonal"):
            bs = bridges.corner_bridges(bez, mode_name, size=1.0)
            self.assertGreaterEqual(len(bs), 2 if mode_name == "diagonal" else 4)
            self.assertEqual(bridges.validate_bridges(bez, bs), [], "мосты должны лежать на прямых")

    def test_uniform_bridges_count(self):
        bez = go.to_bezier(_rect(100, 60))
        bs = bridges.uniform_bridges(bez, step=None, count=4)
        self.assertGreaterEqual(len(bs), 3)
        self.assertEqual(bridges.validate_bridges(bez, bs), [])

    def test_leads_rules(self):
        l = leads.default_lead("outline", 20)           # >10 мм — всегда дуга, радиус t/2
        self.assertEqual(l.type, model.LEAD_ARC)
        self.assertAlmostEqual(l.arc_radius, 10.0)
        self.assertAlmostEqual(l.length, 10.0)
        l = leads.default_lead("outline", 4, lead_type=model.LEAD_LINE)
        self.assertEqual(l.type, model.LEAD_LINE)
        self.assertAlmostEqual(l.length, 2.0)
        self.assertAlmostEqual(l.pos, 0.0)
        l = leads.lead_at(_rect(100, 100), 100, 50, 4)  # ближайшая точка справа по центру
        self.assertAlmostEqual(l.pos, 100.0, delta=1.0)


# ---------------------------------------------------------------------------------------------------------
class TestArticles(unittest.TestCase):
    def test_pattern_and_hits(self):
        rx = ocr.compile_pattern()
        self.assertTrue(rx.match("7-4057"))
        self.assertFalse(rx.match("200"))               # размерка
        part = _square_part("p", 100)
        rep = dxf_import.Report(texts=[("7-4057", 20.0, 20.0, 5.0, 0.0),
                                       ("999", 50.0, 50.0, 5.0, 0.0),
                                       ("X-Y", 500.0, 500.0, 5.0, 0.0)])  # вне контура
        res = ocr.apply_articles(part, rep)
        self.assertEqual([h.text for h in res["found"]], ["7-4057"])
        self.assertEqual([h.text for h in res["disputed"]], ["X-Y"])
        self.assertGreater(res["added"], 0, "артикул должен породить контуры букв")
        eng = [s for s in part.shapes if s.layer == model.LAYER_ENGRAVE]
        self.assertTrue(all(s.channel == model.CH_ENGRAVE for s in eng))
        res2 = ocr.apply_articles(part, rep)            # повторный вызов — без дублей
        self.assertEqual(res2["added"], 0)


# ---------------------------------------------------------------------------------------------------------
class TestGeometryRoundTrip(unittest.TestCase):
    def test_encode_decode(self):
        cases = [("Line", (0, 0), (10, 5)),
                 ("Arc", (1, 2), 3.5, 0.3, 2.0),
                 ("Circle", (0, 0), 7.0, math.pi / 4, 0.0),
                 ("LwPoly", True, [(0, 0, 0.0), (10, 0, 0.5), (10, 10, 0.0)]),
                 ("Bezier", True, [((0, 0), (3, 0), (10, 3), (10, 10))])]
        for geo in cases:
            cls, payload = geometry.enc(geo)
            back = geometry.dec(cls, payload)
            self.assertEqual(back[0], geo[0], "тип не должен меняться при round-trip")
        # длины/площади согласуются после transform
        g = _rect(40, 25)
        self.assertAlmostEqual(go.area(g), 1000.0, places=3)
        self.assertAlmostEqual(go.length(g), 130.0, places=1)
        g2 = go.transform(g, angle=math.pi / 2, dx=5, dy=-7)
        self.assertAlmostEqual(go.area(g2), 1000.0, places=3)

    def test_bcmp_dump_roundtrip(self):
        objs = [bcmp.Obj("TGksTest", [bcmp.Chunk("C", 1, b"\x01\x02\x03")])]
        blob = bcmp.dump_all(objs)
        parsed = bcmp.parse_all(blob)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].chunks[0].payload, b"\x01\x02\x03")


# ---------------------------------------------------------------------------------------------------------
class TestServerAPI(unittest.TestCase):
    """Сквозной тест через HTTP-API сервера (импорт -> нестинг), без открытия браузера."""

    @classmethod
    def setUpClass(cls):
        import server
        from http.server import ThreadingHTTPServer
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        import urllib.request
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/shutdown" % cls.port, "{}", timeout=2)
        except Exception:
            pass
        cls.srv.shutdown()

    def _post(self, path, obj):
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     data=json.dumps(obj).encode(), method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode())

    def test_ping_import_nest(self):
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % self.port, timeout=5) as r:
            ping = json.loads(r.read().decode())
        self.assertTrue(ping["ok"])
        self.assertEqual(ping["ver"], "2026-10-03.2")

        f = SAMPLES / "7040 4мм 8шт.dxf"
        imp = self._post("/api/import", {"files": [{"name": f.name,
                                                    "data": base64.b64encode(f.read_bytes()).decode()}]})
        p = imp["parts"][0]
        self.assertNotIn("error", p, p.get("error"))
        self.assertEqual(p["thickness"], 4.0)
        self.assertEqual(p["qty"], 8)
        nest = self._post("/api/nest", {"items": [{"id": p["id"], "name": p["name"], "qty": 8,
                                                   "material": "Сталь", "thickness": 4}],
                                        "sheet_w": 1000, "sheet_h": 600})
        g = nest["groups"][0]
        self.assertEqual(sum(s["count"] for s in g["sheets"]), 8)
        self.assertEqual(g["unplaced"], [])
        for s in g["sheets"]:
            self.assertTrue(Path(s["file"]).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
