"""Импорт DXF -> model.Part (контур детали + отверстия [+ гравировка/риски по layer_hint]).

Публичное: parse_filename, load_part, batch_load, Report.

Конвейер load_part: чтение (ezdxf, при ошибке recover) -> единицы ($INSUNITS -> мм) -> обход modelspace
(INSERT разворачивается virtual_entities, глубина <= MAX_INSERT_DEPTH; TEXT/MTEXT/ATTRIB -> report.texts)
-> роли слоёв -> удаление нулевых/дублей -> сшивка разрозненных сегментов в контуры с допуском tol
-> классификация (внешний = наибольшая площадь, остальные внутри = отверстия) -> сдвиг bbox внешнего в (0,0).

Геометрия строго по контракту model.py: внешний и некруглые отверстия — замкнутый ("Bezier", True, segs)
(geomops.to_bezier/bezier_join); круглые отверстия — ("Circle", c, r, pi/4, 0).
Слои DXF без layer_hint: если есть слои с "CUT" в имени — контурами считаются только они; иначе — все слои,
кроме служебных (_AUX_LAYER_RE). Слои с именами LAYER_* модели (CUT_OUTLINE/CUT_HOLE/ENGRAVE_TEXT/MARK_RISKS)
назначаются сами себе.
"""
import fnmatch
import math
import os
import re
from pathlib import Path
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import ezdxf
from ezdxf import path as ezpath

import geomops
from model import (CH_CUT, CH_ENGRAVE, LAYER_ENGRAVE, LAYER_HOLE, LAYER_MARK, LAYER_OUTLINE, Part, Shape)

MAX_INSERT_DEPTH = 8
MAX_SELFINT_SEGS = 2000
ZERO_LEN = 1e-6            # мм: элемент короче считается нулевым
DUP_GRID = 1e-3            # мм: сетка округления при поиске дублей
GAP_EXACT = 1e-6           # мм: зазор больше этого при сшивке считается «закрытым разрывом»
_TWO_PI = 2.0 * math.pi
_CIRCLE_A0 = math.pi / 4.0

_UNITS = {0: ("unitless", 1.0), 1: ("inch", 25.4), 2: ("foot", 304.8), 3: ("mile", 1609344.0), 4: ("mm", 1.0),
          5: ("cm", 10.0), 6: ("m", 1000.0), 7: ("km", 1e6), 8: ("microinch", 25.4e-6), 9: ("mil", 0.0254),
          10: ("yard", 914.4), 11: ("angstrom", 1e-7), 12: ("nm", 1e-6), 13: ("micron", 1e-3), 14: ("dm", 100.0),
          15: ("dam", 1e4), 16: ("hm", 1e5), 17: ("gm", 1e12)}
_AUX_LAYER_RE = re.compile(r"^(defpoints|oversize|dim.*|text.*|scribe.*)$", re.I)
_ROLE_OF_MODEL_LAYER = {LAYER_OUTLINE: "outline", LAYER_HOLE: "hole", LAYER_ENGRAVE: "engrave", LAYER_MARK: "mark"}


# ---------------------------------------------------------------- имя файла
_QTY_RE = re.compile(r"(?:^|\s)(\d+)\s*(?:шт|pcs)\.?\s*$", re.I)
_TH_RE = re.compile(r"(?:^|\s)(\d+(?:[.,]\d+)?)\s*(?:мм|mm)\.?\s*$", re.I)


def parse_filename(path):
    """«7-4057 16мм  2шт.dxf» -> {article: '7-4057', thickness: 16.0, qty: 2}; нет толщины/количества -> None."""
    stem = os.path.splitext(os.path.basename(str(path)))[0].strip()
    qty = th = None
    for _ in range(2):                       # порядок «мм»/«шт» в хвосте любой
        m = _QTY_RE.search(stem)
        if m and qty is None:
            qty = int(m.group(1))
            stem = stem[:m.start()].rstrip()
        m = _TH_RE.search(stem)
        if m and th is None:
            th = float(m.group(1).replace(",", "."))
            stem = stem[:m.start()].rstrip()
    article = re.sub(r"\s+", " ", stem).strip() or None
    return {"article": article, "thickness": th, "qty": qty}


# ---------------------------------------------------------------- отчёт
@dataclass
class Report:
    path: str = ""
    units_detected: str = "mm"
    scale: float = 1.0
    warnings: list = field(default_factory=list)
    open_contours: int = 0
    duplicates_removed: int = 0
    gaps_closed: int = 0
    texts: list = field(default_factory=list)          # (текст, x, y, высота, поворот_град) в координатах детали, мм
    ignored: dict = field(default_factory=dict)        # {тип DXF: число} (только геометрия/прочее, не текст)
    ignored_layers: dict = field(default_factory=dict)  # {слой: число}
    self_intersections: list = field(default_factory=list)   # индексы в Part.shapes
    stats: dict = field(default_factory=dict)
    offset: tuple = (0.0, 0.0)                         # сдвиг, применённый к детали (bbox внешнего -> (0,0))
    meta: dict = field(default_factory=dict)           # parse_filename(path)
    error: str = ""

    @property
    def has_outline(self):
        return bool(self.stats.get("outline"))


# ---------------------------------------------------------------- геометрические мелочи
def _d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _line_seg(a, b):
    return [tuple(a), (a[0] + (b[0] - a[0]) / 3.0, a[1] + (b[1] - a[1]) / 3.0),
            (a[0] + 2.0 * (b[0] - a[0]) / 3.0, a[1] + 2.0 * (b[1] - a[1]) / 3.0), tuple(b)]


def _arc_pt(c, r, ang):
    return (c[0] + r * math.cos(ang), c[1] + r * math.sin(ang))


def _ends(geo):
    """(начало, конец) канонического примитива (Line/Arc/Bezier)."""
    k = geo[0]
    if k == "Line":
        return geo[1], geo[2]
    if k == "Arc":
        _, c, r, st, sw = geo
        return _arc_pt(c, r, st), _arc_pt(c, r, st + sw)
    if k == "Bezier":
        return geo[2][0][0], geo[2][-1][3]
    raise ValueError(k)


def _reversed(geo):
    k = geo[0]
    if k == "Line":
        return ("Line", geo[2], geo[1])
    if k == "Arc":
        _, c, r, st, sw = geo
        return ("Arc", c, r, st + sw, -sw)        # отрицательный sweep понимают to_bezier/length/bbox
    if k == "Bezier":
        return ("Bezier", geo[1], [list(reversed(s)) for s in reversed(geo[2])])
    raise ValueError(k)


def _approx_len(geo):
    k = geo[0]
    if k == "Line":
        return _d(geo[1], geo[2])
    if k == "Arc":
        return geo[2] * abs(geo[4])
    if k == "Circle":
        return _TWO_PI * geo[2]
    if k == "Bezier":
        return sum(_d(s[0], s[1]) + _d(s[1], s[2]) + _d(s[2], s[3]) for s in geo[2])   # верхняя оценка
    raise ValueError(k)


def _bulge_geo(p, q, b):
    """Сегмент полилинии p->q с bulge b -> каноническая Line или Arc (sweep > 0). None — нулевая длина."""
    dx, dy = q[0] - p[0], q[1] - p[1]
    dd = math.hypot(dx, dy)
    if dd <= 1e-12:
        return None
    if abs(b) < 1e-12:
        return ("Line", (p[0], p[1]), (q[0], q[1]))
    th = 4.0 * math.atan(b)
    r = dd / (2.0 * math.sin(abs(th) / 2.0))
    h = (dd / 2.0) / math.tan(th / 2.0)                # со знаком: >0 — центр слева от p->q
    c = ((p[0] + q[0]) / 2.0 - dy / dd * h, (p[1] + q[1]) / 2.0 + dx / dd * h)
    ap = math.atan2(p[1] - c[1], p[0] - c[0])
    if th > 0:
        return ("Arc", c, r, ap, th)
    return ("Arc", c, r, ap + th, -th)


def _bulge_pts(p, q, b, step_deg=5.0):
    """Точки сегмента p->q (без q) для LwPoly: дуга разбивается шагом <= step_deg."""
    if abs(b) < 1e-12:
        return [(p[0], p[1])]
    dx, dy = q[0] - p[0], q[1] - p[1]
    dd = math.hypot(dx, dy)
    if dd <= 1e-12:
        return [(p[0], p[1])]
    th = 4.0 * math.atan(b)
    r = dd / (2.0 * math.sin(abs(th) / 2.0))
    h = (dd / 2.0) / math.tan(th / 2.0)
    c = ((p[0] + q[0]) / 2.0 - dy / dd * h, (p[1] + q[1]) / 2.0 + dx / dd * h)
    ap = math.atan2(p[1] - c[1], p[0] - c[0])
    n = max(1, int(math.ceil(abs(th) / math.radians(step_deg))))
    return [_arc_pt(c, r, ap + th * i / n) for i in range(n)]


def _sample_geo(geo, n_bez=8):
    """Полилиния-аппроксимация замкнутой геометрии (Circle/Bezier): список точек без повтора первой."""
    if geo[0] == "Circle":
        (cx, cy), r = geo[1], geo[2]
        return [(cx + r * math.cos(_TWO_PI * i / 72), cy + r * math.sin(_TWO_PI * i / 72)) for i in range(72)]
    pts = []
    for s in geo[2]:
        pts.extend(_sample_seg(s, n_bez)[:-1])
    return pts


def _is_straight(s):
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = s
    dx, dy = x3 - x0, y3 - y0
    L = math.hypot(dx, dy)
    if L < 1e-12:
        return False
    return abs((x1 - x0) * dy - (y1 - y0) * dx) / L < 1e-9 and abs((x2 - x0) * dy - (y2 - y0) * dx) / L < 1e-9


def _sample_seg(s, n):
    if _is_straight(s):
        return [s[0], s[3]]
    out = []
    for i in range(n + 1):
        t = i / n
        u = 1.0 - t
        a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
        out.append((a * s[0][0] + b * s[1][0] + c * s[2][0] + d * s[3][0],
                    a * s[0][1] + b * s[1][1] + c * s[2][1] + d * s[3][1]))
    out[0], out[-1] = tuple(s[0]), tuple(s[3])
    return out


def _pip(pt, poly):
    x, y = pt
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _seg_cross(a, b, c, d):
    """Собственное пересечение отрезков ab и cd (строго, без касаний)."""
    def cr(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    d1, d2 = cr(a, b, c), cr(a, b, d)
    d3, d4 = cr(c, d, a), cr(c, d, b)
    return ((d1 > 1e-9 and d2 < -1e-9) or (d1 < -1e-9 and d2 > 1e-9)) and \
           ((d3 > 1e-9 and d4 < -1e-9) or (d3 < -1e-9 and d4 > 1e-9))


def _polyline_of(geo):
    """Замкнутая ломаная-аппроксимация контура для проверки самопересечений: список отрезков ((x,y),(x,y))."""
    pts = []
    for s in geo[2]:
        pts.extend(_sample_seg(s, 8)[:-1])
    return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def _dedupe_key(geo):
    def r(v):
        return round(v / DUP_GRID)
    k = geo[0]
    if k == "Line":
        return ("L",) + tuple(sorted(((r(geo[1][0]), r(geo[1][1])), (r(geo[2][0]), r(geo[2][1])))))
    if k == "Arc":
        _, c, rad, st, sw = geo
        return ("A", r(c[0]), r(c[1]), r(rad), round((st % _TWO_PI) * 1e4), round(sw * 1e4))
    if k == "Circle":
        return ("C", r(geo[1][0]), r(geo[1][1]), r(geo[2]))
    if k == "Bezier":
        fwd = tuple((r(p[0]), r(p[1])) for s in geo[2] for p in s)
        rev = tuple((r(p[0]), r(p[1])) for s in reversed(geo[2]) for p in reversed(s))
        return ("B", geo[1], min(fwd, rev))
    raise ValueError(k)


# ---------------------------------------------------------------- сущности DXF -> внутренние
class _Ent:
    __slots__ = ("kind", "layer", "geos", "closed", "poly")

    def __init__(self, kind, layer, geos, closed=False, poly=None):
        self.kind, self.layer, self.geos, self.closed, self.poly = kind, layer, geos, closed, poly


class _El:
    __slots__ = ("geo", "a", "b")

    def __init__(self, geo):
        self.geo = geo
        self.a, self.b = _ends(geo)


class _C:
    """Замкнутый контур-кандидат."""
    __slots__ = ("geo", "group", "area", "bbox", "poly", "order")

    def __init__(self, geo, group, order):
        self.geo, self.group, self.order = geo, group, order
        self.area = geomops.area(geo)
        self.bbox = geomops.bbox(geo)
        self.poly = _sample_geo(geo)


def _ocs_mirror(e):
    """False — обычная плоскость (0,0,1); True — (0,0,-1) (зеркало по X); None — иная нормаль."""
    if not e.dxf.hasattr("extrusion"):
        return False
    x = e.dxf.extrusion
    if abs(x.x) < 1e-9 and abs(x.y) < 1e-9:
        return x.z < 0
    return None


def _units_of(doc, rep, path=None):
    """Единицы чертежа. $INSUNITS берётся, только если он реально записан в файле: для DXF без заголовка ezdxf
    подставляет значение по умолчанию (6 = метры), из-за чего детали увеличивались в 1000 раз."""
    code = doc.header.get("$INSUNITS", 0)
    if path is not None:
        try:
            if b"$INSUNITS" not in Path(path).read_bytes():
                code = 0
        except OSError:
            pass
    if code in _UNITS:
        name, k = _UNITS[code]
    else:
        name, k = "unknown(%s)" % code, 1.0
        rep.warnings.append("неизвестный $INSUNITS=%s: принят мм" % code)
    rep.units_detected = "mm(assumed)" if name == "unitless" else name
    rep.scale = k
    return k


class _Importer:
    def __init__(self, rep, k):
        self.rep, self.k = rep, k
        self.ents = []
        self.raw_texts = []
        self.unsupported = Counter()      # типы, не превращённые в геометрию (POINT, DIMENSION, HATCH, ...)

    def ignore(self, typ, layer):
        self.rep.ignored[typ] = self.rep.ignored.get(typ, 0) + 1
        self.rep.ignored_layers[layer] = self.rep.ignored_layers.get(layer, 0) + 1

    # ---- обход
    def collect(self, entities, parent_layer=None, depth=0):
        for e in entities:
            t = e.dxftype()
            layer = e.dxf.layer if e.dxf.hasattr("layer") else "0"
            if layer == "0" and parent_layer not in (None, "0"):
                layer = parent_layer
            if t in ("TEXT", "MTEXT", "ATTRIB"):
                self.text(e, t)
            elif t == "INSERT":
                self.insert(e, layer, depth)
            else:
                try:
                    ent = self.make_ent(e, t, layer)
                except Exception as ex:                       # битая сущность не должна ронять файл
                    self.rep.warnings.append("%s (слой %s): не удалось преобразовать: %s" % (t, layer, ex))
                    ent = None
                if ent is None:
                    self.unsupported[t] += 1
                    self.ignore(t, layer)
                else:
                    self.ents.append(ent)

    def insert(self, e, layer, depth):
        if depth >= MAX_INSERT_DEPTH:
            self.rep.warnings.append("INSERT '%s': превышена глубина вложенности %d" % (e.dxf.name, MAX_INSERT_DEPTH))
            return
        try:
            inss = list(e.multi_insert()) if e.mcount > 1 else [e]
            for ins in inss:
                for a in ins.attribs:
                    self.text(a, "ATTRIB")
                self.collect(list(ins.virtual_entities()), layer, depth + 1)
        except Exception as ex:
            self.rep.warnings.append("INSERT '%s': не развёрнут: %s" % (e.dxf.get("name", "?"), ex))

    def text(self, e, t):
        k = self.k
        if t == "MTEXT":
            s, h, rot = e.plain_text(), e.dxf.get("char_height", 0.0), e.get_rotation()
        else:
            s, h, rot = e.dxf.get("text", ""), e.dxf.get("height", 0.0), e.dxf.get("rotation", 0.0)
        ins = e.dxf.insert
        self.raw_texts.append((s, ins.x * k, ins.y * k, h * k, rot))

    # ---- геометрия
    def make_ent(self, e, t, layer):
        k = self.k
        if t == "LINE":
            s, f = e.dxf.start, e.dxf.end
            return _Ent("LINE", layer, [("Line", (s.x * k, s.y * k), (f.x * k, f.y * k))])
        if t in ("ARC", "CIRCLE"):
            m = _ocs_mirror(e)
            if m is None:
                self.rep.warnings.append("%s (слой %s): нормаль не (0,0,±1): пропущен" % (t, layer))
                return None
            c, r = (e.dxf.center.x * k, e.dxf.center.y * k), e.dxf.radius * k
            if r <= 0:
                return None
            geo = ("Circle", c, r, _CIRCLE_A0, 0.0)
            if t == "ARC":
                sw = math.radians((e.dxf.end_angle - e.dxf.start_angle) % 360.0)
                if 1e-9 < sw < _TWO_PI - 1e-9:
                    geo = ("Arc", c, r, math.radians(e.dxf.start_angle), sw)
            if m:
                geo = geomops.transform(geo, mirror=True)
            return _Ent(t, layer, [geo])
        if t in ("LWPOLYLINE", "POLYLINE"):
            m = _ocs_mirror(e)
            if m is None:
                self.rep.warnings.append("%s (слой %s): нормаль не (0,0,±1): пропущен" % (t, layer))
                return None
            if t == "LWPOLYLINE":
                pts = [(x * k, y * k, b) for x, y, b in e.get_points("xyb")]
                closed = bool(e.closed)
            else:
                if e.is_polygon_mesh or e.is_poly_face_mesh:
                    return None
                pts = [(v.dxf.location.x * k, v.dxf.location.y * k, v.dxf.get("bulge", 0.0)) for v in e.vertices]
                closed = bool(e.is_closed)
            if m:
                pts = [(-x, y, -b) for x, y, b in pts]
            if len(pts) < 2:
                return None
            n = len(pts)
            geos = []
            for i in range(n if closed else n - 1):
                g = _bulge_geo(pts[i], pts[(i + 1) % n], pts[i][2])
                if g is not None:
                    geos.append(g)
            return _Ent(t, layer, geos, closed, pts)
        if t in ("ELLIPSE", "SPLINE"):
            geos = []
            for sp in ezpath.make_path(e).sub_paths():
                cur = sp.start
                segs = []
                for c in sp.commands():
                    nm = c.type.name
                    if nm == "LINE_TO":
                        segs.append(_line_seg((cur.x * k, cur.y * k), (c.end.x * k, c.end.y * k)))
                    elif nm == "CURVE3_TO":
                        p0, q, p3 = (cur.x * k, cur.y * k), (c.ctrl.x * k, c.ctrl.y * k), (c.end.x * k, c.end.y * k)
                        segs.append([p0, (p0[0] + 2 * (q[0] - p0[0]) / 3, p0[1] + 2 * (q[1] - p0[1]) / 3),
                                     (p3[0] + 2 * (q[0] - p3[0]) / 3, p3[1] + 2 * (q[1] - p3[1]) / 3), p3])
                    elif nm == "CURVE4_TO":
                        segs.append([(cur.x * k, cur.y * k), (c.ctrl1.x * k, c.ctrl1.y * k),
                                     (c.ctrl2.x * k, c.ctrl2.y * k), (c.end.x * k, c.end.y * k)])
                    else:
                        continue
                    cur = c.end
                if not segs:
                    continue
                closed = _d(segs[0][0], segs[-1][3]) <= ZERO_LEN
                if closed:
                    segs[-1][3] = segs[0][0]
                geos.append(("Bezier", closed, segs))
            return _Ent(t, layer, geos) if geos else None
        return None


# ---------------------------------------------------------------- сшивка
def _stitch(els, tol):
    """Жадная сшивка элементов в цепочки с допуском tol. -> [(chain, closed)], chain = [(индекс, развёрнут?)]."""
    n = len(els)
    cell = max(tol, 1e-9)
    grid = defaultdict(list)

    def key(p):
        return (math.floor(p[0] / cell), math.floor(p[1] / cell))

    for i, e in enumerate(els):
        grid[key(e.a)].append((i, 0))
        grid[key(e.b)].append((i, 1))
    used = [False] * n

    def find(p):
        ix, iy = key(p)
        best = None
        for gx in (ix - 1, ix, ix + 1):
            for gy in (iy - 1, iy, iy + 1):
                for j, end in grid.get((gx, gy), ()):
                    if used[j]:
                        continue
                    d = _d(p, els[j].a if end == 0 else els[j].b)
                    if d <= tol and (best is None or d < best[0]):
                        best = (d, j, end)
        return best

    def start_of(it):
        return els[it[0]].b if it[1] else els[it[0]].a

    def end_of(it):
        return els[it[0]].a if it[1] else els[it[0]].b

    def single_closable(e):
        return e.geo[0] in ("Bezier", "Arc") and _d(e.a, e.b) <= tol and _approx_len(e.geo) > 10 * tol

    out = []
    for s in range(n):
        if used[s]:
            continue
        used[s] = True
        chain = [(s, False)]
        closed = False
        for _ in range(2):                                  # вперёд; затем разворот и снова вперёд (= назад от seed)
            while True:
                last, first = end_of(chain[-1]), start_of(chain[0])
                can_close = (len(chain) >= 2 or single_closable(els[chain[0][0]])) and _d(last, first) <= tol
                cand = find(last)
                if can_close and (cand is None or _d(last, first) <= cand[0]):
                    closed = True
                    break
                if cand is None:
                    break
                used[cand[1]] = True
                chain.append((cand[1], cand[2] == 1))
            if closed:
                break
            chain = [(i, not r) for i, r in reversed(chain)]
        out.append((chain, closed))
    return out


def _as_circle(geos, tol):
    """Цепочка дуг одного центра/радиуса на полный оборот -> ("Circle", ...) иначе None."""
    if not geos or any(g[0] != "Arc" for g in geos):
        return None
    c0, r0 = geos[0][1], geos[0][2]
    if any(_d(g[1], c0) > tol or abs(g[2] - r0) > tol for g in geos):
        return None
    if abs(sum(abs(g[4]) for g in geos) - _TWO_PI) > 1e-6:
        return None
    n = len(geos)
    c = (sum(g[1][0] for g in geos) / n, sum(g[1][1] for g in geos) / n)
    return ("Circle", c, sum(g[2] for g in geos) / n, _CIRCLE_A0, 0.0)


# ---------------------------------------------------------------- главная функция
def _read_doc(path, rep):
    try:
        return ezdxf.readfile(path)
    except Exception as ex:
        from ezdxf import recover
        try:
            doc, _aud = recover.readfile(path)
        except Exception as ex2:
            raise IOError(("не удалось прочитать DXF: %s; recover: %s" % (ex, ex2))[:300])
        rep.warnings.append("файл прочитан через recover (%s)" % ex)
        return doc


def _role_map(layers, layer_hint):
    """{слой.upper(): роль}; роли: outline, hole, engrave, mark, contour (авто), ignore, aux."""
    hint = {n.upper(): n for n in _ROLE_OF_MODEL_LAYER}            # имена слоёв модели -> сами себе
    for k, v in (layer_hint or {}).items():
        if v not in _ROLE_OF_MODEL_LAYER:
            raise ValueError("layer_hint: значение %r не LAYER_*" % (v,))
        hint[k.upper()] = v
    roles = {}
    unhinted = [l for l in layers if l.upper() not in hint]
    cut = [l for l in unhinted if "CUT" in l.upper()]
    for l in layers:
        u = l.upper()
        if u in hint:
            roles[u] = _ROLE_OF_MODEL_LAYER[hint[u]]
        elif _AUX_LAYER_RE.match(l):
            roles[u] = "aux"
        elif cut:
            roles[u] = "contour" if l in cut else "ignore"
        else:
            roles[u] = "contour"
    return roles


def _flatten_ent(ent):
    """_Ent -> (closed, [(x,y,0.0),...]) для LwPoly."""
    if ent.poly is not None:
        pts, n = ent.poly, len(ent.poly)
        out = []
        for i in range(n if ent.closed else n - 1):
            out.extend(_bulge_pts(pts[i], pts[(i + 1) % n], pts[i][2]))
        if not ent.closed:
            out.append(pts[-1][:2])
        return ent.closed, [(x, y, 0.0) for x, y in out]
    g = ent.geos[0]
    if g[0] == "Line":
        return False, [g[1] + (0.0,), g[2] + (0.0,)]
    if g[0] == "Circle":
        return True, [p + (0.0,) for p in _sample_geo(g)]
    if g[0] == "Arc":
        _, c, r, st, sw = g
        n = max(1, int(math.ceil(sw / math.radians(5.0))))
        return False, [_arc_pt(c, r, st + sw * i / n) + (0.0,) for i in range(n + 1)]
    pts = []
    for s in g[2]:
        pts.extend(_sample_seg(s, 8)[:-1])
    if g[1]:
        return True, [p + (0.0,) for p in pts]
    return False, [p + (0.0,) for p in pts] + [g[2][-1][3] + (0.0,)]


def _self_intersects(geo):
    segs = _polyline_of(geo)
    m = len(segs)
    if m > MAX_SELFINT_SEGS:
        return None
    bb = [(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])) for a, b in segs]
    for i in range(m):
        bi = bb[i]
        for j in range(i + 2, m):
            if i == 0 and j == m - 1:
                continue
            bj = bb[j]
            if bj[0] > bi[2] or bi[0] > bj[2] or bj[1] > bi[3] or bi[1] > bj[3]:
                continue
            if _seg_cross(segs[i][0], segs[i][1], segs[j][0], segs[j][1]):
                return True
    return False


def _inside(c, o, tol):
    """Контур c лежит внутри o: bbox + доля точек c внутри полигона o >= 0.5."""
    cb, ob = c.bbox, o.bbox
    if cb[0] < ob[0] - tol or cb[1] < ob[1] - tol or cb[2] > ob[2] + tol or cb[3] > ob[3] + tol:
        return False
    step = max(1, len(c.poly) // 40)
    pts = c.poly[::step]
    return sum(1 for p in pts if _pip(p, o.poly)) >= 0.5 * len(pts)


def load_part(path, tol=0.05, name=None, layer_hint=None):
    """DXF -> (model.Part, Report). Неразборчивый файл — исключение (IOError); всё остальное — в Report."""
    path = str(path)
    rep = Report(path=path, meta=parse_filename(path))
    doc = _read_doc(path, rep)
    k = _units_of(doc, rep, path)
    imp = _Importer(rep, k)
    imp.collect(list(doc.modelspace()))
    ents = imp.ents

    roles = _role_map(sorted({e.layer for e in ents}), layer_hint)

    # --- разнесение по ролям
    pools = defaultdict(list)        # группа ('outline'|'hole'|None) -> [geo]
    extra = []                       # (роль 'engrave'|'mark', _Ent)
    aux_ignored, layer_ignored = Counter(), Counter()
    for e in ents:
        role = roles[e.layer.upper()]
        if role in ("ignore", "aux"):
            imp.ignore(e.kind, e.layer)
            if role == "aux":
                aux_ignored[e.layer] += 1
            else:
                layer_ignored[e.kind] += 1
        elif role in ("engrave", "mark"):
            extra.append((role, e))
        else:
            pools[{"outline": "outline", "hole": "hole"}.get(role)].extend(e.geos)

    # --- нулевые и дубли
    els_by_group, closed_by_group = defaultdict(list), defaultdict(list)
    zero = 0
    for grp, geos in pools.items():
        seen = set()
        for g in geos:
            if _approx_len(g) <= ZERO_LEN:
                zero += 1
                continue
            key = _dedupe_key(g)
            if key in seen:
                rep.duplicates_removed += 1
                continue
            seen.add(key)
            if g[0] == "Circle" or (g[0] == "Bezier" and g[1]):
                closed_by_group[grp].append(g)
            else:
                els_by_group[grp].append(_El(g))

    # --- сшивка
    contours = []
    open_info = []
    chains_total = 0
    order = 0
    for grp in list(pools):
        for g in closed_by_group[grp]:
            contours.append(_C(g, grp, order))
            order += 1
        els = els_by_group[grp]
        for chain, closed in _stitch(els, tol):
            chains_total += 1
            geos = [_reversed(els[i].geo) if r else els[i].geo for i, r in chain]
            if closed:
                junc = [_d(_ends(geos[i])[1], _ends(geos[(i + 1) % len(geos)])[0]) for i in range(len(geos))]
                circ = _as_circle(geos, tol)
                if circ is not None:
                    geo = circ
                else:
                    geo = geomops.bezier_join(geos, tol=tol * 1.001 + 1e-9)
                    if geo is None or not geo[1]:
                        closed = False
                if closed:
                    rep.gaps_closed += sum(1 for x in junc if x > GAP_EXACT)
                    if geomops.area(geo) < 1e-9:
                        rep.warnings.append("контур нулевой площади из %d элем. пропущен" % len(chain))
                    else:
                        contours.append(_C(geo, grp, order))
                        order += 1
                    continue
            a, b = _ends(geos[0])[0], _ends(geos[-1])[1]
            open_info.append((len(chain), sum(_approx_len(g) for g in geos), _d(a, b), a, b))
    rep.open_contours = len(open_info)
    for n_el, ln, gap, a, b in open_info[:5]:
        rep.warnings.append("незамкнутая цепочка: %d элем., длина ~%.2f, зазор %.3f между (%.2f, %.2f) и (%.2f, %.2f)"
                            % (n_el, ln, gap, a[0], a[1], b[0], b[1]))
    if len(open_info) > 5:
        rep.warnings.append("…и ещё %d незамкнутых цепочек" % (len(open_info) - 5))

    # --- классификация
    outer = None
    holes = []
    n_islands = n_outside = 0
    if contours:
        forced = [c for c in contours if c.group == "outline"]
        outer = max(forced or contours, key=lambda c: c.area)
        cand = []
        for c in contours:
            if c is outer:
                continue
            if _inside(c, outer, tol):
                cand.append(c)
            else:
                n_outside += 1
                rep.warnings.append("контур вне внешнего (площадь %.2f, bbox %s) пропущен"
                                    % (c.area, tuple(round(v, 1) for v in c.bbox)))
        for c in cand:
            if any(h is not c and h.area > c.area and _inside(c, h, tol) for h in cand):
                n_islands += 1
                rep.warnings.append("остров внутри отверстия (площадь %.2f, bbox %s) пропущен"
                                    % (c.area, tuple(round(v, 1) for v in c.bbox)))
            else:
                holes.append(c)
        holes.sort(key=lambda c: c.order)
    else:
        rep.warnings.append("замкнутых контуров не найдено")

    # --- предупреждение об игнорированном
    ign_warn = layer_ignored + imp.unsupported          # служебные слои (aux) молча; остальное — warning
    if ign_warn:
        rep.warnings.append("игнорировано: " + ", ".join("%s×%d" % (t, n) for t, n in ign_warn.items()))

    # --- Part
    shapes = []
    outline_idx = -1
    offset = (0.0, 0.0)
    if outer is not None:
        bb = outer.bbox
        offset = (-bb[0], -bb[1])
        ogeo = geomops.to_bezier(outer.geo) if outer.geo[0] == "Circle" else outer.geo
        shapes.append(Shape(LAYER_OUTLINE, CH_CUT, geomops.transform(ogeo, dx=offset[0], dy=offset[1])))
        outline_idx = 0
        for h in holes:
            g = h.geo
            shapes.append(Shape(LAYER_HOLE, CH_CUT, geomops.transform(g, dx=offset[0], dy=offset[1])))
    rep.offset = offset
    for role, e in extra:
        if role == "mark" and e.kind in ("LINE", "ARC", "CIRCLE"):
            g = e.geos[0]
            g = ("Circle", g[1], g[2], _CIRCLE_A0, 0.0) if g[0] == "Circle" else g
        else:
            closed, pts = _flatten_ent(e)
            if len(pts) < 2:
                continue
            g = ("LwPoly", closed, pts)
        g = geomops.transform(g, dx=offset[0], dy=offset[1])
        if role == "mark":
            shapes.append(Shape(LAYER_MARK, CH_CUT, g))
        else:
            shapes.append(Shape(LAYER_ENGRAVE, CH_ENGRAVE, g))

    # --- самопересечения (только контуры реза)
    skipped_si = 0
    for i, s in enumerate(shapes):
        if s.layer in (LAYER_OUTLINE, LAYER_HOLE) and s.geo[0] == "Bezier":
            r = _self_intersects(s.geo)
            if r is None:
                skipped_si += 1
            elif r:
                rep.self_intersections.append(i)
    if skipped_si:
        rep.warnings.append("самопересечения не проверены у %d контуров (> %d сегментов)" % (skipped_si, MAX_SELFINT_SEGS))
    if rep.self_intersections:
        rep.warnings.append("самопересечения в контурах (индексы Part.shapes): %s" % rep.self_intersections)

    # --- тексты в координатах детали
    rep.texts = [(s, x + offset[0], y + offset[1], h, rot) for s, x, y, h, rot in imp.raw_texts]

    part_name = name or rep.meta.get("article") or os.path.splitext(os.path.basename(path))[0]
    part = Part(part_name, shapes, outline_idx, rep.meta.get("qty") or 1)

    st = rep.stats
    st.update(entities=len(ents), texts=len(rep.texts), chains=chains_total, contours=len(contours),
              holes=len(holes), circle_holes=sum(1 for h in holes if h.geo[0] == "Circle"),
              islands=n_islands, outside=n_outside, zero_length_removed=zero, aux_ignored=dict(aux_ignored),
              engrave=sum(1 for r, _ in extra if r == "engrave"), mark=sum(1 for r, _ in extra if r == "mark"),
              outline=outline_idx >= 0)
    if outer is not None:
        st.update(width=bb[2] - bb[0], height=bb[3] - bb[1], outline_area=outer.area)
    return part, rep


def batch_load(folder, pattern="*.dxf", skip_hidden=True, **kw):
    """Пакетная загрузка. -> (список Part только с найденным внешним контуром, {путь: Report}).
    Ошибка файла -> Report.error, пакет продолжается. skip_hidden: файлы '._*'/'.*' (метаданные macOS) пропускаются."""
    parts, reports = [], {}
    for fn in sorted(fnmatch.filter(os.listdir(str(folder)), pattern)):
        if skip_hidden and fn.startswith("."):
            continue
        p = os.path.join(str(folder), fn)
        if not os.path.isfile(p):
            continue
        try:
            part, rep = load_part(p, **kw)
            if part.outline >= 0:
                parts.append(part)
        except Exception as ex:
            rep = Report(path=p, meta=parse_filename(p), error="%s: %s" % (type(ex).__name__, ex))
        reports[p] = rep
    return parts, reports
