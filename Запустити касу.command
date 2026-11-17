#!/bin/bash
# Запуск каси з Терміналу на macOS / Linux (для звичайних користувачів Mac є «Каса.app»).
# Перед першим запуском один раз у Терміналі (у папці з касою):
#   chmod +x "Запустити касу.command"
#   xattr -d com.apple.quarantine "Запустити касу.command"   # якщо macOS блокує запуск
cd "$(dirname "$0")" || exit 1

pause() { read -r -p "Натисніть Enter, щоб закрити..."; }

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 не знайдено. Встановіть його з https://www.python.org/downloads/"
  pause; exit 1
fi

if [ ! -f checkbox_web.py ]; then
  echo "Не знайдено checkbox_web.py поруч із цим файлом."
  pause; exit 1
fi

[ -f config.json ] || echo "config.json ще немає — налаштування можна заповнити на сторінці каси."

echo "Запускаю касу... Браузер відкриється автоматично."
echo "Щоб зупинити касу — закрийте це вікно або натисніть Ctrl+C."
echo
python3 checkbox_web.py
echo
echo "Касу зупинено."
pause
