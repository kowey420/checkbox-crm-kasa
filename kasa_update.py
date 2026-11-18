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

import base64
import hashlib
import http.client
import io
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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
    s = str(v).strip().lstrip("vV")
    main, _, suffix = s.partition("-")          # «1.7.1.0-alpha1»: alpha молодша за «1.7.1.0»
    nums = [int(x) for x in re.findall(r"\d+", main)[:4]]
    pre = (0, int((re.findall(r"\d+", suffix) or ["0"])[0])) if suffix else (1, 0)
    return tuple(nums + [0] * (4 - len(nums))) + pre


def install_kind():
    """exe — зібраний Kasa.exe; py — запуск .py (зокрема всередині Каса.app); None — оновлення недоступне."""
    if getattr(sys, "frozen", False):
        return "exe" if os.name == "nt" else None
    return "py"


_ORIGINAL = {}


def original_app_dir():
    """
    Справжня папка каси. macOS запускає завантажену програму з позначкою «з інтернету» з тимчасової
    копії /AppTranslocation/… — це дзеркало (nullfs) справжньої папки лише для читання. Звідки воно
    змонтоване, видно в `mount`: «<справжня папка> on <дзеркало> (nullfs, …)». Файли, записані у
    справжню папку, одразу видно й у дзеркалі.
    """
    d = str(APP_DIR)
    if "/AppTranslocation/" not in d:
        return APP_DIR
    if "dir" in _ORIGINAL:
        return _ORIGINAL["dir"]
    found = None
    try:
        out = subprocess.run(["/sbin/mount"], capture_output=True, timeout=10).stdout.decode("utf-8", "replace")
        best = ""
        for line in out.splitlines():
            m = re.match(r"^(.+?) on (.+?) \((.*)\)$", line)
            if not m or "nullfs" not in m.group(3):
                continue
            src, dst = m.group(1), m.group(2).rstrip("/")
            if (d == dst or d.startswith(dst + "/")) and len(dst) > len(best):
                best, found = dst, Path(src + d[len(dst):])
        if found is not None and not found.is_dir():
            found = None
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("Не вдалося визначити справжню папку каси (mount): %s", e)
    _ORIGINAL["dir"] = found
    if found:
        log.info("Каса запущена з тимчасової копії macOS; справжня папка — %s", found)
    return found


def _bundle_of(p):
    """…/Каса.app/Contents/Resources/app → …/Каса.app"""
    for parent in [Path(p)] + list(Path(p).parents):
        if parent.suffix == ".app":
            return parent
    return None


def unquarantine():
    """
    Зняти з «Каса.app» позначку «завантажено з інтернету» (com.apple.quarantine): через неї macOS
    щоразу запускає касу з тимчасової копії лише для читання (App Translocation), навіть із «Програм».
    Наступний запуск піде вже зі справжньої папки.
    """
    if sys.platform != "darwin" or install_kind() != "py":
        return
    orig = original_app_dir()
    bundle = _bundle_of(orig) if orig else None
    if not bundle or not os.access(bundle, os.W_OK):
        return
    try:
        r = subprocess.run(["/usr/bin/xattr", "-dr", "com.apple.quarantine", str(bundle)],
                           capture_output=True, timeout=30)
        if "/AppTranslocation/" in str(APP_DIR):
            log.info("Знято позначку «з інтернету» з %s (%s) — наступний запуск піде без тимчасової копії",
                     bundle, "успішно" if r.returncode == 0 else r.stderr.decode("utf-8", "replace").strip()[:200])
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("Не вдалося зняти позначку «з інтернету» з %s: %s", bundle, e)


def install_dir():
    """Куди записувати оновлення .py: справжня папка каси (а не тимчасова копія macOS)."""
    return original_app_dir() or APP_DIR


def install_problem():
    """Чому оновлення файлів .py неможливе на цьому місці (None — можна)."""
    if install_kind() != "py":
        return None
    target = original_app_dir()
    if target is None:
        # тимчасова копія, а справжню папку визначити не вдалося (напр. образ диска)
        return ("macOS запустила касу з тимчасової копії, у яку не можна записати оновлення. "
                "Закрийте касу, у Finder перетягніть «Каса» в папку «Програми» (Applications) "
                "і запускайте звідти — тоді оновлення встановиться")
    if not os.access(target, os.W_OK):
        return f"Немає прав на запис у папку каси ({target}) — перемістіть касу в папку, де у вас є права, напр. «Програми»"
    return None


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
    if r.status_code in (403, 429):
        h = r.headers
        remaining, reset = h.get("X-RateLimit-Remaining"), h.get("X-RateLimit-Reset")
        try:
            msg = (r.json() or {}).get("message")
        except ValueError:
            msg = r.text[:200]
        until = ""
        if reset and str(reset).isdigit():
            until = datetime.fromtimestamp(int(reset)).strftime("%H:%M")
        proxies = {k: v for k, v in urllib.request.getproxies().items() if k in ("http", "https")}
        log.warning("GitHub %s %s: %s; ліміт: лишилось %s, з %s до %s; проксі системи: %s", r.status_code,
                    url.split("?")[0], msg, remaining or "?", h.get("X-RateLimit-Limit") or "?", until or "?",
                    proxies or "немає")
        raise RateLimited("GitHub тимчасово обмежив кількість запитів" + (f" (до {until})" if until else "")
                          + " — спробуйте пізніше")
    if r.status_code >= 400:
        raise UpdateError(f"GitHub: HTTP {r.status_code}")
    return r.content if raw else r.json()


class RateLimited(UpdateError):
    """API GitHub: вичерпано 60 запитів на годину з цієї IP-адреси (часто — спільна адреса офісу)."""


WEB = os.environ.get("CHECKBOX_UPDATE_WEB", "https://github.com").rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _latest_via_web():
    """
    Остання версія без API: github.com/<repo>/releases/latest перенаправляє на …/releases/tag/vX.
    Сторінки й файли випусків (releases/download/…) не підпадають під ліміт API — працює й тоді,
    коли з офісної адреси вже вичерпано 60 запитів на годину.
    """
    url = f"{WEB}/{repo()}/releases/latest"
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=net.ssl_context()))
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": f"checkbox-kasa/{APP_VERSION}"})
    try:
        opener.open(req, timeout=20)
        raise UpdateError("GitHub не повідомив останню версію")
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location") or ""
        if e.code not in (301, 302, 303, 307, 308) or "/releases/tag/" not in location:
            raise UpdateError(f"GitHub: HTTP {e.code}")
    except (OSError, http.client.HTTPException) as e:
        raise UpdateError(f"Немає зв'язку з GitHub: {getattr(e, 'reason', e)}")
    return _release_from_tag(urllib.parse.unquote(location.rsplit("/releases/tag/", 1)[1].split("?")[0].strip("/")))


_cache = {"time": 0, "data": None}


def channel():
    """Прихований канал оновлень: «"update_channel": "alpha"» у config.json (у налаштуваннях його немає)."""
    try:
        from checkbox_app import load_config
        return "alpha" if str(load_config().get("update_channel") or "").strip().lower() == "alpha" else "stable"
    except Exception:
        return "stable"


def _release_from_tag(tag):
    tag = str(tag)
    base = f"{WEB}/{repo()}/releases/download/{urllib.parse.quote(tag)}"
    return {"tag_name": tag, "html_url": f"{WEB}/{repo()}/releases/tag/{urllib.parse.quote(tag)}", "body": "",
            "assets": [{"name": n, "browser_download_url": f"{base}/{n}"} for n in (EXE_ASSET, PY_ASSET, SUMS_ASSET)],
            "_via": "web"}


def _newest_any():
    """Найновіший випуск разом із тестовими (prerelease). Без ліміту API — через стрічку releases.atom."""
    try:
        rels = _get(f"{API}/repos/{repo()}/releases?per_page=20")
        rels = [r for r in rels if not r.get("draft") and r.get("tag_name")]
        return max(rels, key=lambda r: parse_version(r["tag_name"])) if rels else None
    except RateLimited:
        log.info("API GitHub обмежив запити — тестові випуски шукаю через releases.atom")
    try:
        req = urllib.request.Request(f"{WEB}/{repo()}/releases.atom", headers={"User-Agent": f"checkbox-kasa/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=20, context=net.ssl_context()) as r:
            feed = r.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as e:
        raise UpdateError(f"Немає зв'язку з GitHub: {getattr(e, 'reason', e)}")
    tags = {urllib.parse.unquote(t) for t in re.findall(r"/releases/tag/([^\"<>?#]+)", feed)}
    return _release_from_tag(max(tags, key=parse_version)) if tags else None


def _info(rel, kind):
    latest = str(rel.get("tag_name") or "").lstrip("vV")
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets") or []}
    need = EXE_ASSET if kind == "exe" else PY_ASSET
    return latest, assets, bool(kind and need in assets and SUMS_ASSET in assets and not install_problem())


def check(force=False):
    """Інформація про останній випуск. Результат кешується на CHECK_TTL секунд."""
    if not repo():
        return {"enabled": False, "current": APP_VERSION}
    if not force and _cache["data"] and time.time() - _cache["time"] < CHECK_TTL:
        return _cache["data"]
    try:
        rel = _get(f"{API}/repos/{repo()}/releases/latest")
    except RateLimited:
        log.info("API GitHub обмежив запити з цієї адреси — дізнаюся версію через сторінку випусків")
        rel = _latest_via_web()
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
    blocked = install_problem() if newer else None
    if blocked:
        data["available"] = False
        data["problem"] = blocked
    elif newer and not kind:
        data["problem"] = "Цю збірку оновлюють вручну — завантажте нову версію зі сторінки випуску"
    elif newer and (need not in assets or SUMS_ASSET not in assets):
        data["problem"] = f"У випуску {latest} ще немає файлу {need} — можливо, збирання ще триває"
    if parse_version(APP_VERSION)[4] == 0 and latest and not newer and parse_version(latest) != parse_version(APP_VERSION):
        # стоїть тестова (alpha), а остання стабільна — старша за номером: можна повернутися на прод
        _, _, ok = _info(rel, kind)
        data["stable_back"] = {"latest": latest, "available": ok, "url": rel.get("html_url"), "_assets": assets}
    if channel() == "alpha":
        data["channel"] = "alpha"
        try:
            pre = _newest_any()
        except UpdateError as e:
            data["alpha"] = {"error": str(e)}
            pre = None
        if pre:
            a_latest, a_assets, ok = _info(pre, kind)
            a_newer = parse_version(a_latest) > parse_version(APP_VERSION)
            data["alpha"] = {"latest": a_latest, "newer": a_newer, "available": bool(a_newer and ok),
                             "url": pre.get("html_url"), "_assets": a_assets}
    _cache.update(time=time.time(), data=data)
    log.info("Перевірка оновлень: поточна %s, остання %s%s%s", APP_VERSION, latest or "—",
             " — доступне оновлення" if data["available"] else "",
             f"; канал alpha: {data['alpha'].get('latest') or data['alpha'].get('error')}" if data.get("alpha") else "")
    return data


def public(info):
    return {k: (public(v) if isinstance(v, dict) else v) for k, v in info.items() if not k.startswith("_")}


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

    target = install_dir()
    backup = DATA_DIR / "backup" / f"{APP_VERSION}-{datetime.now():%Y%m%d-%H%M%S}"
    written = []
    try:
        backup.mkdir(parents=True, exist_ok=True)
        for f in files:
            if (target / f).exists():
                (backup / f).write_bytes((target / f).read_bytes())
        for f, content in files.items():
            tmp = target / (f + ".new")
            tmp.write_bytes(content)
            os.replace(tmp, target / f)
            written.append(f)
    except OSError as e:
        # повертаємо вже замінені файли з резервної копії
        for f in written:
            try:
                if (backup / f).exists():
                    os.replace(backup / f, target / f)
            except OSError:
                pass
        raise UpdateError(install_problem() or f"Немає прав на запис у папку каси ({target}): {e}")
    log.info("Оновлено файли %s; резервна копія — %s", ", ".join(files), backup)
    return backup


_pending_swap = None        # (Kasa.new.exe, Kasa.exe): замінити після виходу каси


def _exe_paths():
    exe = Path(sys.executable)
    return exe, exe.with_name(exe.stem + ".new.exe"), exe.with_name(exe.stem + ".old.exe")


def _install_exe(data):
    """
    Нову версію кладемо поруч як Kasa.new.exe, а запущений Kasa.exe не чіпаємо: заміну робить помічник
    уже після виходу каси. Перейменування запущеного Kasa.exe (як було до 1.4.6.5) заважало
    завантажувачу onefile прибрати свою папку _MEI… — «Failed to remove temporary directory».
    """
    global _pending_swap
    if data[:2] != b"MZ":
        raise UpdateError(f"{EXE_ASSET} — не програма Windows")
    exe, new, _ = _exe_paths()
    try:
        tmp = new.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, new)
    except OSError as e:
        raise UpdateError(f"Немає прав на запис у папку каси ({exe.parent}): {e}")
    _pending_swap = (new, exe)
    log.info("Нову версію збережено як %s; Kasa.exe буде замінено після виходу каси", new.name)
    return new


def _swap_now():
    """Замінити Kasa.exe одразу (запасний шлях, коли помічника запустити не вдалося): як до 1.4.6.5."""
    global _pending_swap
    if not _pending_swap:
        return
    new, exe = _pending_swap
    old = exe.with_name(exe.stem + ".old.exe")
    if old.exists():
        try:
            old.unlink()
        except OSError:
            old = exe.with_name(f"{exe.stem}.old-{int(time.time())}.exe")
    os.replace(exe, old)        # запущений exe не можна перезаписати, але можна перейменувати
    os.replace(new, exe)
    _pending_swap = None
    log.info("Kasa.exe оновлено; попередня версія — %s", old.name)


def install(target="stable"):
    """
    Завантажити й встановити випуск. target: «stable» — остання стабільна; «alpha» — найновіша тестова
    (лише з «update_channel»: «alpha» у config.json); «back» — повернутися з тестової на стабільну.
    Після цього треба перезапустити касу.
    """
    full = check(force=True)
    kind = full["kind"]
    info = full if target == "stable" else (full.get("alpha") if target == "alpha" else full.get("stable_back")) or {}
    if not info.get("available"):
        raise UpdateError(info.get("problem") or info.get("error") or "Оновлень немає")
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


def _ps_encoded(script):
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _powershell():
    root = os.environ.get("SystemRoot", r"C:\Windows")
    ps = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return ps if os.path.isfile(ps) else "powershell.exe"


def _spawn_via_wmi(pids, relaunch):
    """
    Запустити помічника, який НЕ є дочірнім процесом каси: його створює служба WMI Windows.
    Будь-який живий дочірній процес у момент виходу (нова Kasa.exe, PowerShell…) заважав
    завантажувачу onefile видалити папку _MEI… — «Failed to remove temporary directory».
    Короткий PowerShell лише передає завдання WMI й завершується ще до виходу каси.
    Помічник чекає, поки стара копія і її завантажувач завершаться, і запускає нову Kasa.exe
    з чистим середовищем (без жодних змінних PyInstaller).
    """
    envs = {"CHECKBOX_NO_BROWSER": "1", "CHECKBOX_RESTARTED": "1", "CHECKBOX_CLEAN_SPAWN": "1"}
    if relaunch:
        envs["CHECKBOX_RELAUNCHED"] = "1"
    exe, new, old = _exe_paths()
    swap = bool(_pending_swap)
    hlog = DATA_DIR / "logs" / "update-helper.log"
    inner = r"""
$log = @LOG@
function L($m) { try { Add-Content -LiteralPath $log -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') + '  ' + $m) -Encoding UTF8 } catch {} }
L ('---- помічник оновлення: чекаю завершення PID @PIDS@')
Wait-Process -Id @PIDS@ -Timeout 120 -ErrorAction SilentlyContinue
L 'стара копія завершилась'
Start-Sleep -Milliseconds 500
$exe = @EXE@; $new = @NEW@; $old = @OLD@
if (@SWAP@ -and (Test-Path -LiteralPath $new)) {
  $ok = $false
  for ($i = 1; $i -le 40 -and -not $ok; $i++) {
    try {
      $o = $old
      if (Test-Path -LiteralPath $o) {
        try { Remove-Item -LiteralPath $o -Force -ErrorAction Stop }
        catch { $o = $old.Substring(0, $old.Length - 4) + '-' + [DateTime]::Now.Ticks + '.exe' }
      }
      Move-Item -LiteralPath $exe -Destination $o -Force -ErrorAction Stop
      try { Move-Item -LiteralPath $new -Destination $exe -Force -ErrorAction Stop; $ok = $true; L ('Kasa.exe замінено новою версією; попередня — ' + (Split-Path $o -Leaf)) }
      catch { Move-Item -LiteralPath $o -Destination $exe -Force -ErrorAction SilentlyContinue; throw }
    } catch { L ('спроба ' + $i + ' замінити Kasa.exe: ' + $_.Exception.Message); Start-Sleep -Milliseconds 500 }
  }
  if (-not $ok) { L 'не вдалося замінити Kasa.exe — запускаю попередню версію' }
}
""" + "".join(f"$env:{k}={_ps_quote(v)}\n" for k, v in envs.items()) + r"""
try { Start-Process -FilePath $exe -WorkingDirectory @DIR@ -ErrorAction Stop; L 'нову копію каси запущено' }
catch { L ('не вдалося запустити касу: ' + $_.Exception.Message) }
"""
    for k, v in (("@LOG@", _ps_quote(hlog)), ("@PIDS@", ",".join(map(str, pids))), ("@EXE@", _ps_quote(exe)),
                 ("@NEW@", _ps_quote(new)), ("@OLD@", _ps_quote(old)), ("@SWAP@", "$true" if swap else "$false"),
                 ("@DIR@", _ps_quote(APP_DIR))):
        inner = inner.replace(k, v)
    cmd = f'"{_powershell()}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand {_ps_encoded(inner)}'
    outer = ("$si = ([wmiclass]'root\\cimv2:Win32_ProcessStartup').CreateInstance(); $si.ShowWindow = 0; "
             f"$r = ([wmiclass]'root\\cimv2:Win32_Process').Create({_ps_quote(cmd)}, {_ps_quote(APP_DIR)}, $si); "
             "Write-Output ('wmi:' + $r.ReturnValue + ':' + $r.ProcessId)")
    r = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-EncodedCommand", _ps_encoded(outer)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=30, creationflags=0x08000000)          # CREATE_NO_WINDOW; чекаємо завершення
    out = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
    m = re.search(r"wmi:(\d+):(\d*)", out)
    if not m or m.group(1) != "0":
        raise OSError(f"WMI не запустив помічника: {out.strip()[:300] or r.returncode}")
    return m.group(2)


def spawn_restarted(relaunch=False):
    """
    Запустити нову копію каси (після того, як стара звільнила порт).
    Kasa.exe на Windows — через помічника від WMI (див. _spawn_via_wmi); не вдалося — напряму, як раніше.
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
        try:
            hp = _spawn_via_wmi(pids, relaunch)
            log.info("Каса перезапускається після оновлення: помічник PID %s (від WMI) запустить нову копію "
                     "після виходу PID %s", hp, ", ".join(map(str, pids)))
            return
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("Перезапуск через WMI не вдався (%s) — запускаю нову копію напряму", e)
    try:
        _swap_now()
    except OSError as e:
        log.error("Не вдалося замінити Kasa.exe новою версією: %s", e)
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
    if os.environ.get("CHECKBOX_RESTARTED") != "1" or os.environ.get("CHECKBOX_RELAUNCHED") == "1":
        return
    if age is None or age > 90:
        log.warning("Каса працює зі спільною тимчасовою папкою старої копії — перезапускаю начисто")
        spawn_restarted(relaunch=True)
        sys.exit(0)


_PYI_ENV_AT_START = sorted(k for k in os.environ if k.upper().startswith(("_PYI_", "_MEIPASS", "PYINSTALLER")))


def log_temp_state():
    """У журнал: власна тимчасова папка Kasa.exe, її вік і змінні PyInstaller на старті."""
    if not getattr(sys, "frozen", False):
        return
    mei, age = _mei_age()
    log.info("Тимчасова папка: %s (створена %s тому); змінні PyInstaller: %s", mei,
             f"{age:.0f} с" if age is not None else "?", ", ".join(_PYI_ENV_AT_START) or "—")


def _is_kasa_mei(folder):
    """Папка _MEI… саме Kasa.exe (а не іншої програми на PyInstaller): є kasa.ico і certifi."""
    return (folder / "kasa.ico").is_file() or ((folder / "certifi").is_dir() and (folder / "pystray").is_dir())


def cleanup_old_temp():
    """
    Прибрати папки _MEI… попередніх копій каси, які їхній завантажувач не зміг видалити
    («Failed to remove temporary directory»), і записати в журнал, що саме там лишилось —
    за цим видно, хто тримав файли. Власну папку й папки інших програм не чіпаємо.
    """
    if not getattr(sys, "frozen", False):
        return
    import shutil
    import tempfile
    own = os.path.normcase(os.path.abspath(getattr(sys, "_MEIPASS", "") or ""))
    try:
        candidates = [p for p in Path(tempfile.gettempdir()).glob("_MEI*") if p.is_dir()]
    except OSError:
        return
    for folder in candidates:
        try:
            if os.path.normcase(os.path.abspath(folder)) == own or not _is_kasa_mei(folder):
                continue
            left = [str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()]
            age = time.time() - folder.stat().st_mtime
        except OSError:
            continue
        if age < 60:                    # ще може належати копії, що саме завершується
            continue
        shutil.rmtree(folder, ignore_errors=True)
        still = [str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()] if folder.exists() else []
        log.info("Залишок тимчасової папки попередньої копії %s: %d файлів (%s)%s", folder.name, len(left),
                 ", ".join(sorted(left)[:15]) + ("…" if len(left) > 15 else ""),
                 f"; не вдалося видалити {len(still)}: {', '.join(sorted(still)[:15])}" if still else " — видалено")


def skip_bootloader_cleanup():
    """
    Лише при перезапуску після оновлення, останнім кроком перед виходом: закрити завантажувач onefile
    (батьківський процес Kasa.exe), поки він не почав прибирати папку _MEI…. Саме це прибирання після
    оновлення натрапляло на тимчасово зайняті файли й показувало «Failed to remove temporary directory»
    (при звичайному «Вийти» — ні). Папку за 20 с прибирає нова копія (cleanup_old_temp) — без помилок.
    Батьківський процес закриваємо, лише якщо це справді наш Kasa.exe.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False) or not getattr(sys, "_MEIPASS", None):
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD))
        k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        ppid = os.getppid()
        h = k32.OpenProcess(0x0001 | 0x1000, False, ppid)     # PROCESS_TERMINATE | QUERY_LIMITED_INFORMATION
        if not h:
            log.info("Завантажувач PID %s недоступний (%s) — пропускаю", ppid, ctypes.get_last_error())
            return
        try:
            buf, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return
            same = os.path.normcase(os.path.abspath(buf.value)) == os.path.normcase(os.path.abspath(sys.executable))
            if not same:
                log.info("Батьківський процес %s — не Kasa.exe (%s), не чіпаю", ppid, buf.value)
                return
            log.info("Закриваю завантажувач Kasa.exe (PID %s): тимчасову папку %s прибере нова копія",
                     ppid, os.path.basename(sys._MEIPASS))
            for h_ in log.handlers if hasattr(log, "handlers") else []:
                try:
                    h_.flush()
                except Exception:  # noqa
                    pass
            k32.TerminateProcess(h, 0)
        finally:
            k32.CloseHandle(h)
    except Exception as e:  # noqa
        log.warning("Не вдалося закрити завантажувач Kasa.exe: %s", e)


def cleanup_after_update():
    """Прибрати Kasa.old.exe, що лишився після попереднього оновлення."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        for old in exe.parent.glob(exe.stem + ".old*.exe"):
            try:
                old.unlink()
            except OSError:
                pass                     # ще зайнятий — спробуємо при наступному запуску
        stale = exe.with_name(exe.stem + ".new.exe")
        if stale.exists():               # помічник не зміг замінити — деталі в logs/update-helper.log
            log.warning("Знайдено незастосоване оновлення %s — видаляю (подробиці в update-helper.log)", stale.name)
            try:
                stale.unlink()
            except OSError:
                pass
