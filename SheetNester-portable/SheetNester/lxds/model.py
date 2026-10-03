"""Модель раскладки — контракт между импортом DXF, геометрией/мостами и писателем .lxds.

Геометрия `geo` — кортежи формата geometry.dec/enc (координаты мм, углы рад):
  ("Line", (x1,y1), (x2,y2))
  ("Arc", (cx,cy), r, start, sweep)          # против часовой
  ("Circle", (cx,cy), r, a0, a1)             # в образцах a0=pi/4, a1=0
  ("LwPoly", closed, [(x,y,bulge), ...])     # bulge пока всегда 0
  ("Bezier", closed, [[P0,P1,P2,P3], ...])   # кубические сегменты
Слои и каналы (подтверждено по 1.lxds): рез — канал 1, гравировка — канал 9.
"""
from dataclasses import dataclass, field
from typing import Optional

CH_CUT, CH_ENGRAVE = 1, 9
LAYER_OUTLINE, LAYER_HOLE = "CUT_OUTLINE", "CUT_HOLE"
LAYER_ENGRAVE, LAYER_MARK = "ENGRAVE_TEXT", "MARK_RISKS"
LEAD_LINE, LEAD_ARC = 1, 2          # LeadType в .lxds (подтверждено пользователем)


@dataclass
class Lead:
    """Врезка. angle — рад; length, arc_radius, hole_r — мм; pos — расстояние по контуру, мм."""
    type: int = LEAD_ARC
    angle: float = 0.7853981633974483
    length: float = 2.0
    arc_radius: float = 3.0
    hole_r: float = 0.5
    pos: float = 0.0


@dataclass
class Bridge:
    """Мост (MicroConnect): pos — длина дуги контура от начальной точки (мм; в файл пишется как сегмент + t),
    size — ширина, мм. Мост занимает [pos, pos+size]."""
    pos: float
    size: float = 1.0
    lead: Optional[Lead] = None      # LeadIn: врезка повторного входа после моста (None — пустой LeadIn)


@dataclass
class Shape:
    layer: Optional[str]
    channel: int
    geo: tuple
    lead: Optional[Lead] = None
    bridges: list = field(default_factory=list)


@dataclass
class Part:
    """Определение детали (блок Flags=512) в собственных координатах."""
    name: str
    shapes: list
    outline: int = -1                # индекс внешнего контура в shapes (-1 — не задан)
    amount: int = 1


@dataclass
class Placement:
    """Размещённая деталь: фигуры уже в координатах листа (поворот/сдвиг запечены)."""
    part: Part
    shapes: list


@dataclass
class Nest:
    sheet_w: float
    sheet_h: float
    parts: list = field(default_factory=list)          # [Part]
    placements: list = field(default_factory=list)     # [Placement]
    thickness: float = 0.0
    material: str = ""
