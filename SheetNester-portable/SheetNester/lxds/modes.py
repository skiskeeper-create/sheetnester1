"""Справочник режимов резки (лист «Режимы» xlsx) и конвертация в патчи CamParams для .lxds.

Встроенный словарь MODES дублирует лист «Режимы» файла «Себестоимость резки почти финал.xlsx»
(снимок 2026-10-03, 28 строк), чтобы приложение работало без xlsx; сверка с xlsx — тестом
(test_modes, пропускается, если xlsx недоступен). Перечитать справочник из файла: load_modes_from_xlsx().

Сталь 14 и 18 мм добавлены 2026-10-03 интерполяцией соседних режимов (12→16→20) с проверкой
диапазона скоростей из «3Kw Carbon Steel Process Parameters» (14 мм: 0.8–1 м/мин, O2; 18 мм:
0.6–0.7 м/мин, O2) — значения согласованы с технологом не были, при необходимости правятся
в листе «Режимы» приложения.

Единицы: скорость — м/мин (в CamParams — мм/с, перевод м/мин × 50/3, округление до 2 знаков, как в образцах:
8.33, 35); прокол — сек (в Pierce1@TimeStay — мс, ×1000, целое); давление — бар (в CamParams — те же бары).

GasType (код газа в Cut/Pierce .lxds): O2=2 и Воздух=6 подтверждены образцами (Carbon steel-O2-8mm.fsm,
stainless steel-AIR-2mm.fsm); N2=3 НЕ подтверждён образцами — проверить на станке!
"""
from pathlib import Path

XLSX_NAME = "Себестоимость резки почти финал.xlsx"     # ищется на уровень выше папки «Раччет»

# GasType: O2=2, Воздух=6 — подтверждено образцами; N2=3 — НЕ подтверждено образцами, проверить на станке!
GAS_TYPE = {"O2": "2", "N2": "3", "Воздух": "6"}

# Имена для MaterialName: «<материал>-<газ>-<толщина>mm.fsm» (как «stainless steel-AIR-2mm.fsm» в образцах;
# для алюминия — «ALU» по каналу 9 шаблона, иное написание не подтверждено)
FSM_MATERIAL = {"Сталь": "Carbon steel", "Нержавейка": "stainless steel", "Алюминий": "ALU"}
FSM_GAS = {"O2": "O2", "N2": "N2", "Воздух": "AIR"}
# MaterialParams@MaterialName (кит. — как в шаблоне: 不锈钢/铝合金; для стали — 碳钢)
ZH_MATERIAL = {"Сталь": "碳钢", "Нержавейка": "不锈钢", "Алюминий": "铝合金"}

# (материал, толщина_мм) -> режим. Ключи толщины: числа (1 == 1.0 при поиске).
MODES = {
    ("Сталь", 0.5): dict(gas="Воздух", speed_m_min=30, pierce_s=0.15, pressure_bar=12, power_kw=6, air_m3h=75, o2_m3h=0, n2_m3h=0, cost_rub_h=864.062874251497),
    ("Сталь", 1): dict(gas="Воздух", speed_m_min=25, pierce_s=0.2, pressure_bar=12, power_kw=6, air_m3h=75, o2_m3h=0, n2_m3h=0, cost_rub_h=864.062874251497),
    ("Сталь", 2): dict(gas="Воздух", speed_m_min=12, pierce_s=0.3, pressure_bar=14, power_kw=6, air_m3h=78, o2_m3h=0, n2_m3h=0, cost_rub_h=868.082874251497),
    ("Сталь", 3): dict(gas="Воздух", speed_m_min=7, pierce_s=0.5, pressure_bar=14, power_kw=6, air_m3h=80, o2_m3h=0, n2_m3h=0, cost_rub_h=870.762874251497),
    ("Сталь", 4): dict(gas="Воздух", speed_m_min=3.5, pierce_s=0.8, pressure_bar=16, power_kw=6, air_m3h=85, o2_m3h=0, n2_m3h=0, cost_rub_h=877.4628742514969),
    ("Сталь", 5): dict(gas="O2", speed_m_min=3, pierce_s=1, pressure_bar=1, power_kw=7, air_m3h=5, o2_m3h=2.5, n2_m3h=0, cost_rub_h=999.762874251497),
    ("Сталь", 6): dict(gas="O2", speed_m_min=2.8, pierce_s=1.2, pressure_bar=1.2, power_kw=7, air_m3h=5, o2_m3h=2.8, n2_m3h=0, cost_rub_h=1026.402874251497),
    ("Сталь", 8): dict(gas="O2", speed_m_min=1.6, pierce_s=2, pressure_bar=1.5, power_kw=7, air_m3h=5, o2_m3h=3, n2_m3h=0, cost_rub_h=1044.162874251497),
    ("Сталь", 10): dict(gas="O2", speed_m_min=1.5, pierce_s=2.5, pressure_bar=1.8, power_kw=7, air_m3h=5, o2_m3h=3.5, n2_m3h=0, cost_rub_h=1088.562874251497),
    ("Сталь", 12): dict(gas="O2", speed_m_min=1, pierce_s=3.5, pressure_bar=2, power_kw=8, air_m3h=5, o2_m3h=4, n2_m3h=0, cost_rub_h=1140.462874251497),
    ("Сталь", 14): dict(gas="O2", speed_m_min=0.9, pierce_s=4, pressure_bar=2, power_kw=8, air_m3h=5, o2_m3h=4.25, n2_m3h=0, cost_rub_h=1162.662874251497),
    ("Сталь", 16): dict(gas="O2", speed_m_min=0.8, pierce_s=4.5, pressure_bar=2, power_kw=8, air_m3h=5, o2_m3h=4.5, n2_m3h=0, cost_rub_h=1184.862874251497),
    ("Сталь", 18): dict(gas="O2", speed_m_min=0.7, pierce_s=5.5, pressure_bar=2.5, power_kw=8, air_m3h=5, o2_m3h=4.75, n2_m3h=0, cost_rub_h=1207.062874251497),
    ("Сталь", 20): dict(gas="O2", speed_m_min=0.6, pierce_s=6, pressure_bar=2.5, power_kw=8, air_m3h=5, o2_m3h=5, n2_m3h=0, cost_rub_h=1229.2628742514971),
    ("Нержавейка", 1): dict(gas="Воздух", speed_m_min=25, pierce_s=0.3, pressure_bar=12, power_kw=6, air_m3h=75, o2_m3h=0, n2_m3h=0, cost_rub_h=864.062874251497),
    ("Нержавейка", 1.5): dict(gas="Воздух", speed_m_min=20, pierce_s=0.35, pressure_bar=12, power_kw=6, air_m3h=75, o2_m3h=0, n2_m3h=0, cost_rub_h=864.062874251497),
    ("Нержавейка", 2): dict(gas="Воздух", speed_m_min=15, pierce_s=0.4, pressure_bar=14, power_kw=6, air_m3h=78, o2_m3h=0, n2_m3h=0, cost_rub_h=868.082874251497),
    ("Нержавейка", 3): dict(gas="Воздух", speed_m_min=8, pierce_s=0.7, pressure_bar=14, power_kw=6, air_m3h=80, o2_m3h=0, n2_m3h=0, cost_rub_h=870.762874251497),
    ("Нержавейка", 4): dict(gas="Воздух", speed_m_min=6, pierce_s=1, pressure_bar=16, power_kw=6, air_m3h=85, o2_m3h=0, n2_m3h=0, cost_rub_h=877.4628742514969),
    ("Нержавейка", 5): dict(gas="N2", speed_m_min=3, pierce_s=1.3, pressure_bar=18, power_kw=8, air_m3h=5, o2_m3h=0, n2_m3h=15, cost_rub_h=3072.762874251497),
    ("Нержавейка", 6): dict(gas="N2", speed_m_min=2.5, pierce_s=1.6, pressure_bar=18, power_kw=8, air_m3h=5, o2_m3h=0, n2_m3h=18, cost_rub_h=3530.262874251497),
    ("Нержавейка", 8): dict(gas="N2", speed_m_min=1.2, pierce_s=2.5, pressure_bar=20, power_kw=9, air_m3h=5, o2_m3h=0, n2_m3h=20, cost_rub_h=3842.762874251497),
    ("Нержавейка", 10): dict(gas="N2", speed_m_min=0.7, pierce_s=4.5, pressure_bar=22, power_kw=9, air_m3h=5, o2_m3h=0, n2_m3h=25, cost_rub_h=4605.262874251497),
    ("Алюминий", 1): dict(gas="Воздух", speed_m_min=28, pierce_s=0.15, pressure_bar=12, power_kw=6, air_m3h=75, o2_m3h=0, n2_m3h=0, cost_rub_h=864.062874251497),
    ("Алюминий", 2): dict(gas="Воздух", speed_m_min=12, pierce_s=0.3, pressure_bar=14, power_kw=6, air_m3h=78, o2_m3h=0, n2_m3h=0, cost_rub_h=868.082874251497),
    ("Алюминий", 3): dict(gas="Воздух", speed_m_min=7, pierce_s=0.5, pressure_bar=14, power_kw=6, air_m3h=80, o2_m3h=0, n2_m3h=0, cost_rub_h=870.762874251497),
    ("Алюминий", 4): dict(gas="Воздух", speed_m_min=3.5, pierce_s=0.8, pressure_bar=16, power_kw=6, air_m3h=85, o2_m3h=0, n2_m3h=0, cost_rub_h=877.4628742514969),
    ("Алюминий", 6): dict(gas="N2", speed_m_min=1.4, pierce_s=2, pressure_bar=18, power_kw=8, air_m3h=5, o2_m3h=0, n2_m3h=20, cost_rub_h=3835.262874251497),
}


def default_xlsx_path():
    """Стандартный путь к xlsx со справочником: на уровень выше папки «Раччет». -> Path."""
    return Path(__file__).resolve().parents[3] / XLSX_NAME


def load_modes_from_xlsx(path):
    """Прочитать лист «Режимы» из xlsx. -> {(материал, толщина_мм): mode-dict} (формат значений как у MODES).
    openpyxl импортируется лениво (нужен только при чтении из файла)."""
    import openpyxl
    wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    try:
        ws = wb["Режимы"]
        table = {}
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[0] is None:
                continue
            material, th, gas = str(row[0]).strip(), float(row[1]), str(row[2]).strip()
            table[(material, th)] = dict(
                gas=gas, speed_m_min=float(row[3]), pierce_s=float(row[4]), pressure_bar=float(row[5]),
                power_kw=float(row[6]), air_m3h=float(row[7]), o2_m3h=float(row[8]), n2_m3h=float(row[9]),
                cost_rub_h=float(row[10]) if row[10] is not None else 0.0)
        return table
    finally:
        wb.close()


def find_mode(material, thickness, table=None):
    """Режим по материалу и толщине (мм; число или строка). -> dict (с добавленными material/thickness) или None,
    если в справочнике нет. table=None — встроенный MODES."""
    table = MODES if table is None else table
    try:
        th = float(thickness)
    except (TypeError, ValueError):
        return None
    m = table.get((str(material).strip(), th))
    if m is None:
        return None
    out = dict(m)
    out["material"], out["thickness"] = str(material).strip(), th
    return out


def _f2(v):
    """Число -> строка с округлением до 2 знаков и без лишних нулей: 13.3333 -> '13.33', 500.0 -> '500', 2.5 -> '2.5'."""
    s = "%.2f" % float(v)
    return s.rstrip("0").rstrip(".")


def _g(v):
    """Число -> компактная строка (%g): 16 -> '16', 0.5 -> '0.5', 1.8 -> '1.8'."""
    return "%g" % float(v)


def mode_to_camparams(mode):
    """mode (dict из find_mode, с ключами material/thickness) -> dict патчей для CamParams канала 1 (рез):
    "WorkSpeed" — мм/с; "Cut/GasPressure", "Cut/GasType"; "Pierce1/TimeStay" — мс;
    "MaterialParams/Thickness", "MaterialParams/MaterialName" (кит. имя); "MaterialName" — '<материал>-<газ>-<толщина>mm.fsm';
    "Note" — описание режима. Газ прокола (Pierce1@GasType/GasPressure) остаётся из шаблона — в профиле шаблона
    он несогласован с газом реза намеренно (пирсинг с O2 при резке AIR)."""
    material = mode.get("material", "")
    thickness = mode.get("thickness", 0)
    gas = mode["gas"]
    speed_mm_s = float(mode["speed_m_min"]) * 50.0 / 3.0        # м/мин -> мм/с
    pierce_ms = int(round(float(mode["pierce_s"]) * 1000))      # сек -> мс
    eng_mat = FSM_MATERIAL.get(material, material)
    note = ("3000W         %smm        %s  \r\nGas: %s        Pressure: %s bar        Speed: %s m/min        "
            "Pierce: %s s" % (_g(thickness), eng_mat, gas, _g(mode["pressure_bar"]),
                              _g(mode["speed_m_min"]), _g(mode["pierce_s"])))
    return {
        "WorkSpeed": _f2(speed_mm_s),
        "Cut/GasPressure": _f2(mode["pressure_bar"]),
        "Cut/GasType": GAS_TYPE[gas],
        "Pierce1/TimeStay": str(pierce_ms),
        "MaterialParams/Thickness": _g(thickness),
        "MaterialParams/MaterialName": ZH_MATERIAL.get(material, material),
        "MaterialName": "%s-%s-%smm.fsm" % (eng_mat, FSM_GAS[gas], _g(thickness)),
        "Note": note,
    }
