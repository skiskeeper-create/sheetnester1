"""Генератор файлов .lxds (CypCut Pro 7.1) по спецификации FORMAT.md («Спецификация генерации»).

Публичное:
  write_lxds(nest, out_path, template=<корень>/1.lxds, thumbnail="generate"|"template"|"none", mode=None) -> dict
  nest_from_lxds(path, keep_orphans=False) -> model.Nest
  place_part(part, angle, dx, dy, mirror=False) -> model.Placement     (angle — РАДИАНЫ, как в geomops.transform)

Схема «как в образце c»: без блока «Группа»; в Nest2D/Results каждое размещение — отдельный Insert с NestPart.
Из шаблона (1.lxds) без изменений копируются Layers, TextStyles, Viewports (кроме VPort Handle), info.xml
(кроме SaveTime); Technical — кроме случая mode= (тогда патчится CamParams канала 1); в content.xml меняются
только HandleSeed, Header ID и Portions ActiveModelID.
Генерируются: Blocks, Shapes2D (+data.bin), Geometry2D (+data.bin, MD5), Nest2D/*, thumbnail.emf.
Только стандартная библиотека.
"""
import copy
import dataclasses
import datetime
import hashlib
import math
import re
import struct
import uuid
import warnings
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import bcmp
import bridges as br
import geometry
import geomops as go
import model
import modes
import reader

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATE = ROOT / "1.lxds"

# --- НЕ ПОДТВЕРЖДЕНО (см. FORMAT.md, «НЕ установлено») ---------------------------------------------------------

# Curve[0] (u32) — смысл не установлен, в образцах непоследователен: 1.lxds (CypCut 7.1.2410.6): Bezier-контур = 1,
# Line/LwPolyline/Circle = 0; образцы b/c (7.1.2240.3): в деталях почти везде 1, в размещениях Bezier/Circle = 0.
# Принято правило по 1.lxds (шаблон, самая новая версия CypCut): 1 для PolyBezier, иначе 0.
CURVE0_BY_KIND = {"Bezier": 1}


def curve_flag(geo):
    """Значение Curve[0] для фигуры (НЕ ПОДТВЕРЖДЕНО, см. CURVE0_BY_KIND)."""
    return CURVE0_BY_KIND.get(geo[0], 0)


# Curve-чанк: 9 x u32: [0] = curve_flag, [2] = 1, [8] = 6 (одинаково во всех образцах), остальное 0.
CURVE_TAIL = (0, 1, 0, 0, 0, 0, 0, 6)

# Стандартные имена (берутся из шаблона, если не заданы явно; в образцах b/c — "Part1" и "Rectangle")
RESULT_NAME = "Nest result"
MATERIAL_NAME = "Material Name"
FIRST_HANDLE = 1000
HOME_REF_TYPE = 515
THUMB_ENTRY = "Nest2D/Results/1/thumbnail.emf"


# =====================================================================================================
#  XML: сериализация по FORMAT.md (CRLF, табуляции, числа '%.9g' с .x, нули не пишутся)
# =====================================================================================================
def fnum(v):
    """Число -> текст: '%.9g', затем 0.x -> .x и -0.x -> -.x."""
    if isinstance(v, bool):
        raise TypeError("bool не число")
    if isinstance(v, float) and not math.isfinite(v):
        raise ValueError("нечисловое значение: %r" % v)
    s = "%.9g" % v
    if s.startswith("0."):
        s = s[1:]
    elif s.startswith("-0."):
        s = "-" + s[2:]
    return s


def _av(v):
    """Значение атрибута -> str, либо None (не писать: None или числовой ноль)."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        raise TypeError("bool в атрибуте")
    if v == 0:
        return None
    if isinstance(v, int):
        return str(v)
    return fnum(v)


def _esc(s, attr):
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return s.replace('"', "&quot;") if attr else s


class El:
    __slots__ = ("tag", "attrs", "kids", "text")

    def __init__(self, tag, attrs=(), kids=(), text=None):
        self.tag = tag
        self.attrs = [(k, s) for k, v in attrs for s in (_av(v),) if s is not None]
        self.kids = list(kids)
        self.text = text


def _ser(e, ind, out):
    pad = "\t" * ind
    a = "".join(' %s="%s"' % (k, _esc(v, True)) for k, v in e.attrs)
    if e.text is not None:
        out.append("%s<%s%s>%s</%s>\r\n" % (pad, e.tag, a, _esc(e.text, False), e.tag))
    elif not e.kids:
        out.append("%s<%s%s/>\r\n" % (pad, e.tag, a))
    else:
        out.append("%s<%s%s>\r\n" % (pad, e.tag, a))
        for k in e.kids:
            _ser(k, ind + 1, out)
        out.append("%s</%s>\r\n" % (pad, e.tag))


def to_xml(root):
    """El -> bytes: UTF-8 без BOM, CRLF, отступ табуляцией."""
    out = ['<?xml version="1.0" encoding="utf-8"?>\r\n']
    _ser(root, 0, out)
    return "".join(out).encode("utf-8")


# =====================================================================================================
#  Вспомогательное
# =====================================================================================================
def _tag(geo):
    return geometry.enc(geo)[0][4:]


def _guid(rng):
    u = uuid.UUID(int=rng.getrandbits(128), version=4) if rng is not None else uuid.uuid4()
    return "{%s}" % str(u).upper()


def _bbox(geo):
    try:
        return go.bbox(geo)
    except ValueError:                       # LwPoly с bulge и т.п. — по вершинам/контрольным точкам
        pts = geometry.bbox(geo)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))


def _lead_to_dict(ld):
    return dict(type=ld.type, pos=ld.pos, angle=ld.angle, length=ld.length, arc_radius=ld.arc_radius,
                hole_r=ld.hole_r)


def _pos_out(sh, s):
    """Позиция на контуре (мм, модель) -> формат CypCut (сегмент + t) для Bezier; у прочих фигур — как есть."""
    return go.param_at(sh.geo, s) if sh.geo[0] == "Bezier" else s


def _pos_in(geo, p):
    return go.s_at_param(geo, p) if geo[0] == "Bezier" else p


def _bin_lead(sh, with_bridges):
    """model.Lead для чанка LeadParam (или None); pos переводится в формат CypCut (сегмент + t)."""
    if sh.lead is None:
        return None
    return dataclasses.replace(sh.lead, pos=_pos_out(sh, sh.lead.pos))


def _micro_el(sh):
    """Мосты: Pos — начало моста (сегмент + t), мост занимает [Pos, Pos+Size]. LeadIn у моста = врезка
    повторного входа после него; у моста, за которым начинается основная врезка, LeadIn пустой (1.lxds)."""
    if not sh.bridges:
        return None
    kids = []
    for b in sh.bridges:
        l = b.lead
        li = El("LeadIn", [("LeadType", l.type), ("Angle", l.angle), ("Length", l.length),
                           ("ArcRadius", l.arc_radius), ("LeadHoleR", l.hole_r)]) if l is not None else El("LeadIn")
        kids.append(El("MicroConnect", [("Pos", _pos_out(sh, b.pos)), ("Size", b.size)], [li]))
    return El("MicroConnects", (), kids)


# =====================================================================================================
#  Утилизация: 100 * sum(площадь внешнего контура - площади отверстий) / (W*H)
# =====================================================================================================
def _rev(segs):
    return [list(reversed(s)) for s in reversed(segs)]


def _loops(geos, tol=1e-3):
    """Замкнутые контуры из набора фигур: замкнутые фигуры + цепочки, склеенные по концам. -> [Bezier-сегменты]."""
    closed, items = [], []
    for g in geos:
        try:
            b = go.to_bezier(g)
        except ValueError:
            continue
        if g[0] == "Circle" or b[1]:
            closed.append(b[2])
        else:
            items.append(b[2])
    d = lambda a, b: math.hypot(a[0] - b[0], a[1] - b[1])
    used = [False] * len(items)
    for i in range(len(items)):
        if used[i]:
            continue
        used[i] = True
        chain = list(items[i])
        while True:
            if d(chain[-1][3], chain[0][0]) < tol:
                closed.append(chain)
                break
            for j in range(len(items)):
                if used[j]:
                    continue
                s = items[j]
                if d(s[0][0], chain[-1][3]) < tol:
                    chain += s
                elif d(s[-1][3], chain[-1][3]) < tol:
                    chain += _rev(s)
                else:
                    continue
                used[j] = True
                break
            else:
                break
    return closed


def utilization(placements, sheet_w, sheet_h):
    """Использование листа, %. Внешний контур размещения — наибольший замкнутый контур канала реза (CH_CUT),
    остальные замкнутые контуры — отверстия. У CypCut расхождение с этой оценкой ≤ 0.006 п.п. (образец c)."""
    tot = 0.0
    for pl in placements:
        areas = sorted((abs(go.signed_area(("Bezier", True, l)))
                        for l in _loops([s.geo for s in pl.shapes if s.channel == model.CH_CUT])), reverse=True)
        if areas:
            tot += areas[0] - sum(areas[1:])
    return 100.0 * tot / (sheet_w * sheet_h)


# =====================================================================================================
#  thumbnail.emf
# =====================================================================================================
EMF_SIZE = 320
EMF_MARGIN = 8
_EMR_HEADER, _EMR_EOF, _EMR_SELECTOBJECT, _EMR_CREATEPEN = 1, 14, 37, 38
_EMR_DELETEOBJECT, _EMR_POLYLINE16 = 40, 87
_STOCK_BLACK_PEN = 0x80000007


def _flatten(geo):
    """geo -> список ломаных [(x, y), ...] (в мм) для эскиза."""
    k = geo[0]
    if k == "Line":
        return [[geo[1], geo[2]]]
    if k in ("Arc", "Circle"):
        (cx, cy), r = geo[1], geo[2]
        st, sw = (geo[3], geo[4]) if k == "Arc" else (0.0, 2 * math.pi)
        n = max(2, int(math.ceil(abs(sw) / math.radians(6.0))))
        return [[(cx + r * math.cos(st + sw * i / n), cy + r * math.sin(st + sw * i / n)) for i in range(n + 1)]]
    if k == "LwPoly":
        pts = [(x, y) for x, y, _ in geo[2]]
        if geo[1] and pts:
            pts.append(pts[0])
        return [pts]
    if k == "Bezier":
        pts = []
        for seg in geo[2]:
            for i in range(0 if not pts else 1, 7):
                pts.append(go._bz(seg, i / 6.0))
        return [pts]
    return []


def _emf_rec(t, payload=b""):
    return struct.pack("<II", t, 8 + len(payload)) + payload


def make_thumbnail_emf(nest):
    """Простой EMF 320x320: лист (серый) и все фигуры размещений (чёрные) ломаными (Polyline16)."""
    W, H = nest.sheet_w, nest.sheet_h
    s = (EMF_SIZE - 2 * EMF_MARGIN) / max(W, H)
    ox, oy = (EMF_SIZE - W * s) / 2.0, (EMF_SIZE - H * s) / 2.0

    def px(p):
        return (int(round(ox + p[0] * s)), int(round(oy + (H - p[1]) * s)))

    def polyline(pts):
        q = []
        for p in map(px, pts):
            if not q or q[-1] != p:
                q.append(p)
        return q if len(q) >= 2 else None

    sheet = [polyline([(0, 0), (W, 0), (W, H), (0, H), (0, 0)])]
    shapes = []
    for pl in nest.placements:
        for sh in pl.shapes:
            for line in _flatten(sh.geo):
                shapes.append(polyline(line))
    recs = []

    def pen(idx, color):
        recs.append(_emf_rec(_EMR_CREATEPEN, struct.pack("<IIiiI", idx, 0, 1, 0, color)))
        recs.append(_emf_rec(_EMR_SELECTOBJECT, struct.pack("<I", idx)))

    allpts = []

    def draw(lines):
        for q in lines:
            if q is None:
                continue
            for i in range(0, len(q), 60000):            # int16-точки, запись <= ~240 КБ
                part = q[max(0, i - 1):i + 60000]
                xs, ys = [p[0] for p in part], [p[1] for p in part]
                allpts.extend(part)
                recs.append(_emf_rec(_EMR_POLYLINE16, struct.pack("<4iI", min(xs), min(ys), max(xs), max(ys),
                                                                    len(part)) +
                                     b"".join(struct.pack("<hh", *p) for p in part)))

    pen(1, 0x00808080)
    draw(sheet)
    pen(2, 0x00000000)
    draw(shapes)
    recs.append(_emf_rec(_EMR_SELECTOBJECT, struct.pack("<I", _STOCK_BLACK_PEN)))
    recs.append(_emf_rec(_EMR_DELETEOBJECT, struct.pack("<I", 1)))
    recs.append(_emf_rec(_EMR_DELETEOBJECT, struct.pack("<I", 2)))
    recs.append(_emf_rec(_EMR_EOF, struct.pack("<III", 0, 16, 20)))
    xs, ys = [p[0] for p in allpts], [p[1] for p in allpts]
    frame = int(round(EMF_SIZE * 508.0 * 100.0 / 1920.0))            # 0.01 мм при 1920 px / 508 мм, как в образцах
    nbytes = 108 + sum(len(r) for r in recs)
    head = struct.pack("<II4i4iIIIIHHIIIiiiiIIIii", _EMR_HEADER, 108, min(xs), min(ys), max(xs), max(ys),
                       0, 0, frame, frame, 0x464D4520, 0x10000, nbytes, len(recs) + 1, 3, 0, 0, 0, 0,
                       1920, 1080, 508, 286, 0, 0, 0, 508000, 285750)
    return head + b"".join(recs)


# =====================================================================================================
#  Внутренние структуры генерации
# =====================================================================================================
class _Blk:
    def __init__(self, name, part_flag, shapes, with_bridges):
        self.name, self.part_flag, self.shapes, self.with_bridges = name, part_flag, shapes, with_bridges
        self.handle = self.shandle = 0
        self.items = []                      # [(handle, model.Shape)]
        self.outline_handle = 0


class _Counter:
    def __init__(self, start):
        self.h = start

    def __call__(self):
        self.h += 1
        return self.h


def _alloc_block(blk, nh):
    blk.handle = nh()
    blk.shandle = nh()
    blk.items = [(nh(), s) for s in blk.shapes]


def _shape_objects(handle, sh, with_bridges):
    """-> (shape_obj, geo_obj, tag) — bcmp.Obj для Shapes2D/data.bin и Geometry2D/data.bin."""
    cls, payload = geometry.enc(sh.geo)
    lead = _bin_lead(sh, with_bridges)
    chunks = [bcmp.Chunk("Object", 1, struct.pack("<I", handle)),
              bcmp.Chunk("Shape", 8, struct.pack("<15I", sh.channel, *([0] * 14)),
                         gap=b"\x01\0\0\0" if lead is not None else bytes(8))]
    if lead is not None:
        chunks.append(bcmp.Chunk("LeadParam", 1, reader.encode_lead(_lead_to_dict(lead)), gap=bytes(4)))
    chunks.append(bcmp.Chunk("Curve", 2, struct.pack("<9I", curve_flag(sh.geo), *CURVE_TAIL)))
    return bcmp.Obj(cls, chunks), bcmp.Obj(cls, [bcmp.Chunk(cls[4:], 1, payload)]), cls[4:]


def _dump_with_offsets(objs):
    """bcmp.dump_all + смещения объектов. -> (bytes, [offset])."""
    offs, pos = [], bcmp.HDR
    for o in objs:
        offs.append(pos)
        pos += len(bcmp.dump_obj(o, pos))
    data = bcmp.dump_all(objs)
    assert len(data) == pos
    return data, offs


def _ins_el(tag, handle, block_name, extra=()):
    return El(tag, [("Class", "Insert")] + [("Handle", handle)] + list(extra) if tag == "Shape"
              else [("Handle", handle)] + list(extra),
              [El("BlockName", text=block_name), El("XScale", text="1"), El("YScale", text="1")])


# =====================================================================================================
#  write_lxds
# =====================================================================================================
def _patch_technical(tech, mode):
    """Подставить режим резки (modes.mode_to_camparams) в канал 1 (рез) Technical/content.xml.
    Патчится только блок Channel ChannelPort="1"; канал 9 (гравировка) и прочие — без изменений."""
    patches = modes.mode_to_camparams(mode)

    def sub1(pattern, repl, data, what, flags=0):
        new, n = re.subn(pattern, repl, data, count=1, flags=flags)
        if n != 1:
            raise ValueError("в Technical не найдено: " + what)
        return new

    m = re.search(r'<Channel ChannelPort="1"[^>]*>', tech)
    end = tech.find('<Channel ChannelPort="2"', m.end() if m else 0)
    if not m or end < 0:
        raise ValueError("в Technical не найден блок канала 1 (ChannelPort=1..2)")
    head, ch1, tail = tech[:m.start()], tech[m.start():end], tech[end:]
    ch1 = sub1(r'WorkSpeed="[^"]*"', 'WorkSpeed="%s"' % patches["WorkSpeed"], ch1, "WorkSpeed")
    ch1 = sub1(r'(<Cut [^>]*?\bGasPressure=")[^"]*(")', r"\g<1>%s\g<2>" % patches["Cut/GasPressure"],
               ch1, "Cut/GasPressure")
    ch1 = sub1(r'(<Cut [^>]*?\bGasType=")[^"]*(")', r"\g<1>%s\g<2>" % patches["Cut/GasType"], ch1, "Cut/GasType")
    ch1 = sub1(r'(<Pierce1 [^>]*?\bTimeStay=")[^"]*(")', r"\g<1>%s\g<2>" % patches["Pierce1/TimeStay"],
               ch1, "Pierce1/TimeStay")
    ch1 = sub1(r'(<MaterialParams [^>]*?\bMaterialName=")[^"]*(")',
               r"\g<1>%s\g<2>" % patches["MaterialParams/MaterialName"], ch1, "MaterialParams/MaterialName")
    ch1 = sub1(r'(<MaterialParams [^>]*?\bThickness=")[^"]*(")',
               r"\g<1>%s\g<2>" % patches["MaterialParams/Thickness"], ch1, "MaterialParams/Thickness")
    ch1 = sub1(r"<MaterialName>[^<]*</MaterialName>",
               "<MaterialName>%s</MaterialName>" % patches["MaterialName"], ch1, "MaterialName")
    note = patches["Note"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    ch1 = sub1(r"<Note>.*?</Note>", "<Note>%s</Note>" % note, ch1, "Note", flags=re.DOTALL)
    return head + ch1 + tail


def write_lxds(nest, out_path, template=DEFAULT_TEMPLATE, thumbnail="generate", *, plate_name=None,
               first_block_name=None, rng=None, now=None, mode=None):
    """Записать nest в .lxds. -> dict(handle_seed, utilization, ext_min, ext_max, guid).

    thumbnail: "generate" — простой EMF 320x320; "template" — копия из шаблона; "none" — без файла.
    plate_name / first_block_name — имена листа и блока первой детали (по умолчанию из шаблона; в образцах b/c
    это "Rectangle" / "Part1"). rng — random.Random для воспроизводимых GUID; now — datetime (UTC) для SaveTime и
    времени записей ZIP. mode — режим резки (dict из modes.find_mode): подставляется в CamParams канала 1 резки
    (Technical/content.xml); None — CamParams шаблона без изменений. Необязательный атрибут nest.orphan_blocks
    (список списков Shape) — блоки без Insert («история правок» CypCut), пишутся между деталями и размещениями
    (нужны для побайтной регенерации образца)."""
    if thumbnail not in ("generate", "template", "none"):
        raise ValueError("thumbnail должен быть 'generate', 'template' или 'none'")
    if not (nest.sheet_w > 0 and nest.sheet_h > 0):
        raise ValueError("размер листа должен быть > 0")
    ids = {id(p) for p in nest.parts}
    if len(ids) != len(nest.parts):
        raise ValueError("одна и та же деталь дважды в nest.parts")
    for pl in nest.placements:
        if id(pl.part) not in ids:
            raise ValueError("Placement.part не входит в nest.parts")
    for p in nest.parts:
        if p.outline >= len(p.shapes) or p.outline < -1:
            raise ValueError("Part %r: outline вне диапазона" % p.name)
    now = now or datetime.datetime.now(datetime.timezone.utc)

    # ---- шаблон ----
    with zipfile.ZipFile(template) as tz:
        order = [i.filename for i in tz.infolist()]
        tpl = {n: tz.read(n) for n in order if not n.endswith("/")}
    tplates = ET.fromstring(tpl["Nest2D/Plates/content.xml"])
    tblocks = ET.fromstring(tpl["Blocks/content.xml"])
    if plate_name is None:
        plate_name = tplates.find("NestPlate").get("Name")
    if first_block_name is None:
        first_block_name = next(b.get("Name") for b in tblocks if b.get("Flags") == "512")

    W, H = float(nest.sheet_w), float(nest.sheet_h)
    orphans = list(getattr(nest, "orphan_blocks", ()))

    # ---- блоки и Handle (порядок выдачи CypCut, FORMAT.md) ----
    nh = _Counter(FIRST_HANDLE)
    pblk, npart, part_ins = [], [], []
    for i, p in enumerate(nest.parts):
        b = _Blk(first_block_name if i == 0 else _guid(rng), True, p.shapes, False)
        _alloc_block(b, nh)
        if p.outline >= 0:
            b.outline_handle = b.items[p.outline][0]
        part_ins.append(nh())
        npart.append(nh())
        pblk.append(b)
    plate_h = nh()
    nh()
    plate_lw = nh()
    oblk = []
    for shapes in orphans:
        b = _Blk(_guid(rng), False, shapes, True)
        _alloc_block(b, nh)
        oblk.append(b)
    plblk = []
    for pl in nest.placements:
        b = _Blk(_guid(rng), False, pl.shapes, True)
        _alloc_block(b, nh)
        plblk.append(b)
    res_h = nh()
    nh()
    nh()
    copy_h = nh()
    res_ins = [nh() for _ in nest.placements]
    seed = nh.h

    # ---- листы и размещения ----
    plate_shape = model.Shape(None, 0, ("LwPoly", True, [(0.0, 0.0, 0.0), (W, 0.0, 0.0), (W, H, 0.0), (0.0, H, 0.0)]))
    allblk = pblk + oblk + plblk
    entries = [(h, s, b.with_bridges) for b in allblk for h, s in b.items]
    entries += [(plate_lw, plate_shape, False), (copy_h, plate_shape, False)]
    sobjs, gobjs, tags = [], [], []
    for h, s, wb in entries:
        so, go_, tg = _shape_objects(h, s, wb)
        sobjs.append(so)
        gobjs.append(go_)
        tags.append(tg)
    sbin, soff = _dump_with_offsets(sobjs)
    gbin, goff = _dump_with_offsets(gobjs)
    smd5 = hashlib.md5(sbin).hexdigest().upper()
    gmd5 = hashlib.md5(gbin).hexdigest().upper()

    # ---- Shapes2D/content.xml ----
    kids = []
    for (h, s, wb), tg, so_, go_ in zip(entries, tags, soff, goff):
        me = _micro_el(s) if wb else None
        kids.append(El(tg, [("Handle", h), ("LayerName", s.layer), ("DataAddr", so_), ("GeoAddr", go_)],
                       [me] if me else []))
    kids.append(El("MD5", text=smd5))
    shapes_xml = to_xml(El("Shapes2D", (), kids))
    geom_xml = to_xml(El("Geometry2D", (), [El("MD5", text=gmd5)]))

    # ---- Blocks/content.xml ----
    bk = []
    for b in allblk:
        bk.append(El("Block", [("Handle", b.handle), ("Name", b.name), ("OutlineID", b.outline_handle),
                               ("Flags", 512 if b.part_flag else 0)],
                     [El("BasePoint"),
                      El("Shapes", [("Handle", b.shandle)],
                         [El("Shape", [("Class", _tag(s.geo)), ("Handle", h)]) for h, s in b.items])]))
    blocks_xml = to_xml(El("Blocks", (), bk))

    # ---- Nest2D/Parts ----
    used = {}
    for pl in nest.placements:
        used[id(pl.part)] = used.get(id(pl.part), 0) + 1
    pk = []
    for p, b, ih, nph in zip(nest.parts, pblk, part_ins, npart):
        pk.append(El("NestPart", [("Handle", nph), ("Name", p.name), ("Amount", p.amount),
                                  ("AmountUsed", used.get(id(p), 0)), ("GUID", _guid(rng)),
                                  ("ShapeCls", "Insert")],
                     [_ins_el("Insert", ih, b.name, [("ChannelPort", model.CH_CUT)]),
                      _ins_el("Shape", ih, b.name, [("ChannelPort", model.CH_CUT)]),
                      El("ExtShapes")]))
    parts_xml = to_xml(El("Parts", (), pk))

    # ---- Nest2D/Plates ----
    poly = [(0, 0), (W, 0), (W, H), (0, H)]
    plates_xml = to_xml(El("Plates", (), [El(
        "NestPlate", [("Handle", plate_h), ("Name", plate_name), ("Amount", 1), ("AmountUsed", 1),
                      ("GUID", _guid(rng)), ("ShapeCls", "LwPolyline")],
        [El("LwPolyline", [("Handle", plate_lw), ("NestPlate", plate_h), ("LeadInOptimizeFlag", 6),
                           ("PointCount", 4), ("PolyFlag", 1)],
            [El("ToolCompensation")] + [El("Point", [("X", x), ("Y", y)]) for x, y in poly]),
         El("Shape", [("Class", "LwPolyline"), ("Handle", plate_lw), ("NestPlate", plate_h)]),
         El("ExtShapes")])]))

    # ---- Nest2D/Results ----
    boxes = [_bbox(plate_shape.geo)] + [_bbox(s.geo) for pl in nest.placements for s in pl.shapes]
    ext_min = (min(b[0] for b in boxes), min(b[1] for b in boxes))
    ext_max = (max(b[2] for b in boxes), max(b[3] for b in boxes))
    util = utilization(nest.placements, W, H)
    result_id = _guid(rng)
    rs = [El("Shape", [("Class", "LwPolyline"), ("Handle", copy_h), ("NestPlate", plate_h)])]
    pidx = {id(p): i for i, p in enumerate(nest.parts)}
    for pl, b, ih in zip(nest.placements, plblk, res_ins):
        allcut = bool(pl.shapes) and all(s.channel == model.CH_CUT for s in pl.shapes)
        rs.append(El("Shape", [("Class", "Insert"), ("Handle", ih), ("ChannelPort", model.CH_CUT if allcut else 0),
                               ("NestPart", npart[pidx[id(pl.part)]])],
                     [El("BlockName", text=b.name), El("XScale", text="1"), El("YScale", text="1")]))
    res_xml = to_xml(El("NestResult", [("Handle", res_h), ("Name", RESULT_NAME), ("ID", result_id),
                                       ("PlateAmount", 1), ("Utilization", util),
                                       ("MaterialName", MATERIAL_NAME)],
                        [El("ExtMin", [("X", ext_min[0]), ("Y", ext_min[1])]),
                         El("ExtMax", [("X", ext_max[0]), ("Y", ext_max[1])]),
                         El("HomeRef", [("RefType", HOME_REF_TYPE), ("X", ext_max[0]), ("Y", ext_min[1])]),
                         El("Shapes", (), rs)]))
    results_xml = to_xml(El("NestResults", (), [El("NestResult", [
        ("Index", 1), ("Name", RESULT_NAME), ("PartCount", len(nest.placements)), ("Utilization", util),
        ("PlanCount", 1), ("SizeX", ext_max[0] - ext_min[0]), ("SizeY", ext_max[1] - ext_min[1]),
        ("ID", result_id)])]))
    nest_xml = to_xml(El("NestInfo", (), [El("Parts", [("Count", len(nest.parts))]), El("NestGroups"),
                                          El("Plates", [("Count", 1)]), El("Results", [("Count", 1)])]))

    # ---- копии из шаблона с точечными правками ----
    def sub1(pattern, repl, data, what):
        new, n = re.subn(pattern, repl, data, count=1)
        if n != 1:
            raise ValueError("в шаблоне не найдено: " + what)
        return new

    saved = now.astimezone(datetime.timezone.utc)
    info = sub1(r'SaveTime="[^"]*"', 'SaveTime="%s"' % saved.strftime("%Y-%m-%dT%H:%M:%SZ"),
                tpl["info.xml"].decode("utf-8"), "info.xml SaveTime")
    content = tpl["content.xml"].decode("utf-8")
    content = sub1(r'HandleSeed="\d+"', 'HandleSeed="%d"' % seed, content, "HandleSeed")
    content = sub1(r'(<Header [^>]*?\bID=")[^"]*(")', r"\g<1>%s\g<2>" % _guid(rng), content, "Header ID")
    content = sub1(r'ActiveModelID="[^"]*"', 'ActiveModelID="%s"' % result_id, content, "ActiveModelID")
    vports = sub1(r'(<VPort Handle=")\d+(")', r"\g<1>%d\g<2>" % (seed + 1), tpl["Viewports/content.xml"].decode("utf-8"),
                  "VPort Handle")

    gen = {
        "info.xml": info.encode("utf-8"), "content.xml": content.encode("utf-8"),
        "Viewports/content.xml": vports.encode("utf-8"),
        "Nest2D/Results/content.xml": results_xml, "Shapes2D/data.bin": sbin, "Shapes2D/content.xml": shapes_xml,
        "Blocks/content.xml": blocks_xml, "Nest2D/Parts/content.xml": parts_xml,
        "Nest2D/Plates/content.xml": plates_xml, "Nest2D/content.xml": nest_xml,
        "Nest2D/Results/1/content.xml": res_xml, "Geometry2D/data.bin": gbin, "Geometry2D/content.xml": geom_xml,
    }
    if mode is not None:
        gen["Technical/content.xml"] = _patch_technical(tpl["Technical/content.xml"].decode("utf-8"),
                                                        mode).encode("utf-8")
    if thumbnail == "generate":
        gen[THUMB_ENTRY] = make_thumbnail_emf(nest)
    elif thumbnail == "template" and THUMB_ENTRY in tpl:
        gen[THUMB_ENTRY] = tpl[THUMB_ENTRY]

    names = [n for n in order if not (n == THUMB_ENTRY and THUMB_ENTRY not in gen)]
    for n in gen:                                    # шаблон без такой записи — добавить в конец
        if n not in names:
            names.append(n)
    dt = (max(saved.year, 1980), saved.month, saved.day, saved.hour, saved.minute, saved.second)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as zf:
        for n in names:
            zi = zipfile.ZipInfo(n, dt)
            zi.compress_type = zipfile.ZIP_STORED
            zi.create_system = 0
            zi.external_attr = 0
            zf.writestr(zi, b"" if n.endswith("/") else gen.get(n, tpl.get(n)))
    return dict(handle_seed=seed, utilization=util, ext_min=ext_min, ext_max=ext_max, guid=result_id)


# =====================================================================================================
#  nest_from_lxds
# =====================================================================================================
def _lead_from_bin(payload):
    d = reader.decode_lead(payload)
    return model.Lead(type=d["type"], angle=d["angle"], length=d["length"], arc_radius=d["arc_radius"],
                      hole_r=d["hole_r"], pos=d["pos"])


def _lead_from_xml(a):
    return model.Lead(type=int(a.get("LeadType", 0)), angle=float(a.get("Angle", 0)),
                      length=float(a.get("Length", 0)), arc_radius=float(a.get("ArcRadius", 0)),
                      hole_r=float(a.get("LeadHoleR", 0)))


def _to_model_shape(s):
    geo = s["geo"]
    lead = _lead_from_bin(s["lead"][0]) if s["lead"] else None
    if lead is not None:
        lead.pos = _pos_in(geo, lead.pos)
    brs = []
    for pos, size, attrs in s["micro"]:
        bl = _lead_from_xml(attrs) if attrs else None
        brs.append(model.Bridge(pos=_pos_in(geo, pos), size=size, lead=bl))
        if bl is not None:
            bl.pos = brs[-1].pos
    return model.Shape(layer=s["attrs"].get("LayerName"), channel=s["channel"], geo=geo, lead=lead, bridges=brs)


def nest_from_lxds(path, keep_orphans=False):
    """Прочитать .lxds в model.Nest.

    Детали (Flags=512) -> Part (name = NestPart.Name, amount = NestPart.Amount, outline = индекс фигуры с
    Handle == Block.OutlineID, иначе -1). Каждый Insert в Nest2D/Results/1 -> Placement; деталь — по NestPart.
    Insert без NestPart (схема «Группа», 1.lxds и образец b): блок разворачивается — плоский блок даёт ОДНО
    размещение, вложенные Insert блока — по размещению на каждый; для такого размещения создаётся синтетическая
    Part (имя = имя блока, amount 1), добавляемая в nest.parts. Insert без геометрии игнорируются.
    Размер листа — по Nest2D/Plates (max X, max Y первой пластины). thickness/material не читаются.
    keep_orphans=True: блоки без Flags, на которые нет ссылок из Results («история правок» CypCut), кладутся в
    nest.orphan_blocks (для побайтной регенерации; write_lxds их пишет)."""
    shapes, blocks = reader.load(path)
    with zipfile.ZipFile(path) as z:
        blk_xml = ET.fromstring(z.read("Blocks/content.xml"))
        parts_xml = ET.fromstring(z.read("Nest2D/Parts/content.xml"))
        plates_xml = ET.fromstring(z.read("Nest2D/Plates/content.xml"))
        res_xml = ET.fromstring(z.read("Nest2D/Results/1/content.xml"))
    outline_id = {b.get("Name"): b.get("OutlineID") for b in blk_xml}

    pts = [(float(p.get("X", 0)), float(p.get("Y", 0))) for p in plates_xml.find("NestPlate/LwPolyline").iter("Point")]
    nest = model.Nest(sheet_w=max(p[0] for p in pts), sheet_h=max(p[1] for p in pts))

    def block_shapes(name):
        return [(h, _to_model_shape(shapes[h])) for cls, h, _ in blocks[name]["items"] if h in shapes]

    by_np = {}
    for np_ in parts_xml.iter("NestPart"):
        bname = np_.find("Insert/BlockName").text
        hs = block_shapes(bname)
        oid = outline_id.get(bname)
        idx = next((i for i, (h, _) in enumerate(hs) if oid is not None and h == int(oid)), -1)
        part = model.Part(name=np_.get("Name"), shapes=[s for _, s in hs], outline=idx,
                          amount=int(np_.get("Amount", 0)))
        nest.parts.append(part)
        by_np[np_.get("Handle")] = part

    referenced = set()

    def unlinked(bname):
        referenced.add(bname)
        items = blocks[bname]["items"]
        direct = [s for _, s in block_shapes(bname)]
        if direct:
            part = model.Part(name=bname, shapes=direct, outline=-1, amount=1)
            nest.parts.append(part)
            nest.placements.append(model.Placement(part, direct))
        for cls, _, child in items:
            if cls == "Insert" and child in blocks:
                unlinked(child)

    for s in res_xml.iter("Shape"):
        if s.get("Class") != "Insert":
            continue
        bname = s.findtext("BlockName")
        if bname not in blocks:
            raise ValueError("Result ссылается на отсутствующий блок " + bname)
        part = by_np.get(s.get("NestPart"))
        if part is not None:
            referenced.add(bname)
            nest.placements.append(model.Placement(part, [x for _, x in block_shapes(bname)]))
        else:
            unlinked(bname)

    if keep_orphans:
        nest.orphan_blocks = [[x for _, x in block_shapes(n)] for n, b in blocks.items()
                              if b["flags"] != "512" and n not in referenced]
    return nest


# =====================================================================================================
#  place_part
# =====================================================================================================
def place_part(part, angle, dx, dy, mirror=False):
    """Размещение детали: зеркало (x -> -x, если mirror) -> поворот на angle (РАДИАНЫ, против ч.с.) вокруг (0,0)
    -> сдвиг (dx, dy); геометрия — geomops.transform.

    Позиции мостов и врезок (длины по контуру от начальной точки) при повороте/сдвиге остаются прежними.
    mirror: geomops.transform разворачивает порядок сегментов PolyBezier (начальная точка контура остаётся),
    поэтому точка на длине s исходного контура оказывается на длине L-s: lead.pos -> L-pos, мост [pos, pos+size]
    -> [L-pos-size, L-pos] (с учётом bridges.BRIDGE_ANCHOR). Для остальных типов порядок вершин не меняется — позиции
    прежние. Углы врезок при mirror НЕ пересчитываются (НЕ ПОДТВЕРЖДЕНО, как CypCut трактует Angle) — выдаётся
    предупреждение."""
    out, warned = [], False
    for s in part.shapes:
        geo = go.transform(s.geo, angle, mirror, dx, dy)
        lead = dataclasses.replace(s.lead) if s.lead is not None else None
        brs = [dataclasses.replace(b, lead=dataclasses.replace(b.lead) if b.lead is not None else None)
               for b in s.bridges]
        if mirror and s.geo[0] == "Bezier":
            L = go.length(s.geo)
            if lead is not None:
                lead.pos = (L - lead.pos) % L
            for b in brs:
                st = br._to_start(b.pos, b.size)
                b.pos = br._to_pos((L - st - b.size) % L, b.size)
                if b.lead is not None:
                    b.lead.pos = b.pos
            brs.sort(key=lambda b: b.pos)
        if mirror and not warned and (lead is not None or any(b.lead for b in brs)):
            warnings.warn("place_part(mirror=True): Angle врезок не пересчитывается (не подтверждено для CypCut)",
                          stacklevel=2)
            warned = True
        out.append(model.Shape(layer=s.layer, channel=s.channel, geo=geo, lead=lead, bridges=brs))
    return model.Placement(part, out)
