#!/usr/bin/env python3
"""
Веб-інтерфейс для простої облікової системи Checkbox.

Запуск:  python checkbox_web.py
Відкриється браузер на http://localhost:8000

Потрібні поруч: checkbox_app.py і config.json.
Залежності: лише стандартна бібліотека Python 3.9+.
"""

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
                          no_tax_goods, round_cash, sync_all)

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
        try:
            r = self.s.request(method, f"{self.url}/{path}", json=body, timeout=timeout)
        except net.RequestException:
            raise UserError(f"POSSrv не відповідає за адресою {self.url}. Перевірте, що POSSrv запущено.")
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

    def call(self, fn, *args):
        """Виклик API з повторною авторизацією, якщо токен застарів."""
        if not self.auth_ok:
            raise UserError("Каса не підключена — відкрийте «Налаштування»")
        try:
            return fn(*args)
        except CheckboxError as e:
            if str(e).startswith("HTTP 401"):
                self.api.signin()
                return fn(*args)
            raise

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
        new.setdefault("client_name", DEFAULT_CLIENT_NAME)
        new.setdefault("client_version", DEFAULT_CLIENT_VERSION)

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
        result = {"saved": True, "cashier": self.cashier}

        # Інша каса — інша база товарів: синхронізуємо одразу
        if old.get("license_key") != new.get("license_key") or not self.db.meta("synced_at"):
            try:
                result["synced"] = self.sync()["count"]
            except (CheckboxError, net.RequestException, UserError) as e:
                result["sync_error"] = str(e)
        return result

    # ---- товари: додавання й редагування (через імпорт номенклатури Checkbox) ----
    def save_good(self, body):
        good_id = body.get("id")
        existing = None
        if good_id:
            existing = self.db.con.execute("SELECT * FROM goods WHERE id=?", (str(good_id),)).fetchone()
            if not existing:
                raise UserError("Товар не знайдено в локальній базі — оновіть сторінку")
        pending_new = existing is not None and existing["pending"] == "new"
        rec = {
            "code": str(body.get("code") or ""), "name": str(body.get("name") or ""),
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
        if errors:
            raise UserError("; ".join(errors))
        if existing:
            changes = goods_csv.diff(existing, good)
            if not changes:
                return {"unchanged": True, "pending": self.db.pending_count()}
            log.info("Локальна правка товару %s: %s", good["code"], "; ".join(changes))
        else:
            log.info("Новий товар (локально) %s «%s»", good["code"], good["name"])
        # лише локально: у Checkbox піде під час синхронізації (кнопка, відкриття/закриття зміни)
        self.db.put_pending(good, existing)
        return {"saved": True, "pending": self.db.pending_count()}

    def discard_good(self, body):
        code = str(body.get("code") or "").strip()
        if not self.db.discard_pending(code):
            raise UserError("Для цього товару немає невідправлених змін")
        log.info("Скасовано локальну правку товару %s", code)
        return {"pending": self.db.pending_count()}

    def push_pending(self):
        """Відправити всі локальні правки в Checkbox одним імпортом. Повертає кількість."""
        pending = self.db.pending()
        if not pending:
            return 0
        goods = [dict(p["data"], match_code=None if p["action"] == "new" else p["code"]) for p in pending]
        try:
            self.call(goods_csv.save_goods_to_checkbox, self.api, self.db, goods, None, False, False)
        except (CheckboxError, net.RequestException) as e:
            self.db.set_pending_error(str(e))
            log.warning("Невідправлені правки товарів (%d): %s", len(pending), e)
            raise
        self.db.clear_pending([p["code"] for p in pending])
        return len(pending)

    def full_sync(self):
        """Спершу відправити локальні правки, потім оновити товари з Checkbox (правки, що не пішли, лишаються)."""
        pushed, push_error = 0, None
        try:
            pushed = self.push_pending()
        except (CheckboxError, net.RequestException) as e:
            push_error = str(e)
        r = self.sync()
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
            goods.append(g)
        return {
            "goods": goods,
            "taxes": [dict(t) for t in self.db.taxes()],
            "taxes_source": self.db.meta("taxes_source"),
            "use_leftovers": self.db.meta("use_leftovers"),
            "synced_at": self.db.meta("synced_at"),
            "cash_rounding": self.rounding_mode(),
            "pending": self.db.pending_count(),
        }

    def rounding_mode(self):
        return self.api.cfg.get("cash_rounding") or None

    def sync(self):
        goods, use_leftovers, taxes, source = self.call(sync_all, self.api, self.db)
        bad = [g["name"] for g in goods if g["code"] != g["code"].strip()]
        return {"count": len(goods), "use_leftovers": use_leftovers, "bad_codes": bad,
                "taxes": len(taxes), "taxes_source": source, "no_tax": no_tax_goods(self.db)}

    # ---- зміна ----
    def shift(self):
        return {"shift": self.call(self.api.current_shift)}

    def _sync_quietly(self):
        """Синхронізація при відкритті/закритті зміни: помилка не зриває саму операцію зі зміною."""
        try:
            r = self.full_sync()
            return {"pushed": r["pushed"], "push_error": r["push_error"], "pending": r["pending"]}
        except (CheckboxError, net.RequestException, UserError) as e:
            log.warning("Синхронізація при зміні не вдалася: %s", e)
            return {"sync_error": str(e), "pending": self.db.pending_count()}

    def open_shift(self):
        if self.call(self.api.current_shift):
            raise UserError("Зміна вже відкрита")
        shift = self.call(self.api.open_shift)
        return {"shift": shift, "sync": self._sync_quietly()}

    def close_shift(self):
        if not self.call(self.api.current_shift):
            raise UserError("Немає відкритої зміни")
        sync = self._sync_quietly()          # правки дня — у Checkbox ще до Z-звіту
        shift = self.call(self.api.close_shift)
        z = shift.get("z_report") or {}
        return {"shift": shift, "z_report_id": z.get("id"), "sync": sync}

    def x_report(self):
        if not self.call(self.api.current_shift):
            raise UserError("Немає відкритої зміни")
        return {"text": self.call(self.api.x_report)}

    # ---- продаж ----
    def sell(self, body):
        items = body.get("items") or []
        payment_type = body.get("payment_type")
        if payment_type not in ("CASH", "CASHLESS"):
            raise UserError("Невірний тип оплати")
        if not items:
            raise UserError("Кошик порожній")
        if not self.call(self.api.current_shift):
            raise UserError("Спочатку відкрийте зміну", code="SHIFT_CLOSED")

        use_leftovers = self.db.meta("use_leftovers")
        cart, total, all_marks = [], 0, set()
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
            if marks and not row["is_weight"] and len(marks) > qty // 1000:
                raise UserError(f"Марок більше, ніж одиниць товару: {row['name']}")
            dup = all_marks.intersection(marks) or (len(set(marks)) != len(marks))
            if dup:
                raise UserError(f"Акцизна марка повторюється: {row['name']}")
            all_marks.update(marks)

            cart.append((row, qty, marks))
            total += round(row["price"] * qty / 1000)

        rounding_mode = self.rounding_mode() if payment_type == "CASH" else None
        to_pay = round_cash(total, rounding_mode)
        pay_value = to_pay
        received = body.get("received")
        if payment_type == "CASH" and received is not None:
            if not isinstance(received, int) or received < to_pay:
                raise UserError(f"Отримано менше, ніж до сплати ({to_pay / 100:.2f} грн)")
            pay_value = received

        card = clean_card_details(body.get("card")) if payment_type == "CASHLESS" else None
        terminal_raw = body.get("terminal_raw") if payment_type == "CASHLESS" else None
        details = None
        if card or terminal_raw:
            details = {"card": card, "terminal": terminal_raw}
        if terminal_raw:
            log.info("POSSrv відповідь: %s", terminal_for_log(terminal_raw))
        try:
            label = clean_payment_label(body.get("label"))
        except ValueError as e:
            raise UserError(str(e))
        extra = dict(card or {})
        if label:
            extra["label"] = label

        # ID генерується один раз: якщо call() повторить запит після 401, чек не задублюється
        receipt_id = new_receipt_id()
        receipt = self.call(self.api.sell, receipt_id, build_goods_payload(cart, use_leftovers),
                            payment_type, pay_value, rounding_mode, extra or None)
        for g, qty, _ in cart:
            self.db.decrease_count(g["id"], qty)
        self.db.add_sale(receipt, payment_type,
                         [{"name": g["name"], "code": g["code"], "qty": qty, "price": g["price"],
                           **({"excise_barcodes": m} if m else {})}
                          for g, qty, m in cart], label, details)
        log.info("Продаж: чек %s, ФН %s, %.2f грн, %s%s, позицій %d", receipt["id"], receipt.get("fiscal_code"),
                 (receipt.get("total_sum") or total) / 100, payment_type, f" ({label})" if label else "", len(cart))
        return {"id": receipt["id"], "fiscal_code": receipt.get("fiscal_code"),
                "total_sum": receipt.get("total_sum", total),
                "round_sum": receipt.get("round_sum"),
                "rest": receipt.get("rest")}

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
        stamp = datetime.now().isoformat(timespec="seconds")
        with self.db.con:
            self.db.con.execute("INSERT OR REPLACE INTO meta VALUES ('receipts_fetched_at', ?)", (stamp,))
        removed, _ = self.purge_old()
        log.info("Завантаження чеків з Checkbox: отримано %d, нових %d", len(receipts), new)
        return {"fetched": len(receipts), "new": new, "removed": removed, "fetched_at": stamp}

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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # тиша в консолі

    def _send(self, code, data, ctype="application/json; charset=utf-8"):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # завжди свіжа версія сторінки після оновлень
        self.end_headers()
        self.wfile.write(body)

    def _run_png(self, receipt_id):
        """Як _run, але відповідь — зображення, а помилка — JSON з кодом 4xx/5xx."""
        try:
            with APP_LOCK:
                png = APP.receipt_png(receipt_id)
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
            "/api/shift": APP.shift,
            "/api/sales": APP.sales,
        }
        if u.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif u.path in routes:
            self._run(routes[u.path])
        elif u.path == "/api/receipt_text":
            self._run(APP.receipt_text, (q.get("id") or [""])[0])
        elif u.path == "/api/receipt_png":
            self._run_png((q.get("id") or [""])[0])
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
<style>
  :root {
    --bg: #f4f5f7; --card: #fff; --text: #1d2330; --muted: #6b7280; --line: #e5e7eb;
    --accent: #2563eb; --accent-2: #1d4ed8; --ok: #16a34a; --warn: #d97706; --bad: #dc2626;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); }
  header { display: flex; align-items: center; gap: 16px; padding: 12px 20px; background: var(--text); color: #fff; }
  header h1 { font-size: 17px; margin: 0; font-weight: 600; }
  header .spacer { flex: 1; }
  header small { color: #b6bcc8; }
  .badge { padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; background: #4b5563; }
  .badge.open { background: var(--ok); }
  nav { display: flex; gap: 4px; padding: 0 20px; background: var(--card); border-bottom: 1px solid var(--line); }
  nav button { border: 0; background: none; padding: 12px 14px; font: inherit; cursor: pointer; color: var(--muted); border-bottom: 2px solid transparent; }
  nav button.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
  main { padding: 20px; max-width: 1300px; margin: 0 auto; }
  .tab { display: none; } .tab.active { display: block; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px; }
  .grid { display: grid; grid-template-columns: 1fr 440px; gap: 20px; align-items: start; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  input[type=text], input[type=number], input[type=password] { width: 100%; padding: 9px 11px; border: 1px solid var(--line); border-radius: 8px; font: inherit; }
  input:focus { outline: 2px solid var(--accent); outline-offset: -1px; border-color: transparent; }
  button.btn { border: 0; border-radius: 8px; padding: 9px 16px; font: inherit; font-weight: 600; cursor: pointer; background: var(--accent); color: #fff; }
  button.btn:hover { background: var(--accent-2); }
  button.btn.sec { background: #e5e7eb; color: var(--text); }
  button.btn.danger { background: var(--bad); }
  button.btn.ok { background: var(--ok); }
  button.btn:disabled { opacity: .5; cursor: wait; }
  .row { display: flex; gap: 8px; align-items: center; }
  .muted { color: var(--muted); }
  .results { margin-top: 12px; max-height: 62vh; overflow: auto; }
  .item { display: flex; justify-content: space-between; gap: 12px; padding: 10px 12px; border-radius: 8px; cursor: pointer; }
  .item:hover { background: #eef2ff; }
  .item small { display: block; color: var(--muted); }
  .item .r { text-align: right; white-space: nowrap; font-weight: 600; }
  .zero { color: var(--bad) !important; font-weight: 600; }
  .tax { display: inline-block; min-width: 20px; padding: 1px 6px; margin-right: 3px; border-radius: 5px; background: #eef2ff; color: var(--accent-2); font-size: 12px; font-weight: 700; text-align: center; cursor: help; }
  .tax.none { background: #fef2f2; color: var(--bad); }
  .marks { list-style: none; padding: 0; margin: 12px 0 0; max-height: 220px; overflow: auto; }
  .marks li { display: flex; justify-content: space-between; align-items: center; padding: 6px 10px; border: 1px solid var(--line); border-radius: 6px; margin-bottom: 6px; font-family: ui-monospace, Consolas, monospace; }
  .markbtn { margin-top: 4px; border: 1px solid var(--line); background: #fff; border-radius: 6px; padding: 2px 8px; font: inherit; font-size: 12px; cursor: pointer; }
  .markbtn.warn { border-color: #fca5a5; background: #fef2f2; color: var(--bad); }
  .markbtn.ok { border-color: #86efac; background: #f0fdf4; color: var(--ok); }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: left; }
  th { font-size: 12px; text-transform: uppercase; letter-spacing: .03em; color: var(--muted); font-weight: 600; }
  td.num, th.num { text-align: right; white-space: nowrap; }
  tbody tr.click { cursor: pointer; } tbody tr.click:hover { background: #f9fafb; }
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
  select { width: 100%; padding: 9px 11px; border: 1px solid var(--line); border-radius: 8px; font: inherit; background: #fff; }
  .pw { position: relative; display: block; }
  .pw input { padding-right: 38px; }
  .eye { position: absolute; right: 6px; top: 50%; transform: translateY(-50%); border: 0; background: none; cursor: pointer; font-size: 15px; opacity: .6; }
  .eye:hover { opacity: 1; }
  .status { margin-top: 12px; padding: 9px 12px; border-radius: 8px; font-size: 13px; }
  .status.ok { background: #ecfdf5; color: #065f46; }
  .status.bad { background: #fef2f2; color: #991b1b; }
  .status.wait { background: #eff6ff; color: var(--accent-2); }
  header .who { color: #b6bcc8; font-size: 12px; }
  details.day { border: 1px solid var(--line); border-radius: 8px; margin-bottom: 8px; }
  details.day > summary { padding: 10px 14px; cursor: pointer; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; list-style-position: inside; }
  details.day[open] > summary { border-bottom: 1px solid var(--line); }
  details.day table { margin: 0; } details.day td, details.day th { padding: 7px 14px; }
  .daysum { font-size: 13px; }
  .shifts { padding: 8px 10px; }
  details.shift { border: 1px solid var(--line); border-radius: 6px; margin-bottom: 6px; background: #fcfcfd; }
  details.shift > summary { padding: 8px 12px; cursor: pointer; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; font-size: 14px; }
  details.shift[open] > summary { border-bottom: 1px solid var(--line); }
  details.shift table { background: #fff; }
  .upd { display: flex; justify-content: center; align-items: center; gap: 14px; padding: 8px 20px; background: #ecfdf5; color: #065f46; border-bottom: 1px solid #a7f3d0; font-size: 14px; }
  .upd a { color: inherit; }
  .upd-overlay { position: fixed; inset: 0; background: rgba(15,23,42,.55); display: flex; align-items: center; justify-content: center; z-index: 50; }
  .upd-overlay .card { text-align: center; min-width: 320px; }
  .spin { width: 34px; height: 34px; margin: 4px auto 12px; border: 4px solid #dbeafe; border-top-color: var(--accent); border-radius: 50%; animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .last-receipt { margin-top: 10px; max-height: 460px; overflow: auto; text-align: center; background: #fff; border-radius: 6px; }
  .last-receipt img { max-width: 100%; cursor: zoom-in; display: block; margin: 0 auto; }
  .receipt-img { margin-top: 12px; max-height: 75vh; overflow: auto; text-align: center; background: #f9fafb; border-radius: 8px; padding: 8px; }
  .receipt-img img { max-width: 100%; }
  .termstate { margin-top: 14px; padding: 16px; border-radius: 10px; font-size: 16px; font-weight: 600; text-align: center; background: #eff6ff; color: var(--accent-2); }
  .termstate.approved { background: #ecfdf5; color: #065f46; }
  .termstate.declined, .termstate.conn, .termstate.shift_unknown { background: #fef2f2; color: #991b1b; }
  .termstate.unknown, .termstate.fiscal_failed, .termstate.shift_closed { background: #fffbeb; color: #92400e; }
  .termrow { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-top: 16px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; font-size: 13px; }
  button.btn.small { padding: 6px 12px; font-size: 13px; white-space: nowrap; }
  .modal .card.wide { max-width: 640px; }
  label.chk { display: flex; align-items: center; gap: 8px; cursor: pointer; }
  .form label .row input { flex: 1; }
  details summary { cursor: pointer; }
  .goods-edit tbody tr { cursor: pointer; } .goods-edit tbody tr:hover { background: #f9fafb; }
  .pend { display: inline-block; margin-left: 6px; padding: 1px 7px; border-radius: 999px; font-size: 11px; font-weight: 600; background: #fef3c7; color: #92400e; vertical-align: middle; }
  .pend.err { background: #fee2e2; color: #991b1b; }
  button.btn.sec.attn { background: #fef3c7; color: #92400e; }
  .taxpick { display: flex; flex-wrap: wrap; gap: 8px; }
  .taxpick label { display: flex; align-items: center; gap: 6px; padding: 6px 10px; border: 1px solid var(--line); border-radius: 8px; cursor: pointer; font-size: 13px; }
  .taxpick label:has(input:checked) { border-color: var(--accent); background: #eff6ff; }
  input[readonly] { background: #f3f4f6; color: var(--muted); }
  .pay.three { grid-template-columns: 1fr 1fr 1fr; }
  button.btn.other { background: #7c3aed; } button.btn.other:hover { background: #6d28d9; }
  .seg { display: grid; grid-template-columns: 1fr 1fr; gap: 0; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  .seg label { display: flex; align-items: center; justify-content: center; gap: 6px; padding: 9px; cursor: pointer; font-weight: 600; }
  .seg input { display: none; }
  .seg label:has(input:checked) { background: var(--accent); color: #fff; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .chip { border: 1px solid var(--line); background: #fff; border-radius: 999px; padding: 4px 10px; font: inherit; font-size: 12px; cursor: pointer; }
  .chip:hover { border-color: var(--accent); color: var(--accent); }
  .chip .del { margin-left: 6px; color: var(--muted); }
  .pay { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .pay button { padding: 14px; font-size: 15px; }
  .last { margin-top: 14px; padding: 10px 12px; border-radius: 8px; background: #ecfdf5; color: #065f46; display: none; }
  pre { background: #111827; color: #e5e7eb; padding: 16px; border-radius: 8px; overflow: auto; font: 13px/1.45 ui-monospace, Consolas, monospace; white-space: pre-wrap; }
  .modal { position: fixed; inset: 0; background: rgba(0,0,0,.45); display: none; align-items: center; justify-content: center; padding: 20px; }
  .modal.show { display: flex; }
  .modal .card { max-width: 560px; width: 100%; max-height: 90vh; overflow: auto; }
  #toast { position: fixed; right: 20px; bottom: 20px; padding: 12px 16px; border-radius: 8px; background: var(--text); color: #fff; opacity: 0; transform: translateY(10px); transition: .2s; max-width: 420px; pointer-events: none; }
  #toast.show { opacity: 1; transform: none; } #toast.err { background: var(--bad); }
  .stats { display: flex; gap: 24px; margin-bottom: 12px; }
  .stats b { font-size: 18px; display: block; }
</style>
</head>
<body>
<header>
  <h1>Checkbox облік</h1>
  <span id="shiftBadge" class="badge">Зміна: …</span>
  <span id="cashierName" class="who"></span>
  <span class="spacer"></span>
  <small>Товари оновлено: <span id="synced">—</span></small>
  <button class="btn sec" id="syncBtn">⟳ Синхронізувати</button>
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
</nav>
<main>
  <!-- КАСА -->
  <section id="pos" class="tab active">
    <div class="grid">
      <div class="card">
        <input type="text" id="search" placeholder="Штрихкод, код або назва — Enter додає в кошик" autofocus>
        <div class="results" id="results"></div>
      </div>
      <div class="card">
        <h3 style="margin-top:0">Кошик</h3>
        <table class="cart">
          <thead><tr><th>Товар</th><th class="num">К-сть</th><th class="num">Сума</th><th></th></tr></thead>
          <tbody id="cart"></tbody>
        </table>
        <div class="total"><span>Разом</span><span id="total">0.00 ₴</span></div>
        <div id="cashBox" class="cashbox">
          <div class="line" id="roundLine"><span>Заокруглення (готівка)</span><span id="roundSum"></span></div>
          <div class="line strong"><span>До сплати готівкою</span><span id="toPayCash">0.00 ₴</span></div>
          <div class="line"><span>Отримано, ₴</span><input type="number" id="received" min="0" step="0.5" placeholder="без решти"></div>
          <div class="line" id="restLine"><span>Решта</span><span id="rest"></span></div>
        </div>
        <div class="pay three">
          <button class="btn ok" id="payCash">Готівка</button>
          <button class="btn" id="payCard">Картка</button>
          <button class="btn other" id="payOther">Інше</button>
        </div>
        <button class="btn sec" id="clearCart" style="width:100%;margin-top:10px">Очистити кошик</button>
        <div class="last" id="last"></div>
      </div>
    </div>
  </section>

  <!-- ТОВАРИ -->
  <section id="goods" class="tab">
    <div class="card">
      <div class="stats">
        <div><span class="muted">Товарів</span><b id="stCount">0</b></div>
        <div><span class="muted">Нульовий залишок</span><b id="stZero">0</b></div>
        <div><span class="muted">Облік залишків</span><b id="stLeft">—</b></div>
        <div><span class="muted">Без податку</span><b id="stNoTax">0</b></div>
      </div>
      <div class="row">
        <input type="text" id="goodsFilter" placeholder="Фільтр…">
        <button class="btn" id="goodAddBtn" style="white-space:nowrap">+ Додати товар</button>
      </div>
      <p class="muted" style="font-size:12px;margin:6px 0 0">Клікніть товар, щоб змінити його. Зміни одразу діють у касі, а в Checkbox ідуть під час синхронізації (⟳, відкриття або закриття зміни).</p>
      <table style="margin-top:12px" class="goods-edit">
        <thead><tr><th>Назва</th><th>Код</th><th>Штрихкод</th><th>УКТЗЕД</th><th>Податок</th><th class="num">Ціна, ₴</th><th class="num">Залишок</th></tr></thead>
        <tbody id="goodsTable"></tbody>
      </table>
    </div>
    <div class="card" style="margin-top:20px">
      <div class="row" style="justify-content:space-between">
        <h3 style="margin:0">Податкові ставки організації</h3>
        <small class="muted">Джерело: <span id="taxSource">—</span></small>
      </div>
      <table style="margin-top:12px">
        <thead><tr><th>Код</th><th>Літера</th><th>Назва</th><th class="num">Ставка</th><th class="num">Акциз</th><th>Тип</th><th class="num">Товарів</th></tr></thead>
        <tbody id="taxTable"></tbody>
      </table>
    </div>
  </section>

  <!-- ЗМІНА -->
  <section id="shift" class="tab">
    <div class="card">
      <div id="shiftInfo" class="muted">Завантаження…</div>
      <div class="row" style="margin-top:14px">
        <button class="btn ok" id="openShift">Відкрити зміну</button>
        <button class="btn sec" id="xReport">X-звіт</button>
        <button class="btn danger" id="closeShift">Закрити зміну (Z-звіт)</button>
      </div>
      <pre id="reportText" style="display:none;margin-top:16px"></pre>
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
  </section>
</main>

<div class="modal" id="modal"><div class="card">
  <div class="row" style="justify-content:space-between"><h3 style="margin:0">Чек</h3>
    <span class="row"><button class="btn sec small" id="receiptPrint" style="display:none">🖨 Друк</button><button class="x" id="modalClose">✕</button></span></div>
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
    <button class="btn sec small" id="checkUpdBtn">Перевірити оновлення</button>
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
    <h3 style="margin:0">Оплата карткою · <span id="termSum"></span></h3>
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
    <label>Код<input type="text" id="gCode" maxlength="64" spellcheck="false"></label>
    <label>Ціна, ₴<input type="text" id="gPrice" inputmode="decimal" placeholder="0.00"></label>
    <label>Штрихкод<input type="text" id="gBarcode" inputmode="numeric" spellcheck="false"></label>
    <label>УКТЗЕД<input type="text" id="gUktzed" placeholder="2202 10 00 00" spellcheck="false"></label>
    <label class="wide chk"><input type="checkbox" id="gWeight"> Ваговий товар (продається в кілограмах)</label>
  </div>
  <p class="muted" style="margin:12px 0 6px">Податкові ставки:</p>
  <div id="gTaxes" class="taxpick"></div>
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
  <input type="text" id="otherLabel" maxlength="100" placeholder="Напр. Сертифікат, Бонуси, Переказ на рахунок">
  <div class="chips" id="otherChips"></div>
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
  <p class="muted" style="margin:0 0 12px">Відскануйте або введіть марку і натисніть Enter — по одній на кожну одиницю.
    Додано: <b id="exciseCount"></b></p>
  <input type="text" id="exciseInput" placeholder="Штрихкод акцизної марки" autocomplete="off">
  <ul class="marks" id="exciseList"></ul>
  <div class="pay" style="margin-top:14px">
    <button class="btn sec" id="exciseNone">Без марок</button>
    <button class="btn" id="exciseDone">Готово</button>
  </div>
</div></div>
<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
let goods = [], cart = [], useLeftovers = false, roundingMode = null, cartTotal = 0, taxes = [];

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
async function busy(btn, fn) {   // true — успіх, false — помилка (вже показана)
  btn.disabled = true;
  try { await fn(); return true; } catch (e) { toast(e.message, true); return false; } finally { btn.disabled = false; }
}
function showText(text) {
  $('#modalImgWrap').style.display = 'none'; $('#receiptPrint').style.display = 'none';
  $('#modalText').style.display = ''; $('#modalText').textContent = text; $('#modal').classList.add('show');
}
const pngUrl = id => '/api/receipt_png?id=' + encodeURIComponent(id);
// PNG чека у вікні; якщо зображення недоступне — текстовий варіант
function showReceipt(id) {
  const img = $('#modalImg');
  let retried = false;
  img.onload = () => { $('#modalText').style.display = 'none'; $('#modalImgWrap').style.display = ''; $('#receiptPrint').style.display = ''; };
  img.onerror = async () => {
    if (!retried) { retried = true; return setTimeout(() => { img.src = pngUrl(id) + '&r=1'; }, 1200); }   // Checkbox міг ще готувати зображення
    try { showText((await api('/api/receipt_text?id=' + encodeURIComponent(id))).text); }
    catch (err) { toast(err.message, true); $('#modal').classList.remove('show'); }
  };
  $('#modalText').style.display = ''; $('#modalText').textContent = 'Завантажую чек…';
  $('#modalImgWrap').style.display = 'none'; $('#receiptPrint').style.display = 'none';
  $('#modal').classList.add('show');
  img.dataset.id = id; img.src = pngUrl(id);
}
$('#receiptPrint') && ($('#receiptPrint').onclick = () => {
  const w = window.open('', '_blank', 'width=420,height=700');
  if (!w) return toast('Браузер заблокував вікно друку — дозвольте спливаючі вікна для каси', true);
  w.document.write(`<!DOCTYPE html><title>Чек</title><style>body{margin:0;text-align:center}img{max-width:100%}</style>
    <img src="${pngUrl($('#modalImg').dataset.id)}" onload="window.focus();window.print();setTimeout(()=>window.close(),300)">`);
  w.document.close();
});
$('#modalClose').onclick = () => $('#modal').classList.remove('show');
$('#modal').onclick = e => { if (e.target.id === 'modal') $('#modal').classList.remove('show'); };

// --- вкладки ---
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button, .tab').forEach(x => x.classList.remove('active'));
  b.classList.add('active'); $('#' + b.dataset.tab).classList.add('active');
  if (b.dataset.tab === 'sales') loadSales();
  if (b.dataset.tab === 'shift') loadShift();
  if (b.dataset.tab === 'pos') $('#search').focus();
});

// --- товари ---
async function loadGoods() {
  const d = await api('/api/goods');
  goods = d.goods; useLeftovers = !!d.use_leftovers; roundingMode = d.cash_rounding; taxes = d.taxes || [];
  renderPending(d.pending);
  renderTaxes(d.taxes_source);
  $('#synced').textContent = d.synced_at ? d.synced_at.replace('T', ' ') : '—';
  renderSearch(); renderGoodsTable(); renderCart();
}
const match = (g, q) => {
  q = q.trim().toLowerCase();
  return !q || g.name.toLowerCase().includes(q) || g.code.trim().toLowerCase().includes(q) || (g.barcode || '').includes(q);
};
function renderSearch() {
  const list = goods.filter(g => match(g, $('#search').value)).slice(0, 100);
  $('#results').innerHTML = list.map(g => `
    <div class="item" data-id="${esc(g.id)}">
      <div><b>${esc(g.name)}</b>${pendingBadge(g)}<small>${esc(g.barcode || g.code)}</small></div>
      <div class="r">${uah(g.price)} ₴<small class="${g.count === 0 ? 'zero' : ''}">${qtyStr(g.count, g.is_weight)}</small></div>
    </div>`).join('') || '<p class="muted">Нічого не знайдено. Можливо, потрібна синхронізація.</p>';
}
function pendingBadge(g) {
  if (!g.pending) return '';
  if (g.pending_error) return ` <span class="pend err" title="Не вдалося відправити в Checkbox: ${esc(g.pending_error)}">⚠ не відправлено</span>`;
  return g.pending === 'new'
    ? ' <span class="pend" title="Новий товар; у Checkbox з\'явиться під час синхронізації">● новий, не в Checkbox</span>'
    : ' <span class="pend" title="Зміни ще не відправлені в Checkbox">● змінено локально</span>';
}
function renderGoodsTable() {
  const list = goods.filter(g => match(g, $('#goodsFilter').value));
  $('#stCount').textContent = goods.length;
  $('#stZero').textContent = goods.filter(g => g.count === 0).length;
  $('#stLeft').textContent = useLeftovers ? 'увімкнено' : 'вимкнено';
  const noTax = goods.filter(g => !(g.tax_codes || []).length).length;
  $('#stNoTax').textContent = noTax; $('#stNoTax').className = noTax ? 'zero' : '';
  $('#goodsTable').innerHTML = list.map(g => `
    <tr data-id="${esc(g.id)}"><td>${esc(g.name)}${pendingBadge(g)}</td>
      <td>${g.code !== g.code.trim() ? '<span class="zero" title="Код містить пробіли">⚠ </span>' : ''}${esc(g.code)}</td>
      <td>${esc(g.barcode)}</td><td>${esc(g.uktzed)}</td><td>${taxBadges(g.tax_codes)}</td>
      <td class="num">${uah(g.price)}</td>
      <td class="num ${g.count === 0 ? 'zero' : ''}">${qtyStr(g.count, g.is_weight)}</td></tr>`).join('');
}
$('#search').oninput = renderSearch;
$('#goodsFilter').oninput = renderGoodsTable;

// --- додавання й редагування товару ---
let editing = null;   // товар, що редагується; null — новий
function goodStatus(text, kind) {
  const st = $('#goodStatus'); st.style.display = text ? 'block' : 'none';
  st.className = 'status ' + (kind || 'ok'); st.textContent = text || '';
}
function openGood(g) {
  editing = g || null;
  $('#goodTitle').textContent = g ? 'Редагування товару' : 'Новий товар';
  $('#gName').value = g ? g.name : '';
  $('#gCode').value = g ? g.code.trim() : '';
  $('#gCode').readOnly = !!g && g.pending !== 'new';   // новий, ще не відправлений товар — код ще можна змінити
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
    ? `Код змінити не можна: Checkbox розпізнає товари за кодом. Залишок (${qtyStr(g.count, g.is_weight)}) змінюється поставками, а не тут.`
    : 'Товар одразу можна продавати; у Checkbox він з\'явиться під час синхронізації. Залишок змінюється поставками в Checkbox.';
  goodStatus(g && g.pending_error ? '⚠ Не вдалося відправити в Checkbox: ' + g.pending_error : '', 'bad');
  $('#goodDiscard').style.display = g && g.pending ? '' : 'none';
  $('#goodDiscard').textContent = g && g.pending === 'new' ? '🗑 Видалити новий товар (ще не в Checkbox)' : '↶ Скасувати невідправлені зміни';
  $('#goodModal').classList.add('show');
  (g ? $('#gName') : $('#gName')).focus();
}
function closeGood() {
  if ($('#goodSave').disabled) return toast('Зачекайте — зберігається в Checkbox', true);
  $('#goodModal').classList.remove('show'); editing = null;
}
$('#goodAddBtn').onclick = () => openGood(null);
$('#goodDiscard').onclick = async () => {
  if (!editing) return;
  const msg = editing.pending === 'new' ? 'Видалити цей новий товар з каси?' : 'Повернути товар до стану з Checkbox?';
  if (!confirm(msg)) return;
  try {
    await api('/api/goods/discard', { code: editing.code.trim() });
    $('#goodModal').classList.remove('show'); editing = null;
    toast('Невідправлені зміни скасовано'); await loadGoods();
  } catch (err) { goodStatus('✖ ' + err.message, 'bad'); }
};
$('#goodsTable').onclick = e => { const tr = e.target.closest('tr[data-id]'); if (tr) openGood(goods.find(g => g.id === tr.dataset.id)); };
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
  if (!body.tax_codes.length && taxes.length && !confirm('Товар без податкової ставки. Зберегти так?')) return;
  btn.disabled = true; $('#goodCancel').disabled = true;
  try {
    const r = await api('/api/goods/save', body);
    btn.disabled = false; $('#goodCancel').disabled = false;
    if (r.unchanged) { goodStatus('Змін немає.', 'ok'); return; }
    $('#goodModal').classList.remove('show');
    toast((editing ? 'Зміни збережено' : 'Товар додано') +
      ' в касі. У Checkbox вони підуть під час синхронізації (⟳, відкриття або закриття зміни).');
    editing = null;
    await loadGoods();
  } catch (err) {
    btn.disabled = false; $('#goodCancel').disabled = false;
    goodStatus('✖ ' + err.message, 'bad');
  }
};
$('#results').onclick = e => { const it = e.target.closest('.item'); if (it) addToCart(it.dataset.id); };
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

// --- кошик ---
function addToCart(id) {
  const g = goods.find(x => x.id === id);
  let qty = 1000;
  if (g.is_weight) {
    const v = prompt(`Вага, кг — ${g.name}`, '1');
    if (v === null) return;
    qty = Math.round(parseFloat(v.replace(',', '.')) * 1000);
    if (!(qty > 0)) return toast('Невірна вага', true);
  }
  const line = cart.find(c => c.id === id);
  const newQty = (line ? line.qty : 0) + qty;
  if (useLeftovers && g.count != null && newQty > g.count)
    return toast(`Недостатньо залишку: є ${qtyStr(g.count, g.is_weight)}`, true);
  let idx;
  if (line) { line.qty = newQty; idx = cart.indexOf(line); }
  else { cart.push({ id, code: g.code.trim(), qty, marks: [] }); idx = cart.length - 1; }
  renderCart();
  if (isExciseGood(g) && !g.is_weight) openExcise(idx); else $('#search').focus();
}

// --- інша оплата ---
const LABELS_KEY = 'checkbox_payment_labels';
const DEFAULT_LABELS = ['Сертифікат', 'Бонуси', 'Переказ на рахунок'];
function loadLabels() {
  try { const v = JSON.parse(localStorage.getItem(LABELS_KEY)); if (Array.isArray(v)) return v; } catch (e) {}
  return DEFAULT_LABELS.slice();
}
function saveLabels(list) { try { localStorage.setItem(LABELS_KEY, JSON.stringify(list.slice(0, 12))); } catch (e) {} }
const otherType = () => document.querySelector('input[name="otherType"]:checked').value;
function renderOther() {
  const sum = otherType() === 'CASH' ? roundCash(cartTotal, roundingMode) : cartTotal;
  $('#otherSum').textContent = uah(sum) + ' ₴';
  $('#otherChips').innerHTML = loadLabels().map((l, i) =>
    `<button class="chip" data-label="${esc(l)}">${esc(l)}<span class="del" data-del="${i}" title="Прибрати">✕</span></button>`).join('');
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
  if (d !== undefined) { e.stopPropagation(); const l = loadLabels(); l.splice(+d, 1); saveLabels(l); return renderOther(); }
  const chip = e.target.closest('.chip'); if (chip) { $('#otherLabel').value = chip.dataset.label; $('#otherLabel').focus(); }
};
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
$('#otherLabel').onkeydown = e => { if (e.key === 'Enter') $('#otherSubmit').click(); if (e.key === 'Escape') closeOther(); };

// --- акцизні марки ---
let exciseIdx = null;
function allMarks(exceptIdx) {
  return cart.flatMap((c, i) => i === exceptIdx ? [] : (c.marks || []));
}
function openExcise(i) {
  exciseIdx = i;
  const c = cart[i], g = goods.find(x => x.id === c.id);
  $('#exciseName').textContent = g.name;
  renderExcise();
  $('#exciseModal').classList.add('show');
  $('#exciseInput').value = ''; $('#exciseInput').focus();
}
function renderExcise() {
  const c = cart[exciseIdx]; const need = Math.round(c.qty / 1000);
  $('#exciseCount').textContent = `${c.marks.length} з ${need}`;
  $('#exciseCount').className = c.marks.length < need ? 'zero' : '';
  $('#exciseList').innerHTML = c.marks.map((m, k) =>
    `<li>${esc(m)} <button class="x" data-k="${k}" title="Видалити">✕</button></li>`).join('');
}
function closeExcise() { $('#exciseModal').classList.remove('show'); exciseIdx = null; renderCart(); $('#search').focus(); }
$('#exciseInput').onkeydown = e => {
  if (e.key === 'Escape') return closeExcise();
  if (e.key !== 'Enter') return;
  e.stopPropagation();
  const v = $('#exciseInput').value.trim(); $('#exciseInput').value = '';
  if (!v) return;
  const c = cart[exciseIdx]; const need = Math.round(c.qty / 1000);
  if (c.marks.includes(v) || allMarks(exciseIdx).includes(v)) return toast('Ця марка вже додана', true);
  if (c.marks.length >= need) return toast(`Вже додано ${need} марок — збільште кількість товару`, true);
  c.marks.push(v); renderExcise();
  if (c.marks.length === need) toast('Усі марки додано');
};
$('#exciseList').onclick = e => { const k = e.target.dataset.k; if (k !== undefined) { cart[exciseIdx].marks.splice(k, 1); renderExcise(); $('#exciseInput').focus(); } };
$('#exciseDone').onclick = closeExcise;
$('#exciseClose').onclick = closeExcise;
$('#exciseNone').onclick = () => { cart[exciseIdx].marks = []; closeExcise(); };
$('#exciseModal').onclick = e => { if (e.target.id === 'exciseModal') closeExcise(); };
function renderCart() {
  cart = cart.filter(c => {
    if (goods.some(g => g.id === c.id)) return true;
    const same = c.code && goods.find(g => g.code.trim() === c.code);   // після синхронізації id міг змінитися
    if (same) { c.id = same.id; return true; }
    return false;
  });
  let total = 0;
  $('#cart').innerHTML = cart.map((c, i) => {
    const g = goods.find(x => x.id === c.id);
    const sum = Math.round(g.price * c.qty / 1000); total += sum;
    let markBtn = '';
    if (isExciseGood(g) && !g.is_weight) {
      c.marks = c.marks || [];
      const need = Math.round(c.qty / 1000), have = c.marks.length;
      markBtn = `<br><button class="markbtn ${have >= need ? 'ok' : 'warn'}" data-marks="${i}">🏷 Марки ${have}/${need}</button>`;
    }
    return `<tr><td>${esc(g.name)}<br><small class="muted">${uah(g.price)} ₴ / ${g.is_weight ? 'кг' : 'шт'}</small> ${taxBadges(g.tax_codes)}${markBtn}</td>
      <td class="num"><input type="number" min="0" step="${g.is_weight ? '0.001' : '1'}" value="${c.qty / 1000}" data-i="${i}"></td>
      <td class="num">${uah(sum)}</td><td><button class="x" data-del="${i}">✕</button></td></tr>`;
  }).join('') || '<tr><td colspan="4" class="muted">Порожньо</td></tr>';
  $('#total').textContent = uah(total) + ' ₴';
  cartTotal = total;
  renderCash();
}
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
  const i = e.target.dataset.del; if (i !== undefined) { cart.splice(i, 1); renderCart(); }
};
$('#clearCart').onclick = () => { cart = []; renderCart(); };

function marksIncomplete() {
  return cart.filter(c => {
    const g = goods.find(x => x.id === c.id);
    return g && isExciseGood(g) && !g.is_weight && (c.marks || []).length < Math.round(c.qty / 1000);
  }).map(c => goods.find(x => x.id === c.id).name);
}
function confirmMarks() {
  const missing = marksIncomplete();
  return !missing.length || confirm(`Не для всіх одиниць додано акцизні марки:\n• ${missing.join('\n• ')}\n\nПробити чек усе одно?`);
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
  if (!opts.marksChecked && !confirmMarks()) return false;
  const body = { items: cart, payment_type: type };
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
      if (e.code !== 'SHIFT_CLOSED' || opts.noShiftPrompt || !confirm('Зміна закрита. Відкрити зміну і пробити чек?')) throw e;
      if (!(await openShiftNow())) throw e;
      r = await api('/api/sell', body);
    }
    cart = []; $('#received').value = '';
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

let term = null;   // поточна оплата: { ws, state, messages, approvedMsg, amount }
function wsUrl() {
  const base = (termCfg.url || 'http://localhost:9020').replace(/^http/i, 'ws').replace(/\/+$/, '');
  return `${base}/api/pos/${encodeURIComponent(termCfg.device_id)}/purchase`;
}
function termRender(state, text) {
  term.state = state;
  const icons = { sending: '⏳', progress: '⏳', approved: '✔', fiscal_failed: '⚠', declined: '✖', unknown: '⚠', conn: '✖',
                  checking: '⏳', shift_closed: '⚠', shift_unknown: '✖', opening: '⏳' };
  $('#termState').className = 'termstate ' + state;
  $('#termState').textContent = `${icons[state] || ''} ${text}`;
  $('#termRaw').textContent = term.messages.map(m => JSON.stringify(m, null, 2)).join('\n\n') || '—';
  const show = (id, on) => { $(id).style.display = on ? '' : 'none'; };
  const openShiftBtn = state === 'shift_closed' || (state === 'fiscal_failed' && term.shiftClosed);
  $('#termOpenShift').textContent = state === 'shift_closed' ? 'Відкрити зміну і оплатити' : 'Відкрити зміну і пробити чек';
  show('#termCancel', state === 'sending' || state === 'progress');
  show('#termRetry', ['declined', 'unknown', 'conn', 'shift_unknown'].includes(state));
  show('#termManual', ['declined', 'unknown', 'conn'].includes(state));
  show('#termOpenShift', openShiftBtn);
  show('#termFiscalRetry', state === 'fiscal_failed' && !term.shiftClosed);
  show('#termCloseBtn', ['declined', 'unknown', 'conn', 'shift_closed', 'shift_unknown'].includes(state));
}
function startTermPayment() {
  const t = term;                    // ця спроба; після закриття вікна її події ігноруються
  t.messages = []; t.approvedMsg = null; t.finished = false;
  termRender('sending', `Надсилаю ${uah(t.amount)} ₴ на термінал…`);
  const live = () => term === t && !t.finished;
  let ws;
  try { ws = new WebSocket(wsUrl()); } catch (e) { t.finished = true; return termRender('conn', 'Невірна адреса POSSrv: ' + e.message); }
  t.ws = ws;
  let opened = false;
  ws.onopen = () => {
    opened = true;
    if (!live()) return;
    ws.send(JSON.stringify({ amount: t.amount }));
    termRender('progress', 'Очікую оплату на терміналі…');
  };
  ws.onmessage = ev => {
    if (!live()) return;
    let msg; try { msg = JSON.parse(ev.data); } catch (e) { msg = { raw: String(ev.data) }; }
    t.messages.push(msg);
    const kind = classifyTerm(msg);
    if (kind === 'approved') { t.finished = true; t.approvedMsg = msg; try { ws.close(); } catch (e) {} fiscalizeAfterTerm(); }
    else if (kind === 'declined') { t.finished = true; try { ws.close(); } catch (e) {} termRender('declined', 'Оплату не проведено: ' + (msg.error || `код ${msg.code}`)); }
    else termRender('progress', msg.error || msg.message || 'Очікую оплату на терміналі…');
  };
  const noConnection = () => termRender('conn', `Немає зв'язку з POSSrv (${termCfg.url}). Перевірте, що POSSrv запущено.`);
  ws.onerror = () => { if (!opened && live()) { t.finished = true; noConnection(); } };
  ws.onclose = () => {
    if (!live()) return;
    t.finished = true;
    if (opened) termRender('unknown', "З'єднання з терміналом завершилося без результату. Подивіться на екран терміналу: якщо оплата пройшла — натисніть «Оплата пройшла, ввести дані».");
    else noConnection();
  };
}
async function fiscalizeAfterTerm() {
  const t = term;
  termRender('approved', 'Оплату схвалено. Пробиваю чек…');
  const ok = await pay('CASHLESS', $('#payCard'), cardFromTerm(t.approvedMsg), null,
                      { marksChecked: true, terminalRaw: t.messages, noShiftPrompt: true });
  if (term !== t) return;
  if (ok) { term = null; $('#termModal').classList.remove('show'); return; }
  t.shiftClosed = (await shiftIsOpen()) === false;
  if (term !== t) return;
  termRender('fiscal_failed', t.shiftClosed
    ? 'Оплату на терміналі схвалено, але чек не пробито: зміна закрита. Відкрийте зміну — картку повторно не буде списано.'
    : 'Оплату на терміналі схвалено, але чек не пробито. Повторіть — картку повторно не буде списано.');
}
// Перед списанням з картки перевіряємо зміну: інакше гроші спишуться, а чек не проб'ється
async function beginTermFlow() {
  const t = term;
  t.messages = [];
  termRender('checking', 'Перевіряю зміну…');
  const open = await shiftIsOpen();
  if (term !== t) return;
  if (open === true) return startTermPayment();
  if (open === false) return termRender('shift_closed', 'Зміна закрита. Відкрийте її, щоб прийняти оплату — інакше чек не буде пробито.');
  termRender('shift_unknown', "Не вдалося перевірити зміну в Checkbox. Перевірте зв'язок і спробуйте ще раз.");
}
function openTermPay() {
  if (!cart.length) return toast('Кошик порожній', true);
  if (!confirmMarks()) return;
  term = { amount: cartTotal, messages: [] };
  $('#termSum').textContent = uah(term.amount) + ' ₴';
  $('#termDevName').textContent = termSummaryText(termCfg);
  $('#termModal').classList.add('show');
  beginTermFlow();
}
function closeTermPay() {
  if (!term) return $('#termModal').classList.remove('show');
  if (term.state === 'approved' || term.state === 'opening') return toast('Зачекайте — операція ще триває', true);
  if (term.state === 'fiscal_failed' &&
      !confirm('Оплату карткою вже проведено, а чек не пробито. Закрити без чека?')) return;
  if (['sending', 'progress'].includes(term.state)) {
    if (!confirm('Оплата ще триває. Скасування в програмі може не скасувати її на терміналі — перевірте екран терміналу. Закрити?')) return;
    term.finished = true; try { term.ws && term.ws.close(); } catch (e) {}
  }
  term = null; $('#termModal').classList.remove('show');
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
$('#termManual').onclick = () => {
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
async function loadShift() {
  try {
    const { shift } = await api('/api/shift');
    const b = $('#shiftBadge');
    if (shift) {
      b.textContent = `Зміна №${shift.serial ?? ''} відкрита`; b.className = 'badge open';
      $('#shiftInfo').innerHTML = `Зміна <b>№${esc(shift.serial)}</b> · статус <b>${esc(shift.status)}</b> · відкрита ${esc((shift.opened_at || '').replace('T', ' ').slice(0, 19))}`;
    } else {
      b.textContent = 'Зміна закрита'; b.className = 'badge';
      $('#shiftInfo').textContent = 'Зміна не відкрита.';
    }
  } catch (e) { $('#shiftBadge').textContent = 'Зміна: помилка'; toast(e.message, true); }
}
$('#openShift').onclick = e => busy(e.target, async () => { if (!(await openShiftNow())) throw new Error('Зміну не відкрито'); });
$('#closeShift').onclick = e => {
  const extra = pendingCount ? `\n\nПеред закриттям у Checkbox буде відправлено невідправлені зміни товарів: ${pendingCount}.` : '';
  if (!confirm('Закрити зміну і сформувати Z-звіт?' + extra)) return;
  busy(e.target, async () => {
    const r = await api('/api/shift/close', {});
    toast('Зміну закрито' + (r.z_report_id ? ', Z-звіт ' + r.z_report_id : '') + syncSummary(r.sync),
          !!(r.sync && (r.sync.sync_error || r.sync.push_error)));
    await loadShift(); loadGoods().catch(() => {});
  });
};
$('#xReport').onclick = e => busy(e.target, async () => {
  const r = await api('/api/xreport', {});
  const p = $('#reportText'); p.textContent = r.text; p.style.display = 'block';
});

// --- журнал ---
// Журнал: чеки за 7 днів, згруповані по днях у розгортуваних списках
const WEEKDAYS = ['неділя', 'понеділок', 'вівторок', 'середа', 'четвер', "п'ятниця", 'субота'];
function dayTitle(day) {           // day — «РРРР-ММ-ДД»
  const d = new Date(day + 'T00:00:00'), today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.round((today - d) / 86400000);
  const date = d.toLocaleDateString('uk-UA', { day: 'numeric', month: 'long' });
  return diff === 0 ? `Сьогодні, ${date}` : diff === 1 ? `Вчора, ${date}` : `${WEEKDAYS[d.getDay()]}, ${date}`;
}
const isReturn = s => (s.receipt_type || '').toUpperCase().includes('RETURN');
function payTitle(s) {
  const base = s.payment_type === 'CASH' ? 'Готівка' : s.payment_type ? 'Картка' : '—';
  return s.payment_label ? `${esc(s.payment_label)} <small class="muted">(${s.payment_type === 'CASH' ? 'готівка' : 'безготівка'})</small>` : base;
}
let openDays = null;    // які дні й зміни розгорнуті (щоб оновлення списку їх не згортало)
const hm = t => (t || '').slice(11, 16);
const dm = t => t ? `${t.slice(8, 10)}.${t.slice(5, 7)} ${hm(t)}` : '';
function totals(list) {
  const sum = t => list.filter(x => !isReturn(x) && (t ? x.payment_type === t : true)).reduce((a, x) => a + (x.total || 0), 0);
  const ret = list.filter(isReturn).reduce((a, x) => a + (x.total || 0), 0);
  return `чеків: ${list.length} · продажі ${uah(sum())} ₴
    <small class="muted">(готівка ${uah(sum('CASH'))}, безготівка ${uah(sum('CASHLESS'))})</small>${ret ? ` · повернення ${uah(ret)} ₴` : ''}`;
}
function shiftTitle(sh, day) {
  if (!sh.id) return 'Зміна невідома <small class="muted">(завантажте чеки з Checkbox, щоб підтягнути зміни)</small>';
  // час показуємо з датою, якщо зміна почалась або закінчилась не в цей день
  const fmt = t => t && t.slice(0, 10) !== day ? dm(t) : hm(t);
  const span = sh.opened ? `${fmt(sh.opened)} – ${sh.closed ? fmt(sh.closed) : 'відкрита'}` : '';
  return `Зміна №${esc(sh.serial || '?')}${span ? ` <small class="muted">· ${span}</small>` : ''}`;
}
function receiptRows(list) {
  return `<table><thead><tr><th>Час</th><th>Тип</th><th>Оплата</th><th class="num">Сума, ₴</th><th>Фіскальний №</th><th></th></tr></thead><tbody>
    ${list.map(s => `<tr class="click" data-id="${esc(s.receipt_id)}">
      <td>${esc(hm(s.created_at))}</td>
      <td>${isReturn(s) ? '<span class="zero">Повернення</span>' : 'Продаж'}</td>
      <td>${payTitle(s)}</td>
      <td class="num">${isReturn(s) ? '−' : ''}${uah(s.total)}</td>
      <td>${esc(s.fiscal_code || '—')}</td>
      <td class="muted" style="font-size:11px">${s.source === 'checkbox' ? 'з Checkbox' : 'з каси'}</td></tr>`).join('')}
    </tbody></table>`;
}
async function loadSales() {
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
  const tr = e.target.closest('tr[data-id]'); if (!tr) return;
  showReceipt(tr.dataset.id);
};
$('#fetchReceipts').onclick = e => busy(e.target, async () => {
  const r = await api('/api/sales/fetch', {});
  toast(`З Checkbox отримано чеків: ${r.fetched}, нових у журналі: ${r.new}` + (r.removed ? ` · видалено старих: ${r.removed}` : ''));
  await loadSales();
});

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
  if (cart.length && !confirm('У кошику є товари. Все одно зупинити касу?')) return;
  if (!cart.length && !confirm('Зупинити касу?')) return;
  try { await api('/api/quit', {}); } catch (e) {}
  document.body.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:8px;font:16px system-ui">
    <b style="font-size:20px">Касу зупинено</b><span style="color:#6b7280">Цю вкладку можна закрити. Щоб почати знову — запустіть Касу ще раз.</span></div>`;
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
  v.textContent = `Версія каси ${r.current}` + (r.enabled === false ? ' · оновлення не налаштовано'
    : r.error ? ` · ⚠ ${r.error}` : r.newer ? ` · доступна ${r.latest}` : r.latest ? ' · це остання версія' : '');
  const link = r.url ? ` · <a href="${esc(r.url)}" target="_blank" rel="noopener">що нового</a>` : '';
  if (r.available) {
    b.innerHTML = `<span>🎉 Доступна нова версія каси <b>${esc(r.latest)}</b> (у вас ${esc(r.current)})${link}</span>
      <button class="btn small" id="updateBtn">Оновити</button>`;
    b.style.display = ''; $('#updateBtn').onclick = runUpdate;
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
async function runUpdate() {
  const warn = cart.length ? 'У кошику є товари — після оновлення кошик буде порожній.\n\n' : '';
  if (!confirm(`${warn}Оновити касу до версії ${updateInfo.latest}? Каса перезапуститься — це до хвилини.`)) return;
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
                return Image.open(Path(folder) / "kasa.ico")
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
    Меню: «Відкрити касу» (також клік по значку), «Журнал роботи», «Вийти».
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
        pystray.MenuItem("Журнал роботи", open_logs),
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
            APP.api.signin()
            APP.auth_ok, APP.auth_error = True, None
            APP.cashier = APP.api.cashier_name()
            log.info("Авторизовано (%s)", auth_mode(cfg))
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
    kasa_update.cleanup_after_update()
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
        return
    log.info("Касу зупинено")
    print("\nКасу зупинено.")


if __name__ == "__main__":
    main()
