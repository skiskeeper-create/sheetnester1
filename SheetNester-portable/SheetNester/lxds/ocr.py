# -*- coding: utf-8 -*-
"""Артикулы деталей (ТЗ разд. 8.4): распознавание и перенос на слой гравировки (ENGRAVE_TEXT, канал 9).

Источники текста (ТЗ 8.4.3):
- текстовые сущности DXF (TEXT/MTEXT/ATTRIB) — напрямую, без OCR: dxf_import складывает их в report.texts;
- текст, преобразованный в векторные контуры (кривые), — рендер контуров в растр + PaddleOCR.
  ВАЖНО: PaddleOCR на CPU обязан работать с отключённым mkldnn (FLAGS_use_mkldnn=0 / enable_mkldnn=False),
  иначе падение «ConvertPirAttribute2RuntimeAttribute not support» (paddle 3.x).

Шаблон артикула по умолчанию (ТЗ 8.4.2): ^[A-Za-zА-Яа-я0-9]{1,6}[-_][A-Za-zА-Яа-я0-9]{1,6}$ — один на заказ,
редактируется технологом (параметр pattern). Всё, что шаблону не соответствует (размерки, штампы, логотипы),
игнорируется (ТЗ 8.4.1).

Статусы (ТЗ 8.4.7/8.4.8): ok → розовый слой; outside (вне контура) → disputed; найдено несколько → все, ручное
подтверждение (интерфейс — позже; модуль возвращает всё для правки).
"""
import math
import re
from dataclasses import dataclass, field

import geomops as go
from model import CH_ENGRAVE, LAYER_ENGRAVE, LAYER_HOLE, LAYER_MARK, LAYER_OUTLINE, Shape

ARTICLE_RE = r"^[A-Za-zА-Яа-я0-9]{1,6}[-_][A-Za-zА-Яа-я0-9]{1,6}$"
DEFAULT_FONT = "arial.ttf"


def compile_pattern(pattern=None):
    """Шаблон артикула (regex-строка) -> re.Pattern. None — шаблон по умолчанию ТЗ."""
    return re.compile(pattern or ARTICLE_RE)


@dataclass
class ArticleHit:
    """Распознанный артикул. source: 'text' (TEXT/MTEXT) | 'curves' (OCR контуров). shapes — для source='curves'."""
    text: str
    x: float
    y: float
    h: float                       # высота текста, мм
    rot_deg: float = 0.0
    source: str = "text"
    conf: float = 1.0
    status: str = "ok"             # ok | disputed
    shapes: list = field(default_factory=list)


# ---------------------------------------------------------------------------------------------------------
#  геометрия-помощники
# ---------------------------------------------------------------------------------------------------------
def _flat(geo, per_seg=16):
    """geo -> [(x, y), ...] ломаная (сэмплирование Безье/дуг). Для hit-тестов и рендера."""
    bez = go.to_bezier(geo)
    pts = []
    for seg in bez[2]:
        for i in range(per_seg + 1):
            t = i / per_seg
            mt = 1.0 - t
            x = mt ** 3 * seg[0][0] + 3 * mt ** 2 * t * seg[1][0] + 3 * mt * t ** 2 * seg[2][0] + t ** 3 * seg[3][0]
            y = mt ** 3 * seg[0][1] + 3 * mt ** 2 * t * seg[1][1] + 3 * mt * t ** 2 * seg[2][1] + t ** 3 * seg[3][1]
            pts.append((x, y))
    return pts


def _point_in_poly(pts, x, y):
    """Лучевой тест точки в многоугольнике. pts — замкнутая ломаная."""
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def point_in_outline(part, x, y):
    """Точка внутри внешнего контура детали? None — контура нет или он не сэмплируется."""
    if part.outline < 0 or part.outline >= len(part.shapes):
        return None
    try:
        pts = _flat(part.shapes[part.outline].geo)
    except Exception:
        return None
    if len(pts) < 3:
        return None
    return _point_in_poly(pts, x, y)


# ---------------------------------------------------------------------------------------------------------
#  TEXT/MTEXT -> контуры гравировки
# ---------------------------------------------------------------------------------------------------------
def text_to_shapes(text, x, y, h, rot_deg=0.0, font_name=DEFAULT_FONT):
    """Текст -> [Shape(LAYER_ENGRAVE, CH_ENGRAVE, LwPoly)] — контуры букв (TTF через ezdxf).
    (x, y) — точка вставки (левый край базовой линии, halign=left/valign=baseline — допущение MVP),
    h — высота (cap height), мм; rot_deg — поворот против ч.с. Без ezdxf/font — RuntimeError."""
    try:
        from ezdxf.fonts import fonts
        font = fonts.make_font(font_name, float(h))
        cap_h = float(font.cap_height)
        paths = font.text_glyph_paths(text, cap_h)
    except Exception as e:
        raise RuntimeError("шрифт %r недоступен (%s: %s) — гравировка текста невозможна"
                           % (font_name, type(e).__name__, e))
    out = []
    for p in paths:
        pts = [(float(px), float(py)) for px, py in p.flattening(0.05)]
        if len(pts) < 2:
            continue
        if p.is_closed and (pts[0] != pts[-1]):
            pts.append(pts[0])
        geo = ("LwPoly", bool(p.is_closed), [(px, py, 0.0) for px, py in pts])
        if rot_deg:
            geo = go.transform(geo, angle=math.radians(rot_deg))
        geo = go.transform(geo, dx=float(x), dy=float(y))
        out.append(Shape(LAYER_ENGRAVE, CH_ENGRAVE, geo))
    return out


def find_text_articles(part, report, pattern=None):
    """report.texts -> [ArticleHit] только подходящие под шаблон. Внутри контура — ok, вне/нет контура — disputed."""
    rx = compile_pattern(pattern)
    hits = []
    for s, x, y, h, rot in (report.texts or []):
        s = str(s).strip()
        if not rx.match(s):
            continue                            # размерки, штампы и пр. — удаляются (ТЗ 8.4.1)
        inside = point_in_outline(part, float(x), float(y))
        hits.append(ArticleHit(text=s, x=float(x), y=float(y), h=float(h), rot_deg=float(rot or 0.0),
                               source="text", conf=1.0, status="ok" if inside else "disputed"))
    return hits


# ---------------------------------------------------------------------------------------------------------
#  OCR контуров (текст-как-кривые)
# ---------------------------------------------------------------------------------------------------------
_engine = None


def _ocr_engine():
    """Ленивый синглтон PaddleOCR (mkldnn отключён!)."""
    global _engine
    if _engine is None:
        from paddleocr import PaddleOCR
        _engine = PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False,
                            use_textline_orientation=False, lang="ru", enable_mkldnn=False)
    return _engine


def _clusters(shapes, gap=None):
    """Разбить фигуры на пространственные кластеры (union-find по bbox). -> [[Shape, ...], ...]."""
    boxes = [go.bbox(s.geo) for s in shapes]
    n = len(shapes)
    if gap is None:
        hs = sorted(b[3] - b[1] for b in boxes)
        gap = max(5.0, (hs[len(hs) // 2] if hs else 5.0))
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            ax0, ay0, ax1, ay1 = boxes[i]
            bx0, by0, bx1, by1 = boxes[j]
            if not (ax1 + gap < bx0 or bx1 + gap < ax0 or ay1 + gap < by0 or by1 + gap < ay0):
                union(i, j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(shapes[i])
    return list(groups.values())


def render_cluster(shapes, width_px=1200):
    """Кластер фигур -> PIL.Image (чёрное на белом), масштаб под ширину. Для OCR."""
    from PIL import Image, ImageDraw
    x0 = min(go.bbox(s.geo)[0] for s in shapes)
    y0 = min(go.bbox(s.geo)[1] for s in shapes)
    x1 = max(go.bbox(s.geo)[2] for s in shapes)
    y1 = max(go.bbox(s.geo)[3] for s in shapes)
    w_mm, h_mm = max(x1 - x0, 0.1), max(y1 - y0, 0.1)
    scale = width_px / w_mm
    img = Image.new("L", (width_px, max(2, int(h_mm * scale) + 20)), 255)
    d = ImageDraw.Draw(img)
    for s in shapes:
        pts = _flat(s.geo)
        d.line([(int((px - x0) * scale), int((y1 - py) * scale + 10)) for px, py in pts],
               fill=0, width=max(2, int(scale * 0.3)))
    return img


def find_curve_articles(part, pattern=None, engine=None):
    """Кандидатные фигуры (не CUT_OUTLINE/CUT_HOLE/MARK_RISKS) -> кластеры -> рендер -> OCR -> [ArticleHit].
    Совпавшие кластеры помечаются (слой/канал гравировки) в возвращённых hit.shapes — запись в .lxds как обычно.
    Несколько совпадений — все (ТЗ 8.4.8, ручное подтверждение — в интерфейсе)."""
    rx = compile_pattern(pattern)
    cand = [s for s in part.shapes
            if s.layer not in (LAYER_OUTLINE, LAYER_HOLE, LAYER_MARK, LAYER_ENGRAVE)]
    hits = []
    if not cand:
        return hits
    eng = engine or _ocr_engine()
    for cl in _clusters(cand):
        x0 = min(go.bbox(s.geo)[0] for s in cl)
        y1 = max(go.bbox(s.geo)[3] for s in cl)
        img = render_cluster(cl)
        import numpy as np
        arr = np.asarray(img.convert("RGB"))
        for res in eng.predict(arr):
            for txt, conf in zip(res["rec_texts"], res["rec_scores"]):
                t = str(txt).strip()
                if rx.match(t) and float(conf) >= 0.7:          # порог уверенности ТЗ 12
                    for s in cl:                                # совпавший кластер -> розовый слой
                        s.layer, s.channel = LAYER_ENGRAVE, CH_ENGRAVE
                    hits.append(ArticleHit(text=t, x=x0, y=y1, h=8.0, source="curves",
                                           conf=float(conf), status="ok", shapes=cl))
    return hits


# ---------------------------------------------------------------------------------------------------------
#  Применение: артикулы -> розовый слой
# ---------------------------------------------------------------------------------------------------------
def apply_articles(part, report, pattern=None, use_ocr=False, font_name=DEFAULT_FONT):
    """Найти артикулы и перенести на слой гравировки. -> dict(found=[ArticleHit...], disputed=[ArticleHit...],
    added=int). Текстовые сущности (ок) превращаются в контуры букв (text_to_shapes) и добавляются в part.shapes;
    OCR-контуры (use_ocr=True) — помечаются LAYER_ENGRAVE/CH_ENGRAVE. Повторный вызов дубли не создаёт."""
    found, disputed = [], []
    seen = {(s.text, round(s.x, 1), round(s.y, 1)) for s in getattr(part, "_article_hits", [])}
    for hit in find_text_articles(part, report, pattern):
        key = (hit.text, round(hit.x, 1), round(hit.y, 1))
        if key in seen:
            continue
        (found if hit.status == "ok" else disputed).append(hit)
        seen.add(key)
    added = 0
    for hit in found:                                   # контуры букв найденных текстов -> розовый слой
        shapes = text_to_shapes(hit.text, hit.x, hit.y, hit.h, hit.rot_deg, font_name)
        part.shapes.extend(shapes)
        added += len(shapes)
    if use_ocr:
        for hit in find_curve_articles(part, pattern):
            key = (hit.text, round(hit.x, 1), round(hit.y, 1))
            if key in seen:
                continue
            found.append(hit)
            seen.add(key)
    part._article_hits = list(getattr(part, "_article_hits", [])) + found + disputed
    return dict(found=found, disputed=disputed, added=added)
