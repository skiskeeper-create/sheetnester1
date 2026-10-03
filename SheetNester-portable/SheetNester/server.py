"""SheetNester — локальный сервер: калькулятор себестоимости + нестинг DXF -> .lxds (CypCut Pro).

Запуск: python server.py [--port N] [--no-browser]. Слушает только 127.0.0.1 (с других компьютеров недоступен).
"""
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP = Path(__file__).resolve().parent
APP_VER = "2026-10-03.2"          # версия сборки: отдаётся в /api/ping, сверяется со страницей
sys.path.insert(0, str(APP / "lxds"))

import dxf_import  # noqa: E402
import model      # noqa: E402
import modes      # noqa: E402
import nesting    # noqa: E402
import ocr        # noqa: E402
import warehouse  # noqa: E402

HTML = APP / "Расчёт себестоимости резки.html"
TEMPLATE = APP / "data" / "template.lxds"
OUT_ROOT = APP / "раскрой"
UPLOAD = Path(tempfile.mkdtemp(prefix="sheetnester_"))
PARTS = {}                      # id -> dict(part, report, file)
LOCK = threading.Lock()
STOCK = warehouse.Warehouse(APP / "data" / "warehouse.db")


def _safe_name(s):
    return re.sub(r'[\\/:*?"<>|]+', "_", str(s)).strip() or "файл"


def api_import(req):
    out = []
    pattern = (req.get("pattern") or "").strip() or None
    if pattern:
        try:
            ocr.compile_pattern(pattern)
        except Exception:
            pattern = None                 # невалидный regex — будет шаблон по умолчанию ТЗ
    rx = ocr.compile_pattern(pattern)
    for f in req.get("files", []):
        name = _safe_name(Path(f["name"]).name)
        pid = uuid.uuid4().hex[:12]
        d = UPLOAD / pid
        d.mkdir()
        path = d / name
        path.write_bytes(base64.b64decode(f["data"]))
        try:
            part, rep = dxf_import.load_part(path)
        except Exception as e:                     # файл не прочитан — показать ошибку, остальные грузятся
            out.append(dict(id=pid, file=name, error=f"{type(e).__name__}: {e}"))
            continue
        if not rep.has_outline or part is None or part.outline < 0:
            out.append(dict(id=pid, file=name, error=rep.error or "не найден замкнутый внешний контур",
                            warnings=rep.warnings))
            continue
        with LOCK:
            PARTS[pid] = dict(part=part, report=rep, file=name)
        m = nesting.part_metrics(part)
        meta = rep.meta or {}
        holes = sum(1 for s in part.shapes if s.layer == model.LAYER_HOLE)
        art = ocr.apply_articles(part, rep, pattern=pattern)   # артикулы -> розовый слой (ТЗ 8.4); «спорные» — в ответ
        warnings = list(rep.warnings)
        skipped = []
        for s, *_ in (rep.texts or []):
            t = str(s).strip()
            if t and not rx.match(t):
                skipped.append(t)
        if skipped:                    # почему текст на детали не попал в гравировку — видно сразу
            warnings.append("текст не под шаблон артикула (не гравируется): "
                            + ", ".join(skipped[:3]) + ("…" if len(skipped) > 3 else ""))
        out.append(dict(id=pid, file=name, name=meta.get("article") or Path(name).stem,
                        thickness=meta.get("thickness"), qty=meta.get("qty") or 1,
                        w=round(m["bbox"][0], 2), h=round(m["bbox"][1], 2), area=m["area"], cut_m=m["cut_m"],
                        holes=holes, warnings=warnings, svg=nesting.part_svg(part),
                        article=(art["found"][0].text if art["found"] else None),
                        article_disputed=[h.text for h in art["disputed"]]))
    return {"parts": out}


def api_nest(req):
    sw, sh = float(req["sheet_w"]), float(req["sheet_h"])
    groups = {}
    for it in req["items"]:
        qty = int(float(it.get("qty") or 0))
        if qty <= 0:
            continue
        with LOCK:
            rec = PARTS.get(it["id"])
        if rec is None:
            raise ValueError(f"деталь {it.get('name')} не найдена — загрузите DXF заново")
        key = (it["material"], float(it["thickness"]))
        groups.setdefault(key, []).append((it["id"], it.get("name") or rec["file"], rec["part"], qty))
    if not groups:
        raise ValueError("нет деталей с количеством > 0")
    out_dir = Path(req.get("out_dir") or "") if req.get("out_dir") else OUT_ROOT
    out_dir = out_dir / datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    lead = model.LEAD_LINE if req.get("lead_type") == "line" else model.LEAD_ARC
    result = []
    for (mat, th), entries in groups.items():
        mode = modes.find_mode(mat, th)            # режим из справочника; None — тогда CamParams шаблона
        keepouts = [tuple(float(v) for v in r) for r in (req.get("keepouts") or [])]
        r = nesting.run_group(entries, sw, sh, th, out_dir, _safe_name(f"{mat}_{th:g}мм"), TEMPLATE,
                              margin=float(req.get("margin", 10)), gap=float(req.get("gap", 5)),
                              res=float(req.get("res", 2.5)), rotate=bool(req.get("rotate", True)),
                              bridge_mode=req.get("bridge_mode", "diagonal"),
                              bridge_size=float(req.get("bridge_size", 1.0)), lead_type=lead,
                              mode=mode, keepouts=keepouts or None,
                              priority=req.get("priority", "area"),
                              free_angles=[float(a) for a in (req.get("free_angles") or [])],
                              place=req.get("place", "blf"))
        names = {k: n for k, n, _, _ in entries}
        stock = STOCK.available(mat, th, sw, sh)
        warning = None
        if stock["qty"] < len(r["sheets"]):
            warning = ("на складе %d подходящих листов %s %sмм %g×%g, требуется %d"
                       % (stock["qty"], mat, th, sw, sh, len(r["sheets"])))
        result.append(dict(material=mat, thickness=th, sheets=r["sheets"],
                           unplaced=[dict(name=names[k], qty=q) for k, q in r["unplaced"]],
                           per_type={k: dict(name=names[k], **v) for k, v in r["per_type"].items()},
                           stock=dict(warning=warning, available=stock["qty"])))
    return {"groups": result, "out_dir": str(out_dir)}


def api_stock_apply(req):
    """Применить раскладку к складу: списать листы и создать остаток (кнопка «Списать листы со склада»)."""
    used = req.get("used")
    r = STOCK.consume(req["material"], float(req["thickness"]), count=int(req["count"]),
                      sheet_l=float(req["sheet_l"]), sheet_w=float(req["sheet_w"]),
                      used=(float(used[0]), float(used[1])) if used else None,
                      source=req.get("source") or "")
    out = dict(spent=r["spent"], requested=int(req["count"]), remnant_id=r["remnant_id"])
    if r["remnant_id"]:
        for m in STOCK.list_remnants():
            if m["id"] == r["remnant_id"]:
                out["remnant"] = "%g×%g мм × %d шт" % (m["length"], m["width"], m["qty"])
    return out


def api_stock_list(req):
    return {"sheets": STOCK.list_sheets(req.get("material") or None,
                                        req.get("thickness") if req.get("thickness") not in (None, "") else None),
            "remnants": STOCK.list_remnants(req.get("material") or None,
                                            req.get("thickness") if req.get("thickness") not in (None, "") else None)}


def api_stock_add(req):
    return {"id": STOCK.add_sheet(**req.get("row", {}))}


def api_stock_writeoff(req):
    return {"balance": STOCK.write_off(req["table"], int(req["id"]), int(req["qty"]),
                                       req.get("reason") or "")}


def api_stock_adjust(req):
    return {"balance": STOCK.adjust(req["table"], int(req["id"]), int(req["qty"]), req.get("reason") or "")}


def api_stock_import(req):
    ok, errors = STOCK.import_csv(req.get("text") or "")
    return {"ok": ok, "errors": errors}


def api_stock_history(req):
    return {"history": STOCK.history(int(req.get("limit") or 200))}


def api_stock_report(req):
    return STOCK.report()


def api_open(req):
    p = Path(req["path"])
    if not p.exists():
        raise ValueError("папка не найдена")
    os.startfile(str(p if p.is_dir() else p.parent))       # Windows
    return {"ok": True}


ROUTES = {"/api/import": api_import, "/api/nest": api_nest, "/api/open": api_open,
          "/api/stock/list": api_stock_list, "/api/stock/add": api_stock_add,
          "/api/stock/writeoff": api_stock_writeoff, "/api/stock/adjust": api_stock_adjust,
          "/api/stock/import": api_stock_import, "/api/stock/history": api_stock_history,
          "/api/stock/report": api_stock_report, "/api/stock/apply": api_stock_apply}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):         # тихий режим: без строки на каждый запрос
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/ping":
            return self._send(200, json.dumps({"ok": True, "out_root": str(OUT_ROOT), "ver": APP_VER}),
                              "application/json")
        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        fn = ROUTES.get(self.path)
        if fn is None:
            return self._send(404, "not found", "text/plain; charset=utf-8")
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            res = fn(req)
            self._send(200, json.dumps(res, ensure_ascii=False), "application/json; charset=utf-8")
        except Exception as e:
            traceback.print_exc()
            self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False), "application/json; charset=utf-8")


def _kill_stale_servers(lo=8765, hi=8784):
    """Закрыть висящие экземпляры SheetNester (python с server.py), слушающие порты приложения:
    иначе старый процесс держит старый код/порт, а браузер продолжает ходить на него
    (причина классической путаницы «правки не применились»)."""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
        pids = set()
        for ln in out.splitlines():
            t = ln.split()
            if len(t) >= 5 and t[0] == "TCP" and t[2] in ("0.0.0.0:0", "[::]:0"):
                try:
                    port = int(t[1].rsplit(":", 1)[1])
                except ValueError:
                    continue
                if lo <= port <= hi:
                    pids.add(int(t[-1]))
        if not pids:
            return
        flt = " or ".join("ProcessId=%d" % p for p in pids)
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"%s\" | "
             "Where-Object { $_.CommandLine -match 'server\\.py' } | Select-Object -ExpandProperty ProcessId"
             % flt],
            capture_output=True, text=True).stdout.split()
        for p in out:
            if int(p) != os.getpid():
                print("закрыт предыдущий экземпляр сервера (PID %s) — запускаю свежий" % p)
                subprocess.run(["taskkill", "/PID", p, "/F"], capture_output=True)
    except Exception:
        pass                                 # не мешаем запуску, если что-то пошло не так


def main():
    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1]) if "--port" in args else 8765
    _kill_stale_servers(port, port + 19)
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    else:
        sys.exit("нет свободного порта")
    url = f"http://127.0.0.1:{srv.server_port}/"
    print(f"SheetNester запущен: {url}\nНе закрывайте это окно, пока работаете. Закрыть окно = остановить программу.")
    if "--no-browser" not in args:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
