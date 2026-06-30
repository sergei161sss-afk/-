"""
Запустить из папки bot_sansara:
    pip install gspread google-auth
    python fix_journal.py

Что делает:
  1. Обновляет заголовки Журнала до нового формата (16 колонок)
  2. Удаляет строки старого формата (где нет tg_id)
"""
import os
import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "1YLGV-Lprd5HZ7wwph728zPgISaVjPflhjlleZbV2Sco"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
ROLE_CODES = {"м1", "мв", "м2", "техпер", "менеджер"}

HEADERS = [
    "Дата", "Имя", "Telegram ID", "Роль", "Филиал",
    "Начало", "Конец", "Тип", "Позиция",
    "Кол-во", "Цена", "Сумма",
    "Период", "День", "Месяц", "Год"
]

# Ищем файл credentials (может быть с двойным расширением)
creds_file = None
for name in ["credentials.json", "credentials.json.json"]:
    if os.path.exists(name):
        creds_file = name
        break

if not creds_file:
    print("❌ Файл credentials.json не найден. Запускай из папки bot_sansara.")
    exit(1)

print(f"✅ Используем: {creds_file}")

creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(SPREADSHEET_ID)
ws = sh.worksheet("Журнал")

rows = ws.get_all_values()
print(f"Строк до обработки: {len(rows)}")

# Фильтруем: оставляем только строки нового формата v3 (tg_id в позиции 2)
good_rows = []
skipped = 0
for row in rows:
    if not any(row):
        continue
    # Пропускаем старый заголовок
    if row[0] == "Дата" and (len(row) < 3 or row[2] in ("Роль", "Telegram ID")):
        continue
    # Старый формат v1/v2: row[2] == роль
    if len(row) >= 3 and row[2] in ROLE_CODES:
        print(f"  ПРОПУСК (старый формат): {row[:5]}")
        skipped += 1
        continue
    # Дополняем до 16 колонок
    while len(row) < 16:
        row.append("")
    good_rows.append(row[:16])

print(f"Удалено старых строк: {skipped}")
print(f"Хороших строк: {len(good_rows)}")

# Очищаем лист и пишем заново
ws.clear()
all_data = [HEADERS] + good_rows
ws.update("A1", all_data)
print(f"✅ Готово! Записано строк: {len(all_data)} (включая заголовок).")
