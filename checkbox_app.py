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
import re
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

APP_VERSION = "1.4.6.4"      # версія каси; тег випуску на GitHub — v + ця версія
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

# Журнал роботи — у файл (logs/kasa.log, новий файл щодня, зберігаються 30 днів), а не в консоль.
# Формат — JSON-рядки як у loguru (serialize=True): {"text": "...", "record": {...}} — як у журналах Checkbox.
log = logging.getLogger("kasa")
_STARTED = time.time()
_LEVEL_ICONS = {"DEBUG": "🐞", "INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "❌", "CRITICAL": "☠️"}


class JsonLogFormatter(logging.Formatter):
    """
    Один запис — один рядок JSON. Додаткові дані — через extra={"data": {...}}: вони йдуть у record.extra.
    text: «2026-11-17 19:51:24.533 | INFO     | модуль:функція:рядок - повідомлення».
    """

    def format(self, record):
        message = record.getMessage()
        dt = datetime.fromtimestamp(record.created).astimezone()
        name = record.module if record.name == "kasa" else record.name
        text = (f"{dt.strftime('%Y-%m-%d %H:%M:%S')}.{int(record.msecs):03d} | {record.levelname:<8} | "
                f"{name}:{record.funcName}:{record.lineno} - {message}\n")
        exception = None
        if record.exc_info and record.exc_info[0]:
            exc_type, exc_value, _ = record.exc_info
            exception = {"type": exc_type.__name__, "value": str(exc_value), "traceback": True}
            text += self.formatException(record.exc_info) + "\n"
        elapsed = record.created - _STARTED
        h, rest = divmod(elapsed, 3600)
        m, sec = divmod(rest, 60)
        try:
            path = str(Path(record.pathname).relative_to(APP_DIR))
        except ValueError:
            path = record.pathname
        extra = getattr(record, "data", None)
        return json.dumps({"text": text, "record": {
            "elapsed": {"repr": f"{int(h)}:{int(m):02d}:{sec:09.6f}", "seconds": round(elapsed, 6)},
            "exception": exception,
            "extra": extra if isinstance(extra, dict) else {},
            "file": {"name": Path(record.pathname).name, "path": path},
            "function": record.funcName,
            "level": {"icon": _LEVEL_ICONS.get(record.levelname, ""), "name": record.levelname, "no": record.levelno},
            "line": record.lineno,
            "message": message,
            "module": record.module,
            "name": name,
            "process": {"id": record.process, "name": record.processName},
            "thread": {"id": record.thread, "name": record.threadName},
            "time": {"repr": dt.isoformat(sep=" "), "timestamp": record.created},
        }}, default=str)


def setup_logging():
    if log.handlers:
        return LOG_PATH
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.TimedRotatingFileHandler(
        LOG_PATH, when="midnight", backupCount=30, encoding="utf-8")
    handler.setFormatter(JsonLogFormatter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return LOG_PATH


# ───────── Що пишемо в журнал із запитів: без паролів, пін-кодів і токенів ─────────
_SECRET_KEYS = {"password", "pin_code", "pin", "access_token", "refresh_token", "token", "license_key",
                "secret", "authorization", "x-license-key"}
LOG_BODY_TEXT_MAX = 4000        # скільки символів тіла запиту показувати в тексті рядка журналу
LOG_RESPONSE_TEXT_MAX = 2000    # … і відповіді (повністю — у record.extra.response)
LOG_RESPONSE_MAX = 300000       # відповідь довша за це в extra обрізається (напр. великий список товарів)


def redact(obj):
    """Копія даних для журналу: секрети → «***», телефон покупця — з маскою."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = str(k).lower()
            if key in _SECRET_KEYS:
                out[k] = "***" if v else v
            elif key == "phone" and isinstance(v, str):
                out[k] = mask_phone(v)
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def log_body(kw):
    """Тіло запиту для журналу: JSON (без секретів) або короткий опис файлу; None — тіла немає."""
    if kw.get("json") is not None:
        return redact(kw["json"])
    data = kw.get("data")
    if data:
        return f"<{(kw.get('content_type') or 'дані').split(';')[0]}, {len(data)} байт>"
    return None


def log_json(obj, limit=LOG_BODY_TEXT_MAX):
    text = json.dumps(obj, ensure_ascii=False, default=str) if not isinstance(obj, str) else obj
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} символів)"


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
        """
        Сертифікати читаються в пам'ять одразу (каса викликає це при старті): у Kasa.exe файл certifi
        лежить у тимчасовій папці _MEI…, і якщо вона зникне під час роботи, HTTPS не має ламатися
        («[Errno 2] No such file or directory»). Немає файлу — беремо системні сертифікати Windows/Mac.
        """
        if cls._ssl_context is None:
            ctx = None
            try:
                import certifi
                with open(certifi.where(), encoding="ascii", errors="ignore") as f:
                    ctx = ssl.create_default_context(cadata=f.read())
            except ImportError:
                pass
            except (OSError, ssl.SSLError, ValueError) as e:
                log.warning("Сертифікати certifi недоступні (%s) — використовую системні", e)
            cls._ssl_context = ctx or ssl.create_default_context()
        return cls._ssl_context

    class Session:
        def __init__(self):
            self.headers = {}

        def request(self, method, url, params=None, json=None, timeout=30, data=None, content_type=None,
                    headers=None):
            if params:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
            headers = {"Accept": "application/json", **self.headers, **(headers or {})}
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
DEFAULT_CLIENT_NAME = "Checkbox CRM Kasa"
DEFAULT_CLIENT_VERSION = APP_VERSION          # завжди версія каси: оновилась каса — оновився заголовок
# значення, які вшивались раніше: з конфігів їх прибираємо, щоб діяли актуальні
_OLD_CLIENT_NAMES = ("py-accounting", "Petruk AM")
_OLD_CLIENT_VERSIONS = ("1.0", "alpha")
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
    # Client-Name / Client-Version: старі вшиті значення прибираємо — тоді діють «Checkbox CRM Kasa» і версія каси.
    # Власні значення в config.json (якщо їх вписали вручну) лишаються.
    changed = False
    if cfg.get("client_name") in _OLD_CLIENT_NAMES or cfg.get("client_name") == DEFAULT_CLIENT_NAME:
        cfg.pop("client_name")
        changed = True
    if "client_name" not in cfg and "client_version" in cfg:
        cfg.pop("client_version")                    # версія за замовчуванням — завжди поточна версія каси
        changed = True
    elif cfg.get("client_version") in _OLD_CLIENT_VERSIONS:
        cfg.pop("client_version")
        changed = True
    if changed:
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

    RATE_LIMIT_MAX_WAIT = 20      # с: довше на «занадто часто» (429) не чекаємо

    @staticmethod
    def _retry_after(r):
        """Скільки секунд просить зачекати Checkbox у відповіді 429 (заголовок або текст «через 15 секунд»)."""
        try:
            v = float(r.headers.get("Retry-After") or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
        try:
            text = json.dumps(r.json(), ensure_ascii=False)     # текст повідомлення без \u-екранування
        except ValueError:
            text = r.text or ""
        found = re.search(r"(\d+)\s*(?:секунд|сек|second|s\b)", text)
        return float(found.group(1)) if found else 5.0

    def _req(self, method, path, raw=False, **kw):
        timeout = kw.pop("timeout", 30)
        sent = log_body(kw)
        info = {"method": method, "path": path}
        if kw.get("params"):
            info["params"] = redact(kw["params"])
        if sent is not None:
            info["request"] = sent
        tail = f" | запит: {log_json(sent)}" if sent is not None else ""
        for attempt in range(3):
            started = time.monotonic()
            try:
                r = self.s.request(method, self.base + path, timeout=timeout, **kw)
            except net.RequestException as e:
                log.error("Checkbox %s %s — %s%s", method, path, e, tail, extra={"data": dict(info, error=str(e))})
                raise
            if r.status_code != 429 or attempt == 2:
                break
            # 429 «Занадто часто»: запит не виконано — чекаємо, скільки просить Checkbox, і повторюємо
            wait = min(self._retry_after(r) + 1, self.RATE_LIMIT_MAX_WAIT)
            log.warning("Checkbox %s %s → 429, повтор через %.0f с", method, path, wait,
                        extra={"data": dict(info, status=429, wait=wait)})
            time.sleep(wait)
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
            try:
                answer = redact(r.json())
            except ValueError:
                answer = (r.text or "")[:4000]
            log.warning("Checkbox %s %s → %s за %d мс: %s%s", method, path, r.status_code, ms, str(msg)[:1000], tail,
                        extra={"data": dict(info, status=r.status_code, ms=ms, response=answer)})
            raise CheckboxError(f"HTTP {r.status_code}: {msg}")
        # тіло запиту — без паролів, пін-кодів і токенів; відповідь — у extra.response (до LOG_RESPONSE_MAX),
        # її початок — у тексті рядка; зображення й файли — лише розмір
        done = dict(info, status=r.status_code, ms=ms)
        answer_tail = ""
        if r.content:
            if not raw and "json" in r.headers.get("Content-Type", ""):
                try:
                    answer = redact(r.json())
                    text = json.dumps(answer, ensure_ascii=False, default=str)
                    done["response"] = answer if len(text) <= LOG_RESPONSE_MAX else log_json(text, LOG_RESPONSE_MAX)
                    answer_tail = f" | відповідь: {log_json(text, LOG_RESPONSE_TEXT_MAX)}"
                except ValueError:
                    pass
            elif not raw and "text" in r.headers.get("Content-Type", ""):
                done["response"] = log_json(r.text, LOG_RESPONSE_MAX)
                answer_tail = f" | відповідь: {log_json(r.text, LOG_RESPONSE_TEXT_MAX)}"
            else:
                done["response"] = f"<{r.headers.get('Content-Type') or 'дані'}, {len(r.content)} байт>"
        log.info("Checkbox %s %s → %s за %d мс%s%s", method, path, r.status_code, ms, tail, answer_tail,
                 extra={"data": done})
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

    # --- групи товарів ---
    @staticmethod
    def _parse_group(g):
        if not isinstance(g, dict) or not g.get("id"):
            return None
        parent = g.get("parent_id") or g.get("parent_group_id") or g.get("parent")
        if isinstance(parent, dict):
            parent = parent.get("id")
        return {"id": str(g["id"]), "name": g.get("name") or g.get("title"), "parent_id": str(parent) if parent else None,
                "description": g.get("description")}

    def get_groups(self, want=None, limit=1000):
        """
        Групи й підгрупи товарів (GET /goods/groups). None — метод недоступний
        (тоді назви беремо з товарів чи вивантаження).
        want — id груп, на які посилаються товари: якщо якихось немає в списку (метод віддав лише
        верхній рівень), підгрупи дозапитуються через parent_id, рівень за рівнем.
        """
        try:
            result = self._groups_page({}, limit)
            have = {g["id"] for g in result}
            missing = set(want or ()) - have
            asked = set()
            while missing:
                todo = [g["id"] for g in result if g["id"] not in asked]
                if not todo or len(asked) > 500:
                    break
                for pid in todo:
                    asked.add(pid)
                    for g in self._groups_page({"parent_id": pid}, limit):
                        if g["id"] not in have:
                            g["parent_id"] = g["parent_id"] or pid
                            result.append(g)
                            have.add(g["id"])
                missing -= have
            if want and set(want) - have:
                log.info("Групи товарів: не знайдено в довіднику %d груп(и): %s", len(set(want) - have),
                         ", ".join(sorted(set(want) - have)))
            return result
        except (CheckboxError, net.RequestException) as e:
            log.info("Групи товарів через /goods/groups недоступні (%s) — назви з товарів", e)
            return None

    def _groups_page(self, params, limit):
        """Усі сторінки GET /goods/groups з параметрами; групи з children розгортаються в плаский список."""
        offset, result = 0, []
        if True:
            while True:
                data = self._req("GET", "/goods/groups", params=dict(params, limit=limit, offset=offset))
                batch = data.get("results", []) if isinstance(data, dict) else (data or [])
                for g in batch:
                    flat = [g]
                    while flat:                       # групи можуть приходити деревом (children)
                        item = flat.pop()
                        parsed = self._parse_group(item)
                        if parsed:
                            result.append(parsed)
                        for child in (item.get("children") or []) if isinstance(item, dict) else []:
                            if isinstance(child, dict):
                                if not child.get("parent_id"):
                                    child = dict(child, parent_id=item.get("id"))
                                flat.append(child)
                            elif isinstance(child, str) and parsed:     # дочірня група лише як id
                                result.append({"id": child, "name": None, "parent_id": parsed["id"]})
                if not isinstance(data, dict) or len(batch) < limit:
                    return result
                offset += limit

    # --- товар напряму (без імпорту файлу) ---
    def get_good(self, good_id):
        return self._req("GET", f"/goods/{good_id}")

    def update_good(self, good_id, body):
        return self._req("PUT", f"/goods/{good_id}", json=body)

    def create_good(self, body):
        return self._req("POST", "/goods", json=body)

    def create_group(self, name, description=None, parent_id=None):
        return self._req("POST", "/goods/groups", json={"name": name, "description": description, "parent_id": parent_id})

    def update_group(self, group_id, name, description=None, parent_id=None):
        return self._req("PUT", f"/goods/groups/{group_id}",
                         json={"name": name, "description": description, "parent_id": parent_id})

    def delete_group(self, group_id):
        return self._req("DELETE", f"/goods/groups/{group_id}")

    def move_group_goods(self, group_id, to_group):
        """Перенести всі товари групи в іншу групу."""
        return self._req("PUT", f"/goods/groups/{group_id}/move-goods", json={"to_group": to_group})

    def group_names_from_export(self):
        """{group_id: назва} з вивантаження номенклатури (колонка «група товарів») — запасний варіант."""
        import goods_csv                    # тут, щоб не було циклічного імпорту
        task = self.goods_export_start("csv")
        self.wait_task(self.goods_export_status, task, ready={"done"})
        f = goods_csv.CheckboxFile.from_bytes(self.goods_export_file(task))
        col = next((i for i, h in enumerate(f.header) if goods_csv._norm_header(h) in ("група товарів", "група")), None)
        code_col = next((i for i, h in enumerate(f.header) if goods_csv._norm_header(h) == "код"), None)
        if col is None or code_col is None:
            return {}
        return {str(r[code_col]).strip(): r[col].strip() for r in f.rows if len(r) > max(col, code_col) and r[col].strip()}

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
             payment_extra=None, discounts=None, related_receipt_id=None, delivery=None):
        """
        Чек продажу або повернення (для повернення в товарах is_return: true).
        receipt_id    — UUID v4, який генерує інтеграція (обов'язково).
        pay_value     — сума оплати в копійках; для готівки із заокругленням —
                        вже заокруглена сума (або більша, якщо видається решта).
        payment_extra — додаткові поля платежу, напр. дані ЕПЗ для картки
                        (card_mask, rrn, auth_code, bank_name ...).
        discounts     — знижки/надбавки на весь чек.
        related_receipt_id — для повернення: UUID чека продажу.
        """
        payment = {"type": payment_type, "value": pay_value}
        if payment_extra:
            payment.update(payment_extra)
        body = {
            "id": receipt_id,
            "goods": goods,
            "payments": [payment],
        }
        if discounts:
            body["discounts"] = discounts
        if related_receipt_id:
            body["related_receipt_id"] = related_receipt_id
        if delivery:
            body["delivery"] = delivery
        if payment_type == "CASH" and rounding_mode:
            body["rounding"] = True
            body["rounding_mode"] = rounding_mode
        return self._create_receipt("/receipts/sell", body)

    def service(self, receipt_id, value):
        """Службове внесення (value > 0) або винесення (value < 0) готівки, копійки."""
        return self._create_receipt("/receipts/service", {
            "id": receipt_id, "payment": {"type": "CASH", "value": value}})

    def _create_receipt(self, path, body):
        receipt_id = body["id"]
        try:
            self._req("POST", path, json=body)
        except net.RequestException:
            # Зв'язок обірвався: перевіряємо, чи чек усе ж створено, щоб не пробити його двічі
            try:
                self._req("GET", f"/receipts/{receipt_id}")
            except (CheckboxError, net.RequestException):
                raise CheckboxError(
                    f"Немає зв'язку, і чек {receipt_id} не знайдено. "
                    "Перевірте в кабінеті перед повторною спробою.")
        return self._wait_receipt(receipt_id)

    def get_receipt(self, receipt_id):
        return self._req("GET", f"/receipts/{receipt_id}")

    # --- передплата / післяплата (ланцюжок чеків одного замовлення) ---
    @staticmethod
    def relation_id_of(data):
        """pre_payment_relation_id з відповіді (чек або обгортка з чеком)."""
        if not isinstance(data, dict):
            return None
        for key in ("pre_payment_relation_id", "relation_id", "custom_relation_id"):
            if data.get(key):
                return str(data[key])
        for key in ("receipt", "pre_payment", "data"):
            rid = CheckboxAPI.relation_id_of(data.get(key))
            if rid:
                return rid
        return None

    def prepayment(self, receipt_id, goods, payment, discounts=None, delivery=None):
        """
        Чек передплати: усі товари замовлення, оплата — лише внесок (менше за суму товарів).
        Повертає (чек, pre_payment_relation_id).
        """
        body = {"id": receipt_id, "goods": goods, "payments": [payment]}
        if discounts:
            body["discounts"] = discounts
        if delivery:
            body["delivery"] = delivery
        resp = self._post_receipt("/prepayment-receipts", body)
        receipt = self._wait_receipt(receipt_id)
        return receipt, self.relation_id_of(resp) or self.relation_id_of(receipt)

    def postpayment(self, relation_id, receipt_id, payment, rounding_mode=None, delivery=None):
        """
        Наступний чек ланцюжка: оплата, менша за залишок, — ще одна передплата;
        рівна залишку (або більша, з рештою готівкою) — післяплата, що закриває замовлення.
        """
        body = {"id": receipt_id, "payments": [payment]}
        if delivery:
            body["delivery"] = delivery
        if payment.get("type") == "CASH" and rounding_mode:
            body["rounding"] = True
            body["rounding_mode"] = rounding_mode
        self._post_receipt(f"/prepayment-receipts/{relation_id}", body, headers={"relation_id": relation_id})
        return self._wait_receipt(receipt_id)

    def prepayment_state(self, relation_id):
        return self._req("GET", f"/prepayment-receipts/{relation_id}", headers={"relation_id": relation_id})

    def prepayment_return(self, relation_id):
        """Повернення всього ланцюжка: Checkbox створює чек повернення на кожен його чек."""
        return self._req("POST", f"/prepayment-receipts/{relation_id}/return",
                         json={}, headers={"relation_id": relation_id})

    def _post_receipt(self, path, body, headers=None):
        """POST чека; якщо зв'язок обірвався — перевіряємо, чи чек усе ж створено."""
        try:
            return self._req("POST", path, json=body, headers=headers)
        except net.RequestException:
            try:
                return self._req("GET", f"/receipts/{body['id']}")
            except (CheckboxError, net.RequestException):
                raise CheckboxError(
                    f"Немає зв'язку, і чек {body['id']} не знайдено. "
                    "Перевірте в кабінеті перед повторною спробою.")

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
    def create_x_report(self):
        """X-звіт поточної зміни; повертає об'єкт звіту (з id)."""
        return self._req("POST", "/reports")

    def x_report(self):
        return self.report_text(self.create_x_report()["id"])

    def report_text(self, report_id):
        return self._req("GET", f"/reports/{report_id}/text")

    def report_png(self, report_id):
        """Зображення X- чи Z-звіту (PNG, байти)."""
        return self._req("GET", f"/reports/{report_id}/png", raw=True)


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
        # Ланцюжки передплата → (передплата…) → післяплата одного замовлення
        self.con.execute("""CREATE TABLE IF NOT EXISTS prepayments (
            relation_id TEXT PRIMARY KEY,   -- pre_payment_relation_id у Checkbox
            created_at TEXT, note TEXT,
            total INTEGER,                  -- сума замовлення після знижок, копійки
            paid INTEGER,                   -- сплачено, копійки
            items TEXT,                     -- JSON товарів
            status TEXT,                    -- OPEN | PAID | RETURNED
            receipts TEXT,                  -- JSON: [{id, kind, value, payment_type, fiscal_code, at}]
            updated_at TEXT)""")
        # міграція старої бази: колонка з кодами податків товару
        cols = [r["name"] for r in self.con.execute("PRAGMA table_info(goods)")]
        if "group_id" not in cols:          # група товару в Checkbox
            self.con.execute("ALTER TABLE goods ADD COLUMN group_id TEXT")
        # групи й підгрупи товарів (parent_id — батьківська група)
        self.con.execute("CREATE TABLE IF NOT EXISTS goods_groups (id TEXT PRIMARY KEY, name TEXT, parent_id TEXT)")
        if "description" not in [r["name"] for r in self.con.execute("PRAGMA table_info(goods_groups)")]:
            self.con.execute("ALTER TABLE goods_groups ADD COLUMN description TEXT")
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
        if "related_id" not in scols:        # для повернення — UUID чека продажу
            self.con.execute("ALTER TABLE sales ADD COLUMN related_id TEXT")
        for col in ("shift_id", "shift_serial", "shift_opened_at", "shift_closed_at"):   # зміна чека
            if col not in scols:
                self.con.execute(f"ALTER TABLE sales ADD COLUMN {col} TEXT")

    def replace_goods(self, goods, use_leftovers):
        now = datetime.now().isoformat(timespec="seconds")
        with self.con:
            self.con.execute("DELETE FROM goods")
            self.con.executemany(
                "INSERT INTO goods (id, code, name, barcode, price, count, is_weight, uktzed, "
                "synced_at, tax_codes, group_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(g["id"], g["code"], g["name"], g.get("barcode"),
                  g["price"], g.get("count"), int(bool(g.get("is_weight"))),
                  g.get("uktzed"), now,
                  json.dumps(ordered_tax_codes(g.get("taxes") or [])), good_group_id(g))
                 for g in goods])
            self.con.execute("INSERT OR REPLACE INTO meta VALUES ('use_leftovers', ?)",
                             (json.dumps(use_leftovers),))
            self.con.execute("INSERT OR REPLACE INTO meta VALUES ('synced_at', ?)", (now,))
            self._overlay_pending()        # невідправлені правки не губляться при синхронізації

    # --- групи товарів ---
    def group_rows(self):
        return [dict(r) for r in self.con.execute("SELECT id, name, parent_id, description FROM goods_groups")]

    def replace_groups(self, groups):
        """groups — [{id, name, parent_id}] з Checkbox; назви, відомі раніше, не губимо, якщо нових немає."""
        with self.con:
            known = {r["id"]: r["name"] for r in self.con.execute("SELECT id, name FROM goods_groups")}
            self.con.execute("DELETE FROM goods_groups")
            merged = {}
            for g in groups:              # та сама група може прийти і в списку, і в children батьківської
                m = merged.setdefault(g["id"], {"name": None, "parent_id": None, "description": None})
                m["name"] = m["name"] or g.get("name")
                m["parent_id"] = m["parent_id"] or g.get("parent_id")
                m["description"] = m["description"] or g.get("description")
            self.con.executemany("INSERT OR REPLACE INTO goods_groups (id, name, parent_id, description) VALUES (?,?,?,?)",
                                 [(gid, m["name"] or known.get(gid), m["parent_id"], m["description"])
                                  for gid, m in merged.items()])

    def set_group_names(self, names):
        """Назви груп з іншого джерела (group_name товару, вивантаження) — лише для груп без назви."""
        with self.con:
            for gid, name in names.items():
                self.con.execute("INSERT INTO goods_groups (id, name) VALUES (?, ?) "
                                 "ON CONFLICT(id) DO UPDATE SET name=COALESCE(goods_groups.name, excluded.name)",
                                 (gid, name))

    def groups(self):
        """Групи з повним шляхом: [{id, name, parent_id, path: [назви від кореня]}]."""
        rows = {r["id"]: dict(r) for r in self.con.execute("SELECT id, name, parent_id, description FROM goods_groups")}
        counts = dict(self.con.execute("SELECT group_id, COUNT(*) FROM goods WHERE group_id IS NOT NULL GROUP BY group_id").fetchall())
        used = {r["group_id"] for r in self.con.execute("SELECT DISTINCT group_id FROM goods WHERE group_id IS NOT NULL")}
        for gid in used - rows.keys():       # група товару, якої немає в довіднику
            rows[gid] = {"id": gid, "name": None, "parent_id": None, "description": None}

        def path(gid, seen=()):
            g = rows.get(gid)
            if not g or gid in seen:
                return []
            name = g["name"] or f"Група {gid[:8]}"
            return path(g["parent_id"], seen + (gid,)) + [name] if g["parent_id"] else [name]

        for g in rows.values():
            g["path"] = path(g["id"])
            g["goods"] = counts.get(g["id"], 0)          # товарів саме в цій групі (без підгруп)
        return sorted(rows.values(), key=lambda g: [p.lower() for p in g["path"]])

    def groups_without_names(self):
        return {r["group_id"] for r in self.con.execute(
            "SELECT DISTINCT g.group_id FROM goods g LEFT JOIN goods_groups gg ON gg.id = g.group_id "
            "WHERE g.group_id IS NOT NULL AND gg.name IS NULL")}

    # --- невідправлені правки товарів ---
    LOCAL_ID_PREFIX = "local-"         # тимчасовий id нового товару, поки його немає в Checkbox

    def _overlay_one(self, code, action, data):
        fields = (data["name"], data["code"], data.get("barcode"), data["price"],
                  int(bool(data.get("is_weight"))), data.get("uktzed"), json.dumps(data.get("tax_codes") or []))
        stock = data.get("stock")          # бажаний залишок (тисячні) — піде в Checkbox поставкою
        if action == "new":
            self.con.execute("DELETE FROM goods WHERE id=?", (self.LOCAL_ID_PREFIX + code,))
            self.con.execute(
                "INSERT INTO goods (id, name, code, barcode, price, is_weight, uktzed, tax_codes, count, pending) "
                "VALUES (?,?,?,?,?,?,?,?,?,'new')", (self.LOCAL_ID_PREFIX + code,) + fields + (stock,))
        else:
            self.con.execute(
                "UPDATE goods SET name=?, code=?, barcode=?, price=?, is_weight=?, uktzed=?, tax_codes=?, "
                "count=COALESCE(?, count), pending='change' WHERE TRIM(code)=?", fields + (stock, code))
        if "group_id" in data:             # група — лише якщо правка її задає (старі правки без групи не чіпають)
            self.con.execute("UPDATE goods SET group_id=? WHERE TRIM(code)=? OR id=?",
                             (data["group_id"], code, self.LOCAL_ID_PREFIX + code))

    def _overlay_pending(self):
        for p in self.con.execute("SELECT * FROM goods_pending").fetchall():
            self._overlay_one(p["code"], p["action"], json.loads(p["data"]))

    def put_pending(self, good, existing=None):
        """
        Зберегти правку локально. existing — поточний рядок goods (None для нового товару).
        Повторні правки одного товару зливаються; оригінал зберігається з першої правки.
        """
        data = {k: good.get(k) for k in ("name", "code", "price", "barcode", "uktzed", "is_weight", "tax_codes", "stock")}
        if "group_id" in good:
            data["group_id"] = good["group_id"]
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

    def checkbox_count(self, row):
        """Залишок товару в Checkbox (до локальної правки залишку); None — залишок не ведеться."""
        if row["pending"] == "new":
            return None
        p = self.con.execute("SELECT original FROM goods_pending WHERE code=?", ((row["code"] or "").strip(),)).fetchone()
        if p and p["original"]:
            return json.loads(p["original"]).get("count")
        return row["count"]

    def clear_pending(self, codes):
        with self.con:
            self.con.executemany("DELETE FROM goods_pending WHERE code=?", [(c,) for c in codes])

    def set_pending_error(self, error, code=None):
        with self.con:
            if code is None:
                self.con.execute("UPDATE goods_pending SET error=?", (error,))
            else:
                self.con.execute("UPDATE goods_pending SET error=? WHERE code=?", (error, code))

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

    def add_sale(self, receipt, payment_type, items, payment_label=None, payment_details=None,
                 receipt_type="SELL", related_id=None, total=None):
        with self.con:
            self.con.execute(
                "INSERT OR REPLACE INTO sales (receipt_id, created_at, fiscal_code, payment_type, "
                "total, items, payment_label, payment_details, receipt_type, source, related_id) "
                "VALUES (?,?,?,?,?,?,?,?,?, 'kasa', ?)",
                (receipt["id"], datetime.now().isoformat(timespec="seconds"),
                 receipt.get("fiscal_code"), payment_type,
                 abs(total if total is not None else (receipt.get("total_sum") or 0)),
                 json.dumps(items, ensure_ascii=False), payment_label,
                 json.dumps(payment_details, ensure_ascii=False) if payment_details else None,
                 receipt_type, related_id))
        self.set_receipt_shift(receipt)

    def returned_qty(self, sale_id):
        """Скільки вже повернуто за чеком продажу: {позиція: тисячні}. Позиція — номер рядка чека."""
        out = {}
        for r in self.con.execute("SELECT items FROM sales WHERE receipt_type='RETURN' AND lower(related_id)=lower(?)",
                                  (sale_id,)):
            for it in json.loads(r["items"] or "[]"):
                key = it.get("line")
                if key is None:
                    key = "code:" + str(it.get("code"))
                out[key] = out.get(key, 0) + (it.get("qty") or 0)
        return out

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
            "payment_label, receipt_type, source, related_id) VALUES (?,?,?,?,?,?,?,?, 'checkbox', ?)",
            (r["id"], local_time(r.get("fiscal_date") or r.get("created_at")), r.get("fiscal_code"), ptype,
             abs(r.get("total_sum") or 0), json.dumps(items, ensure_ascii=False), label, r.get("type"),
             r.get("related_receipt_id")))
        self.con.commit()
        self.set_receipt_shift(r)        # і для нових, і для вже наявних записів без зміни
        return cur.rowcount == 1

    # --- передплати ---
    def add_prepayment(self, relation_id, note, total, paid, items, receipt):
        now = datetime.now().isoformat(timespec="seconds")
        with self.con:
            self.con.execute(
                "INSERT INTO prepayments (relation_id, created_at, note, total, paid, items, status, receipts, updated_at) "
                "VALUES (?,?,?,?,?,?, 'OPEN', ?, ?)",
                (relation_id, now, note, total, paid, json.dumps(items, ensure_ascii=False),
                 json.dumps([receipt], ensure_ascii=False), now))

    def prepayment(self, relation_id):
        r = self.con.execute("SELECT * FROM prepayments WHERE relation_id=?", (relation_id,)).fetchone()
        return self._prepayment_dict(r) if r else None

    @staticmethod
    def _prepayment_dict(r):
        d = dict(r)
        d["items"] = json.loads(d["items"] or "[]")
        d["receipts"] = json.loads(d["receipts"] or "[]")
        d["left"] = max((d["total"] or 0) - (d["paid"] or 0), 0)
        return d

    def update_prepayment(self, relation_id, paid=None, status=None, receipt=None):
        p = self.prepayment(relation_id)
        receipts = p["receipts"] + ([receipt] if receipt else [])
        with self.con:
            self.con.execute(
                "UPDATE prepayments SET paid=?, status=?, receipts=?, updated_at=? WHERE relation_id=?",
                (p["paid"] if paid is None else paid, status or p["status"], json.dumps(receipts, ensure_ascii=False),
                 datetime.now().isoformat(timespec="seconds"), relation_id))

    def set_prepayment_state(self, relation_id, total, paid, status=None):
        """Суми й статус замовлення з Checkbox."""
        with self.con:
            self.con.execute("UPDATE prepayments SET total=?, paid=?, status=COALESCE(?, status), updated_at=? "
                             "WHERE relation_id=?",
                             (total, paid, status, datetime.now().isoformat(timespec="seconds"), relation_id))

    def prepayments(self, limit=200):
        return [self._prepayment_dict(r) for r in self.con.execute(
            "SELECT * FROM prepayments ORDER BY CASE status WHEN 'OPEN' THEN 0 ELSE 1 END, created_at DESC LIMIT ?",
            (limit,))]

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


# поля тіла PUT /goods/{id}, які переносимо з поточного товару Checkbox, щоб їх не затерти
_GOOD_KEEP = ("name", "short_name", "type", "code", "is_weight", "barcode", "uktzed", "price", "position",
              "external_id", "source")


def good_put_body(current, data):
    """
    Тіло PUT /goods/{id} (або POST /goods, якщо current = None): поточний стан товару в Checkbox
    + локальні правки data (name, code, price, barcode, uktzed, is_weight, tax_codes, group_id).
    """
    body = {}
    if current:
        for k in _GOOD_KEEP:
            if current.get(k) is not None:
                body[k] = current[k]
        bc = current.get("barcodes")
        if not isinstance(bc, list):
            rel = current.get("related_barcodes")
            bc = [x for x in re.split(r"[,;\s]+", rel) if x] if isinstance(rel, str) else None
        if bc:
            body["barcodes"] = bc
        if good_group_id(current):
            body["group"] = good_group_id(current)
        body["tax_codes"] = ordered_tax_codes(current.get("taxes") or [])
        kids = [c.get("id") if isinstance(c, dict) else c for c in current.get("children") or []]
        if kids:
            body["children"] = [k for k in kids if k]
        if isinstance(current.get("branches_info"), list):
            body["branches_info"] = current["branches_info"]
    body.setdefault("type", "good")
    for k in ("name", "code", "price", "barcode", "uktzed"):
        if k in data:
            body[k] = data[k] if data[k] not in ("",) else None
    if "is_weight" in data:
        body["is_weight"] = bool(data["is_weight"])
    if "tax_codes" in data:
        body["tax_codes"] = list(data["tax_codes"] or [])
    if "group_id" in data:
        body["group"] = data["group_id"] or None
    if body.get("barcode") and body.get("barcodes") is not None and current and current.get("barcode") \
            and current["barcode"] != body["barcode"]:
        # основний штрихкод змінено — старий прибираємо зі списку, новий додаємо
        body["barcodes"] = [b for b in body["barcodes"] if b != current["barcode"]]
    return body


def good_group_id(g):
    """Група товару з відповіді /goods: group_id або вкладений об'єкт group."""
    gid = g.get("group_id")
    if not gid and isinstance(g.get("group"), dict):
        gid = g["group"].get("id")
    return str(gid) if gid else None


def build_goods_payload(cart, use_leftovers, line_discounts=None):
    """
    cart — список (row, qty_thousandths) або (row, qty_thousandths, excise_barcodes).
    good_id передається, якщо ведеться облік залишків.
    line_discounts — знижки/надбавки кожного рядка (з apply_discounts).
    """
    payload = []
    for i, (g, qty, *rest) in enumerate(cart):
        good = {"code": g["code"], "name": g["name"], "price": g["price"]}
        codes = good_tax_codes(g)
        if codes:
            good["tax"] = codes
        marks = rest[0] if rest else None
        if marks:
            good["excise_barcodes"] = list(marks)
        item = {"good": good, "quantity": qty}
        if line_discounts and line_discounts[i]:
            item["discounts"] = line_discounts[i]
        if use_leftovers and not str(g["id"]).startswith(LocalDB.LOCAL_ID_PREFIX):
            item["good_id"] = g["id"]   # нового (ще не синхронізованого) товару в Checkbox поки немає
        payload.append(item)
    return payload


# ───────────── Знижки й надбавки ─────────────
# Інтерфейс задає знижку відсотком або сумою, а в Checkbox вона завжди йде абсолютним значенням
# (mode VALUE, копійки) — так рекомендує документація: сума в чеку не залежить від заокруглень сервера.

DISCOUNT_TYPES = {"DISCOUNT": "Знижка", "EXTRA_CHARGE": "Надбавка"}
DISCOUNT_NAME_MAX = 100


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def discount_amount(spec, base, where):
    """
    spec — {"type": DISCOUNT|EXTRA_CHARGE, "mode": PERCENT|VALUE, "value": …, "name": …};
    для VALUE value — копійки (ціле), для PERCENT — відсоток (до двох знаків після коми).
    base — сума, від якої рахується знижка (копійки). Повертає payload знижки Checkbox або None.
    """
    if not spec:
        return None
    if not isinstance(spec, dict):
        raise ValueError(f"Невірна знижка: {where}")
    kind, mode, v = spec.get("type"), spec.get("mode"), spec.get("value")
    if kind not in DISCOUNT_TYPES or mode not in ("PERCENT", "VALUE") or not _num(v) or v <= 0:
        raise ValueError(f"Невірна знижка: {where}")
    title = DISCOUNT_TYPES[kind]
    if mode == "PERCENT":
        pv = round(v * 100)                     # соті частки відсотка
        if abs(v * 100 - pv) > 1e-6:
            raise ValueError(f"{where}: відсоток — не більше двох знаків після коми")
        if kind == "DISCOUNT" and pv >= 10000:
            raise ValueError(f"{where}: знижка має бути меншою за 100%")
        if pv > 100000:
            raise ValueError(f"{where}: надбавка — не більше 1000%")
        amount = (base * pv + 5000) // 10000    # заокруглення до копійки (0.5 — вгору)
        default = f"{title} {v:g}%"
    else:
        if not isinstance(v, int):
            raise ValueError(f"{where}: сума знижки — у копійках")
        amount, default = v, title
    if amount <= 0:
        raise ValueError(f"{where}: {title.lower()} менша за 1 копійку")
    if kind == "DISCOUNT" and amount >= base:
        raise ValueError(f"{where}: знижка має бути меншою за суму ({base / 100:.2f} грн)")
    name = " ".join(str(spec.get("name") or "").split())[:DISCOUNT_NAME_MAX] or default
    return {"type": kind, "mode": "VALUE", "value": amount, "name": name}


def tax_fields(codes):
    """Податки, від яких рахується знижка: tax_code для однієї ставки, tax_codes — для кількох."""
    codes = list(codes or [])
    if not codes:
        return {}
    return {"tax_code": codes[0]} if len(codes) == 1 else {"tax_codes": codes}


def discount_delta(d):
    return -d["value"] if d["type"] == "DISCOUNT" else d["value"]


def apply_discounts(lines, receipt_spec=None):
    """
    lines — список {"base": копійки, "tax_codes": [...], "spec": знижка рядка або None, "name": …}.
    Повертає (знижки кожного рядка, знижки чека, суми рядків після знижок, разом).
    Знижка на чек іде в чек цілою, якщо всі товари мають однакові податки;
    інакше її розподілено між товарами пропорційно сумі (щоб податки рахувались правильно).
    """
    per_line, totals = [], []
    for i, ln in enumerate(lines):
        ds = []
        d = discount_amount(ln.get("spec"), ln["base"], ln.get("name") or f"позиція {i + 1}")
        if d:
            d.update(tax_fields(ln["tax_codes"]))
            ds.append(d)
        per_line.append(ds)
        totals.append(ln["base"] + sum(discount_delta(x) for x in ds))
    sub = sum(totals)
    receipt_ds = []
    rd = discount_amount(receipt_spec, sub, "знижка на чек") if receipt_spec else None
    if rd:
        tax_sets = {tuple(ln["tax_codes"] or ()) for ln in lines}
        if len(tax_sets) == 1:
            rd.update(tax_fields(next(iter(tax_sets))))
            receipt_ds.append(rd)
            return per_line, receipt_ds, totals, sub + discount_delta(rd)
        amount = rd["value"]
        shares = [amount * t // sub for t in totals]
        for i in sorted(range(len(totals)), key=lambda k: -totals[k])[:amount - sum(shares)]:
            shares[i] += 1
        for i, share in enumerate(shares):
            if share <= 0:
                continue
            if rd["type"] == "DISCOUNT" and share >= totals[i]:
                raise ValueError("Знижка на чек завелика для розподілу між товарами")
            x = {"type": rd["type"], "mode": "VALUE", "value": share, "name": rd["name"],
                 **tax_fields(lines[i]["tax_codes"])}
            per_line[i].append(x)
            totals[i] += discount_delta(x)
    return per_line, receipt_ds, totals, sum(totals)


# ───────────── Повернення ─────────────

def _line_tax_codes(line):
    """Коди податків рядка з чека Checkbox (good.tax — коди або об'єкти; або line.taxes)."""
    good = line.get("good") or {}
    objs = [t for t in (line.get("taxes") or []) if isinstance(t, dict) and t.get("code") is not None]
    if objs:
        return ordered_tax_codes(objs)
    codes = []
    for t in good.get("tax") or []:
        c = t.get("code") if isinstance(t, dict) else t
        if c is not None and c not in codes:
            codes.append(c)
    return codes


def _line_total(line):
    """Сума рядка після його знижок/надбавок, копійки."""
    if isinstance(line.get("total_sum"), int):
        return line["total_sum"]
    good = line.get("good") or {}
    base = line.get("sum") if isinstance(line.get("sum"), int) else line_sum(good.get("price", 0), line.get("quantity", 0))
    for d in line.get("discounts") or []:
        amount = d.get("sum") if isinstance(d.get("sum"), int) else (d.get("value") if d.get("mode") == "VALUE" else 0)
        base += -amount if d.get("type") == "DISCOUNT" else amount
    return base


def share_of(total, part, whole):
    """Частка суми total, що припадає на part із whole (заокруглення 0.5 — вгору)."""
    if whole <= 0:
        return 0
    return (total * part * 2 + whole) // (2 * whole)


def return_lines(receipt, returned):
    """
    Рядки чека продажу для повернення. Сума кожного рядка враховує його знижки
    і частку знижки на весь чек, тож повне повернення дає рівно суму продажу.
    returned — вже повернуто: {номер рядка або "code:<код>": тисячні}.
    """
    src = [ln for ln in receipt.get("goods") or [] if not ln.get("is_return")]
    totals = [_line_total(ln) for ln in src]
    receipt_disc = sum(totals) - receipt["total_sum"] if isinstance(receipt.get("total_sum"), int) else 0
    if receipt_disc:                       # знижка/надбавка на весь чек → розподіл між рядками
        shares = [share_of(receipt_disc, t, sum(totals)) for t in totals]
        shares[-1] += receipt_disc - sum(shares)
        totals = [t - sh for t, sh in zip(totals, shares)]
    by_code = dict((k[5:], v) for k, v in returned.items() if isinstance(k, str) and k.startswith("code:"))
    out = []
    for i, (ln, eff) in enumerate(zip(src, totals)):
        good = ln.get("good") or {}
        qty = ln.get("quantity") or 0
        done = returned.get(i, 0)
        code = str(good.get("code"))
        if by_code.get(code):                  # повернення, завантажені з Checkbox (без номера рядка)
            take = min(by_code[code], qty - done)
            done += take
            by_code[code] -= take
        out.append({
            "line": i, "code": good.get("code"), "name": good.get("name"), "price": good.get("price") or 0,
            "barcode": good.get("barcode"), "qty": qty, "returned": done, "max": max(qty - done, 0),
            "total": eff, "tax_codes": _line_tax_codes(ln), "good_id": ln.get("good_id"),
            "excise_barcodes": list(good.get("excise_barcodes") or ln.get("excise_barcodes") or []),
            "discount_name": next((d.get("name") for d in ln.get("discounts") or [] if d.get("name")), None),
        })
    return out


def return_amount(line, qty):
    """Сума повернення qty (тисячні) з рядка: частка від уже повернутого до нового підсумку."""
    before = line["qty"] - line["max"]
    return share_of(line["total"], before + qty, line["qty"]) - share_of(line["total"], before, line["qty"])


def return_good_payload(line, qty, use_leftovers):
    """Товар для чека повернення: is_return, та сама ціна, різниця з сумою — знижкою чи надбавкою."""
    good = {"code": line["code"], "name": line["name"], "price": line["price"]}
    if line["tax_codes"]:
        good["tax"] = list(line["tax_codes"])
    if line.get("barcode"):
        good["barcode"] = line["barcode"]
    marks = line.get("excise_barcodes") or []
    if marks:
        before = (line["qty"] - line["max"]) // 1000
        part = marks[before:before + qty // 1000]
        if part:
            good["excise_barcodes"] = part
    item = {"good": good, "quantity": qty, "is_return": True}
    if use_leftovers and line.get("good_id"):
        item["good_id"] = line["good_id"]
    amount = return_amount(line, qty)
    diff = amount - line_sum(line["price"], qty)
    if diff:
        kind = "DISCOUNT" if diff < 0 else "EXTRA_CHARGE"
        item["discounts"] = [{"type": kind, "mode": "VALUE", "value": abs(diff),
                              "name": line.get("discount_name") or DISCOUNT_TYPES[kind],
                              **tax_fields(line["tax_codes"])}]
    return item, amount


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


def line_sum(price, qty):
    """Сума рядка: ціна (коп.) × кількість (тисячні), заокруглення 0.5 копійки — вгору."""
    return (price * qty + 500) // 1000


def clean_phone(value):
    """
    Телефон покупця для delivery.phone (копія чека в SMS/Viber): формат 380XXXXXXXXX.
    Приймає «+38 (067) 123-45-67», «0671234567», «671234567». Порожньо → None; невірно → ValueError.
    """
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return None
    if len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    elif len(digits) == 9:
        digits = "380" + digits
    if not re.fullmatch(r"380\d{9}", digits):
        raise ValueError("Невірний номер телефону — потрібен український номер, напр. 067 123 45 67")
    return digits


def mask_phone(phone):
    return phone[:5] + "*****" + phone[-2:] if phone else ""


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

def sync_groups(api, db, goods):
    """Групи товарів: довідник Checkbox, а для груп без назви — group_name товарів чи вивантаження."""
    groups = api.get_groups({good_group_id(g) for g in goods} - {None})
    if groups is not None:
        db.replace_groups(groups)
    names = {}
    for g in goods:
        grp = g.get("group") if isinstance(g.get("group"), dict) else {}
        gid, name = good_group_id(g), g.get("group_name") or grp.get("name")
        if gid and name:
            names[gid] = name
    if names:
        db.set_group_names(names)
    missing = db.groups_without_names()
    last = db.meta("groups_export_at")
    recent = last and (datetime.now() - datetime.fromisoformat(json.loads(last))).total_seconds() < 6 * 3600
    if missing and not recent:   # останній варіант — колонка «група товарів» у вивантаженні (не частіше раз на 6 год)
        with db.con:
            db.con.execute("INSERT OR REPLACE INTO meta VALUES ('groups_export_at', ?)",
                           (json.dumps(datetime.now().isoformat(timespec="seconds")),))
        try:
            by_code = api.group_names_from_export()
            code_group = {str(g["code"]).strip(): good_group_id(g) for g in goods}
            db.set_group_names({code_group[c]: n for c, n in by_code.items() if code_group.get(c) in missing})
        except (CheckboxError, net.RequestException) as e:
            log.info("Назви груп з вивантаження недоступні: %s", e)


def sync_all(api, db):
    """Синхронізує товари, групи і податкові ставки. Повертає (goods, use_leftovers, taxes, source)."""
    goods, use_leftovers = api.get_all_goods()
    db.replace_goods(goods, use_leftovers)
    sync_groups(api, db, goods)
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
