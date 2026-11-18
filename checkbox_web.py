#!/usr/bin/env python3
"""
Веб-інтерфейс для простої облікової системи Checkbox.

Запуск:  python checkbox_web.py
Відкриється браузер на http://localhost:8000

Потрібні поруч: checkbox_app.py і config.json.
Залежності: лише стандартна бібліотека Python 3.9+.
"""

import base64
import hashlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


import goods_csv
import kasa_update
from checkbox_app import (APP_VERSION, AUTH_MODES, DATA_DIR, LOG_PATH, log, setup_logging, net, CONFIG_PATH, DEFAULT_CLIENT_NAME, DEFAULT_CLIENT_VERSION,
                          ROUNDING_MODES, CheckboxAPI,
                          CheckboxError, LocalDB, auth_mode, load_config, save_config,
                          build_goods_payload, clean_payment_label, good_tax_codes, new_receipt_id,
                          no_tax_goods, round_cash, sync_all, apply_discounts, return_lines,
                          return_good_payload, local_time, line_sum, clean_phone, mask_phone, redact, log_json,
                          good_put_body)

# Порти, які пробує каса: спершу той, що був минулого разу, далі по черзі від 8000
PORT_RANGE = range(8000, 8020)
PORT_FILE = DATA_DIR / "port"          # останній порт: щоб адреса (і дані браузера) не змінювались без потреби
PORT = None                            # фактичний порт після запуску
APP_ID = "checkbox-kasa"
JOURNAL_DAYS = 7   # скільки днів чеків тримати в касі (записи журналу і PNG)
# Сервер багатопотоковий: сторінка віддається одразу, навіть коли інший запит чекає на Checkbox.
# Усі звернення до каси й бази виконуються по черзі під цим замком.
APP_LOCK = threading.Lock()   # відповідь /api/ping: так повторний запуск упізнає вже запущену касу
SERVER = None
RESTART_AFTER_STOP = False   # True — після оновлення запустити нову копію каси


class UserError(Exception):
    """Помилка, яку треба просто показати користувачу. code — машинна позначка для сторінки."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


# Поля ЕПЗ, які дозволено передавати в платіж CASHLESS (за документацією Checkbox)
CARD_TEXT_FIELDS = ("card_mask", "rrn", "auth_code", "receipt_no", "payment_system",
                    "bank_name", "owner_name", "terminal", "acquirer_and_seller")


def clean_card_details(raw):
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise UserError("Невірні дані ЕПЗ")
    out = {}
    for key in CARD_TEXT_FIELDS:
        val = str(raw.get(key) or "").strip()
        if val:
            out[key] = val
    if len(out.get("card_mask", "")) > 19:
        raise UserError("Маска картки — не більше 19 символів")
    if raw.get("signature_required"):
        out["signature_required"] = True
    if raw.get("provider_type") in ("TAPXPHONE", "POSCONTROL", "TERMINAL"):   # значення з документації Checkbox
        out["provider_type"] = raw["provider_type"]
    return out or None


# ───────────────────────── Платіжний термінал (POSSrv) ─────────────────────────

POSSRV_DEFAULT_URL = "http://localhost:9020"


def terminal_for_log(raw):
    """Відповідь терміналу для журналу: без HTML-сліпа банку (він великий і вже є в базі)."""
    def clean(obj):
        if isinstance(obj, dict):
            return {k: (f"<{len(v)} символів>" if k == "receipt" and isinstance(v, str) and len(v) > 200 else clean(v))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [clean(x) for x in obj]
        return obj
    return json.dumps(clean(raw), ensure_ascii=False)[:4000]
TERMINAL_BANKS = {"oschadbank": "Ощадбанк", "privatbank": "ПриватБанк"}


def clean_possrv_url(url):
    url = str(url or "").strip().rstrip("/") or POSSRV_DEFAULT_URL
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UserError("Адреса POSSrv має виглядати так: http://localhost:9020")
    return f"{parts.scheme}://{parts.netloc}"


class PosSrv:
    """HTTP-методи POSSrv (документація «REST API для роботи з POS терміналами»)."""

    def __init__(self, url):
        self.url = clean_possrv_url(url)
        self.s = net.Session()

    def _req(self, method, path, body=None, timeout=10):
        tail = f" | запит: {log_json(redact(body))}" if body is not None else ""
        info = {"method": method, "url": f"{self.url}/{path}", "request": redact(body)}
        try:
            r = self.s.request(method, f"{self.url}/{path}", json=body, timeout=timeout)
        except net.RequestException as e:
            log.warning("POSSrv %s %s — немає зв'язку%s", method, path, tail, extra={"data": dict(info, error=str(e))})
            raise UserError(f"POSSrv не відповідає за адресою {self.url}. Перевірте, що POSSrv запущено.")
        log.info("POSSrv %s %s → %s%s", method, path, r.status_code, tail,
                 extra={"data": dict(info, status=r.status_code, response=(r.text or "")[:4000])})
        if r.status_code >= 400:
            raise UserError(f"POSSrv: HTTP {r.status_code} {r.text[:300]}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    def devices(self):
        data = self._req("GET", "api/devices")
        return data if isinstance(data, list) else []

    def register(self, body):
        device = self._req("POST", "api/devices/register", body)
        self._req("POST", "api/pos/saveconfig")   # без цього термінал зникне після перезапуску POSSrv
        return device


def terminal_register_body(raw):
    """Тіло api/devices/register з даних форми; помилки — зрозумілою мовою."""
    name = str(raw.get("name") or "").strip()
    bank = raw.get("type")
    mode = raw.get("mode")
    if not name:
        raise UserError("Вкажіть назву терміналу")
    if bank not in TERMINAL_BANKS:
        raise UserError("Оберіть банк терміналу")
    if mode not in ("rs232", "tcp"):
        raise UserError("Оберіть тип підключення")

    def as_int(key, label, lo, hi):
        try:
            v = int(str(raw.get(key)).strip())
        except (TypeError, ValueError):
            raise UserError(f"{label}: потрібне ціле число")
        if not lo <= v <= hi:
            raise UserError(f"{label}: від {lo} до {hi}")
        return v

    props = {"connectType": mode, "mode": mode}
    if mode == "rs232":
        props["port"] = as_int("port", "Номер COM-порту", 1, 256)
        props["baudrate"] = as_int("baudrate", "Швидкість COM", 1200, 921600) if raw.get("baudrate") else 115200
    else:
        host = str(raw.get("host") or "").strip()
        if not host:
            raise UserError("Вкажіть IP-адресу терміналу")
        props["host"] = host
        props["port"] = as_int("port", "TCP-порт", 1, 65535)
    merchant = str(raw.get("merchant_id") or "").strip()
    if merchant:
        props["merchant_id"] = merchant
    return {"name": name, "type": bank, "properties": props}


class App:
    def __init__(self, api, db, auth_ok=True, auth_error=None, api_factory=CheckboxAPI):
        self.api = api
        self.db = db
        self.auth_ok = auth_ok
        self.auth_error = auth_error
        self.api_factory = api_factory
        self.cashier = None
        self.connecting = False     # True, поки при старті йде вхід і синхронізація
        self._shift = {"at": 0.0, "data": None}   # кеш стану зміни (див. shift_state)

    def call(self, fn, *args):
        """Виклик API з повторною авторизацією, якщо токен застарів."""
        if not self.auth_ok:
            raise UserError("Каса не підключена — відкрийте «Налаштування»")
        try:
            return fn(*args)
        except CheckboxError as e:
            if str(e).startswith("HTTP 401"):
                log.info("Токен касира застарів — авторизуюся знову")
                self.api.signin()
                return fn(*args)
            self._shift["at"] = 0.0          # помилка Checkbox (напр. зміну закрито деінде) — стан зміни перечитаємо
            raise

    # ---- каса (пРРО): фіскальний номер — не змінюється, тож запитуємо раз на ключ ліцензії ----
    def register_info(self, refresh=False):
        lic = (self.api.cfg.get("license_key") or "")
        tag = hashlib.sha256(lic.encode("utf-8")).hexdigest()[:16]
        try:
            saved = json.loads(self.db.meta("cash_register") or "null")
        except ValueError:
            saved = None
        if saved and saved.get("tag") == tag and not refresh:
            return saved
        if not self.auth_ok and not refresh:
            return saved if saved and saved.get("tag") == tag else None
        try:
            info = self.api.cash_register_info()
        except (CheckboxError, net.RequestException) as e:
            log.info("Дані каси (GET /cash-registers/info) недоступні: %s", e)
            return saved if saved and saved.get("tag") == tag else None
        data = {"tag": tag, "fiscal_number": info.get("fiscal_number"), "number": info.get("number"),
                "title": info.get("title"), "address": info.get("address")}
        with self.db.con:
            self.db.con.execute("INSERT OR REPLACE INTO meta VALUES ('cash_register', ?)",
                                (json.dumps(data, ensure_ascii=False),))
        log.info("Каса: ФН %s%s", data["fiscal_number"], f" ({data['title']})" if data.get("title") else "")
        return data

    # ---- стан зміни: кешуємо, щоб не питати GET /cashier/shift перед кожною дією ----
    SHIFT_TTL = 600                       # с; відкриття/закриття з цієї каси оновлюють кеш одразу

    def shift_state(self, force=False):
        c = self._shift
        if force or time.time() - c["at"] > self.SHIFT_TTL:
            c["data"], c["at"] = self.call(self.api.current_shift), time.time()
        return c["data"]

    def _set_shift(self, shift):
        self._shift.update(data=shift, at=time.time())

    # ---- налаштування ----
    def config(self):
        cfg = self.api.cfg
        return {
            "auth_mode": auth_mode(cfg),
            "license_key": cfg.get("license_key") or "",
            "login": cfg.get("login") or "",
            "has_password": bool(cfg.get("password")),
            "has_pin": bool(cfg.get("pin_code")),
            # новий конфіг — 50 копійок за замовчуванням; явне null — без заокруглення
            "cash_rounding": (cfg.get("cash_rounding") or "") if "cash_rounding" in cfg else "ROUND_50",
            "auth_ok": self.auth_ok,
            "connecting": self.connecting,
            "auth_error": self.auth_error,
            "cashier": self.cashier,
            "register": self.register_info() if self.auth_ok else None,
            "config_exists": CONFIG_PATH.exists(),
            "terminal": self.terminal_settings(),
            "log_path": str(LOG_PATH),
            "version": APP_VERSION,
        }

    # ---- платіжний термінал ----
    def terminal_settings(self):
        t = dict(self.api.cfg.get("terminal") or {})
        t.setdefault("enabled", False)
        t.setdefault("url", POSSRV_DEFAULT_URL)
        t["bank_name"] = TERMINAL_BANKS.get(t.get("device_type"), "")
        return t

    def terminal_devices(self, body):
        return {"devices": PosSrv(body.get("url")).devices()}

    def terminal_register(self, body):
        possrv = PosSrv(body.get("url"))
        device = possrv.register(terminal_register_body(body))
        return {"device": device, "devices": possrv.devices()}

    def terminal_save(self, body):
        enabled = bool(body.get("enabled"))
        url = clean_possrv_url(body.get("url"))
        device_id = str(body.get("device_id") or "").strip()
        if enabled and not device_id:
            raise UserError("Оберіть термінал зі списку")
        t = {"enabled": enabled, "url": url, "device_id": device_id,
             "device_name": str(body.get("device_name") or "").strip(),
             "device_type": body.get("device_type") if body.get("device_type") in TERMINAL_BANKS else ""}
        cfg = dict(self.api.cfg)
        cfg["terminal"] = t
        save_config(cfg)              # вхід у Checkbox для цього не потрібен
        log.info("Термінал: %s, POSSrv %s, пристрій %s", "увімкнено" if enabled else "вимкнено", url, device_id or "—")
        self.api.cfg["terminal"] = t
        return {"terminal": self.terminal_settings()}

    def save_config(self, body):
        old = dict(self.api.cfg)
        new = dict(old)  # base_url, client_name, client_version — лише в config.json, не змінюються

        mode = body.get("auth_mode")
        if mode not in AUTH_MODES:
            raise UserError("Оберіть спосіб авторизації")
        new["auth_mode"] = mode
        for key in ("license_key", "login"):
            if key in body:
                new[key] = str(body.get(key) or "").strip()
        for key in ("password", "pin_code"):
            value = str(body.get(key) or "")
            if value:                       # порожнє поле = залишити збережене значення
                new[key] = value
        rounding = body.get("cash_rounding") or None
        if rounding not in (None,) + ROUNDING_MODES:
            raise UserError("Невірний режим заокруглення")
        new["cash_rounding"] = rounding
        # у вікні їх немає: вшиті значення дописуються в config.json, щоб було видно, де їх змінити

        # Спершу перевіряємо, що з новими даними можна увійти, і лише потім зберігаємо
        api = self.api_factory(new)
        try:
            api.signin()
        except CheckboxError as e:
            raise UserError(f"Не вдалося авторизуватися: {e}. Налаштування не збережено.")
        except net.RequestException as e:
            raise UserError(f"Немає зв'язку з Checkbox: {e}. Налаштування не збережено.")

        save_config(new)
        log.info("Налаштування збережено: авторизація %s, ключ ліцензії …%s", mode, (new.get("license_key") or "")[-4:])
        self.api, self.auth_ok, self.auth_error = api, True, None
        self.cashier = api.cashier_name()
        self.register_info(refresh=True)
        result = {"saved": True, "cashier": self.cashier}

        # Інша каса — інша база товарів: синхронізуємо одразу
        if old.get("license_key") != new.get("license_key") or not self.db.meta("synced_at"):
            try:
                result["synced"] = self.sync()["count"]
            except (CheckboxError, net.RequestException, UserError) as e:
                result["sync_error"] = str(e)
        return result

    # ---- товари: додавання й редагування (через імпорт номенклатури Checkbox) ----
    def next_code(self):
        """Наступний вільний код товару: найбільший «короткий» числовий код + 1 (довгі коди-штрихкоди
        й мітки часу на кшталт 1794467826139 не враховуємо, щоб нумерація лишалась зручною)."""
        codes = {(r[0] or "").strip() for r in self.db.con.execute("SELECT code FROM goods")}
        nums = [int(c) for c in codes if c.isdigit() and len(c) <= 7]
        n = max(nums, default=0) + 1
        while str(n) in codes:
            n += 1
        return {"code": str(n)}

    def save_good(self, body):
        good_id = body.get("id")
        existing = None
        if good_id:
            existing = self.db.con.execute("SELECT * FROM goods WHERE id=?", (str(good_id),)).fetchone()
            if not existing:
                raise UserError("Товар не знайдено в локальній базі — оновіть сторінку")
        pending_new = existing is not None and existing["pending"] == "new"
        code_in = str(body.get("code") or "")
        if existing is None and not code_in.strip():       # новий товар без коду — код генеруємо
            code_in = self.next_code()["code"]
        rec = {
            "code": code_in, "name": str(body.get("name") or ""),
            "price": str(body.get("price") or ""), "barcode": str(body.get("barcode") or ""),
            "uktzed": str(body.get("uktzed") or ""),
            "is_weight": "1" if body.get("is_weight") else "0",
            "tax_codes": ",".join(str(int(c)) for c in (body.get("tax_codes") or [])),
        }
        by_code, by_symbol = goods_csv.tax_lookup(self.db)
        cols = {"code", "name", "price", "barcode", "uktzed", "is_weight", "tax_codes"}
        good, errors, _ = goods_csv.check_row(rec, by_code, by_symbol, cols, existing is None)
        code_changed = existing is not None and good["code"] and good["code"] != (existing["code"] or "").strip()
        if code_changed and not pending_new:
            errors.append("код наявного товару змінити не можна — Checkbox розпізнає товари за кодом")
        if good["code"] and (existing is None or code_changed):
            dup = self.db.con.execute("SELECT * FROM goods WHERE TRIM(code)=?", (good["code"],)).fetchone()
            if dup:
                errors.append(f"код {good['code']} уже має товар «{dup['name']}»")
        # залишок: бажане значення в тисячних; у Checkbox іде поставкою, тож лише збільшення.
        # stock = None — залишок у Checkbox не змінювати.
        stock = body.get("stock")
        cb_count = self.db.checkbox_count(existing) if existing is not None else None
        if stock is not None:
            if not isinstance(stock, int) or isinstance(stock, bool) or stock < 0 or stock > 10 ** 12:
                errors.append("невірний залишок")
            elif not good.get("is_weight") and stock % 1000:
                errors.append("залишок штучного товару — ціле число")
            elif cb_count is not None and stock < cb_count:
                errors.append(f"зменшити залишок (у Checkbox {cb_count / 1000:g}) можна лише списанням у кабінеті Checkbox")
            elif stock == (cb_count or 0):
                stock = None
        if "group_id" in body:                        # група товару: id групи або None — без групи
            gid = body.get("group_id") or None
            if gid and not self.db.con.execute("SELECT 1 FROM goods_groups WHERE id=?", (gid,)).fetchone():
                errors.append("групу не знайдено — синхронізуйте товари")
            good["group_id"] = gid
        if errors:
            raise UserError("; ".join(errors))
        good["stock"] = stock
        prev = next((p for p in self.db.pending()
                     if existing is not None and p["code"] == (existing["code"] or "").strip()), None)
        prev_stock = prev["data"].get("stock") if prev else None
        if existing:
            changes = goods_csv.diff(existing, good)
            if stock != prev_stock:
                changes.append(f"залишок → {stock / 1000:g}" if stock is not None else "залишок — як у Checkbox")
            if "group_id" in good and good["group_id"] != existing["group_id"]:
                changes.append("група → " + (good["group_id"] or "без групи"))
            if not changes:
                return {"unchanged": True, "pending": self.db.pending_count()}
            log.info("Локальна правка товару %s: %s", good["code"], "; ".join(changes))
        else:
            log.info("Новий товар (локально) %s «%s»", good["code"], good["name"])
        # лише локально: у Checkbox піде під час синхронізації (кнопка, відкриття/закриття зміни)
        self.db.put_pending(good, existing)
        if existing is not None and stock is None and prev_stock is not None:
            with self.db.con:                        # правку залишку знято — показуємо залишок Checkbox
                self.db.con.execute("UPDATE goods SET count=? WHERE TRIM(code)=?", (cb_count, good["code"]))
        return {"saved": True, "pending": self.db.pending_count()}

    def discard_good(self, body):
        code = str(body.get("code") or "").strip()
        if not self.db.discard_pending(code):
            raise UserError("Для цього товару немає невідправлених змін")
        log.info("Скасовано локальну правку товару %s", code)
        return {"pending": self.db.pending_count()}

    def push_pending(self):
        """
        Відправити локальні правки в Checkbox: змінені товари — PUT /goods/{id} (поточний стан товару
        з Checkbox + правки), нові — POST /goods; якщо прямий метод недоступний — одним імпортом файлу.
        Залишки — окремо, поставкою (auto_supply). Помилка одного товару не зупиняє решту.
        Повертає кількість відправлених правок.
        """
        pending = self.db.pending()
        if not pending:
            return 0
        done, failed, via_import = [], {}, []
        for p in pending:
            data = p["data"]
            try:
                if p["action"] == "new":
                    try:
                        self.call(self.api.create_good, good_put_body(None, data))
                    except CheckboxError as e:
                        if not re.search(r"HTTP (404|405)\b", str(e)):
                            raise
                        via_import.append(p)          # POST /goods недоступний — імпортом
                        continue
                else:
                    row = self.db.con.execute("SELECT id FROM goods WHERE TRIM(code)=? AND id NOT LIKE 'local-%'",
                                              (p["code"],)).fetchone()
                    if not row:
                        raise CheckboxError("товару немає в Checkbox — синхронізуйте товари")
                    current = self.call(self.api.get_good, row["id"])
                    self.call(self.api.update_good, row["id"], good_put_body(current, data))
                done.append(p)
                log.info("Товар %s збережено в Checkbox (%s)", p["code"], "новий" if p["action"] == "new" else "зміни")
            except (CheckboxError, net.RequestException) as e:
                failed[p["code"]] = str(e)
                self.db.set_pending_error(str(e), p["code"])
                log.warning("Товар %s не збережено в Checkbox: %s", p["code"], e)
        saved = {}
        if via_import:
            goods = [dict(p["data"], match_code=None) for p in via_import]
            try:
                saved = self.call(goods_csv.save_goods_to_checkbox, self.api, self.db, goods, None, False, False)
                done.extend(via_import)
                grouped = {p["code"]: p["data"]["group_id"] for p in via_import if p["data"].get("group_id")}
                if grouped:                           # файл імпорту групу не передає — ставимо її через PUT
                    ids = {str(g["code"]).strip(): g["id"] for g in self.call(self.api.get_all_goods)[0]}
                    for code, gid in grouped.items():
                        try:
                            if ids.get(code):
                                current = self.call(self.api.get_good, ids[code])
                                self.call(self.api.update_good, ids[code], good_put_body(current, {"group_id": gid}))
                        except (CheckboxError, net.RequestException) as e:
                            log.warning("Групу товару %s не встановлено: %s", code, e)
            except (CheckboxError, net.RequestException) as e:
                for p in via_import:
                    failed[p["code"]] = str(e)
                    self.db.set_pending_error(str(e), p["code"])
                log.warning("Нові товари (%d) не збережено імпортом: %s", len(via_import), e)
        self.db.clear_pending([p["code"] for p in done])
        pending = done
        stock = {p["data"]["code"]: (p["data"]["stock"], bool(p["data"].get("is_weight")))
                 for p in pending if p["data"].get("stock") is not None}
        if stock:
            # правки товарів уже в Checkbox; поставку не повторюємо автоматично — лише повідомляємо
            try:
                failed_stock = self.call(goods_csv.supply_stock, self.api, stock, None, saved.get("file"))
            except (CheckboxError, net.RequestException) as e:
                raise CheckboxError(f"товари збережено, але залишки не внесено: {e}")
            if failed_stock:
                raise CheckboxError("товари збережено, але Checkbox не прийняв залишки: " + "; ".join(failed_stock))
        if failed:
            raise CheckboxError(f"не збережено товарів: {len(failed)} — "
                                + "; ".join(f"{c}: {e}" for c, e in list(failed.items())[:3]))
        return len(pending)

    def full_sync(self, groups=True):
        """Спершу відправити локальні правки, потім оновити товари з Checkbox (правки, що не пішли, лишаються).
        groups=False — автоматична синхронізація: групи лише якщо з'явилась невідома."""
        pushed, push_error = 0, None
        try:
            pushed = self.push_pending()
        except (CheckboxError, net.RequestException) as e:
            push_error = str(e)
        r = self.sync(groups)
        r.update(pushed=pushed, push_error=push_error, pending=self.db.pending_count())
        return r

    # ---- товари ----
    def goods(self):
        rows = self.db.search("")
        errors = {p["code"]: p["error"] for p in self.db.pending() if p["error"]}
        goods = []
        for r in rows:
            g = dict(r)
            g["tax_codes"] = good_tax_codes(r)
            if g.get("pending"):
                g["pending_error"] = errors.get((g["code"] or "").strip())
                g["cb_count"] = self.db.checkbox_count(r)
            goods.append(g)
        groups = self.db.groups()
        paths = {g["id"]: g["path"] for g in groups}
        for g in goods:
            g["group_path"] = paths.get(g.get("group_id")) or []
        return {
            "goods": goods,
            "groups": groups,
            "taxes": [dict(t) for t in self.db.taxes()],
            "taxes_source": self.db.meta("taxes_source"),
            "use_leftovers": self.db.meta("use_leftovers"),
            "synced_at": self.db.meta("synced_at"),
            "cash_rounding": self.rounding_mode(),
            "pending": self.db.pending_count(),
            "images": images_index(),
            "excise_total": self.db.con.execute("SELECT COUNT(*) FROM excise_marks").fetchone()[0],
        }

    def rounding_mode(self):
        return self.api.cfg.get("cash_rounding") or None

    def refresh_taxes(self):
        """Кнопка «Оновити ставки»: довідник податкових ставок з Checkbox (GET /tax)."""
        taxes, source = self.call(self.api.get_taxes, [])
        if not taxes:
            raise UserError("Checkbox не повернув податкових ставок")
        known = {t["code"]: dict(t) for t in self.db.taxes()}
        merged = {**{c: t for c, t in known.items()}, **{t["code"]: t for t in taxes}}
        self.db.replace_taxes(sorted(merged.values(), key=lambda t: t["code"]), source)
        with self.db.con:
            self.db.con.execute("INSERT OR REPLACE INTO meta VALUES ('taxes_fetched_at', ?)",
                                (datetime.now().isoformat(timespec="seconds"),))
        log.info("Податкові ставки оновлено: %d (%s)", len(taxes), source)
        return {"taxes": len(taxes), "source": source}

    def sync(self, groups=True):
        goods, use_leftovers, taxes, source = self.call(sync_all, self.api, self.db, False, groups)
        bad = [g["name"] for g in goods if g["code"] != g["code"].strip()]
        return {"count": len(goods), "use_leftovers": use_leftovers, "bad_codes": bad,
                "taxes": len(taxes), "taxes_source": source, "no_tax": no_tax_goods(self.db)}

    # ---- зміна ----
    def shift(self, fresh=False):
        """fresh — для вкладки «Зміна» (там потрібен актуальний баланс); інакше — з кешу."""
        return {"shift": self.shift_state(force=fresh), "last_z": self.last_z()}

    def _synced_today(self):
        at = self.db.meta("synced_at") or ""
        return at[:10] == datetime.now().strftime("%Y-%m-%d")

    def _sync_quietly(self, opening=False):
        """
        Синхронізація при відкритті/закритті зміни: помилка не зриває саму операцію зі зміною.
        Локальні правки відправляються завжди. Товари з порталу (звірка зі змінами, зробленими в кабінеті
        Checkbox) — раз на день, перед першим відкриттям зміни; далі — лише кнопкою «Синхронізувати».
        """
        try:
            if opening and not self._synced_today():
                log.info("Перше відкриття зміни сьогодні — звіряю товари з Checkbox")
                r = self.full_sync(groups=False)
                return {"pushed": r["pushed"], "push_error": r["push_error"], "pending": r["pending"], "goods": r["count"]}
            pushed, push_error = 0, None
            if self.db.pending_count():
                try:
                    pushed = self.push_pending()
                except (CheckboxError, net.RequestException) as e:
                    push_error = str(e)
                if pushed:          # відправлені правки — у локальну копію товарів (без груп і ставок)
                    self.sync(groups=False)
            return {"pushed": pushed, "push_error": push_error, "pending": self.db.pending_count()}
        except (CheckboxError, net.RequestException, UserError) as e:
            log.warning("Синхронізація при зміні не вдалася: %s", e)
            return {"sync_error": str(e), "pending": self.db.pending_count()}

    def open_shift(self):
        if self.shift_state(force=True):
            raise UserError("Зміна вже відкрита")
        sync = self._sync_quietly(opening=True)    # товари звіряємо до відкриття: продаж — уже з актуальними
        shift = self.call(self.api.open_shift)
        self._set_shift(shift)
        return {"shift": shift, "sync": sync}

    def close_shift(self):
        if not self.shift_state(force=True):
            raise UserError("Немає відкритої зміни")
        sync = self._sync_quietly()          # правки дня — у Checkbox ще до Z-звіту
        shift = self.call(self.api.close_shift)
        self._set_shift(None)
        if shift.get("id") and shift.get("closed_at"):      # журнал: зміна вже не «відкрита»
            with self.db.con:
                self.db.con.execute("UPDATE sales SET shift_closed_at=? WHERE shift_id=?",
                                    (local_time(shift["closed_at"]), shift["id"]))
        z = shift.get("z_report") or {}
        if z.get("id"):
            self.db.add_z_report(dict(z, shift_id=z.get("shift_id") or shift.get("id"),
                                      created_at=z.get("created_at") or shift.get("closed_at")),
                                 shift.get("serial"), "kasa")
            with self.db.con:
                self.db.con.execute("INSERT OR REPLACE INTO meta VALUES ('last_z_report', ?)",
                                    (json.dumps({"id": z["id"], "serial": shift.get("serial"),
                                                 "closed_at": local_time(shift.get("closed_at"))}),))
        log.info("Зміну №%s закрито, Z-звіт %s", shift.get("serial"), z.get("id"))
        return {"shift": shift, "z_report_id": z.get("id"), "sync": sync}

    def x_report(self):
        if not self.shift_state():
            raise UserError("Немає відкритої зміни")
        report = self.call(self.api.create_x_report)
        return {"report_id": report["id"]}

    def report_text(self, report_id):
        if not self.RECEIPT_ID.match(report_id or ""):
            raise UserError("Невірний ID звіту")
        return {"text": self.call(self.api.report_text, report_id)}

    def report_png(self, report_id):
        """PNG X- чи Z-звіту. Звіт не змінюється — з Checkbox беремо лише раз, далі з диска."""
        if not self.RECEIPT_ID.match(report_id or ""):
            raise UserError("Невірний ID звіту")
        cache = DATA_DIR / "reports" / f"{report_id.lower()}.png"
        if cache.exists():
            return cache.read_bytes()
        png = self.call(self.api.report_png, report_id)
        if not png.startswith(b"\x89PNG"):
            raise CheckboxError("Checkbox повернув не PNG")
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".tmp")
            tmp.write_bytes(png)
            tmp.replace(cache)
        except OSError:
            pass
        return png

    def z_reports(self):
        return {"reports": self.db.z_reports()}

    def last_z(self):
        try:
            return json.loads(self.db.meta("last_z_report") or "null")
        except ValueError:
            return None

    # ---- службове внесення / винесення ----
    def service(self, body):
        value = body.get("value")
        if not isinstance(value, int) or isinstance(value, bool) or not value or abs(value) > 10 ** 10:
            raise UserError("Невірна сума")
        shift = self.shift_state(force=value < 0)   # для винесення — актуальний залишок готівки
        if not shift:
            raise UserError("Спочатку відкрийте зміну", code="SHIFT_CLOSED")
        if value < 0:
            cash = (shift.get("balance") or {}).get("balance")
            if isinstance(cash, int) and -value > cash:
                raise UserError(f"У касі лише {cash / 100:.2f} грн готівки")
        kind = "SERVICE_IN" if value > 0 else "SERVICE_OUT"
        receipt = self.call(self.api.service, new_receipt_id(), value)
        self.db.add_sale(receipt, "CASH", [], None, None, kind, total=abs(value))
        log.info("%s: чек %s, %.2f грн", "Внесення" if value > 0 else "Винесення", receipt["id"], abs(value) / 100)
        return {"id": receipt["id"], "fiscal_code": receipt.get("fiscal_code"), "value": value}

    # ---- продаж ----
    def _cart(self, body):
        """Кошик з інтерфейсу → (рядки, знижки рядків, знижки чека, суми рядків, разом, облік залишків)."""
        items = body.get("items") or []
        if not items:
            raise UserError("Кошик порожній")
        use_leftovers = self.db.meta("use_leftovers")
        cart, all_marks = [], set()
        for it in items:
            row = self.db.con.execute("SELECT * FROM goods WHERE id=?", (it.get("id"),)).fetchone()
            if not row:
                raise UserError("Товар не знайдено в локальній базі — синхронізуйте товари")
            qty = it.get("qty")
            if not isinstance(qty, int) or qty <= 0 or (not row["is_weight"] and qty % 1000):
                raise UserError(f"Невірна кількість: {row['name']}")
            if use_leftovers and row["count"] is not None and qty > row["count"]:
                raise UserError(f"Недостатньо залишку: {row['name']}")

            marks = it.get("marks") or []
            if not isinstance(marks, list) or not all(isinstance(m, str) for m in marks):
                raise UserError(f"Невірні акцизні марки: {row['name']}")
            marks = [m.strip() for m in marks if m.strip()]
            bad = [m for m in marks if len(m) != EXCISE_MARK_LEN]
            if bad:
                raise UserError(f"Акцизна марка має містити {EXCISE_MARK_LEN} символів: «{bad[0]}» — {len(bad[0])} ({row['name']})",
                                code="EXCISE_FORMAT")
            if marks and not row["is_weight"] and len(marks) > qty // 1000:
                raise UserError(f"Марок більше, ніж одиниць товару: {row['name']}")
            dup = all_marks.intersection(marks) or (len(set(marks)) != len(marks))
            if dup:
                raise UserError(f"Акцизна марка повторюється: {row['name']}")
            all_marks.update(marks)

            cart.append((row, qty, marks))
        sold = self.db.excise_sold(all_marks)
        if sold:
            m, r = next(iter(sold.items()))
            when = (r.get("sold_at") or "").replace("T", " ")[:16]
            more = f" (і ще {len(sold) - 1})" if len(sold) > 1 else ""
            raise UserError(f"Акцизна марка {m} вже продана{more}: {r.get('good_name') or 'товар'}, "
                            f"чек {r.get('fiscal_code') or r.get('receipt_id') or '—'} від {when or '—'}. "
                            "Повторно продати марку можна лише після повернення.", code="EXCISE_SOLD")

        try:
            line_ds, receipt_ds, line_totals, total = apply_discounts(
                [{"base": line_sum(g["price"], q), "tax_codes": good_tax_codes(g), "name": g["name"],
                  "spec": it.get("disc")} for (g, q, _), it in zip(cart, items)],
                body.get("discount"))
        except ValueError as e:
            raise UserError(str(e))
        if total <= 0:
            raise UserError("Сума чека має бути більшою за нуль")

        return cart, line_ds, receipt_ds, line_totals, total, use_leftovers

    def _payment(self, body, payment_type):
        """Дані ЕПЗ, сира відповідь терміналу й назва оплати з запиту."""
        card = clean_card_details(body.get("card")) if payment_type == "CASHLESS" else None
        terminal_raw = body.get("terminal_raw") if payment_type == "CASHLESS" else None
        if terminal_raw:
            log.info("POSSrv відповідь: %s", terminal_for_log(terminal_raw))
        try:
            label = clean_payment_label(body.get("label"))
        except ValueError as e:
            raise UserError(str(e))
        extra = dict(card or {})
        if label:
            extra["label"] = label
        details = {"card": card, "terminal": terminal_raw} if (card or terminal_raw) else None
        return extra, label, details

    @staticmethod
    def _delivery(body):
        """Телефон покупця → блок delivery (копія чека в SMS/Viber)."""
        try:
            phone = clean_phone(body.get("phone"))
        except ValueError as e:
            raise UserError(str(e))
        if phone:
            log.info("Копія чека на телефон %s", mask_phone(phone))
        return {"phone": phone} if phone else None

    def sell(self, body):
        payment_type = body.get("payment_type")
        if payment_type not in ("CASH", "CASHLESS"):
            raise UserError("Невірний тип оплати")
        if not body.get("items"):
            raise UserError("Кошик порожній")
        if not self.shift_state():
            raise UserError("Спочатку відкрийте зміну", code="SHIFT_CLOSED")
        cart, line_ds, receipt_ds, line_totals, total, use_leftovers = self._cart(body)

        rounding_mode = self.rounding_mode() if payment_type == "CASH" else None
        to_pay = round_cash(total, rounding_mode)
        pay_value = to_pay
        received = body.get("received")
        if payment_type == "CASH" and received is not None:
            if not isinstance(received, int) or received < to_pay:
                raise UserError(f"Отримано менше, ніж до сплати ({to_pay / 100:.2f} грн)")
            pay_value = received

        extra, label, details = self._payment(body, payment_type)

        # ID генерується один раз: якщо call() повторить запит після 401, чек не задублюється
        receipt_id = new_receipt_id()
        delivery = self._delivery(body)
        receipt = self.call(self.api.sell, receipt_id, build_goods_payload(cart, use_leftovers, line_ds),
                            payment_type, pay_value, rounding_mode, extra or None, receipt_ds, None, delivery)
        if receipt.get("total_sum") is not None and receipt["total_sum"] != total:
            log.warning("Сума чека %s у Checkbox %s ≠ розрахованій касою %s", receipt["id"], receipt["total_sum"], total)
        for g, qty, _ in cart:
            self.db.decrease_count(g["id"], qty)
        self.db.add_sale(receipt, payment_type,
                         [{"name": g["name"], "code": g["code"], "qty": qty, "price": g["price"], "sum": t,
                           **({"excise_barcodes": m} if m else {})}
                          for (g, qty, m), t in zip(cart, line_totals)], label, details)
        log.info("Продаж: чек %s, ФН %s, %.2f грн, %s%s, позицій %d", receipt["id"], receipt.get("fiscal_code"),
                 (receipt.get("total_sum") or total) / 100, payment_type, f" ({label})" if label else "", len(cart))
        return {"id": receipt["id"], "fiscal_code": receipt.get("fiscal_code"),
                "total_sum": receipt.get("total_sum", total),
                "round_sum": receipt.get("round_sum"),
                "rest": receipt.get("rest")}

    # ---- акцизні марки ----
    def excise_check(self, mark):
        mark = str(mark or "").strip()
        r = self.db.excise_sold([mark]).get(mark) if mark else None
        return {"mark": mark, "sold": bool(r), "info": r}

    def excise_list(self, q):
        rows, total, sold = self.db.excise_list(q)
        return {"marks": rows, "total": total, "sold": sold}

    # ---- групи товарів (одразу в Checkbox: POST/PUT/DELETE /goods/groups) ----
    GROUP_NAME_MAX = 100

    def _refresh_groups(self):
        groups = self.call(self.api.get_groups)
        if groups is not None:
            self.db.replace_groups(groups)

    def group_save(self, body):
        name = " ".join(str(body.get("name") or "").split())
        if not name:
            raise UserError("Вкажіть назву групи")
        if len(name) > self.GROUP_NAME_MAX:
            raise UserError(f"Назва групи — до {self.GROUP_NAME_MAX} символів")
        description = " ".join(str(body.get("description") or "").split()) or None
        gid, parent = body.get("id") or None, body.get("parent_id") or None
        rows = {g["id"]: g for g in self.db.group_rows()}
        if parent and parent not in rows:
            raise UserError("Батьківську групу не знайдено — синхронізуйте товари")
        if gid:
            if gid not in rows:
                raise UserError("Групу не знайдено — синхронізуйте товари")
            x, n = parent, 0               # не можна вкласти групу саму в себе чи у власну підгрупу
            while x and n < 50:
                if x == gid:
                    raise UserError("Групу не можна перенести в неї саму чи в її підгрупу")
                x, n = (rows.get(x) or {}).get("parent_id"), n + 1
            self.call(self.api.update_group, gid, name, description, parent)
            log.info("Групу «%s» змінено", name)
        else:
            created = self.call(self.api.create_group, name, description, parent)
            gid = (created or {}).get("id")
            log.info("Створено групу «%s» (%s)", name, gid)
        self._refresh_groups()
        return {"id": gid}

    def group_delete(self, body):
        gid = body.get("id")
        g = next((x for x in self.db.group_rows() if x["id"] == gid), None)
        if not g:
            raise UserError("Групу не знайдено")
        n = self.db.con.execute("SELECT COUNT(*) FROM goods WHERE group_id=?", (gid,)).fetchone()[0]
        kids = [x for x in self.db.group_rows() if x["parent_id"] == gid]
        if n or kids:
            raise UserError(f"У групі є {'товари (' + str(n) + ')' if n else ''}{' і ' if n and kids else ''}"
                            f"{'підгрупи (' + str(len(kids)) + ')' if kids else ''} — спершу перенесіть їх")
        self.call(self.api.delete_group, gid)
        log.info("Групу «%s» видалено", g["name"])
        self._refresh_groups()
        return {"ok": True}

    def group_move_goods(self, body):
        gid, to = body.get("id"), body.get("to_group")
        rows = {g["id"]: g for g in self.db.group_rows()}
        if gid not in rows or to not in rows or gid == to:
            raise UserError("Оберіть іншу групу")
        res = self.call(self.api.move_group_goods, gid, to)
        goods, use_leftovers = self.call(self.api.get_all_goods)     # group_id товарів змінився
        self.db.replace_goods(goods, use_leftovers)
        self._refresh_groups()
        log.info("Товари групи «%s» перенесено в «%s»: %s", rows[gid]["name"], rows[to]["name"], res)
        return {"moved": (res or {}).get("update")}

    # ---- передплата / післяплата ----
    NOTE_MAX = 120

    def prepay(self, body):
        """Чек передплати: усі товари кошика, оплачено лише внесок. Створює ланцюжок замовлення."""
        payment_type = body.get("payment_type")
        if payment_type not in ("CASH", "CASHLESS"):
            raise UserError("Невірний тип оплати")
        if not body.get("items"):
            raise UserError("Кошик порожній")
        if not self.shift_state():
            raise UserError("Спочатку відкрийте зміну", code="SHIFT_CLOSED")
        cart, line_ds, receipt_ds, line_totals, total, use_leftovers = self._cart(body)
        amount = body.get("amount")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise UserError("Вкажіть суму передплати")
        if amount >= total:
            raise UserError(f"Передплата має бути меншою за суму замовлення ({total / 100:.2f} грн) — "
                            "інакше це звичайний продаж")
        note = " ".join(str(body.get("note") or "").split())[:self.NOTE_MAX] or None
        extra, label, details = self._payment(body, payment_type)
        receipt, relation_id = self.call(
            self.api.prepayment, new_receipt_id(), build_goods_payload(cart, use_leftovers, line_ds),
            {"type": payment_type, "value": amount, **extra}, receipt_ds, self._delivery(body))
        if not relation_id:
            log.error("Чек передплати %s створено, але Checkbox не повернув pre_payment_relation_id", receipt["id"])
            raise UserError("Чек передплати пробито, але Checkbox не повернув номер замовлення. "
                            "Знайдіть його в кабінеті Checkbox.")
        items = [{"name": g["name"], "code": g["code"], "qty": q, "price": g["price"], "sum": t,
                  **({"excise_barcodes": m} if m else {})}
                 for (g, q, m), t in zip(cart, line_totals)]
        self.db.add_prepayment(relation_id, note, total, amount, items, self._chain_entry(receipt, "PREPAYMENT", amount, payment_type))
        self.db.add_sale(receipt, payment_type, items, label, details, "PREPAYMENT", relation_id, amount)
        log.info("Передплата: замовлення %s, чек %s, %.2f з %.2f грн, %s", relation_id, receipt["id"],
                 amount / 100, total / 100, payment_type)
        return {"id": receipt["id"], "relation_id": relation_id, "fiscal_code": receipt.get("fiscal_code"),
                "amount": amount, "total": total, "left": total - amount}

    @staticmethod
    def _chain_entry(receipt, kind, value, payment_type):
        return {"id": receipt["id"], "kind": kind, "value": value, "payment_type": payment_type,
                "fiscal_code": receipt.get("fiscal_code"), "at": datetime.now().isoformat(timespec="seconds")}

    def _open_chain(self, relation_id):
        p = self.db.prepayment(str(relation_id or ""))
        if not p:
            raise UserError("Замовлення не знайдено в касі")
        if p["status"] == "PAID":
            raise UserError("Замовлення вже повністю сплачено")
        if p["status"] == "RETURNED":
            raise UserError("Замовлення повернуто")
        return p

    def postpay(self, body):
        """
        Наступний чек ланцюжка. Сума, менша за залишок, — ще одна передплата;
        рівна залишку — післяплата, яка закриває замовлення (готівкою — із заокругленням і рештою).
        """
        p = self._open_chain(body.get("relation_id"))
        payment_type = body.get("payment_type")
        if payment_type not in ("CASH", "CASHLESS"):
            raise UserError("Невірний тип оплати")
        if not self.shift_state():
            raise UserError("Спочатку відкрийте зміну", code="SHIFT_CLOSED")
        left = p["left"]
        amount = body.get("amount", left)
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise UserError("Невірна сума")
        if amount > left:
            raise UserError(f"До сплати лишилось {left / 100:.2f} грн")
        closing = amount == left
        rounding_mode = self.rounding_mode() if payment_type == "CASH" and closing else None
        value = round_cash(amount, rounding_mode)
        received = body.get("received")
        if payment_type == "CASH" and closing and received is not None:
            if not isinstance(received, int) or received < value:
                raise UserError(f"Отримано менше, ніж до сплати ({value / 100:.2f} грн)")
            value = received
        extra, label, details = self._payment(body, payment_type)
        receipt = self.call(self.api.postpayment, p["relation_id"], new_receipt_id(),
                            {"type": payment_type, "value": value, **extra}, rounding_mode, self._delivery(body))
        kind = "POSTPAYMENT" if closing else "PREPAYMENT"
        self.db.update_prepayment(p["relation_id"], paid=p["paid"] + amount, status="PAID" if closing else None,
                                  receipt=self._chain_entry(receipt, kind, amount, payment_type))
        self.db.add_sale(receipt, payment_type, p["items"] if closing else [], label, details, kind,
                         p["relation_id"], amount)
        log.info("%s: замовлення %s, чек %s, %.2f грн, %s", "Післяплата" if closing else "Ще передплата",
                 p["relation_id"], receipt["id"], amount / 100, payment_type)
        return {"id": receipt["id"], "fiscal_code": receipt.get("fiscal_code"), "closed": closing,
                "amount": amount, "left": left - amount, "round_sum": receipt.get("round_sum"),
                "rest": receipt.get("rest") or receipt.get("total_rest")}

    def prepay_return(self, body):
        """Повернення всього замовлення: Checkbox пробиває чек повернення на кожен чек ланцюжка."""
        p = self.db.prepayment(str(body.get("relation_id") or ""))
        if not p:
            raise UserError("Замовлення не знайдено в касі")
        if p["status"] == "RETURNED":
            raise UserError("Замовлення вже повернуто")
        if not self.shift_state():
            raise UserError("Спочатку відкрийте зміну", code="SHIFT_CLOSED")
        resp = self.call(self.api.prepayment_return, p["relation_id"])
        self.db.update_prepayment(p["relation_id"], status="RETURNED")
        marks = [m for it in p.get("items") or [] for m in it.get("excise_barcodes") or []]
        if marks:
            self.db.excise_release(marks, p["relation_id"])
        # чеки повернення — у журнал (якщо Checkbox повернув їх у відповіді)
        found = []
        def walk(x):
            if isinstance(x, dict):
                if x.get("id") and x.get("type") and ("goods" in x or "payments" in x):
                    found.append(x)
                else:
                    for v in x.values():
                        walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(resp)
        for r in found:
            self.db.add_checkbox_receipt(r)
        log.info("Повернення замовлення %s: чеків повернення у відповіді %d", p["relation_id"], len(found))
        return {"relation_id": p["relation_id"], "receipts": [r["id"] for r in found], "paid": p["paid"]}

    # pre_payment_status Checkbox → статус замовлення в касі
    PRE_STATUS = {"PARTIAL_PAID": "OPEN", "CREATED": "OPEN", "NEW": "OPEN", "PAID": "PAID", "COMPLETED": "PAID",
                  "CANCELLED": "RETURNED", "CANCELED": "RETURNED", "RETURNED": "RETURNED"}

    @staticmethod
    def _chain_receipts(chain, kind):
        """Чеки однієї частини ланцюжка (список, один чек або лише id) → короткі записи для касира."""
        if chain is None:
            return []
        out = []
        for r in chain if isinstance(chain, list) else [chain]:
            if isinstance(r, str):
                out.append({"id": r, "kind": kind})
                continue
            if not isinstance(r, dict):
                continue
            pays = r.get("payments") or []
            paid = sum(x.get("value") or 0 for x in pays if isinstance(x, dict))
            ptypes = {x.get("type") for x in pays if isinstance(x, dict)}
            labels = [x.get("label") for x in pays if isinstance(x, dict) and x.get("label")]
            out.append({
                "id": r.get("id"), "kind": "RETURN" if (r.get("type") == "RETURN" or kind == "RETURN") else kind,
                "fiscal_code": r.get("fiscal_code"), "serial": r.get("serial"), "status": r.get("status"),
                "at": local_time(r.get("fiscal_date") or r.get("created_at")) if (r.get("fiscal_date") or r.get("created_at")) else None,
                "sum": paid if pays else r.get("total_sum"),
                "payment": "CASH" if ptypes == {"CASH"} else "CASHLESS" if ptypes == {"CASHLESS"} else ("MIXED" if ptypes else None),
                "label": ", ".join(dict.fromkeys(labels)) or None,
            })
        return out

    def _state_summary(self, st):
        """Відповідь GET /prepayment-receipts/{id} → те, що потрібно касиру."""
        chain = st.get("receipts_chain") or {}
        ids = st.get("receipt_chain") or {}
        receipts = (self._chain_receipts(chain.get("pre_payment_receipts") or ids.get("pre_payment_receipts"), "PREPAYMENT")
                    + self._chain_receipts(chain.get("after_payment_receipt") or ids.get("after_payment_receipt"), "POSTPAYMENT")
                    + self._chain_receipts(chain.get("returned_receipts") or ids.get("returned_receipts"), "RETURN"))
        receipts.sort(key=lambda r: r.get("at") or "")
        goods = []
        first = next((r for r in (chain.get("pre_payment_receipts") or []) if isinstance(r, dict)), None)
        for ln in (first or {}).get("goods") or []:
            g = ln.get("good") or {}
            qty = ln.get("quantity") or 0
            goods.append({"name": g.get("name"), "qty": qty, "price": g.get("price"),
                          "sum": ln.get("total_sum") if isinstance(ln.get("total_sum"), int) else ln.get("sum"),
                          "is_weight": bool(qty % 1000)})
        raw = str(st.get("pre_payment_status") or st.get("status") or "").upper()
        return {
            "relation_id": str(st.get("relation_id") or ""), "status_raw": raw or None,
            "status": self.PRE_STATUS.get(raw), "total": st.get("total_sum"), "paid": st.get("paid_sum"),
            "paid_cash": st.get("paid_by_cash_sum"), "left": st.get("left_to_pay"),
            "created_at": local_time(st["created_at"]) if st.get("created_at") else None,
            "returned_at": local_time(st["last_returned_receipt_at"]) if st.get("last_returned_receipt_at") else None,
            "goods": goods, "receipts": receipts,
        }

    def prepay_state(self, relation_id):
        p = self.db.prepayment(str(relation_id or ""))
        if not p:
            raise UserError("Замовлення не знайдено в касі")
        state = self.call(self.api.prepayment_state, p["relation_id"])
        if not isinstance(state, dict):
            raise CheckboxError("Неочікувана відповідь про стан замовлення")
        summary = self._state_summary(state)
        # Checkbox — джерело правди: суми й статус могли змінитися поза касою (доплата в кабінеті тощо)
        if isinstance(summary["paid"], int) and isinstance(summary["total"], int):
            self.db.set_prepayment_state(p["relation_id"], summary["total"], summary["paid"], summary["status"])
        summary["note"] = p["note"]
        return {"summary": summary, "state": state}

    def prepayments(self):
        return {"prepayments": self.db.prepayments(), "cash_rounding": self.rounding_mode()}

    # ---- повернення ----
    def _sale_for_return(self, receipt_id):
        if not self.RECEIPT_ID.match(receipt_id or ""):
            raise UserError("Невірний ID чека")
        r = self.call(self.api.get_receipt, receipt_id)
        if (r.get("type") or "SELL") != "SELL":
            raise UserError("Повернути можна лише чек продажу")
        if r.get("status") not in (None, "DONE"):
            raise UserError(f"Чек ще не фіскалізовано (статус {r.get('status')})")
        lines = return_lines(r, self.db.returned_qty(r["id"]))
        for ln in lines:            # ваговий: за локальним товаром, інакше — за дробовою кількістю
            row = self.db.con.execute("SELECT is_weight FROM goods WHERE TRIM(code)=TRIM(?)",
                                      (str(ln["code"]),)).fetchone()
            ln["is_weight"] = bool(row["is_weight"]) if row else bool(ln["qty"] % 1000)
        return r, lines

    def receipt_info(self, receipt_id):
        """Чек продажу для вікна повернення: рядки з уже повернутим, оплата оригіналу."""
        r, lines = self._sale_for_return(receipt_id)
        pays = r.get("payments") or []
        p = pays[0] if pays else {}
        card = {k: p.get(k) for k in CARD_TEXT_FIELDS if p.get(k)}
        return {"id": r["id"], "fiscal_code": r.get("fiscal_code"), "total_sum": r.get("total_sum"),
                "terminal": self._sale_terminal(r["id"], card),
                "created_at": local_time(r.get("fiscal_date") or r.get("created_at")),
                "payment_type": p.get("type") or "CASH", "payment_label": p.get("label"),
                "payments": len(pays), "card": card, "lines": lines,
                "cash_rounding": self.rounding_mode()}

    def _sale_terminal(self, receipt_id, card):
        """
        Реквізити банківської транзакції продажу для команди «Відміна / повернення» POSSrv:
        з відповіді терміналу, збереженої при продажу, інакше — з даних ЕПЗ у чеку Checkbox.
        """
        out = {}
        row = self.db.con.execute("SELECT payment_details FROM sales WHERE lower(receipt_id)=lower(?)",
                                  (receipt_id,)).fetchone()
        try:
            details = json.loads(row["payment_details"] or "null") if row else None
        except ValueError:
            details = None
        msgs = (details or {}).get("terminal") or []
        for m in reversed(msgs if isinstance(msgs, list) else [msgs]):
            res = m.get("result") if isinstance(m, dict) else None
            if isinstance(res, dict) and (res.get("rrn") or res.get("invoice_num")):
                for key, src in (("rrn", "rrn"), ("invoicenum", "invoice_num"), ("auth_code", "auth_code")):
                    if res.get(src) not in (None, "", 0):
                        out[key] = str(res[src])
                break
        if not out.get("rrn") and card.get("rrn"):
            out["rrn"] = card["rrn"]
        if not out.get("auth_code") and card.get("auth_code"):
            out["auth_code"] = card["auth_code"]
        if not out.get("invoicenum") and card.get("receipt_no"):
            out["invoicenum"] = card["receipt_no"]
        return out

    def return_receipt(self, body):
        if not self.shift_state():
            raise UserError("Спочатку відкрийте зміну", code="SHIFT_CLOSED")
        orig, lines = self._sale_for_return(body.get("id"))
        payment_type = body.get("payment_type")
        if payment_type not in ("CASH", "CASHLESS"):
            raise UserError("Невірний тип оплати")
        use_leftovers = self.db.meta("use_leftovers")
        goods, items, total, seen = [], [], 0, set()
        for it in body.get("items") or []:
            idx, qty = it.get("line"), it.get("qty")
            if not isinstance(idx, int) or not 0 <= idx < len(lines) or idx in seen:
                raise UserError("Невірна позиція чека")
            seen.add(idx)
            ln = lines[idx]
            if not isinstance(qty, int) or qty <= 0 or (not ln["is_weight"] and qty % 1000):
                raise UserError(f"Невірна кількість: {ln['name']}")
            if qty > ln["max"]:
                raise UserError(f"{ln['name']}: можна повернути не більше {ln['max'] / 1000:g}")
            item, amount = return_good_payload(ln, qty, use_leftovers)
            goods.append(item)
            total += amount
            items.append({"line": idx, "name": ln["name"], "code": ln["code"], "qty": qty,
                          "price": ln["price"], "sum": amount,
                          **({"excise_barcodes": item["good"]["excise_barcodes"]}
                             if item["good"].get("excise_barcodes") else {})})
        if not goods:
            raise UserError("Оберіть товари для повернення")
        if total <= 0:
            raise UserError("Сума повернення має бути більшою за нуль")
        rounding_mode = self.rounding_mode() if payment_type == "CASH" else None
        pay_value = round_cash(total, rounding_mode)
        extra, label, details = self._payment(body, payment_type)
        receipt = self.call(self.api.sell, new_receipt_id(), goods, payment_type, pay_value,
                            rounding_mode, extra or None, None, orig["id"], self._delivery(body))
        if use_leftovers:                     # повернутий товар знову на залишку
            for g in goods:
                if g.get("good_id"):
                    self.db.decrease_count(g["good_id"], -g["quantity"])
        self.db.add_sale(receipt, payment_type, items, label, details, "RETURN", orig["id"], total)
        log.info("Повернення: чек %s до %s, %.2f грн, %s, позицій %d", receipt["id"], orig["id"],
                 total / 100, payment_type, len(items))
        return {"id": receipt["id"], "fiscal_code": receipt.get("fiscal_code"),
                "total_sum": receipt.get("total_sum", total), "round_sum": receipt.get("round_sum")}

    # ---- журнал ----
    # ---- журнал чеків: останні JOURNAL_DAYS днів ----
    def _cutoff(self):
        return datetime.now() - timedelta(days=JOURNAL_DAYS)

    def purge_old(self):
        """Видалити з каси записи й PNG чеків, старші за JOURNAL_DAYS днів (у Checkbox вони лишаються)."""
        cutoff = self._cutoff()
        ids = {i.lower() for i in self.db.purge_sales(cutoff.isoformat(timespec="seconds"))}
        removed_png = 0
        folder = DATA_DIR / "receipts"
        if folder.exists():
            limit = cutoff.timestamp()
            for f in folder.glob("*.png"):
                try:
                    if f.stem.lower() in ids or f.stat().st_mtime < limit:
                        f.unlink()
                        removed_png += 1
                except OSError:
                    pass
        if ids or removed_png:
            log.info("Очищення журналу: видалено чеків %d, зображень %d (старші за %d днів)",
                     len(ids), removed_png, JOURNAL_DAYS)
        return len(ids), removed_png

    def sales(self):
        self.purge_old()
        since = self._cutoff().isoformat(timespec="seconds")
        return {"sales": [dict(r) for r in self.db.sales(5000, since)],
                "days": JOURNAL_DAYS, "fetched_at": self.db.meta("receipts_fetched_at")}

    def fetch_receipts(self):
        """Завантажити з Checkbox чеки за останні JOURNAL_DAYS днів (лише нові записи)."""
        now = datetime.now().astimezone()
        receipts = self.call(self.api.search_receipts, now - timedelta(days=JOURNAL_DAYS), now)
        new = sum(1 for r in receipts if r.get("id") and self.db.add_checkbox_receipt(r))
        z_new, z_error = 0, None
        try:
            for z in self.call(self.api.z_reports, now - timedelta(days=JOURNAL_DAYS), now):
                z_new += 1 if self.db.add_z_report(z) else 0
        except (CheckboxError, net.RequestException) as e:
            z_error = str(e)
            log.warning("Z-звіти з Checkbox не завантажено: %s", e)
        stamp = datetime.now().isoformat(timespec="seconds")
        with self.db.con:
            self.db.con.execute("INSERT OR REPLACE INTO meta VALUES ('receipts_fetched_at', ?)", (stamp,))
        removed, _ = self.purge_old()
        log.info("Завантаження чеків з Checkbox: отримано %d, нових %d", len(receipts), new)
        return {"fetched": len(receipts), "new": new, "removed": removed, "fetched_at": stamp, "z_new": z_new, "z_error": z_error}

    def receipt_text(self, receipt_id):
        if not receipt_id:
            raise UserError("Не вказано ID чека")
        return {"text": self.call(self.api.receipt_text, receipt_id)}

    RECEIPT_ID = re.compile(r"^[0-9a-fA-F-]{36}$")

    def receipt_png(self, receipt_id):
        """PNG чека. Фіскальний чек не змінюється, тому кешуємо на диску: з Checkbox — лише раз."""
        if not self.RECEIPT_ID.match(receipt_id or ""):
            raise UserError("Невірний ID чека")
        cache = DATA_DIR / "receipts" / f"{receipt_id.lower()}.png"
        if cache.exists():
            return cache.read_bytes()
        png = self.call(self.api.receipt_png, receipt_id)
        if not png.startswith(b"\x89PNG"):
            raise CheckboxError("Checkbox повернув не PNG")
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_bytes(png)
        tmp.replace(cache)
        return png


APP: App = None
EXCISE_MARK_LEN = 10        # акцизна марка: серія й номер — рівно 10 символів

# --- власні зображення плиток (лише локально, у папці даних) ---
IMG_DIR = DATA_DIR / "images"
IMG_KINDS = ("good", "group")          # товар — за кодом, група — за id з Checkbox
IMG_TYPES = {"png": b"\x89PNG", "jpeg": b"\xff\xd8\xff", "webp": b"RIFF", "gif": b"GIF8"}
IMG_MAX = 3 * 1024 * 1024


def _img_stem(kind, key):
    return f"{kind}_{key.encode('utf-8').hex()}"


def _img_check(kind, key):
    key = str(key or "").strip()
    if kind not in IMG_KINDS or not key or len(key) > 200:
        raise UserError("Невірний об'єкт для зображення")
    return key


def images_index():
    """{"good:<код>": версія, "group:<id>": версія} — щоб сторінка знала, де показати власну картинку."""
    out = {}
    if IMG_DIR.is_dir():
        for f in IMG_DIR.iterdir():
            kind, _, rest = f.stem.partition("_")
            if kind in IMG_KINDS and f.suffix[1:] in IMG_TYPES:
                try:
                    out[f"{kind}:{bytes.fromhex(rest).decode('utf-8')}"] = int(f.stat().st_mtime * 1000)
                except (ValueError, OSError):
                    continue
    return out


def _img_files(kind, key):
    stem = _img_stem(kind, key)
    return [IMG_DIR / f"{stem}.{ext}" for ext in IMG_TYPES]


def image_set(body):
    kind = (body or {}).get("kind")
    key = _img_check(kind, body.get("key"))
    m = re.match(r"^data:image/(png|jpeg|webp|gif);base64,(.+)$", str(body.get("data") or ""), re.S)
    if not m:
        raise UserError("Потрібне зображення PNG, JPEG, WebP або GIF")
    try:
        raw = base64.b64decode(m.group(2), validate=False)
    except ValueError:
        raise UserError("Пошкоджене зображення")
    ext = m.group(1)
    if not raw.startswith(IMG_TYPES[ext]) or len(raw) > IMG_MAX:
        raise UserError("Пошкоджене або завелике зображення (до 3 МБ)")
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    for f in _img_files(kind, key):
        f.unlink(missing_ok=True)
    target = IMG_DIR / f"{_img_stem(kind, key)}.{ext}"
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(raw)
    tmp.replace(target)
    log.info("Зображення плитки %s %s: %d байт", kind, key, len(raw))
    return {"ok": True, "images": images_index()}


def image_delete(body):
    kind = (body or {}).get("kind")
    key = _img_check(kind, body.get("key"))
    for f in _img_files(kind, key):
        f.unlink(missing_ok=True)
    log.info("Зображення плитки %s %s прибрано", kind, key)
    return {"ok": True, "images": images_index()}


def image_file(k):
    kind, _, key = str(k or "").partition(":")
    try:
        key = _img_check(kind, key)
    except UserError:
        return None, None
    for f in _img_files(kind, key):
        if f.is_file():
            return f.read_bytes(), "image/" + f.suffix[1:]
    return None, None


def termlog(body):
    """
    Обмін браузера з POSSrv через WebSocket (оплата, повернення) — у журнал: що надіслано на термінал
    і що він відповів. Самі запити йдуть з браузера напряму, тож сервер каси їх інакше не бачить.
    """
    if not isinstance(body, dict):
        raise UserError("Невірні дані")
    event, cmd = str(body.get("event") or "")[:20], str(body.get("cmd") or "")[:20]
    data = redact(body.get("data"))
    titles = {"send": "→ термінал", "message": "← термінал", "close": "з'єднання закрито", "error": "помилка з'єднання"}
    log.info("POSSrv WS %s %s%s", cmd, titles.get(event, event), f": {log_json(data)}" if data is not None else "",
             extra={"data": {"ws": cmd, "event": event, "payload": data, "url": body.get("url")}})
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # тиша в консолі

    def _send(self, code, data, ctype="application/json; charset=utf-8", cache="no-store"):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)  # no-store: завжди свіжа версія сторінки після оновлень
        self.end_headers()
        self.wfile.write(body)

    def _run_png(self, fn, object_id):
        """Як _run, але відповідь — зображення, а помилка — JSON з кодом 4xx/5xx."""
        try:
            with APP_LOCK:
                png = fn(object_id)
            self._send(200, png, "image/png")
        except UserError as e:
            self._send(400, {"error": str(e)})
        except CheckboxError as e:
            self._send(502, {"error": f"Checkbox: {e}"})
        except net.RequestException as e:
            self._send(502, {"error": f"Немає зв'язку з Checkbox: {e}"})

    def _run(self, fn, *args, locked=True):
        try:
            if locked:
                with APP_LOCK:
                    result = fn(*args)
            else:
                result = fn(*args)
            self._send(200, result)
        except UserError as e:
            log.warning("%s %s: %s", self.command, self.path, e)
            self._send(400, {"error": str(e), "code": e.code})
        except CheckboxError as e:
            self._send(502, {"error": f"Checkbox: {e}"})
        except net.RequestException as e:
            self._send(502, {"error": f"Немає зв'язку з Checkbox: {e}"})
        except Exception as e:  # noqa
            log.exception("Внутрішня помилка: %s %s", self.command, self.path)   # з трасуванням
            self._send(500, {"error": f"Внутрішня помилка: {e}. Подробиці — у журналі {LOG_PATH}"})

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/ping":
            return self._send(200, {"app": APP_ID})
        if u.path == "/api/config":         # лише читає прапорці — без черги, щоб показати «Підключаюся…»
            return self._run(APP.config, locked=False)
        if u.path == "/api/update/check":   # звернення до GitHub — без черги, щоб не гальмувати касу
            return self._run(update_check, bool(q.get("force")), locked=False)
        routes = {
            "/api/goods": APP.goods,
            "/api/sales": APP.sales,
        }
        if u.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif u.path in routes:
            self._run(routes[u.path])
        elif u.path == "/img":      # власне зображення плитки; ?v= змінюється при заміні — можна кешувати
            data, ctype = image_file((q.get("k") or [""])[0])
            if data is None:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, data, ctype, "private, max-age=31536000, immutable")
        elif u.path == "/api/receipt_text":
            self._run(APP.receipt_text, (q.get("id") or [""])[0])
        elif u.path == "/api/receipt_png":
            self._run_png(APP.receipt_png, (q.get("id") or [""])[0])
        elif u.path == "/api/report_png":
            self._run_png(APP.report_png, (q.get("id") or [""])[0])
        elif u.path == "/api/report_text":
            self._run(APP.report_text, (q.get("id") or [""])[0])
        elif u.path == "/api/receipt":
            self._run(APP.receipt_info, (q.get("id") or [""])[0])
        elif u.path == "/api/shift":
            self._run(APP.shift, bool(q.get("fresh")))
        elif u.path == "/api/goods/next-code":
            self._run(APP.next_code)
        elif u.path == "/api/excise/check":
            self._run(APP.excise_check, (q.get("mark") or [""])[0])
        elif u.path == "/api/excise":
            self._run(APP.excise_list, (q.get("q") or [""])[0])
        elif u.path == "/api/zreports":
            self._run(APP.z_reports)
        elif u.path == "/api/prepayments":
            self._run(APP.prepayments)
        elif u.path == "/api/prepay/state":
            self._run(APP.prepay_state, (q.get("id") or [""])[0])
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "Невірний JSON"})
        routes = {
            "/api/sync": APP.full_sync,
            "/api/shift/open": APP.open_shift,
            "/api/shift/close": APP.close_shift,
            "/api/xreport": APP.x_report,
        }
        path = urlparse(self.path).path
        if path in routes:
            self._run(routes[path])
        elif path == "/api/sell":
            self._run(APP.sell, body)
        elif path == "/api/return":
            self._run(APP.return_receipt, body)
        elif path == "/api/groups/save":
            self._run(APP.group_save, body)
        elif path == "/api/groups/delete":
            self._run(APP.group_delete, body)
        elif path == "/api/groups/move-goods":
            self._run(APP.group_move_goods, body)
        elif path == "/api/image/set":
            self._run(image_set, body, locked=False)
        elif path == "/api/image/delete":
            self._run(image_delete, body, locked=False)
        elif path == "/api/taxes/refresh":
            self._run(APP.refresh_taxes)
        elif path == "/api/termlog":
            self._run(termlog, body, locked=False)
        elif path == "/api/prepay":
            self._run(APP.prepay, body)
        elif path == "/api/postpay":
            self._run(APP.postpay, body)
        elif path == "/api/prepay/return":
            self._run(APP.prepay_return, body)
        elif path == "/api/service":
            self._run(APP.service, body)
        elif path == "/api/config":
            self._run(APP.save_config, body)
        elif path == "/api/terminal/devices":
            self._run(APP.terminal_devices, body)
        elif path == "/api/terminal/register":
            self._run(APP.terminal_register, body)
        elif path == "/api/goods/save":
            self._run(APP.save_good, body)
        elif path == "/api/sales/fetch":
            self._run(APP.fetch_receipts)
        elif path == "/api/goods/discard":
            self._run(APP.discard_good, body)
        elif path == "/api/terminal/save":
            self._run(APP.terminal_save, body)
        elif path == "/api/update/install":
            self._run(update_install)
        elif path == "/api/quit":
            self._send(200, {"ok": True})
            # shutdown() не можна викликати з потоку, що обслуговує запит
            threading.Thread(target=SERVER.shutdown, daemon=True).start()
        else:
            self._send(404, {"error": "not found"})


# Браузер закрив з'єднання посеред відповіді (оновили/закрили вкладку) — це не помилка
CLIENT_GONE = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


class KasaServer(ThreadingHTTPServer):
    daemon_threads = True
    # У Windows SO_REUSEADDR дозволяє двом програмам зайняти один порт — там вимикаємо
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, CLIENT_GONE):
            return
        # стандартний сервер друкує трасування в консоль; ми пишемо його в журнал
        log.exception("Помилка обробки запиту від %s", client_address[0])


PAGE = r"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Checkbox облік</title>
<meta name="color-scheme" content="light dark">
<meta name="darkreader-lock">
<script>
// тема до першого малювання сторінки (без білого спалаху): auto — як у системі
(function () {
  var m = 'auto'; try { m = localStorage.getItem('kasa_theme') || 'auto'; } catch (e) {}
  var dark = m === 'dark' || (m === 'auto' && window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
})();
</script>
<style>
  :root {
    color-scheme: light;
    --bg: #f4f5f7; --card: #fff; --surface: #fff; --subtle: #f6f7f9; --hover: #f3f4f6; --text: #1d2330; --muted: #6b7280; --line: #e5e7eb;
    --accent: #2563eb; --accent-2: #1d4ed8; --ok: #16a34a; --warn: #d97706; --bad: #dc2626;
    --accent-btn: #2563eb; --accent-btn-2: #1d4ed8; --ok-btn: #16a34a; --bad-btn: #dc2626; --purple: #7c3aed; --purple-2: #6d28d9;
    --sec: #e5e7eb; --sec-hover: #d9dce1; --header: #1d2330; --header-muted: #b6bcc8; --badge: #4b5563; --toast: #1d2330; --pre: #111827;
    --ok-bg: #ecfdf5; --ok-fg: #065f46; --ok-line: #86efac; --bad-bg: #fef2f2; --bad-fg: #991b1b; --bad-line: #fca5a5;
    --warn-bg: #fffbeb; --warn-fg: #92400e; --info-bg: #eff6ff; --pre-bg: #ede9fe; --pre-fg: #5b21b6;
    --shadow: 0 10px 30px rgba(15, 23, 42, .18);
  }
  /* Темна тема: графітовий фон, приглушені підкладки, яскравіші акценти; кнопки — насичені, щоб білий текст читався */
  :root[data-theme="dark"] {
    color-scheme: dark;
    --bg: #0f1217; --card: #171b22; --surface: #1d222b; --subtle: #1b2028; --hover: #222833; --text: #e4e7ec; --muted: #8e98a8; --line: #2a313c;
    --accent: #6ea2ff; --accent-2: #8fb7ff; --ok: #34d399; --warn: #fbbf24; --bad: #f87171;
    --accent-btn: #2f6fe4; --accent-btn-2: #3b7cf2; --ok-btn: #15803d; --bad-btn: #c62828; --purple: #7c4dff; --purple-2: #8b63ff;
    --sec: #262c36; --sec-hover: #2f3642; --header: #0a0d11; --header-muted: #8e98a8; --badge: #2f3642; --toast: #2a313c; --pre: #0b0e12;
    --ok-bg: rgba(52, 211, 153, .12); --ok-fg: #6ee7b7; --ok-line: rgba(52, 211, 153, .45);
    --bad-bg: rgba(248, 113, 113, .13); --bad-fg: #fca5a5; --bad-line: rgba(248, 113, 113, .45);
    --warn-bg: rgba(251, 191, 36, .12); --warn-fg: #fcd34d; --info-bg: rgba(110, 162, 255, .13);
    --pre-bg: rgba(139, 92, 246, .2); --pre-fg: #c4b5fd;
    --shadow: 0 10px 30px rgba(0, 0, 0, .55);
  }
  :root[data-theme="dark"] .modal { background: rgba(0, 0, 0, .6); }
  :root[data-theme="dark"] .modal .card { box-shadow: var(--shadow); border-color: #323a46; }
  :root[data-theme="dark"] input:not([type=checkbox]):not([type=radio]), :root[data-theme="dark"] select { background: var(--surface); color: var(--text); border-color: var(--line); }
  :root[data-theme="dark"] input::placeholder { color: #6b7483; }
  :root[data-theme="dark"] .receipt-img img, :root[data-theme="dark"] .last-receipt img { border-radius: 4px; box-shadow: 0 0 0 1px #323a46; }
  :root[data-theme="dark"] a { color: var(--accent); }
  :root[data-theme="dark"] .upd { background: var(--ok-bg); color: var(--ok-fg); border-bottom-color: var(--ok-line); }
  .themebtn { min-width: 40px; }
  * { box-sizing: border-box; }
  input[type=checkbox], input[type=radio] { accent-color: var(--accent-btn); }
  body { margin: 0; font: 14px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); }
  header { display: flex; align-items: center; gap: 16px; padding: 12px 20px; background: var(--header); color: #fff; }
  header h1 { font-size: 17px; margin: 0; font-weight: 600; }
  header .spacer { flex: 1; }
  header small { color: var(--header-muted); }
  .badge { padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; background: var(--badge); }
  .badge.open { background: var(--ok); }
  nav { display: flex; gap: 4px; padding: 0 20px; background: var(--card); border-bottom: 1px solid var(--line); }
  nav button { border: 0; background: none; padding: 12px 14px; font: inherit; cursor: pointer; color: var(--muted); border-bottom: 2px solid transparent; }
  nav button.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
  main { padding: 20px; max-width: 1300px; margin: 0 auto; }
  .tab { display: none; } .tab.active { display: block; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px; }
  .grid { display: grid; grid-template-columns: 1fr 440px; gap: 20px; align-items: start; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  /* вкладка «Товари»: меню розділів зліва */
  .sublayout { display: grid; grid-template-columns: 236px minmax(0, 1fr); gap: 20px; align-items: start; }
  .subnav { position: sticky; top: 12px; display: flex; flex-direction: column; gap: 4px; padding: 8px;
            border: 1px solid var(--line); border-radius: 12px; background: var(--card); }
  .subnav button { display: flex; align-items: center; gap: 10px; width: 100%; padding: 10px 12px; border: 0; border-radius: 8px;
                   background: none; color: var(--text); font: inherit; font-size: 14px; text-align: left; cursor: pointer; }
  .subnav button:hover { background: var(--hover); }
  .subnav button.on { background: var(--accent-btn); color: #fff; font-weight: 600; }
  .subnav .si { width: 20px; text-align: center; font-size: 16px; }
  .subnav .st { flex: 1; white-space: nowrap; }
  .subnav .sc { font-size: 12px; color: var(--muted); background: var(--subtle); border-radius: 10px; padding: 1px 8px; min-width: 22px; text-align: center; }
  .subnav button.on .sc { color: #fff; background: rgba(255,255,255,.22); }
  .subpane { display: none; } .subpane.on { display: block; }
  @media (max-width: 900px) {
    .sublayout { grid-template-columns: 1fr; }
    .subnav { position: static; flex-direction: row; overflow-x: auto; }
    .subnav button { width: auto; white-space: nowrap; }
  }
  input[type=text], input[type=number], input[type=password] { width: 100%; padding: 9px 11px; border: 1px solid var(--line); border-radius: 8px; font: inherit; }
  input:focus { outline: 2px solid var(--accent); outline-offset: -1px; border-color: transparent; }
  button.btn { border: 0; border-radius: 8px; padding: 9px 16px; font: inherit; font-weight: 600; cursor: pointer; background: var(--accent-btn); color: #fff; }
  button.btn:hover { background: var(--accent-btn-2); }
  button.btn.sec { background: var(--sec); color: var(--text); }
  button.btn.sec:hover { background: var(--sec-hover); }
  button.btn.danger { background: var(--bad-btn); }
  button.btn.ok { background: var(--ok-btn); }
  button.btn:disabled { opacity: .5; cursor: wait; }
  .row { display: flex; gap: 8px; align-items: center; }
  .muted { color: var(--muted); }
  .results { margin-top: 12px; max-height: 62vh; overflow: auto; }
  .item { display: flex; justify-content: space-between; gap: 12px; padding: 10px 12px; border-radius: 8px; cursor: pointer; }
  .item:hover { background: var(--info-bg); }
  .item small { display: block; color: var(--muted); }
  .item .r { text-align: right; white-space: nowrap; font-weight: 600; }
  .zero { color: var(--bad) !important; font-weight: 600; }
  .tax { display: inline-block; min-width: 20px; padding: 1px 6px; margin-right: 3px; border-radius: 5px; background: var(--info-bg); color: var(--accent-2); font-size: 12px; font-weight: 700; text-align: center; cursor: help; }
  .tax.none { background: var(--bad-bg); color: var(--bad); }
  .marks { list-style: none; padding: 0; margin: 12px 0 0; max-height: 220px; overflow: auto; }
  .marks li { display: flex; justify-content: space-between; align-items: center; padding: 6px 10px; border: 1px solid var(--line); border-radius: 6px; margin-bottom: 6px; font-family: ui-monospace, Consolas, monospace; }
  .markbtn { margin-top: 4px; border: 1px solid var(--line); background: var(--surface); border-radius: 6px; padding: 2px 8px; font: inherit; font-size: 12px; cursor: pointer; }
  .markbtn.warn { border-color: var(--bad-line); background: var(--bad-bg); color: var(--bad); }
  .markbtn.ok { border-color: var(--ok-line); background: var(--ok-bg); color: var(--ok); }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: left; }
  th { font-size: 12px; text-transform: uppercase; letter-spacing: .03em; color: var(--muted); font-weight: 600; }
  td.num, th.num { text-align: right; white-space: nowrap; }
  tbody tr.click { cursor: pointer; } tbody tr.click:hover { background: var(--hover); }
  .cart td input { width: 80px; padding: 5px 7px; text-align: right; }
  .x { border: 0; background: none; color: var(--muted); cursor: pointer; font-size: 16px; }
  .x:hover { color: var(--bad); }
  .total { display: flex; justify-content: space-between; font-size: 22px; font-weight: 700; margin: 16px 0; }
  .cashbox { border-top: 1px dashed var(--line); padding-top: 10px; margin: -6px 0 14px; }
  .cashbox .line { display: flex; justify-content: space-between; align-items: center; padding: 3px 0; color: var(--muted); }
  .cashbox .line.strong { color: var(--text); font-weight: 700; font-size: 16px; }
  .cashbox input { width: 120px; text-align: right; padding: 5px 7px; }
  .cashbox .bad { color: var(--bad); }
  .form { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; }
  .form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
  .form label.wide { grid-column: 1 / -1; }
  .form label.chk { flex-direction: row; align-items: center; gap: 8px; font-size: 13px; color: var(--text); }
  .form.one { grid-template-columns: 1fr; margin-top: 14px; }
  select { width: 100%; padding: 9px 11px; border: 1px solid var(--line); border-radius: 8px; font: inherit; background: var(--surface); }
  .pw { position: relative; display: block; }
  .pw input { padding-right: 38px; }
  .eye { position: absolute; right: 6px; top: 50%; transform: translateY(-50%); border: 0; background: none; cursor: pointer; font-size: 15px; opacity: .6; }
  .eye:hover { opacity: 1; }
  .status { margin-top: 12px; padding: 9px 12px; border-radius: 8px; font-size: 13px; }
  .status.ok { background: var(--ok-bg); color: var(--ok-fg); }
  .status.bad { background: var(--bad-bg); color: var(--bad-fg); }
  .status.wait { background: var(--info-bg); color: var(--accent-2); }
  header .who { color: var(--header-muted); font-size: 12px; }
  header .who.reg { font-family: ui-monospace, Consolas, monospace; letter-spacing: .02em; }
  header .who.reg:empty { display: none; }
  details.day { border: 1px solid var(--line); border-radius: 8px; margin-bottom: 8px; }
  details.day > summary { padding: 10px 14px; cursor: pointer; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; list-style-position: inside; }
  details.day[open] > summary { border-bottom: 1px solid var(--line); }
  details.day table { margin: 0; } details.day td, details.day th { padding: 7px 14px; }
  .daysum { font-size: 13px; }
  .shifts { padding: 8px 10px; }
  details.shift { border: 1px solid var(--line); border-radius: 6px; margin-bottom: 6px; background: var(--subtle); }
  details.shift > summary { padding: 8px 12px; cursor: pointer; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; font-size: 14px; }
  details.shift[open] > summary { border-bottom: 1px solid var(--line); }
  details.shift table { background: var(--card); }
  .upd { display: flex; justify-content: center; align-items: center; gap: 14px; padding: 8px 20px; background: var(--ok-bg); color: var(--ok-fg); border-bottom: 1px solid var(--ok-line); font-size: 14px; }
  .upd a { color: inherit; }
  .upd-overlay { position: fixed; inset: 0; background: rgba(15,23,42,.55); display: flex; align-items: center; justify-content: center; z-index: 50; }
  .upd-overlay .card { text-align: center; min-width: 320px; }
  .spin { width: 34px; height: 34px; margin: 4px auto 12px; border: 4px solid var(--info-bg); border-top-color: var(--accent); border-radius: 50%; animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .last-receipt { margin-top: 10px; max-height: 460px; overflow: auto; text-align: center; background: var(--subtle); border-radius: 6px; }
  .last-receipt img { max-width: 100%; cursor: zoom-in; display: block; margin: 0 auto; }
  .receipt-img { margin-top: 12px; max-height: 75vh; overflow: auto; text-align: center; background: var(--subtle); border-radius: 8px; padding: 8px; }
  .receipt-img img { max-width: 100%; }
  .termstate { margin-top: 14px; padding: 16px; border-radius: 10px; font-size: 16px; font-weight: 600; text-align: center; background: var(--info-bg); color: var(--accent-2); }
  .termstate.approved { background: var(--ok-bg); color: var(--ok-fg); }
  .termstate.declined, .termstate.conn, .termstate.shift_unknown { background: var(--bad-bg); color: var(--bad-fg); }
  .termstate.unknown, .termstate.fiscal_failed, .termstate.shift_closed { background: var(--surface)beb; color: var(--warn-fg); }
  .termrow { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-top: 16px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; font-size: 13px; }
  button.btn.small { padding: 6px 12px; font-size: 13px; white-space: nowrap; }
  .modal .card.wide { max-width: 640px; }
  label.chk { display: flex; align-items: center; gap: 8px; cursor: pointer; }
  .form label .row input { flex: 1; }
  details summary { cursor: pointer; }
  .goods-edit tbody tr { cursor: pointer; } .goods-edit tbody tr:hover { background: var(--hover); }
  .pend { display: inline-block; margin-left: 6px; padding: 1px 7px; border-radius: 999px; font-size: 11px; font-weight: 600; background: var(--warn-bg); color: var(--warn-fg); vertical-align: middle; }
  .pend.err { background: var(--bad-bg); color: var(--bad-fg); }
  button.btn.sec.attn { background: var(--warn-bg); color: var(--warn-fg); }
  .taxpick { display: flex; flex-wrap: wrap; gap: 8px; }
  .taxpick label { display: flex; align-items: center; gap: 6px; padding: 6px 10px; border: 1px solid var(--line); border-radius: 8px; cursor: pointer; font-size: 13px; }
  .taxpick label:has(input:checked) { border-color: var(--accent); background: var(--info-bg); }
  input[readonly] { background: var(--subtle); color: var(--muted); }
  .pay.three { grid-template-columns: 1fr 1fr 1fr; }
  button.btn.other { background: var(--purple); } button.btn.other:hover { background: var(--purple-2); }
  .seg { display: grid; grid-template-columns: 1fr 1fr; gap: 0; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  .seg label { display: flex; align-items: center; justify-content: center; gap: 6px; padding: 9px; cursor: pointer; font-weight: 600; }
  .seg input { display: none; }
  .seg label:has(input:checked) { background: var(--accent-btn); color: #fff; }
  .codebox { display: flex; gap: 6px; } .codebox input { flex: 1; min-width: 0; }
  .optlist { display: flex; flex-direction: column; border: 1px solid var(--line); border-radius: 10px; overflow: hidden; max-height: 260px; overflow-y: auto; }
  .optlist .opt { display: flex; align-items: center; gap: 10px; padding: 9px 12px; border: 0; border-bottom: 1px solid var(--line);
                  background: var(--surface); color: var(--text); font: inherit; font-size: 14px; text-align: left; cursor: pointer; }
  .optlist .opt:last-child { border-bottom: 0; }
  .optlist .opt:hover { background: var(--hover); }
  .optlist .opt .dot { width: 16px; height: 16px; border-radius: 50%; border: 2px solid var(--line); flex-shrink: 0; }
  .optlist .opt.on { background: var(--hover); font-weight: 600; }
  .optlist .opt.on .dot { border-color: var(--accent); background: radial-gradient(var(--accent) 0 45%, transparent 50%); }
  .optlist .opt .nm { flex: 1; }
  .optlist .opt .del { color: var(--muted); padding: 0 4px; border-radius: 4px; }
  .optlist .opt .del:hover { color: var(--bad); }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .chip { border: 1px solid var(--line); background: var(--surface); border-radius: 999px; padding: 4px 10px; font: inherit; font-size: 12px; cursor: pointer; }
  .chip:hover { border-color: var(--accent); color: var(--accent); }
  .chip .del { margin-left: 6px; color: var(--muted); }
  .pay { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .pay button { padding: 14px; font-size: 15px; }
  .last { margin-top: 14px; padding: 10px 12px; border-radius: 8px; background: var(--ok-bg); color: var(--ok-fg); display: none; }
  pre { background: var(--pre); color: #e5e7eb; padding: 16px; border-radius: 8px; overflow: auto; font: 13px/1.45 ui-monospace, Consolas, monospace; white-space: pre-wrap; }
  .modal { position: fixed; inset: 0; background: rgba(0,0,0,.45); display: none; align-items: center; justify-content: center; padding: 20px; }
  .modal.show { display: flex; }
  .modal .card { max-width: 560px; width: 100%; max-height: 90vh; overflow: auto; }
  #toast { position: fixed; right: 20px; bottom: 20px; padding: 12px 16px; border-radius: 8px; background: var(--toast); color: #fff; opacity: 0; transform: translateY(10px); transition: .2s; max-width: 420px; pointer-events: none; }
  #toast.show { opacity: 1; transform: none; } #toast.err { background: var(--bad); }
  .stats { display: flex; gap: 24px; margin-bottom: 12px; }
  .discrow { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-top: 10px; font-size: 13px; }
  .disc { color: var(--ok); font-size: 12px; } .disc.up { color: var(--warn); }
  s.old { color: var(--muted); font-size: 12px; font-weight: 400; }
  .retlines td input[type=number] { width: 90px; padding: 5px 7px; text-align: right; }
  .retlines tr.off td { color: var(--muted); }
  .kind { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; background: var(--subtle); }
  .kind.ret { background: var(--bad-bg); color: var(--bad-fg); } .kind.in { background: var(--ok-bg); color: var(--ok-fg); } .kind.out { background: var(--warn-bg); color: var(--warn-fg); }
  .cashnow { font-size: 15px; margin-top: 8px; color: var(--text); }
  .kind.pre { background: var(--pre-bg); color: var(--pre-fg); }
  .phonebox { margin: 0 0 12px; }
  .phonebox label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
  .phonebox input.bad, input.bad { border-color: var(--bad); outline-color: var(--bad); }
  .phonebox small { font-size: 12px; }
  .groupbar { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 2px; align-items: center; }
  .groupbar .crumb { font-size: 13px; color: var(--muted); }
  .groupbar .crumb a { color: var(--accent); cursor: pointer; text-decoration: none; }
  .groupbar .chip.on { background: var(--accent-btn); border-color: var(--accent-btn); color: #fff; }
  .groupbar .chip small { color: inherit; opacity: .7; margin-left: 2px; }
  .ghead { margin: 12px 0 2px; padding: 4px 2px; font-size: 12px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .03em; border-bottom: 1px solid var(--line); }
  .ghead:first-child { margin-top: 4px; }
  tr.grouprow td { background: var(--subtle); font-weight: 700; font-size: 13px; color: var(--muted); cursor: default; }
  tr.grouprow td small { font-weight: 400; }
  .ghead { cursor: pointer; user-select: none; } .ghead:hover { color: var(--text); }
  /* плитки: папки-групи і товари */
  .viewbar { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-top: 10px; min-height: 30px; }
  .viewbar .crumbs { font-size: 14px; color: var(--muted); display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
  .viewbar .crumbs a { color: var(--accent); cursor: pointer; text-decoration: none; padding: 2px 4px; border-radius: 4px; }
  .viewbar .crumbs a:hover { background: var(--hover); }
  .viewbar .crumbs b { color: var(--text); padding: 2px 4px; }
  .viewsw { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; flex-shrink: 0; }
  .viewsw button { border: 0; background: var(--surface); color: var(--muted); padding: 5px 10px; cursor: pointer; font: inherit; font-size: 13px; }
  .viewsw button.on { background: var(--accent-btn); color: #fff; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(142px, 1fr)); gap: 10px; margin-top: 10px; }
  .tile { position: relative; display: flex; flex-direction: column; align-items: center; text-align: center; gap: 4px;
          padding: 12px 8px 10px; border: 1px solid var(--line); border-radius: 12px; background: var(--card); cursor: pointer;
          transition: transform .08s, border-color .12s, box-shadow .12s; min-height: 132px; }
  .tile:hover { border-color: var(--accent); box-shadow: 0 4px 14px rgba(37, 99, 235, .14); transform: translateY(-1px); }
  .tile:active { transform: translateY(0); }
  .tile .ico { width: 44px; height: 44px; display: block; }
  .tile .tn { font-weight: 600; font-size: 13px; line-height: 1.25; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; word-break: break-word; }
  .tile .tp { font-weight: 700; font-size: 14px; margin-top: auto; }
  .tile .ts { font-size: 11px; color: var(--muted); }
  .tile .ts.zero { color: var(--bad); }
  .tile .tg { font-size: 11px; color: var(--muted); display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical; overflow: hidden; }
  .tile.folder { background: var(--subtle); }
  .tile.folder .tn { font-size: 14px; }
  .tile.up { border-style: dashed; background: transparent; }
  .tile .dot { position: absolute; top: 8px; right: 8px; width: 8px; height: 8px; border-radius: 50%; background: var(--warn); }
  .tile .dot.err { background: var(--bad); }
  .tile .incart { position: absolute; top: 6px; left: 6px; min-width: 22px; height: 22px; padding: 0 6px; border-radius: 11px; background: var(--ok-btn); color: #fff; font-size: 12px; font-weight: 700; line-height: 22px; }
  .tile.off { opacity: .55; }
  .tile .pic { width: 72px; height: 72px; object-fit: contain; border-radius: 8px; margin: -6px 0 -2px; }
  .tile .fbadge { position: absolute; top: 6px; left: 6px; }
  .tile .fbadge .ico { width: 22px; height: 22px; }
  .tile.drop { border-color: var(--accent); border-style: dashed; background: var(--hover); }
  .ctxmenu { position: fixed; z-index: 1000; min-width: 220px; padding: 5px; border: 1px solid var(--line); border-radius: 10px;
             background: var(--card); box-shadow: 0 10px 30px rgba(0,0,0,.22); display: none; }
  .ctxmenu.show { display: block; }
  .ctxmenu .ch { font-size: 12px; color: var(--muted); padding: 5px 10px 6px; border-bottom: 1px solid var(--line); margin-bottom: 4px;
                 max-width: 260px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ctxmenu button { display: block; width: 100%; text-align: left; border: 0; background: none; color: var(--text); font: inherit; font-size: 14px;
                    padding: 7px 10px; border-radius: 6px; cursor: pointer; }
  .ctxmenu button:hover, .ctxmenu button:focus { background: var(--hover); outline: none; }
  .ctxmenu button.danger { color: var(--bad); }
  .tilesbox { max-height: 62vh; overflow: auto; padding: 2px; }
  .ghead.sub { text-transform: none; letter-spacing: 0; font-size: 13px; border-bottom-style: dashed; margin-top: 6px; }
  .grow-sub td { font-weight: 600 !important; }
  tr.grouprow td { cursor: pointer; user-select: none; } tr.grouprow:hover td { color: var(--text); }
  .tw { display: inline-block; width: 14px; color: var(--muted); }
  .gtree { margin-top: 8px; }
  .gnode { display: flex; align-items: center; gap: 8px; padding: 7px 8px; border-bottom: 1px solid var(--line); }
  .gnode:hover { background: var(--hover); }
  .gnode .gname { flex: 1; min-width: 0; }
  .gnode .gname small { color: var(--muted); margin-left: 6px; }
  .gnode .gacts { display: flex; gap: 4px; opacity: .55; } .gnode:hover .gacts { opacity: 1; }
  .gnode .tog { width: 22px; border: 0; background: none; color: var(--muted); cursor: pointer; font: inherit; padding: 0; }
  .stockbox { margin-top: 14px; padding: 12px; border: 1px solid var(--line); border-radius: 8px; }
  #askModal { z-index: 60; }
  #scanModal .card { max-width: 620px; }
  #scanInput { font-size: 20px; padding: 14px 16px; letter-spacing: .02em; }
  #scanInput.mark { outline: 3px solid var(--warn); }
  .scanhint { margin: 8px 0 0; padding: 10px 12px; border-radius: 8px; font-size: 14px; background: var(--info-bg); color: var(--accent-2); }
  .scanhint.ok { background: var(--ok-bg); color: var(--ok-fg); } .scanhint.bad { background: var(--bad-bg); color: var(--bad-fg); } .scanhint.warn { background: var(--surface)beb; color: var(--warn-fg); }
  .scanlist { margin-top: 12px; max-height: 42vh; overflow: auto; border: 1px solid var(--line); border-radius: 8px; }
  .scanlist table td { padding: 7px 10px; }
  .scanlist tr.scanned td { background: var(--ok-bg); transition: background .6s; }
  .scantotal { display: flex; justify-content: space-between; align-items: baseline; margin: 14px 0 4px; font-size: 22px; font-weight: 700; }
  .scanpay { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-top: 12px; }
  .scanpay button { padding: 13px; font-size: 15px; }
  .qbtn { border: 1px solid var(--line); background: var(--surface); border-radius: 6px; width: 26px; height: 26px; cursor: pointer; font: inherit; line-height: 1; }
  .qbtn:hover { border-color: var(--accent); color: var(--accent); }
  #askModal .card { max-width: 460px; }
  #askText { margin: 10px 0 0; white-space: pre-line; line-height: 1.5; }
  #askText b { white-space: nowrap; }
  .askicon { font-size: 22px; line-height: 1; }
  .chain-receipts { margin: 6px 0 0; padding: 0; list-style: none; font-size: 13px; }
  .chain-receipts li { padding: 2px 0; }
  .prebar { height: 6px; border-radius: 3px; background: var(--sec); overflow: hidden; margin-top: 4px; min-width: 90px; }
  .prebar i { display: block; height: 100%; background: var(--purple); }
  .sumgrid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 12px; }
  .sumgrid div { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
  .sumgrid span { display: block; font-size: 12px; color: var(--muted); }
  .sumgrid b { font-size: 18px; }
  .sumgrid small { display: block; color: var(--muted); font-size: 12px; margin-top: 2px; }
  #psModal h4 { margin: 16px 0 6px; font-size: 13px; text-transform: uppercase; letter-spacing: .03em; color: var(--muted); }
  .stats b { font-size: 18px; display: block; }
</style>
</head>
<body>
<header>
  <h1>Checkbox облік</h1>
  <span id="shiftBadge" class="badge">Зміна: …</span>
  <span id="regInfo" class="who reg"></span>
  <span id="cashierName" class="who"></span>
  <span class="spacer"></span>
  <small>Товари оновлено: <span id="synced">—</span></small>
  <button class="btn sec" id="syncBtn">⟳ Синхронізувати</button>
  <button class="btn sec themebtn" id="themeBtn" title="Тема"></button>
  <button class="btn sec" id="settingsBtn" title="Налаштування підключення">⚙ Налаштування</button>
  <button class="btn sec" id="quitBtn" title="Зупинити касу">⏻ Вийти</button>
</header>
<div id="updateBanner" class="upd" style="display:none"></div>
<div id="updateOverlay" class="upd-overlay" style="display:none"><div class="card"><div class="spin"></div><b id="updateStage">Оновлення…</b>
  <p class="muted" id="updateHint" style="margin:6px 0 0">Не закривайте цю вкладку — вона перезавантажиться сама.</p></div></div>
<nav>
  <button data-tab="pos" class="active">Каса</button>
  <button data-tab="goods">Товари</button>
  <button data-tab="shift">Зміна</button>
  <button data-tab="sales">Журнал</button>
  <button data-tab="prepay">Передплати</button>
</nav>
<main>
  <!-- КАСА -->
  <section id="pos" class="tab active">
    <div class="grid">
      <div class="card">
        <div class="row">
          <input type="text" id="search" placeholder="Штрихкод, код або назва — Enter додає в кошик" autofocus>
          <button class="btn" id="scanBtn" style="white-space:nowrap" title="Сканувати товари один за одним, потім одразу оплата (F2)">▦ Сканувати підряд</button>
        </div>
        <div class="viewbar"><div class="crumbs" id="posCrumbs"></div>
          <div class="viewsw" id="posView"><button data-v="tiles" title="Плитками: групи — папки">▦ Плитки</button><button data-v="list" title="Списком: дерево груп">☰ Список</button></div></div>
        <div class="groupbar" id="posGroups"></div>
        <div class="results" id="results"></div>
        <div class="tilesbox" id="posTiles"><div class="tiles" id="posTilesGrid"></div></div>
      </div>
      <div class="card">
        <h3 style="margin-top:0">Кошик</h3>
        <table class="cart">
          <thead><tr><th>Товар</th><th class="num">К-сть</th><th class="num">Сума</th><th></th></tr></thead>
          <tbody id="cart"></tbody>
        </table>
        <div class="discrow"><button class="markbtn" id="cartDiscBtn">％ Знижка / надбавка на чек</button><span id="cartDiscSum" class="disc"></span></div>
        <div class="total"><span>Разом</span><span id="total">0.00 ₴</span></div>
        <div id="cashBox" class="cashbox">
          <div class="line" id="roundLine"><span>Заокруглення (готівка)</span><span id="roundSum"></span></div>
          <div class="line strong"><span>До сплати готівкою</span><span id="toPayCash">0.00 ₴</span></div>
          <div class="line"><span>Отримано, ₴</span><input type="number" id="received" min="0" step="0.5" placeholder="без решти"></div>
          <div class="line" id="restLine"><span>Решта</span><span id="rest"></span></div>
        </div>
        <div class="phonebox">
          <label>📱 Телефон покупця — копія чека в SMS/Viber (необов'язково)
            <input type="text" id="phone" inputmode="tel" autocomplete="off" placeholder="067 123 45 67"></label>
          <small class="muted" id="phoneHint"></small>
        </div>
        <div class="pay three">
          <button class="btn ok" id="payCash">Готівка</button>
          <button class="btn" id="payCard">Картка</button>
          <button class="btn other" id="payOther">Інше</button>
        </div>
        <div class="pay" style="margin-top:10px">
          <button class="btn sec" id="payPre" title="Частина суми зараз, решта — пізніше (чеки передплати й післяплати)">⏳ Передплата</button>
          <button class="btn sec" id="clearCart">Очистити кошик</button>
        </div>
        <div class="last" id="last"></div>
      </div>
    </div>
  </section>

  <!-- ТОВАРИ -->
  <section id="goods" class="tab">
   <div class="sublayout">
    <aside class="subnav" id="goodsNav">
      <button data-pane="gpGoods"><span class="si">📦</span><span class="st">Товари</span><span class="sc" id="navGoodsN"></span></button>
      <button data-pane="gpGroups"><span class="si">📁</span><span class="st">Групи товарів</span><span class="sc" id="navGroupsN"></span></button>
      <button data-pane="gpTaxes"><span class="si">％</span><span class="st">Податкові ставки</span><span class="sc" id="navTaxesN"></span></button>
      <button data-pane="gpExcise"><span class="si">🏷</span><span class="st">Акцизні марки</span><span class="sc" id="navExciseN"></span></button>
    </aside>
    <div class="subpanes">
    <div class="card subpane" id="gpGoods">
      <div class="stats">
        <div><span class="muted">Товарів</span><b id="stCount">0</b></div>
        <div><span class="muted">Нульовий залишок</span><b id="stZero">0</b></div>
        <div><span class="muted">Облік залишків</span><b id="stLeft">—</b></div>
        <div><span class="muted">Без податку</span><b id="stNoTax">0</b></div>
      </div>
      <div class="row">
        <input type="text" id="goodsFilter" placeholder="Фільтр…">
        <select id="goodsGroup" style="max-width:280px" title="Група товарів"></select>
        <button class="btn" id="goodAddBtn" style="white-space:nowrap">+ Додати товар</button>
      </div>
      <p class="muted" style="font-size:12px;margin:6px 0 0">Клікніть товар, щоб змінити його. Зміни одразу діють у касі, а в Checkbox ідуть під час синхронізації (⟳, відкриття або закриття зміни).</p>
      <div class="viewbar"><div class="crumbs" id="gdCrumbs"></div>
        <div class="viewsw" id="gdView"><button data-v="tiles">▦ Плитки</button><button data-v="list">☰ Таблиця</button></div></div>
      <div class="tiles" id="gdTiles"></div>
      <table style="margin-top:12px" class="goods-edit" id="gdTable">
        <thead><tr><th>Назва</th><th>Код</th><th>Штрихкод</th><th>УКТЗЕД</th><th>Податок</th><th class="num">Ціна, ₴</th><th class="num">Залишок</th></tr></thead>
        <tbody id="goodsTable"></tbody>
      </table>
    </div>
    <div class="card subpane" id="gpGroups">
      <div class="row" style="justify-content:space-between">
        <h3 style="margin:0">Групи товарів</h3>
        <button class="btn" id="grpAdd">+ Група</button>
      </div>
      <p class="muted" style="font-size:12px;margin:6px 0 0">Групи й підгрупи — як у Checkbox. Зміни груп ідуть у Checkbox одразу. Клік по ▸ / ▾ — згорнути чи розгорнути.</p>
      <div class="gtree" id="grpTree"></div>
    </div>
    <div class="card subpane" id="gpTaxes">
      <div class="row" style="justify-content:space-between">
        <h3 style="margin:0">Податкові ставки організації</h3>
        <span class="row" style="gap:10px;flex-wrap:nowrap"><small class="muted">Джерело: <span id="taxSource">—</span></small>
          <button class="btn sec small" id="taxRefresh" title="Завантажити довідник ставок з Checkbox (GET /tax)">⟳ Оновити ставки</button></span>
      </div>
      <table style="margin-top:12px">
        <thead><tr><th>Код</th><th>Літера</th><th>Назва</th><th class="num">Ставка</th><th class="num">Акциз</th><th>Тип</th><th class="num">Товарів</th></tr></thead>
        <tbody id="taxTable"></tbody>
      </table>
    </div>
    <div class="card subpane" id="gpExcise">
      <div class="row" style="justify-content:space-between">
        <h3 style="margin:0">Акцизні марки</h3>
        <small class="muted" id="exciseStat"></small>
      </div>
      <p class="muted" style="font-size:12px;margin:6px 0 0">Марки з чеків продажу й передплати. Продану марку каса не дасть продати вдруге,
        поки її не повернуть чеком повернення. Чеки, пробиті з інших кас, враховуються після «Завантажити з Checkbox» у журналі.</p>
      <div class="row" style="margin-top:10px"><input type="text" id="exciseFilter" placeholder="Марка, товар або ФН чека…"></div>
      <table style="margin-top:12px">
        <thead><tr><th>Марка</th><th>Товар</th><th>Стан</th><th>Чек продажу</th><th>Продано</th><th>Повернуто</th></tr></thead>
        <tbody id="exciseTable"></tbody>
      </table>
    </div>
    </div>
   </div>
  </section>

  <!-- ЗМІНА -->
  <section id="shift" class="tab">
    <div class="card">
      <div id="shiftInfo" class="muted">Завантаження…</div>
      <div id="cashNow" class="cashnow"></div>
      <div class="row" style="margin-top:14px">
        <button class="btn ok" id="openShift">Відкрити зміну</button>
        <button class="btn sec" id="xReport">X-звіт</button>
        <button class="btn danger" id="closeShift">Закрити зміну (Z-звіт)</button>
      </div>
      <p class="muted" id="lastZ" style="margin:12px 0 0;font-size:13px"></p>
    </div>
    <div class="card" style="margin-top:20px">
      <h3 style="margin:0">Службове внесення і винесення готівки</h3>
      <p class="muted" style="margin:6px 0 12px">Розмінні гроші на початку зміни або інкасація. Це не продаж — суми потрапляють у Z-звіт окремо.</p>
      <div class="row">
        <input type="text" id="svcSum" inputmode="decimal" placeholder="Сума, ₴" style="max-width:180px">
        <button class="btn ok" id="svcIn">＋ Внесення</button>
        <button class="btn" id="svcOut" style="background:var(--warn)">− Винесення</button>
      </div>
    </div>
  </section>

  <!-- ПЕРЕДПЛАТИ -->
  <section id="prepay" class="tab">
    <div class="card">
      <p class="muted" style="margin:0 0 12px">Замовлення з передплатою: доплата (ще одна передплата або післяплата, що закриває замовлення) і повернення всього замовлення.
        Нове замовлення — кнопкою «⏳ Передплата» на вкладці «Каса».</p>
      <table>
        <thead><tr><th>Дата</th><th>Замовлення</th><th class="num">Сума, ₴</th><th class="num">Сплачено</th><th class="num">Залишок</th><th>Статус</th><th></th></tr></thead>
        <tbody id="prepayTable"></tbody>
      </table>
    </div>
  </section>

  <!-- ЖУРНАЛ -->
  <section id="sales" class="tab">
    <div class="card">
      <div class="row" style="justify-content:space-between;flex-wrap:wrap">
        <p class="muted" style="margin:0">Чеки за останні 7 днів. Клікніть чек, щоб побачити його. Старші записи й зображення каса видаляє сама.</p>
        <span class="row"><small class="muted" id="fetchedAt"></small>
          <button class="btn sec small" id="fetchReceipts">⟳ Завантажити з Checkbox</button></span>
      </div>
      <div id="salesDays" style="margin-top:12px"></div>
    </div>
    <div class="card" style="margin-top:20px">
      <div class="row" style="justify-content:space-between">
        <h3 style="margin:0">Z-звіти</h3>
        <small class="muted" id="zStat"></small>
      </div>
      <p class="muted" style="font-size:12px;margin:6px 0 0">Звіти зберігаються в касі: закриті з цієї каси — одразу, з Checkbox — за останні 7 днів
        кнопкою «⟳ Завантажити з Checkbox». Клікніть звіт, щоб побачити його.</p>
      <table style="margin-top:12px" class="zrep">
        <thead><tr><th>Дата</th><th>Зміна</th><th>Z №</th><th>ФН</th><th class="num">Продажі</th><th class="num">Повернення</th><th class="num">Чеків</th><th class="num">Готівка в касі</th></tr></thead>
        <tbody id="zTable"></tbody>
      </table>
    </div>
  </section>
</main>

<div class="modal" id="psModal"><div class="card wide">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0" id="psTitle">Замовлення</h3>
    <button class="x" id="psClose">✕</button>
  </div>
  <p class="muted" style="margin:4px 0 0" id="psSub"></p>
  <div class="sumgrid" id="psSums"></div>
  <div class="prebar" style="height:8px;margin-top:10px"><i id="psBar"></i></div>
  <h4>Товари</h4>
  <table><tbody id="psGoods"></tbody></table>
  <h4>Чеки замовлення</h4>
  <table>
    <thead><tr><th>Чек</th><th>Дата</th><th>Оплата</th><th class="num">Сума, ₴</th><th>Фіскальний №</th></tr></thead>
    <tbody id="psReceipts"></tbody>
  </table>
  <details style="margin-top:14px"><summary class="muted" style="font-size:12px">Повна відповідь Checkbox (для підтримки)</summary><pre id="psRaw" style="max-height:260px"></pre></details>
  <div class="pay" style="margin-top:16px">
    <button class="btn sec" id="psOk">Закрити</button>
    <button class="btn other" id="psPay">Доплатити</button>
  </div>
</div></div>
<div class="modal" id="modal"><div class="card">
  <div class="row" style="justify-content:space-between"><h3 style="margin:0" id="modalTitle">Чек</h3>
    <span class="row"><button class="btn danger small" id="receiptReturn" style="display:none">↩ Повернення</button>
      <button class="btn sec small" id="receiptPrint" style="display:none">🖨 Друк</button><button class="x" id="modalClose">✕</button></span></div>
  <div id="modalImgWrap" class="receipt-img" style="display:none"><img id="modalImg" alt="Чек"></div>
  <pre id="modalText"></pre>
</div></div>
<div class="modal" id="cardModal"><div class="card">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0">Оплата карткою · <span id="cardSum"></span></h3>
    <button class="x" id="cardClose">✕</button>
  </div>
  <p class="muted" style="margin:6px 0 14px">Дані ЕПЗ з банківського сліпа терміналу. Усі поля необов'язкові — заповніть, що є.</p>
  <div class="form">
    <label>Маска картки<input type="text" data-card="card_mask" maxlength="19" placeholder="5168 **** **** 1234"></label>
    <label>RRN<input type="text" data-card="rrn" placeholder="123456789012"></label>
    <label>Код авторизації<input type="text" data-card="auth_code" placeholder="A1B2C3"></label>
    <label>№ банківського чека<input type="text" data-card="receipt_no"></label>
    <label>Платіжна система<input type="text" data-card="payment_system" data-remember placeholder="Visa / Mastercard"></label>
    <label>Власник картки<input type="text" data-card="owner_name"></label>
    <label>Банк<input type="text" data-card="bank_name" data-remember placeholder="ПриватБанк"></label>
    <label>Термінал<input type="text" data-card="terminal" data-remember placeholder="ID терміналу"></label>
    <label class="wide">Еквайр і торговець<input type="text" data-card="acquirer_and_seller" data-remember></label>
    <label class="wide chk"><input type="checkbox" data-card="signature_required"> Потрібен підпис власника картки і касира</label>
  </div>
  <p class="muted" style="font-size:12px;margin:10px 0 0">Банк, термінал, еквайр і платіжна система запам'ятовуються для наступних продажів.</p>
  <div class="pay" style="margin-top:16px">
    <button class="btn sec" id="cardSkip">Пропустити</button>
    <button class="btn" id="cardSubmit">Пробити з даними ЕПЗ</button>
  </div>
</div></div>
<div class="modal" id="settingsModal"><div class="card">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0">Налаштування каси</h3>
    <button class="x" id="settingsClose">✕</button>
  </div>
  <div id="settingsStatus" class="status"></div>
  <div class="form one">
    <label>Ключ ліцензії каси<input type="text" id="cfgLicense" autocomplete="off" spellcheck="false"></label>
  </div>
  <p class="muted" style="margin:14px 0 6px">Авторизація касира:</p>
  <div class="seg">
    <label><input type="radio" name="authMode" value="password" checked> Логін і пароль</label>
    <label><input type="radio" name="authMode" value="pin"> Пін-код</label>
  </div>
  <div class="form" id="authPassword" style="margin-top:12px">
    <label>Логін касира<input type="text" id="cfgLogin" autocomplete="off" spellcheck="false"></label>
    <label>Пароль касира
      <span class="pw"><input type="password" id="cfgPassword" autocomplete="new-password"><button type="button" class="eye" data-for="cfgPassword" title="Показати">👁</button></span>
    </label>
  </div>
  <div class="form" id="authPin" style="margin-top:12px;display:none">
    <label>Пін-код касира
      <span class="pw"><input type="password" id="cfgPin" inputmode="numeric" autocomplete="new-password"><button type="button" class="eye" data-for="cfgPin" title="Показати">👁</button></span>
    </label>
  </div>
  <p class="muted" style="font-size:12px;margin:6px 0 0" id="secretHint"></p>
  <div class="form" style="margin-top:14px">
    <label>Заокруглення готівки
      <select id="cfgRounding">
        <option value="ROUND_50">До 50 копійок (ROUND_50)</option>
        <option value="ROUND_10">До 10 копійок (ROUND_10)</option>
        <option value="">Без заокруглення</option>
      </select>
    </label>
  </div>
  <div class="termrow">
    <span>💳 Платіжний термінал: <b id="termSummary">не налаштовано</b></span>
    <button class="btn sec small" id="termCfgOpen">Налаштувати</button>
  </div>
  <p class="muted" style="font-size:12px;margin:12px 0 0">Журнал роботи: <code id="logPath"></code></p>
  <div class="row" style="justify-content:space-between;margin-top:8px;font-size:13px">
    <span id="verInfo" class="muted">Версія…</span>
    <span class="row" style="gap:8px;flex-wrap:nowrap">
      <button class="btn small" id="updNowBtn" style="display:none">⬆ Оновити</button>
      <button class="btn sec small" id="checkUpdBtn">Перевірити оновлення</button>
    </span>
  </div>
  <div class="pay" style="margin-top:18px">
    <button class="btn sec" id="settingsCancel">Скасувати</button>
    <button class="btn" id="settingsSave">Зберегти і підключитися</button>
  </div>
</div></div>
<div class="modal" id="termCfgModal"><div class="card wide">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0">Платіжний термінал (POSSrv)</h3>
    <button class="x" id="termCfgClose">✕</button>
  </div>
  <label class="chk" style="margin-top:12px"><input type="checkbox" id="termEnabled"> Приймати оплату карткою через термінал</label>
  <div class="form one">
    <label>Адреса POSSrv
      <span class="row"><input type="text" id="termUrl" placeholder="http://localhost:9020" spellcheck="false">
      <button class="btn sec small" id="termRefresh">Оновити список</button></span>
    </label>
    <label>Термінал
      <select id="termDevice"><option value="">— натисніть «Оновити список» —</option></select>
    </label>
  </div>
  <div id="termCfgStatus" class="status" style="display:none"></div>
  <details id="termAdd" style="margin-top:14px">
    <summary><b>Додати новий термінал у POSSrv</b></summary>
    <div class="form" style="margin-top:10px">
      <label>Назва<input type="text" id="regName" placeholder="Термінал каси 1"></label>
      <label>Банк<select id="regType"><option value="oschadbank">Ощадбанк (Ingenico)</option><option value="privatbank">ПриватБанк (PAX A930)</option></select></label>
      <label>Підключення<select id="regMode"><option value="rs232">USB / COM-порт</option><option value="tcp">Мережа (Wi-Fi / LAN)</option></select></label>
      <label>ID мерчанта (необов'язково)<input type="text" id="regMerchant"></label>
      <label class="rs232">Номер COM-порту<input type="number" id="regCom" min="1" max="256" placeholder="4"></label>
      <label class="rs232">Швидкість<input type="number" id="regBaud" placeholder="115200"></label>
      <label class="tcp" style="display:none">IP-адреса терміналу<input type="text" id="regHost" placeholder="192.168.0.50"></label>
      <label class="tcp" style="display:none">TCP-порт<input type="number" id="regPort" min="1" max="65535"></label>
    </div>
    <button class="btn sec" id="regSubmit" style="margin-top:10px">Зареєструвати термінал</button>
  </details>
  <div class="pay" style="margin-top:18px">
    <button class="btn sec" id="termCfgCancel">Скасувати</button>
    <button class="btn" id="termCfgSave">Зберегти</button>
  </div>
</div></div>
<div class="modal" id="termModal"><div class="card">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0"><span id="termTitle">Оплата карткою</span> · <span id="termSum"></span></h3>
    <button class="x" id="termX">✕</button>
  </div>
  <p class="muted" style="margin:4px 0 0;font-size:12px">Термінал: <span id="termDevName"></span></p>
  <div id="termState" class="termstate"></div>
  <details style="margin-top:10px"><summary class="muted" style="font-size:12px">Відповідь терміналу</summary><pre id="termRaw" style="max-height:220px"></pre></details>
  <div class="pay" style="margin-top:16px;grid-template-columns:1fr 1fr">
    <button class="btn sec" id="termCancel">Скасувати</button>
    <button class="btn sec" id="termCloseBtn">Закрити</button>
    <button class="btn sec" id="termManual">Оплата пройшла, ввести дані</button>
    <button class="btn" id="termRetry">Спробувати ще раз</button>
    <button class="btn ok" id="termOpenShift" style="grid-column:1/-1">Відкрити зміну і пробити чек</button>
    <button class="btn" id="termFiscalRetry" style="grid-column:1/-1">Пробити чек ще раз</button>
  </div>
</div></div>
<div class="modal" id="goodModal"><div class="card wide">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0" id="goodTitle">Новий товар</h3>
    <button class="x" id="goodClose">✕</button>
  </div>
  <div class="form" style="margin-top:12px">
    <label class="wide">Назва<input type="text" id="gName" maxlength="255"></label>
    <label>Код<span class="codebox"><input type="text" id="gCode" maxlength="64" spellcheck="false">
      <button type="button" class="btn sec small" id="gCodeGen" title="Згенерувати новий унікальний код">↻</button></span></label>
    <label>Ціна, ₴<input type="text" id="gPrice" inputmode="decimal" placeholder="0.00"></label>
    <label>Штрихкод<input type="text" id="gBarcode" inputmode="numeric" spellcheck="false"></label>
    <label>УКТЗЕД<input type="text" id="gUktzed" placeholder="2202 10 00 00" spellcheck="false"></label>
    <label class="wide" id="gGroupWrap">Група<select id="gGroup"></select></label>
    <label class="wide chk"><input type="checkbox" id="gWeight"> Ваговий товар (продається в кілограмах)</label>
  </div>
  <p class="muted" style="margin:12px 0 6px">Податкові ставки:</p>
  <div id="gTaxes" class="taxpick"></div>
  <div class="stockbox">
    <label class="chk"><input type="checkbox" id="gTrack"> <b>Залишковий товар</b> — вести залишок</label>
    <div class="row" id="gStockRow" style="margin-top:8px;align-items:flex-end">
      <label style="display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted);max-width:200px">
        <span>Залишок, <span id="gStockUnit">шт</span></span><input type="text" id="gStock" inputmode="decimal" placeholder="0"></label>
      <span class="muted" style="font-size:12px;padding-bottom:10px" id="gStockNow"></span>
    </div>
    <p class="muted" style="font-size:12px;margin:6px 0 0">Збільшення залишку піде в Checkbox поставкою (імпорт з автопоставкою) під час синхронізації; у касі діє одразу.
      Зменшити залишок можна лише списанням у кабінеті Checkbox.</p>
  </div>
  <p class="muted" style="font-size:12px;margin:10px 0 0" id="gNote"></p>
  <div id="goodStatus" class="status" style="display:none"></div>
  <button class="btn sec small" id="goodDiscard" style="margin-top:12px;display:none">↶ Скасувати невідправлені зміни</button>
  <div class="pay" style="margin-top:16px">
    <button class="btn sec" id="goodCancel">Закрити</button>
    <button class="btn" id="goodSave">Зберегти</button>
  </div>
</div></div>
<div class="modal" id="otherModal"><div class="card">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0">Інша оплата · <span id="otherSum"></span></h3>
    <button class="x" id="otherClose">✕</button>
  </div>
  <p class="muted" style="margin:6px 0 10px">Як ця оплата піде у фіскальний чек:</p>
  <div class="seg">
    <label><input type="radio" name="otherType" value="CASHLESS" checked> Безготівка</label>
    <label><input type="radio" name="otherType" value="CASH"> Готівка</label>
  </div>
  <p class="muted" style="margin:14px 0 6px">Назва оплати в чеку:</p>
  <div class="optlist" id="otherChips" role="listbox"></div>
  <input type="text" id="otherLabel" maxlength="100" placeholder="Або введіть свою назву…" style="margin-top:8px">
  <div class="pay" style="margin-top:16px">
    <button class="btn sec" id="otherCancel">Скасувати</button>
    <button class="btn other" id="otherSubmit">Пробити чек</button>
  </div>
</div></div>
<div class="modal" id="exciseModal"><div class="card">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0">Акцизні марки</h3>
    <button class="x" id="exciseClose">✕</button>
  </div>
  <p style="margin:6px 0 2px"><b id="exciseName"></b></p>
  <p class="muted" style="margin:0 0 12px">Відскануйте або введіть марку (10 символів) і натисніть «+» або Enter — по одній на кожну одиницю.
    Додано: <b id="exciseCount"></b></p>
  <div class="row" style="flex-wrap:nowrap">
    <input type="text" id="exciseInput" placeholder="Акцизна марка — 10 символів" autocomplete="off" maxlength="64" style="flex:1">
    <button class="btn" id="exciseAdd" title="Додати марку" style="min-width:52px;font-size:20px;line-height:1">+</button>
  </div>
  <div class="muted" id="exciseLen" style="font-size:12px;margin-top:4px;min-height:16px"></div>
  <ul class="marks" id="exciseList"></ul>
  <div class="pay" style="margin-top:14px">
    <button class="btn sec" id="exciseNone">Без марок</button>
    <button class="btn" id="exciseDone">Готово</button>
  </div>
</div></div>
<div class="modal" id="discModal"><div class="card">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0" id="discTitle">Знижка</h3>
    <button class="x" id="discClose">✕</button>
  </div>
  <p class="muted" style="margin:4px 0 12px" id="discFor"></p>
  <div class="seg">
    <label><input type="radio" name="discType" value="DISCOUNT" checked> Знижка</label>
    <label><input type="radio" name="discType" value="EXTRA_CHARGE"> Надбавка</label>
  </div>
  <div class="seg" style="margin-top:8px">
    <label><input type="radio" name="discMode" value="PERCENT" checked> У відсотках, %</label>
    <label><input type="radio" name="discMode" value="VALUE"> Сумою, ₴</label>
  </div>
  <div class="form" style="margin-top:12px">
    <label>Значення<input type="text" id="discValue" inputmode="decimal" placeholder="10"></label>
    <label>Назва в чеку (необов'язково)<input type="text" id="discName" maxlength="100" placeholder="Напр. Знижка постійного клієнта"></label>
  </div>
  <div id="discPreview" class="status wait" style="display:none"></div>
  <p class="muted" style="font-size:12px;margin:10px 0 0">У чек знижка йде сумою в гривнях — відсоток каса перераховує сама, як радить Checkbox.</p>
  <div class="pay" style="margin-top:16px">
    <button class="btn sec" id="discRemove">Прибрати</button>
    <button class="btn" id="discApply">Застосувати</button>
  </div>
</div></div>
<div class="modal" id="retModal"><div class="card wide">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0">Повернення · чек <span id="retFn"></span></h3>
    <button class="x" id="retClose">✕</button>
  </div>
  <p class="muted" style="margin:4px 0 10px" id="retInfo"></p>
  <table class="retlines">
    <thead><tr><th></th><th>Товар</th><th class="num">Продано</th><th class="num">Повернути</th><th class="num">Сума, ₴</th></tr></thead>
    <tbody id="retLines"></tbody>
  </table>
  <div class="total"><span>До повернення</span><span id="retTotal">0.00 ₴</span></div>
  <p class="muted" style="margin:-10px 0 12px;font-size:13px" id="retRound"></p>
  <p class="muted" style="margin:0 0 6px">Як повертаємо гроші:</p>
  <div class="seg">
    <label><input type="radio" name="retType" value="CASH" checked> Готівкою</label>
    <label><input type="radio" name="retType" value="CASHLESS"> На картку / безготівка</label>
  </div>
  <div id="retCardBox" class="status wait" style="display:none"></div>
  <div class="form one" style="margin-top:12px">
    <label>📱 Телефон покупця — копія чека повернення в SMS/Viber (необов'язково)<input type="text" id="retPhone" inputmode="tel" autocomplete="off" placeholder="067 123 45 67"></label>
  </div>
  <div class="pay" style="margin-top:16px">
    <button class="btn sec" id="retCancel">Скасувати</button>
    <button class="btn danger" id="retSubmit">Пробити повернення</button>
  </div>
</div></div>
<div class="modal" id="preModal"><div class="card">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0">Передплата · замовлення на <span id="preTotal"></span></h3>
    <button class="x" id="preClose">✕</button>
  </div>
  <p class="muted" style="margin:4px 0 12px">У чек передплати підуть усі товари кошика, а оплачено буде лише внесок. Решту покупець доплатить пізніше — на вкладці «Передплати».</p>
  <div class="form">
    <label>Внесок зараз, ₴<input type="text" id="preAmount" inputmode="decimal" placeholder="напр. 200"></label>
    <label>Замовлення / покупець (для себе)<input type="text" id="preNote" maxlength="120" placeholder="Напр. Іваненко, тел. 067…"></label>
  </div>
  <p style="margin:10px 0 0" id="preLeft" class="muted"></p>
  <p class="muted" style="margin:12px 0 6px">Чим платить:</p>
  <div class="seg">
    <label><input type="radio" name="preType" value="CASH" checked> Готівка</label>
    <label><input type="radio" name="preType" value="CASHLESS"> Картка / безготівка</label>
  </div>
  <div class="pay" style="margin-top:16px">
    <button class="btn sec" id="preCancel">Скасувати</button>
    <button class="btn other" id="preSubmit">Пробити передплату</button>
  </div>
</div></div>
<div class="modal" id="postModal"><div class="card">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0" id="postTitle">Доплата</h3>
    <button class="x" id="postClose">✕</button>
  </div>
  <p class="muted" style="margin:4px 0 12px" id="postInfo"></p>
  <div class="form">
    <label>Сума, ₴<input type="text" id="postAmount" inputmode="decimal"></label>
    <label id="postRecvWrap">Отримано готівкою, ₴<input type="text" id="postReceived" inputmode="decimal" placeholder="без решти"></label>
    <label class="wide">📱 Телефон покупця — копія чека в SMS/Viber (необов'язково)<input type="text" id="postPhone" inputmode="tel" autocomplete="off" placeholder="067 123 45 67"></label>
  </div>
  <div id="postCalc" class="status wait"></div>
  <p class="muted" style="margin:12px 0 6px">Чим платить:</p>
  <div class="seg">
    <label><input type="radio" name="postType" value="CASH" checked> Готівка</label>
    <label><input type="radio" name="postType" value="CASHLESS"> Картка / безготівка</label>
  </div>
  <div class="pay" style="margin-top:16px">
    <button class="btn sec" id="postCancel">Скасувати</button>
    <button class="btn other" id="postSubmit">Пробити післяплату</button>
  </div>
</div></div>
<div class="modal" id="closeModal"><div class="card">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0">🧾 Закриття зміни</h3>
    <button class="x" id="clsX">✕</button>
  </div>
  <p style="margin:10px 0 4px">У касі <b id="clsCash"></b> готівки. Винести її перед закриттям зміни (інкасація)?</p>
  <p class="muted" style="margin:0 0 10px;font-size:13px">Буде пробито службовий чек винесення, потім — Z-звіт.</p>
  <div class="form one" style="margin-top:0">
    <label>Сума винесення, ₴<input type="text" id="clsSum" inputmode="decimal"></label>
  </div>
  <div class="row" style="gap:6px;margin-top:6px">
    <button class="chip" id="clsAll">Уся готівка</button>
    <span class="muted" style="font-size:12px" id="clsLeft"></span>
  </div>
  <p class="muted" style="margin:10px 0 0;font-size:13px" id="clsPending"></p>
  <div class="pay three" style="margin-top:16px">
    <button class="btn sec" id="clsCancel">Скасувати</button>
    <button class="btn sec" id="clsNoOut">Закрити без винесення</button>
    <button class="btn danger" id="clsOut">Винести й закрити</button>
  </div>
</div></div>
<div class="ctxmenu" id="ctxMenu" role="menu"></div>
<input type="file" id="imgFile" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp" style="display:none">
<div class="modal" id="grpModal"><div class="card">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0" id="grpTitle">Група</h3>
    <button class="x" id="grpX">✕</button>
  </div>
  <div class="form one" style="margin-top:12px">
    <label>Назва<input type="text" id="grpName" maxlength="100"></label>
    <label>Опис (необов'язково)<input type="text" id="grpDesc" maxlength="250"></label>
    <label>Входить у групу<select id="grpParent"></select></label>
  </div>
  <div class="pay" style="margin-top:16px">
    <button class="btn sec" id="grpCancel">Скасувати</button>
    <button class="btn" id="grpSave">Зберегти</button>
  </div>
</div></div>
<div class="modal" id="grpMoveModal"><div class="card">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0">Перенести товари</h3>
    <button class="x" id="gmX">✕</button>
  </div>
  <p style="margin:10px 0" id="gmText"></p>
  <div class="form one"><label>У групу<select id="gmTo"></select></label></div>
  <div class="pay" style="margin-top:16px">
    <button class="btn sec" id="gmCancel">Скасувати</button>
    <button class="btn" id="gmOk">Перенести</button>
  </div>
</div></div>
<div class="modal" id="askModal" role="dialog" aria-modal="true"><div class="card">
  <div class="row" style="gap:10px"><span class="askicon" id="askIcon">❓</span><h3 style="margin:0" id="askTitle">Підтвердження</h3></div>
  <p id="askText"></p>
  <div class="pay" style="margin-top:18px">
    <button class="btn sec" id="askNo">Скасувати</button>
    <button class="btn" id="askYes">Так</button>
  </div>
</div></div>
<div class="modal" id="scanModal"><div class="card">
  <div class="row" style="justify-content:space-between">
    <h3 style="margin:0">▦ Сканування товарів</h3>
    <span class="row"><label class="chk" style="font-size:12px;color:var(--muted)"><input type="checkbox" id="scanSound" checked> звук</label>
      <button class="x" id="scanX" title="Готово">✕</button></span>
  </div>
  <p class="muted" style="margin:4px 0 10px;font-size:13px">Скануйте товари один за одним — кожен скан додається в кошик. Можна ввести штрихкод чи код вручну і натиснути Enter.</p>
  <input type="text" id="scanInput" autocomplete="off" spellcheck="false" placeholder="Скануйте штрихкод…">
  <div class="row" id="scanWeightRow" style="display:none;margin-top:8px">
    <input type="text" id="scanWeight" inputmode="decimal" placeholder="Вага, кг (напр. 0.456)" style="max-width:220px">
    <button class="btn sec small" id="scanWeightOk">Додати</button>
    <button class="btn sec small" id="scanWeightCancel">Не додавати</button>
  </div>
  <div class="scanhint" id="scanHint">Готово до сканування</div>
  <div class="scanlist"><table><tbody id="scanList"></tbody></table></div>
  <div class="row" style="justify-content:space-between;margin-top:8px">
    <button class="btn sec small" id="scanUndo">↶ Прибрати останнє</button>
    <span class="muted" style="font-size:13px" id="scanCount"></span>
  </div>
  <div class="scantotal"><span>Разом</span><span id="scanTotal">0.00 ₴</span></div>
  <div class="scanpay">
    <button class="btn sec" id="scanDone">✓ Готово</button>
    <button class="btn ok" id="scanCash">Готівка</button>
    <button class="btn" id="scanCard">Картка</button>
  </div>
</div></div>
<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
let images = {};        // власні картинки плиток: {"good:<код>"|"group:<id>": версія}
let goods = [], cart = [], useLeftovers = false, roundingMode = null, cartTotal = 0, taxes = [];
let cartDisc = null;
// групи товарів: groupList — [{id, name, parent_id, path}], posGroup — обрана група в касі (null — усі)
let groupList = [], groupById = {}, groupChildren = {}, posGroup = null;
const NO_GROUP = '__none__';
// згорнуті групи (ключ — повний шлях групи), окремо — у дереві груп
function loadSet(key) { try { return new Set(JSON.parse(localStorage.getItem(key) || '[]')); } catch (e) { return new Set(); } }
function saveSet(key, set) { try { localStorage.setItem(key, JSON.stringify([...set])); } catch (e) {} }
const collapsed = loadSet('kasa_groups_collapsed_ids'), treeCollapsed = loadSet('kasa_tree_collapsed');
const fullKey = g => g.group_path && g.group_path.length ? g.group_path.join(' / ') : 'Без групи';
function toggleCollapsed(key) { collapsed.has(key) ? collapsed.delete(key) : collapsed.add(key); saveSet('kasa_groups_collapsed_ids', collapsed); }   // знижка/надбавка на весь чек: { type, mode, value, name }

const taxBadges = codes => {
  if (!codes || !codes.length) return '<span class="tax none" title="Товар без податкової ставки — чек буде без податку">—</span>';
  return codes.map(c => {
    const t = taxes.find(x => x.code === c);
    return `<span class="tax" title="${esc(t ? `${t.label} (код ${c})` : 'Код ' + c + ' — немає в довіднику')}">${esc(t ? (t.symbol || c) : c)}</span>`;
  }).join('');
};
// Товар з акцизною ставкою (окремий акциз або «ПДВ + Акцизний податок»)
const isExciseTax = t => !!(t.extra_rate || /акциз/i.test(t.label || ''));
const isExciseGood = g => (g.tax_codes || []).some(c => { const t = taxes.find(x => x.code === c); return t && isExciseTax(t); });

function renderTaxes(source) {
  $('#taxSource').textContent = source || '—';
  $('#taxTable').innerHTML = taxes.map(t => {
    const used = goods.filter(g => (g.tax_codes || []).includes(t.code)).length;
    const kind = t.no_vat ? 'без ПДВ' : (t.included ? 'включено в ціну' : 'зверху');
    return `<tr><td>${t.code}</td><td><span class="tax">${esc(t.symbol)}</span></td><td>${esc(t.label)}</td>
      <td class="num">${t.rate ?? 0}%</td><td class="num">${t.extra_rate ? t.extra_rate + '%' : '—'}</td>
      <td>${kind}</td><td class="num">${used || '<span class="muted">0</span>'}</td></tr>`;
  }).join('') || '<tr><td colspan="7" class="muted">Ставок немає — натисніть «Синхронізувати»</td></tr>';
}

// Те саме правило, що й round_cash() у checkbox_app.py
function roundCash(total, mode) {
  if (mode === 'ROUND_50') return total - total % 100 + 50 * Math.floor((total % 100 + 25) / 50);
  if (mode === 'ROUND_10') return total - total % 10 + 10 * Math.floor((total % 10 + 5) / 10);
  return total;
}

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const uah = k => k == null ? '—' : (k / 100).toFixed(2);
const qtyStr = (t, w) => t == null ? '—' : w ? (t / 1000).toFixed(3) + ' кг' : (t / 1000) + ' шт';

async function api(path, body) {
  const opts = body === undefined ? {} : { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  const d = await r.json();
  if (!r.ok) { const err = new Error(d.error || r.statusText); err.code = d.code; throw err; }
  return d;
}
// Відкриває зміну і оновлює шапку; повертає true, якщо вдалося
async function openShiftNow() {
  try {
    const r = await api('/api/shift/open', {});
    await loadShift(); loadGoods().catch(() => {});
    toast('Зміну відкрито' + syncSummary(r.sync), !!(r.sync && (r.sync.sync_error || r.sync.push_error)));
    return true;
  } catch (e) { toast(e.message, true); return false; }
}
async function shiftIsOpen() {
  try { return !!(await api('/api/shift')).shift; } catch (e) { return null; }   // null — невідомо
}
let toastTimer;
function toast(msg, err) {
  const t = $('#toast'); t.textContent = msg; t.className = 'show' + (err ? ' err' : '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.className = '', err ? 6000 : 3000);
}
// Підтвердження у вікні каси замість системного confirm(): await ask(текст, { title, ok, cancel, danger, icon })
let askResolve = null;
function ask(text, o = {}) {
  if (askResolve) askResolve(false);          // попереднє питання, якщо ще відкрите, — «ні»
  $('#askTitle').textContent = o.title || 'Підтвердження';
  $('#askIcon').textContent = o.icon || (o.danger ? '⚠️' : '❓');
  $('#askText').textContent = text;
  $('#askYes').textContent = o.ok || 'Так';
  $('#askNo').textContent = o.cancel || 'Скасувати';
  $('#askYes').className = 'btn' + (o.danger ? ' danger' : '');
  $('#askModal').classList.add('show');
  setTimeout(() => (o.danger ? $('#askNo') : $('#askYes')).focus(), 0);   // небезпечне — не підтверджується випадковим Enter
  return new Promise(res => { askResolve = res; });
}
function askDone(v) {
  $('#askModal').classList.remove('show');
  const r = askResolve; askResolve = null;
  if (r) r(v);
}
$('#askYes').onclick = () => askDone(true);
$('#askNo').onclick = () => askDone(false);
$('#askModal').onclick = e => { if (e.target.id === 'askModal') askDone(false); };
$('#askModal').addEventListener('keydown', e => {
  e.stopPropagation();                        // Enter/Escape не мають дійти до вікна під питанням
  if (e.key === 'Escape') askDone(false);
  if (e.key === 'Enter') { e.preventDefault(); askDone(document.activeElement !== $('#askNo')); }
});
const askOpenShift = what => ask(`Зміна закрита. Відкрити зміну і ${what}?`, { title: 'Зміна закрита', ok: 'Відкрити зміну', icon: '🕘' });
async function busy(btn, fn) {   // true — успіх, false — помилка (вже показана)
  btn.disabled = true;
  try { await fn(); return true; } catch (e) { toast(e.message, true); return false; } finally { btn.disabled = false; }
}
function showText(text) {
  $('#modalImgWrap').style.display = 'none'; $('#receiptPrint').style.display = 'none';
  $('#modalText').style.display = ''; $('#modalText').textContent = text; $('#modal').classList.add('show');
}
const pngUrl = id => '/api/receipt_png?id=' + encodeURIComponent(id);
const reportPngUrl = id => '/api/report_png?id=' + encodeURIComponent(id);
let shownDoc = null;    // що відкрито у вікні: { id, report, returnable }
// PNG чека чи звіту у вікні; якщо зображення недоступне — текстовий варіант
function showReceipt(id, opts = {}) {
  const img = $('#modalImg'), url = opts.report ? reportPngUrl : pngUrl;
  shownDoc = { id, ...opts };
  let tries = 0;
  $('#modalTitle').textContent = opts.title || 'Чек';
  $('#receiptReturn').style.display = opts.returnable ? '' : 'none';
  img.onload = () => { $('#modalText').style.display = 'none'; $('#modalImgWrap').style.display = ''; $('#receiptPrint').style.display = ''; };
  img.onerror = async () => {
    if (tries++ < (opts.report ? 3 : 1)) return setTimeout(() => { img.src = url(id) + '&r=' + tries; }, 1500);   // Checkbox міг ще готувати зображення
    try {
      const t = await api((opts.report ? '/api/report_text?id=' : '/api/receipt_text?id=') + encodeURIComponent(id));
      $('#modalImgWrap').style.display = 'none'; $('#modalText').style.display = ''; $('#modalText').textContent = t.text;
    } catch (err) { toast(err.message, true); $('#modal').classList.remove('show'); }
  };
  $('#modalText').style.display = ''; $('#modalText').textContent = opts.report ? 'Формую звіт…' : 'Завантажую чек…';
  $('#modalImgWrap').style.display = 'none'; $('#receiptPrint').style.display = 'none';
  $('#modal').classList.add('show');
  img.src = url(id);
}
$('#receiptPrint') && ($('#receiptPrint').onclick = () => {
  const w = window.open('', '_blank', 'width=420,height=700');
  if (!w) return toast('Браузер заблокував вікно друку — дозвольте спливаючі вікна для каси', true);
  w.document.write(`<!DOCTYPE html><title>${esc($('#modalTitle').textContent)}</title><style>body{margin:0;text-align:center}img{max-width:100%}</style>
    <img src="${$('#modalImg').src}" onload="window.focus();window.print();setTimeout(()=>window.close(),300)">`);
  w.document.close();
});
$('#receiptReturn').onclick = () => { const id = shownDoc && shownDoc.id; $('#modal').classList.remove('show'); if (id) openReturn(id); };
$('#modalClose').onclick = () => $('#modal').classList.remove('show');
$('#modal').onclick = e => { if (e.target.id === 'modal') $('#modal').classList.remove('show'); };

// --- вкладки ---
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button, .tab').forEach(x => x.classList.remove('active'));
  b.classList.add('active'); $('#' + b.dataset.tab).classList.add('active');
  if (b.dataset.tab === 'sales') loadSales();
  if (b.dataset.tab === 'shift') loadShift(true);
  if (b.dataset.tab === 'prepay') loadPrepayments();
  if (b.dataset.tab === 'pos') $('#search').focus();
});

// --- товари ---
async function loadGoods() {
  const d = await api('/api/goods');
  goods = d.goods; images = d.images || {}; window.exciseTotal = d.excise_total; useLeftovers = !!d.use_leftovers; roundingMode = d.cash_rounding; taxes = d.taxes || [];
  setGroups(d.groups || []);
  renderPending(d.pending);
  renderTaxes(d.taxes_source);
  $('#synced').textContent = d.synced_at ? d.synced_at.replace('T', ' ') : '—';
  renderSearch(); renderGoodsTable(); renderCart(); renderGroupTree(); renderGoodsNav();
}
const match = (g, q) => {
  q = q.trim().toLowerCase();
  return !q || g.name.toLowerCase().includes(q) || g.code.trim().toLowerCase().includes(q) || (g.barcode || '').includes(q);
};
// --- групи й підгрупи ---
function setGroups(list) {
  groupList = list; groupById = {}; groupChildren = {};
  list.forEach(g => { groupById[g.id] = g; });
  list.forEach(g => { const p = g.parent_id && groupById[g.parent_id] ? g.parent_id : ''; (groupChildren[p] = groupChildren[p] || []).push(g); });
  if (posGroup && posGroup !== NO_GROUP && !groupById[posGroup]) posGroup = null;
  const sel = $('#goodsGroup'), cur = sel.value;
  sel.innerHTML = '<option value="">Усі групи</option>' + list.map(g =>
    `<option value="${esc(g.id)}">${'\u00a0\u00a0'.repeat(g.path.length - 1)}${esc(g.path[g.path.length - 1])}</option>`).join('') +
    (goods.some(g => !g.group_id) && list.length ? `<option value="${NO_GROUP}">Без групи</option>` : '');
  sel.value = [...sel.options].some(o => o.value === cur) ? cur : '';
  sel.style.display = list.length ? '' : 'none';
}
function inGroup(g, gid) {            // товар у групі gid або її підгрупах
  if (!gid) return true;
  if (gid === NO_GROUP) return !g.group_id || !groupById[g.group_id];
  for (let x = g.group_id, n = 0; x && n < 20; x = (groupById[x] || {}).parent_id, n++) if (x === gid) return true;
  return false;
}
const groupKey = g => (g.group_path && g.group_path.length ? '0' + g.group_path.join('\u0001') : '1').toLowerCase();
const sortByGroup = list => list.slice().sort((a, b) => groupKey(a).localeCompare(groupKey(b), 'uk') || a.name.localeCompare(b.name, 'uk'));
const groupTitle = (g, from) => (g.group_path && g.group_path.length ? g.group_path.slice(from).join(' / ') : 'Без групи') || '';
function renderPosGroups() {
  const bar = $('#posGroups');
  if (!groupList.length) { bar.innerHTML = ''; bar.style.display = 'none'; return; }
  bar.style.display = '';
  const searching = !!$('#search').value.trim();
  const count = gid => goods.filter(g => inGroup(g, gid)).length;
  const chain = [];
  for (let x = posGroup !== NO_GROUP ? posGroup : null; x && groupById[x]; x = groupById[x].parent_id) chain.unshift(groupById[x]);
  const crumbs = `<span class="crumb"><a data-grp="">Усі товари</a>${chain.map(g => ` › <a data-grp="${esc(g.id)}">${esc(g.name || g.path[g.path.length - 1])}</a>`).join('')}${posGroup === NO_GROUP ? ' › Без групи' : ''}</span>`;
  const level = posGroup === NO_GROUP ? [] : (groupChildren[posGroup || ''] || []).filter(g => count(g.id));
  const chips = level.map(g => `<button class="chip" data-grp="${esc(g.id)}">${esc(g.path[g.path.length - 1])}<small>${count(g.id)}</small></button>`).join('');
  const none = !posGroup && goods.some(g => !g.group_id || !groupById[g.group_id]) ? `<button class="chip" data-grp="${NO_GROUP}">Без групи<small>${count(NO_GROUP)}</small></button>` : '';
  bar.innerHTML = (posGroup ? crumbs : '') + chips + none + (searching && posGroup ? '<span class="crumb">· пошук по всіх групах</span>' : '');
}
$('#posGroups').onclick = e => {
  const a = e.target.closest('[data-grp]'); if (!a) return;
  posGroup = a.dataset.grp || null; renderSearch(); $('#search').focus();
};
// Дерево «група → підгрупи → товари» для списку товарів. rootId — показувати від цієї групи
// (її власні товари — без заголовка); open — розгорнути все (пошук/фільтр); head/item — розмітка.
const byName = (a, b) => (a.name || '').localeCompare(b.name || '', 'uk');
function groupTree(list, rootId, open, head, item) {
  const direct = {};
  list.forEach(g => { const k = g.group_id && groupById[g.group_id] ? g.group_id : NO_GROUP; (direct[k] = direct[k] || []).push(g); });
  const total = {};
  const count = id => total[id] != null ? total[id]
    : (total[id] = (direct[id] || []).length + (groupChildren[id] || []).reduce((a, c) => a + count(c.id), 0));
  const node = (grp, depth) => {
    const n = count(grp.id);
    if (!n) return '';
    const shut = !open && collapsed.has(grp.id);
    let h = head(grp.id, grp.path[grp.path.length - 1], depth, n, shut);
    if (shut) return h;
    h += (direct[grp.id] || []).slice().sort(byName).map(g => item(g, depth + 1)).join('');
    return h + (groupChildren[grp.id] || []).slice().sort(byName).map(c => node(c, depth + 1)).join('');
  };
  if (!groupList.length) return list.slice().sort(byName).map(g => item(g, 0)).join('');
  if (rootId === NO_GROUP) return (direct[NO_GROUP] || []).slice().sort(byName).map(g => item(g, 0)).join('');
  let html = rootId ? (direct[rootId] || []).slice().sort(byName).map(g => item(g, 0)).join('') : '';
  html += (groupChildren[rootId || ''] || []).slice().sort(byName).map(c => node(c, 0)).join('');
  if (!rootId && (direct[NO_GROUP] || []).length) {
    const shut = !open && collapsed.has(NO_GROUP);
    html += head(NO_GROUP, 'Без групи', 0, direct[NO_GROUP].length, shut);
    if (!shut) html += direct[NO_GROUP].slice().sort(byName).map(g => item(g, 1)).join('');
  }
  return html;
}
function renderSearch() {
  const tiles = posViewMode === 'tiles' && groupList.length;
  setViewButtons($('#posView'), tiles ? 'tiles' : 'list');
  $('#posView').style.display = groupList.length ? '' : 'none';
  $('#results').style.display = tiles ? 'none' : '';
  $('#posTiles').style.display = tiles ? '' : 'none';
  $('#posGroups').style.display = tiles ? 'none' : '';
  if (tiles) {
    const q = $('#search').value;
    $('#posCrumbs').innerHTML = q.trim() ? '<span>🔎 Пошук по всіх групах</span>' : crumbsHtml(posGroup, 'Усі товари');
    const inCart = g => { const c = cart.find(x => x.id === g.id); return c ? qtyStr(c.qty, g.is_weight).replace(/ (шт|кг)$/, '') : 0; };
    $('#posTilesGrid').innerHTML = tilesHtml(posGroup, q, { cartQty: inCart })
      || '<p class="muted">Нічого не знайдено. Можливо, потрібна синхронізація.</p>';
    return;
  }
  $('#posCrumbs').innerHTML = '';
  renderPosGroups();
  const q = $('#search').value, searching = !!q.trim();
  // пошук — по всіх товарах; без пошуку — товари обраної групи з підгрупами, деревом
  const list = goods.filter(g => searching ? match(g, q) : inGroup(g, posGroup)).slice(0, 300);
  const pad = d => d ? ` style="margin-left:${d * 16}px"` : '';
  const head = (id, name, depth, n, shut) => `<div class="ghead${depth ? ' sub' : ''}" data-gkey="${esc(id)}"${pad(depth)}>` +
    `<span class="tw">${shut ? '▸' : '▾'}</span>${esc(name)} <small style="font-weight:400">· ${n}</small></div>`;
  const item = (g, depth) => `
    <div class="item" data-id="${esc(g.id)}"${pad(depth)}>
      <div><b>${esc(g.name)}</b>${pendingBadge(g)}<small>${esc(g.barcode || g.code)}</small></div>
      <div class="r">${uah(g.price)} ₴<small class="${g.count === 0 ? 'zero' : ''}">${qtyStr(g.count, g.is_weight)}</small></div>
    </div>`;
  $('#results').innerHTML = groupTree(list, searching ? null : posGroup, searching, head, item)
    || '<p class="muted">Нічого не знайдено. Можливо, потрібна синхронізація.</p>';
}
function pendingBadge(g) {
  if (!g.pending) return '';
  if (g.pending_error) return ` <span class="pend err" title="Не вдалося відправити в Checkbox: ${esc(g.pending_error)}">⚠ не відправлено</span>`;
  return g.pending === 'new'
    ? ' <span class="pend" title="Новий товар; у Checkbox з\'явиться під час синхронізації">● новий, не в Checkbox</span>'
    : ' <span class="pend" title="Зміни ще не відправлені в Checkbox">● змінено локально</span>';
}
function renderGoodsTable() {
  const tiles = gdViewMode === 'tiles' && groupList.length;
  setViewButtons($('#gdView'), tiles ? 'tiles' : 'list');
  $('#gdView').style.display = groupList.length ? '' : 'none';
  $('#gdTiles').style.display = tiles ? '' : 'none';
  $('#gdTable').style.display = tiles ? 'none' : '';
  $('#goodsGroup').style.display = tiles || !groupList.length ? 'none' : '';
  if (tiles) {
    if (gdFolder && gdFolder !== NO_GROUP && !groupById[gdFolder]) gdFolder = null;
    const q = $('#goodsFilter').value;
    $('#gdCrumbs').innerHTML = q.trim() ? '<span>🔎 Фільтр по всіх групах</span>' : crumbsHtml(gdFolder, 'Усі товари');
    $('#gdTiles').innerHTML = tilesHtml(gdFolder, q, { showEmpty: true }) || '<p class="muted">Нічого не знайдено</p>';
  } else $('#gdCrumbs').innerHTML = '';
  const gsel = $('#goodsGroup').value;
  const list = sortByGroup(goods.filter(g => match(g, $('#goodsFilter').value) && inGroup(g, gsel || null)));
  $('#stCount').textContent = goods.length;
  $('#stZero').textContent = goods.filter(g => g.count === 0).length;
  $('#stLeft').textContent = useLeftovers ? 'увімкнено' : 'вимкнено';
  const noTax = goods.filter(g => !(g.tax_codes || []).length).length;
  $('#stNoTax').textContent = noTax; $('#stNoTax').className = noTax ? 'zero' : '';
  const filtering = !!$('#goodsFilter').value.trim();
  const ind = d => d ? ` style="padding-left:${12 + d * 18}px"` : '';
  const head = (id, name, depth, n, shut) => `<tr class="grouprow${depth ? ' grow-sub' : ''}" data-gkey="${esc(id)}"><td colspan="7"${ind(depth)}>` +
    `<span class="tw">${shut ? '▸' : '▾'}</span>${esc(name)} <small>· ${n}</small></td></tr>`;
  const item = (g, depth) => `
    <tr data-id="${esc(g.id)}"><td${ind(depth)}>${esc(g.name)}${pendingBadge(g)}</td>
      <td>${g.code !== g.code.trim() ? '<span class="zero" title="Код містить пробіли">⚠ </span>' : ''}${esc(g.code)}</td>
      <td>${esc(g.barcode)}</td><td>${esc(g.uktzed)}</td><td>${taxBadges(g.tax_codes)}</td>
      <td class="num">${uah(g.price)}</td>
      <td class="num ${g.count === 0 ? 'zero' : ''}">${qtyStr(g.count, g.is_weight)}</td></tr>`;
  $('#goodsTable').innerHTML = groupTree(list, filtering ? null : (gsel || null), filtering, head, item)
    || '<tr><td colspan="7" class="muted">Нічого не знайдено</td></tr>';
}
$('#goodsGroup').onchange = renderGoodsTable;
$('#search').oninput = renderSearch;
$('#goodsFilter').oninput = renderGoodsTable;

// --- вкладка «Товари»: меню розділів ---
let goodsPane = (() => { try { return localStorage.getItem('kasa_goods_pane') || 'gpGoods'; } catch (e) { return 'gpGoods'; } })();
function showGoodsPane(id) {
  if (!document.getElementById(id)) id = 'gpGoods';
  goodsPane = id;
  try { localStorage.setItem('kasa_goods_pane', id); } catch (e) {}
  document.querySelectorAll('#goods .subpane').forEach(p => p.classList.toggle('on', p.id === id));
  document.querySelectorAll('#goodsNav button').forEach(b => b.classList.toggle('on', b.dataset.pane === id));
  if (id === 'gpExcise') loadExcise();
}
function renderGoodsNav() {
  $('#navGoodsN').textContent = goods.length;
  $('#navGroupsN').textContent = groupList.length;
  $('#navTaxesN').textContent = taxes.length;
  if (window.exciseTotal != null) $('#navExciseN').textContent = window.exciseTotal;
}
$('#taxRefresh').onclick = e => busy(e.target, async () => {
  const r = await api('/api/taxes/refresh', {});
  toast(`Податкові ставки оновлено: ${r.taxes}`);
  await loadGoods();
});
$('#goodsNav').onclick = e => { const b = e.target.closest('button[data-pane]'); if (b) showGoodsPane(b.dataset.pane); };
let exciseTimer = null;
async function loadExcise() {
  try {
    const d = await api('/api/excise?q=' + encodeURIComponent($('#exciseFilter').value));
    $('#navExciseN').textContent = d.total;
    $('#exciseStat').textContent = `усього ${d.total} · продано ${d.sold} · повернуто ${d.total - d.sold}`;
    const t = s => (s || '').replace('T', ' ').slice(0, 16);
    $('#exciseTable').innerHTML = d.marks.map(m => `<tr>
      <td><code>${esc(m.mark)}</code></td><td>${esc(m.good_name || m.good_code || '—')}</td>
      <td>${m.status === 'SOLD' ? '<span class="badge" style="background:var(--bad);color:#fff">продана</span>' : '<span class="badge">повернута</span>'}</td>
      <td>${esc(m.fiscal_code || (m.receipt_id || '').slice(0, 8) || '—')}</td><td>${esc(t(m.sold_at))}</td><td>${m.status === 'SOLD' ? '—' : esc(t(m.returned_at))}</td></tr>`).join('')
      || '<tr><td colspan="6" class="muted">Марок ще немає</td></tr>';
  } catch (e) { toast(e.message, true); }
}
$('#exciseFilter').oninput = () => { clearTimeout(exciseTimer); exciseTimer = setTimeout(loadExcise, 250); };
showGoodsPane(goodsPane);

// --- плитки: групи — папки, товари — картки ---
const ICON_FOLDER = '<svg class="ico" viewBox="0 0 48 48" aria-hidden="true"><path d="M5 13a4 4 0 0 1 4-4h10l4 5h16a4 4 0 0 1 4 4v19a4 4 0 0 1-4 4H9a4 4 0 0 1-4-4z" fill="#f5b740"/><path d="M5 19h38v18a4 4 0 0 1-4 4H9a4 4 0 0 1-4-4z" fill="#fcd36b"/></svg>';
const ICON_UP = '<svg class="ico" viewBox="0 0 48 48" aria-hidden="true"><path d="M24 12 12 24h8v12h8V24h8z" fill="currentColor" opacity=".55"/></svg>';
function goodIcon(g) {
  const c = isExciseGood(g) ? '#e5484d' : g.is_weight ? '#16a34a' : '#3b82f6';
  if (g.is_weight)   // ваги
    return `<svg class="ico" viewBox="0 0 48 48" aria-hidden="true"><path d="M24 8v6M10 18h28" stroke="${c}" stroke-width="3" stroke-linecap="round"/><path d="M10 18 5 30a5 5 0 0 0 10 0zM38 18l-5 12a5 5 0 0 0 10 0z" fill="${c}" opacity=".85"/><rect x="18" y="36" width="12" height="4" rx="2" fill="${c}"/><path d="M24 14v22" stroke="${c}" stroke-width="3"/></svg>`;
  return `<svg class="ico" viewBox="0 0 48 48" aria-hidden="true"><path d="M24 5 41 14v20L24 43 7 34V14z" fill="${c}" opacity=".18"/><path d="M24 5 41 14 24 23 7 14z" fill="${c}" opacity=".55"/><path d="M24 23v20L7 34V14z" fill="${c}" opacity=".75"/><path d="M24 23v20l17-9V14z" fill="${c}"/></svg>`;
}
function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  return `${n} ${m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many}`;
}
function folderStats(id) {      // товарів у групі з підгрупами; кількість підгруп з товарами
  const n = goods.filter(g => inGroup(g, id)).length;
  const subs = (groupChildren[id] || []).filter(c => goods.some(g => inGroup(g, c.id))).length;
  return { n, subs };
}
function crumbsHtml(cur, rootLabel) {
  const chain = [];
  for (let x = cur && cur !== NO_GROUP ? cur : null; x && groupById[x]; x = groupById[x].parent_id) chain.unshift(groupById[x]);
  const parts = [cur ? `<a data-fold="">🏠 ${esc(rootLabel)}</a>` : `<b>🏠 ${esc(rootLabel)}</b>`];
  chain.forEach((g, i) => parts.push(i === chain.length - 1 ? `<b>${esc(g.path[g.path.length - 1])}</b>` : `<a data-fold="${esc(g.id)}">${esc(g.path[g.path.length - 1])}</a>`));
  return parts.join('<span>›</span>');
}
// Плитки поточної папки cur (null — верхній рівень): «вгору», підгрупи, товари; при пошуку — знайдені товари
function tilesHtml(cur, q, opts) {
  const searching = !!q.trim();
  const tileGood = g => {
    const inCart = opts.cartQty ? opts.cartQty(g) : 0, dot = g.pending ? `<span class="dot${g.pending_error ? ' err' : ''}" title="${g.pending_error ? 'Не відправлено в Checkbox' : 'Змінено локально'}"></span>` : '';
    return `<div class="tile${g.count === 0 && useLeftovers ? ' off' : ''}" data-good="${esc(g.id)}" title="${esc(g.name)}\n${esc(g.barcode || g.code)}">
      ${inCart ? `<span class="incart">${esc(inCart)}</span>` : ''}${dot}${picHtml('good', g.code) || goodIcon(g)}
      <span class="tn">${esc(g.name)}</span>
      ${searching && g.group_path && g.group_path.length ? `<span class="tg">📁 ${esc(g.group_path.join(' / '))}</span>` : ''}
      <span class="tp">${uah(g.price)} ₴</span><span class="ts ${g.count === 0 ? 'zero' : ''}">${g.count != null ? qtyStr(g.count, g.is_weight) : (g.is_weight ? 'кг' : 'шт')}</span></div>`;
  };
  if (searching) return goods.filter(g => match(g, q)).sort(byName).slice(0, 200).map(tileGood).join('');
  let html = '';
  if (cur) {
    const up = cur === NO_GROUP ? '' : (groupById[cur] && groupById[cur].parent_id) || '';
    html += `<div class="tile up" data-fold="${esc(up)}">${ICON_UP}<span class="tn">Назад</span></div>`;
  }
  if (cur !== NO_GROUP) {
    html += (groupChildren[cur || ''] || []).slice().sort(byName).map(c => {
      const st = folderStats(c.id);
      if (!st.n && !opts.showEmpty) return '';
      return `<div class="tile folder" data-fold="${esc(c.id)}" title="${esc(c.path.join(' / '))}">${picHtml('group', c.id) ? '<span class="fbadge">' + ICON_FOLDER + '</span>' + picHtml('group', c.id) : ICON_FOLDER}
        <span class="tn">${esc(c.path[c.path.length - 1])}</span>
        <span class="ts">${plural(st.n, 'товар', 'товари', 'товарів')}${st.subs ? ' · ' + plural(st.subs, 'підгрупа', 'підгрупи', 'підгруп') : ''}</span></div>`;
    }).join('');
  }
  const own = goods.filter(g => cur === NO_GROUP || !cur ? (!g.group_id || !groupById[g.group_id]) : g.group_id === cur);
  return html + own.sort(byName).map(tileGood).join('');
}
function loadView(key) { try { return localStorage.getItem(key) || 'tiles'; } catch (e) { return 'tiles'; } }
function saveView(key, v) { try { localStorage.setItem(key, v); } catch (e) {} }
let posViewMode = loadView('kasa_pos_view'), gdViewMode = loadView('kasa_goods_view'), gdFolder = null;
function setViewButtons(box, mode) { box.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === mode)); }
$('#posView').onclick = e => { const b = e.target.closest('button'); if (!b) return; posViewMode = b.dataset.v; saveView('kasa_pos_view', posViewMode); renderSearch(); $('#search').focus(); };
$('#gdView').onclick = e => { const b = e.target.closest('button'); if (!b) return; gdViewMode = b.dataset.v; saveView('kasa_goods_view', gdViewMode); renderGoodsTable(); };
$('#posCrumbs').onclick = e => { const a = e.target.closest('[data-fold]'); if (!a) return; posGroup = a.dataset.fold || null; renderSearch(); $('#search').focus(); };
$('#gdCrumbs').onclick = e => { const a = e.target.closest('[data-fold]'); if (!a) return; gdFolder = a.dataset.fold || null; renderGoodsTable(); };
$('#posTilesGrid').onclick = e => {
  const f = e.target.closest('[data-fold]'); if (f) { posGroup = f.dataset.fold || null; renderSearch(); $('#posTiles').scrollTop = 0; $('#search').focus(); return; }
  const t = e.target.closest('[data-good]'); if (t) { addToCart(t.dataset.good); renderSearch(); }
};
$('#gdTiles').onclick = e => {
  const f = e.target.closest('[data-fold]'); if (f) { gdFolder = f.dataset.fold || null; return renderGoodsTable(); }
  const t = e.target.closest('[data-good]'); if (t) openGood(goods.find(g => g.id === t.dataset.good));
};

// --- власні зображення плиток: ПКМ по плитці або перетягнути файл на неї ---
const imgKey = (kind, key) => kind + ':' + String(key == null ? '' : key).trim();
function picHtml(kind, key) {
  const k = imgKey(kind, key), v = images[k];
  return v ? `<img class="pic" src="/img?k=${encodeURIComponent(k)}&v=${v}" alt="" loading="lazy" draggable="false">` : '';
}
function tileTarget(el) {     // плитка → {kind, key, name, good?}
  const t = el && el.closest('.tile');
  if (!t || t.classList.contains('up')) return null;
  if (t.dataset.good) {
    const g = goods.find(x => x.id === t.dataset.good);
    return g && String(g.code || '').trim() ? { kind: 'good', key: String(g.code).trim(), name: g.name, good: g } : null;
  }
  if (t.dataset.fold && groupById[t.dataset.fold]) {
    const gr = groupById[t.dataset.fold];
    return { kind: 'group', key: gr.id, name: gr.path[gr.path.length - 1], group: gr };
  }
  return null;
}
// Зменшуємо до 256 px по більшій стороні й кладемо у WebP (або PNG, якщо браузер не вміє) — кілька десятків КБ
function shrinkImage(file) {
  return new Promise((resolve, reject) => {
    if (!file || !/^image\//.test(file.type)) return reject(new Error('Це не зображення'));
    const url = URL.createObjectURL(file), img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      const max = 256, k = Math.min(1, max / Math.max(img.naturalWidth, img.naturalHeight));
      const c = document.createElement('canvas');
      c.width = Math.max(1, Math.round(img.naturalWidth * k)); c.height = Math.max(1, Math.round(img.naturalHeight * k));
      const cx = c.getContext('2d'); cx.imageSmoothingQuality = 'high'; cx.drawImage(img, 0, 0, c.width, c.height);
      let data = c.toDataURL('image/webp', 0.88);
      if (!data.startsWith('data:image/webp')) data = c.toDataURL('image/png');
      resolve(data);
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('Не вдалося прочитати зображення')); };
    img.src = url;
  });
}
function rerenderTiles() { renderSearch(); renderGoodsTable(); }
async function setTileImage(t, file) {
  try {
    const data = await shrinkImage(file);
    const r = await api('/api/image/set', { kind: t.kind, key: t.key, data });
    images = r.images || images; rerenderTiles(); toast('Зображення встановлено: ' + t.name);
  } catch (e) { toast(e.message, true); }
}
async function clearTileImage(t) {
  try { const r = await api('/api/image/delete', { kind: t.kind, key: t.key }); images = r.images || {}; rerenderTiles(); toast('Повернуто стандартний значок'); }
  catch (e) { toast(e.message, true); }
}
let ctxTarget = null;
function closeCtx() { $('#ctxMenu').classList.remove('show'); }
function openCtx(e, t, where) {
  e.preventDefault();
  ctxTarget = t;
  const has = !!images[imgKey(t.kind, t.key)], m = $('#ctxMenu');
  m.innerHTML = `<div class="ch">${t.kind === 'group' ? '📁' : '📦'} ${esc(t.name)}</div>
    <button data-a="img">🖼 ${has ? 'Змінити зображення…' : 'Встановити зображення…'}</button>
    ${has ? '<button data-a="clr" class="danger">✕ Прибрати зображення</button>' : ''}
    ${t.kind === 'good' && where === 'pos' ? '<button data-a="add">➕ Додати в кошик</button>' : ''}
    ${t.kind === 'good' ? '<button data-a="edit">✏ Редагувати товар</button>' : '<button data-a="grp">✏ Редагувати групу</button>'}`;
  m.classList.add('show');
  const w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = Math.min(e.clientX, innerWidth - w - 6) + 'px';
  m.style.top = Math.min(e.clientY, innerHeight - h - 6) + 'px';
  m.querySelector('button').focus();
}
$('#ctxMenu').onclick = e => {
  const b = e.target.closest('button'); if (!b || !ctxTarget) return;
  const t = ctxTarget; closeCtx();
  if (b.dataset.a === 'img') { $('#imgFile').value = ''; $('#imgFile').click(); return; }
  if (b.dataset.a === 'clr') return clearTileImage(t);
  if (b.dataset.a === 'add') { addToCart(t.good.id); return renderSearch(); }
  if (b.dataset.a === 'edit') return openGood(t.good);
  if (b.dataset.a === 'grp') return openGroup(t.key);
};
$('#ctxMenu').onkeydown = e => {
  const bs = [...$('#ctxMenu').querySelectorAll('button')], i = bs.indexOf(document.activeElement);
  if (e.key === 'Escape') { closeCtx(); e.preventDefault(); }
  else if (e.key === 'ArrowDown') { bs[(i + 1) % bs.length].focus(); e.preventDefault(); }
  else if (e.key === 'ArrowUp') { bs[(i - 1 + bs.length) % bs.length].focus(); e.preventDefault(); }
};
document.addEventListener('mousedown', e => { if (!e.target.closest('#ctxMenu')) closeCtx(); });
window.addEventListener('blur', closeCtx);
window.addEventListener('resize', closeCtx);
document.addEventListener('scroll', closeCtx, true);
$('#imgFile').onchange = () => { const f = $('#imgFile').files[0]; if (f && ctxTarget) setTileImage(ctxTarget, f); };
[['#posTilesGrid', 'pos'], ['#gdTiles', 'goods']].forEach(([sel, where]) => {
  const box = $(sel);
  box.addEventListener('contextmenu', e => { const t = tileTarget(e.target); if (t) openCtx(e, t, where); });
  box.addEventListener('dragover', e => {
    if (!e.dataTransfer || ![...e.dataTransfer.types].includes('Files')) return;
    const tile = e.target.closest('.tile'); if (!tileTarget(tile)) return;
    e.preventDefault(); box.querySelectorAll('.tile.drop').forEach(x => x !== tile && x.classList.remove('drop')); tile.classList.add('drop');
  });
  box.addEventListener('dragleave', e => { const tile = e.target.closest('.tile'); if (tile && !tile.contains(e.relatedTarget)) tile.classList.remove('drop'); });
  box.addEventListener('drop', e => {
    const tile = e.target.closest('.tile'), t = tileTarget(tile); if (!t) return;
    e.preventDefault(); tile.classList.remove('drop');
    const f = e.dataTransfer.files[0]; if (f) setTileImage(t, f);
  });
});
// файл, кинутий повз плитку, не має відкриватися замість каси
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => e.preventDefault());

// --- дерево груп і їх редагування (одразу в Checkbox) ---
function subtreeCount(id) { return goods.filter(g => inGroup(g, id)).length; }
function renderGroupTree() {
  const box = $('#grpTree');
  if (!groupList.length) { box.innerHTML = '<p class="muted">Груп ще немає.</p>'; return; }
  const row = (g, depth) => {
    const kids = groupChildren[g.id] || [], shut = treeCollapsed.has(g.id), total = subtreeCount(g.id);
    const name = g.path[g.path.length - 1];
    const counts = g.goods === total ? `товарів: ${total}` : `товарів: ${g.goods}, з підгрупами ${total}`;
    return `<div class="gnode" style="padding-left:${8 + depth * 22}px">
        <button class="tog" data-tog="${esc(g.id)}" ${kids.length ? '' : 'disabled style="visibility:hidden"'}>${shut ? '▸' : '▾'}</button>
        <div class="gname"><b>${esc(name)}</b><small>${counts}${kids.length ? ` · підгруп: ${kids.length}` : ''}</small>
          ${g.description ? `<br><small style="margin:0">${esc(g.description)}</small>` : ''}</div>
        <div class="gacts">
          <button class="btn small sec" data-gedit="${esc(g.id)}" title="Перейменувати, опис, батьківська група">✎</button>
          <button class="btn small sec" data-gsub="${esc(g.id)}" title="Додати підгрупу">＋</button>
          ${g.goods ? `<button class="btn small sec" data-gmove="${esc(g.id)}" title="Перенести товари в іншу групу">⇄</button>` : ''}
          <button class="btn small sec" data-gdel="${esc(g.id)}" title="Видалити групу">🗑</button>
        </div></div>` + (shut ? '' : kids.map(k => row(k, depth + 1)).join(''));
  };
  box.innerHTML = (groupChildren[''] || []).map(g => row(g, 0)).join('');
}
let grpEdit = null;     // { id? } — група, що редагується; parent — для нової
function descendants(id) { const out = new Set([id]); let added = true; while (added) { added = false; groupList.forEach(g => { if (g.parent_id && out.has(g.parent_id) && !out.has(g.id)) { out.add(g.id); added = true; } }); } return out; }
function groupOptions(exclude, current, rootLabel) {
  return (rootLabel ? `<option value="">${esc(rootLabel)}</option>` : '') + groupList.filter(g => !exclude.has(g.id)).map(g =>
    `<option value="${esc(g.id)}" ${g.id === current ? 'selected' : ''}>${'  '.repeat(g.path.length - 1)}${esc(g.path[g.path.length - 1])}</option>`).join('');
}
function openGroup(id, parentId) {
  const g = id ? groupById[id] : null;
  grpEdit = { id: id || null };
  $('#grpTitle').textContent = g ? 'Група · ' + g.path[g.path.length - 1] : (parentId ? 'Нова підгрупа' : 'Нова група');
  $('#grpName').value = g ? (g.name || '') : '';
  $('#grpDesc').value = g ? (g.description || '') : '';
  $('#grpParent').innerHTML = groupOptions(g ? descendants(g.id) : new Set(), g ? g.parent_id : parentId, '— коренева група —');
  $('#grpModal').classList.add('show'); $('#grpName').focus();
}
const closeGroup = () => $('#grpModal').classList.remove('show');
$('#grpAdd').onclick = () => openGroup(null, null);
$('#grpX').onclick = closeGroup; $('#grpCancel').onclick = closeGroup;
$('#grpModal').onkeydown = e => { if (e.key === 'Enter') $('#grpSave').click(); if (e.key === 'Escape') closeGroup(); };
$('#grpSave').onclick = e => busy(e.target, async () => {
  const name = $('#grpName').value.trim();
  if (!name) { $('#grpName').focus(); throw new Error('Вкажіть назву групи'); }
  await api('/api/groups/save', { id: grpEdit.id, name, description: $('#grpDesc').value.trim(), parent_id: $('#grpParent').value || null });
  closeGroup(); toast(grpEdit.id ? 'Групу збережено в Checkbox' : 'Групу створено в Checkbox');
  await loadGoods();
});
let gmFrom = null;
$('#grpTree').onclick = async e => {
  const b = e.target.closest('button'); if (!b) return;
  const d = b.dataset;
  if (d.tog) { treeCollapsed.has(d.tog) ? treeCollapsed.delete(d.tog) : treeCollapsed.add(d.tog); saveSet('kasa_tree_collapsed', treeCollapsed); return renderGroupTree(); }
  if (d.gedit) return openGroup(d.gedit);
  if (d.gsub) return openGroup(null, d.gsub);
  if (d.gmove) {
    const g = groupById[d.gmove]; gmFrom = g.id;
    $('#gmText').textContent = `Усі товари групи «${g.path.join(' / ')}» (${g.goods}) буде перенесено в обрану групу.`;
    $('#gmTo').innerHTML = groupOptions(new Set([g.id]), null, '');
    $('#grpMoveModal').classList.add('show'); return;
  }
  if (d.gdel) {
    const g = groupById[d.gdel];
    if (!(await ask(`Видалити групу «${g.path.join(' / ')}» у Checkbox?`, { title: 'Видалення групи', ok: 'Видалити', danger: true, icon: '🗑' }))) return;
    await busy(b, async () => { await api('/api/groups/delete', { id: g.id }); toast('Групу видалено'); await loadGoods(); });
  }
};
const closeGm = () => $('#grpMoveModal').classList.remove('show');
$('#gmX').onclick = closeGm; $('#gmCancel').onclick = closeGm;
$('#gmOk').onclick = e => busy(e.target, async () => {
  const r = await api('/api/groups/move-goods', { id: gmFrom, to_group: $('#gmTo').value });
  closeGm(); toast(`Товари перенесено${r.moved != null ? ': ' + r.moved : ''}`); await loadGoods();
});

// --- додавання й редагування товару ---
let editing = null;   // товар, що редагується; null — новий
function goodStatus(text, kind) {
  const st = $('#goodStatus'); st.style.display = text ? 'block' : 'none';
  st.className = 'status ' + (kind || 'ok'); st.textContent = text || '';
}
async function genGoodCode() {
  try { const r = await api('/api/goods/next-code'); if (!editing || editing.pending === 'new') $('#gCode').value = r.code; }
  catch (e) { /* код можна ввести вручну; без коду сервер згенерує його сам */ }
}
$('#gCodeGen').onclick = () => genGoodCode();
function openGood(g) {
  editing = g || null;
  $('#goodTitle').textContent = g ? 'Редагування товару' : 'Новий товар';
  $('#gName').value = g ? g.name : '';
  $('#gCode').value = g ? g.code.trim() : '';
  $('#gCode').readOnly = !!g && g.pending !== 'new';   // новий, ще не відправлений товар — код ще можна змінити
  $('#gCodeGen').style.display = $('#gCode').readOnly ? 'none' : '';
  if (!g) genGoodCode();
  $('#gPrice').value = g ? (g.price / 100).toFixed(2) : '';
  $('#gBarcode').value = g ? (g.barcode || '') : '';
  $('#gUktzed').value = g ? (g.uktzed || '') : '';
  $('#gWeight').checked = g ? !!g.is_weight : false;
  const sel = g ? (g.tax_codes || []) : [];
  $('#gTaxes').innerHTML = taxes.length ? taxes.map(t => `<label title="${esc(t.label)}">
      <input type="checkbox" value="${t.code}" ${sel.includes(t.code) ? 'checked' : ''}>
      <span class="tax">${esc(t.symbol || t.code)}</span> ${esc(t.label)}</label>`).join('')
    : '<span class="muted">Довідник ставок порожній — синхронізуйте касу</span>';
  $('#gNote').textContent = (g && g.pending !== 'new')
    ? 'Код змінити не можна: Checkbox розпізнає товари за кодом. Зміни підуть у Checkbox під час синхронізації (PUT /goods).'
    : 'Товар одразу можна продавати; у Checkbox він з\'явиться під час синхронізації.';
  $('#gGroupWrap').style.display = groupList.length ? '' : 'none';
  $('#gGroup').innerHTML = groupOptions(new Set(), g ? g.group_id : null, '— без групи —');
  // залишок: cb — залишок у Checkbox (без локальної правки), g.count — у касі (з правкою)
  const cb = g ? (g.pending ? g.cb_count : g.count) : null;
  stockCb = cb;
  $('#gTrack').checked = !!g && g.count != null;
  $('#gTrack').disabled = cb != null;                  // уже залишковий у Checkbox
  $('#gStock').value = g && g.count != null ? String(g.count / 1000) : '';
  $('#gStockNow').textContent = cb != null ? `у Checkbox зараз: ${qtyStr(cb, g.is_weight)}` : 'у Checkbox залишок не ведеться';
  renderStock();
  goodStatus(g && g.pending_error ? '⚠ Не вдалося відправити в Checkbox: ' + g.pending_error : '', 'bad');
  $('#goodDiscard').style.display = g && g.pending ? '' : 'none';
  $('#goodDiscard').textContent = g && g.pending === 'new' ? '🗑 Видалити новий товар (ще не в Checkbox)' : '↶ Скасувати невідправлені зміни';
  $('#goodModal').classList.add('show');
  (g ? $('#gName') : $('#gName')).focus();
}
let stockCb = null;
function renderStock() {
  $('#gStockRow').style.display = $('#gTrack').checked ? '' : 'none';
  $('#gStockUnit').textContent = $('#gWeight').checked ? 'кг' : 'шт';
}
$('#gTrack').onchange = () => { renderStock(); if ($('#gTrack').checked) $('#gStock').focus(); };
$('#gWeight').addEventListener('change', renderStock);
function readStock() {         // тисячні або null; виняток — невірне значення
  if (!$('#gTrack').checked) return null;
  const raw = $('#gStock').value.trim().replace(',', '.');
  if (!raw) return stockCb != null ? stockCb : null;
  const v = Number(raw);
  if (!(v >= 0) || !/^\d+(\.\d{1,3})?$/.test(raw)) throw new Error('Невірний залишок — напр. 12 або 3.250');
  if (!$('#gWeight').checked && !Number.isInteger(v)) throw new Error('Залишок штучного товару — ціле число');
  const t = Math.round(v * 1000);
  if (stockCb != null && t < stockCb) throw new Error(`Зменшити залишок (у Checkbox ${qtyStr(stockCb, $('#gWeight').checked)}) можна лише списанням у кабінеті Checkbox`);
  return t;
}
function closeGood() {
  if ($('#goodSave').disabled) return toast('Зачекайте — зберігається в Checkbox', true);
  $('#goodModal').classList.remove('show'); editing = null;
}
$('#goodAddBtn').onclick = () => openGood(null);
$('#goodDiscard').onclick = async () => {
  if (!editing) return;
  const msg = editing.pending === 'new' ? 'Видалити цей новий товар з каси?' : 'Повернути товар до стану з Checkbox?';
  if (!(await ask(msg, { title: 'Скасувати зміни товару?', ok: 'Скасувати зміни', cancel: 'Залишити', danger: true }))) return;
  try {
    await api('/api/goods/discard', { code: editing.code.trim() });
    $('#goodModal').classList.remove('show'); editing = null;
    toast('Невідправлені зміни скасовано'); await loadGoods();
  } catch (err) { goodStatus('✖ ' + err.message, 'bad'); }
};
$('#goodsTable').onclick = e => {
  const gr = e.target.closest('tr.grouprow'); if (gr) { toggleCollapsed(gr.dataset.gkey); renderGoodsTable(); renderSearch(); return; }
  const tr = e.target.closest('tr[data-id]'); if (tr) openGood(goods.find(g => g.id === tr.dataset.id));
};
$('#goodClose').onclick = closeGood; $('#goodCancel').onclick = closeGood;
$('#goodModal').onkeydown = e => { if (e.key === 'Escape') closeGood(); };
$('#goodSave').onclick = async e => {
  const btn = e.target;
  const body = {
    id: editing ? editing.id : null,
    name: $('#gName').value, code: $('#gCode').value, price: $('#gPrice').value,
    barcode: $('#gBarcode').value, uktzed: $('#gUktzed').value, is_weight: $('#gWeight').checked,
    tax_codes: [...document.querySelectorAll('#gTaxes input:checked')].map(i => +i.value),
  };
  try { body.stock = readStock(); } catch (err) { goodStatus('✖ ' + err.message, 'bad'); return $('#gStock').focus(); }
  if (groupList.length) body.group_id = $('#gGroup').value || null;   // без довідника груп групу не чіпаємо
  if (!body.tax_codes.length && taxes.length && !(await ask('Товар без податкової ставки — чек з ним буде без податку. Зберегти так?', { title: 'Без податку', ok: 'Зберегти', icon: '⚠️' }))) return;
  btn.disabled = true; $('#goodCancel').disabled = true;
  try {
    const r = await api('/api/goods/save', body);
    btn.disabled = false; $('#goodCancel').disabled = false;
    if (r.unchanged) { goodStatus('Змін немає.', 'ok'); return; }
    $('#goodModal').classList.remove('show');
    toast((editing ? 'Зміни збережено' : 'Товар додано') +
      ' в касі. У Checkbox вони підуть під час синхронізації (⟳, відкриття або закриття зміни)' +
      (body.stock != null && body.stock !== stockCb ? ', залишок — поставкою.' : '.'));
    editing = null;
    await loadGoods();
  } catch (err) {
    btn.disabled = false; $('#goodCancel').disabled = false;
    goodStatus('✖ ' + err.message, 'bad');
  }
};
$('#results').onclick = e => {
  const h = e.target.closest('.ghead'); if (h) { toggleCollapsed(h.dataset.gkey); renderSearch(); return; }
  const it = e.target.closest('.item'); if (it) addToCart(it.dataset.id);
};
$('#search').onkeydown = e => {
  if (e.key !== 'Enter') return;
  const q = $('#search').value.trim();
  if (!q) return;
  const exact = goods.find(g => g.code.trim() === q || g.barcode === q);
  const list = goods.filter(g => match(g, q));
  const g = exact || (list.length === 1 ? list[0] : null);
  if (!g) return toast(list.length ? 'Знайдено кілька товарів — оберіть зі списку' : 'Товар не знайдено', true);
  addToCart(g.id); $('#search').value = ''; renderSearch();
};

// --- тема: авто (як у системі) → світла → темна ---
const THEME_KEY = 'kasa_theme', THEMES = ['auto', 'light', 'dark'];
const THEME_UI = { auto: ['🌓', 'Тема: як у системі'], light: ['☀️', 'Тема: світла'], dark: ['🌙', 'Тема: темна'] };
const darkQuery = window.matchMedia ? matchMedia('(prefers-color-scheme: dark)') : null;
function themeMode() { try { return localStorage.getItem(THEME_KEY) || 'auto'; } catch (e) { return 'auto'; } }
function applyTheme() {
  const m = themeMode(), dark = m === 'dark' || (m === 'auto' && darkQuery && darkQuery.matches);
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  $('#themeBtn').textContent = THEME_UI[m][0];
  $('#themeBtn').title = THEME_UI[m][1] + ' (натисніть, щоб змінити)';
}
$('#themeBtn').onclick = () => {
  const next = THEMES[(THEMES.indexOf(themeMode()) + 1) % THEMES.length];
  try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
  applyTheme(); toast(THEME_UI[next][1]);
};
if (darkQuery) (darkQuery.addEventListener ? darkQuery.addEventListener('change', applyTheme) : darkQuery.addListener(applyTheme));
applyTheme();

// --- телефон покупця (delivery.phone, формат 380XXXXXXXXX) — те саме правило, що й clean_phone() ---
function cleanPhone(v) {          // '' — не вказано; null — невірний; інакше 380XXXXXXXXX
  let d = String(v || '').replace(/\D/g, '');
  if (!d) return '';
  if (d.length === 10 && d[0] === '0') d = '38' + d;
  else if (d.length === 9) d = '380' + d;
  return /^380\d{9}$/.test(d) ? d : null;
}
const fmtPhone = p => `+${p.slice(0, 3)} ${p.slice(3, 5)} ${p.slice(5, 8)} ${p.slice(8, 10)} ${p.slice(10)}`;
function phoneOf(sel) {           // значення поля або виняток, якщо номер невірний
  const p = cleanPhone($(sel).value);
  if (p === null) { $(sel).classList.add('bad'); $(sel).focus(); throw new Error('Невірний номер телефону — напр. 067 123 45 67'); }
  return p;
}
function renderPhone() {
  const v = $('#phone').value, p = cleanPhone(v);
  $('#phone').classList.toggle('bad', p === null && v.trim().length >= 10);
  $('#phoneHint').textContent = p ? `Чек надійде на ${fmtPhone(p)}` : p === null && v.trim().length >= 10 ? 'Невірний номер' : '';
}
$('#phone').oninput = renderPhone;
['#postPhone', '#retPhone'].forEach(id => $(id).addEventListener('input', () => $(id).classList.remove('bad')));

// --- кошик ---
// Додати кількість товару в кошик; повертає номер рядка або null (недостатньо залишку — показано повідомлення)
function addQty(g, qty, quiet) {
  const line = cart.find(c => c.id === g.id);
  const newQty = (line ? line.qty : 0) + qty;
  if (useLeftovers && g.count != null && newQty > g.count) {
    if (!quiet) toast(`Недостатньо залишку: є ${qtyStr(g.count, g.is_weight)}`, true);
    return null;
  }
  if (line) { line.qty = newQty; return cart.indexOf(line); }
  cart.push({ id: g.id, code: g.code.trim(), qty, marks: [] });
  return cart.length - 1;
}
const findByCode = q => goods.find(g => g.code.trim() === q || g.barcode === q);
function addToCart(id) {
  const g = goods.find(x => x.id === id);
  let qty = 1000;
  if (g.is_weight) {
    const v = prompt(`Вага, кг — ${g.name}`, '1');
    if (v === null) return;
    qty = Math.round(parseFloat(v.replace(',', '.')) * 1000);
    if (!(qty > 0)) return toast('Невірна вага', true);
  }
  const idx = addQty(g, qty);
  if (idx == null) return;
  renderCart();
  if (isExciseGood(g) && !g.is_weight) openExcise(idx); else $('#search').focus();
}


// --- сканування підряд ---
let scan = null;   // { last: індекс рядка, history: [{idx, qty, mark}], weightFor: товар, markFor: індекс рядка }
const SCAN_SOUND_KEY = 'checkbox_scan_sound';
let audioCtx = null;
function beep(ok) {
  if (!$('#scanSound').checked) return;
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.type = 'square'; o.frequency.value = ok ? 1400 : 220;
    g.gain.value = 0.05; o.connect(g); g.connect(audioCtx.destination);
    o.start(); o.stop(audioCtx.currentTime + (ok ? 0.07 : 0.25));
  } catch (e) {}
}
function scanHint(text, kind) { $('#scanHint').className = 'scanhint' + (kind ? ' ' + kind : ''); $('#scanHint').textContent = text; }
function needMarks(i) {
  const c = cart[i], g = c && goods.find(x => x.id === c.id);
  return g && isExciseGood(g) && !g.is_weight ? Math.round(c.qty / 1000) - (c.marks || []).length : 0;
}
function renderScan() {
  renderCart();
  const T = computeTotals();
  $('#scanList').innerHTML = cart.map((c, i) => {
    const g = goods.find(x => x.id === c.id), L = T.lines[i], miss = needMarks(i);
    return `<tr class="${i === scan.last ? 'scanned' : ''}"><td>${esc(g.name)}${miss > 0 ? ` <span class="pend err">марок бракує: ${miss}</span>` : ''}
        <br><small class="muted">${esc(g.barcode || g.code)} · ${uah(g.price)} ₴</small></td>
      <td class="num" style="white-space:nowrap">${g.is_weight ? '' : `<button class="qbtn" data-sm="${i}" title="Мінус одна">−</button> `}<b>${qtyStr(c.qty, g.is_weight)}</b>${g.is_weight ? '' : ` <button class="qbtn" data-sp="${i}" title="Плюс одна">+</button>`}</td>
      <td class="num">${uah(L.total)}</td><td><button class="x" data-sd="${i}" title="Прибрати">✕</button></td></tr>`;
  }).join('') || '<tr><td class="muted">Ще нічого не відскановано</td></tr>';
  const last = $('#scanList tr.scanned'); if (last) last.scrollIntoView({ block: 'nearest' });
  $('#scanTotal').textContent = uah(T.total) + ' ₴';
  const units = cart.reduce((a, c) => a + (goods.find(x => x.id === c.id).is_weight ? 1 : Math.round(c.qty / 1000)), 0);
  $('#scanCount').textContent = cart.length ? `позицій: ${cart.length} · одиниць: ${units}` : '';
  $('#scanInput').classList.toggle('mark', scan.markFor != null);
  $('#scanInput').placeholder = scan.markFor != null ? 'Скануйте акцизну марку…' : 'Скануйте штрихкод…';
}
function scanFocus() { setTimeout(() => (scan && scan.weightFor ? $('#scanWeight') : $('#scanInput')).focus(), 0); }
function openScan() {
  if (scan) return scanFocus();
  scan = { last: null, history: [], weightFor: null, markFor: null };
  try { $('#scanSound').checked = localStorage.getItem(SCAN_SOUND_KEY) !== '0'; } catch (e) {}
  $('#scanInput').value = ''; $('#scanWeightRow').style.display = 'none';
  scanHint(cart.length ? 'Кошик уже має товари — скановані додадуться до них' : 'Готово до сканування');
  renderScan();
  $('#scanModal').classList.add('show'); scanFocus();
}
function closeScan() {
  $('#scanModal').classList.remove('show'); scan = null; renderCart(); $('#search').focus();
}
function askMarkIfNeeded(i) {
  if (needMarks(i) > 0) {
    scan.markFor = i;
    const g = goods.find(x => x.id === cart[i].id);
    scanHint(`Тепер відскануйте акцизну марку: ${g.name} (бракує ${needMarks(i)}). Без марки — Esc`, 'warn');
  } else scan.markFor = null;
}
async function scanCode(q) {
  // очікуємо акцизну марку — але штрихкод товару з бази теж приймаємо як новий товар
  if (scan.markFor != null && !findByCode(q)) {
    const i = scan.markFor, c = cart[i];
    if (allMarks(-1).includes(q)) { beep(false); return scanHint('Ця акцизна марка вже додана', 'bad'); }
    const p = markProblem(q);
    if (p) { beep(false); return scanHint(p, 'bad'); }
    const sold = await soldMarkInfo(q);
    if (!scan || scan.markFor !== i) return;
    if (sold) { beep(false); return scanHint(soldText(q, sold), 'bad'); }
    if (allMarks(-1).includes(q)) return;
    c.marks.push(q); scan.history.push({ idx: i, mark: q }); beep(true);
    scanHint(`Марку додано: ${goods.find(x => x.id === c.id).name}`, 'ok');
    askMarkIfNeeded(i);
    return renderScan();
  }
  const g = findByCode(q);
  if (!g) { beep(false); return scanHint(`Товар «${q}» не знайдено. Перевірте код або синхронізуйте товари.`, 'bad'); }
  if (g.is_weight) {
    scan.weightFor = g; $('#scanWeightRow').style.display = ''; $('#scanWeight').value = '';
    beep(true); scanHint(`Ваговий товар: ${g.name}. Введіть вагу в кілограмах і натисніть Enter`, 'warn');
    return scanFocus();
  }
  const i = addQty(g, 1000, true);
  if (i == null) { beep(false); return scanHint(`Недостатньо залишку: ${g.name} (є ${qtyStr(g.count, g.is_weight)})`, 'bad'); }
  scan.last = i; scan.history.push({ idx: i, qty: 1000 }); beep(true);
  scanHint(`+ ${g.name} — ${uah(g.price)} ₴`, 'ok');
  askMarkIfNeeded(i);
  renderScan();
}
function scanWeightAdd() {
  const g = scan.weightFor, kg = parseFloat($('#scanWeight').value.trim().replace(',', '.'));
  const qty = Math.round(kg * 1000);
  if (!(qty > 0)) { beep(false); return scanHint('Невірна вага — напр. 0.456', 'bad'); }
  const i = addQty(g, qty, true);
  if (i == null) { beep(false); return scanHint(`Недостатньо залишку: ${g.name} (є ${qtyStr(g.count, true)})`, 'bad'); }
  scan.weightFor = null; $('#scanWeightRow').style.display = 'none';
  scan.last = i; scan.history.push({ idx: i, qty }); beep(true);
  scanHint(`+ ${g.name} — ${qtyStr(qty, true)}`, 'ok');
  renderScan(); scanFocus();
}
function scanWeightCancel() { scan.weightFor = null; $('#scanWeightRow').style.display = 'none'; scanHint('Ваговий товар не додано'); scanFocus(); }
$('#scanBtn').onclick = openScan;
document.addEventListener('keydown', e => {
  if (e.key === 'F2' && !document.querySelector('.modal.show:not(#scanModal)')) { e.preventDefault(); openScan(); }
});
$('#scanInput').onkeydown = e => {
  if (e.key === 'Escape') {
    e.preventDefault();
    if (scan.markFor != null) { scan.markFor = null; scanHint('Без акцизної марки — її можна додати пізніше в кошику', 'warn'); return renderScan(); }
    return closeScan();
  }
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const q = $('#scanInput').value.trim(); $('#scanInput').value = '';
  if (q) scanCode(q);
};
$('#scanWeight').onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); scanWeightAdd(); } if (e.key === 'Escape') { e.preventDefault(); scanWeightCancel(); } };
$('#scanWeightOk').onclick = scanWeightAdd;
$('#scanWeightCancel').onclick = scanWeightCancel;
$('#scanSound').onchange = () => { try { localStorage.setItem(SCAN_SOUND_KEY, $('#scanSound').checked ? '1' : '0'); } catch (e) {} scanFocus(); };
$('#scanList').onclick = e => {
  const b = e.target.closest('[data-sm],[data-sp],[data-sd]'); if (!b) return;
  const i = +(b.dataset.sm ?? b.dataset.sp ?? b.dataset.sd), c = cart[i], g = goods.find(x => x.id === c.id), n = cart.length;
  if (b.dataset.sp !== undefined) { if (addQty(g, 1000) != null) { scan.last = i; askMarkIfNeeded(i); } }
  else if (b.dataset.sm !== undefined) {
    if (c.qty > 1000) { c.qty -= 1000; if (c.marks.length > c.qty / 1000) c.marks.length = c.qty / 1000; } else cart.splice(i, 1);
    scan.markFor = null; scan.last = null;
  } else { cart.splice(i, 1); scan.markFor = null; scan.last = null; }
  if (cart.length !== n) scan.history = [];   // рядки зсунулись — «прибрати останнє» починається заново
  renderScan(); scanFocus();
};
$('#scanUndo').onclick = () => {
  const h = scan.history.pop();
  if (!h) { scanHint('Немає що прибирати'); return scanFocus(); }
  const c = cart[h.idx];
  if (c) {
    if (h.mark) c.marks = c.marks.filter(m => m !== h.mark);
    else { c.qty -= h.qty; if (c.qty <= 0) cart.splice(h.idx, 1); else if (c.marks.length > Math.round(c.qty / 1000)) c.marks.length = Math.round(c.qty / 1000); }
  }
  scan.markFor = null; scan.last = null;
  scanHint(h.mark ? 'Прибрано останню марку' : 'Прибрано останній скан');
  renderScan(); scanFocus();
};
$('#scanDone').onclick = closeScan;
$('#scanX').onclick = closeScan;
$('#scanCash').onclick = () => { if (!cart.length) { scanHint('Кошик порожній', 'bad'); return scanFocus(); } closeScan(); $('#payCash').click(); };
$('#scanCard').onclick = () => { if (!cart.length) { scanHint('Кошик порожній', 'bad'); return scanFocus(); } closeScan(); $('#payCard').click(); };
$('#scanModal').onclick = e => { if (e.target.id === 'scanModal' || e.target.closest('.card')) { if (!e.target.closest('button,input,label')) scanFocus(); } };

// --- інша оплата ---
const LABELS_KEY = 'checkbox_payment_labels';
const DEFAULT_LABELS = ['Сертифікат', 'Бонуси', 'Переказ на рахунок', 'Інтернет Банкінг', 'Інтернет Еквайринг'];
const ADDED_LABELS = { 'v1.4.6.10': ['Інтернет Банкінг', 'Інтернет Еквайринг'] };   // нові варіанти — і в уже збережені списки
function loadLabels() {
  let list = null;
  try { const v = JSON.parse(localStorage.getItem(LABELS_KEY)); if (Array.isArray(v)) list = v; } catch (e) {}
  if (!list) return DEFAULT_LABELS.slice();
  let done = [];
  try { done = JSON.parse(localStorage.getItem(LABELS_KEY + '_added')) || []; } catch (e) {}
  const todo = Object.keys(ADDED_LABELS).filter(k => !done.includes(k));
  if (todo.length) {
    todo.forEach(k => ADDED_LABELS[k].forEach(l => { if (!list.some(x => x.toLowerCase() === l.toLowerCase())) list.push(l); }));
    saveLabels(list);
    try { localStorage.setItem(LABELS_KEY + '_added', JSON.stringify(done.concat(todo))); } catch (e) {}
  }
  return list;
}
function saveLabels(list) { try { localStorage.setItem(LABELS_KEY, JSON.stringify(list.slice(0, 20))); } catch (e) {} }
const otherType = () => document.querySelector('input[name="otherType"]:checked').value;
function renderOther() {
  const sum = otherType() === 'CASH' ? roundCash(cartTotal, roundingMode) : cartTotal;
  $('#otherSum').textContent = uah(sum) + ' ₴';
  renderOtherList();
}
function renderOtherList() {
  const cur = $('#otherLabel').value.trim().toLowerCase();
  const list = loadLabels();
  $('#otherChips').innerHTML = list.map((l, i) =>
    `<button type="button" class="opt${l.toLowerCase() === cur ? ' on' : ''}" role="option" data-label="${esc(l)}">
       <span class="dot"></span><span class="nm">${esc(l)}</span><span class="del" data-del="${i}" title="Прибрати зі списку">✕</span></button>`).join('')
    || '<div class="muted" style="padding:9px 12px">Список порожній — введіть назву нижче</div>';
  $('#otherChips').style.display = '';
}
function openOther() {
  if (!cart.length) return toast('Кошик порожній', true);
  $('#otherLabel').value = '';
  renderOther();
  $('#otherModal').classList.add('show'); $('#otherLabel').focus();
}
function closeOther() { $('#otherModal').classList.remove('show'); }
document.querySelectorAll('input[name="otherType"]').forEach(r => r.onchange = renderOther);
$('#otherChips').onclick = e => {
  const d = e.target.dataset.del;
  if (d !== undefined) { e.stopPropagation(); const l = loadLabels(); l.splice(+d, 1); saveLabels(l); return renderOtherList(); }
  const opt = e.target.closest('.opt');
  if (opt) {
    $('#otherLabel').value = opt.dataset.label;
    $('#otherChips').querySelectorAll('.opt').forEach(o => o.classList.toggle('on', o === opt));
    $('#otherLabel').focus();
  }
};
$('#otherChips').ondblclick = e => { if (e.target.closest('.opt') && !e.target.dataset.del) $('#otherSubmit').click(); };
$('#otherLabel').oninput = renderOtherList;
$('#payOther').onclick = openOther;
$('#otherClose').onclick = closeOther; $('#otherCancel').onclick = closeOther;
$('#otherModal').onclick = e => { if (e.target.id === 'otherModal') closeOther(); };
$('#otherSubmit').onclick = async () => {
  const label = $('#otherLabel').value.trim().replace(/\s+/g, ' ');
  if (!label) { toast('Вкажіть назву оплати', true); return $('#otherLabel').focus(); }
  const list = loadLabels().filter(l => l.toLowerCase() !== label.toLowerCase());
  saveLabels([label, ...list]);
  const type = otherType();
  closeOther();
  await pay(type, $('#payOther'), null, label);
};
$('#otherLabel').onkeydown = e => {
  if (e.key === 'Enter') $('#otherSubmit').click();
  if (e.key === 'Escape') closeOther();
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {      // стрілками — по списку
    e.preventDefault();
    const list = loadLabels(); if (!list.length) return;
    const cur = list.findIndex(l => l.toLowerCase() === $('#otherLabel').value.trim().toLowerCase());
    const next = cur < 0 ? (e.key === 'ArrowDown' ? 0 : list.length - 1) : (cur + (e.key === 'ArrowDown' ? 1 : -1) + list.length) % list.length;
    $('#otherLabel').value = list[next]; renderOtherList();
    const el = $('#otherChips').querySelectorAll('.opt')[next]; if (el) el.scrollIntoView({ block: 'nearest' });
  }
};

// --- акцизні марки ---
let exciseIdx = null;
const EXCISE_MARK_LEN = 10;   // серія й номер акцизної марки
function allMarks(exceptIdx) {
  return cart.flatMap((c, i) => i === exceptIdx ? [] : (c.marks || []));
}
function openExcise(i) {
  exciseIdx = i;
  const c = cart[i], g = goods.find(x => x.id === c.id);
  $('#exciseName').textContent = g.name;
  renderExcise();
  $('#exciseModal').classList.add('show');
  $('#exciseInput').value = ''; renderExciseLen(); $('#exciseInput').focus();
}
function renderExcise() {
  const c = cart[exciseIdx]; const need = Math.round(c.qty / 1000);
  $('#exciseCount').textContent = `${c.marks.length} з ${need}`;
  $('#exciseCount').className = c.marks.length < need ? 'zero' : '';
  $('#exciseList').innerHTML = c.marks.map((m, k) =>
    `<li>${esc(m)} <button class="x" data-k="${k}" title="Видалити">✕</button></li>`).join('');
}
function closeExcise() { $('#exciseModal').classList.remove('show'); exciseIdx = null; renderCart(); $('#search').focus(); }
const markProblem = v => v.length !== EXCISE_MARK_LEN ? `Акцизна марка має містити ${EXCISE_MARK_LEN} символів — введено ${v.length}` : null;
async function soldMarkInfo(v) {       // марка вже продана? (сервер усе одно перевірить при пробитті чека)
  try { const r = await api('/api/excise/check?mark=' + encodeURIComponent(v)); return r.sold ? (r.info || {}) : null; }
  catch (err) { return null; }
}
const soldText = (v, i) => `Марка ${v} вже продана: ${i.good_name || 'товар'}, чек ${i.fiscal_code || i.receipt_id || '—'} від ${(i.sold_at || '').replace('T', ' ').slice(0, 16)}`;
function renderExciseLen() {
  const v = $('#exciseInput').value.trim(), el = $('#exciseLen');
  el.textContent = v ? `${v.length} з ${EXCISE_MARK_LEN} символів` : '';
  el.style.color = v && v.length !== EXCISE_MARK_LEN ? 'var(--bad)' : '';
}
$('#exciseInput').oninput = renderExciseLen;
$('#exciseAdd').onclick = () => { addExciseMark(); $('#exciseInput').focus(); };
$('#exciseInput').onkeydown = e => {
  if (e.key === 'Escape') return closeExcise();
  if (e.key !== 'Enter') return;
  e.stopPropagation();
  addExciseMark();
};
async function addExciseMark() {
  const v = $('#exciseInput').value.trim();
  if (!v || exciseIdx == null) return;
  const p = markProblem(v);
  if (p) { toast(p, true); return $('#exciseInput').select(); }
  $('#exciseInput').value = ''; renderExciseLen();
  const idx = exciseIdx, c = cart[idx]; const need = Math.round(c.qty / 1000);
  if (c.marks.includes(v) || allMarks(idx).includes(v)) return toast('Ця марка вже додана', true);
  if (c.marks.length >= need) return toast(`Вже додано ${need} марок — збільште кількість товару`, true);
  const sold = await soldMarkInfo(v);
  if (sold) return toast(soldText(v, sold), true);
  if (exciseIdx !== idx || c.marks.includes(v)) return;
  c.marks.push(v); renderExcise();
  if (c.marks.length === need) toast('Усі марки додано');
}
$('#exciseList').onclick = e => { const k = e.target.dataset.k; if (k !== undefined) { cart[exciseIdx].marks.splice(k, 1); renderExcise(); $('#exciseInput').focus(); } };
$('#exciseDone').onclick = closeExcise;
$('#exciseClose').onclick = closeExcise;
$('#exciseNone').onclick = () => { cart[exciseIdx].marks = []; closeExcise(); };
$('#exciseModal').onclick = e => { if (e.target.id === 'exciseModal') closeExcise(); };
// --- знижки й надбавки: ті самі правила, що й apply_discounts() у checkbox_app.py ---
const lineSum = (price, qty) => Math.floor((price * qty + 500) / 1000);
const DISC_TITLE = { DISCOUNT: 'Знижка', EXTRA_CHARGE: 'Надбавка' };
function discAmount(spec, base) {        // зі знаком: знижка — мінус; помилка — виняток з поясненням
  if (!spec) return 0;
  let a;
  if (spec.mode === 'PERCENT') {
    const pv = Math.round(spec.value * 100);
    if (spec.type === 'DISCOUNT' && pv >= 10000) throw new Error('Знижка має бути меншою за 100%');
    a = Math.floor((base * pv + 5000) / 10000);
  } else a = spec.value;
  if (!(a > 0)) throw new Error(`${DISC_TITLE[spec.type]} менша за 1 копійку`);
  if (spec.type === 'DISCOUNT' && a >= base) throw new Error(`Знижка має бути меншою за суму (${uah(base)} ₴)`);
  return spec.type === 'DISCOUNT' ? -a : a;
}
function discLabel(spec, amount) {
  const title = spec.name || DISC_TITLE[spec.type] + (spec.mode === 'PERCENT' ? ` ${spec.value}%` : '');
  return `${title} ${amount > 0 ? '+' : '−'}${uah(Math.abs(amount))} ₴`;
}
function computeTotals() {
  const lines = cart.map(c => {
    const g = goods.find(x => x.id === c.id), base = lineSum(g.price, c.qty);
    let d = 0, err = null;
    try { d = discAmount(c.disc, base); } catch (e) { err = e.message; }
    return { base, d, err, total: base + d };
  });
  const sub = lines.reduce((a, l) => a + l.total, 0);
  let rd = 0, rerr = null;
  try { rd = cartDisc ? discAmount(cartDisc, sub) : 0; } catch (e) { rerr = e.message; }
  return { lines, sub, rd, rerr, total: sub + rd };
}
function renderCart() {
  cart = cart.filter(c => {
    if (goods.some(g => g.id === c.id)) return true;
    const same = c.code && goods.find(g => g.code.trim() === c.code);   // після синхронізації id міг змінитися
    if (same) { c.id = same.id; return true; }
    return false;
  });
  if (!cart.length) cartDisc = null;
  const T = computeTotals();
  $('#cart').innerHTML = cart.map((c, i) => {
    const g = goods.find(x => x.id === c.id), L = T.lines[i];
    let markBtn = '';
    if (isExciseGood(g) && !g.is_weight) {
      c.marks = c.marks || [];
      const need = Math.round(c.qty / 1000), have = c.marks.length;
      markBtn = ` <button class="markbtn ${have >= need ? 'ok' : 'warn'}" data-marks="${i}">🏷 Марки ${have}/${need}</button>`;
    }
    const discBtn = c.disc
      ? `<button class="markbtn ${L.err ? 'warn' : 'ok'}" data-disc="${i}" title="${esc(L.err || 'Змінити')}">${L.err ? '⚠ ' + esc(L.err) : esc(discLabel(c.disc, L.d))}</button>`
      : `<button class="markbtn" data-disc="${i}" title="Знижка або надбавка на цей товар">％</button>`;
    const sumCell = L.d && !L.err ? `<s class="old">${uah(L.base)}</s><br>${uah(L.total)}` : uah(L.base);
    return `<tr><td>${esc(g.name)}<br><small class="muted">${uah(g.price)} ₴ / ${g.is_weight ? 'кг' : 'шт'}</small> ${taxBadges(g.tax_codes)}<br>${discBtn}${markBtn}</td>
      <td class="num"><input type="number" min="0" step="${g.is_weight ? '0.001' : '1'}" value="${c.qty / 1000}" data-i="${i}"></td>
      <td class="num">${sumCell}</td><td><button class="x" data-del="${i}">✕</button></td></tr>`;
  }).join('') || '<tr><td colspan="4" class="muted">Порожньо</td></tr>';
  $('#cartDiscBtn').style.display = cart.length ? '' : 'none';
  $('#cartDiscSum').className = 'disc' + (T.rd > 0 ? ' up' : '');
  $('#cartDiscSum').textContent = !cartDisc ? '' : T.rerr ? '⚠ ' + T.rerr : discLabel(cartDisc, T.rd);
  if (T.rerr) $('#cartDiscSum').className = 'zero';
  $('#cartDiscBtn').textContent = cartDisc ? '％ Змінити знижку на чек' : '％ Знижка / надбавка на чек';
  $('#total').textContent = uah(T.total) + ' ₴';
  cartTotal = T.total;
  renderCash();
}
// --- вікно знижки: target — номер рядка кошика або 'receipt' ---
let discTarget = null;
const radioVal = name => document.querySelector(`input[name="${name}"]:checked`).value;
const setRadio = (name, v) => { const r = document.querySelector(`input[name="${name}"][value="${v}"]`); if (r) r.checked = true; };
function discBase() {
  const T = computeTotals();
  return discTarget === 'receipt' ? T.sub : T.lines[discTarget].base;
}
function readDisc() {         // специфікація з форми або виняток
  const raw = $('#discValue').value.trim().replace(',', '.');
  const v = parseFloat(raw);
  if (!raw || !(v > 0)) throw new Error('Вкажіть значення більше нуля');
  const mode = radioVal('discMode');
  const spec = { type: radioVal('discType'), mode, name: $('#discName').value.trim().replace(/\s+/g, ' ') };
  if (mode === 'PERCENT') {
    if (Math.abs(v * 100 - Math.round(v * 100)) > 1e-6) throw new Error('Відсоток — не більше двох знаків після коми');
    spec.value = v;
  } else spec.value = Math.round(v * 100);
  if (!spec.name) delete spec.name;
  return spec;
}
function renderDiscPreview() {
  const box = $('#discPreview');
  if (!$('#discValue').value.trim()) { box.style.display = 'none'; return; }
  box.style.display = '';
  try {
    const base = discBase(), a = discAmount(readDisc(), base);
    box.className = 'status ok';
    box.textContent = `${uah(base)} ₴ → ${uah(base + a)} ₴ (${a > 0 ? '+' : '−'}${uah(Math.abs(a))} ₴)`;
  } catch (e) { box.className = 'status bad'; box.textContent = e.message; }
}
function openDisc(target) {
  discTarget = target;
  const cur = target === 'receipt' ? cartDisc : cart[target].disc;
  $('#discTitle').textContent = target === 'receipt' ? 'Знижка / надбавка на чек' : 'Знижка / надбавка на товар';
  $('#discFor').textContent = target === 'receipt' ? `Сума товарів: ${uah(computeTotals().sub)} ₴`
    : `${goods.find(x => x.id === cart[target].id).name} · ${uah(discBase())} ₴`;
  setRadio('discType', cur ? cur.type : 'DISCOUNT');
  setRadio('discMode', cur ? cur.mode : 'PERCENT');
  $('#discValue').value = cur ? (cur.mode === 'VALUE' ? (cur.value / 100).toFixed(2) : cur.value) : '';
  $('#discName').value = (cur && cur.name) || '';
  $('#discRemove').style.display = cur ? '' : 'none';
  renderDiscPreview();
  $('#discModal').classList.add('show'); $('#discValue').focus(); $('#discValue').select();
}
function closeDisc() { $('#discModal').classList.remove('show'); discTarget = null; $('#search').focus(); }
function setDisc(spec) {
  if (discTarget === 'receipt') cartDisc = spec; else cart[discTarget].disc = spec;
}
$('#discApply').onclick = () => {
  let spec;
  try { spec = readDisc(); discAmount(spec, discBase()); } catch (e) { toast(e.message, true); return $('#discValue').focus(); }
  setDisc(spec); closeDisc(); renderCart();
};
$('#discRemove').onclick = () => { setDisc(null); closeDisc(); renderCart(); };
$('#discClose').onclick = closeDisc;
$('#discModal').onclick = e => { if (e.target.id === 'discModal') closeDisc(); };
$('#discValue').oninput = renderDiscPreview;
document.querySelectorAll('input[name="discType"], input[name="discMode"]').forEach(r => r.onchange = renderDiscPreview);
$('#discModal').onkeydown = e => { if (e.key === 'Enter') $('#discApply').click(); if (e.key === 'Escape') closeDisc(); };
$('#cartDiscBtn').onclick = () => openDisc('receipt');
function receivedKop() {
  const v = $('#received').value.trim();
  return v === '' ? null : Math.round(parseFloat(v.replace(',', '.')) * 100);
}
function renderCash() {
  const toPay = roundCash(cartTotal, roundingMode);
  const diff = toPay - cartTotal;
  $('#roundLine').style.display = diff ? 'flex' : 'none';
  $('#roundSum').textContent = (diff > 0 ? '+' : '') + uah(diff) + ' ₴';
  $('#toPayCash').textContent = uah(toPay) + ' ₴';
  const got = receivedKop();
  const rest = $('#rest');
  $('#restLine').style.display = got == null ? 'none' : 'flex';
  if (got != null) {
    rest.textContent = got >= toPay ? uah(got - toPay) + ' ₴' : 'недостатньо';
    rest.className = got >= toPay ? '' : 'bad';
  }
}
$('#received').oninput = renderCash;
$('#cart').onchange = e => {
  const i = e.target.dataset.i; if (i === undefined) return;
  const c = cart[i], g = goods.find(x => x.id === c.id);
  let qty = Math.round(parseFloat(e.target.value) * 1000);
  if (!g.is_weight) qty = Math.round(qty / 1000) * 1000;
  if (!(qty > 0)) cart.splice(i, 1);
  else if (useLeftovers && g.count != null && qty > g.count) toast(`Недостатньо залишку: є ${qtyStr(g.count, g.is_weight)}`, true);
  else {
    c.qty = qty;
    const units = Math.round(qty / 1000);
    if (c.marks && c.marks.length > units) {
      c.marks = c.marks.slice(0, units);
      toast(`Кількість зменшено — залишено ${units} акцизних марок`, true);
    }
  }
  renderCart();
};
$('#cart').onclick = e => {
  const m = e.target.dataset.marks; if (m !== undefined) return openExcise(+m);
  const d = e.target.closest('[data-disc]'); if (d) return openDisc(+d.dataset.disc);
  const i = e.target.dataset.del; if (i !== undefined) { cart.splice(i, 1); renderCart(); }
};
$('#clearCart').onclick = () => { cart = []; cartDisc = null; renderCart(); };

function marksIncomplete() {
  return cart.filter(c => {
    const g = goods.find(x => x.id === c.id);
    return g && isExciseGood(g) && !g.is_weight && (c.marks || []).length < Math.round(c.qty / 1000);
  }).map(c => goods.find(x => x.id === c.id).name);
}
async function confirmMarks() {
  const missing = marksIncomplete();
  return !missing.length || await ask(`Не для всіх одиниць додано акцизні марки:\n• ${missing.join('\n• ')}\n\nПробити чек усе одно?`,
                                      { title: 'Акцизні марки', ok: 'Пробити без марок', cancel: 'Додати марки', icon: '🏷' });
}
// Зображення останнього чека під кошиком; Checkbox може готувати PNG секунду-дві — пробуємо ще раз
function loadLastPng(img, id, attempt = 0) {
  img.onerror = () => {
    if (attempt < 2) return setTimeout(() => loadLastPng(img, id, attempt + 1), 1500);
    img.closest('.last-receipt').innerHTML = '<span class="muted" style="font-size:12px">Зображення чека недоступне — натисніть «Показати чек»</span>';
  };
  img.src = pngUrl(id) + (attempt ? '&r=' + attempt : '');
}
async function pay(type, btn, card, label, opts = {}) {   // повертає true, якщо чек пробито
  if (!cart.length) { toast('Кошик порожній', true); return false; }
  if (!opts.marksChecked && !(await confirmMarks())) return false;
  const de = discountError();
  if (de) { toast('Виправте знижку: ' + de, true); return false; }
  let phone;
  try { phone = phoneOf('#phone'); } catch (e) { toast(e.message, true); return false; }
  const body = { items: cart, payment_type: type };
  if (cartDisc) body.discount = cartDisc;
  if (phone) body.phone = phone;
  if (card) body.card = card;
  if (label) body.label = label;
  if (opts.terminalRaw) body.terminal_raw = opts.terminalRaw;
  if (type === 'CASH' && !label) {
    const got = receivedKop();
    if (got != null) {
      if (!(got >= roundCash(cartTotal, roundingMode))) { toast('Отримано менше, ніж до сплати', true); return false; }
      body.received = got;
    }
  }
  return await busy(btn, async () => {
    let r;
    try {
      r = await api('/api/sell', body);
    } catch (e) {
      if (e.code !== 'SHIFT_CLOSED' || opts.noShiftPrompt || !(await askOpenShift('пробити чек'))) throw e;
      if (!(await openShiftNow())) throw e;
      r = await api('/api/sell', body);
    }
    cart = []; cartDisc = null; $('#received').value = ''; $('#phone').value = ''; renderPhone();
    const last = $('#last');
    last.style.display = 'block';
    const extra = (r.round_sum ? ` · заокруглення ${uah(r.round_sum)} ₴` : '') + (r.rest ? ` · решта ${uah(r.rest)} ₴` : '');
    last.innerHTML = `✔ Чек фіскалізовано: ${uah(r.total_sum)} ₴${extra}, ФН ${esc(r.fiscal_code)} · <a href="#" data-id="${esc(r.id)}">Показати чек</a>
      <div class="last-receipt"><img data-id="${esc(r.id)}" alt="Чек" title="Клікніть, щоб збільшити й надрукувати"></div>`;
    loadLastPng(last.querySelector('img'), r.id);
    toast('Чек фіскалізовано');
    await loadGoods();
  });
}
$('#payCash').onclick = e => pay('CASH', e.target);
// --- оплата карткою: форма ЕПЗ ---
const cardInputs = () => document.querySelectorAll('#cardModal [data-card]');
const CARD_KEY = 'checkbox_card_defaults';
function openCardModal(prefill) {
  if (!cart.length) return toast('Кошик порожній', true);
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(CARD_KEY) || '{}'); } catch (e) {}
  cardInputs().forEach(i => {
    if (i.type === 'checkbox') i.checked = !!(prefill && prefill[i.dataset.card]);
    else i.value = (prefill && prefill[i.dataset.card]) || (i.hasAttribute('data-remember') ? (saved[i.dataset.card] || '') : '');
  });
  $('#cardSum').textContent = uah(cartTotal) + ' ₴';
  $('#cardModal').classList.add('show');
  $('#cardModal [data-card="card_mask"]').focus();
}
function closeCardModal() { $('#cardModal').classList.remove('show'); }
function collectCard() {
  const card = {}, remember = {};
  cardInputs().forEach(i => {
    if (i.type === 'checkbox') { if (i.checked) card[i.dataset.card] = true; return; }
    const v = i.value.trim();
    if (v) card[i.dataset.card] = v;
    if (i.hasAttribute('data-remember')) remember[i.dataset.card] = v;
  });
  try { localStorage.setItem(CARD_KEY, JSON.stringify(remember)); } catch (e) {}
  return Object.keys(card).length ? card : null;
}
$('#payCard').onclick = () => {
  if (termCfg.enabled && termCfg.device_id) openTermPay(); else openCardModal();
};

// --- оплата через платіжний термінал (POSSrv, WebSocket) ---
// Документація не описує код успіху і поля ЕПЗ, тому відповідь розбирається гнучко,
// а сира відповідь показується й зберігається разом із продажем.
const pick = (o, ...keys) => { for (const k of keys) { const v = o && o[k]; if (v !== undefined && v !== null && v !== '' && v !== 0) return String(v); } return ''; };
function classifyTerm(msg) {
  if (!msg || typeof msg !== 'object') return 'progress';
  if (msg.success === false) return 'declined';
  const r = msg.result || {};
  if (msg.success === true && (Number(r.amount) > 0 || Number(r.txn_num) > 0 || Number(r.invoice_num) > 0 ||
      pick(r, 'rrn', 'auth_code', 'approval_code', 'pan', 'card_mask'))) return 'approved';
  return 'progress';
}
function cardFromTerm(msg) {
  // Назви полів — зі справжньої відповіді POSSrv (ПриватБанк, PAX A930); вони збігаються з полями платежу Checkbox
  const r = (msg && msg.result) || {};
  const card = {
    card_mask: pick(r, 'card_mask', 'pan', 'masked_pan').slice(0, 19),
    rrn: pick(r, 'rrn'),
    auth_code: pick(r, 'auth_code', 'approval_code'),
    receipt_no: pick(r, 'receipt_no', 'invoice_num'),
    payment_system: pick(r, 'payment_system', 'issuer_name'),
    owner_name: pick(r, 'owner_name', 'card_holder'),
    bank_name: pick(r, 'bank_name', 'bank_acquirer') || termCfg.bank_name || '',
    terminal: pick(r, 'terminal', 'terminal_id'),
    acquirer_and_seller: pick(r, 'acquirer_and_seller', 'merchant_id'),
    provider_type: pick(r, 'provider_type'),
  };
  if (r.signature_required === true || r.sign_verif === true) card.signature_required = true;
  Object.keys(card).forEach(k => { if (!card[k]) delete card[k]; });
  return card;
}

// term — поточна операція на терміналі: { ws, state, messages, approvedMsg, amount, op? }
// op — не звичайний продаж з кошика: { cmd: 'purchase'|'cancel', payload, refund, fiscalize(card, raw) → bool, back() }
//   cancel — команда POSSrv «Відміна транзакції / Повернення коштів»
let term = null;
function wsUrl(command) {
  const base = (termCfg.url || 'http://localhost:9020').replace(/^http/i, 'ws').replace(/\/+$/, '');
  return `${base}/api/pos/${encodeURIComponent(termCfg.device_id)}/${command}`;
}
const termWords = t => t && t.op && t.op.refund
  ? { title: 'Повернення на картку', op: 'повернення', notDone: 'Повернення не проведено', ok: 'Повернення на терміналі проведено',
      manual: 'Повернення пройшло, пробити чек', again: 'кошти повторно не буде повернено', startShift: 'Відкрити зміну і повернути',
      needShift: 'щоб провести повернення', running: 'Повернення ще триває', doneUnsent: 'Повернення на картку вже проведено' }
  : { title: 'Оплата карткою', op: 'оплату', notDone: 'Оплату не проведено', ok: 'Оплату на терміналі схвалено',
      manual: 'Оплата пройшла, ввести дані', again: 'картку повторно не буде списано', startShift: 'Відкрити зміну і оплатити',
      needShift: 'щоб прийняти оплату', running: 'Оплата ще триває', doneUnsent: 'Оплату карткою вже проведено' };
function termRender(state, text) {
  term.state = state;
  const icons = { sending: '⏳', progress: '⏳', approved: '✔', fiscal_failed: '⚠', declined: '✖', unknown: '⚠', conn: '✖',
                  checking: '⏳', shift_closed: '⚠', shift_unknown: '✖', opening: '⏳' };
  $('#termState').className = 'termstate ' + state;
  $('#termState').textContent = `${icons[state] || ''} ${text}`;
  $('#termRaw').textContent = term.messages.map(m => JSON.stringify(m, null, 2)).join('\n\n') || '—';
  const show = (id, on) => { $(id).style.display = on ? '' : 'none'; };
  const openShiftBtn = state === 'shift_closed' || (state === 'fiscal_failed' && term.shiftClosed);
  const W = termWords(term);
  $('#termOpenShift').textContent = state === 'shift_closed' ? W.startShift : 'Відкрити зміну і пробити чек';
  $('#termManual').textContent = W.manual;
  show('#termCancel', state === 'sending' || state === 'progress');
  show('#termRetry', ['declined', 'unknown', 'conn', 'shift_unknown'].includes(state));
  show('#termManual', ['declined', 'unknown', 'conn'].includes(state));
  show('#termOpenShift', openShiftBtn);
  show('#termFiscalRetry', state === 'fiscal_failed' && !term.shiftClosed);
  show('#termCloseBtn', ['declined', 'unknown', 'conn', 'shift_closed', 'shift_unknown'].includes(state));
}
// обмін з терміналом — у журнал каси (запити йдуть з браузера, сервер їх інакше не бачить)
function termLog(event, t, data) {
  fetch('/api/termlog', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event, cmd: t.op ? t.op.cmd : 'purchase', data, url: wsUrl(t.op ? t.op.cmd : 'purchase') }) }).catch(() => {});
}
function startTermPayment() {
  const t = term;                    // ця спроба; після закриття вікна її події ігноруються
  t.messages = []; t.approvedMsg = null; t.finished = false;
  termRender('sending', `Надсилаю ${uah(t.amount)} ₴ на термінал…`);
  const live = () => term === t && !t.finished;
  let ws;
  const W = termWords(t);
  try { ws = new WebSocket(wsUrl(t.op ? t.op.cmd : 'purchase')); } catch (e) { t.finished = true; return termRender('conn', 'Невірна адреса POSSrv: ' + e.message); }
  t.ws = ws;
  let opened = false;
  ws.onopen = () => {
    opened = true;
    if (!live()) return;
    const payload = t.op ? t.op.payload : { amount: t.amount };
    ws.send(JSON.stringify(payload));
    termLog('send', t, payload);
    termRender('progress', `Очікую ${W.op} на терміналі…`);
  };
  ws.onmessage = ev => {
    if (!live()) return;
    let msg; try { msg = JSON.parse(ev.data); } catch (e) { msg = { raw: String(ev.data) }; }
    t.messages.push(msg);
    termLog('message', t, msg);
    const kind = classifyTerm(msg);
    if (kind === 'approved') { t.finished = true; t.approvedMsg = msg; try { ws.close(); } catch (e) {} fiscalizeAfterTerm(); }
    else if (kind === 'declined') { t.finished = true; try { ws.close(); } catch (e) {} termRender('declined', `${W.notDone}: ` + (msg.error || `код ${msg.code}`)); }
    else termRender('progress', msg.error || msg.message || `Очікую ${W.op} на терміналі…`);
  };
  const noConnection = () => termRender('conn', `Немає зв'язку з POSSrv (${termCfg.url}). Перевірте, що POSSrv запущено.`);
  ws.onerror = () => { termLog('error', t, null); if (!opened && live()) { t.finished = true; noConnection(); } };
  ws.onclose = () => {
    if (!t.finished) termLog('close', t, null);
    if (!live()) return;
    t.finished = true;
    if (opened) termRender('unknown', `З'єднання з терміналом завершилося без результату. Подивіться на екран терміналу: якщо ${W.op} пройшл${t.op && t.op.refund ? 'о' : 'а'} — натисніть «${W.manual}».`);
    else noConnection();
  };
}
async function fiscalizeAfterTerm() {
  const t = term, W = termWords(t);
  termRender('approved', `${W.ok}. Пробиваю чек…`);
  const card = t.approvedMsg ? cardFromTerm(t.approvedMsg) : null;
  const ok = t.op ? await t.op.fiscalize(card, t.messages)
    : await pay('CASHLESS', $('#payCard'), card, null, { marksChecked: true, terminalRaw: t.messages, noShiftPrompt: true });
  if (term !== t) return;
  if (ok) { term = null; $('#termModal').classList.remove('show'); return; }
  t.shiftClosed = (await shiftIsOpen()) === false;
  if (term !== t) return;
  termRender('fiscal_failed', t.shiftClosed
    ? `${W.ok}, але чек не пробито: зміна закрита. Відкрийте зміну — ${W.again}.`
    : `${W.ok}, але чек не пробито. Повторіть — ${W.again}.`);
}
// Перед списанням з картки перевіряємо зміну: інакше гроші спишуться, а чек не проб'ється
async function beginTermFlow() {
  const t = term;
  t.messages = [];
  termRender('checking', 'Перевіряю зміну…');
  const open = await shiftIsOpen();
  if (term !== t) return;
  if (open === true) return startTermPayment();
  if (open === false) return termRender('shift_closed', `Зміна закрита. Відкрийте її, ${termWords(t).needShift} — інакше чек не буде пробито.`);
  termRender('shift_unknown', "Не вдалося перевірити зміну в Checkbox. Перевірте зв'язок і спробуйте ще раз.");
}
function discountError() {
  const T = computeTotals(), bad = T.lines.find(l => l.err);
  return bad ? bad.err : T.rerr;
}
async function openTermPay() {
  if (!cart.length) return toast('Кошик порожній', true);
  const de = discountError();
  if (de) return toast('Виправте знижку: ' + de, true);   // до списання з картки, а не після
  try { phoneOf('#phone'); } catch (e) { return toast(e.message, true); }
  if (!(await confirmMarks())) return;
  term = { amount: cartTotal, messages: [] };
  showTermModal();
}
function showTermModal() {
  $('#termTitle').textContent = termWords(term).title;
  $('#termSum').textContent = uah(term.amount) + ' ₴';
  $('#termDevName').textContent = termSummaryText(termCfg);
  $('#termModal').classList.add('show');
  beginTermFlow();
}
async function closeTermPay() {
  if (!term) return $('#termModal').classList.remove('show');
  const t = term, W = termWords(t);
  if (t.state === 'approved' || t.state === 'opening') return toast('Зачекайте — операція ще триває', true);
  if (t.state === 'fiscal_failed' &&
      !(await ask(`${W.doneUnsent}, а чек не пробито.\nЗакрити без чека?`, { title: 'Чек не пробито', ok: 'Закрити без чека', cancel: 'Повернутися', danger: true }))) return;
  if (term !== t) return;
  if (['sending', 'progress'].includes(t.state)) {
    if (!(await ask(`${W.running}. Скасування в програмі може не скасувати її на терміналі — перевірте екран терміналу.\nЗакрити вікно?`,
                    { title: 'Операція на терміналі триває', ok: 'Закрити', cancel: 'Чекати далі', danger: true }))) return;
    if (term !== t) return;
    term.finished = true; try { term.ws && term.ws.close(); } catch (e) {}
  }
  const op = term.op;
  term = null; $('#termModal').classList.remove('show');
  if (op && op.back) op.back();          // назад до вікна, з якого почали
}
$('#termCancel').onclick = closeTermPay;
$('#termCloseBtn').onclick = closeTermPay;
$('#termX').onclick = closeTermPay;
$('#termRetry').onclick = () => beginTermFlow();
$('#termOpenShift').onclick = async () => {
  const t = term, after = t.state;          // shift_closed → оплата; fiscal_failed → лише чек
  termRender('opening', 'Відкриваю зміну…');
  const ok = await openShiftNow();
  if (term !== t) return;
  if (!ok) {
    if (after === 'shift_closed') return termRender('shift_closed', 'Не вдалося відкрити зміну. Спробуйте ще раз або відкрийте її на вкладці «Зміна».');
    return termRender('fiscal_failed', 'Не вдалося відкрити зміну. Оплату вже схвалено — спробуйте ще раз, картку повторно не буде списано.');
  }
  t.shiftClosed = false;
  if (after === 'fiscal_failed') fiscalizeAfterTerm(); else startTermPayment();
};
$('#termModal').onkeydown = e => { if (e.key === 'Escape') closeTermPay(); };
$('#termFiscalRetry').onclick = () => fiscalizeAfterTerm();
$('#termManual').onclick = async () => {
  if (term && term.op) {
    const t = term;
    if (!(await ask(t.op.refund ? 'Повернення на терміналі точно пройшло? Буде пробито чек повернення.'
                                : 'Оплата на терміналі точно пройшла? Чек буде пробито без даних картки.',
                    { title: 'Підтвердіть за екраном терміналу', ok: 'Так, пробити чек', cancel: 'Ні' })) || term !== t) return;
    term.approvedMsg = null;
    return fiscalizeAfterTerm();
  }
  const prefill = { bank_name: termCfg.bank_name || '' };
  term = null; $('#termModal').classList.remove('show');
  openCardModal(prefill);
};
$('#cardClose').onclick = closeCardModal;
$('#cardModal').onclick = e => { if (e.target.id === 'cardModal') closeCardModal(); };
$('#cardSkip').onclick = async e => { closeCardModal(); await pay('CASHLESS', $('#payCard')); };
$('#cardSubmit').onclick = async e => { const card = collectCard(); closeCardModal(); await pay('CASHLESS', $('#payCard'), card); };
$('#cardModal').onkeydown = e => { if (e.key === 'Enter') $('#cardSubmit').click(); if (e.key === 'Escape') closeCardModal(); };
$('#last').onclick = async e => {
  if (!e.target.dataset.id) return; e.preventDefault();
  showReceipt(e.target.dataset.id);
};

// --- синхронізація ---
let pendingCount = 0;
function renderPending(n) {
  pendingCount = n || 0;
  $('#syncBtn').textContent = pendingCount ? `⟳ Синхронізувати · ${pendingCount}` : '⟳ Синхронізувати';
  $('#syncBtn').title = pendingCount
    ? `Невідправлених змін товарів: ${pendingCount}. Вони підуть у Checkbox під час синхронізації.`
    : 'Оновити товари з Checkbox';
  $('#syncBtn').classList.toggle('attn', !!pendingCount);
}
function syncSummary(s) {      // s — результат синхронізації (кнопка або зміна)
  if (!s) return '';
  if (s.sync_error) return ` · ⚠ синхронізація не вдалася: ${s.sync_error}`;
  let t = s.pushed ? ` · у Checkbox відправлено змін: ${s.pushed}` : '';
  if (s.push_error) t += ` · ⚠ зміни товарів не відправлено (${s.pending}): ${s.push_error}`;
  return t;
}
$('#syncBtn').onclick = e => busy(e.target, async () => {
  const r = await api('/api/sync', {});
  await loadGoods();
  toast(`Синхронізовано товарів: ${r.count}, ставок: ${r.taxes}` + syncSummary(r) +
    (r.no_tax.length ? ` · ⚠ без податку: ${r.no_tax.length}` : '') +
    (r.bad_codes.length ? ` · ⚠ пробіли в коді: ${r.bad_codes.length}` : ''), !!r.push_error);
});

// --- зміна ---
let shiftOpen = false;
async function loadShift(fresh) {
  try {
    // актуальний баланс потрібен лише на вкладці «Зміна»; для шапки досить стану з кешу каси
    if (fresh === undefined) fresh = !!document.querySelector('#shift.tab.active');
    const { shift, last_z } = await api('/api/shift' + (fresh ? '?fresh=1' : ''));
    const b = $('#shiftBadge');
    shiftOpen = !!shift;
    if (shift) {
      b.textContent = `Зміна №${shift.serial ?? ''} відкрита`; b.className = 'badge open';
      $('#shiftInfo').innerHTML = `Зміна <b>№${esc(shift.serial)}</b> · статус <b>${esc(shift.status)}</b> · відкрита ${esc((shift.opened_at || '').replace('T', ' ').slice(0, 19))}`;
      const bal = shift.balance || {};
      $('#cashNow').innerHTML = bal.balance != null ? `Готівка в касі: <b>${uah(bal.balance)} ₴</b>` +
        `<small class="muted"> · на початку ${uah(bal.initial || 0)} · продажі ${uah(bal.cash_sales || 0)} · повернення ${uah(bal.cash_returns || 0)}` +
        ` · внесено ${uah(bal.service_in || 0)} · винесено ${uah(bal.service_out || 0)}</small>` : '';
    } else {
      b.textContent = 'Зміна закрита'; b.className = 'badge';
      $('#shiftInfo').textContent = 'Зміна не відкрита.';
      $('#cashNow').textContent = '';
    }
    $('#openShift').style.display = shift ? 'none' : '';
    $('#xReport').style.display = $('#closeShift').style.display = shift ? '' : 'none';
    $('#lastZ').innerHTML = last_z ? `Останній Z-звіт: зміна №${esc(last_z.serial ?? '?')}, ${esc((last_z.closed_at || '').replace('T', ' ').slice(0, 16))} · <a href="#" id="lastZLink">показати</a>` : '';
    if (last_z) $('#lastZLink').onclick = e => { e.preventDefault(); showReceipt(last_z.id, { report: true, title: `Z-звіт · зміна №${last_z.serial ?? ''}` }); };
  } catch (e) { $('#shiftBadge').textContent = 'Зміна: помилка'; toast(e.message, true); }
}
$('#openShift').onclick = e => busy(e.target, async () => { if (!(await openShiftNow())) throw new Error('Зміну не відкрито'); });
// --- закриття зміни: спершу пропонуємо винести готівку (за замовчуванням — усю) ---
let clsCashKop = 0, clsBtn = null;
async function doCloseShift(btn, outKop) {
  await busy(btn, async () => {
    if (outKop > 0) {
      try { await api('/api/service', { value: -outKop }); }
      catch (e) { throw new Error('Готівку не винесено, зміну не закрито: ' + e.message); }
      toast(`Винесено ${uah(outKop)} ₴`);
    }
    const r = await api('/api/shift/close', {});
    toast('Зміну закрито' + (outKop > 0 ? ` · винесено ${uah(outKop)} ₴` : '') + syncSummary(r.sync),
          !!(r.sync && (r.sync.sync_error || r.sync.push_error)));
    await loadShift(); loadGoods().catch(() => {});
    if (r.z_report_id) showReceipt(r.z_report_id, { report: true, title: `Z-звіт · зміна №${r.shift.serial ?? ''}` });
  });
}
function renderCls() {
  const v = kopOf($('#clsSum').value);
  const bad = v == null || v <= 0 || v > clsCashKop;
  $('#clsSum').classList.toggle('bad', bad && $('#clsSum').value.trim() !== '');
  $('#clsLeft').textContent = v != null && v > clsCashKop ? `більше, ніж є в касі (${uah(clsCashKop)} ₴)`
    : v != null && v > 0 ? `у касі лишиться ${uah(clsCashKop - v)} ₴` : '';
  $('#clsOut').disabled = bad;
  $('#clsOut').textContent = bad ? 'Винести й закрити' : `Винести ${uah(v)} ₴ і закрити`;
}
const closeCls = () => $('#closeModal').classList.remove('show');
$('#closeShift').onclick = async e => {
  clsBtn = e.target;
  const pend = pendingCount ? `Перед закриттям у Checkbox буде відправлено невідправлені зміни товарів: ${pendingCount}.` : '';
  let cash = null;
  try { const { shift } = await api('/api/shift?fresh=1'); cash = shift && shift.balance ? shift.balance.balance : null; }
  catch (err) { return toast(err.message, true); }
  if (!(cash > 0)) {      // готівки немає (або Checkbox не повідомив залишок) — звичайне підтвердження
    if (!(await ask('Закрити зміну і сформувати Z-звіт?' + (pend ? '\n\n' + pend : ''), { title: 'Закриття зміни', ok: 'Закрити зміну', danger: true, icon: '🧾' }))) return;
    return doCloseShift(clsBtn, 0);
  }
  clsCashKop = cash;
  $('#clsCash').textContent = uah(cash) + ' ₴';
  $('#clsSum').value = (cash / 100).toFixed(2);
  $('#clsPending').textContent = pend;
  renderCls();
  $('#closeModal').classList.add('show');
  setTimeout(() => { $('#clsSum').focus(); $('#clsSum').select(); }, 0);
};
$('#clsSum').oninput = renderCls;
$('#clsAll').onclick = () => { $('#clsSum').value = (clsCashKop / 100).toFixed(2); renderCls(); $('#clsSum').focus(); };
$('#clsX').onclick = closeCls; $('#clsCancel').onclick = closeCls;
$('#closeModal').onclick = e => { if (e.target.id === 'closeModal') closeCls(); };
$('#closeModal').onkeydown = e => {
  if (e.key === 'Escape') closeCls();
  if (e.key === 'Enter' && e.target.id === 'clsSum' && !$('#clsOut').disabled) $('#clsOut').click();
};
$('#clsOut').onclick = () => { const v = kopOf($('#clsSum').value); if (!(v > 0 && v <= clsCashKop)) return renderCls(); closeCls(); doCloseShift(clsBtn, v); };
$('#clsNoOut').onclick = () => { closeCls(); doCloseShift(clsBtn, 0); };
$('#xReport').onclick = e => busy(e.target, async () => {
  const r = await api('/api/xreport', {});
  showReceipt(r.report_id, { report: true, title: 'X-звіт' });
});
// --- службове внесення / винесення ---
async function service(sign, btn) {
  const raw = $('#svcSum').value.trim().replace(',', '.');
  const kop = Math.round(parseFloat(raw) * 100);
  if (!raw || !(kop > 0) || !/^\d+(\.\d{1,2})?$/.test(raw)) { toast('Вкажіть суму, напр. 500 або 125.50', true); return $('#svcSum').focus(); }
  const what = sign > 0 ? 'Внесення' : 'Винесення';
  if (!(await ask(`${what} ${uah(kop)} ₴ готівки?`, { title: what + ' готівки', ok: what, icon: sign > 0 ? '➕' : '➖' }))) return;
  await busy(btn, async () => {
    let r;
    try { r = await api('/api/service', { value: sign * kop }); }
    catch (e) {
      if (e.code !== 'SHIFT_CLOSED' || !(await askOpenShift('провести ' + what.toLowerCase()))) throw e;
      if (!(await openShiftNow())) throw e;
      r = await api('/api/service', { value: sign * kop });
    }
    $('#svcSum').value = '';
    toast(`${what}: ${uah(kop)} ₴`);
    await loadShift();
    showReceipt(r.id, { title: `Службовий чек · ${what.toLowerCase()}` });
  });
}
$('#svcIn').onclick = e => service(1, e.target);
$('#svcOut').onclick = e => service(-1, e.target);
$('#svcSum').onkeydown = e => { if (e.key === 'Enter') $('#svcIn').click(); };

// --- журнал ---
// Журнал: чеки за 7 днів, згруповані по днях у розгортуваних списках
const WEEKDAYS = ['неділя', 'понеділок', 'вівторок', 'середа', 'четвер', "п'ятниця", 'субота'];
function dayTitle(day) {           // day — «РРРР-ММ-ДД»
  const d = new Date(day + 'T00:00:00'), today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.round((today - d) / 86400000);
  const date = d.toLocaleDateString('uk-UA', { day: 'numeric', month: 'long' });
  return diff === 0 ? `Сьогодні, ${date}` : diff === 1 ? `Вчора, ${date}` : `${WEEKDAYS[d.getDay()]}, ${date}`;
}
const rtype = s => (s.receipt_type || 'SELL').toUpperCase();
const isReturn = s => rtype(s).includes('RETURN');
const isService = s => rtype(s).startsWith('SERVICE');
const KINDS = { SELL: ['', 'Продаж'], RETURN: ['ret', 'Повернення'], SERVICE_IN: ['in', 'Внесення'], SERVICE_OUT: ['out', 'Винесення'],
                PREPAYMENT: ['pre', 'Передплата'], POSTPAYMENT: ['pre', 'Післяплата'] };
function payTitle(s) {
  if (isService(s)) return 'Готівка';
  const base = s.payment_type === 'CASH' ? 'Готівка' : s.payment_type ? 'Картка' : '—';
  return s.payment_label ? `${esc(s.payment_label)} <small class="muted">(${s.payment_type === 'CASH' ? 'готівка' : 'безготівка'})</small>` : base;
}
let openDays = null;    // які дні й зміни розгорнуті (щоб оновлення списку їх не згортало)
const hm = t => (t || '').slice(11, 16);
const dm = t => t ? `${t.slice(8, 10)}.${t.slice(5, 7)} ${hm(t)}` : '';
function totals(list) {
  const sales = list.filter(x => !isReturn(x) && !isService(x));
  const sum = t => sales.filter(x => t ? x.payment_type === t : true).reduce((a, x) => a + (x.total || 0), 0);
  const of = k => list.filter(x => rtype(x) === k).reduce((a, x) => a + (x.total || 0), 0);
  const ret = list.filter(isReturn).reduce((a, x) => a + (x.total || 0), 0);
  const sin = of('SERVICE_IN'), sout = of('SERVICE_OUT');
  return `чеків: ${list.length} · продажі ${uah(sum())} ₴
    <small class="muted">(готівка ${uah(sum('CASH'))}, безготівка ${uah(sum('CASHLESS'))})</small>${ret ? ` · повернення ${uah(ret)} ₴` : ''}` +
    (sin || sout ? ` <small class="muted">· внесено ${uah(sin)} · винесено ${uah(sout)}</small>` : '');
}
function shiftTitle(sh, day) {
  if (!sh.id) return 'Зміна невідома <small class="muted">(завантажте чеки з Checkbox, щоб підтягнути зміни)</small>';
  // час показуємо з датою, якщо зміна почалась або закінчилась не в цей день
  const fmt = t => t && t.slice(0, 10) !== day ? dm(t) : hm(t);
  const span = sh.opened ? `${fmt(sh.opened)} – ${sh.closed ? fmt(sh.closed) : 'відкрита'}` : '';
  const z = zByShift[sh.id];
  const zl = z ? ` · <a href="#" class="zlink" data-zid="${esc(z.id)}" data-no="${esc(sh.serial || '')}">Z-звіт</a>` : '';
  return `Зміна №${esc(sh.serial || '?')}${span ? ` <small class="muted">· ${span}</small>` : ''}${zl}`;
}
function receiptRows(list) {
  return `<table><thead><tr><th>Час</th><th>Тип</th><th>Оплата</th><th class="num">Сума, ₴</th><th>Фіскальний №</th><th></th></tr></thead><tbody>
    ${list.map(s => { const k = KINDS[rtype(s)] || ['', rtype(s)]; return `<tr class="click" data-id="${esc(s.receipt_id)}" data-type="${esc(rtype(s))}">
      <td>${esc(hm(s.created_at))}</td>
      <td><span class="kind ${k[0]}">${esc(k[1])}</span></td>
      <td>${payTitle(s)}</td>
      <td class="num">${isReturn(s) || rtype(s) === 'SERVICE_OUT' ? '−' : rtype(s) === 'SERVICE_IN' ? '+' : ''}${uah(s.total)}</td>
      <td>${esc(s.fiscal_code || '—')}</td>
      <td class="muted" style="font-size:11px">${s.source === 'checkbox' ? 'з Checkbox' : 'з каси'}</td></tr>`; }).join('')}
    </tbody></table>`;
}
let zByShift = {};
async function loadZ() {
  try {
    const { reports } = await api('/api/zreports');
    zByShift = {};
    reports.forEach(z => { if (z.shift_id) zByShift[z.shift_id] = z; });
    const money = v => v == null ? '—' : uah(v) + ' ₴';
    $('#zStat').textContent = reports.length ? `збережено: ${reports.length}` : '';
    $('#zTable').innerHTML = reports.map(z => `<tr data-z="${esc(z.id)}" data-no="${esc(z.shift_no ?? '')}" style="cursor:pointer">
      <td>${esc((z.created_at || '').replace('T', ' ').slice(0, 16)) || '—'}</td><td>${z.shift_no != null ? '№' + esc(z.shift_no) : '—'}</td>
      <td>${esc(z.serial ?? '—')}</td><td>${esc(z.fiscal_code || '—')}</td><td class="num">${money(z.sell_sum)}</td>
      <td class="num">${money(z.return_sum)}</td><td class="num">${z.sell_count != null ? esc(z.sell_count + (z.return_count || 0)) : '—'}</td>
      <td class="num">${money(z.balance)}</td></tr>`).join('')
      || '<tr><td colspan="8" class="muted">Z-звітів ще немає</td></tr>';
  } catch (e) { toast(e.message, true); }
}
$('#zTable').onclick = e => {
  const tr = e.target.closest('tr[data-z]'); if (!tr) return;
  showReceipt(tr.dataset.z, { report: true, title: `Z-звіт · зміна №${tr.dataset.no || ''}` });
};
async function loadSales() {
  await loadZ();
  try {
    const r = await api('/api/sales');
    $('#fetchedAt').textContent = r.fetched_at ? 'Завантажено з Checkbox: ' + r.fetched_at.replace('T', ' ').slice(0, 16) : '';
    const byDay = {};
    for (const s of r.sales) (byDay[s.created_at.slice(0, 10)] = byDay[s.created_at.slice(0, 10)] || []).push(s);
    const days = Object.keys(byDay).sort().reverse();
    if (openDays === null) {           // спершу розгорнуто найсвіжіший день і його найсвіжішу зміну
      openDays = new Set(days.slice(0, 1));
      if (days.length) openDays.add(days[0] + '|' + (byDay[days[0]][0].shift_id || ''));
    }
    $('#salesDays').innerHTML = days.map(day => {
      const list = byDay[day];
      // зміни дня в порядку останнього чека (список чеків уже відсортований від нових)
      const shifts = [];
      for (const s of list) {
        let sh = shifts.find(x => x.id === (s.shift_id || ''));
        if (!sh) shifts.push(sh = { id: s.shift_id || '', serial: s.shift_serial, opened: s.shift_opened_at, closed: s.shift_closed_at, list: [] });
        sh.list.push(s);
      }
      const inner = shifts.map(sh => {
        const key = day + '|' + sh.id;
        return `<details class="shift" data-key="${esc(key)}" ${openDays.has(key) ? 'open' : ''}>
          <summary><span>${shiftTitle(sh, day)}</span><span class="daysum">${totals(sh.list)}</span></summary>
          ${receiptRows(sh.list)}</details>`;
      }).join('');
      return `<details class="day" data-key="${day}" ${openDays.has(day) ? 'open' : ''}>
        <summary><b>${dayTitle(day)}</b><span class="daysum">${shifts.length > 1 ? `змін: ${shifts.length} · ` : ''}${totals(list)}</span></summary>
        <div class="shifts">${inner}</div></details>`;
    }).join('') || '<p class="muted">За останні 7 днів чеків немає. Натисніть «Завантажити з Checkbox».</p>';
  } catch (e) { toast(e.message, true); }
}
$('#salesDays').addEventListener('toggle', e => {
  const d = e.target; if (!d.dataset || !d.dataset.key || !openDays) return;
  d.open ? openDays.add(d.dataset.key) : openDays.delete(d.dataset.key);
}, true);
$('#salesDays').onclick = e => {
  const zl = e.target.closest('.zlink');
  if (zl) { e.preventDefault(); return showReceipt(zl.dataset.zid, { report: true, title: `Z-звіт · зміна №${zl.dataset.no}` }); }
  const tr = e.target.closest('tr[data-id]'); if (!tr) return;
  const k = KINDS[tr.dataset.type];
  showReceipt(tr.dataset.id, { returnable: tr.dataset.type === 'SELL', title: k ? `Чек · ${k[1].toLowerCase()}` : 'Чек' });
};
$('#fetchReceipts').onclick = e => busy(e.target, async () => {
  const r = await api('/api/sales/fetch', {});
  toast(`З Checkbox отримано чеків: ${r.fetched}, нових у журналі: ${r.new}` + (r.removed ? ` · видалено старих: ${r.removed}` : '')
    + (r.z_error ? ` · Z-звіти не завантажено: ${r.z_error}` : r.z_new ? ` · нових Z-звітів: ${r.z_new}` : ''), !!r.z_error);
  await loadSales();
});

// --- повернення за чеком продажу (ті самі правила суми, що й return_amount() у checkbox_app.py) ---
let ret = null;    // { info, qty: {line: тисячні}, on: Set }
const shareOf = (t, part, whole) => whole > 0 ? Math.floor((t * part * 2 + whole) / (2 * whole)) : 0;
const retAmount = (ln, q) => { const before = ln.qty - ln.max; return shareOf(ln.total, before + q, ln.qty) - shareOf(ln.total, before, ln.qty); };
const STD_LABELS = ['Готівка', 'Картка', 'Безготівка'];
async function openReturn(id) {
  let info;
  try { info = await api('/api/receipt?id=' + encodeURIComponent(id)); } catch (e) { return toast(e.message, true); }
  if (!info.lines.some(l => l.max > 0)) return toast('За цим чеком усі товари вже повернуто', true);
  ret = { info, qty: {}, on: new Set() };
  $('#retPhone').value = '';
  info.lines.forEach(l => { if (l.max > 0) { ret.qty[l.line] = l.max; ret.on.add(l.line); } });
  $('#retFn').textContent = info.fiscal_code || info.id.slice(0, 8);
  const payWas = info.payment_type === 'CASH' ? 'готівка' : 'безготівка';
  $('#retInfo').textContent = `Продаж ${info.created_at.replace('T', ' ').slice(0, 16)} · ${uah(info.total_sum)} ₴ · ${info.payment_label || payWas}` +
    (info.payments > 1 ? ' (кілька оплат — повернення однією)' : '');
  setRadio('retType', info.payment_type === 'CASHLESS' ? 'CASHLESS' : 'CASH');
  renderReturn();
  $('#retModal').classList.add('show');
}
function retTotalNow() {
  return ret.info.lines.reduce((a, l) => a + (ret.on.has(l.line) ? retAmount(l, ret.qty[l.line] || 0) : 0), 0);
}
function renderReturn() {
  const { info } = ret;
  $('#retLines').innerHTML = info.lines.map(l => {
    const off = l.max <= 0, on = ret.on.has(l.line), q = ret.qty[l.line] || 0;
    const sold = qtyStr(l.qty, l.is_weight) + (l.returned ? `<br><small class="muted">повернуто ${qtyStr(l.returned, l.is_weight)}</small>` : '');
    return `<tr class="${off || !on ? 'off' : ''}">
      <td><input type="checkbox" data-ron="${l.line}" ${on ? 'checked' : ''} ${off ? 'disabled' : ''}></td>
      <td>${esc(l.name)}<br><small class="muted">${uah(l.price)} ₴${l.total !== lineSum(l.price, l.qty) ? ` · у чеку ${uah(l.total)} ₴ зі знижкою` : ''}</small></td>
      <td class="num">${sold}</td>
      <td class="num">${off ? '—' : `<input type="number" min="0" max="${l.max / 1000}" step="${l.is_weight ? '0.001' : '1'}" value="${q / 1000}" data-rq="${l.line}" ${on ? '' : 'disabled'}>`}</td>
      <td class="num">${on && q ? uah(retAmount(l, q)) : '—'}</td></tr>`;
  }).join('');
  const total = retTotalNow(), type = radioVal('retType');
  const pay = type === 'CASH' ? roundCash(total, info.cash_rounding) : total;
  $('#retTotal').textContent = uah(pay) + ' ₴';
  $('#retRound').textContent = pay !== total ? `Заокруглення готівки: ${uah(total)} → ${uah(pay)} ₴` : '';
  const box = $('#retCardBox');
  box.style.display = type === 'CASHLESS' ? '' : 'none';
  const c = info.card || {}, viaTerm = canRefundViaTerminal();
  ret.pay = pay;
  const cardLine = Object.keys(c).length ? `<br><small>Картка продажу: ${esc([c.card_mask, c.payment_system, c.rrn && 'RRN ' + c.rrn, c.bank_name].filter(Boolean).join(' · '))}</small>` : '';
  box.innerHTML = info.payment_type !== 'CASHLESS'
    ? 'Продаж був готівкою. Безготівкове повернення — без даних картки.'
    : viaTerm
      ? `<label class="chk"><input type="checkbox" id="retViaTerm" ${ret.viaTerm !== false ? 'checked' : ''}> Повернути ${uah(pay)} ₴ на картку через термінал, потім пробити чек</label>` +
        `<small>Зніміть позначку, якщо повернення вже проведено на терміналі вручну.</small>` + cardLine
      : `Спершу проведіть повернення ${uah(pay)} ₴ на платіжному терміналі, потім пробийте чек.` + cardLine;
  $('#retSubmit').textContent = type === 'CASHLESS' && viaTerm && ret.viaTerm !== false ? 'Повернути через термінал' : 'Пробити повернення';
}
const canRefundViaTerminal = () => !!(termCfg.enabled && termCfg.device_id && ret && ret.info.payment_type === 'CASHLESS');
$('#retCardBox').onchange = e => { if (e.target.id === 'retViaTerm') { ret.viaTerm = e.target.checked; renderReturn(); } };
$('#retLines').onchange = e => {
  const on = e.target.dataset.ron, rq = e.target.dataset.rq;
  if (on !== undefined) e.target.checked ? ret.on.add(+on) : ret.on.delete(+on);
  if (rq !== undefined) {
    const l = ret.info.lines.find(x => x.line === +rq);
    let q = Math.round(parseFloat(e.target.value.replace(',', '.')) * 1000) || 0;
    if (!l.is_weight) q = Math.round(q / 1000) * 1000;
    if (q > l.max) { q = l.max; toast(`Можна повернути не більше ${qtyStr(l.max, l.is_weight)}`, true); }
    if (q <= 0) { ret.on.delete(l.line); q = l.max; }
    ret.qty[l.line] = q;
  }
  renderReturn();
};
document.querySelectorAll('input[name="retType"]').forEach(r => r.onchange = renderReturn);
function closeReturn() { $('#retModal').classList.remove('show'); ret = null; }
$('#retClose').onclick = closeReturn; $('#retCancel').onclick = closeReturn;
$('#retModal').onkeydown = e => { if (e.key === 'Escape') closeReturn(); };
async function sendReturn(body, opts = {}) {     // пробиває чек повернення; повертає відповідь сервера
  let r;
  try { r = await api('/api/return', body); }
  catch (err) {
    if (err.code !== 'SHIFT_CLOSED' || opts.noShiftPrompt || !(await askOpenShift('пробити повернення'))) throw err;
    if (!(await openShiftNow())) throw err;
    r = await api('/api/return', body);
  }
  closeReturn();
  toast(`Повернення пробито: ${uah(r.total_sum)} ₴`);
  loadSales(); loadGoods().catch(() => {}); loadShift();
  showReceipt(r.id, { title: 'Чек · повернення' });
  return r;
}
$('#retSubmit').onclick = async e => {
  const { info } = ret, type = radioVal('retType');
  const items = info.lines.filter(l => ret.on.has(l.line) && ret.qty[l.line] > 0).map(l => ({ line: l.line, qty: ret.qty[l.line] }));
  if (!items.length) return toast('Оберіть товари для повернення', true);
  const body = { id: info.id, items, payment_type: type };
  try { const ph = phoneOf('#retPhone'); if (ph) body.phone = ph; } catch (err) { return toast(err.message, true); }
  if (type === info.payment_type && info.payment_label && !STD_LABELS.includes(info.payment_label)) body.label = info.payment_label;
  const origCard = type === 'CASHLESS' && info.payment_type === 'CASHLESS' && Object.keys(info.card || {}).length ? info.card : null;
  if (origCard) body.card = origCard;
  const pay = ret.pay;
  if (type === 'CASHLESS' && canRefundViaTerminal() && ret.viaTerm !== false) {
    // POSSrv «Відміна транзакції / Повернення коштів»: сума і реквізити транзакції продажу
    const tr = info.terminal || {};
    if (!tr.rrn) return toast('Немає RRN продажу — без нього термінал не поверне кошти. Зніміть позначку «через термінал» і проведіть повернення на терміналі вручну.', true);
    if (!(await ask(`Повернути ${uah(pay)} ₴ на картку через термінал і пробити чек повернення?\n\nКартка продажу: ${[info.card && info.card.card_mask, tr.rrn && 'RRN ' + tr.rrn].filter(Boolean).join(' · ')}`,
                    { title: 'Повернення на картку', ok: `Повернути ${uah(pay)} ₴`, danger: true, icon: '💳' }))) return;
    // головне для повернення — RRN продажу та id пристрою
    const payload = { amount: pay, id: termCfg.device_id, rrn: tr.rrn, ...(tr.invoicenum ? { invoicenum: tr.invoicenum } : {}),
                      ...(tr.auth_code ? { auth_code: tr.auth_code } : {}) };
    term = { amount: pay, messages: [], op: {
      cmd: 'cancel', refund: true, payload, back: () => { if (ret) $('#retModal').classList.add('show'); },
      fiscalize: async (card, raw) => {
        const b = { ...body, terminal_raw: raw };
        if (card && Object.keys(card).length) b.card = { ...(origCard || {}), ...card };   // дані операції повернення
        try { await sendReturn(b, { noShiftPrompt: true }); return true; }
        catch (err) { toast(err.message, true); return false; }
      } } };
    $('#retModal').classList.remove('show');
    showTermModal();
    return;
  }
  if (!(await ask(`Пробити чек повернення на ${uah(pay)} ₴?` + (type === 'CASHLESS' ? '\n\nПовернення на терміналі вже проведено?' : '\n\nВидайте покупцю готівку з каси.'),
                  { title: 'Чек повернення', ok: 'Пробити повернення', danger: true, icon: '↩️' }))) return;
  await busy(e.target, () => sendReturn(body));
};

// --- передплата / післяплата ---
const kopOf = v => { const t = String(v || '').trim().replace(',', '.'); return /^\d+(\.\d{1,2})?$/.test(t) ? Math.round(parseFloat(t) * 100) : null; };
async function withShift(fn, what) {      // виконати; якщо зміна закрита — запропонувати відкрити і повторити
  try { return await fn(); }
  catch (e) {
    if (e.code !== 'SHIFT_CLOSED' || !(await askOpenShift(what))) throw e;
    if (!(await openShiftNow())) throw e;
    return await fn();
  }
}
const useTerminal = () => !!(termCfg.enabled && termCfg.device_id);
// Безготівка через термінал: спершу оплата на терміналі, потім чек (send(card, raw) → bool)
function payViaTerminal(amount, send, back) {
  term = { amount, messages: [], op: { cmd: 'purchase', payload: { amount }, back, fiscalize: async (card, raw) => {
    try { await send(card, raw); return true; } catch (e) { toast(e.message, true); return false; } } } };
  showTermModal();
}
// нове замовлення з кошика
async function openPre() {
  if (!cart.length) return toast('Кошик порожній', true);
  const de = discountError(); if (de) return toast('Виправте знижку: ' + de, true);
  if (!(await confirmMarks())) return;
  $('#preTotal').textContent = uah(cartTotal) + ' ₴';
  $('#preAmount').value = ''; $('#preNote').value = '';
  renderPre();
  $('#preModal').classList.add('show'); $('#preAmount').focus();
}
function renderPre() {
  const a = kopOf($('#preAmount').value);
  $('#preLeft').textContent = a == null ? '' : a >= cartTotal ? '⚠ Внесок має бути меншим за суму замовлення' : `Залишок до сплати: ${uah(cartTotal - a)} ₴`;
  $('#preLeft').className = a != null && a >= cartTotal ? 'zero' : 'muted';
}
const closePre = () => $('#preModal').classList.remove('show');
$('#payPre').onclick = openPre;
$('#preAmount').oninput = renderPre;
$('#preClose').onclick = closePre; $('#preCancel').onclick = closePre;
$('#preModal').onkeydown = e => { if (e.key === 'Enter') $('#preSubmit').click(); if (e.key === 'Escape') closePre(); };
$('#preSubmit').onclick = async e => {
  const amount = kopOf($('#preAmount').value);
  if (!(amount > 0)) { toast('Вкажіть суму внеску, напр. 200 або 150.50', true); return $('#preAmount').focus(); }
  if (amount >= cartTotal) { toast('Внесок має бути меншим за суму замовлення — інакше це звичайний продаж', true); return $('#preAmount').focus(); }
  const type = radioVal('preType');
  const body = { items: cart, payment_type: type, amount, note: $('#preNote').value.trim() };
  if (cartDisc) body.discount = cartDisc;
  try { const ph = phoneOf('#phone'); if (ph) body.phone = ph; } catch (err) { closePre(); return toast(err.message, true); }
  const done = r => {
    cart = []; cartDisc = null; renderCart(); closePre(); $('#phone').value = ''; renderPhone();
    toast(`Передплату пробито: ${uah(r.amount)} ₴, залишок ${uah(r.left)} ₴ (замовлення ${r.relation_id})`);
    showReceipt(r.id, { title: `Чек передплати · замовлення ${r.relation_id}` });
  };
  if (type === 'CASHLESS' && useTerminal()) {
    if (!(await shiftIsOpen())) return toast('Спершу відкрийте зміну — інакше гроші спишуться, а чек не проб\'ється', true);
    closePre();
    return payViaTerminal(amount, async (card, raw) => done(await api('/api/prepay', { ...body, card, terminal_raw: raw })),
                          () => $('#preModal').classList.add('show'));
  }
  await busy(e.target, async () => done(await withShift(() => api('/api/prepay', body), 'пробити передплату')));
};

// список замовлень
let prepayList = [], prepayRounding = null, openChains = new Set();
const PRE_STATUS = { OPEN: ['pre', 'Очікує доплати'], PAID: ['in', 'Сплачено'], RETURNED: ['ret', 'Повернуто'] };
async function loadPrepayments() {
  try {
    const r = await api('/api/prepayments');
    prepayList = r.prepayments; prepayRounding = r.cash_rounding;
    $('#prepayTable').innerHTML = prepayList.map(p => {
      const st = PRE_STATUS[p.status] || ['', p.status], pct = p.total ? Math.round(p.paid * 100 / p.total) : 0;
      const goodsTxt = p.items.map(i => i.name).join(', ');
      const receipts = openChains.has(p.relation_id) ? `<ul class="chain-receipts">${p.receipts.map(c =>
        `<li><a href="#" data-receipt="${esc(c.id)}">${(KINDS[c.kind] || ['', c.kind])[1]} ${uah(c.value)} ₴</a> <small class="muted">${esc(dm(c.at))} · ${c.payment_type === 'CASH' ? 'готівка' : 'безготівка'} · ФН ${esc(c.fiscal_code || '—')}</small></li>`).join('')}</ul>` : '';
      return `<tr>
        <td>${esc(dm(p.created_at))}</td>
        <td><b>${esc(p.note || 'Замовлення')}</b> <small class="muted">№ ${esc(p.relation_id)}</small><br><small class="muted">${esc(goodsTxt.length > 90 ? goodsTxt.slice(0, 90) + '…' : goodsTxt)}</small>
          <br><a href="#" data-chain="${esc(p.relation_id)}" style="font-size:12px">Чеки (${p.receipts.length}) ${openChains.has(p.relation_id) ? '▴' : '▾'}</a>${receipts}</td>
        <td class="num">${uah(p.total)}</td>
        <td class="num">${uah(p.paid)}<div class="prebar"><i style="width:${pct}%"></i></div></td>
        <td class="num">${p.status === 'OPEN' ? '<b>' + uah(p.left) + '</b>' : '—'}</td>
        <td><span class="kind ${st[0]}">${st[1]}</span></td>
        <td style="white-space:nowrap">${p.status === 'OPEN' ? `<button class="btn small other" data-pay="${esc(p.relation_id)}">Доплатити</button> ` : ''}
          ${p.status !== 'RETURNED' ? `<button class="btn small sec" data-ret="${esc(p.relation_id)}">Повернути</button> ` : ''}
          <button class="btn small sec" data-state="${esc(p.relation_id)}" title="Стан замовлення в Checkbox">ⓘ</button></td></tr>`;
    }).join('') || '<tr><td colspan="7" class="muted">Замовлень із передплатою ще немає.</td></tr>';
  } catch (e) { toast(e.message, true); }
}
$('#prepayTable').onclick = async e => {
  const a = e.target.closest('[data-chain],[data-receipt],[data-pay],[data-ret],[data-state]'); if (!a) return;
  e.preventDefault();
  const d = a.dataset;
  if (d.chain) { openChains.has(d.chain) ? openChains.delete(d.chain) : openChains.add(d.chain); return loadPrepayments(); }
  if (d.receipt) return showReceipt(d.receipt, { title: 'Чек замовлення' });
  if (d.pay) return openPost(d.pay);
  if (d.state) return busy(a, async () => { showPrepayState(await api('/api/prepay/state?id=' + encodeURIComponent(d.state))); loadPrepayments(); });
  if (d.ret) {
    const p = prepayList.find(x => x.relation_id === d.ret);
    const card = p.receipts.some(c => c.payment_type === 'CASHLESS');
    if (!(await ask(`Повернути все замовлення «${p.note || p.relation_id}»?\n\nCheckbox пробиє чек повернення на кожен чек замовлення (${p.receipts.length}), разом ${uah(p.paid)} ₴.` +
                 (card ? '\nБезготівкові частини поверніть покупцю на терміналі окремо.' : '') + '\nЧастково замовлення не повертається.', { title: 'Повернення замовлення', ok: 'Повернути замовлення', danger: true, icon: '↩️' }))) return;
    return busy(a, async () => {
      const r = await withShift(() => api('/api/prepay/return', { relation_id: p.relation_id }), 'повернути замовлення');
      toast(`Замовлення повернуто: ${uah(r.paid)} ₴` + (r.receipts.length ? `, чеків повернення: ${r.receipts.length}` : ' — чеки повернення в журналі після «Завантажити з Checkbox»'));
      loadPrepayments(); loadShift();
    });
  }
};

// стан замовлення в Checkbox — коротко, для касира
const PS_STATUS = { OPEN: ['pre', 'Очікує доплати'], PAID: ['in', 'Сплачено повністю'], RETURNED: ['ret', 'Повернуто'] };
const PAY_NAME = { CASH: 'Готівка', CASHLESS: 'Картка / безготівка', MIXED: 'Змішана' };
let psRelation = null;
function showPrepayState(r) {
  const s = r.summary;
  psRelation = s.relation_id;
  $('#psTitle').textContent = `${s.note || 'Замовлення'} · № ${s.relation_id}`;
  const st = PS_STATUS[s.status] || ['', s.status_raw || 'невідомо'];
  $('#psSub').innerHTML = `<span class="kind ${st[0]}">${esc(st[1])}</span>` +
    (s.created_at ? ` <span class="muted">· створено ${esc(dm(s.created_at))}</span>` : '') +
    (s.returned_at ? ` <span class="muted">· повернуто ${esc(dm(s.returned_at))}</span>` : '');
  const cashPart = s.paid_cash != null && s.paid ? `<small>готівкою ${uah(s.paid_cash)}, безготівково ${uah(s.paid - s.paid_cash)}</small>` : '';
  $('#psSums').innerHTML = `<div><span>Сума замовлення</span><b>${uah(s.total)} ₴</b></div>
    <div><span>Сплачено</span><b>${uah(s.paid)} ₴</b>${cashPart}</div>
    <div><span>Залишок до сплати</span><b>${s.status === 'RETURNED' ? '—' : uah(s.left) + ' ₴'}</b></div>`;
  $('#psBar').style.width = (s.total ? Math.min(100, Math.round((s.paid || 0) * 100 / s.total)) : 0) + '%';
  $('#psGoods').innerHTML = s.goods.map(g => `<tr><td>${esc(g.name)}</td><td class="num">${qtyStr(g.qty, g.is_weight)}</td>
    <td class="num">${uah(g.sum)} ₴</td></tr>`).join('') || '<tr><td class="muted">Товари не вказано у відповіді</td></tr>';
  $('#psReceipts').innerHTML = s.receipts.map(c => {
    const k = KINDS[c.kind] || ['', c.kind];
    return `<tr class="click" data-id="${esc(c.id)}"><td><span class="kind ${k[0]}">${esc(k[1])}</span></td>
      <td>${c.at ? esc(dm(c.at)) : '—'}</td><td>${esc(c.label || PAY_NAME[c.payment] || '—')}</td>
      <td class="num">${c.sum != null ? (c.kind === 'RETURN' ? '−' : '') + uah(c.sum) : '—'}</td>
      <td>${esc(c.fiscal_code || (c.serial != null ? '№ ' + c.serial : '—'))}</td></tr>`;
  }).join('') || '<tr><td colspan="5" class="muted">Чеків немає</td></tr>';
  $('#psRaw').textContent = JSON.stringify(r.state, null, 2);
  $('#psPay').style.display = s.status === 'OPEN' ? '' : 'none';
  $('#psOk').parentElement.style.gridTemplateColumns = s.status === 'OPEN' ? '' : '1fr';
  $('#psModal').classList.add('show');
}
const closePs = () => $('#psModal').classList.remove('show');
$('#psClose').onclick = closePs; $('#psOk').onclick = closePs;
$('#psModal').onclick = e => { if (e.target.id === 'psModal') closePs(); };
$('#psModal').onkeydown = e => { if (e.key === 'Escape') closePs(); };
$('#psReceipts').onclick = e => { const tr = e.target.closest('tr[data-id]'); if (tr) showReceipt(tr.dataset.id, { title: 'Чек замовлення' }); };
$('#psPay').onclick = async () => { closePs(); await loadPrepayments(); if (prepayList.some(p => p.relation_id === psRelation)) openPost(psRelation); };

// доплата: менше за залишок — ще одна передплата; весь залишок — післяплата, що закриває замовлення
let post = null;
function openPost(id) {
  const p = prepayList.find(x => x.relation_id === id);
  post = p;
  $('#postTitle').textContent = `Доплата · ${p.note || 'замовлення ' + p.relation_id}`;
  $('#postInfo').textContent = `Сума замовлення ${uah(p.total)} ₴ · сплачено ${uah(p.paid)} ₴ · залишок ${uah(p.left)} ₴`;
  $('#postAmount').value = (p.left / 100).toFixed(2); $('#postReceived').value = ''; $('#postPhone').value = '';
  renderPost();
  $('#postModal').classList.add('show'); $('#postAmount').focus(); $('#postAmount').select();
}
function postCalc() {
  const amount = kopOf($('#postAmount').value), type = radioVal('postType');
  const closing = amount === post.left;
  const value = closing && type === 'CASH' ? roundCash(amount, prepayRounding) : amount;
  const recv = type === 'CASH' && closing ? kopOf($('#postReceived').value) : null;
  return { amount, type, closing, value, recv };
}
function renderPost() {
  const c = postCalc(), box = $('#postCalc');
  $('#postRecvWrap').style.visibility = c.type === 'CASH' && c.closing ? '' : 'hidden';
  $('#postSubmit').textContent = c.amount != null && c.amount < post.left ? 'Пробити передплату' : 'Пробити післяплату';
  if (!(c.amount > 0)) { box.className = 'status bad'; box.textContent = 'Вкажіть суму, напр. 300 або 125.50'; return; }
  if (c.amount > post.left) { box.className = 'status bad'; box.textContent = `Більше залишку (${uah(post.left)} ₴)`; return; }
  box.className = 'status ' + (c.closing ? 'ok' : 'wait');
  box.textContent = c.closing
    ? `Післяплата закриє замовлення. До сплати ${uah(c.value)} ₴` + (c.value !== c.amount ? ` (заокруглення ${uah(c.value - c.amount)})` : '') +
      (c.recv != null ? (c.recv >= c.value ? ` · решта ${uah(c.recv - c.value)} ₴` : ' · отримано недостатньо') : '')
    : `Ще одна передплата. Після неї лишиться ${uah(post.left - c.amount)} ₴`;
}
const closePost = () => { $('#postModal').classList.remove('show'); };
['#postAmount', '#postReceived'].forEach(id => $(id).oninput = renderPost);
document.querySelectorAll('input[name="postType"]').forEach(r => r.onchange = renderPost);
$('#postClose').onclick = closePost; $('#postCancel').onclick = closePost;
$('#postModal').onkeydown = e => { if (e.key === 'Enter') $('#postSubmit').click(); if (e.key === 'Escape') closePost(); };
$('#postSubmit').onclick = async e => {
  const c = postCalc();
  if (!(c.amount > 0) || c.amount > post.left) { renderPost(); return $('#postAmount').focus(); }
  if (c.recv != null && c.recv < c.value) return toast('Отримано менше, ніж до сплати', true);
  const body = { relation_id: post.relation_id, payment_type: c.type, amount: c.amount };
  if (c.recv != null) body.received = c.recv;
  try { const ph = phoneOf('#postPhone'); if (ph) body.phone = ph; } catch (err) { return toast(err.message, true); }
  const p = post;
  const done = r => {
    closePost(); loadPrepayments(); loadShift();
    toast(r.closed ? `Післяплату пробито — замовлення сплачено${r.rest ? `, решта ${uah(r.rest)} ₴` : ''}` : `Передплату пробито, залишок ${uah(r.left)} ₴`);
    showReceipt(r.id, { title: `${r.closed ? 'Чек післяплати' : 'Чек передплати'} · замовлення ${p.relation_id}` });
  };
  if (c.type === 'CASHLESS' && useTerminal()) {
    if (!(await shiftIsOpen())) return toast('Спершу відкрийте зміну — інакше гроші спишуться, а чек не проб\'ється', true);
    closePost();
    return payViaTerminal(c.value, async (card, raw) => done(await api('/api/postpay', { ...body, card, terminal_raw: raw })),
                          () => { post = p; $('#postModal').classList.add('show'); });
  }
  await busy(e.target, async () => done(await withShift(() => api('/api/postpay', body), 'пробити чек')));
};

// --- налаштування ---
let cfgCache = null;
const authModeVal = () => document.querySelector('input[name="authMode"]:checked').value;
function renderAuthMode() {
  const pin = authModeVal() === 'pin';
  $('#authPassword').style.display = pin ? 'none' : 'grid';
  $('#authPin').style.display = pin ? 'grid' : 'none';
  const has = pin ? cfgCache?.has_pin : cfgCache?.has_password;
  $('#secretHint').textContent = has ? `${pin ? 'Пін-код' : 'Пароль'} збережено — залиште поле порожнім, щоб не змінювати.` : '';
}
function renderStatus(c) {
  const st = $('#settingsStatus');
  if (c.auth_ok) { st.className = 'status ok'; st.textContent = '✔ Підключено' + (c.cashier ? ` · касир: ${c.cashier}` : ''); }
  else { st.className = 'status bad'; st.textContent = '✖ Не підключено' + (c.auth_error ? `: ${c.auth_error}` : ''); }
  $('#cashierName').textContent = c.auth_ok ? (c.cashier ? 'Касир: ' + c.cashier : '') : '⚠ Каса не підключена';
  const r = c.auth_ok && c.register;
  $('#regInfo').textContent = r && r.fiscal_number ? `ФН каси ${r.fiscal_number}` : '';
  $('#regInfo').title = r ? [r.title, r.address, r.number != null ? `№ ${r.number}` : ''].filter(Boolean).join(' · ') : '';
}
async function loadConfig() {
  cfgCache = await api('/api/config');
  renderStatus(cfgCache);
  termCfg = cfgCache.terminal || termCfg;
  $('#logPath').textContent = cfgCache.log_path || '';
  $('#termSummary').textContent = termSummaryText(termCfg);
  return cfgCache;
}
async function openSettings() {
  const c = await loadConfig();
  $('#cfgLicense').value = c.license_key;
  $('#cfgLogin').value = c.login;
  $('#cfgPassword').value = ''; $('#cfgPin').value = '';
  $('#cfgPassword').placeholder = c.has_password ? '•••••••• (збережено)' : '';
  $('#cfgPin').placeholder = c.has_pin ? '•••• (збережено)' : '';
  $('#cfgRounding').value = c.cash_rounding;
  document.querySelector(`input[name="authMode"][value="${c.auth_mode}"]`).checked = true;
  renderAuthMode();
  $('#settingsModal').classList.add('show');
  $('#cfgLicense').focus();
}
function closeSettings() { $('#settingsModal').classList.remove('show'); }
document.querySelectorAll('input[name="authMode"]').forEach(r => r.onchange = renderAuthMode);
document.querySelectorAll('.eye').forEach(b => b.onclick = () => {
  const i = document.getElementById(b.dataset.for); i.type = i.type === 'password' ? 'text' : 'password';
});
$('#settingsBtn').onclick = () => openSettings().catch(e => toast(e.message, true));
$('#settingsClose').onclick = closeSettings; $('#settingsCancel').onclick = closeSettings;
$('#settingsModal').onclick = e => { if (e.target.id === 'settingsModal') closeSettings(); };
$('#settingsModal').onkeydown = e => { if (e.key === 'Escape') closeSettings(); if (e.key === 'Enter' && e.target.tagName === 'INPUT') $('#settingsSave').click(); };
$('#settingsSave').onclick = e => busy(e.target, async () => {
  const mode = authModeVal();
  const body = {
    auth_mode: mode,
    license_key: $('#cfgLicense').value, login: $('#cfgLogin').value,
    cash_rounding: $('#cfgRounding').value,
  };
  if (mode === 'password' && $('#cfgPassword').value) body.password = $('#cfgPassword').value;
  if (mode === 'pin' && $('#cfgPin').value) body.pin_code = $('#cfgPin').value;
  try {
    const r = await api('/api/config', body);
    closeSettings();
    toast('Налаштування збережено' + (r.cashier ? `, касир: ${r.cashier}` : '') +
      (r.synced != null ? ` · синхронізовано товарів: ${r.synced}` : '') +
      (r.sync_error ? ` · ⚠ синхронізація: ${r.sync_error}` : ''));
    await loadConfig(); await loadGoods(); await loadShift();
  } catch (err) {
    // помилка лишається у вікні, щоб її було видно поруч із полями
    const st = $('#settingsStatus'); st.className = 'status bad'; st.textContent = '✖ ' + err.message;
    throw err;
  }
});

// --- налаштування терміналу ---
let termCfg = { enabled: false, url: 'http://localhost:9020' };
let termDevices = [];
const BANKS = { oschadbank: 'Ощадбанк', privatbank: 'ПриватБанк' };
function termSummaryText(t) {
  if (!t || !t.enabled || !t.device_id) return 'не налаштовано';
  return `${t.device_name || t.device_id}${t.bank_name ? ' · ' + t.bank_name : ''}`;
}
function renderTermDevices(selectedId) {
  const sel = $('#termDevice');
  if (!termDevices.length) {
    sel.innerHTML = selectedId
      ? `<option value="${esc(selectedId)}">${esc(termCfg.device_name || selectedId)} (збережений)</option>`
      : '<option value="">— у POSSrv немає терміналів, додайте нижче —</option>';
    return;
  }
  sel.innerHTML = termDevices.map(d =>
    `<option value="${esc(d.id)}">${esc(d.name || d.id)} · ${esc(BANKS[d.type] || d.type || '')}${d.connected === false ? ' · не підключено' : d.connected ? ' · підключено' : ''}</option>`).join('');
  if (selectedId && termDevices.some(d => d.id === selectedId)) sel.value = selectedId;
}
function termStatus(text, ok) {
  const st = $('#termCfgStatus'); st.style.display = text ? 'block' : 'none';
  st.className = 'status ' + (ok ? 'ok' : 'bad'); st.textContent = text || '';
}
async function refreshTermDevices(quiet) {
  try {
    const r = await api('/api/terminal/devices', { url: $('#termUrl').value });
    termDevices = r.devices;
    renderTermDevices($('#termDevice').value || termCfg.device_id);
    termStatus(`✔ POSSrv відповідає, терміналів: ${termDevices.length}`, true);
  } catch (e) {
    termDevices = []; renderTermDevices(termCfg.device_id);
    if (!quiet) termStatus('✖ ' + e.message, false); else termStatus('⚠ ' + e.message, false);
  }
}
function renderRegMode() {
  const tcp = $('#regMode').value === 'tcp';
  document.querySelectorAll('#termAdd .tcp').forEach(e => e.style.display = tcp ? '' : 'none');
  document.querySelectorAll('#termAdd .rs232').forEach(e => e.style.display = tcp ? 'none' : '');
}
async function openTermCfg() {
  termCfg = cfgCache?.terminal || termCfg;
  $('#termEnabled').checked = !!termCfg.enabled;
  $('#termUrl').value = termCfg.url || 'http://localhost:9020';
  termDevices = []; renderTermDevices(termCfg.device_id);
  termStatus('');
  $('#termCfgModal').classList.add('show');
  refreshTermDevices(true);
}
function closeTermCfg() { $('#termCfgModal').classList.remove('show'); }
$('#termCfgOpen').onclick = openTermCfg;
$('#termCfgClose').onclick = closeTermCfg; $('#termCfgCancel').onclick = closeTermCfg;
$('#termRefresh').onclick = e => busy(e.target, () => refreshTermDevices(false));
$('#regMode').onchange = renderRegMode;
$('#regSubmit').onclick = e => busy(e.target, async () => {
  const mode = $('#regMode').value;
  const body = {
    url: $('#termUrl').value, name: $('#regName').value, type: $('#regType').value, mode,
    merchant_id: $('#regMerchant').value,
    port: mode === 'tcp' ? $('#regPort').value : $('#regCom').value,
    baudrate: $('#regBaud').value, host: $('#regHost').value,
  };
  try {
    const r = await api('/api/terminal/register', body);
    termDevices = r.devices.length ? r.devices : [r.device];
    renderTermDevices(r.device && r.device.id);
    $('#termAdd').open = false;
    termStatus(`✔ Термінал «${r.device?.name || ''}» зареєстровано і збережено в POSSrv`, true);
  } catch (err) { termStatus('✖ ' + err.message, false); throw err; }
});
$('#termCfgSave').onclick = e => busy(e.target, async () => {
  const id = $('#termDevice').value;
  const dev = termDevices.find(d => d.id === id);
  try {
    const r = await api('/api/terminal/save', {
      enabled: $('#termEnabled').checked, url: $('#termUrl').value, device_id: id,
      device_name: dev ? dev.name : (id === termCfg.device_id ? termCfg.device_name : ''),
      device_type: dev ? dev.type : (id === termCfg.device_id ? termCfg.device_type : ''),
    });
    termCfg = r.terminal;
    if (cfgCache) cfgCache.terminal = termCfg;
    $('#termSummary').textContent = termSummaryText(termCfg);
    closeTermCfg();
    toast(termCfg.enabled ? 'Термінал увімкнено для оплати карткою' : 'Оплата через термінал вимкнена');
  } catch (err) { termStatus('✖ ' + err.message, false); throw err; }
});

// --- вихід ---
$('#quitBtn').onclick = async () => {
  if (!(await ask(cart.length ? 'У кошику є товари. Все одно зупинити касу?' : 'Зупинити касу? Вкладку можна буде закрити.',
                  { title: 'Вихід', ok: 'Зупинити касу', danger: !!cart.length, icon: '⏻' }))) return;
  try { await api('/api/quit', {}); } catch (e) {}
  document.body.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:8px;font:16px system-ui">
    <b style="font-size:20px">Касу зупинено</b><span style="color:var(--muted)">Цю вкладку можна закрити. Щоб почати знову — запустіть Касу ще раз.</span></div>`;
};

loadGoods().catch(e => toast(e.message, true));
async function initConnection() {
  let c = await loadConfig();
  while (c.connecting) {                       // каса ще входить у Checkbox при старті
    $('#cashierName').textContent = 'Підключаюся до Checkbox…';
    await new Promise(r => setTimeout(r, 1000));
    c = await loadConfig();
  }
  if (!c.auth_ok) openSettings();
  else { loadShift(); loadGoods().catch(() => {}); }
}
initConnection().catch(e => toast(e.message, true));

// --- оновлення з GitHub ---
let updateInfo = null;
const sleep = ms => new Promise(r => setTimeout(r, ms));
function renderUpdate(r) {
  const v = $('#verInfo'), b = $('#updateBanner');
  $('#updNowBtn').style.display = r.available ? '' : 'none';
  v.textContent = `Версія каси ${r.current}` + (r.enabled === false ? ' · оновлення не налаштовано'
    : r.error ? ` · ⚠ ${r.error}` : r.newer ? ` · доступна ${r.latest}` : r.latest ? ' · це остання версія' : '');
  const link = r.url ? ` · <a href="${esc(r.url)}" target="_blank" rel="noopener">що нового</a>` : '';
  if (r.available) {
    b.innerHTML = `<span>🎉 Доступна нова версія каси <b>${esc(r.latest)}</b> (у вас ${esc(r.current)})${link}</span>
      <button class="btn small" id="updateBtn">Оновити</button>`;
    b.style.display = ''; $('#updateBtn').onclick = runUpdate;
    $('#updNowBtn').textContent = `⬆ Оновити до ${r.latest}`;
  } else if (r.newer && r.problem) {
    b.innerHTML = `<span>ℹ Доступна версія <b>${esc(r.latest)}</b>: ${esc(r.problem)}${link}</span>`; b.style.display = '';
  } else b.style.display = 'none';
}
async function checkUpdates(force) {
  try { updateInfo = await api('/api/update/check' + (force ? '?force=1' : '')); renderUpdate(updateInfo); return updateInfo; }
  catch (e) { if (force) toast(e.message, true); }
}
$('#checkUpdBtn').onclick = e => busy(e.target, async () => {
  const r = await checkUpdates(true);
  if (r && !r.error && r.enabled !== false && !r.newer) toast(`У вас остання версія (${r.current})`);
  if (r && r.error) throw new Error(r.error);
});
$('#updNowBtn').onclick = () => runUpdate();
async function runUpdate() {
  const warn = cart.length ? 'У кошику є товари — після оновлення кошик буде порожній.\n\n' : '';
  if (!(await ask(`${warn}Оновити касу до версії ${updateInfo.latest}? Каса перезапуститься — це до хвилини.`, { title: 'Оновлення', ok: 'Оновити', icon: '⬆️' }))) return;
  const ov = $('#updateOverlay'); ov.style.display = 'flex';
  $('#updateStage').textContent = `Завантажую й встановлюю версію ${updateInfo.latest}…`;
  try { await api('/api/update/install', {}); }
  catch (e) { ov.style.display = 'none'; toast('Оновлення не вдалося: ' + e.message, true); return; }
  $('#updateStage').textContent = 'Каса перезапускається…';
  // чекаємо, поки стара копія зупиниться, а нова відповість; тоді перезавантажуємо сторінку
  const started = Date.now(); let wentDown = false;
  while (Date.now() - started < 90000) {
    await sleep(1000);
    try {
      const r = await fetch('/api/ping', { cache: 'no-store' });
      if (r.ok && (wentDown || Date.now() - started > 15000)) { location.reload(); return; }
      if (!r.ok) wentDown = true;
    } catch (e) { wentDown = true; }
  }
  $('#updateStage').textContent = 'Каса не відповідає';
  $('#updateHint').textContent = 'Запустіть касу ще раз — оновлення вже встановлено.';
  document.querySelector('#updateOverlay .spin').style.display = 'none';
}
setTimeout(() => checkUpdates(false), 1500);   // у фоні, щоб не гальмувати відкриття каси
</script>
</body>
</html>
"""


def already_running(port):
    """True, якщо на порту вже працює наша каса."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == APP_ID
    except (OSError, ValueError):
        return False


def candidate_ports():
    ports = []
    try:
        last = int(PORT_FILE.read_text(encoding="utf-8").strip())
        if last in PORT_RANGE:
            ports.append(last)
    except (OSError, ValueError):
        pass
    return ports + [p for p in PORT_RANGE if p not in ports]


# ───────────────────────── Значок у треї (Windows) ─────────────────────────

def _tray_image(Image):
    """Іконка для трея: kasa.ico поруч зі скриптом або вбудована в Kasa.exe; інакше — намальована."""
    for folder in (getattr(sys, "_MEIPASS", None), Path(__file__).resolve().parent):
        if folder and (Path(folder) / "kasa.ico").exists():
            try:
                # у пам'ять: Image.open лінивий і тримав би kasa.ico в _MEI… відкритим увесь час роботи
                img = Image.open(io.BytesIO((Path(folder) / "kasa.ico").read_bytes()))
                img.load()
                return img
            except OSError:
                pass
    from PIL import ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), 14, fill=(37, 99, 235, 255))
    d.rectangle((20, 12, 44, 50), fill=(255, 255, 255, 255))
    for y in (19, 26, 33):
        d.rectangle((24, y, 40, y + 3), fill=(147, 197, 253, 255))
    return img


def start_tray(url):
    """
    Значок у треї Windows: каса працює й після закриття вкладки, і її видно.
    Меню: «Відкрити касу» (також клік по значку), «Відкрити папку з логами», «Відкрити поточний лог», «Вийти».
    Потрібні pystray і Pillow; якщо їх немає — каса просто працює без значка.
    """
    if os.name != "nt" and os.environ.get("CHECKBOX_FORCE_TRAY") != "1":
        return None
    try:
        import pystray
        from PIL import Image
    except ImportError:
        log.info("Значок у треї недоступний: не встановлено pystray/Pillow")
        return None

    def open_kasa(icon=None, item=None):
        webbrowser.open(url)

    def open_log_file(icon=None, item=None):
        if hasattr(os, "startfile") and LOG_PATH.exists():
            try:
                os.startfile(str(LOG_PATH))          # програма, що відкриває .log
            except OSError:                          # для .log немає програми — Блокнот
                import subprocess
                subprocess.Popen(["notepad.exe", str(LOG_PATH)])
        else:
            open_logs()

    def open_logs(icon=None, item=None):
        folder = str(LOG_PATH.parent)
        if hasattr(os, "startfile"):
            os.startfile(folder)                    # Провідник Windows
        else:
            webbrowser.open(LOG_PATH.parent.as_uri())

    def quit_kasa(icon, item=None):
        log.info("Вихід через значок у треї")
        icon.stop()
        threading.Thread(target=SERVER.shutdown, daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("Відкрити касу", open_kasa, default=True),
        pystray.MenuItem("Відкрити папку з логами", open_logs),
        pystray.MenuItem("Відкрити поточний лог", open_log_file),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Вийти", quit_kasa),
    )
    try:
        icon = pystray.Icon("kasa", _tray_image(Image), f"Каса — {url}", menu)
        threading.Thread(target=icon.run, daemon=True, name="tray").start()
    except Exception:  # noqa — без трея каса все одно має працювати
        log.exception("Не вдалося показати значок у треї")
        return None
    log.info("Значок у треї показано")
    return icon


def start_server():
    """
    Знайти порт: якщо на якомусь уже працює ця каса — повернути (None, порт, …);
    інакше зайняти перший вільний — (сервер, порт, …). Усі зайняті іншими програмами — (None, None, …).
    Третє значення — порти, зайняті іншими програмами.
    """
    busy = []
    if os.environ.get("CHECKBOX_RESTARTED") == "1":
        # перезапуск після оновлення: стара копія може ще кілька секунд тримати свій порт
        first = candidate_ports()[0]
        for _ in range(30):
            try:
                return KasaServer(("127.0.0.1", first), Handler), first, busy
            except OSError:
                time.sleep(0.5)
    for port in candidate_ports():
        if already_running(port):
            return None, port, busy
        try:
            return KasaServer(("127.0.0.1", port), Handler), port, busy
        except OSError:
            busy.append(port)      # зайнятий іншою програмою — пробуємо наступний
    return None, None, busy


def startup_connect(cfg):
    """Вхід у Checkbox і перша синхронізація — у фоні, щоб сторінка відкрилась одразу."""
    try:
        _startup_connect(cfg)
    finally:
        APP.connecting = False


def _startup_connect(cfg):
    with APP_LOCK:
        try:
            me = None
            if APP.api.restore_token():          # збережений токен — без POST /cashier/signin
                try:
                    me = APP.api.me()
                    log.info("Авторизовано збереженим токеном (%s)", auth_mode(cfg))
                except CheckboxError as e:
                    if not str(e).startswith("HTTP 401"):
                        raise
                    log.info("Збережений токен касира недійсний — авторизуюся знову")
            if me is None:
                APP.api.signin()
                me = APP.api.me()
                log.info("Авторизовано (%s)", auth_mode(cfg))
            APP.auth_ok, APP.auth_error = True, None
            APP.cashier = (me or {}).get("full_name")
            APP.register_info()                  # фіскальний номер каси — лише якщо ще не збережено
        except (CheckboxError, net.RequestException) as e:
            APP.auth_ok, APP.auth_error = False, str(e)
            log.warning("Не вдалося авторизуватися: %s", e)
            print("⚠ Не вдалося підключитися до Checkbox — перевірте «Налаштування» на сторінці.")
            return
        if not APP.db.meta("synced_at") or not APP.db.taxes():   # порожня база або ще без податків
            log.info("Синхронізую товари і податкові ставки…")
            try:
                r = APP.sync()
                log.info("Синхронізовано товарів: %s, податкових ставок: %s", r["count"], r["taxes"])
            except (CheckboxError, net.RequestException, UserError) as e:
                log.warning("Не вдалося синхронізувати: %s", e)


def update_check(force=False):
    try:
        return kasa_update.public(kasa_update.check(force))
    except kasa_update.UpdateError as e:
        return {"enabled": True, "current": APP_VERSION, "error": str(e)}


def update_install():
    """Встановити оновлення і перезапустити касу (сторінка дочекається й перезавантажиться)."""
    global RESTART_AFTER_STOP
    try:
        version = kasa_update.install()
    except kasa_update.UpdateError as e:
        log.warning("Оновлення не вдалося: %s", e)
        raise UserError(str(e))
    RESTART_AFTER_STOP = True
    threading.Timer(0.5, SERVER.shutdown).start()     # спершу відповісти сторінці, потім зупинитись
    return {"installed": version}


def notify(message):
    """Повідомлення користувачу: у консоль, а в зібраному exe без консолі — вікном Windows."""
    print(message)
    if getattr(sys, "frozen", False) and os.name == "nt" and not sys.stdout.isatty():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "Каса", 0x30)   # значок «увага»
        except Exception:  # noqa
            pass


def main():
    global APP, SERVER, PORT
    # Kasa.exe без консолі: stdout/stderr відсутні, і print() інакше падав би
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    kasa_update.relaunch_if_inherited()
    kasa_update.cleanup_after_update()
    net.ssl_context()           # сертифікати — у пам'ять, поки файл точно на місці
    # порт займаємо до довгої авторизації; повторний запуск лише відкриває вже запущену касу
    SERVER, PORT, busy = start_server()
    # 127.0.0.1, а не localhost: інакше браузер у Windows спершу пробує IPv6 і чекає кілька секунд
    url = f"http://127.0.0.1:{PORT}"
    if SERVER is None and PORT is not None:
        print(f"Каса вже працює — відкриваю {url}")
        webbrowser.open(url)
        return
    if SERVER is None:
        notify(f"Не вдалося запустити касу: усі порти {PORT_RANGE.start}–{PORT_RANGE.stop - 1} "
               "зайняті іншими програмами.\n\nЗакрийте зайві програми або перезавантажте комп'ютер.")
        sys.exit(2)
    try:
        PORT_FILE.write_text(str(PORT), encoding="utf-8")
    except OSError:
        pass

    log_path = setup_logging()
    log.info("──── Запуск каси %s, дані: %s", APP_VERSION, CONFIG_PATH.parent)
    kasa_update.log_temp_state()
    kasa_update.unquarantine()          # Mac: щоб наступні запуски йшли не з тимчасової копії
    threading.Timer(20, kasa_update.cleanup_old_temp).start()   # залишки _MEI… попередніх копій
    if busy:
        log.info("Зайняті іншими програмами порти: %s — каса працює на порту %d", ", ".join(map(str, busy)), PORT)
        print(f"Порт {busy[0]} зайнятий іншою програмою — каса працює на порту {PORT}.")
    print(f"Запускаю касу… Журнал роботи: {log_path}")

    cfg = load_config()
    APP = App(CheckboxAPI(cfg), LocalDB(), False,
              "Підключаюся до Checkbox…" if cfg else "Налаштування ще не заповнені")
    try:
        APP.purge_old()                 # прибрати чеки, старші за тиждень
    except Exception:  # noqa — очищення не повинне заважати запуску
        log.exception("Очищення журналу при запуску не вдалося")
    if not cfg:
        log.info("config.json не знайдено — налаштування треба заповнити на сторінці")
    else:
        APP.connecting = True
        threading.Thread(target=startup_connect, args=(cfg,), daemon=True).start()

    log.info("Каса працює на %s", url)
    print(f"✔ Каса працює: {url}")
    tray = start_tray(url)
    if tray:
        print("  Каса є в треї (біля годинника): клік — відкрити, правий клік → «Вийти» — зупинити.")
    else:
        print("  Зупинити — кнопка «⏻ Вийти» на сторінці або Ctrl+C у цьому вікні.")
    if os.environ.get("CHECKBOX_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass
    if tray:
        try:
            tray.stop()                 # вихід кнопкою на сторінці чи Ctrl+C — прибрати й значок
        except Exception:  # noqa
            pass
    SERVER.server_close()
    if RESTART_AFTER_STOP:
        kasa_update.spawn_restarted()
        print("\nКасу оновлено — запускаю нову версію…")
        kasa_update.skip_bootloader_cleanup()     # останнім: без вікна «Failed to remove temporary directory»
        return
    log.info("Касу зупинено")
    print("\nКасу зупинено.")


if __name__ == "__main__":
    main()
