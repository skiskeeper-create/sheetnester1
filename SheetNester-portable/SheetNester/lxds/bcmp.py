"""Контейнер BCMP (data.bin в .lxds/.nrp2 CypCut/CypNest): чтение и запись.

Файл: заголовок 4096 байт ('BCMP', u32 полный размер, u32 1, u32 0, нули),
далее объекты.
Объект : D1D1D1D1, u32 0, u32 длина, u32 0, строка класса, [чанки], D0D0D0D0
Чанк   : C1C1C1C1, u32 версия, u32 длина, u32 0, строка имени, payload, C0C0C0C0
Строка : u16 0x53, u16 n, n байт; затем добор нулями до кратности 8 (АБСОЛЮТНОГО
         смещения в файле). Объект добивается нулями до 8 после D0D0D0D0.
Длина объекта/чанка — от первого маркера до конца конечного маркера включительно.
Между чанками возможен «зазор» (gap): 8 нулей после Shape, либо u32 1 перед
LeadParam.
"""
import struct

OBJ_B, OBJ_E, CH_B, CH_E = 0xD1D1D1D1, 0xD0D0D0D0, 0xC1C1C1C1, 0xC0C0C0C0
HDR = 4096
ZERO = bytes(1)


def rd_str(b, o):
    tag, n = struct.unpack_from("<HH", b, o)
    assert tag == 0x53, (hex(tag), o)
    return b[o + 4:o + 4 + n].decode("latin1"), o + 4 + n


class Chunk:
    def __init__(s, name, ver, payload, gap=b"", off=0):
        s.name, s.ver, s.payload, s.gap, s.off = name, ver, payload, gap, off


class Obj:
    def __init__(s, cls, chunks, off=0, ln=0):
        s.cls, s.chunks, s.off, s.ln = cls, chunks, off, ln


# ---------- чтение ----------
def parse_obj(b, o):
    m, z, ln, z2 = struct.unpack_from("<IIII", b, o)
    assert m == OBJ_B and z == 0 and z2 == 0, (hex(m), z, z2, o)
    cls, p = rd_str(b, o + 16)
    p = (p + 7) & ~7
    end = o + ln
    chunks = []
    while struct.unpack_from("<I", b, p)[0] != OBJ_E:
        m = struct.unpack_from("<I", b, p)[0]
        assert m == CH_B, (hex(m), p)
        ver, cl, _ = struct.unpack_from("<III", b, p + 4)
        name, q = rd_str(b, p + 16)
        q = (q + 7) & ~7
        cend = p + cl
        assert struct.unpack_from("<I", b, cend - 4)[0] == CH_E, (p, cl)
        ch = Chunk(name, ver, b[q:cend - 4], off=p)
        chunks.append(ch)
        p = cend
        g0 = p
        while struct.unpack_from("<I", b, p)[0] not in (CH_B, OBJ_E):
            p += 4
        ch.gap = b[g0:p]
    assert p + 4 == end, (o, p, end)
    return Obj(cls, chunks, o, ln)


def parse_all(b):
    assert b[:4] == b"BCMP" and struct.unpack_from("<I", b, 4)[0] == len(b)
    o, out = HDR, []
    while o < len(b):
        ob = parse_obj(b, o)
        out.append(ob)
        o += (ob.ln + 7) & ~7
    assert o == len(b), (o, len(b))
    return out


# ---------- запись ----------
def _str(s):
    d = s.encode("latin1")
    return struct.pack("<HH", 0x53, len(d)) + d


def dump_chunk(c, pos):
    """pos — абсолютное смещение начала чанка (выравнивание по 8 абсолютное)."""
    head = struct.pack("<IIII", CH_B, c.ver, 0, 0) + _str(c.name)
    head += ZERO * (-(pos + len(head)) % 8)
    body = head + c.payload + struct.pack("<I", CH_E)
    body = body[:8] + struct.pack("<I", len(body)) + body[12:]
    return body + c.gap


def dump_obj(o, pos=HDR):
    head = struct.pack("<IIII", OBJ_B, 0, 0, 0) + _str(o.cls)
    head += ZERO * (-(pos + len(head)) % 8)
    body = head
    for c in o.chunks:
        body += dump_chunk(c, pos + len(body))
    body += struct.pack("<I", OBJ_E)
    body = body[:8] + struct.pack("<I", len(body)) + body[12:]
    return body + ZERO * (-(pos + len(body)) % 8)


def dump_all(objs):
    body = b""
    for o in objs:
        body += dump_obj(o, HDR + len(body))
    total = HDR + len(body)
    head = b"BCMP" + struct.pack("<III", total, 1, 0)
    return head + ZERO * (HDR - len(head)) + body
