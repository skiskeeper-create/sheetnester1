"""Геометрические операции над `geo`-кортежами (формат — model.py / geometry.py; мм, рад, чистая stdlib).

  ("Line", (x1,y1), (x2,y2))            ("Arc", (cx,cy), r, start, sweep)   # против ч.с.
  ("Circle", (cx,cy), r, a0, a1)        ("LwPoly", closed, [(x,y,bulge),..])  # bulge только 0
  ("Bezier", closed, [[P0,P1,P2,P3],..])  # кубические сегменты, P3(i)==P0(i+1)

Публичное: transform, to_bezier, bezier_join, seg_length, length, cum_lengths, point_at, tangent_at,
bbox, area, signed_area, orientation.
"""
import math
from bisect import bisect_right

# --- узлы/веса Гаусса-Лежандра 5 точек на [-1, 1] ---
_GL_X = (0.0, 0.5384693101056831, -0.5384693101056831, 0.9061798459386640, -0.9061798459386640)
_GL_W = (0.5688888888888889, 0.4786286704993665, 0.4786286704993665, 0.2369268850561891, 0.2369268850561891)

ARC_MAX_DEG = 30.0   # макс. угол одного Безье-сегмента при to_bezier(Arc): 30° -> отклонение < 4e-5 мм на r=100


# ---------------------------------------------------------------- Безье: примитивы
def _bz(seg, t):
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = seg
    u = 1.0 - t
    a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return (a * x0 + b * x1 + c * x2 + d * x3, a * y0 + b * y1 + c * y2 + d * y3)


def _dbz(seg, t):
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = seg
    u = 1.0 - t
    a, b, c = 3 * u * u, 6 * u * t, 3 * t * t
    return (a * (x1 - x0) + b * (x2 - x1) + c * (x3 - x2), a * (y1 - y0) + b * (y2 - y1) + c * (y3 - y2))


def _speed(seg, t):
    dx, dy = _dbz(seg, t)
    return math.hypot(dx, dy)


def _unit(v):
    n = math.hypot(v[0], v[1])
    return (v[0] / n, v[1] / n) if n > 1e-12 else None


def _start_dir(seg):
    """Единичная касательная в начале сегмента (при вырожденных P1/P2 — по следующим точкам)."""
    p0 = seg[0]
    for q in (seg[1], seg[2], seg[3]):
        u = _unit((q[0] - p0[0], q[1] - p0[1]))
        if u:
            return u
    return None


def _end_dir(seg):
    p3 = seg[3]
    for q in (seg[2], seg[1], seg[0]):
        u = _unit((p3[0] - q[0], p3[1] - q[1]))
        if u:
            return u
    return None


def _dir(seg, t):
    """Единичная касательная в точке параметра t (None для полностью вырожденного сегмента)."""
    if t <= 0.0:
        return _start_dir(seg)
    if t >= 1.0:
        return _end_dir(seg)
    u = _unit(_dbz(seg, t))
    if u:
        return u
    return _unit((seg[3][0] - seg[0][0], seg[3][1] - seg[0][1]))


def seg_length(seg, n=200):
    """Длина кубического Безье-сегмента: составное правило Симпсона, n интервалов (округляется до чётного)."""
    n = max(2, int(n))
    if n % 2:
        n += 1
    h = 1.0 / n
    acc = _speed(seg, 0.0) + _speed(seg, 1.0)
    for i in range(1, n):
        acc += (4 if i % 2 else 2) * _speed(seg, i * h)
    return acc * h / 3.0


def _plen(seg, t, panels=8):
    """Длина части сегмента [0, t]: Гаусс-Лежандр 5 точек на панель (для обращения s -> t)."""
    if t <= 0.0:
        return 0.0
    m = max(1, int(math.ceil(panels * t)))
    w = t / m
    acc = 0.0
    for k in range(m):
        c = (k + 0.5) * w
        for x, wt in zip(_GL_X, _GL_W):
            acc += wt * _speed(seg, c + 0.5 * w * x)
    return acc * 0.5 * w


def _t_at(seg, sl, total):
    """Параметр t, при котором длина дуги от начала сегмента равна sl (Ньютон + бисекция)."""
    if sl <= 0.0:
        return 0.0
    if sl >= total:
        return 1.0
    lo, hi, t = 0.0, 1.0, sl / total
    for _ in range(60):
        f = _plen(seg, t) - sl
        if abs(f) < 1e-12:
            break
        if f > 0:
            hi = t
        else:
            lo = t
        sp = _speed(seg, t)
        tn = t - f / sp if sp > 1e-14 else None
        if tn is None or not (lo < tn < hi):
            tn = 0.5 * (lo + hi)
        t = tn
    return t


# ---------------------------------------------------------------- transform
def transform(geo, angle=0.0, mirror=False, dx=0.0, dy=0.0):
    """Зеркало (x -> -x, если mirror) -> поворот на angle (рад, против ч.с.) вокруг (0,0) -> сдвиг (dx,dy).

    Arc при зеркале: те же точки, sweep остаётся > 0 (start' = pi - start - sweep).
    Circle: центр, a0/a1 не меняются. LwPoly: bulge != 0 -> ValueError; порядок вершин сохраняется.
    Bezier при зеркале: порядок сегментов и точек разворачивается (обход контура сохраняется, цепочка
    непрерывна; у замкнутого контура начальная точка остаётся начальной).
    """
    ca, sa = math.cos(angle), math.sin(angle)

    def P(p):
        x, y = p[0], p[1]
        if mirror:
            x = -x
        return (ca * x - sa * y + dx, sa * x + ca * y + dy)

    k = geo[0]
    if k == "Line":
        return ("Line", P(geo[1]), P(geo[2]))
    if k == "Arc":
        _, c, r, st, sw = geo
        if mirror:
            if sw >= 0:
                st = math.pi - st - sw
            else:
                st, sw = math.pi - st, -sw
        return ("Arc", P(c), r, st + angle, sw)
    if k == "Circle":
        return ("Circle", P(geo[1]), geo[2], geo[3], geo[4])
    if k == "LwPoly":
        _, closed, vs = geo
        out = []
        for x, y, b in vs:
            if b != 0:
                raise ValueError("LwPoly с ненулевым bulge не поддерживается")
            out.append(P((x, y)) + (b,))
        return ("LwPoly", closed, out)
    if k == "Bezier":
        _, closed, segs = geo
        if mirror:
            return ("Bezier", closed, [[P(q) for q in reversed(s)] for s in reversed(segs)])
        return ("Bezier", closed, [[P(q) for q in s] for s in segs])
    raise ValueError(k)


# ---------------------------------------------------------------- to_bezier / bezier_join
def _line_seg(a, b):
    ax, ay = a
    bx, by = b
    return [(ax, ay), (ax + (bx - ax) / 3.0, ay + (by - ay) / 3.0),
            (ax + 2.0 * (bx - ax) / 3.0, ay + 2.0 * (by - ay) / 3.0), (bx, by)]


def _arc_segs(c, r, start, sweep, max_deg, kscale=1.0):
    if r <= 0 or sweep == 0:
        raise ValueError("Arc/Circle: нужны r > 0 и ненулевой sweep")
    n = max(1, int(math.ceil(abs(sweep) / math.radians(max_deg) - 1e-9)))
    th = sweep / n
    k = 4.0 / 3.0 * math.tan(th / 4.0) * kscale
    cx, cy = c
    pts = [(cx + r * math.cos(start + i * th), cy + r * math.sin(start + i * th)) for i in range(n + 1)]
    segs = []
    for i in range(n):
        a0, a1 = start + i * th, start + (i + 1) * th
        p0, p3 = pts[i], pts[i + 1]
        p1 = (p0[0] - k * r * math.sin(a0), p0[1] + k * r * math.cos(a0))
        p2 = (p3[0] + k * r * math.sin(a1), p3[1] - k * r * math.cos(a1))
        segs.append([p0, p1, p2, p3])
    return segs


def to_bezier(geo, arc_max_deg=ARC_MAX_DEG, circle_segs=4):
    """geo -> ("Bezier", closed, segs). Line — 1 вырожденный сегмент (точки на 1/3, 2/3); Arc — сегменты
    <= arc_max_deg (по умолч. 30°, погрешность < 4e-5 мм на r=100); Circle — circle_segs (4) сегментов от угла 0,
    closed=True (4 сегмента с оптимальным k=0.55192: радиальная ошибка ~2e-4*r (0.02 мм на r=100), но длина/площадь < 1e-4 отн.;
    для радиальной ошибки 1e-4 мм на r=100 нужно circle_segs >= 12);
    LwPoly без bulge — по сегменту на ребро (closed добавляет замыкающее ребро). Bezier возвращается копией."""
    k = geo[0]
    if k == "Line":
        return ("Bezier", False, [_line_seg(geo[1], geo[2])])
    if k == "Arc":
        _, c, r, st, sw = geo
        return ("Bezier", False, _arc_segs(c, r, st, sw, arc_max_deg))
    if k == "Circle":
        _, c, r, _a0, _a1 = geo
        ks = 0.5519150244935106 / 0.5522847498307936 if circle_segs == 4 else 1.0   # оптимальный k для 90°: длина и площадь ~точные
        return ("Bezier", True, _arc_segs(c, r, 0.0, 2.0 * math.pi, 360.0 / circle_segs, ks))
    if k == "LwPoly":
        _, closed, vs = geo
        if any(b != 0 for _, _, b in vs):
            raise ValueError("LwPoly с ненулевым bulge не поддерживается")
        pts = [(x, y) for x, y, _ in vs]
        if len(pts) < 2:
            raise ValueError("LwPoly: меньше двух вершин")
        if closed:
            pts.append(pts[0])
        segs = [_line_seg(a, b) for a, b in zip(pts, pts[1:]) if math.hypot(b[0] - a[0], b[1] - a[1]) > 1e-12]
        if not segs:
            raise ValueError("LwPoly: нулевая длина")
        return ("Bezier", bool(closed), segs)
    if k == "Bezier":
        return ("Bezier", geo[1], [list(s) for s in geo[2]])
    raise ValueError(k)


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def bezier_join(items, tol=1e-6):
    """Склейка упорядоченной цепочки примитивов (Line/Arc/LwPoly/Bezier) в один Bezier.
    Конец каждого должен совпадать с началом следующего в пределах tol, иначе None. closed=True, если конец
    цепочки совпал с началом. Ориентация элементов не меняется; стыки приводятся к точному P3(i)==P0(i+1)."""
    if not items:
        return None
    segs = []
    for g in items:
        b = to_bezier(g)
        s = b[2]
        for i in range(len(s) - 1):
            if _dist(s[i][3], s[i + 1][0]) > tol:      # внутренний разрыв элемента
                return None
        if segs and _dist(segs[-1][3], s[0][0]) > tol:
            return None
        for i, q in enumerate(s):
            q = list(q)
            if i == 0 and segs:
                q[0] = segs[-1][3]
            segs.append(q)
    closed = _dist(segs[-1][3], segs[0][0]) <= tol
    if closed:
        segs[-1][3] = segs[0][0]
    return ("Bezier", closed, segs)


# ---------------------------------------------------------------- длины
def length(geo):
    k = geo[0]
    if k == "Line":
        return _dist(geo[1], geo[2])
    if k == "Arc":
        return geo[2] * abs(geo[4])
    if k == "Circle":
        return 2.0 * math.pi * geo[2]
    if k == "LwPoly":
        if any(b != 0 for _, _, b in geo[2]):
            raise ValueError("LwPoly с ненулевым bulge не поддерживается")
        pts = [(x, y) for x, y, _ in geo[2]]
        if geo[1] and pts:
            pts.append(pts[0])
        return sum(_dist(a, b) for a, b in zip(pts, pts[1:]))
    if k == "Bezier":
        return sum(seg_length(s) for s in geo[2])
    raise ValueError(k)


def cum_lengths(bez):
    """[0, s1, s2, ..., периметр]: длина дуги до конца каждого сегмента Bezier (n+1 значение)."""
    cum = [0.0]
    for s in bez[2]:
        cum.append(cum[-1] + seg_length(s))
    return cum


def _locate(bez, s):
    """(индекс сегмента, локальная длина, cum, периметр) для s (closed: s mod периметр; open: clamp)."""
    segs = bez[2]
    cum = cum_lengths(bez)
    L = cum[-1]
    if L <= 0:
        raise ValueError("Bezier нулевой длины")
    s = s % L if bez[1] else min(max(s, 0.0), L)
    i = min(bisect_right(cum, s) - 1, len(segs) - 1)
    while i > 0 and cum[i + 1] - cum[i] <= 0:
        i -= 1
    return i, s - cum[i], cum, L


def param_at(bez, s):
    """Длина дуги s (мм) -> позиция в формате CypCut: номер сегмента + параметр t в нём (MicroConnect.Pos,
    LeadParam.pos). Подтверждено по 1.lxds: мост 11.673 = сегмент 11, t=0.673; врезка после него = +1мм/39мм."""
    i, sl, _, _ = _locate(bez, s)
    seg = bez[2][i]
    return i + _t_at(seg, sl, _plen(seg, 1.0))


def s_at_param(bez, p):
    """Обратное к param_at: позиция CypCut (сегмент + t) -> длина дуги от начальной точки, мм."""
    segs = bez[2]
    i = min(int(p), len(segs) - 1)
    return cum_lengths(bez)[i] + _plen(segs[i], p - i)


def point_at(bez, s):
    """Точка на Bezier на длине дуги s от начальной (closed: s mod периметр; open: s ограничено [0, L])."""
    i, sl, cum, _ = _locate(bez, s)
    seg = bez[2][i]
    return _bz(seg, _t_at(seg, sl, _plen(seg, 1.0)))


def tangent_at(bez, s):
    """Единичная касательная в точке длины дуги s. В точке стыка сегментов — касательная следующего сегмента."""
    i, sl, cum, _ = _locate(bez, s)
    seg = bez[2][i]
    return _dir(seg, _t_at(seg, sl, _plen(seg, 1.0)))


def nearest_s(geo, x, y, refine=50):
    """Ближайшая к точке (x, y) точка контура -> (s — длина дуги от начала, мм; расстояние, мм).
    Выбор точки врезки кликом (ТЗ 7.3): равномерная сетка по длине дуги (прямая оценка Безье-сегментов,
    без повторного _locate) + тернарный поиск. Точность ~0.01 мм."""
    bez = to_bezier(geo)
    segs = bez[2]
    cum = cum_lengths(bez)
    total = cum[-1]
    if total <= 0:
        raise ValueError("контур нулевой длины")

    def sample(i, t):
        seg = segs[i]
        return _bz(seg, t)

    best = None                       # (d2, s)
    n = max(64, min(20000, int(total / 0.25)))
    for i, seg in enumerate(segs):
        sl = cum[i + 1] - cum[i]
        if sl <= 0:
            continue
        k = max(2, int(n * sl / total))
        for j in range(k + 1):
            t = j / k
            px, py = sample(i, t)
            d2 = (px - x) ** 2 + (py - y) ** 2
            if best is None or d2 < best[0]:
                best = (d2, cum[i] + t * sl)
    lo, hi = max(0.0, best[1] - total / n), min(total, best[1] + total / n)

    def d2_at(s):
        i = min(bisect_right(cum, s) - 1, len(segs) - 1)
        while i > 0 and cum[i + 1] - cum[i] <= 0:
            i -= 1
        px, py = sample(i, _t_at(segs[i], s - cum[i], _plen(segs[i], 1.0)))
        return (px - x) ** 2 + (py - y) ** 2

    for _ in range(refine):
        m1 = lo + (hi - lo) / 3.0
        m2 = hi - (hi - lo) / 3.0
        if d2_at(m1) < d2_at(m2):
            hi = m2
        else:
            lo = m1
    s = (lo + hi) / 2.0
    return s, math.sqrt(d2_at(s))


# ---------------------------------------------------------------- bbox / area / orientation
def _axis_extrema(p0, p1, p2, p3):
    """Параметры t в (0,1), где производная кубической координаты равна нулю."""
    a = -p0 + 3 * p1 - 3 * p2 + p3
    b = 2.0 * (p0 - 2 * p1 + p2)
    c = p1 - p0
    ts = []
    if abs(a) < 1e-14:
        if abs(b) > 1e-14:
            ts.append(-c / b)
    else:
        d = b * b - 4 * a * c
        if d >= 0:
            sq = math.sqrt(d)
            ts += [(-b + sq) / (2 * a), (-b - sq) / (2 * a)]
    return [t for t in ts if 0.0 < t < 1.0]


def bbox(geo):
    """(xmin, ymin, xmax, ymax) по самой кривой (Безье — по корням производной, не по контрольным точкам)."""
    k = geo[0]
    if k == "Line":
        pts = [geo[1], geo[2]]
    elif k == "Circle":
        (x, y), r = geo[1], geo[2]
        pts = [(x - r, y - r), (x + r, y + r)]
    elif k == "Arc":
        (cx, cy), r, st, sw = geo[1], geo[2], geo[3], geo[4]
        a0, a1 = (st, st + sw) if sw >= 0 else (st + sw, st)
        angs = [a0, a1]
        for base in (0.0, math.pi / 2, math.pi, 1.5 * math.pi):
            n = math.ceil((a0 - base) / (2 * math.pi) - 1e-12)
            if base + 2 * math.pi * n <= a1 + 1e-12:
                angs.append(base + 2 * math.pi * n)
        pts = [(cx + r * math.cos(a), cy + r * math.sin(a)) for a in angs]
    elif k == "LwPoly":
        if any(b != 0 for _, _, b in geo[2]):
            raise ValueError("LwPoly с ненулевым bulge не поддерживается")
        pts = [(x, y) for x, y, _ in geo[2]]
    elif k == "Bezier":
        pts = []
        for s in geo[2]:
            pts += [s[0], s[3]]
            ts = set(_axis_extrema(*(q[0] for q in s))) | set(_axis_extrema(*(q[1] for q in s)))
            pts += [_bz(s, t) for t in ts]
    else:
        raise ValueError(k)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def signed_area(geo):
    """Площадь со знаком (+ против ч.с.) замкнутого Bezier/LwPoly/Circle. Безье — точно (формула Грина,
    подынтегральная функция степени 5 -> Гаусс-5 точен). Незамкнутый -> ValueError."""
    k = geo[0]
    if k == "Circle":
        return math.pi * geo[2] ** 2
    if k == "LwPoly":
        if not geo[1]:
            raise ValueError("площадь незамкнутого контура")
        if any(b != 0 for _, _, b in geo[2]):
            raise ValueError("LwPoly с ненулевым bulge не поддерживается")
        pts = [(x, y) for x, y, _ in geo[2]]
        return 0.5 * sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))
    if k == "Bezier":
        if not geo[1]:
            raise ValueError("площадь незамкнутого контура")
        acc = 0.0
        for s in geo[2]:
            for x, wt in zip(_GL_X, _GL_W):
                t = 0.5 * (x + 1.0)
                px, py = _bz(s, t)
                dx, dy = _dbz(s, t)
                acc += 0.5 * wt * 0.5 * (px * dy - py * dx)
        a, b = geo[2][-1][3], geo[2][0][0]      # если цепочка не сомкнута — замыкающая прямая
        acc += 0.5 * (a[0] * b[1] - b[0] * a[1])
        return acc
    raise ValueError("площадь: неподдерживаемый тип " + str(k))


def area(geo):
    return abs(signed_area(geo))


def orientation(geo):
    """+1 — обход против ч.с., -1 — по ч.с. (замкнутый контур)."""
    return 1 if signed_area(geo) >= 0 else -1
