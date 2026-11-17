#!/usr/bin/env python3
"""
Проста облікова система для роботи з API Checkbox.

Можливості:
  - синхронізація всієї бази товарів (з пагінацією) у локальну SQLite;
  - перегляд і пошук товарів із цінами та залишками;
  - відкриття / закриття зміни;
  - продаж (готівка / безготівка) з кошиком;
  - X-звіт;
  - локальний журнал продажів і перегляд тексту чека.

Налаштування: config.json поруч зі скриптом (див. config.example.json)
або в папці з CHECKBOX_DATA_DIR. Налаштувати можна й на сторінці веб-версії.
Залежності: лише стандартна бібліотека Python 3.9+ (certifi — необов'язково).
"""

import http.client
import json
import logging
import logging.handlers
import os
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

APP_VERSION = "1.0.0"      # версія каси; тег випуску на GitHub — v + ця версія
# Репозиторій з випусками для оновлення («власник/назва»). Порожній — оновлення вимкнено.
GITHUB_REPO = "kowey420/checkbox-crm-kasa"

APP_DIR = Path(__file__).resolve().parent


def _default_data_dir():
    """
    Де зберігати config.json, checkbox.db і журнали:
      • CHECKBOX_DATA_DIR, якщо задано (так робить Каса.app на Mac);
      • зібраний Kasa.exe: поруч з exe, якщо там уже є config.json (портативний режим),
        інакше %LOCALAPPDATA%\\Каса Checkbox (у тимчасовій папці exe дані б зникали);
      • звичайний запуск .py — поруч зі скриптом.
    """
    env = os.environ.get("CHECKBOX_DATA_DIR")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "config.json").exists():
            return exe_dir
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Каса Checkbox"
    return APP_DIR


DATA_DIR = _default_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "checkbox.db"
LOG_DIR = DATA_DIR / "logs"
LOG_PATH = LOG_DIR / "kasa.log"

# Журнал роботи — у файл (logs/kasa.log, новий файл щодня, зберігаються 30 днів), а не в консоль
log = logging.getLogger("kasa")


def setup_logging():
    if log.handlers:
        return LOG_PATH
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.TimedRotatingFileHandler(
        LOG_PATH, when="midnight", backupCount=30, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return LOG_PATH


# ───────────────────────── HTTP без сторонніх бібліотек ─────────────────────────

class net:
    """
    Мінімальна заміна requests на urllib: Session.request(), Response,
    RequestException. Сертифікати — з certifi, якщо він є (Python з python.org
    на Mac без нього не перевіряє HTTPS), інакше — системні.
    """

    class RequestException(Exception):
        """Немає зв'язку, тайм-аут, помилка TLS тощо."""

    class Response:
        def __init__(self, status, headers, content):
            self.status_code = status
            self.headers = headers          # HTTPMessage: get() без урахування регістру
            self.content = content

        @property
        def text(self):
            return self.content.decode("utf-8", errors="replace")

        def json(self):
            return json.loads(self.content.decode("utf-8"))

    _ssl_context = None

    @classmethod
    def ssl_context(cls):
        if cls._ssl_context is None:
            try:
                import certifi
                cls._ssl_context = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                cls._ssl_context = ssl.create_default_context()
        return cls._ssl_context

    class Session:
        def __init__(self):
            self.headers = {}

        def request(self, method, url, params=None, json=None, timeout=30, data=None, content_type=None):
            if params:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
            headers = {"Accept": "application/json", **self.headers}
            if json is not None:
                data = _json_dumps(json).encode("utf-8")
                headers["Content-Type"] = "application/json"
            elif content_type:
                headers["Content-Type"] = content_type
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=net.ssl_context()) as r:
                    return net.Response(r.status, r.headers, r.read())
            except urllib.error.HTTPError as e:      # 4xx/5xx — це відповідь, а не обрив зв'язку
                return net.Response(e.code, e.headers, e.read())
            except (OSError, http.client.HTTPException) as e:  # URLError, тайм-аут, TLS, обрив з'єднання
                reason = getattr(e, "reason", e)
                raise net.RequestException(f"немає зв'язку з {urllib.parse.urlsplit(url).netloc}: {reason}") from e


def _json_dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def multipart_body(field, filename, content, mime="text/csv"):
    """Тіло multipart/form-data з одним файлом; повертає (bytes, content_type)."""
    boundary = "----kasa" + uuid.uuid4().hex
    safe_name = filename.replace('"', "")
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{safe_name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    return head + content + tail, f"multipart/form-data; boundary={boundary}"

# Адреса API статична; у config.json її можна перевизначити вручну, але в інтерфейсі вона не показується
DEFAULT_BASE_URL = "https://api.checkbox.in.ua/api/v1"
# Значення заголовків X-Client-Name / X-Client-Version, якщо в config.json їх немає
DEFAULT_CLIENT_NAME = "Petruk AM"
DEFAULT_CLIENT_VERSION = "alpha"
# значення, які помилково вшивались раніше: у конфігах їх замінюємо на актуальні
_OLD_DEFAULT_CLIENT = ("py-accounting", "1.0")
AUTH_MODES = ("password", "pin")
ROUNDING_MODES = ("ROUND_50", "ROUND_10")


def auth_mode(cfg):
    """Спосіб авторизації: явний auth_mode, а для старих конфігів — за наявністю pin_code."""
    mode = cfg.get("auth_mode")
    if mode in AUTH_MODES:
        return mode
    return "pin" if cfg.get("pin_code") else "password"


def header_value(value, default):
    """HTTP-заголовки приймають лише латиницю: кирилиця чи порожнє значення → значення за замовчуванням."""
    value = str(value or "").strip()
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return default
    return value or default


def load_config():
    if not CONFIG_PATH.exists():
        return {}
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if cfg.get("client_name") == _OLD_DEFAULT_CLIENT[0]:
        cfg["client_name"] = DEFAULT_CLIENT_NAME
        if cfg.get("client_version") in (None, "", _OLD_DEFAULT_CLIENT[1]):
            cfg["client_version"] = DEFAULT_CLIENT_VERSION
        try:
            save_config(cfg)          # виправляємо файл одразу, щоб значення не повернулось
        except OSError:
            pass
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)  # атомарний запис: файл не зіпсується, якщо програма впаде посередині


# ───────────────────────── API ─────────────────────────

class CheckboxError(Exception):
    pass


class CheckboxAPI:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = (cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self.s = net.Session()
        self.s.headers.update({
            "X-Client-Name": header_value(cfg.get("client_name"), DEFAULT_CLIENT_NAME),
            "X-Client-Version": header_value(cfg.get("client_version"), DEFAULT_CLIENT_VERSION),
            "X-License-Key": cfg.get("license_key") or "",
        })

    def _req(self, method, path, raw=False, **kw):
        started = time.monotonic()
        try:
            r = self.s.request(method, self.base + path, timeout=kw.pop("timeout", 30), **kw)
        except net.RequestException as e:
            log.error("Checkbox %s %s — %s", method, path, e)
            raise
        ms = int((time.monotonic() - started) * 1000)
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("message") or body
                detail = body.get("detail")
                if detail:
                    msg = f"{msg} | {detail}"
            except ValueError:
                msg = r.text
            log.warning("Checkbox %s %s → %s за %d мс: %s", method, path, r.status_code, ms, str(msg)[:1000])
            raise CheckboxError(f"HTTP {r.status_code}: {msg}")
        # тіла запитів не пишемо: там можуть бути пароль чи пін-код касира
        log.info("Checkbox %s %s → %s за %d мс", method, path, r.status_code, ms)
        if raw:
            return r.content
        if not r.content:
            return None
        if "json" in r.headers.get("Content-Type", ""):
            return r.json()
        return r.text

    # --- авторизація ---
    def signin(self):
        if not self.cfg.get("license_key"):
            raise CheckboxError("Не вказано ключ ліцензії каси")
        if auth_mode(self.cfg) == "pin":
            if not self.cfg.get("pin_code"):
                raise CheckboxError("Не вказано пін-код касира")
            data = self._req("POST", "/cashier/signinPinCode",
                             json={"pin_code": self.cfg["pin_code"]})
        else:
            if not self.cfg.get("login") or not self.cfg.get("password"):
                raise CheckboxError("Не вказано логін або пароль касира")
            data = self._req("POST", "/cashier/signin",
                             json={"login": self.cfg["login"],
                                   "password": self.cfg["password"]})
        self.s.headers["Authorization"] = f"Bearer {data['access_token']}"

    def cashier_name(self):
        """Ім'я касира для показу в інтерфейсі; None, якщо отримати не вдалося."""
        try:
            return (self._req("GET", "/cashier/me") or {}).get("full_name")
        except (CheckboxError, net.RequestException, AttributeError):
            return None

    # --- товари ---
    def get_all_goods(self, limit=100):
        offset, result, use_leftovers = 0, [], None
        while True:
            data = self._req("GET", "/goods",
                             params={"limit": limit, "offset": offset})
            batch = data.get("results", [])
            result.extend(batch)
            use_leftovers = data.get("use_leftovers", use_leftovers)
            if len(batch) < limit:
                return result, use_leftovers
            offset += limit

    # --- податкові ставки ---
    def get_taxes(self, goods=None):
        """
        Ставки організації. Повертає (список, джерело).
        Основне джерело: 1) GET /tax; якщо недоступно — 2) ставки з поточної зміни.
        Ставки, які є в товарах, але відсутні в основному джерелі, додаються завжди.
        """
        result, source = {}, "товари"
        try:
            data = self._req("GET", "/tax")
            items = data.get("results", data) if isinstance(data, dict) else data
            if isinstance(items, list) and items:
                result = {t["code"]: t for t in items}
                source = "GET /tax"
        except CheckboxError:
            pass
        if not result:
            shift = self.current_shift()
            if shift and shift.get("taxes"):
                result = {t["code"]: t for t in shift["taxes"]}
                source = "поточна зміна"
        added = 0
        for g in goods or []:
            for t in g.get("taxes") or []:
                if t["code"] not in result:
                    result[t["code"]] = t
                    added += 1
        if added and source != "товари":
            source += f" + товари ({added})"
        return sorted(result.values(), key=lambda t: t["code"]), source

    # --- імпорт / експорт номенклатури (асинхронні завдання Checkbox) ---
    IMPORT_UPLOAD_FIELD = "file"   # ім'я поля файлу в multipart

    @staticmethod
    def _task_id(data):
        tid = (data.get("task_id") or data.get("id")) if isinstance(data, dict) else None
        if not tid:
            raise CheckboxError(f"Checkbox не повернув task_id: {data}")
        return tid

    def goods_import_upload(self, filename, content, ignore_barcode_duplicates=True, auto_supply=False):
        mime = {"csv": "text/csv", "json": "application/json",
                "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}.get(
            filename.rsplit(".", 1)[-1].lower(), "application/octet-stream")
        body, ctype = multipart_body(self.IMPORT_UPLOAD_FIELD, filename, content, mime)
        data = self._req("POST", "/goods/import/upload", data=body, content_type=ctype, timeout=120,
                         params={"ignore_barcode_duplicates": str(ignore_barcode_duplicates).lower(),
                                 "auto_supply": str(auto_supply).lower()})
        return self._task_id(data)

    def goods_import_status(self, task_id):
        return self._req("GET", f"/goods/import/task_status/{task_id}")

    def goods_import_apply(self, task_id):
        return self._task_id(self._req("POST", f"/goods/import/apply_changes/{task_id}"))

    def goods_export_start(self, extension="csv"):
        return self._task_id(self._req("GET", f"/goods/export/{extension}"))

    def goods_export_status(self, task_id):
        return self._req("GET", f"/goods/export/task_status/{task_id}")

    def goods_export_file(self, task_id):
        return self._req("GET", f"/goods/export/file/{task_id}", raw=True, timeout=120)

    def wait_task(self, status_fn, task_id, ready, timeout=300, on_status=None):
        """
        Опитує статус завдання, поки він не стане одним із ready ("completed", "done").
        Статуси з error/fail/cancel — помилка. Повертає останню відповідь статусу.
        """
        started, last = time.monotonic(), None
        delay = 1
        while time.monotonic() - started < timeout:
            data = status_fn(task_id)
            status = str((data or {}).get("status", "")).lower() if isinstance(data, dict) else ""
            if status != last and on_status:
                on_status(status, data)
            last = status
            if status in ready:
                return data
            if any(w in status for w in ("error", "fail", "cancel", "reject")):
                raise CheckboxError(f"Завдання {task_id} завершилося зі статусом «{status}»: {data}")
            time.sleep(delay)
            delay = min(delay + 1, 5)
        raise CheckboxError(f"Завдання {task_id} не завершилося за {timeout} с (останній статус «{last}»)")

    # --- зміни ---
    def current_shift(self):
        return self._req("GET", "/cashier/shift")  # None, якщо зміни немає

    def _wait_shift(self, shift_id, target, timeout=60):
        start = time.time()
        while time.time() - start < timeout:
            shift = self._req("GET", f"/shifts/{shift_id}")
            if shift["status"] == target:
                return shift
            time.sleep(1)
        raise CheckboxError(f"Зміна не перейшла в статус {target} за {timeout} с")

    def open_shift(self):
        shift = self._req("POST", "/shifts")
        return self._wait_shift(shift["id"], "OPENED")

    def close_shift(self):
        shift = self._req("POST", "/shifts/close")
        return self._wait_shift(shift["id"], "CLOSED")

    # --- чеки ---
    def sell(self, receipt_id, goods, payment_type, pay_value, rounding_mode=None,
             payment_extra=None):
        """
        receipt_id    — UUID v4, який генерує інтеграція (обов'язково).
        pay_value     — сума оплати в копійках; для готівки із заокругленням —
                        вже заокруглена сума (або більша, якщо видається решта).
        payment_extra — додаткові поля платежу, напр. дані ЕПЗ для картки
                        (card_mask, rrn, auth_code, bank_name ...).
        """
        payment = {"type": payment_type, "value": pay_value}
        if payment_extra:
            payment.update(payment_extra)
        body = {
            "id": receipt_id,
            "goods": goods,
            "payments": [payment],
        }
        if payment_type == "CASH" and rounding_mode:
            body["rounding"] = True
            body["rounding_mode"] = rounding_mode
        try:
            self._req("POST", "/receipts/sell", json=body)
        except net.RequestException:
            # Зв'язок обірвався: перевіряємо, чи чек усе ж створено, щоб не пробити його двічі
            try:
                self._req("GET", f"/receipts/{receipt_id}")
            except (CheckboxError, net.RequestException):
                raise CheckboxError(
                    f"Немає зв'язку, і чек {receipt_id} не знайдено. "
                    "Перевірте в кабінеті перед повторним продажем.")
        return self._wait_receipt(receipt_id)

    def _wait_receipt(self, receipt_id, timeout=60):
        start = time.time()
        while time.time() - start < timeout:
            r = self._req("GET", f"/receipts/{receipt_id}")
            if r["status"] == "DONE":
                return r
            if r["status"] == "ERROR":
                raise CheckboxError(f"Чек {receipt_id} у статусі ERROR")
            time.sleep(1)
        raise CheckboxError(f"Чек {receipt_id} не фіскалізовано за {timeout} с")

    def receipt_text(self, receipt_id):
        return self._req("GET", f"/receipts/{receipt_id}/text")

    def search_receipts(self, date_from, date_to, limit=100):
        """
        Чеки за період (datetime з поясом). GET /receipts/search з пагінацією limit/offset.
        Повертає список чеків.
        """
        out, offset = [], 0
        while True:
            data = self._req("GET", "/receipts/search", params={
                "from_date": date_from.isoformat(timespec="seconds"),
                "to_date": date_to.isoformat(timespec="seconds"),
                "desc": "true", "limit": limit, "offset": offset})
            batch = (data or {}).get("results", []) if isinstance(data, dict) else (data or [])
            out.extend(batch)
            if len(batch) < limit or offset > 20000:
                return out
            offset += limit

    def receipt_png(self, receipt_id):
        """Зображення фіскалізованого чека (PNG, байти)."""
        return self._req("GET", f"/receipts/{receipt_id}/png", raw=True)

    # --- звіти ---
    def x_report(self):
        report = self._req("POST", "/reports")
        return self._req("GET", f"/reports/{report['id']}/text")


# ───────────────────────── Локальна БД ─────────────────────────

class LocalDB:
    def __init__(self, path=DB_PATH):
        # check_same_thread=False: веб-сервер багатопотоковий, доступ до бази серіалізує APP_LOCK
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS goods (
                id TEXT PRIMARY KEY,
                code TEXT, name TEXT, barcode TEXT,
                price INTEGER,           -- копійки
                count INTEGER,           -- тисячні (1000 = 1 шт / 1 кг)
                is_weight INTEGER,
                uktzed TEXT,
                synced_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sales (
                receipt_id TEXT PRIMARY KEY,
                created_at TEXT,
                fiscal_code TEXT,
                payment_type TEXT,
                total INTEGER,           -- копійки
                items TEXT               -- JSON
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS taxes (
                code INTEGER PRIMARY KEY,
                symbol TEXT, label TEXT,
                rate REAL, extra_rate REAL,
                included INTEGER, no_vat INTEGER
            );
            -- Правки товарів, ще не відправлені в Checkbox. Ключ — код товару в Checkbox
            -- (для нових — їхній код). Поверх даних Checkbox накладаються при кожній синхронізації.
            CREATE TABLE IF NOT EXISTS goods_pending (
                code TEXT PRIMARY KEY,
                action TEXT,          -- new | change
                data TEXT,            -- JSON: name, code, price, barcode, uktzed, is_weight, tax_codes
                original TEXT,        -- JSON рядка goods до першої правки (для «скасувати»)
                error TEXT,           -- остання помилка відправки в Checkbox
                updated_at TEXT
            );
        """)
        # міграція старої бази: колонка з кодами податків товару
        cols = [r["name"] for r in self.con.execute("PRAGMA table_info(goods)")]
        if "tax_codes" not in cols:
            self.con.execute("ALTER TABLE goods ADD COLUMN tax_codes TEXT")  # JSON-список кодів
        if "pending" not in cols:
            self.con.execute("ALTER TABLE goods ADD COLUMN pending TEXT")    # new | change | NULL
        scols = [r["name"] for r in self.con.execute("PRAGMA table_info(sales)")]
        if "payment_label" not in scols:
            self.con.execute("ALTER TABLE sales ADD COLUMN payment_label TEXT")
        if "payment_details" not in scols:   # JSON: дані ЕПЗ і сира відповідь терміналу
            self.con.execute("ALTER TABLE sales ADD COLUMN payment_details TEXT")
        if "receipt_type" not in scols:      # SELL / RETURN …
            self.con.execute("ALTER TABLE sales ADD COLUMN receipt_type TEXT")
        if "source" not in scols:            # kasa — пробито з цієї каси; checkbox — завантажено з Checkbox
            self.con.execute("ALTER TABLE sales ADD COLUMN source TEXT")
        for col in ("shift_id", "shift_serial", "shift_opened_at", "shift_closed_at"):   # зміна чека
            if col not in scols:
                self.con.execute(f"ALTER TABLE sales ADD COLUMN {col} TEXT")

    def replace_goods(self, goods, use_leftovers):
        now = datetime.now().isoformat(timespec="seconds")
        with self.con:
            self.con.execute("DELETE FROM goods")
            self.con.executemany(
                "INSERT INTO goods (id, code, name, barcode, price, count, is_weight, uktzed, "
                "synced_at, tax_codes) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(g["id"], g["code"], g["name"], g.get("barcode"),
                  g["price"], g.get("count"), int(bool(g.get("is_weight"))),
                  g.get("uktzed"), now,
                  json.dumps(ordered_tax_codes(g.get("taxes") or [])))
                 for g in goods])
            self.con.execute("INSERT OR REPLACE INTO meta VALUES ('use_leftovers', ?)",
                             (json.dumps(use_leftovers),))
            self.con.execute("INSERT OR REPLACE INTO meta VALUES ('synced_at', ?)", (now,))
            self._overlay_pending()        # невідправлені правки не губляться при синхронізації

    # --- невідправлені правки товарів ---
    LOCAL_ID_PREFIX = "local-"         # тимчасовий id нового товару, поки його немає в Checkbox

    def _overlay_one(self, code, action, data):
        fields = (data["name"], data["code"], data.get("barcode"), data["price"],
                  int(bool(data.get("is_weight"))), data.get("uktzed"), json.dumps(data.get("tax_codes") or []))
        if action == "new":
            self.con.execute("DELETE FROM goods WHERE id=?", (self.LOCAL_ID_PREFIX + code,))
            self.con.execute(
                "INSERT INTO goods (id, name, code, barcode, price, is_weight, uktzed, tax_codes, count, pending) "
                "VALUES (?,?,?,?,?,?,?,?,NULL,'new')", (self.LOCAL_ID_PREFIX + code,) + fields)
        else:
            self.con.execute(
                "UPDATE goods SET name=?, code=?, barcode=?, price=?, is_weight=?, uktzed=?, tax_codes=?, "
                "pending='change' WHERE TRIM(code)=?", fields + (code,))

    def _overlay_pending(self):
        for p in self.con.execute("SELECT * FROM goods_pending").fetchall():
            self._overlay_one(p["code"], p["action"], json.loads(p["data"]))

    def put_pending(self, good, existing=None):
        """
        Зберегти правку локально. existing — поточний рядок goods (None для нового товару).
        Повторні правки одного товару зливаються; оригінал зберігається з першої правки.
        """
        data = {k: good.get(k) for k in ("name", "code", "price", "barcode", "uktzed", "is_weight", "tax_codes")}
        now = datetime.now().isoformat(timespec="seconds")
        with self.con:
            if existing is not None and existing["pending"] == "new":
                # ще не відправлений новий товар: можна змінити навіть код
                old = existing["id"][len(self.LOCAL_ID_PREFIX):]
                self.con.execute("DELETE FROM goods_pending WHERE code=?", (old,))
                self.con.execute("DELETE FROM goods WHERE id=?", (existing["id"],))
                key, action, original = data["code"], "new", None
            elif existing is not None:
                key, action = (existing["code"] or "").strip(), "change"
                prev = self.con.execute("SELECT original FROM goods_pending WHERE code=?", (key,)).fetchone()
                original = prev["original"] if prev else json.dumps(dict(existing), ensure_ascii=False)
            else:
                key, action, original = data["code"], "new", None
            self.con.execute(
                "INSERT OR REPLACE INTO goods_pending (code, action, data, original, error, updated_at) "
                "VALUES (?,?,?,?,NULL,?)", (key, action, json.dumps(data, ensure_ascii=False), original, now))
            self._overlay_one(key, action, data)

    def pending(self):
        return [dict(r, data=json.loads(r["data"])) for r in
                self.con.execute("SELECT * FROM goods_pending ORDER BY updated_at").fetchall()]

    def pending_count(self):
        return self.con.execute("SELECT COUNT(*) FROM goods_pending").fetchone()[0]

    def clear_pending(self, codes):
        with self.con:
            self.con.executemany("DELETE FROM goods_pending WHERE code=?", [(c,) for c in codes])

    def set_pending_error(self, error):
        with self.con:
            self.con.execute("UPDATE goods_pending SET error=?", (error,))

    def discard_pending(self, code):
        """Скасувати правку: новий товар прибрати, змінений — повернути до стану Checkbox."""
        with self.con:
            p = self.con.execute("SELECT * FROM goods_pending WHERE code=?", (code,)).fetchone()
            if not p:
                return False
            if p["action"] == "new":
                self.con.execute("DELETE FROM goods WHERE id=?", (self.LOCAL_ID_PREFIX + code,))
            elif p["original"]:
                o = json.loads(p["original"])
                self.con.execute(
                    "UPDATE goods SET name=?, code=?, barcode=?, price=?, is_weight=?, uktzed=?, tax_codes=?, "
                    "pending=NULL WHERE id=?", (o["name"], o["code"], o["barcode"], o["price"], o["is_weight"],
                                                o["uktzed"], o["tax_codes"], o["id"]))
            self.con.execute("DELETE FROM goods_pending WHERE code=?", (code,))
            return True

    def replace_taxes(self, taxes, source):
        with self.con:
            self.con.execute("DELETE FROM taxes")
            self.con.executemany(
                "INSERT OR REPLACE INTO taxes VALUES (?,?,?,?,?,?,?)",
                [(t["code"], t.get("symbol"), t.get("label"), t.get("rate"), t.get("extra_rate"),
                  int(bool(t.get("included"))), int(bool(t.get("no_vat")))) for t in taxes])
            self.con.execute("INSERT OR REPLACE INTO meta VALUES ('taxes_source', ?)", (source,))

    def taxes(self):
        return self.con.execute("SELECT * FROM taxes ORDER BY code").fetchall()

    def meta(self, key):
        row = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row and key == "use_leftovers" else (row["value"] if row else None)

    def search(self, text=""):
        t = f"%{text.strip()}%"
        return self.con.execute(
            "SELECT * FROM goods WHERE name LIKE ? OR TRIM(code) LIKE ? OR barcode LIKE ? "
            "ORDER BY name", (t, t, t)).fetchall()

    def find_exact(self, text):
        t = text.strip()
        return self.con.execute(
            "SELECT * FROM goods WHERE TRIM(code)=? OR barcode=?", (t, t)).fetchone()

    def decrease_count(self, good_id, qty):
        with self.con:
            self.con.execute("UPDATE goods SET count = count - ? WHERE id=? AND count IS NOT NULL",
                             (qty, good_id))

    def add_sale(self, receipt, payment_type, items, payment_label=None, payment_details=None):
        with self.con:
            self.con.execute(
                "INSERT OR REPLACE INTO sales (receipt_id, created_at, fiscal_code, payment_type, "
                "total, items, payment_label, payment_details, receipt_type, source) "
                "VALUES (?,?,?,?,?,?,?,?, 'SELL', 'kasa')",
                (receipt["id"], datetime.now().isoformat(timespec="seconds"),
                 receipt.get("fiscal_code"), payment_type,
                 receipt.get("total_sum"), json.dumps(items, ensure_ascii=False), payment_label,
                 json.dumps(payment_details, ensure_ascii=False) if payment_details else None))
        self.set_receipt_shift(receipt)

    def sales(self, limit=50, since=None):
        if since:
            return self.con.execute("SELECT * FROM sales WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                                    (since, limit)).fetchall()
        return self.con.execute(
            "SELECT * FROM sales ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    @staticmethod
    def _shift_fields(r):
        sh = r.get("shift") or {}
        sid = sh.get("id") or r.get("shift_id")
        if not sid:
            return None
        return (sid, str(sh.get("serial")) if sh.get("serial") is not None else None,
                local_time(sh["opened_at"]) if sh.get("opened_at") else None,
                local_time(sh["closed_at"]) if sh.get("closed_at") else None)

    def set_receipt_shift(self, r):
        """Записати/оновити зміну чека (дані зміни, напр. час закриття, можуть з'явитися пізніше)."""
        f = self._shift_fields(r)
        if f:
            self.con.execute(
                "UPDATE sales SET shift_id=?, shift_serial=COALESCE(?, shift_serial), "
                "shift_opened_at=COALESCE(?, shift_opened_at), shift_closed_at=COALESCE(?, shift_closed_at) "
                "WHERE receipt_id=?", f + (r["id"],))
            self.con.commit()

    def add_checkbox_receipt(self, r):
        """
        Чек, завантажений з Checkbox. Якщо такий уже є (пробитий з цієї каси) — не чіпаємо:
        у локальному записі є дані ЕПЗ і відповідь терміналу, яких немає в Checkbox.
        Повертає True, якщо запис новий.
        """
        payments = r.get("payments") or []
        ptype = payments[0].get("type") if payments else None
        labels = [p.get("label") for p in payments if p.get("label")]
        label = ", ".join(dict.fromkeys(labels)) if labels else None
        if label in ("Готівка", "Картка", "Безготівка"):
            label = None                      # стандартні назви показуємо за типом оплати
        items = [{"name": (g.get("good") or {}).get("name"), "code": (g.get("good") or {}).get("code"),
                  "qty": g.get("quantity"), "price": (g.get("good") or {}).get("price")} for g in r.get("goods") or []]
        cur = self.con.execute(
            "INSERT OR IGNORE INTO sales (receipt_id, created_at, fiscal_code, payment_type, total, items, "
            "payment_label, receipt_type, source) VALUES (?,?,?,?,?,?,?,?, 'checkbox')",
            (r["id"], local_time(r.get("fiscal_date") or r.get("created_at")), r.get("fiscal_code"), ptype,
             r.get("total_sum"), json.dumps(items, ensure_ascii=False), label, r.get("type")))
        self.con.commit()
        self.set_receipt_shift(r)        # і для нових, і для вже наявних записів без зміни
        return cur.rowcount == 1

    def purge_sales(self, older_than):
        """Видалити записи журналу, старші за older_than (рядок часу ISO). Повертає id видалених чеків."""
        ids = [r[0] for r in self.con.execute("SELECT receipt_id FROM sales WHERE created_at < ?", (older_than,))]
        with self.con:
            self.con.execute("DELETE FROM sales WHERE created_at < ?", (older_than,))
        return ids


# ───────────────────────── Хелпери ─────────────────────────

def round_cash(total, mode):
    """
    Заокруглення суми готівкою (у копійках) за правилами НБУ.
    ROUND_50: 1–24 → вниз до 00; 25–74 → до 50; 75–99 → вгору до наступних 00.
    ROUND_10: 1–4 → вниз до 0; 5–9 → вгору до 10.
    Інше / None — без заокруглення.
    """
    if mode == "ROUND_50":
        return total - total % 100 + 50 * ((total % 100 + 25) // 50)
    if mode == "ROUND_10":
        return total - total % 10 + 10 * ((total % 10 + 5) // 10)
    return total


def is_excise_only(t):
    """Чистий акциз (без ПДВ): ставка 0 і є extra_rate, або в назві лише акциз."""
    label = (t.get("label") or "").lower()
    return (not t.get("rate") and t.get("extra_rate")) or (label.startswith("акциз") and "пдв" not in label)


def ordered_tax_codes(taxes):
    """Коди податків товару в порядку, який вимагає Checkbox: спершу ПДВ, потім акциз."""
    return [t["code"] for t in sorted(taxes, key=lambda t: 1 if is_excise_only(t) else 0)]


def good_tax_codes(row):
    try:
        return json.loads(row["tax_codes"] or "[]")
    except (ValueError, IndexError, KeyError):
        return []


def is_excise_tax(t):
    """Ставка містить акциз (окремий акциз або «ПДВ + Акцизний податок»)."""
    return bool(t.get("extra_rate")) or "акциз" in (t.get("label") or "").lower()


def build_goods_payload(cart, use_leftovers):
    """
    cart — список (row, qty_thousandths) або (row, qty_thousandths, excise_barcodes).
    good_id передається, якщо ведеться облік залишків.
    """
    payload = []
    for g, qty, *rest in cart:
        good = {"code": g["code"], "name": g["name"], "price": g["price"]}
        codes = good_tax_codes(g)
        if codes:
            good["tax"] = codes
        marks = rest[0] if rest else None
        if marks:
            good["excise_barcodes"] = list(marks)
        item = {"good": good, "quantity": qty}
        if use_leftovers and not str(g["id"]).startswith(LocalDB.LOCAL_ID_PREFIX):
            item["good_id"] = g["id"]   # нового (ще не синхронізованого) товару в Checkbox поки немає
        payload.append(item)
    return payload


PAYMENT_LABEL_MAX = 100


def clean_payment_label(label):
    """Назва форми оплати для чека: обрізані пробіли, обмеження довжини; порожня → None."""
    label = " ".join(str(label or "").split())
    if len(label) > PAYMENT_LABEL_MAX:
        raise ValueError(f"Назва оплати — не більше {PAYMENT_LABEL_MAX} символів")
    return label or None


def payment_title(payment_type, label=None):
    base = "Готівка" if payment_type == "CASH" else "Картка"
    return label or base


def new_receipt_id():
    return str(uuid.uuid4())


def local_time(value):
    """ISO-час із Checkbox (UTC або з поясом) → місцевий час без поясу, як у журналі каси."""
    if not value:
        return datetime.now().isoformat(timespec="seconds")
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)[:19]
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


def uah(kop):
    return "—" if kop is None else f"{kop / 100:.2f}"


def qty_str(thousandths, is_weight):
    if thousandths is None:
        return "—"
    v = thousandths / 1000
    return f"{v:.3f} кг" if is_weight else f"{v:g} шт"


def ask(prompt, default=None):
    v = input(f"{prompt}{f' [{default}]' if default is not None else ''}: ").strip()
    return v or (str(default) if default is not None else "")


def print_goods(rows, db=None):
    if not rows:
        print("  Нічого не знайдено.")
        return
    symbols = {t["code"]: t["symbol"] or str(t["code"]) for t in db.taxes()} if db else {}
    print(f"  {'Назва':40} {'Штрихкод':15} {'Ціна':>9} {'Залишок':>12}  Податок")
    print("  " + "─" * 88)
    for g in rows:
        tax = ",".join(symbols.get(c, str(c)) for c in good_tax_codes(g)) or "—"
        print(f"  {g['name'][:40]:40} {(g['barcode'] or '')[:15]:15} "
              f"{uah(g['price']):>9} {qty_str(g['count'], g['is_weight']):>12}  {tax}")
    print(f"  Всього: {len(rows)}")


# ───────────────────────── Дії меню ─────────────────────────

def sync_all(api, db):
    """Синхронізує товари і податкові ставки. Повертає (goods, use_leftovers, taxes, source)."""
    goods, use_leftovers = api.get_all_goods()
    db.replace_goods(goods, use_leftovers)
    taxes, source = api.get_taxes(goods)
    db.replace_taxes(taxes, source)
    return goods, use_leftovers, taxes, source


def no_tax_goods(db):
    return [r["name"] for r in db.con.execute("SELECT name, tax_codes FROM goods")
            if not good_tax_codes(r)]


def action_sync(api, db):
    goods, use_leftovers, taxes, source = sync_all(api, db)
    print(f"✔ Синхронізовано товарів: {len(goods)} (облік залишків: "
          f"{'увімкнено' if use_leftovers else 'вимкнено'})")
    print(f"✔ Податкових ставок: {len(taxes)} (джерело: {source})")
    missing = no_tax_goods(db)
    if missing:
        print(f"⚠ Товари без податкової ставки ({len(missing)}): {', '.join(missing[:10])}"
              f"{' …' if len(missing) > 10 else ''}")
    bad = [g for g in goods if g["code"] != g["code"].strip()]
    if bad:
        print(f"⚠ Товари з пробілами в коді ({len(bad)}):")
        for g in bad:
            print(f"   - {g['name']} → '{g['code']}'")


def action_list(api, db):
    print_goods(db.search(ask("Пошук (назва/код/штрихкод, Enter — усі)", "")), db)


def action_taxes(api, db):
    rows = db.taxes()
    if not rows:
        print("Ставок немає — синхронізуйте товари.")
        return
    print(f"  Джерело: {db.meta('taxes_source')}")
    print(f"  {'Код':>4} {'Літера':6} {'Назва':32} {'Ставка':>7} {'Акциз':>6}  Тип")
    print("  " + "─" * 70)
    for t in rows:
        kind = "без ПДВ" if t["no_vat"] else ("включено" if t["included"] else "зверху")
        print(f"  {t['code']:>4} {t['symbol'] or '':6} {(t['label'] or '')[:32]:32} "
              f"{(t['rate'] or 0):>6g}% {(t['extra_rate'] or 0):>5g}%  {kind}")


def action_shift_status(api, db):
    shift = api.current_shift()
    if not shift:
        print("Зміна не відкрита.")
    else:
        print(f"Зміна №{shift.get('serial')} — {shift['status']}, відкрита {shift.get('opened_at')}")


def action_open_shift(api, db):
    if api.current_shift():
        print("Зміна вже відкрита.")
        return
    shift = api.open_shift()
    print(f"✔ Зміну №{shift.get('serial')} відкрито.")


def action_close_shift(api, db):
    if not api.current_shift():
        print("Немає відкритої зміни.")
        return
    if ask("Закрити зміну і сформувати Z-звіт? (y/n)", "n").lower() != "y":
        return
    shift = api.close_shift()
    z = shift.get("z_report") or {}
    print(f"✔ Зміну закрито. Z-звіт: {z.get('id', '—')}")


def action_sell(api, db):
    if not api.current_shift():
        print("Спочатку відкрийте зміну.")
        return
    use_leftovers = db.meta("use_leftovers")
    cart = []  # (row, qty_thousandths, excise_barcodes)
    excise_codes = {t["code"] for t in db.taxes() if is_excise_tax(dict(t))}
    used_marks = set()

    while True:
        q = ask("Штрихкод / код / частина назви (Enter — до оплати)", "")
        if not q:
            break
        good = db.find_exact(q)
        if not good:
            found = db.search(q)
            if not found:
                print("  Не знайдено. Можливо, варто синхронізувати товари.")
                continue
            for i, g in enumerate(found[:10], 1):
                print(f"  {i}. {g['name']} — {uah(g['price'])} грн, "
                      f"залишок {qty_str(g['count'], g['is_weight'])}")
            idx = ask("Номер товару", "1")
            if not idx.isdigit() or not 1 <= int(idx) <= min(10, len(found)):
                continue
            good = found[int(idx) - 1]

        unit = "кг" if good["is_weight"] else "шт"
        try:
            qty = round(float(ask(f"Кількість, {unit}", 1).replace(",", ".")) * 1000)
        except ValueError:
            print("  Невірна кількість.")
            continue
        if qty <= 0 or (not good["is_weight"] and qty % 1000):
            print("  Невірна кількість.")
            continue

        in_cart = sum(c[1] for c in cart if c[0]["id"] == good["id"])
        if use_leftovers and good["count"] is not None and in_cart + qty > good["count"]:
            print(f"  ⚠ Недостатньо залишку: є {qty_str(good['count'], good['is_weight'])}.")
            continue

        marks = []
        if not good["is_weight"] and excise_codes.intersection(good_tax_codes(good)):
            need = qty // 1000
            print(f"  Акцизний товар: відскануйте марки ({need} шт), Enter — завершити")
            while len(marks) < need:
                m = ask(f"   Марка {len(marks) + 1}/{need}", "")
                if not m:
                    break
                if m in marks or m in used_marks:
                    print("   Ця марка вже додана.")
                    continue
                marks.append(m)
            if len(marks) < need:
                print(f"  ⚠ Додано марок: {len(marks)} з {need}")
            used_marks.update(marks)

        cart.append((good, qty, marks))
        print(f"  + {good['name']} × {qty_str(qty, good['is_weight'])}")

    if not cart:
        print("Кошик порожній.")
        return

    total = 0
    print("\nКошик:")
    for g, qty, _ in cart:
        line = round(g["price"] * qty / 1000)
        total += line
        print(f"  {g['name'][:40]:40} {qty_str(qty, g['is_weight']):>12} {uah(line):>10}")
    print(f"  {'РАЗОМ':40} {'':>12} {uah(total):>10}")

    rounding_mode = api.cfg.get("cash_rounding") or None
    to_pay_cash = round_cash(total, rounding_mode)
    if to_pay_cash != total:
        print(f"  {'Заокруглення':40} {'':>12} {uah(to_pay_cash - total):>10}")
        print(f"  {'До сплати готівкою':40} {'':>12} {uah(to_pay_cash):>10}")

    pay = ask("Оплата: 1 — готівка, 2 — картка, 3 — інше, 0 — скасувати", "1")
    if pay not in ("1", "2", "3"):
        print("Скасовано.")
        return
    payment_label = None
    if pay == "3":
        kind = ask("  У фіскальному чеку як: 1 — готівка, 2 — безготівка", "2")
        if kind not in ("1", "2"):
            print("Скасовано.")
            return
        payment_type = "CASH" if kind == "1" else "CASHLESS"
        try:
            payment_label = clean_payment_label(ask("  Назва оплати в чеку (напр. Сертифікат, Бонуси)", ""))
        except ValueError as e:
            print(e)
            return
        if not payment_label:
            print("Для «Інше» потрібна назва оплати.")
            return
    else:
        payment_type = "CASH" if pay == "1" else "CASHLESS"
    pay_value = total

    if payment_type == "CASH" and pay != "3":
        pay_value = to_pay_cash
        got = ask(f"Отримано готівкою, грн (Enter — без решти)", "")
        if got:
            try:
                got_kop = round(float(got.replace(",", ".")) * 100)
            except ValueError:
                print("Невірна сума.")
                return
            if got_kop < to_pay_cash:
                print(f"Недостатньо: до сплати {uah(to_pay_cash)} грн.")
                return
            pay_value = got_kop
            print(f"  Решта: {uah(got_kop - to_pay_cash)} грн")

    if payment_type == "CASH" and pay == "3":
        pay_value = to_pay_cash
    receipt = api.sell(new_receipt_id(), build_goods_payload(cart, use_leftovers),
                       payment_type, pay_value,
                       rounding_mode if payment_type == "CASH" else None,
                       {"label": payment_label} if payment_label else None)
    for g, qty, _ in cart:
        db.decrease_count(g["id"], qty)
    db.add_sale(receipt, payment_type,
                [{"name": g["name"], "code": g["code"], "qty": qty, "price": g["price"]}
                 for g, qty, _ in cart], payment_label)
    print(f"✔ Чек фіскалізовано. ID: {receipt['id']}, ФН: {receipt.get('fiscal_code')}, "
          f"сума: {uah(receipt.get('total_sum'))} грн")


def action_sales(api, db):
    rows = db.sales()
    if not rows:
        print("Продажів ще не було.")
        return
    for s in rows:
        title = payment_title(s["payment_type"], s["payment_label"])
        print(f"  {s['created_at']}  {title[:16]:16}  {uah(s['total']):>10} грн  "
              f"ФН {s['fiscal_code']}  {s['receipt_id']}")


def action_receipt_text(api, db):
    rid = ask("ID чека (Enter — останній)", "")
    if not rid:
        rows = db.sales(1)
        if not rows:
            print("Продажів ще не було.")
            return
        rid = rows[0]["receipt_id"]
    print(api.receipt_text(rid))


def action_x_report(api, db):
    if not api.current_shift():
        print("Немає відкритої зміни.")
        return
    print(api.x_report())


MENU = [
    ("Синхронізувати товари з Checkbox", action_sync),
    ("Переглянути / знайти товари", action_list),
    ("Податкові ставки організації", action_taxes),
    ("Статус зміни", action_shift_status),
    ("Відкрити зміну", action_open_shift),
    ("Продаж", action_sell),
    ("X-звіт", action_x_report),
    ("Закрити зміну (Z-звіт)", action_close_shift),
    ("Журнал продажів", action_sales),
    ("Текст чека", action_receipt_text),
]


def main():
    if not CONFIG_PATH.exists():
        sys.exit(f"Не знайдено {CONFIG_PATH.name}. Налаштуйте касу у веб-версії (кнопка «Налаштування») "
                 "або скопіюйте config.example.json → config.json")
    cfg = load_config()

    api = CheckboxAPI(cfg)
    db = LocalDB()
    try:
        api.signin()
    except (CheckboxError, net.RequestException) as e:
        sys.exit(f"Помилка авторизації: {e}")
    print("✔ Авторизовано.")
    if not db.meta("synced_at") or not db.taxes():  # порожня база або ще без податків
        print("Синхронізую товари і податкові ставки...")
        try:
            action_sync(api, db)
        except (CheckboxError, net.RequestException) as e:
            print(f"✖ {e}")

    while True:
        print(f"\n═══ Checkbox облік ═══  (товари оновлено: {db.meta('synced_at') or '—'})")
        for i, (title, _) in enumerate(MENU, 1):
            print(f"  {i}. {title}")
        print("  0. Вихід")
        choice = input("> ").strip()
        if choice == "0":
            break
        if not choice.isdigit() or not 1 <= int(choice) <= len(MENU):
            continue
        try:
            MENU[int(choice) - 1][1](api, db)
        except (CheckboxError, net.RequestException) as e:
            print(f"✖ {e}")
        except KeyboardInterrupt:
            print("\nПерервано.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
