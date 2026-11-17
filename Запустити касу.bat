@echo off
chcp 65001 >nul
title Checkbox каса
cd /d "%~dp0"
set PYTHONUTF8=1

rem --- шукаємо Python ---
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo Python не знайдено. Встановіть його з https://www.python.org/downloads/
    echo під час встановлення поставте галочку "Add python.exe to PATH".
    pause
    exit /b 1
)

rem --- перевіряємо файли ---
if not exist "checkbox_web.py" (
    echo Не знайдено checkbox_web.py поруч із цим файлом.
    pause
    exit /b 1
)
if not exist "config.json" (
    echo config.json ще немає - налаштування можна заповнити на сторінці каси.
)

rem --- значок у треї: потрібні pystray і Pillow (без них каса працює, але без значка) ---
%PY% -c "import pystray, PIL" >nul 2>nul || (
    echo Встановлюю бібліотеку для значка в треї...
    %PY% -m pip install --quiet pystray pillow >nul 2>nul
)

echo Запускаю касу... Браузер відкриється автоматично.
echo Щоб зупинити касу — закрийте це вікно.
echo.
%PY% checkbox_web.py
echo.
echo Касу зупинено.
pause
