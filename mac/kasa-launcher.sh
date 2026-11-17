#!/bin/bash
# Запускач «Каса.app»: знаходить Python, запускає касу у фоні й відкриває браузер — без Терміналу.
# Повідомлення — звичайні вікна macOS.
# Журнали: ~/Library/Application Support/Каса Checkbox/logs/ (kasa.log — робота каси, launcher.log — запуск)

RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
DATA="$HOME/Library/Application Support/Каса Checkbox"
LOGS="$DATA/logs"
LOG="$LOGS/launcher.log"
mkdir -p "$LOGS"

alert() {  # $1 — текст
  /usr/bin/osascript - "$1" >/dev/null 2>&1 <<'AS'
on run argv
  display dialog (item 1 of argv) with title "Каса" buttons {"OK"} default button "OK" with icon caution
end run
AS
}

ask() {  # $1 — текст, $2 — кнопка «так»; успіх, якщо натиснуто її
  local answer
  answer=$(/usr/bin/osascript - "$1" "$2" 2>/dev/null <<'AS'
on run argv
  try
    set r to display dialog (item 1 of argv) with title "Каса" buttons {"Скасувати", item 2 of argv} default button 2 with icon note
    return button returned of r
  on error
    return ""
  end try
end run
AS
)
  [ "$answer" = "$2" ]
}

# Каса сама обирає вільний порт 8000–8019 і записує його у файл port
saved_port() { cat "$DATA/port" 2>/dev/null | tr -dc '0-9'; }
is_running() { /usr/bin/curl -s -m 2 "http://127.0.0.1:$1/api/ping" 2>/dev/null | grep -q '"checkbox-kasa"'; }
find_running() {           # друкує порт уже запущеної каси
  local p
  for p in $(saved_port) $(seq 8000 8019); do
    if is_running "$p"; then echo "$p"; return 0; fi
  done
  return 1
}
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; }

# 1. Каса вже працює — лише відкриваємо сторінку
if RUNNING=$(find_running); then
  /usr/bin/open "http://127.0.0.1:$RUNNING"
  exit 0
fi

# 2. Шукаємо Python 3.9+: python.org, Homebrew, а потім вбудований від Apple
PY=""
for c in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -x "$c" ] && py_ok "$c"; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
  if /usr/bin/xcode-select -p >/dev/null 2>&1; then
    if py_ok /usr/bin/python3; then
      PY=/usr/bin/python3
    else
      alert "Встановлена версія Python застаріла для Каси. Зараз відкриється сайт python.org — завантажте й встановіть Python, а потім запустіть Касу ще раз."
      /usr/bin/open "https://www.python.org/downloads/macos/"
      exit 1
    fi
  else
    # Без Command Line Tools вбудований python3 — лише заглушка, що пропонує їх встановити
    if ask "Для роботи Каси потрібні безкоштовні компоненти Apple (Command Line Tools). Їх встановлюють один раз, це займає кілька хвилин.

Натисніть «Продовжити», а у вікні, що з'явиться, — «Встановити». Коли встановлення завершиться, запустіть Касу ще раз." "Продовжити"; then
      /usr/bin/xcode-select --install >/dev/null 2>&1
    fi
    exit 0
  fi
fi

# 3. Запускаємо касу у фоні: дані — у ~/Library/Application Support, а не всередині програми
export CHECKBOX_DATA_DIR="$DATA"
export PYTHONPATH="$RES/vendor"
export PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1
cd "$RES/app" || { alert "Програму пошкоджено: не знайдено файли каси. Завантажте Касу ще раз."; exit 1; }

echo "=== $(date '+%Y-%m-%d %H:%M:%S') запуск, $PY ===" >> "$LOG"
nohup "$PY" checkbox_web.py >> "$LOG" 2>&1 &
PID=$!

# 4. Чекаємо, поки каса відповість (перший запуск із синхронізацією товарів може тривати довше)
i=0
while [ $i -lt 240 ]; do            # до 2 хвилин
  P=$(saved_port)
  if [ -n "$P" ] && is_running "$P"; then exit 0; fi    # браузер каса відкриває сама
  if ! kill -0 "$PID" 2>/dev/null; then break; fi
  sleep 0.5
  i=$((i + 1))
done

if kill -0 "$PID" 2>/dev/null; then
  exit 0                            # ще запускається — браузер відкриється, щойно буде готово
fi

wait "$PID"
CODE=$?
if [ "$CODE" -eq 2 ]; then
  alert "Не вдалося запустити Касу: усі порти 8000–8019 зайняті іншими програмами. Закрийте зайві програми або перезавантажте Mac."
elif ask "Не вдалося запустити Касу. Подробиці записано в журнали — їх можна передати в підтримку." "Показати журнали"; then
  /usr/bin/open "$LOGS"
fi
exit 1
