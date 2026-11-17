#!/usr/bin/env python3
"""
Експорт і імпорт бази товарів каси у CSV.

  python goods_csv.py export [файл.csv]   — вивантажити товари з локальної бази каси (для сайту)
  python goods_csv.py check  файл.csv     — розібрати CSV у форматі каси і показати план змін

Робота з Checkbox (головна база товарів):
  python goods_csv.py pull [файл.csv]     — вивантажити номенклатуру з Checkbox у його форматі
  python goods_csv.py push файл.csv       — завантажити файл у форматі Checkbox, показати результат
                                            перевірки і після підтвердження застосувати зміни
     --yes                 застосувати без запитання
     --auto-supply         створити поставки для нових товарів, якщо у файлі є кількість
     --barcode-duplicates  НЕ ігнорувати дублікати штрихкодів (за замовчуванням Checkbox їх ігнорує)
  push приймає обидва формати:
    • файл Checkbox (з pull) — завантажується як є;
    • файл у форматі каси (з export або власний: Назва/Код/Ціна…) — спершу перевіряється
      як у check, потім нові й змінені товари записуються в Checkbox.
  Код наявного товару змінити не можна: Checkbox розпізнає товари за кодом.

Головна база товарів — Checkbox. Локальна база (checkbox.db) — її копія після
синхронізації. Формат файлу Checkbox — за прикладом checkbox_goods.csv:
Windows-1251, роздільник «,», колонки «ім'я товару, код, група товарів, штрихкод,
штрихкоди, уктзед, ціна, ваговий товар, тип товару, податкові ставки».

Формат файлу каси (так само при експорті й імпорті):
  id;code;barcode;name;price;count;is_weight;uktzed;tax_codes
  • price      — ціна в гривнях: 53.49 (при імпорті можна й 53,49)
  • count      — залишок у штуках або кілограмах; лише для довідки, імпорт його не змінює
  • is_weight  — 1 / 0 (при імпорті також так/ні, true/false)
  • tax_codes  — коди податкових ставок через кому: 5,2 (при імпорті можна й літерами: Д,Б)
  • id         — UUID товару в Checkbox; для нових товарів залишити порожнім
Замість англійських заголовків можна українські: Назва, Код, Штрихкод, Ціна,
Залишок, Ваговий, УКТЗЕД, Податки, ID.

Кодування файлу каси: експорт — UTF-8 з BOM (Excel відкриває кирилицю правильно),
імпорт — UTF-8 або Windows-1251.
"""

import argparse
import csv
import io
import re
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import json

from checkbox_app import (CheckboxAPI, CheckboxError, LocalDB, good_tax_codes, is_excise_only,
                          load_config, log, net, setup_logging, sync_all)

COLUMNS = ["id", "code", "barcode", "name", "price", "count", "is_weight", "uktzed", "tax_codes"]

# заголовки, які розуміє імпорт (без урахування регістру й пробілів)
HEADER_ALIASES = {
    "id": "id", "uuid": "id", "ід": "id",
    "code": "code", "код": "code", "код товару": "code", "артикул": "code",
    "barcode": "barcode", "штрихкод": "barcode", "штрих-код": "barcode", "ean": "barcode",
    "name": "name", "назва": "name", "найменування": "name", "назва товару": "name",
    "price": "price", "ціна": "price", "ціна, грн": "price", "ціна грн": "price",
    "count": "count", "залишок": "count", "кількість": "count",
    "is_weight": "is_weight", "ваговий": "is_weight", "вагова": "is_weight", "ваговий товар": "is_weight",
    "uktzed": "uktzed", "уктзед": "uktzed", "код уктзед": "uktzed",
    "tax_codes": "tax_codes", "tax": "tax_codes", "податки": "tax_codes", "податок": "tax_codes",
    "податкові ставки": "tax_codes",
}
YES = {"1", "так", "true", "yes", "y", "т", "+", "ваговий", "вага"}
NO = {"", "0", "ні", "false", "no", "n", "н", "-"}


# ───────────────────────── Експорт ─────────────────────────

def fmt_price(kop):
    return f"{kop / 100:.2f}" if kop is not None else ""


def fmt_count(thousandths, is_weight):
    if thousandths is None:
        return ""
    v = Decimal(thousandths) / 1000
    return f"{v:.3f}" if is_weight else f"{v.normalize():f}"


def export_goods(db, path):
    rows = db.search("")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(COLUMNS)
        for g in rows:
            w.writerow([
                g["id"], g["code"], g["barcode"] or "", g["name"], fmt_price(g["price"]),
                fmt_count(g["count"], g["is_weight"]), 1 if g["is_weight"] else 0,
                g["uktzed"] or "", ",".join(str(c) for c in good_tax_codes(g)),
            ])
    return len(rows)


# ───────────────────────── Читання CSV ─────────────────────────

def read_text(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise ValueError("Не вдалося визначити кодування файлу (очікується UTF-8 або Windows-1251)")


def read_csv(path):
    text, enc = read_text(path)
    first = text.split("\n", 1)[0]
    # роздільник: той, якого найбільше в рядку заголовків
    delim = max(";,\t", key=first.count)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = list(reader)
    if not rows:
        raise ValueError("Файл порожній")
    header = [HEADER_ALIASES.get(h.strip().lower().replace("﻿", "")) for h in rows[0]]
    unknown = [rows[0][i] for i, h in enumerate(header) if h is None and rows[0][i].strip()]
    if "name" not in header or "code" not in header:
        raise ValueError("У файлі мають бути колонки «name» (Назва) і «code» (Код). "
                         f"Знайдено заголовки: {', '.join(rows[0])}")
    records = []
    for n, row in enumerate(rows[1:], start=2):           # n — номер рядка у файлі
        if not any(c.strip() for c in row):
            continue
        rec = {}
        for i, key in enumerate(header):
            if key and i < len(row):
                rec[key] = row[i]
        records.append((n, rec))
    return records, {"encoding": enc, "delimiter": delim, "unknown_columns": unknown,
                     "columns": {h for h in header if h}}


# ───────────────────────── Перевірка ─────────────────────────

def parse_price(s):
    s = (s or "").strip().replace(" ", "").replace(" ", "").replace("грн", "").replace(",", ".")
    if not s:
        raise ValueError("ціна не вказана")
    try:
        v = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"ціна «{s}» — не число")
    if v < 0:
        raise ValueError("ціна від'ємна")
    if v != v.quantize(Decimal("0.01")):
        raise ValueError(f"ціна «{s}» має більше двох знаків після коми")
    return int(v * 100)


def parse_bool(s):
    v = (s or "").strip().lower()
    if v in YES:
        return True
    if v in NO:
        return False
    raise ValueError(f"«{s}» — очікується 1/0 або так/ні")


def tax_lookup(db):
    by_code, by_symbol = {}, {}
    for t in db.taxes():
        t = dict(t)
        by_code[t["code"]] = t
        if t.get("symbol"):
            by_symbol[t["symbol"].strip().upper()] = t
    return by_code, by_symbol


def parse_taxes(s, by_code, by_symbol):
    parts = [p.strip() for p in re.split(r"[,;/ ]+", s or "") if p.strip()]
    taxes = []
    for p in parts:
        t = by_code.get(int(p)) if p.isdigit() else by_symbol.get(p.upper())
        if not t:
            known = ", ".join(f"{c} ({t['symbol']})" for c, t in sorted(by_code.items()))
            raise ValueError(f"невідома податкова ставка «{p}». Є: {known or 'довідник порожній — синхронізуйте касу'}")
        if t not in taxes:
            taxes.append(t)
    # порядок, якого вимагає Checkbox: спершу ПДВ, потім акциз
    return [t["code"] for t in sorted(taxes, key=lambda t: 1 if is_excise_only(t) else 0)]


def _cp1251_ok(ch):
    try:
        ch.encode("cp1251")
        return True
    except UnicodeEncodeError:
        return False


def check_row(rec, by_code, by_symbol, cols, is_new):
    """
    Повертає (good, errors, warnings). good — нормалізовані дані для Checkbox.
    Поля, колонок яких у файлі немає, не потрапляють у good, тобто не змінюються.
    Порожня клітинка в наявній колонці — «очистити» (для необов'язкових полів).
    """
    errors, warnings = [], []
    raw_code = rec.get("code") or ""
    code = raw_code.strip()
    if not code:
        errors.append("код не вказано")
    elif code != raw_code:
        warnings.append("пробіли на початку/в кінці коду прибрано")
    name = " ".join((rec.get("name") or "").split())
    if not name:
        errors.append("назва не вказана")
    elif len(name) > 255:
        errors.append("назва довша за 255 символів")

    for label, value in (("назва", name), ("код", code)):
        bad = "".join(sorted({ch for ch in value if not _cp1251_ok(ch)}))
        if bad:
            errors.append(f"{label}: символи «{bad}» не підтримуються файлом Checkbox (Windows-1251)")

    good = {"id": (rec.get("id") or "").strip() or None, "code": code, "name": name}
    if "price" in cols or is_new:
        try:
            good["price"] = parse_price(rec.get("price"))
            if good["price"] == 0:
                warnings.append("ціна 0")
        except ValueError as e:
            errors.append(str(e))
    if "is_weight" in cols or is_new:
        try:
            good["is_weight"] = parse_bool(rec.get("is_weight"))
        except ValueError as e:
            errors.append(f"ваговий: {e}")
    if "barcode" not in cols:
        rec = dict(rec, barcode=None)

    barcode = re.sub(r"\s", "", rec.get("barcode") or "")
    if barcode.endswith(".0"):            # Excel іноді зберігає штрихкод як число з «.0»
        barcode = barcode[:-2]
    if barcode and ("e+" in barcode.lower()):
        errors.append(f"штрихкод «{barcode}» зіпсовано Excel (експоненційний запис) — задайте колонці формат «Текст»")
    elif barcode and not barcode.isdigit():
        warnings.append(f"штрихкод «{barcode}» містить не лише цифри")
    elif barcode and len(barcode) not in (8, 12, 13, 14):
        warnings.append(f"штрихкод «{barcode}» нестандартної довжини ({len(barcode)})")
    if "barcode" in cols or is_new:
        good["barcode"] = barcode or None

    uktzed = " ".join((rec.get("uktzed") or "").split())
    if uktzed and not re.fullmatch(r"[\d ]+", uktzed):
        errors.append(f"УКТЗЕД «{uktzed}» має містити лише цифри")
    elif uktzed and len(uktzed.replace(" ", "")) not in (4, 6, 8, 10):
        warnings.append(f"УКТЗЕД «{uktzed}» нестандартної довжини")
    if "uktzed" in cols or is_new:
        good["uktzed"] = uktzed or None

    if "tax_codes" in cols or is_new:
        try:
            good["tax_codes"] = parse_taxes(rec.get("tax_codes"), by_code, by_symbol)
            if not good["tax_codes"]:
                warnings.append("без податкової ставки")
        except ValueError as e:
            errors.append(str(e))
    return good, errors, warnings


FIELD_NAMES = {"code": "код", "name": "назва", "price": "ціна", "barcode": "штрихкод",
               "is_weight": "ваговий", "uktzed": "УКТЗЕД", "tax_codes": "податки"}


def diff(existing, good):
    """Список змін «поле: було → стало» відносно локальної копії."""
    old = {
        "code": (existing["code"] or "").strip(), "name": existing["name"], "price": existing["price"],
        "barcode": existing["barcode"] or None, "is_weight": bool(existing["is_weight"]),
        "uktzed": existing["uktzed"] or None, "tax_codes": good_tax_codes(existing),
    }
    changes = []
    for k, label in FIELD_NAMES.items():
        if k in good and good[k] != old[k]:
            show = (lambda v: fmt_price(v)) if k == "price" else (lambda v: "—" if v in (None, [], "") else
                                                                    (",".join(map(str, v)) if isinstance(v, list) else
                                                                     ("так" if v is True else "ні" if v is False else str(v))))
            changes.append(f"{label}: {show(old[k])} → {show(good[k])}")
    return changes


def find_existing(rec, by_id, by_local_code):
    rid = (rec.get("id") or "").strip()
    if rid:
        return by_id.get(rid)
    return by_local_code.get((rec.get("code") or "").strip())


def build_plan(db, records, cols):
    by_code, by_symbol = tax_lookup(db)
    local = db.search("")
    by_id = {g["id"]: g for g in local}
    by_local_code = {}
    for g in local:
        by_local_code.setdefault((g["code"] or "").strip(), g)

    plan, seen_codes = [], {}
    for line, rec in records:
        is_new = find_existing(rec, by_id, by_local_code) is None
        good, errors, warnings = check_row(rec, by_code, by_symbol, cols, is_new)
        if good["code"]:
            if good["code"] in seen_codes:
                errors.append(f"код повторюється (уже в рядку {seen_codes[good['code']]})")
            else:
                seen_codes[good["code"]] = line

        existing = None
        if good["id"]:
            existing = by_id.get(good["id"])
            if not existing:
                errors.append("товару з таким id немає в базі — для нового товару залиште id порожнім")
        elif good["code"]:
            existing = by_local_code.get(good["code"])
        if existing and good["id"] is None:
            good["id"] = existing["id"]
        if existing and good["code"] and (existing["code"] or "").strip() != good["code"]:
            other = by_local_code.get(good["code"])
            if other and other["id"] != existing["id"]:
                errors.append(f"код {good['code']} уже має інший товар: «{other['name']}»")
            else:
                errors.append("змінити код наявного товару через імпорт Checkbox не можна — "
                              "Checkbox розпізнає товари за кодом")
        good["match_code"] = (existing["code"] or "").strip() if existing else None

        if errors:
            action, changes = "помилка", []
        elif not existing:
            action, changes = "новий", []
        else:
            changes = diff(existing, good)
            action = "зміна" if changes else "без змін"
        plan.append({"line": line, "action": action, "good": good, "changes": changes,
                     "errors": errors, "warnings": warnings})
    return plan


def write_report(plan, path):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["рядок", "дія", "код", "назва", "зміни", "помилки", "попередження"])
        for p in plan:
            g = p["good"]
            w.writerow([p["line"], p["action"], g.get("code"), g.get("name"),
                        "; ".join(p["changes"]), "; ".join(p["errors"]), "; ".join(p["warnings"])])


def print_plan(plan, info, report_path):
    counts = {}
    for p in plan:
        counts[p["action"]] = counts.get(p["action"], 0) + 1
    enc = {"utf-8-sig": "UTF-8", "cp1251": "Windows-1251"}.get(info["encoding"], info["encoding"])
    delim = {";": "крапка з комою", ",": "кома", "\t": "табуляція"}[info["delimiter"]]
    print(f"Файл: кодування {enc}, роздільник — {delim}, рядків з товарами: {len(plan)}")
    if info["unknown_columns"]:
        print(f"⚠ Невідомі колонки (пропущено): {', '.join(info['unknown_columns'])}")
    print(f"  нових товарів:  {counts.get('новий', 0)}")
    print(f"  змін:           {counts.get('зміна', 0)}")
    print(f"  без змін:       {counts.get('без змін', 0)}")
    print(f"  з помилками:    {counts.get('помилка', 0)}")
    shown = 0
    for p in plan:
        if p["action"] in ("помилка", "зміна", "новий") and shown < 30:
            g = p["good"]
            head = f"  рядок {p['line']:>4} · {p['action']:<8} · {g.get('code') or '—'} · {g.get('name') or '—'}"
            print(head)
            for e in p["errors"]:
                print(f"      ✖ {e}")
            for c in p["changes"]:
                print(f"      • {c}")
            shown += 1
    if shown >= 30:
        print("  … решта — у звіті")
    warn = sum(len(p["warnings"]) for p in plan)
    if warn:
        print(f"Попереджень: {warn} (див. звіт)")
    print(f"Звіт: {report_path}")
    print("Зміни НЕ застосовано: запис у Checkbox буде додано після отримання опису API товарів.")


# ───────────────────────── Файл номенклатури Checkbox ─────────────────────────
# Формат — за прикладом checkbox_goods.csv з документації: Windows-1251, кома, CRLF.
# Колонки (у такому порядку створюється новий файл):
CHECKBOX_HEADER = ["ім'я товару", "код", "група товарів", "штрихкод", "штрихкоди", "уктзед",
                   "ціна", "ваговий товар", "тип товару", "податкові ставки"]
CHECKBOX_FIELDS = {"ім'я товару": "name", "код": "code", "група товарів": "group", "штрихкод": "barcode",
                   "штрихкоди": "barcodes", "уктзед": "uktzed", "ціна": "price",
                   "ваговий товар": "is_weight", "тип товару": "type", "податкові ставки": "taxes"}


def _norm_header(h):
    return h.strip().lower().replace("\ufeff", "").replace("’", "'").replace("ʼ", "'").replace("`", "'")


class CheckboxFile:
    """
    Файл номенклатури у форматі Checkbox. Зміни вносяться лише в колонки, які знає каса;
    решта (група, додаткові штрихкоди тощо) лишаються як були.
    """

    def __init__(self, header, rows, encoding="cp1251", delimiter=","):
        self.header, self.rows, self.encoding, self.delimiter = header, rows, encoding, delimiter
        self.col = {}
        for i, h in enumerate(header):
            field = CHECKBOX_FIELDS.get(_norm_header(h))
            if field:
                self.col[field] = i
        missing = [h for h, f in CHECKBOX_FIELDS.items() if f in ("name", "code", "price") and f not in self.col]
        if missing:
            raise CheckboxError("Файл Checkbox має незнайомий формат: немає колонок "
                                f"{', '.join(missing)}. Знайдено: {', '.join(header)}")
        self.tax_sep = self._detect_tax_sep()

    @classmethod
    def from_bytes(cls, content):
        if content.startswith(b"\xef\xbb\xbf"):
            text, enc = content.decode("utf-8-sig"), "utf-8-sig"
        else:
            try:      # текст у Windows-1251 майже ніколи не є коректним UTF-8
                text, enc = content.decode("utf-8"), "utf-8"
            except UnicodeDecodeError:
                text, enc = content.decode("cp1251"), "cp1251"
        lines = text.splitlines()
        if not lines or not lines[0].strip():
            return cls.empty()
        delim = max(",;\t", key=lines[0].count)
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        return cls(rows[0], [r for r in rows[1:] if any(c.strip() for c in r)], enc, delim)

    @classmethod
    def empty(cls):
        return cls(list(CHECKBOX_HEADER), [])

    def _detect_tax_sep(self):
        """Як Checkbox розділяє кілька податків у клітинці: беремо з наявних рядків, інакше — кома."""
        i = self.col.get("taxes")
        if i is None:
            return ","
        for r in self.rows:
            cell = r[i].strip() if i < len(r) else ""
            for sep in (",", ";", " ", "/"):
                if sep in cell:
                    return sep
        return ","

    def _cell(self, row, field):
        i = self.col.get(field)
        return row[i] if i is not None and i < len(row) else ""

    def _set(self, row, field, value):
        i = self.col.get(field)
        if i is None:
            return False
        while len(row) <= i:
            row.append("")
        row[i] = value
        return True

    def find(self, code):
        code = (code or "").strip()
        for r in self.rows:
            if self._cell(r, "code").strip() == code:
                return r
        return None

    def apply(self, row, good, tax_symbols):
        """Записати в рядок поля товару, які є в good (решту не чіпати)."""
        self._set(row, "name", good["name"])
        self._set(row, "code", good["code"])
        if "price" in good:
            self._set(row, "price", f"{good['price'] / 100:.2f}")
        if "barcode" in good:
            self._set(row, "barcode", good["barcode"] or "")
        if "uktzed" in good:
            self._set(row, "uktzed", good["uktzed"] or "")
        if "is_weight" in good:
            self._set(row, "is_weight", "True" if good["is_weight"] else "False")
        if "tax_codes" in good:
            if good["tax_codes"] and "taxes" not in self.col:
                raise CheckboxError("У файлі Checkbox немає колонки податкових ставок")
            self._set(row, "taxes", self.tax_sep.join(tax_symbols[c] for c in good["tax_codes"]))
        if not self._cell(row, "type").strip():
            self._set(row, "type", "good")

    def add(self, good, tax_symbols):
        row = [""] * len(self.header)
        self.apply(row, good, tax_symbols)
        self.rows.append(row)
        return row

    def to_bytes(self):
        out = io.StringIO()
        w = csv.writer(out, delimiter=self.delimiter, lineterminator="\r\n")
        w.writerow(self.header)
        w.writerows(self.rows)
        text = out.getvalue()
        enc = "cp1251" if self.encoding == "cp1251" else self.encoding
        try:
            return text.encode(enc)
        except UnicodeEncodeError as e:
            bad = text[e.start:e.end]
            line = text[:e.start].count("\n") + 1
            raise CheckboxError(f"Символ «{bad}» (рядок {line}) не підтримується у файлі Checkbox "
                                "(кодування Windows-1251) — приберіть його з назви чи інших полів.")


def tax_symbols_map(db):
    return {t["code"]: (t["symbol"] or str(t["code"])) for t in db.taxes()}


def result_errors(result):
    """Помилки з відповіді перевірки Checkbox: будь-яке непорожнє поле зі словом error/помилк."""
    found = []
    if isinstance(result, dict):
        for k, v in result.items():
            if ("error" in k.lower() or "помилк" in k.lower()) and v not in (None, "", [], {}, 0, False):
                found.append(f"{k}: {json.dumps(v, ensure_ascii=False)[:500]}")
    return found


def save_goods_to_checkbox(api, db, goods, on_status=None, auto_supply=False, sync=True):
    """
    Додати/змінити товари в Checkbox через імпорт номенклатури.
    goods — список нормалізованих товарів (як із check_row) з ключем match_code:
    код наявного товару в Checkbox або None для нового.
    Кроки: свіже вивантаження → зміна рядків → завантаження → перевірка → застосування → синхронізація.
    """
    say = on_status or (lambda text: None)
    symbols = tax_symbols_map(db)

    say("Вивантажую номенклатуру з Checkbox…")
    task = api.goods_export_start("csv")
    api.wait_task(api.goods_export_status, task, ready={"done"})
    cbfile = CheckboxFile.from_bytes(api.goods_export_file(task))

    added = changed = 0
    for g in goods:
        unknown = [c for c in g.get("tax_codes", []) if c not in symbols]
        if unknown:
            raise CheckboxError(f"«{g['name']}»: невідомі податкові ставки {unknown}")
        if g.get("match_code") is None:
            if cbfile.find(g["code"]) is not None:
                raise CheckboxError(f"Товар з кодом {g['code']} уже є в Checkbox")
            cbfile.add(g, symbols)
            added += 1
        else:
            if g["code"] != g["match_code"].strip():
                raise CheckboxError(f"«{g['name']}»: змінити код наявного товару через імпорт Checkbox не можна "
                                    "(Checkbox розпізнає товари за кодом і створить новий)")
            row = cbfile.find(g["match_code"])
            if row is None:
                raise CheckboxError(f"Товару з кодом {g['match_code']} немає у вивантаженні Checkbox — "
                                    "синхронізуйте касу")
            cbfile.apply(row, g, symbols)
            changed += 1

    say("Завантажую зміни в Checkbox…")
    content = cbfile.to_bytes()
    task = api.goods_import_upload("kasa_goods.csv", content, True, auto_supply)
    log.info("Збереження товарів: нових %d, змінених %d, завдання %s", added, changed, task)
    say("Checkbox перевіряє файл…")
    result = api.wait_task(api.goods_import_status, task, ready={"completed", "done"})
    log.info("Збереження товарів: перевірка %s", json.dumps(result, ensure_ascii=False)[:4000])
    errors = result_errors(result)
    if errors:
        raise CheckboxError("Checkbox знайшов помилки у файлі: " + "; ".join(errors))
    if str(result.get("status", "")).lower() != "done":
        say("Застосовую зміни…")
        apply_task = api.goods_import_apply(task)
        api.wait_task(api.goods_import_status, apply_task, ready={"done"})

    log.info("Збереження товарів завершено: нових %d, змінених %d", added, changed)
    out = {"added": added, "changed": changed, "result": result}
    if sync:
        say("Оновлюю локальну базу…")
        synced, _, _, _ = sync_all(api, db)
        out["synced"] = len(synced)
    return out


# Колонка кількості у файлі імпорту для автоматичної поставки (auto_supply).
# Якщо у вивантаженні Checkbox уже є така колонка — беремо її назву, інакше додаємо «кількість».
STOCK_HEADER = "кількість"
STOCK_HEADERS = ("кількість", "к-сть", "залишок", "залишки", "кількість товару", "count", "quantity", "amount")


def supply_stock(api, stock, on_status=None):
    """
    Довести залишки товарів у Checkbox до бажаних значень поставкою через імпорт.
    stock — {код: (бажаний залишок у тисячних, ваговий)}. Поставка лише додає, тож у файл іде різниця
    між бажаним і поточним залишком Checkbox. Файл містить лише ці товари (решта номенклатури не чіпається).
    Повертає список описів товарів, для яких після імпорту залишок не збігся (порожній — усе вдалося).
    """
    say = on_status or (lambda text: None)
    current = {str(g["code"]).strip(): g.get("count") for g in api.get_all_goods()[0]}
    todo = {}
    for code, (target, is_weight) in stock.items():
        delta = target - (current.get(code) or 0)
        if delta > 0:
            todo[code] = (delta, is_weight, target)
    if not todo:
        return []
    say("Вивантажую номенклатуру з Checkbox…")
    task = api.goods_export_start("csv")
    api.wait_task(api.goods_export_status, task, ready={"done"})
    src = CheckboxFile.from_bytes(api.goods_export_file(task))
    header = list(src.header)
    qcol = next((i for i, h in enumerate(header) if _norm_header(h) in STOCK_HEADERS), None)
    if qcol is None:
        header.append(STOCK_HEADER)
        qcol = len(header) - 1
    rows, missing = [], []
    for code, (delta, is_weight, _) in todo.items():
        row = src.find(code)
        if row is None:
            missing.append(code)
            continue
        row = list(row) + [""] * (len(header) - len(row))
        row[qcol] = fmt_count(delta, is_weight)
        rows.append(row)
    if missing:
        raise CheckboxError(f"товарів з кодами {', '.join(missing)} немає у вивантаженні Checkbox")
    out = CheckboxFile(header, rows, src.encoding, src.delimiter)
    say("Вношу залишки поставкою…")
    task = api.goods_import_upload("kasa_stock.csv", out.to_bytes(), True, True)
    log.info("Поставка залишків через імпорт (колонка «%s»): %s, завдання %s", header[qcol],
             {c: fmt_count(d, w) for c, (d, w, _) in todo.items()}, task)
    result = api.wait_task(api.goods_import_status, task, ready={"completed", "done"})
    log.info("Поставка залишків: перевірка %s", json.dumps(result, ensure_ascii=False)[:4000])
    errors = result_errors(result)
    if errors:
        raise CheckboxError("Checkbox знайшов помилки у файлі: " + "; ".join(errors))
    if str(result.get("status", "")).lower() != "done":
        apply_task = api.goods_import_apply(task)
        api.wait_task(api.goods_import_status, apply_task, ready={"done"})
    # звірка: чи справді залишки стали такими, як треба
    after = {str(g["code"]).strip(): g.get("count") for g in api.get_all_goods()[0]}
    failed = []
    for code, (delta, is_weight, target) in todo.items():
        got = after.get(code)
        if got != target:
            failed.append(f"{code}: очікувався залишок {fmt_count(target, is_weight)}, у Checkbox "
                          f"{'—' if got is None else fmt_count(got, is_weight)}")
    log.log(30 if failed else 20, "Поставка залишків: %s", "; ".join(failed) or "усі залишки збіглися")
    return failed


# ───────────────────────── Checkbox: pull / push ─────────────────────────

def connect():
    cfg = load_config()
    if not cfg:
        raise SystemExit("Немає config.json — спершу налаштуйте касу (кнопка «Налаштування» на сторінці).")
    api = CheckboxAPI(cfg)
    try:
        api.signin()
    except (CheckboxError, net.RequestException) as e:
        raise SystemExit(f"✖ Не вдалося увійти в Checkbox: {e}")
    return api


def show_status(status, data):
    print(f"   статус: {status or '—'}")


def short(obj, limit=1500):
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    return text if len(text) <= limit else text[:limit] + "\n   … (повністю — у журналі logs/kasa.log)"


def pull(path):
    api = connect()
    print("Створюю завдання на експорт у Checkbox…")
    task = api.goods_export_start("csv")
    api.wait_task(api.goods_export_status, task, ready={"done"}, on_status=show_status)
    content = api.goods_export_file(task)
    path = Path(path or f"checkbox_товари_{datetime.now():%Y-%m-%d_%H%M}.csv")
    path.write_bytes(content)
    try:
        cb = CheckboxFile.from_bytes(content)
        enc = {"cp1251": "Windows-1251", "utf-8": "UTF-8", "utf-8-sig": "UTF-8"}.get(cb.encoding, cb.encoding)
        print(f"✔ Збережено: {path.resolve()} (товарів: {len(cb.rows)}, кодування {enc})")
        print(f"   колонки: {', '.join(cb.header)}")
    except CheckboxError as e:
        print(f"✔ Збережено: {path.resolve()} ({len(content)} байт) — ⚠ {e}")
    log.info("Експорт номенклатури з Checkbox: завдання %s → %s", task, path)
    return 0


def looks_like_kasa_format(content):
    try:
        first = content.decode("utf-8-sig").splitlines()[0]
    except (UnicodeDecodeError, IndexError):
        return False
    cols = [c.strip().strip('"').lower() for c in re.split(r"[;,\t]", first)]
    return cols[:len(COLUMNS)] == COLUMNS


def _has_kasa_headers(content):
    """Файл з українськими заголовками каси (Назва/Код/Ціна…), а не формату Checkbox."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1251", errors="replace")
    first = [_norm_header(h) for h in re.split(r"[;,\t]", text.splitlines()[0] if text else "")]
    return "ім'я товару" not in first and ("назва" in first or "name" in first)


def push_kasa_format(path, yes, auto_supply):
    """Файл у форматі каси: перевірити, показати план і зберегти нові/змінені товари в Checkbox."""
    db = LocalDB()
    records, info = read_csv(path)
    plan = build_plan(db, records, info["columns"])
    report = path.with_name(path.stem + "_перевірка.csv")
    write_report(plan, report)
    counts = {a: sum(1 for p in plan if p["action"] == a) for a in ("новий", "зміна", "без змін", "помилка")}
    print(f"Файл у форматі каси: нових {counts['новий']}, змін {counts['зміна']}, "
          f"без змін {counts['без змін']}, з помилками {counts['помилка']} (звіт: {report})")
    if counts["помилка"]:
        print("✖ Спершу виправте помилки (див. «python goods_csv.py check» або звіт) — у Checkbox нічого не відправлено.")
        return 1
    goods = [p["good"] for p in plan if p["action"] in ("новий", "зміна")]
    if not goods:
        print("Змін немає — відправляти нічого.")
        return 0
    if not yes and input(f"Зберегти в Checkbox {len(goods)} товар(и)? Введіть «так»: ").strip().lower() not in ("так", "y", "yes", "т"):
        print("Скасовано — у Checkbox нічого не змінено.")
        return 1
    r = save_goods_to_checkbox(connect(), db, goods, on_status=print, auto_supply=auto_supply)
    print(f"✔ Збережено в Checkbox: нових {r['added']}, змінених {r['changed']}. Товарів у касі: {r['synced']}")
    return 0


def push(path, yes=False, auto_supply=False, ignore_barcode_duplicates=True):
    path = Path(path)
    if not path.exists():
        print(f"Файл не знайдено: {path}")
        return 1
    if path.suffix.lower() not in (".csv", ".xlsx", ".json"):
        print("Checkbox приймає файли .csv, .xlsx або .json")
        return 1
    content = path.read_bytes()
    if looks_like_kasa_format(content) or _has_kasa_headers(content):
        return push_kasa_format(path, yes, auto_supply)

    api = connect()
    print(f"Завантажую {path.name} у Checkbox…")
    task = api.goods_import_upload(path.name, content, ignore_barcode_duplicates, auto_supply)
    log.info("Імпорт номенклатури: файл %s, завдання %s", path.name, task)
    print("Checkbox перевіряє файл…")
    result = api.wait_task(api.goods_import_status, task, ready={"completed", "done"}, on_status=show_status)
    log.info("Імпорт номенклатури: результат перевірки %s", json.dumps(result, ensure_ascii=False)[:4000])
    print("Результат перевірки від Checkbox:")
    print(short(result))

    if str(result.get("status", "")).lower() == "done":
        print("✔ Checkbox уже застосував зміни.")
    else:
        if not yes:
            answer = input("Застосувати ці зміни в Checkbox? Введіть «так»: ").strip().lower()
            if answer not in ("так", "y", "yes", "т"):
                print("Скасовано — у Checkbox нічого не змінено.")
                return 1
        apply_task = api.goods_import_apply(task)
        print("Застосовую зміни…")
        done = api.wait_task(api.goods_import_status, apply_task, ready={"done"}, on_status=show_status)
        log.info("Імпорт номенклатури застосовано: завдання %s, %s", apply_task, json.dumps(done, ensure_ascii=False)[:2000])
        print("✔ Зміни в Checkbox застосовано.")

    print("Оновлюю локальну базу каси…")
    goods, _, taxes, _ = sync_all(api, LocalDB())
    print(f"✔ Синхронізовано товарів: {len(goods)}")
    return 0


# ───────────────────────── Запуск ─────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="Експорт / перевірка імпорту товарів каси у CSV")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="вивантажити товари в CSV")
    e.add_argument("file", nargs="?", help="куди зберегти (за замовчуванням товари_РРРР-ММ-ДД.csv)")
    c = sub.add_parser("check", help="розібрати CSV і показати план змін")
    c.add_argument("file")
    pl = sub.add_parser("pull", help="вивантажити номенклатуру з Checkbox")
    pl.add_argument("file", nargs="?")
    ps = sub.add_parser("push", help="завантажити файл у Checkbox і застосувати зміни")
    ps.add_argument("file")
    ps.add_argument("--yes", action="store_true", help="застосувати без запитання")
    ps.add_argument("--auto-supply", action="store_true", help="створити поставки для нових товарів")
    ps.add_argument("--barcode-duplicates", action="store_true", help="не ігнорувати дублікати штрихкодів")
    args = ap.parse_args(argv)
    try:   # щоб кирилиця й позначки не ламали вивід у старих консолях Windows
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass

    if args.cmd in ("pull", "push"):
        setup_logging()
        try:
            if args.cmd == "pull":
                return pull(args.file)
            return push(args.file, args.yes, args.auto_supply, not args.barcode_duplicates)
        except CheckboxError as e:
            log.warning("%s: %s", args.cmd, e)
            print(f"✖ Checkbox: {e}")
            return 1
        except net.RequestException as e:
            print(f"✖ Немає зв'язку з Checkbox: {e}")
            return 1

    db = LocalDB()
    if args.cmd == "export":
        path = Path(args.file or f"товари_{datetime.now():%Y-%m-%d}.csv")
        n = export_goods(db, path)
        if not n:
            print("⚠ Локальна база товарів порожня — спершу синхронізуйте касу.")
        print(f"✔ Вивантажено товарів: {n} → {path.resolve()}")
        return 0

    path = Path(args.file)
    if not path.exists():
        print(f"Файл не знайдено: {path}")
        return 1
    try:
        records, info = read_csv(path)
    except ValueError as e:
        print(f"✖ {e}")
        return 1
    plan = build_plan(db, records, info["columns"])
    report = path.with_name(path.stem + "_перевірка.csv")
    write_report(plan, report)
    print_plan(plan, info, report)
    return 1 if any(p["action"] == "помилка" for p in plan) else 0


if __name__ == "__main__":
    sys.exit(main())
