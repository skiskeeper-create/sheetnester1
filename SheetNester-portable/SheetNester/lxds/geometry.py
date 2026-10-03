"""Геометрия фигур CypCut в Geometry2D/data.bin (payload чанка) <-> python-кортежи.

Точка = 4 байта тега 10 00 02 00 + double x + double y (20 байт).
Line     : 2 точки.
Arc      : центр(точка), r, u32 0, начальный угол (рад), угол дуги (рад, против ч.с.).
Circle   : как Arc; в образцах угол0 = pi/4, угол1 = 0.
LwPolyline: u32[3] нули, u32 closed(@8), u32 n(@12), u32 1(@16), далее n вершин по 32 байта:
           x, y, bulge (double), u32 0, u32 «номер следующей вершины» (1-based, у последней 0).
PolyBezier: u32[2] нули, u32 closed(@8), u32 0, u32 n сегментов(@16), u32 1(@20), далее n сегментов по 88 байт:
           4 точки кубического Безье (P0..P3), u32 0, u32 «следующий сегмент» (1-based, у последнего 0).
"""
import struct
def pt(p,o): 
    assert p[o:o+4]==b"\x10\x00\x02\x00", p[o:o+4].hex()
    return struct.unpack_from("<dd",p,o+4)
def dec(cls,p):
    if cls=="TGksLine": return ("Line",pt(p,0),pt(p,20))
    if cls in("TGksArc","TGksCircle"):
        c=pt(p,0); r=struct.unpack_from("<d",p,20)[0]; a,b=struct.unpack_from("<dd",p,32)
        return ("Arc" if cls=="TGksArc" else "Circle",c,r,a,b)
    if cls=="TGksLwPolyline":
        n=struct.unpack_from("<I",p,12)[0]; cl=struct.unpack_from("<I",p,8)[0]
        return ("LwPoly",bool(cl),[struct.unpack_from("<ddd",p,20+32*i) for i in range(n)])
    if cls=="TGksPolyBezier":
        n=struct.unpack_from("<I",p,16)[0]; cl=struct.unpack_from("<I",p,8)[0]
        segs=[[pt(p,24+88*i+20*k) for k in range(4)] for i in range(n)]
        return ("Bezier",bool(cl),segs)
    raise ValueError(cls)
def bbox(g):
    k=g[0]
    if k=="Line": pts=[g[1],g[2]]
    elif k in("Arc","Circle"): (x,y),r=g[1],g[2]; pts=[(x-r,y-r),(x+r,y+r)]
    elif k=="LwPoly": pts=[(x,y) for x,y,_ in g[2]]
    else: pts=[q for s in g[2] for q in s]
    return pts

# ---------- кодирование (обратное dec) ----------
PT = b"\x10\x00\x02\x00"
def _pt(p): return PT + struct.pack("<dd", *p)
def enc(g, circle_extra=None):
    k = g[0]
    if k == "Line": return "TGksLine", _pt(g[1]) + _pt(g[2])
    if k in ("Arc", "Circle"):
        _, c, r, a, b = g
        return ("TGksArc" if k == "Arc" else "TGksCircle",
                _pt(c) + struct.pack("<d", r) + b"\0\0\0\0" + struct.pack("<dd", a, b))
    if k == "LwPoly":
        _, closed, v = g
        out = struct.pack("<IIII", 0, 0, int(closed), len(v)) + struct.pack("<I", 1)
        for i, (x, y, bu) in enumerate(v):
            out += struct.pack("<dddII", x, y, bu, 0, i + 2 if i < len(v) - 1 else 0)
        return "TGksLwPolyline", out
    if k == "Bezier":
        _, closed, segs = g
        out = struct.pack("<IIIII", 0, 0, int(closed), 0, len(segs)) + struct.pack("<I", 1)
        for i, s in enumerate(segs):
            out += b"".join(_pt(q) for q in s) + struct.pack("<II", 0, i + 2 if i < len(segs) - 1 else 0)
        return "TGksPolyBezier", out
    raise ValueError(k)
