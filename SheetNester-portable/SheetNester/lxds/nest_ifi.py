"""Точный нестинг: IFI (Item-Fit-Polygon) + компактизация regret re-insertion.

Отличие от сеточного nesting.nest():
1. Позиции с плавающими координатами из «контактных» кандидатов (прижим к листу и рёбрам
   уже размещённых деталей) — нет квантования res, деталь встаёт вплотную к зазору gap.
2. Зазор проверяется ТОЧНЫМ расстоянием между контурами (shapely), а не клеточной маской
   с запасом d = gap/2 + res*sqrt(2)/2 → экономия до ~1.3 мм на каждом межцентровом шаге.
3. Отверстия деталей — реальные свободные зоны: деталь может встать внутрь отверстия соседа
   (проверка контур-к-контуру, mask-метод этого не умеет).
4. После initial packing — проходы компактизации: детали снимаются и переставляются в
   порядке ухудшения пригодности, пока высота занятой области листа уменьшается.

Интерфейс совместим с nesting.nest() (res принимается и игнорируется).
Требует: shapely>=2.0.
"""
import math
import time
from collections import namedtuple

import numpy as np

try:
    from shapely.geometry import Polygon
    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False

import geomops as go
from nesting import part_polys, _rot_angle

EPS = 1e-6


def _cols(P):
    """Колонки (xs, ys) из массива N×2 или кортежа колонок (результат _rot_angle)."""
    if isinstance(P, tuple):
        return np.asarray(P[0], float), np.asarray(P[1], float)
    P = np.asarray(P, float)
    return P[:, 0], P[:, 1]

# Placement: размещение детали; bbox — габарит в мировых координатах (для быстрого broad-phase)
Placement = namedtuple("Placement", "key part angle tx ty bbox")


def _rot_local(P, a):
    """Поворот вокруг (0,0) -> numpy-массив N×2. _rot_angle возвращает list[массив] на 1 поворот — берём [0]."""
    R = _rot_angle(P, a)
    return R[0] if isinstance(R, list) else R


class PartGeom:
    """Кэш геометрии детали во всех ориентациях; локальный габарит прижат к (0,0).

    solid = внешний контур МИНУС собственные отверстия (в них могут вставать другие детали),
    но ПЛЮС область отверстий остаётся свободной; буфер outer_buf задан на gap/2 + запас на
    дискретизацию дуг — расстояние между outer_buf >= gap <=> расстояние между истинными
    контурами >= gap (аппроксимация хордами «вписана», отсюда запас chord_pad).
    """

    def __init__(self, part, angles, step=0.75, gap=None):
        self.part = part
        polys = part_polys(part, step)
        self.chord_pad = (step * step / 8.0) if gap is not None else 0.0
        e = (gap / 2.0 + self.chord_pad) if gap is not None else 0.0
        self.orients = []
        for a in angles:
            R = [_rot_local(P, a) for P in polys]
            xs0 = min(_cols(p)[0].min() for p in R)
            ys0 = min(_cols(p)[1].min() for p in R)
            N = [tuple(np.column_stack([xs - xs0, ys - ys0])) for xs, ys in map(_cols, R)]
            outer = _mkpoly(N[0])
            holes = [_mkpoly(h) for h in N[1:]]
            w = float(max(_cols(p)[0].max() for p in N))
            h = float(max(_cols(p)[1].max() for p in N))
            solid = outer.difference(_union(holes)) if holes else outer
            self.orients.append(dict(angle=a, w=w, h=h, solid=solid,
                                     outer_buf=outer.buffer(e)))

    def world(self, oi, tx, ty):
        o = self.orients[oi]
        return _shift(o["solid"], tx, ty), _shift(o["outer_buf"], tx, ty)


def _mkpoly(P):
    # P — кортеж колонок (xs, ys) или массив N×2
    if isinstance(P, tuple):
        xs, ys = P
    else:
        xs, ys = P[:, 0], P[:, 1]
    p = Polygon(list(zip(map(float, xs), map(float, ys))))
    if not p.is_valid:
        p = p.buffer(0)
    return p


def _union(polys):
    from shapely.ops import unary_union
    return unary_union(polys)


def _shift(g, tx, ty):
    return g if (tx == 0 and ty == 0) else _translate(g, tx, ty)


def _translate(g, tx, ty):
    from shapely.affinity import translate
    return translate(g, xoff=tx, yoff=ty)


def _bbox_overlap(a, b, slack=0.0):
    return (a[0] - slack < b[2] and b[0] - slack < a[2] and
            a[1] - slack < b[3] and b[1] - slack < a[3])


def make_placement(key, pg, oi, tx, ty):
    o = pg.orients[oi]
    return Placement(key, pg.part, o["angle"], tx, ty, (tx, ty, tx + o["w"], ty + o["h"]))


# ───────────────────────── кандидаты позиций ─────────────────────────

def _candidate_offsets(pg, oi, placed, sheet_w, sheet_h, margin, gap):
    """Точки (tx, ty) левых-нижних углов габарита: прижим к листу / рёбрам соседей по X и Y.

    Любая BLF-оптимальная позиция имеет деталь, прижатую слева или снизу к листу/соседу
    (или справа/сверху — для заполнения выемок), поэтому набор полон для критерия (top,left).
    """
    o = pg.orients[oi]
    xs = {margin, sheet_w - margin - o["w"]}
    ys = {margin, sheet_h - margin - o["h"]}
    for pl in placed:
        bx = pl.bbox
        xs.add(bx[0] - o["w"] - gap)     # мы слева от соседа (вплотную через зазор)
        xs.add(bx[2] + gap)              # мы справа от соседа
        ys.add(bx[1] - o["h"] - gap)
        ys.add(bx[3] + gap)
    cands = []
    for x in sorted(xs):
        if x < margin - EPS or x + o["w"] > sheet_w - margin + EPS:
            continue
        for y in sorted(ys):
            if y < margin - EPS or y + o["h"] > sheet_h - margin + EPS:
                continue
            cands.append((x, y))
    return cands


def clearance_ok(wpair, bx, placed, gap, ko_polys):
    """Точная проверка: solid не пересекается с чужими solid/зонами запрета;
    расстояние outer_buf >= gap (гарантия зазора между истинными контурами)."""
    ws, wb = wpair
    for pl in placed:
        if not _bbox_overlap(bx, pl.bbox, -gap):
            continue
        os_, ob = pl_wp(pl)
        if ws.intersects(os_):
            return False
        if wb.distance(ob) < gap - EPS:
            return False
    for kp in ko_polys:
        if ws.intersects(kp):
            return False
    return True


# Кэш мировых пар (solid, outer_buf) размещений по id Placement
_WP_CACHE = {}


def pl_wp(pl):
    wp = _WP_CACHE.get(id(pl))
    if wp is None:
        pg = _PG_BY_PART_ID[id(pl.part)]
        oi = next(i for i, o in enumerate(pg.orients) if abs(o["angle"] - pl.angle) < 1e-12)
        wp = pg.world(oi, pl.tx, pl.ty)
        _WP_CACHE[id(pl)] = wp
    return wp


_PG_BY_PART_ID = {}


# ───────────────────────── основной алгоритм ─────────────────────────

def nest_ifi(items, sheet_w, sheet_h, margin=10.0, gap=5.0, res=None,
             rotations=(0, 1, 2, 3), progress=None, keepouts=None,
             priority="area", rates=None, free_angles=(), place="blf",
             max_passes=6, time_limit=None):
    """items: [(key, part, qty)] -> (sheets, unplaced); формат как у nesting.nest()."""
    if not HAS_SHAPELY:
        raise RuntimeError("nest_ifi требует shapely>=2.0 (pip install shapely)")
    global _WP_CACHE, _PG_BY_PART_ID
    _WP_CACHE, _PG_BY_PART_ID = {}, {}

    t_start = time.perf_counter()
    angles = [k * math.pi / 2 for k in rotations] + [math.radians(a) for a in free_angles]
    aw, ah = sheet_w - 2 * margin, sheet_h - 2 * margin

    ko_polys = []
    if keepouts:
        e = gap / 2.0
        for (kx, ky, kw, kh) in keepouts:
            ko_polys.append(Polygon([(kx - e, ky - e), (kx + kw + e, ky - e),
                                     (kx + kw + e, ky + kh + e), (kx - e, ky + kh + e)]))

    types, unplaced = [], []
    for key, part, qty in items:
        if qty <= 0:
            continue
        x0, y0, x1, y1 = go.bbox(part.shapes[part.outline].geo)
        w, h = x1 - x0, y1 - y0
        cardinal_ok = (w <= aw and h <= ah) or (any(k % 2 for k in rotations) and h <= aw and w <= ah)
        if not cardinal_ok and not free_angles:
            unplaced.append((key, qty))          # больше листа — маску/полигон не строим
            continue
        area = abs(go.signed_area(part.shapes[part.outline].geo))
        types.append((area, key, part, qty))
    types.sort(key=lambda t: -t[0])

    queue = []
    for _area, key, part, qty in types:
        pg = PartGeom(part, angles, gap=gap)
        _PG_BY_PART_ID[id(part)] = pg
        for _q in range(qty):
            queue.append((key, part, pg))

    total = len(queue)
    done = 0

    def best_place(key, pg, placed):
        """Лучшая BLF-позиция (минимум верхнего края, затем левого) среди ориентаций/кандидатов."""
        best = None
        for oi, o in enumerate(pg.orients):
            if o["w"] > aw + EPS or o["h"] > ah + EPS:
                continue
            for (tx, ty) in _candidate_offsets(pg, oi, placed, sheet_w, sheet_h, margin, gap):
                bx = (tx, ty, tx + o["w"], ty + o["h"])
                wp = pg.world(oi, tx, ty)
                if not clearance_ok(wp, bx, placed, gap, ko_polys):
                    continue
                cand = (bx[3], bx[0])
                if best is None or cand < best[0]:
                    best = (cand, make_placement(key, pg, oi, tx, ty), wp)
        return best

    sheets = []
    for idx, (key, part, pg) in enumerate(queue):
        placed_ok = False
        for sheet in sheets:
            b = best_place(key, pg, sheet)
            if b is not None:
                sheet.append(b[1])
                _WP_CACHE[id(b[1])] = b[2]
                placed_ok = True
                break
        if not placed_ok:
            sheet = []
            b = best_place(key, pg, sheet)
            if b is None:
                unplaced.append((key, 1))
            else:
                sheet.append(b[1])
                _WP_CACHE[id(b[1])] = b[2]
                sheets.append(sheet)
        done += 1
        if progress:
            progress(done, total)
        if time_limit and time.perf_counter() - t_start > time_limit:
            rest = {}
            for k2, _p2, _g2 in queue[idx + 1:]:
                rest[k2] = rest.get(k2, 0) + 1
            unplaced.extend(rest.items())
            break

    # ── Compact pass: сняли все детали листа, ставим заново в худшем порядке сначала ──
    def occupied_height(sheet):
        return max((pl.bbox[3] for pl in sheet), default=0.0)

    def _IDPG(part):
        return _PG_BY_PART_ID[id(part)]

    def reinsert_order(pl):
        # крупные и «верхние» снимаются/возвращаются позже мелких и нижних
        return (-pl.bbox[3], -abs(go.signed_area(pl.part.shapes[pl.part.outline].geo)))

    def compact(sheet):
        base_top = occupied_height(sheet)
        for _pass in range(max_passes):
            if time_limit and time.perf_counter() - t_start > time_limit:
                break
            items_pl = list(sheet)
            # пробуем несколько порядков повторной установки; лучший — с минимальной высотой
            orders = [sorted(items_pl, key=reinsert_order),
                      sorted(items_pl, key=lambda pl: -reinsert_order(pl)[1]),
                      sorted(items_pl, key=reinsert_order, reverse=True)]
            best_work, best_top = None, base_top
            for order in orders:
                work, failed = [], []
                for pl in order:
                    b = best_place(pl.key, _IDPG(pl.part), work)
                    (work if b else failed).append(b[1] if b else pl)
                    if b:
                        _WP_CACHE[id(b[1])] = b[2]
                ok = True
                for pl in failed:                 # не влезли в начало очереди — докидываем в конец
                    b = best_place(pl.key, _IDPG(pl.part), work)
                    if b is None:
                        ok = False
                        break
                    work.append(b[1])
                    _WP_CACHE[id(b[1])] = b[2]
                if not ok or len(work) != len(items_pl):
                    continue
                top = occupied_height(work)
                if top < best_top - 0.05:
                    best_work, best_top = work, top
            if best_work is None:
                break
            sheet[:] = best_work
        return best_top < base_top

    for sheet in sheets:
        if len(sheet) > 1:
            compact(sheet)

    result = [[(pl.key, pl.part, pl.angle, (pl.tx, pl.ty)) for pl in s] for s in sheets]
    return result, unplaced
