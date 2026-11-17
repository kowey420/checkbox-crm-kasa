#!/usr/bin/env python3
"""
Збирає «Каса.app» для macOS і пакує в «Каса для Mac.zip».
Працює на будь-якій ОС (потрібні лише Python і Pillow для іконки).

Запуск:  python build_mac_app.py
Поруч мають лежати kasa-launcher.sh і «Прочитай мене.txt»,
а рівнем вище — checkbox_app.py і checkbox_web.py.
"""

import os
import plistlib
import shutil
import stat
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
OUT = HERE / "dist"
APP_NAME = "Каса"
VERSION = "1.0"
BUNDLE = OUT / f"{APP_NAME}.app"
ZIP_PATH = OUT / f"{APP_NAME} для Mac.zip"


def make_icon(path):
    from PIL import Image, ImageDraw

    S = 1024
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # фон: заокруглений квадрат із вертикальним градієнтом (сітка іконок macOS: 824 px з 1024)
    pad, r = 100, 185
    bg = Image.new("RGBA", (S, S))
    top, bottom = (59, 130, 246), (29, 78, 216)
    for y in range(S):
        t = y / (S - 1)
        bg.paste(tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,), (0, y, S, y + 1))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((pad, pad, S - pad, S - pad), r, fill=255)
    img.paste(bg, (0, 0), mask)

    d = ImageDraw.Draw(img)
    # чек із зубчастим нижнім краєм
    x0, x1, y0, y1 = 330, 694, 230, 760
    tooth = 26
    pts = [(x0, y0), (x1, y0), (x1, y1)]
    x, up = x1, True
    while x > x0:
        x = max(x0, x - tooth)
        pts.append((x, y1 - tooth if up else y1))
        up = not up
    pts.append((x0, y0))
    d.polygon(pts, fill=(255, 255, 255, 255))
    for i, w in enumerate((250, 200, 250, 160)):
        yy = 320 + i * 70
        d.rounded_rectangle((x0 + 50, yy, x0 + 50 + w, yy + 26), 13, fill=(191, 219, 254, 255))
    d.rounded_rectangle((x0 + 50, 610, x1 - 50, 640), 15, fill=(37, 99, 235, 255))
    # зелена позначка «чек пробито»
    cx, cy, cr = 694, 724, 120
    d.ellipse((cx - cr, cy - cr, cx + cr, cy + cr), fill=(22, 163, 74, 255), outline=(255, 255, 255, 255), width=18)
    d.line([(cx - 58, cy + 2), (cx - 14, cy + 46), (cx + 62, cy - 42)], fill=(255, 255, 255, 255), width=30, joint="curve")

    img.save(path, format="ICNS")
    img.resize((256, 256), Image.LANCZOS).save(HERE / "icon-preview.png")


def build():
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    contents = BUNDLE / "Contents"
    macos, res = contents / "MacOS", contents / "Resources"
    (res / "app").mkdir(parents=True)
    macos.mkdir(parents=True)

    with open(contents / "Info.plist", "wb") as f:
        plistlib.dump({
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleIdentifier": "local.kasa.checkbox-crm",
            "CFBundleExecutable": "kasa",
            "CFBundleIconFile": "AppIcon",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "CFBundleDevelopmentRegion": "uk",
            "LSMinimumSystemVersion": "10.15",
            "NSHighResolutionCapable": True,
        }, f)

    shutil.copy(HERE / "kasa-launcher.sh", macos / "kasa")
    for name in ("checkbox_app.py", "checkbox_web.py", "goods_csv.py", "kasa_update.py"):
        shutil.copy(SRC / name, res / "app" / name)

    # certifi: сертифікати для HTTPS (Python з python.org на Mac своїх не має)
    import certifi
    shutil.copytree(Path(certifi.__file__).parent, res / "vendor" / "certifi",
                    ignore=shutil.ignore_patterns("__pycache__", "tests"))

    make_icon(res / "AppIcon.icns")


def pack():
    """zip із правами Unix: інакше на Mac запускач втратить право на запуск."""
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as z:
        def add(path, arcname):
            info = zipfile.ZipInfo.from_file(path, arcname)
            info.create_system = 3                     # Unix — Archive Utility поважає права
            if path.is_dir():
                info.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
                z.writestr(info, b"")
            else:
                mode = 0o755 if path.parent.name == "MacOS" else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, path.read_bytes())

        add(BUNDLE, BUNDLE.name + "/")
        for p in sorted(BUNDLE.rglob("*")):
            rel = p.relative_to(OUT).as_posix()
            add(p, rel + ("/" if p.is_dir() else ""))
        readme = HERE / "Прочитай мене.txt"
        add(readme, readme.name)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    build()
    pack()
    print(f"Готово: {ZIP_PATH}")
    sys.exit(0)
