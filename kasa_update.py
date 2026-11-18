"""
Оновлення каси з випусків GitHub.

Випуск (GitHub Release з тегом vX.Y.Z) збирає GitHub Actions (.github/workflows/release.yml):
  • Kasa.exe        — для зібраної версії Windows;
  • kasa-py.zip     — .py-файли: для запуску через Python і для Каса.app на Mac;
  • Kasa-mac.zip    — повна збірка для Mac (для нових встановлень);
  • SHA256SUMS.txt  — контрольні суми; файл без правильної суми не встановлюється.

Каса лише перевіряє останній випуск і показує банер; встановлення — після натискання «Оновити».
Перед заміною .py-файлів робиться резервна копія в DATA_DIR/backup/.
"""

import hashlib
import io
import os
import re
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

from checkbox_app import APP_DIR, APP_VERSION, DATA_DIR, GITHUB_REPO, log, net

API = os.environ.get("CHECKBOX_UPDATE_API", "https://api.github.com").rstrip("/")
PY_ASSET, EXE_ASSET, SUMS_ASSET = "kasa-py.zip", "Kasa.exe", "SHA256SUMS.txt"
PY_FILES = ("checkbox_app.py", "checkbox_web.py", "goods_csv.py", "kasa_update.py")
EXTRA_FILES = ("kasa.ico", "config.example.json", "Запустити касу.bat", "Запустити касу.command")
CHECK_TTL = 6 * 3600          # як часто звертатися до GitHub (обмеження — 60 запитів на годину з однієї адреси)


class UpdateError(Exception):
    pass


def repo():
    return (os.environ.get("CHECKBOX_UPDATE_REPO") or GITHUB_REPO).strip().strip("/")


def parse_version(v):
    """«1.4.6» → (1, 4, 6, 0); «1.4.6.1» → (1, 4, 6, 1). Каси до 1.4.6 бачать лише перші три числа."""
    nums = [int(x) for x in re.findall(r"\d+", str(v))[:4]]
    return tuple(nums + [0] * (4 - len(nums)))


def install_kind():
    """exe — зібраний Kasa.exe; py — запуск .py (зокрема всередині Каса.app); None — оновлення недоступне."""
    if getattr(sys, "frozen", False):
        return "exe" if os.name == "nt" else None
    return "py"


def _session():
    s = net.Session()
    s.headers["User-Agent"] = f"checkbox-kasa/{APP_VERSION}"     # GitHub вимагає User-Agent
    return s


def _get(url, timeout=20, raw=False):
    try:
        r = _session().request("GET", url, timeout=timeout)
    except net.RequestException as e:
        raise UpdateError(f"Немає зв'язку з GitHub: {e}")
    if r.status_code == 404:
        raise UpdateError("Випусків ще немає або репозиторій не знайдено")
    if r.status_code == 403:
        raise UpdateError("GitHub тимчасово обмежив кількість запитів — спробуйте пізніше")
    if r.status_code >= 400:
        raise UpdateError(f"GitHub: HTTP {r.status_code}")
    return r.content if raw else r.json()


_cache = {"time": 0, "data": None}


def check(force=False):
    """Інформація про останній випуск. Результат кешується на CHECK_TTL секунд."""
    if not repo():
        return {"enabled": False, "current": APP_VERSION}
    if not force and _cache["data"] and time.time() - _cache["time"] < CHECK_TTL:
        return _cache["data"]
    rel = _get(f"{API}/repos/{repo()}/releases/latest")
    latest = str(rel.get("tag_name") or "").lstrip("vV")
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets") or []}
    kind = install_kind()
    need = EXE_ASSET if kind == "exe" else PY_ASSET
    newer = parse_version(latest) > parse_version(APP_VERSION)
    data = {
        "enabled": True, "current": APP_VERSION, "latest": latest, "kind": kind,
        "newer": newer, "url": rel.get("html_url"), "notes": (rel.get("body") or "")[:3000],
        "available": bool(newer and kind and need in assets and SUMS_ASSET in assets),
        "_assets": assets, "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    if newer and not kind:
        data["problem"] = "Цю збірку оновлюють вручну — завантажте нову версію зі сторінки випуску"
    elif newer and (need not in assets or SUMS_ASSET not in assets):
        data["problem"] = f"У випуску {latest} ще немає файлу {need} — можливо, збирання ще триває"
    _cache.update(time=time.time(), data=data)
    log.info("Перевірка оновлень: поточна %s, остання %s%s", APP_VERSION, latest or "—",
             " — доступне оновлення" if data["available"] else "")
    return data


def public(info):
    return {k: v for k, v in info.items() if not k.startswith("_")}


def _download_verified(assets, name):
    sums = {}
    for line in _get(assets[SUMS_ASSET], raw=True).decode("utf-8", "replace").splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            sums[parts[1].lstrip("*")] = parts[0].lower()
    if name not in sums:
        raise UpdateError(f"У {SUMS_ASSET} немає суми для {name}")
    data = _get(assets[name], timeout=300, raw=True)
    if hashlib.sha256(data).hexdigest() != sums[name]:
        raise UpdateError(f"Контрольна сума {name} не збігається — файл пошкоджено, оновлення скасовано")
    return data


def _install_py(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UpdateError(f"{PY_ASSET} пошкоджено")
    names = {Path(n).name: n for n in z.namelist() if not n.endswith("/")}
    missing = [f for f in PY_FILES if f not in names]
    if missing:
        raise UpdateError(f"У {PY_ASSET} немає файлів: {', '.join(missing)}")
    files = {}
    for f in PY_FILES + EXTRA_FILES:
        if f in names:
            files[f] = z.read(names[f])
    for f in PY_FILES:                      # нова версія має хоча б компілюватися
        try:
            compile(files[f].decode("utf-8"), f, "exec")
        except (SyntaxError, UnicodeDecodeError) as e:
            raise UpdateError(f"{f} у новій версії пошкоджений: {e}")

    backup = DATA_DIR / "backup" / f"{APP_VERSION}-{datetime.now():%Y%m%d-%H%M%S}"
    written = []
    try:
        backup.mkdir(parents=True, exist_ok=True)
        for f in files:
            if (APP_DIR / f).exists():
                (backup / f).write_bytes((APP_DIR / f).read_bytes())
        for f, content in files.items():
            tmp = APP_DIR / (f + ".new")
            tmp.write_bytes(content)
            os.replace(tmp, APP_DIR / f)
            written.append(f)
    except OSError as e:
        # повертаємо вже замінені файли з резервної копії
        for f in written:
            try:
                if (backup / f).exists():
                    os.replace(backup / f, APP_DIR / f)
            except OSError:
                pass
        raise UpdateError(f"Немає прав на запис у папку каси ({APP_DIR}): {e}")
    log.info("Оновлено файли %s; резервна копія — %s", ", ".join(files), backup)
    return backup


def _install_exe(data):
    if data[:2] != b"MZ":
        raise UpdateError(f"{EXE_ASSET} — не програма Windows")
    exe = Path(sys.executable)
    new, old = exe.with_name(exe.stem + ".new.exe"), exe.with_name(exe.stem + ".old.exe")
    try:
        new.write_bytes(data)
        if old.exists():
            try:
                old.unlink()
            except OSError:
                # попередня версія ще зайнята (не встигла закритися) — беремо інше ім'я, приберемо пізніше
                old = exe.with_name(f"{exe.stem}.old-{int(time.time())}.exe")
        os.replace(exe, old)        # запущений exe у Windows не можна перезаписати, але можна перейменувати
        os.replace(new, exe)
    except OSError as e:
        raise UpdateError(f"Немає прав на заміну {exe.name} ({exe.parent}): {e}")
    log.info("Kasa.exe оновлено; попередня версія — %s", old.name)
    return old


def install():
    """Завантажити й встановити останній випуск. Після цього треба перезапустити касу."""
    info = check(force=True)
    if not info.get("available"):
        raise UpdateError(info.get("problem") or "Оновлень немає")
    kind = info["kind"]
    name = EXE_ASSET if kind == "exe" else PY_ASSET
    log.info("Оновлення до %s (%s)…", info["latest"], name)
    data = _download_verified(info["_assets"], name)
    (_install_exe if kind == "exe" else _install_py)(data)
    return info["latest"]


def restart_command():
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(APP_DIR / "checkbox_web.py")]


def _clean_env():
    env = dict(os.environ, CHECKBOX_NO_BROWSER="1", CHECKBOX_RESTARTED="1")
    env.pop("CHECKBOX_RELAUNCHED", None)
    if getattr(sys, "frozen", False):
        # Без цього нова копія Kasa.exe (onefile) підхоплює тимчасову папку _MEI… старої,
        # і стара при виході не може її видалити: «Failed to remove temporary directory».
        # PyInstaller ≥ 6.9: нова копія розпакується у власну папку, як при звичайному запуску.
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        for key in list(env):
            if key.upper().startswith(("_PYI_", "_MEIPASS", "PYI_")) and key.upper() != "PYINSTALLER_RESET_ENVIRONMENT":
                env.pop(key)
        mei = getattr(sys, "_MEIPASS", None)
        if mei and env.get("PATH"):     # шляхи в стару тимчасову папку нова копія не має бачити
            env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep)
                                          if p and not os.path.normcase(p).startswith(os.path.normcase(mei)))
    return env


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def spawn_restarted(relaunch=False):
    """
    Запустити нову копію каси (після того, як стара звільнила порт).

    Kasa.exe на Windows: нову копію запускає не сама каса, а окремий прихований PowerShell, який
    спершу чекає, поки стара копія (і її завантажувач) повністю завершиться й прибере свою
    тимчасову папку _MEI…. Тоді нова не може ні зачепити, ні заблокувати старі файли — і вікна
    «Failed to remove temporary directory» немає. Немає PowerShell — запускаємо напряму, як раніше.
    """
    env = _clean_env()
    if relaunch:
        env["CHECKBOX_RELAUNCHED"] = "1"
    frozen = getattr(sys, "frozen", False)
    if os.name == "nt" and frozen:
        try:
            import ctypes
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        except Exception:  # noqa
            pass
        pids = [os.getpid()]
        try:
            ppid = os.getppid()          # завантажувач onefile — саме він видаляє _MEI…
            if ppid and ppid != os.getpid():
                pids.append(ppid)
        except (AttributeError, OSError):
            pass
        exe = sys.executable
        script = (f"Wait-Process -Id {','.join(map(str, pids))} -Timeout 40 -ErrorAction SilentlyContinue; "
                  "Start-Sleep -Milliseconds 300; "
                  f"Start-Process -FilePath {_ps_quote(exe)} -WorkingDirectory {_ps_quote(APP_DIR)}")
        root = os.environ.get("SystemRoot", r"C:\Windows")
        ps = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        try:
            subprocess.Popen([ps if os.path.isfile(ps) else "powershell.exe", "-NoProfile", "-NonInteractive",
                              "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", script],
                             env=env, cwd=str(APP_DIR), close_fds=True,
                             creationflags=0x08000000 | 0x00000200)     # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
            log.info("Каса перезапускається після оновлення (нова копія стартує після виходу цієї; чекаю PID %s)",
                     ", ".join(map(str, pids)))
            return
        except OSError as e:
            log.warning("PowerShell недоступний (%s) — запускаю нову копію напряму", e)
    kw = {"env": env, "cwd": str(APP_DIR), "close_fds": True}
    if os.name == "nt":
        flags = 0x00000200                                  # CREATE_NEW_PROCESS_GROUP
        flags |= 0x00000008 if frozen else 0x00000010       # DETACHED / NEW_CONSOLE
        kw["creationflags"] = flags
    else:
        kw["start_new_session"] = True
    subprocess.Popen(restart_command(), **kw)
    log.info("Каса перезапускається після оновлення")


def _mei_age():
    mei = getattr(sys, "_MEIPASS", None)
    if not mei:
        return None, None
    try:
        st = os.stat(mei)
        return mei, time.time() - getattr(st, "st_birthtime", st.st_ctime)
    except OSError:
        return mei, None


def relaunch_if_inherited():
    """
    Каси до 1.2.5 після оновлення запускали нову Kasa.exe зі своїми змінними PyInstaller, і вона
    працювала з тимчасової папки _MEI… старої копії. Стара закривалась, папка зникала — і разом
    з нею сертифікати: «Немає зв'язку … [Errno 2] No such file or directory» на кожному запиті.
    Таку копію (папка створена давно, а не щойно нами) перезапускаємо «начисто» — один раз.
    """
    os.environ.pop("CHECKBOX_CLEAN_SPAWN", None)
    if not getattr(sys, "frozen", False):
        return
    mei, age = _mei_age()
    pyi = sorted(k for k in os.environ if k.upper().startswith(("_PYI_", "_MEIPASS", "PYINSTALLER")))
    log.info("Тимчасова папка: %s (створена %s тому); змінні PyInstaller: %s", mei,
             f"{age:.0f} с" if age is not None else "?", ", ".join(pyi) or "—")
    if os.environ.get("CHECKBOX_RESTARTED") != "1" or os.environ.get("CHECKBOX_RELAUNCHED") == "1":
        return
    if age is None or age > 90:
        log.warning("Каса працює зі спільною тимчасовою папкою старої копії — перезапускаю начисто")
        spawn_restarted(relaunch=True)
        sys.exit(0)


def cleanup_after_update():
    """Прибрати Kasa.old.exe, що лишився після попереднього оновлення."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        for old in exe.parent.glob(exe.stem + ".old*.exe"):
            try:
                old.unlink()
            except OSError:
                pass                     # ще зайнятий — спробуємо при наступному запуску
