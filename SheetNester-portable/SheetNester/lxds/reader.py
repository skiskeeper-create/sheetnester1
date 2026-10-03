"""Чтение .lxds/.nrp2 напрямую из zip: фигуры (геометрия + служебные чанки) и блоки."""
import struct
import zipfile
import xml.etree.ElementTree as ET
import bcmp
import geometry


def _micro(e):
    """Мосты (MicroConnect) фигуры: [(pos_мм_по_контуру, ширина_мм, атрибуты LeadIn)]."""
    return [(float(m.get("Pos")), float(m.get("Size")), dict(m.find("LeadIn").attrib))
            for m in e.iterfind("MicroConnects/MicroConnect")]


def decode_lead(p):
    """LeadParam (56 байт): u32 тип, u32 0, double 0, double pos, double angle, length, arc_radius, hole_r."""
    t = struct.unpack_from("<I", p, 0)[0]
    _, pos, ang, ln, r, hr = struct.unpack_from("<6d", p, 8)
    return dict(type=t, pos=pos, angle=ang, length=ln, arc_radius=r, hole_r=hr)


def encode_lead(d):
    return (struct.pack("<II", d["type"], 0) + struct.pack("<6d", 0.0, d["pos"], d["angle"],
            d["length"], d["arc_radius"], d["hole_r"]))


def load(path):
    z = zipfile.ZipFile(path)
    geo = {o.off: o for o in bcmp.parse_all(z.read("Geometry2D/data.bin"))}
    sh = {o.off: o for o in bcmp.parse_all(z.read("Shapes2D/data.bin"))}
    shapes = {}
    for e in ET.fromstring(z.read("Shapes2D/content.xml")):
        if e.get("GeoAddr") is None:          # <MD5>
            continue
        g, s = geo[int(e.get("GeoAddr"))], sh[int(e.get("DataAddr"))]
        shapes[int(e.get("Handle"))] = dict(
            cls=e.tag, attrs=dict(e.attrib),
            geo=geometry.dec(g.cls, g.chunks[0].payload),
            shape=s.chunks[1].payload, curve=s.chunks[-1].payload,
            lead=[c.payload for c in s.chunks if c.name == "LeadParam"],
            channel=struct.unpack_from("<I", s.chunks[1].payload, 0)[0],
            micro=_micro(e))
    blocks = {}
    for b in ET.fromstring(z.read("Blocks/content.xml")):
        blocks[b.get("Name")] = dict(
            handle=int(b.get("Handle")), flags=b.get("Flags"),
            items=[(s.get("Class"), int(s.get("Handle")), s.findtext("BlockName"))
                   for s in b.find("Shapes")])
    return shapes, blocks
