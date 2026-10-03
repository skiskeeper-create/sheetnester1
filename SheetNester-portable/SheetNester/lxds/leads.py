"""Параметры врезки (lead-in) по ТЗ 7.3: тип/размеры по умолчанию и выбор точки врезки на контуре."""
import math

import geomops
import model

THICK_ARC = 10.0     # толщина > 10 мм -> всегда дуга с авто-радиусом


def default_lead(kind, thickness, lead_type=model.LEAD_ARC, arc_radius=2.0, arc_angle_deg=30.0, hole_pos=0.0):
    """model.Lead по умолчанию. kind: 'outline' | 'hole'.
    Толщина > 10 мм: всегда LEAD_ARC, радиус max(arc_radius, thickness/2). length = max(thickness/2, 1.0)
    (для линии и дуги, контура и отверстия). Дуга: angle = radians(arc_angle_deg); линия: pi/2. hole_r = 0.5.
    pos = hole_pos для отверстия, 0.0 для контура (начало контура). Направление отхода внутрь/наружу CypCut
    выводит из типа контура (внешний/отверстие) — в формате .lxds не хранится."""
    if kind not in ("outline", "hole"):
        raise ValueError("kind должен быть 'outline' или 'hole'")
    if thickness < 0:
        raise ValueError("thickness < 0")
    if lead_type not in (model.LEAD_LINE, model.LEAD_ARC):
        raise ValueError("lead_type должен быть LEAD_LINE или LEAD_ARC")
    if thickness > THICK_ARC:
        lead_type = model.LEAD_ARC
        arc_radius = max(arc_radius, thickness / 2.0)
    angle = math.radians(arc_angle_deg) if lead_type == model.LEAD_ARC else math.pi / 2
    return model.Lead(type=lead_type, angle=angle, length=max(thickness / 2.0, 1.0), arc_radius=arc_radius,
                      hole_r=0.5, pos=hole_pos if kind == "hole" else 0.0)


def lead_at(geo, x, y, thickness, kind="outline", lead_type=model.LEAD_ARC, **kw):
    """Врезка в ближайшей к точке (x, y) точке контура (выбор точки врезки, ТЗ 7.3/7.6).
    kind='outline': pos = длина дуги до ближайшей точки. kind='hole': pos не хранится (врезка из центра),
    ближайшая точка не вычисляется."""
    lead = default_lead(kind, thickness, lead_type, **kw)
    if kind == "outline":
        lead.pos, _ = geomops.nearest_s(geo, float(x), float(y))
    return lead


def move_lead(shape, x, y):
    """Перенести врезку существующего контура в ближайшую к клику (x, y) точку.
    У контура с мостами перенос запрещён: основная врезка привязана к концу первого моста (ограничение .lxds)."""
    if not shape.lead:
        raise ValueError("у контура нет врезки")
    if shape.bridges:
        raise ValueError("у контура с мостами точка врезки фиксирована (конец первого моста)")
    shape.lead.pos, _ = geomops.nearest_s(shape.geo, float(x), float(y))
    return shape.lead
