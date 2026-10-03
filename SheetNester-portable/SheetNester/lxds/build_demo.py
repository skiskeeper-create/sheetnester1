"""Сквозной пример: папка DXF -> раскладка «полками» -> мосты и врезки -> .lxds.
Запуск: python build_demo.py [папка_DXF] [выход.lxds] [толщина_мм]
Раскладка здесь примитивная (габаритные прямоугольники, повороты 0/90) — это заглушка до модуля нестинга."""
import copy
import math
import sys
from pathlib import Path

import bridges
import dxf_import
import geomops
import leads
import model
import writer

ROOT = Path(__file__).resolve().parents[2]
SHEET_W, SHEET_H = 1500.0, 3000.0     # как в образцах CypCut (X × Y)
MARGIN, GAP = 10.0, 10.0


def prepare_part(part, thickness, with_bridges=True):
    """Врезки и мосты по умолчанию (правила по 1.lxds): основная врезка начинается в конце первого моста,
    у этого моста LeadIn пустой, у остальных мостов LeadIn = врезка повторного входа."""
    out = part.shapes[part.outline]
    out.bridges = []
    if with_bridges and out.geo[0] == "Bezier":
        try:
            out.bridges = bridges.corner_bridges(out.geo, "diagonal", size=1.0)
        except ValueError:
            out.bridges = []
    out.lead = leads.default_lead("outline", thickness)
    if out.bridges:
        out.lead.pos = out.bridges[0].pos + out.bridges[0].size
        for br in out.bridges[1:]:
            br.lead = copy.copy(out.lead)
    for sh in part.shapes:
        if sh.layer == model.LAYER_HOLE:
            sh.lead = leads.default_lead("hole", thickness)


def shelf_pack(parts):
    """Полочная раскладка: (part, angle, dx, dy). Деталь при необходимости поворачивается на 90°, чтобы влезть в ширину."""
    items, x, y, shelf_h = [], MARGIN, MARGIN, 0.0
    for p in sorted(parts, key=lambda q: -max(geomops.bbox(q.shapes[q.outline].geo)[2:])):
        x0, y0, x1, y1 = geomops.bbox(p.shapes[p.outline].geo)
        w, h = x1 - x0, y1 - y0
        ang = 0.0
        if w > SHEET_W - 2 * MARGIN and h <= SHEET_W - 2 * MARGIN:
            ang = math.pi / 2
        for _ in range(p.amount):
            if ang:
                bw, bh = h, w
            else:
                bw, bh = w, h
            if x + bw > SHEET_W - MARGIN:
                x, y, shelf_h = MARGIN, y + shelf_h + GAP, 0.0
            if y + bh > SHEET_H - MARGIN:
                raise RuntimeError(f"не помещается на лист: {p.name}")
            if ang:   # поворот на 90° вокруг начала: bbox -> x in [-y1,-y0], y in [x0,x1]
                dx, dy = x + y1, y + (-x0)
            else:
                dx, dy = x - x0, y - y0
            items.append((p, ang, dx, dy))
            x += bw + GAP
            shelf_h = max(shelf_h, bh)
    return items


def main(folder=None, out=None, thickness=None, with_bridges=True, mode=None, articles=False):
    folder = Path(folder or ROOT / "чистые DXF")
    out = Path(out or ROOT / "демо_раскладка.lxds")
    parts, reports = dxf_import.batch_load(folder)
    if thickness is not None:      # только детали заданной толщины (один лист = один материал/толщина)
        keep = [p for p in parts if reports_meta(reports, p) == thickness]
        parts = keep
    if articles:                   # артикулы -> розовый слой (гравировка)
        import ocr
        for p in parts:
            rep = report_for(reports, p)
            if rep is not None:
                ocr.apply_articles(p, rep)
    for p in parts:
        prepare_part(p, thickness or 16.0, with_bridges)
    placed = []
    for part, ang, dx, dy in shelf_pack(parts):
        placed.append(writer.place_part(part, ang, dx, dy))
    nest = model.Nest(SHEET_W, SHEET_H, parts=parts, placements=placed, thickness=thickness or 0.0)
    info = writer.write_lxds(nest, out, mode=mode)
    print(f"деталей: {len(parts)}, размещений: {len(placed)}, КИМ (Utilization): {info['utilization']:.2f}%, файл: {out}")
    return nest, info, out


def report_for(reports, part):
    for r in reports.values():
        if getattr(r, "part_name", None) == part.name or (r.meta or {}).get("article") == part.name:
            return r
    return None


def reports_meta(reports, part):
    r = report_for(reports, part)
    return (r.meta or {}).get("thickness") if r else None


if __name__ == "__main__":
    a = sys.argv[1:]
    nb = "--no-bridges" in a
    a = [x for x in a if x != "--no-bridges"]
    main(a[0] if len(a) > 0 and a[0] else None, a[1] if len(a) > 1 and a[1] else None,
         float(a[2]) if len(a) > 2 else None, with_bridges=not nb)
