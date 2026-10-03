"""Бенчмарк качества раскладки: current (BLF) vs new (IFI + 2opt).

Использует DXF samples из проекта. Считает utilization и время.
"""
import sys, time, math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "lxds"))

import dxf_import, model, nesting
import numpy as np

SAMPLES = [
    "3-S301 20мм 12шт.dxf",
    "7-4055 20мм 1шт.dxf",
    "7-4056 20мм 1шт.dxf",
    "7-4057 16мм 1шт.dxf",
    "7-4058 16мм 4шт.dxf",
    "7-4059 16мм 1шт.dxf",
    "7004 12мм 1шт.dxf",
    "7040 4мм 8шт.dxf",
    "МЭ11 14мм 22шт.dxf",
    "МЭ12 14мм 8шт.dxf",
]

SHEET_W, SHEET_H = 1500.0, 3000.0
MARGIN, GAP, RES = 10.0, 5.0, 2.5


def load_items():
    items = []
    for i, fn in enumerate(SAMPLES):
        path = Path("DXF samples") / fn
        if not path.exists():
            continue
        part, rep = dxf_import.load_part(path)
        meta = dxf_import.parse_filename(fn)
        qty = int(meta.get("qty") or 1)
        key = f"{i}_{fn}"
        items.append((key, part, qty))
    return items


def sheet_area(sheets):
    """Суммарная площадь деталей на листах."""
    total = 0.0
    for sheet in sheets:
        for key, part, angle, T in sheet:
            m = nesting.part_metrics(part)
            total += m["area"]
    return total


def run_bench(nest_func, label, items):
    t0 = time.perf_counter()
    sheets, unplaced = nest_func(items)
    elapsed = time.perf_counter() - t0
    n_placed = sum(len(s) for s in sheets)
    n_unplaced = sum(q for _, q in unplaced)
    util = sheet_area(sheets) / (len(sheets) * SHEET_W * SHEET_H) * 100 if sheets else 0
    print(f"[{label}] sheets={len(sheets)} placed={n_placed} unplaced={n_unplaced} "
          f"util={util:.2f}% time={elapsed:.2f}s")
    return util, elapsed, len(sheets), unplaced


def main():
    items = load_items()
    print(f"Loaded {len(items)} item types, total qty={sum(q for _,_,q in items)}")

    # Current: BLF
    def cur(items):
        return nesting.nest(items, SHEET_W, SHEET_H, MARGIN, GAP, RES,
                            rotations=(0,1,2,3), place="blf")

    run_bench(cur, "current-BLF", items)

    # New: IFI + 2opt
    try:
        from lxds.nest_ifi import nest_ifi
        def new(items):
            return nest_ifi(items, SHEET_W, SHEET_H, MARGIN, GAP, RES,
                            rotations=(0,1,2,3), max_iter=200)
        run_bench(new, "new-IFI+2opt", items)
    except ImportError:
        print("nest_ifi not found — skipping new algorithm bench")


if __name__ == "__main__":
    main()
