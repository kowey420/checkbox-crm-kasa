@echo off
chcp 65001 >nul
title Збирання Kasa.exe
cd /d "%~dp0"
rem у репозиторії цей файл лежить у папці win, а код — рівнем вище
if not exist "checkbox_web.py" if exist "..\checkbox_web.py" cd ..
set PYTHONUTF8=1

rem ── Збирає Kasa.exe з checkbox_web.py (разом з checkbox_app.py і goods_csv.py) ──
rem Потрібні: Python 3.9+ та інтернет (один раз — для встановлення PyInstaller).

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo Python не знайдено. Встановіть його з https://www.python.org/downloads/
    pause
    exit /b 1
)

for %%f in (checkbox_app.py checkbox_web.py goods_csv.py kasa_update.py kasa.ico) do (
    if not exist "%%f" (
        echo Не знайдено %%f — покладіть цей файл поруч із «Зібрати exe.bat».
        pause
        exit /b 1
    )
)

echo [1/3] Встановлюю PyInstaller і бібліотеку для значка в треї...
%PY% -m pip install --upgrade --quiet pyinstaller certifi pystray pillow
if errorlevel 1 (
    echo Не вдалося встановити PyInstaller. Перевірте інтернет і спробуйте ще раз.
    pause
    exit /b 1
)

echo [2/3] Збираю Kasa.exe (1-2 хвилини)...
%PY% -m PyInstaller --noconfirm --clean --log-level WARN ^
    --onefile --noconsole --name Kasa --icon kasa.ico ^
    --hidden-import goods_csv --hidden-import kasa_update --hidden-import pystray._win32 --collect-data certifi ^
    --add-data "kasa.ico;." ^
    checkbox_web.py
if errorlevel 1 (
    echo Збирання не вдалося — повідомлення про помилку вище.
    pause
    exit /b 1
)

echo [3/3] Прибираю тимчасові файли...
copy /y "dist\Kasa.exe" "Kasa.exe" >nul
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del /q Kasa.spec 2>nul

echo.
echo Готово: %CD%\Kasa.exe
echo Kasa.exe можна копіювати на інші комп'ютери — Python там не потрібен.
echo Налаштування й база зберігатимуться в %%LOCALAPPDATA%%\Каса Checkbox
echo (або поруч з Kasa.exe, якщо там лежить config.json).
pause
