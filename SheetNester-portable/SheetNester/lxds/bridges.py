"""Расстановка мостов (микроперемычек) по геометрии замкнутого Bezier-контура.

Мост (model.Bridge): pos — длина дуги контура от его начальной точки, size — ширина, мм.
Мост должен лежать целиком на ПРЯМОМ участке (straight_intervals) и не пересекаться с другими мостами.
"""
import math

import geomops as go
import model

# Занимает ли мост [pos, pos+size] ("start") или центрирован на pos ("center") — в 1.lxds НЕ установлено.
# Принято "start". Все функции модуля учитывают константу через _to_start/_to_pos.
BRIDGE_ANCHOR = "start"

STRAIGHT_TOL = 1e-3      # мм: макс. отклонение контрольных точек от прямой, чтобы сегмент считался прямым
JOINT_TOL_DEG = 0.5      # град: макс. излом на стыке двух прямых сегментов, чтобы считать их одной прямой
_EPS = 1e-9


def _to_start(pos, size):
    return pos if BRIDGE_ANCHOR == "start" else pos - size / 2.0


def _to_pos(start, size):
    return start if BRIDGE_ANCHOR == "start" else start + size / 2.0


def _wrap(a):
    """Угол -> (-pi, pi]."""
    a = math.fmod(a + math.pi, 2 * math.pi)
    if a <= 0:
        a += 2 * math.pi
    return a - math.pi


def _ang(u):
    return math.atan2(u[1], u[0])


def _is_straight(seg):
    (x0, y0), p1, p2, (x3, y3) = seg
    dx, dy = x3 - x0, y3 - y0
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return False
    ux, uy = dx / L, dy / L
    for px, py in (p1, p2):
        rx, ry = px - x0, py - y0
        if abs(rx * uy - ry * ux) > STRAIGHT_TOL:
            return False
        if not (-STRAIGHT_TOL <= rx * ux + ry * uy <= L + STRAIGHT_TOL):
            return False
    return True


def _internal_turn(seg, n=16):
    """Знаковый суммарный поворот касательной вдоль сегмента, рад."""
    prev = go._dir(seg, 0.0)
    tot = 0.0
    for i in range(1, n + 1):
        cur = go._dir(seg, i / n)
        if prev and cur:
            tot += _wrap(_ang(cur) - _ang(prev))
        prev = cur or prev
    return tot


def _prep(bez):
    """Разбор контура: (S, joints, P, cyc). S — невырожденные сегменты (dict s0,s1,seg,straight,d0,d1,turn);
    joints[j] — знаковый поворот на стыке S[j-1]|S[j] (j=0 — шов замкнутого контура); cyc — контур сомкнут."""
    if bez[0] != "Bezier":
        raise ValueError("нужен Bezier")
    cum = go.cum_lengths(bez)
    P = cum[-1]
    S = []
    for i, seg in enumerate(bez[2]):
        if cum[i + 1] - cum[i] < _EPS:
            continue
        S.append(dict(s0=cum[i], s1=cum[i + 1], seg=seg, straight=_is_straight(seg),
                      d0=_ang(go._start_dir(seg)), d1=_ang(go._end_dir(seg)), turn=_internal_turn(seg)))
    if not S:
        raise ValueError("контур нулевой длины")
    cyc = bool(bez[1]) and go._dist(S[-1]["seg"][3], S[0]["seg"][0]) <= STRAIGHT_TOL
    joints = [_wrap(S[j]["d0"] - S[j - 1]["d1"]) if (j > 0 or cyc) else 0.0 for j in range(len(S))]
    return S, joints, P, cyc


# ---------------------------------------------------------------- прямые участки
def straight_intervals(bez, joint_tol_deg=JOINT_TOL_DEG):
    """Прямые участки контура: [(lo, hi)] по длине дуги. Соседние прямые сегменты объединяются, если излом на
    стыке <= joint_tol_deg и они лежат на одной прямой (STRAIGHT_TOL). У замкнутого контура участок через шов
    имеет hi > периметра (lo < периметра). Отсортировано по lo."""
    S, joints, P, cyc = _prep(bez)
    m = len(S)
    tol = math.radians(joint_tol_deg)

    def cont(j):
        return (j > 0 or cyc) and S[j - 1]["straight"] and S[j]["straight"] and abs(joints[j]) <= tol

    k = 0
    if cyc:
        k = next((j for j in range(m) if not cont(j)), 0)
    ivs, cur = [], None
    for j in range(m):
        i = (k + j) % m
        sg = S[i]
        if not sg["straight"]:
            cur = None
            continue
        off = P if (cyc and i < k) else 0.0
        seg = sg["seg"]
        if cur is not None and cont(i):
            ux, uy, ax, ay = cur[2]
            if all(abs((q[0] - ax) * uy - (q[1] - ay) * ux) <= STRAIGHT_TOL for q in seg):
                cur[1] = sg["s1"] + off
                continue
        p0, p3 = seg[0], seg[3]
        L = math.hypot(p3[0] - p0[0], p3[1] - p0[1])
        cur = [sg["s0"] + off, sg["s1"] + off, ((p3[0] - p0[0]) / L, (p3[1] - p0[1]) / L, p0[0], p0[1])]
        ivs.append(cur)
    return sorted((c[0], c[1]) for c in ivs)


def _shifts(bez, P):
    return (-P, 0.0, P) if bez[1] else (0.0,)


def _fits(ivs, shifts, a, b):
    return any(lo + sh - _EPS <= a and b <= hi + sh + _EPS for lo, hi in ivs for sh in shifts)


# ---------------------------------------------------------------- углы
def find_corners(bez, min_turn_deg=45.0):
    """Углы контура -> список dict(s_start, s_end, s_mid, turn_deg, point, sign), отсортирован по s_mid.

    Острый угол — стык с изломом касательной >= min_turn (s_start == s_end == позиция стыка).
    Скруглённый — серия подряд идущих непрямых сегментов без острых стыков, суммарный поворот >= min_turn;
    если серия перекрывает шов (начало контура внутри скругления), s_start > s_end (позиции mod периметр).
    Полностью гладкий замкнутый контур (круг, эллипс) углов не имеет. turn_deg > 0; sign = +1 (левый
    поворот, против ч.с.) / -1. point — точка на контуре в s_mid."""
    S, joints, P, cyc = _prep(bez)
    m = len(S)
    thr = math.radians(min_turn_deg)
    out = []

    def emit(s0, s1, sm, turn):
        sm %= P
        out.append(dict(s_start=s0 % P, s_end=s1 % P, s_mid=sm, turn_deg=math.degrees(abs(turn)),
                        point=go.point_at(bez, sm), sign=1 if turn >= 0 else -1))

    for j in range(m):
        if (j > 0 or cyc) and abs(joints[j]) >= thr:
            s = S[j]["s0"] if j > 0 else 0.0
            emit(s, s, s, joints[j])

    def boundary(j):
        if j == 0 and not cyc:
            return True
        return S[j - 1]["straight"] or S[j]["straight"] or abs(joints[j]) >= thr

    k = 0
    if cyc:
        k = next((j for j in range(m) if boundary(j)), None)
    if k is not None:
        run = None
        runs = []
        for n in range(m):
            i = (k + n) % m
            if S[i]["straight"]:
                run = None
                continue
            off = P if (cyc and i < k) else 0.0
            if run is None or boundary(i):
                run = dict(s0=S[i]["s0"] + off, s1=S[i]["s1"] + off, turn=S[i]["turn"])
                runs.append(run)
            else:
                run["s1"] = S[i]["s1"] + off
                run["turn"] += joints[i] + S[i]["turn"]
        for r in runs:
            if abs(r["turn"]) >= thr:
                emit(r["s0"], r["s1"], 0.5 * (r["s0"] + r["s1"]), r["turn"])
    out.sort(key=lambda c: c["s_mid"])
    return out


# ---------------------------------------------------------------- мосты
def _perimeter(bez):
    return go.cum_lengths(bez)[-1]


def corner_bridges(bez, mode, size=1.0, offset=3.0, lead_side=None, min_turn_deg=45.0):
    """Мосты у углов: mode "all" — у каждого; "diagonal" — у пары углов с макс. расстоянием между point
    (<2 углов -> ValueError). Мост на ребре ПОСЛЕ угла по ходу обхода: pos = s_end + offset (mod периметр).
    Если [pos, pos+size] не помещается на прямом участке до следующего угла, offset уменьшается (не ниже
    min(size, offset)); иначе ValueError. lead_side зарезервирован (сейчас не используется).
    Результат отсортирован по pos и проверен validate_bridges."""
    if mode not in ("all", "diagonal"):
        raise ValueError("mode должен быть 'all' или 'diagonal'")
    if not bez[1]:
        raise ValueError("мосты ставятся на замкнутый контур")
    if size <= 0:
        raise ValueError("size должен быть > 0")
    corners = find_corners(bez, min_turn_deg)
    P = _perimeter(bez)
    if mode == "diagonal":
        if len(corners) < 2:
            raise ValueError("для diagonal нужно >= 2 углов")
        best, pair = -1.0, None
        for i in range(len(corners)):
            for j in range(i + 1, len(corners)):
                d = go._dist(corners[i]["point"], corners[j]["point"])
                if d > best + 1e-9:
                    best, pair = d, (i, j)
        chosen = [corners[pair[0]], corners[pair[1]]]
    else:
        chosen = corners
    ivs = straight_intervals(bez)
    shifts = _shifts(bez, P)
    o_min = min(size, offset)
    res = []
    for c in chosen:
        e = c["s_end"]
        # расстояние до начала следующего (другого) угла — мост не должен его пересекать
        room = P
        for c2 in corners:
            if c2 is c:
                continue
            room = min(room, (c2["s_start"] - e) % P)
        best_o = None
        for lo, hi in ivs:
            for sh in shifts:
                l2, h2 = lo + sh, min(hi + sh, e + room)
                if h2 <= e - _EPS:
                    continue
                o = min(offset, h2 - e - size)
                if o >= max(o_min, l2 - e) - _EPS and (best_o is None or o > best_o):
                    best_o = o
        if best_o is None:
            raise ValueError("угол s=%.4f: нет прямого участка для моста size=%g" % (e, size))
        res.append(model.Bridge(pos=_to_pos(e + best_o, size) % P, size=size))
    res.sort(key=lambda b: b.pos)
    errs = validate_bridges(bez, res)
    if errs:
        raise ValueError("; ".join(errs))
    return res


def _overlaps(starts_sizes, P, closed):
    """Индексы пар пересекающихся отрезков [start, start+size] (циклически для closed)."""
    bad = []
    n = len(starts_sizes)
    for i in range(n):
        ai, si = starts_sizes[i]
        for j in range(i + 1, n):
            aj, sj = starts_sizes[j]
            if closed:
                d = (aj - ai) % P
                ov = d < si - _EPS or (P - d) < sj - _EPS
            else:
                ov = aj < ai + si - _EPS and ai < aj + sj - _EPS
            if ov:
                bad.append((i, j))
    return bad


def uniform_bridges(bez, step, size=1.0, count=None, start=0.0):
    """Равномерно по периметру: номинальные позиции start + k*step (или P/count при заданном count).
    Мост сдвигается вперёд по контуру до ближайшего места, где [pos, pos+size] на прямом участке; сдвиг больше
    step/2 или пересечение с уже поставленным -> мост пропускается. Результат отсортирован по pos."""
    if not bez[1]:
        raise ValueError("мосты ставятся на замкнутый контур")
    if size <= 0:
        raise ValueError("size должен быть > 0")
    P = _perimeter(bez)
    if count is not None:
        if count < 0:
            raise ValueError("count < 0")
        if count == 0:
            return []
        step = P / count
        n = count
    else:
        if step is None or step <= 0:
            raise ValueError("нужен step > 0 или count")
        n = int(math.ceil((P - start) / step - 1e-9))
    ivs = straight_intervals(bez)
    shifts = _shifts(bez, P)
    placed = []                          # позиции начала участков
    for k in range(n):
        p = _to_start(start + k * step, size) % P
        q = None
        for lo, hi in ivs:
            for sh in shifts:
                l2, h2 = lo + sh, hi + sh
                cand = max(p, l2)
                if cand + size <= h2 + _EPS and (q is None or cand < q):
                    q = cand
        if q is None or q - p > step / 2.0 + _EPS:
            continue
        q %= P
        if any((q - a) % P < size - _EPS or (a - q) % P < size - _EPS for a in placed):
            continue                     # пересекается с уже поставленным (дубль)
        placed.append(q)
    return sorted((model.Bridge(pos=_to_pos(a, size) % P, size=size) for a in placed), key=lambda b: b.pos)


def manual_bridges(bez, positions, size=1.0):
    """Мосты в заданных позициях (длина дуги от начала контура); позиции не меняются и не проверяются —
    проверка через validate_bridges. Отсортировано по pos."""
    return sorted((model.Bridge(pos=float(p), size=size) for p in positions), key=lambda b: b.pos)


def validate_bridges(bez, bridges):
    """Список сообщений об ошибках (пустой = всё в порядке): pos вне [0, периметр), size <= 0, мост не на
    прямом участке, пересечение/совпадение мостов."""
    errs = []
    P = _perimeter(bez)
    ivs = straight_intervals(bez)
    shifts = _shifts(bez, P)
    for i, b in enumerate(bridges):
        if not (0.0 <= b.pos < P):
            errs.append("мост %d: pos=%.6g вне [0, %.6g)" % (i, b.pos, P))
        if b.size <= 0:
            errs.append("мост %d: size=%.6g <= 0" % (i, b.size))
            continue
        a = _to_start(b.pos, b.size)
        if not _fits(ivs, shifts, a, a + b.size):
            errs.append("мост %d: участок [%.6g, %.6g] не на прямом участке контура" % (i, a, a + b.size))
    ok = [(_to_start(b.pos, b.size), b.size) for b in bridges if b.size > 0]
    for i, j in _overlaps(ok, P, bool(bez[1])):
        errs.append("мосты %d и %d пересекаются" % (i, j))
    return errs


def should_bridge(bbox_w, bbox_h, min_len=None, min_wid=None):
    """ТЗ 7.4: True, если большая сторона > min_len или меньшая > min_wid (None — порог игнорируется)."""
    big, small = max(bbox_w, bbox_h), min(bbox_w, bbox_h)
    return (min_len is not None and big > min_len) or (min_wid is not None and small > min_wid)
