"""Нестинг по реальному контуру (растровый) + подготовка мостов/врезок + метрики для расчёта стоимости.

Метод: лист и детали переводятся в сетку с шагом res (мм). Клетка детали помечается занятой, если её ЦЕНТР лежит
внутри детали или ближе d = gap/2 + res*sqrt(2)/2 к её контуру. Смещения детали — целые клетки, поэтому центры
клеток детали и листа совпадают. Если две детали не делят ни одной клетки, реальное расстояние между ними >= gap
(для точек a, b на расстоянии < gap центр клетки, содержащей середину отрезка ab, ближе d к обеим деталям).
Отверстия деталей остаются свободными — в них попадают мелкие детали. Свободные позиции для всех смещений сразу —
корреляция занятости листа с маской детали через БПФ. Выбор позиции: минимальный верхний край, затем левый край
(«снизу-слева»), среди поворотов 0/90/180/270°. Порядок: по убыванию площади детали.
"""
import copy
import math

import numpy as np

import bridges
import geomops as go
import leads
import model
import writer

CUT_LAYERS = (model.LAYER_OUTLINE, model.LAYER_HOLE)


# ---------------------------------------------------------------- геометрия детали -> полигоны
def _is_straight(seg, tol=1e-6):
    (x0, y0), p1, p2, (x3, y3) = seg
    dx, dy = x3 - x0, y3 - y0
    L = math.hypot(dx, dy)
    if L < 1e-12:
        return False
    return all(abs((p[0] - x0) * dy - (p[1] - y0) * dx) / L < tol for p in (p1, p2))


def flatten(geo, step):
    """Замкнутый контур -> массив точек (N×2) без повтора первой точки; step — макс. длина хорды для кривых, мм."""
    if geo[0] == "Circle":
        (cx, cy), r = geo[1], geo[2]
        n = min(4096, max(12, int(math.ceil(2 * math.pi * r / step))))
        a = np.linspace(0, 2 * math.pi, n, endpoint=False)
        return np.column_stack([cx + r * np.cos(a), cy + r * np.sin(a)])
    bz = go.to_bezier(geo) if geo[0] != "Bezier" else geo
    pts = []
    for seg in bz[2]:
        if _is_straight(seg):
            pts.append(seg[0])
            continue
        n = min(64, max(2, int(math.ceil(go.seg_length(seg) / step))))
        for k in range(n):
            t = k / n
            u = 1 - t
            pts.append(tuple(u ** 3 * seg[0][i] + 3 * u * u * t * seg[1][i] + 3 * u * t * t * seg[2][i]
                             + t ** 3 * seg[3][i] for i in (0, 1)))
    return np.asarray(pts, float)


def cut_shapes(part):
    """Замкнутые контуры реза детали: внешний (part.outline) + отверстия."""
    return [s for i, s in enumerate(part.shapes)
            if s.channel == model.CH_CUT and (i == part.outline or s.layer == model.LAYER_HOLE)]


def part_polys(part, step):
    return [flatten(s.geo, step) for s in cut_shapes(part)]


def _rot(pts, k):
    """Поворот на k*90° против ч.с. вокруг (0,0) — точно (без погрешности cos/sin)."""
    x, y = pts[:, 0], pts[:, 1]
    return [np.column_stack(v) for v in ((x, y), (-y, x), (-x, -y), (y, -x))][k % 4]


def _rot_angle(pts, angle):
    """Поворот на произвольный угол (рад) вокруг (0,0). Углы, кратные 90°, — точно (через _rot)."""
    q = round(angle / (math.pi / 2))
    if abs(angle - q * math.pi / 2) < 1e-9:
        return _rot(pts, q % 4)
    ca, sa = math.cos(angle), math.sin(angle)
    x, y = pts[:, 0], pts[:, 1]
    return np.column_stack((x * ca - y * sa, x * sa + y * ca))


# ---------------------------------------------------------------- растеризация
def _inside(cx, cy, polys):
    """Чётно-нечётное заполнение: центры (cx — 1D по x, cy — 1D по y) внутри области polys -> bool[len(cy), len(cx)]."""
    ins = np.zeros((len(cy), len(cx)), bool)
    for P in polys:
        Q = np.roll(P, -1, axis=0)
        for (x1, y1), (x2, y2) in zip(P, Q):
            if y1 == y2:
                continue
            lo, hi = (y1, y2) if y1 < y2 else (y2, y1)
            rows = np.flatnonzero((cy >= lo) & (cy < hi))
            if not len(rows):
                continue
            xs = x1 + (cy[rows] - y1) * (x2 - x1) / (y2 - y1)
            ins[rows] ^= cx[None, :] < xs[:, None]
    return ins


def _near(cx, cy, polys, d, res, x0, y0):
    """Центры клеток на расстоянии <= d от контуров."""
    out = np.zeros((len(cy), len(cx)), bool)
    d2 = d * d
    for P in polys:
        Q = np.roll(P, -1, axis=0)
        for (x1, y1), (x2, y2) in zip(P, Q):
            c0 = max(0, int((min(x1, x2) - d - x0) / res) - 1)
            c1 = min(len(cx), int((max(x1, x2) + d - x0) / res) + 2)
            r0 = max(0, int((min(y1, y2) - d - y0) / res) - 1)
            r1 = min(len(cy), int((max(y1, y2) + d - y0) / res) + 2)
            if c0 >= c1 or r0 >= r1:
                continue
            X, Y = np.meshgrid(cx[c0:c1], cy[r0:r1])
            vx, vy = x2 - x1, y2 - y1
            L2 = vx * vx + vy * vy
            t = np.clip(((X - x1) * vx + (Y - y1) * vy) / L2, 0, 1) if L2 > 0 else 0.0
            dist2 = (X - x1 - t * vx) ** 2 + (Y - y1 - t * vy) ** 2
            out[r0:r1, c0:c1] |= dist2 <= d2
    return out


class Orient:
    """Деталь в одном повороте (angle — рад, кратные 90° точные): маска и привязка к сетке."""

    def __init__(self, polys, angle, res, d, margin=0.0):
        self.angle = angle
        self.polys = [_rot_angle(P, angle) for P in polys]
        outer = self.polys[0]
        self.bb = (*outer.min(0), *outer.max(0))
        # запас слева/снизу delta >= d и delta ≡ margin (mod res): деталь встаёт точно на отступ от края листа
        self.delta = d + ((margin - d) % res)
        self.ox, self.oy = self.bb[0] - self.delta, self.bb[1] - self.delta   # мировые координаты угла маски
        self.mw = int(math.ceil((self.bb[2] - self.bb[0] + self.delta + d) / res)) + 1
        self.mh = int(math.ceil((self.bb[3] - self.bb[1] + self.delta + d) / res)) + 1
        cx = self.ox + (np.arange(self.mw) + 0.5) * res
        cy = self.oy + (np.arange(self.mh) + 0.5) * res
        self.mask = _inside(cx, cy, self.polys) | _near(cx, cy, self.polys, d, res, self.ox, self.oy)
        self.fft = None


class Sheet:
    def __init__(self, Hs, Ws):
        self.occ = np.zeros((Hs, Ws), np.float32)
        self.items = []          # (item, Orient, T)
        self.nofit = set()


def nest(items, sheet_w, sheet_h, margin=10.0, gap=5.0, res=2.5, rotations=(0, 1, 2, 3), progress=None,
         keepouts=None, priority="area", rates=None, free_angles=(), place="blf"):
    """items: [(key, part, qty)]. -> (sheets, unplaced), sheets: [[(key, part, angle_rad, (tx, ty))...]].
    Размещение = поворот на angle_rad вокруг (0,0), затем сдвиг (tx, ty). unplaced: [(key, qty)] — не влезают на лист.

    keepouts — зоны запрета (струбцины, дефекты): [(x, y, w, h)] в координатах листа, мм; детали не ближе gap/2.
    priority — порядок очереди деталей (ТЗ 7.2): 'area' (макс. КИМ, по умолчанию) | 'time' | 'cost'
    (требуют rates=(скорость_м/мин, прокол_с, станко-час_руб/ч) — оценка времени/стоимости резки детали;
    эвристика первого прохода: меняет только очерёдность размещения).
    free_angles — произвольные углы поворота (градусы, ТЗ 7.2) в дополнение к rotations (шаги по 90°);
    каждый угол = отдельная ориентация с маской (дороже по времени). Гарантия зазора сохраняется: маски
    любого поворота строятся на той же решётке (delta ≡ margin (mod res)).
    place — критерий выбора позиции: 'blf' (bottom-left-fill: минимум верхнего края, затем левого)
    | 'contact' (сначала максимум контакта с размещённым — заполнение выемок, затем 'blf'; лучше работает
    с free_angles).
    """
    d = gap / 2 + res * math.sqrt(2) / 2 + 0.05   # +0.05 мм: запас на хорды при аппроксимации дуг
    pad = int(math.ceil(d / res)) + 1
    Ws = int(math.floor(sheet_w / res)) + 2 * pad
    Hs = int(math.floor(sheet_h / res)) + 2 * pad
    angles = [k * math.pi / 2 for k in rotations] + [math.radians(a) for a in free_angles]

    def mark_keepouts(occ):
        """Растеризация зон запрета (расширены на gap/2): клетки помечаются занятыми."""
        if not keepouts:
            return
        e = gap / 2.0
        for (kx, ky, kw, kh) in keepouts:
            x0, y0, x1, y1 = kx - e, ky - e, kx + kw + e, ky + kh + e
            j0 = max(0, int(math.floor(x0 / res)) + pad)
            j1 = min(Ws - 1, int(math.floor(x1 / res)) + pad)
            i0 = max(0, int(math.floor(y0 / res)) + pad)
            i1 = min(Hs - 1, int(math.floor(y1 / res)) + pad)
            if j1 >= j0 and i1 >= i0:
                occ[i0:i1 + 1, j0:j1 + 1] += 1.0

    def sort_value(area, key, part):
        if priority == "area" or not rates:
            return area
        v, tp, _rate = rates
        m = part_metrics(part)
        minutes = m["cut_m"] / max(v, 1e-6) + m["pierces"] * tp / 60.0
        return minutes if priority == "time" else minutes * rates[2]

    types, unplaced = [], []
    for key, part, qty in items:
        if qty <= 0:
            continue
        x0, y0, x1, y1 = go.bbox(part.shapes[part.outline].geo)
        w, h, aw, ah = x1 - x0, y1 - y0, sheet_w - 2 * margin, sheet_h - 2 * margin
        cardinal_ok = (w <= aw and h <= ah) or (any(k % 2 for k in rotations) and h <= aw and w <= ah)
        if not cardinal_ok and not free_angles:
            unplaced.append((key, qty))          # больше листа в кардинальных поворотах — маску не строим
            continue
        polys = part_polys(part, max(res / 2, 0.5))
        area = abs(go.signed_area(part.shapes[part.outline].geo))
        types.append((sort_value(area, key, part), key, part, qty,
                      [Orient(polys, a, res, d, margin) for a in angles]))
    types.sort(key=lambda t: -t[0])
    sheets = []
    total = sum(t[3] for t in types)
    done = 0

    def fits(sheet, orients):
        best = None
        F = np.fft.rfft2(sheet.occ)
        for o in orients:
            if o.mw > Ws or o.mh > Hs:
                continue
            if o.fft is None:
                m = np.zeros((Hs, Ws), np.float32)
                m[:o.mh, :o.mw] = o.mask
                o.fft = np.conj(np.fft.rfft2(m))
            corr = np.fft.irfft2(F * o.fft, s=(Hs, Ws))[:Hs - o.mh + 1, :Ws - o.mw + 1]
            # границы: реальный габарит в [margin, W-margin] × [margin, H-margin]
            # T = ((j-pad)*res - ox, (i-pad)*res - oy); xmin = bb0 + Tx = (j-pad)*res + delta
            e = o.delta
            j0 = max(0, int(math.ceil((margin - e) / res - 1e-9)) + pad)
            j1 = min(corr.shape[1] - 1, int(math.floor((sheet_w - margin - e - (o.bb[2] - o.bb[0])) / res + 1e-9)) + pad)
            i0 = max(0, int(math.ceil((margin - e) / res - 1e-9)) + pad)
            i1 = min(corr.shape[0] - 1, int(math.floor((sheet_h - margin - e - (o.bb[3] - o.bb[1])) / res + 1e-9)) + pad)
            if j0 > j1 or i0 > i1:
                continue
            free = corr[i0:i1 + 1, j0:j1 + 1] < 0.5
            if not free.any():
                continue
            sub = corr[i0:i1 + 1, j0:j1 + 1]
            if place == "contact":
                # максимум контакта с размещённым (заполнение выемок), затем минимум (top, left)
                mx = sub[free].max()
                cand = free & (sub >= mx - 1.0)
            else:
                cand = free
            rows = np.flatnonzero(cand.any(1))
            if not len(rows):
                continue
            i = i0 + rows[0]
            j = j0 + int(np.argmax(cand[rows[0]]))
            top = (i - pad) * res + e + (o.bb[3] - o.bb[1])
            left = (j - pad) * res + e
            if best is None or (top, left) < best[0]:
                best = ((top, left), o, i, j)
        return best

    for _sv, key, part, qty, orients in types:
        left = qty
        while left > 0:
            placed = False
            for sh in sheets:
                if key in sh.nofit:
                    continue
                b = fits(sh, orients)
                if b is None:
                    sh.nofit.add(key)
                    continue
                _, o, i, j = b
                sh.occ[i:i + o.mh, j:j + o.mw] += o.mask
                T = ((j - pad) * res - o.ox, (i - pad) * res - o.oy)
                sh.items.append((key, part, o.angle, T))
                placed = True
                break
            if not placed:
                sh = Sheet(Hs, Ws)
                mark_keepouts(sh.occ)
                if fits(sh, orients) is None:
                    unplaced.append((key, left))
                    done += left
                    break
                sheets.append(sh)
                continue
            left -= 1
            done += 1
            if progress:
                progress(done, total)
    return [s.items for s in sheets], unplaced


# ---------------------------------------------------------------- мосты и врезки (правила по 1.lxds)
def prepare_part(part, thickness, bridge_mode="diagonal", bridge_size=1.0, lead_type=model.LEAD_ARC):
    """Копия детали с врезками и мостами. bridge_mode: 'none' | 'diagonal' | 'all'.
    Основная врезка — в конце первого моста (у этого моста LeadIn пустой, у остальных — врезка повторного входа)."""
    p = copy.deepcopy(part)
    out = p.shapes[p.outline]
    out.bridges = []
    if bridge_mode != "none" and out.geo[0] == "Bezier":
        try:
            out.bridges = bridges.corner_bridges(out.geo, bridge_mode, size=bridge_size)
        except ValueError:
            out.bridges = []
    out.lead = leads.default_lead("outline", thickness, lead_type=lead_type)
    if out.bridges:
        out.lead.pos = out.bridges[0].pos + out.bridges[0].size
        for b in out.bridges[1:]:
            b.lead = copy.copy(out.lead)
    for s in p.shapes:
        if s.layer == model.LAYER_HOLE and s.channel == model.CH_CUT:
            s.lead = leads.default_lead("hole", thickness, lead_type=lead_type)
    return p


def part_metrics(part):
    """Длина реза (м), проколы, площадь нетто (мм²), габарит. Проколов на внешний контур = max(1, число мостов):
    после каждого моста, кроме последнего по ходу, — повторная врезка."""
    cut = 0.0
    pierces = 0
    holes = 0
    for s in cut_shapes(part):
        cut += go.length(s.geo)
        pierces += max(1, len(s.bridges))
        if s is not part.shapes[part.outline]:
            holes += 1
    outer = abs(go.signed_area(part.shapes[part.outline].geo))
    holes_area = sum(abs(go.signed_area(s.geo)) for s in cut_shapes(part) if s is not part.shapes[part.outline])
    x0, y0, x1, y1 = go.bbox(part.shapes[part.outline].geo)
    return dict(cut_m=cut / 1000.0, pierces=pierces, area=outer - holes_area, holes=holes, bbox=(x1 - x0, y1 - y0))


# ---------------------------------------------------------------- SVG
def svg_paths(polys):
    return "".join("M" + "L".join(f"{x:.2f},{y:.2f}" for x, y in P) + "Z" for P in polys)


def part_svg(part, size=120):
    polys = part_polys(part, 2.0)
    eng = [flatten(s.geo, 1.0) for s in part.shapes if s.channel == model.CH_ENGRAVE]
    allp = np.vstack(polys + eng) if eng else np.vstack(polys)
    x0, y0 = allp.min(0)
    x1, y1 = allp.max(0)
    w, h = max(x1 - x0, 1), max(y1 - y0, 1)
    pad = max(w, h) * 0.04
    eng_p = f'<path d="{svg_paths(eng)}" class="en"/>' if eng else ""
    return (f'<svg viewBox="{x0 - pad:.2f} {-(y1 + pad):.2f} {w + 2 * pad:.2f} {h + 2 * pad:.2f}" width="{size}" '
            f'height="{size}"><g transform="scale(1,-1)"><path d="{svg_paths(polys)}" fill-rule="evenodd" '
            f'class="pf"/>{eng_p}</g></svg>')


def sheet_svg(sheet_w, sheet_h, placements, keepouts=None):
    parts = []
    for pl in placements:
        polys = [flatten(s.geo, 3.0) for i, s in enumerate(pl.shapes)
                 if s.channel == model.CH_CUT and (i == pl.part.outline or s.layer == model.LAYER_HOLE)]
        marks = ""
        o = pl.shapes[pl.part.outline]
        for b in o.bridges:
            x, y = go.point_at(o.geo, b.pos + b.size / 2)
            marks += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" class="br"/>'
        if o.lead is not None and o.geo[0] == "Bezier":
            x, y = go.point_at(o.geo, o.lead.pos)
            marks += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" class="ld"/>'
        eng = [flatten(s.geo, 2.0) for s in pl.shapes if s.channel == model.CH_ENGRAVE]
        eng_p = f'<path d="{svg_paths(eng)}" class="en"/>' if eng else ""
        parts.append(f'<path d="{svg_paths(polys)}" fill-rule="evenodd" class="pf"><title>{_esc(pl.part.name)}</title>'
                     f'</path>{eng_p}{marks}')
    zones = ""
    if keepouts:
        for i, (kx, ky, kw, kh) in enumerate(keepouts):
            zones += (f'<rect x="{kx:.1f}" y="{ky:.1f}" width="{kw:.1f}" height="{kh:.1f}" class="ko"/>'
                      f'<text x="{kx + kw / 2:.1f}" y="{ky + kh / 2:.1f}" class="kot">запрет {i + 1}</text>')
    return (f'<svg viewBox="-20 {-sheet_h - 20} {sheet_w + 40} {sheet_h + 40}" preserveAspectRatio="xMidYMid meet">'
            f'<g transform="scale(1,-1)"><rect x="0" y="0" width="{sheet_w}" height="{sheet_h}" class="sh"/>'
            + zones + "".join(parts) + "</g></svg>")


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- весь процесс для группы (материал + толщина)
def run_group(entries, sheet_w, sheet_h, thickness, out_dir, file_prefix, template, margin=10.0, gap=5.0, res=2.5,
              rotate=True, bridge_mode="diagonal", bridge_size=1.0, lead_type=model.LEAD_ARC, progress=None,
              mode=None, keepouts=None, priority="area", free_angles=(), place="blf"):
    """entries: [(key, name, part, qty)] одной толщины/материала. Пишет по .lxds на лист. -> dict для интерфейса.
    mode — режим резки из modes.find_mode(): параметры подставляются в CamParams канала 1 (Technical/content.xml)
    и дают rates для priority='time'/'cost'. keepouts — зоны запрета [(x, y, w, h)], мм, в координатах листа.
    free_angles — произвольные углы поворота деталей (градусы) в дополнение к 0/90/180/270.
    place — критерий позиции 'blf' | 'contact' (см. nest)."""
    prepared = {}
    for key, name, part, qty in entries:
        p = prepare_part(part, thickness, bridge_mode, bridge_size, lead_type)
        p.name = name
        prepared[key] = p
    rots = (0, 1, 2, 3) if rotate else (0,)
    rates = None
    if mode and priority != "area":
        rates = (float(mode["speed_m_min"]), float(mode["pierce_s"]), float(mode["cost_rub_h"]))
    sheets, unplaced = nest([(k, prepared[k], q) for k, _, _, q in entries], sheet_w, sheet_h, margin, gap, res,
                            rots, progress, keepouts=keepouts, priority=priority, rates=rates,
                            free_angles=free_angles, place=place)
    res_sheets = []
    for n, items in enumerate(sheets, 1):
        used, pls, counts = [], [], {}
        for key, part, angle, (tx, ty) in items:
            if part not in used:
                used.append(part)
            pls.append(writer.place_part(part, angle, tx, ty))
            counts[key] = counts.get(key, 0) + 1
        nest_model = model.Nest(sheet_w, sheet_h, parts=used, placements=pls, thickness=thickness)
        path = out_dir / f"{file_prefix}_лист{n}.lxds"
        info = writer.write_lxds(nest_model, path, template=template, mode=mode)
        m = [part_metrics(pl.part) for pl in pls]
        pm = [part_metrics(model.Part(pl.part.name, pl.shapes, pl.part.outline)) for pl in pls]
        # габарит занятой деталями области (без листа) — для остатков склада
        cut = [s.geo for pl in pls for i, s in enumerate(pl.shapes)
               if s.channel == model.CH_CUT and (i == pl.part.outline or s.layer == model.LAYER_HOLE)]
        bx = [go.bbox(g) for g in cut]
        ext = ([min(b[0] for b in bx), min(b[1] for b in bx)], [max(b[2] for b in bx), max(b[3] for b in bx)])
        res_sheets.append(dict(file=str(path), name=path.name, utilization=info["utilization"],
                               cut_m=sum(x["cut_m"] for x in pm), pierces=sum(x["pierces"] for x in pm),
                               area=sum(x["area"] for x in m), count=len(pls), counts=counts,
                               ext=[list(ext[0]), list(ext[1])],
                               svg=sheet_svg(sheet_w, sheet_h, pls, keepouts=keepouts)))
    per_type = {k: part_metrics(p) for k, p in prepared.items()}
    return dict(sheets=res_sheets, unplaced=unplaced, per_type=per_type)
