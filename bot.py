#!/usr/bin/env python3
"""
Сансара — бот учёта смен и процедур v2
Запуск: python bot.py
"""
import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q",
                       "gspread==6.1.2", "google-auth==2.29.0"])
import os
import json
import math
import logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = "8960632739:AAFnJBzn-89ctfWOiImkk24bbjpTWZqGZPk"
ADMIN_CHAT_ID = None  # например: 123456789

# ─────────────────────── GOOGLE SHEETS ────────────────────────────
SPREADSHEET_ID = "1YLGV-Lprd5HZ7wwph728zPgISaVjPflhjlleZbV2Sco"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def _get_journal():
    creds = None
    if os.path.exists("credentials.json"):
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    elif os.getenv("GOOGLE_CREDENTIALS_JSON"):
        info = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    if creds is None:
        return None
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet("Журнал")

def write_shift(s: dict):
    try:
        ws = _get_journal()
        if ws is None:
            logger.warning("Google Sheets не подключены — пропускаем запись.")
            return
        date_str = s["date"]
        day   = int(date_str.split(".")[0])
        month = int(date_str.split(".")[1])
        year  = int(date_str.split(".")[2])
        period = "1-15" if day <= 15 else "16-31"
        role   = s["role"]
        cat    = CATALOGUE.get(role, {}).get(s["branch"], {})

        rows = []
        rates_dict = dict(cat.get("ставки", []))
        for pos, qty in s["ставки"].items():
            price = rates_dict.get(pos, 0)
            rows.append([date_str, s["name"], role, s["branch"], "ставка",
                         pos, qty, price, price * qty, period, day, month, year])

        if s.get("процедуры") and "процедуры" in cat:
            procs_dict = dict(cat["процедуры"])
            for pos, qty in s["процедуры"].items():
                price = procs_dict.get(pos, 0)
                rows.append([date_str, s["name"], role, s["branch"], "процедура",
                             pos, qty, price, price * qty, period, day, month, year])

        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            logger.info(f"Google Sheets: записано {len(rows)} строк для {s['name']}")
    except Exception as e:
        logger.error(f"Ошибка записи в Google Sheets: {e}")

# ─────────────────────── ГЕОЛОКАЦИЯ ────────────────────────────────
# Координаты филиалов (Ростов-на-Дону)
BRANCHES_GEO = {
    "Ирий": (47.2333, 39.7489),   # ул. Каскадная, 138а
    "Правь": (47.2367, 39.6942),  # ул. Крупской, 66
}
GEO_RADIUS_OK   = 150   # метров — принято
GEO_RADIUS_WARN = 500   # метров — предупреждение

def haversine(lat1, lon1, lat2, lon2) -> float:
    """Расстояние в метрах между двумя точками."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# ─────────────────────── ПРОФИЛИ ПОЛЬЗОВАТЕЛЕЙ ─────────────────────
USERS_FILE = "users.json"

def load_users() -> dict:
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_users(users: dict):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

def get_user(telegram_id: int) -> dict | None:
    users = load_users()
    return users.get(str(telegram_id))

def set_user(telegram_id: int, data: dict):
    users = load_users()
    users[str(telegram_id)] = data
    save_users(users)

# ─────────────────────────── СОСТОЯНИЯ ────────────────────────────
(REGISTER_NAME, REGISTER_ROLE,
 CHOOSE_BRANCH, LOCATION_CHECK, CHECKLIST,
 MAIN_MENU, ADD_ITEM, ADD_QTY, CONFIRM_CLOSE,
 PROFILE_MENU) = range(10)

# ─────────────────────── СПИСОК ИМЁН ──────────────────────────────
# Все имена одним списком (имя выбирается ДО роли)
ALL_NAMES = [
    "Сергей", "Станислав", "Антон", "Игорь",
    "Анна", "Юлия", "Александр", "Михаил", "Елена",
    "Мария", "Елена Х", "Оля",
]

# ─────────────────────────── РОЛИ ─────────────────────────────────
ROLE_LABELS = {
    "м1":       "👨‍🍳 М-1 (Пармастер, полная смена)",
    "мв":       "📲 М-В (Вызывной пармастер)",
    "м2":       "🔧 М-2 (Пармастер + Тех.пер)",
    "техпер":   "🧹 Тех.пер",
    "менеджер": "💼 Менеджер продаж",
}

ROLE_SHORT = {
    "м1":       "М-1",
    "мв":       "М-В",
    "м2":       "М-2",
    "техпер":   "Тех.пер",
    "менеджер": "Менеджер",
}

# Роли с процедурами (пармастера)
ROLES_WITH_PROCS = {"м1", "мв", "м2"}

# ─────────────────────────── СПРАВОЧНИК ───────────────────────────
CATALOGUE = {
    "м1": {
        "Ирий": {
            "ставки": [
                ("Смена М1",            1500),
                ("Сенная парная",        200),
                ("Самовар",              150),
                ("Чан 1-й",              450),
                ("Чан 2-й",              300),
                ("Чан 3-й",              300),
                ("Подготовка 1-4",       250),
                ("Подготовка 5-6",       300),
                ("Подготовка 7-10",      400),
                ("Ранний выход",         400),
            ],
            "процедуры": [
                ("Первый пар",           150),
                ("Арома Медитация",      210),
                ("Колл Пар крыло",       300),
                ("Колл Пар лед и пламя", 360),
                ("Парение",             1140),
                ("Парение в 4 руки",    1000),
                ("Спа церемония",        600),
                ("Догрев",               480),
                ("Доп Парение",         1500),
                ("Доп спа",              900),
                ("Дет. Пар",             500),
                ("Дет. Спа",             500),
                ("Слияние душ",         2400),
                ("Царевич",             2200),
                ("ИП СХ",              2000),
                ("Массаж 60",           1750),
                ("Массаж 45",           1400),
                ("Массаж 30",            900),
            ],
        },
        "Правь": {
            "ставки": [
                ("Смена М1",            1500),
                ("Самовар",              150),
                ("Подготовка 1-4",       250),
                ("Подготовка 5-6",       300),
                ("Подготовка 7-10",      400),
                ("Ранний выход",         400),
            ],
            "процедуры": [
                ("Арома Медитация",      150),
                ("Колл Пар",             210),
                ("Парение",              750),
                ("Спа церемония",        750),
                ("Догрев",               480),
                ("Дет. Пар",             500),
                ("Дет. Спа",             500),
                ("Доп Парение",          900),
                ("Доп спа",              900),
                ("Слияние душ",         2400),
                ("Массаж 60",           1750),
                ("Массаж 45",           1400),
                ("Массаж 30",            900),
            ],
        },
    },
    "мв": {  # Вызывной — те же ставки/процедуры что М-1
        "Ирий": {
            "ставки": [
                ("Ставка за выход М-В", 1500),
                ("Чан 1-й",              450),
                ("Чан 2-й",              300),
                ("Чан 3-й",              300),
            ],
            "процедуры": [
                ("Первый пар",           150),
                ("Арома Медитация",      210),
                ("Колл Пар крыло",       300),
                ("Колл Пар лед и пламя", 360),
                ("Парение",             1140),
                ("Парение в 4 руки",    1000),
                ("Спа церемония",        600),
                ("Догрев",               480),
                ("Доп Парение",         1500),
                ("Доп спа",              900),
                ("Дет. Пар",             500),
                ("Дет. Спа",             500),
                ("Слияние душ",         2400),
                ("Царевич",             2200),
                ("ИП СХ",              2000),
                ("Массаж 60",           1750),
                ("Массаж 45",           1400),
                ("Массаж 30",            900),
            ],
        },
        "Правь": {
            "ставки": [
                ("Ставка за выход М-В", 1500),
                ("Чан 1-й",              450),
                ("Чан 2-й",              300),
            ],
            "процедуры": [
                ("Арома Медитация",      150),
                ("Колл Пар",             210),
                ("Парение",              750),
                ("Спа церемония",        750),
                ("Догрев",               480),
                ("Дет. Пар",             500),
                ("Дет. Спа",             500),
                ("Доп Парение",          900),
                ("Доп спа",              900),
                ("Слияние душ",         2400),
                ("Массаж 60",           1750),
                ("Массаж 45",           1400),
                ("Массаж 30",            900),
            ],
        },
    },
    "м2": {  # Совмещает тех.пер + пармастер
        "Ирий": {
            "ставки": [
                ("Смена М2",            2200),
                ("Сенная парная",        200),
                ("Самовар",              150),
                ("Чан 1-й",              450),
                ("Чан 2-й",              300),
                ("Чан 3-й",              300),
                ("Подготовка 1-4",       250),
                ("Подготовка 5-6",       300),
                ("Подготовка 7-10",      400),
                ("Ранний выход",         400),
            ],
            "процедуры": [
                ("Первый пар",           150),
                ("Арома Медитация",      210),
                ("Колл Пар крыло",       300),
                ("Колл Пар лед и пламя", 360),
                ("Парение",             1140),
                ("Парение в 4 руки",    1000),
                ("Спа церемония",        600),
                ("Догрев",               480),
                ("Доп Парение",         1500),
                ("Доп спа",              900),
                ("Дет. Пар",             500),
                ("Дет. Спа",             500),
                ("Слияние душ",         2400),
                ("Царевич",             2200),
                ("ИП СХ",              2000),
                ("Массаж 60",           1750),
                ("Массаж 45",           1400),
                ("Массаж 30",            900),
            ],
        },
        "Правь": {
            "ставки": [
                ("Смена М2",            2200),
                ("Самовар",              150),
                ("Подготовка 1-4",       250),
                ("Подготовка 5-6",       300),
                ("Подготовка 7-10",      400),
                ("Ранний выход",         400),
            ],
            "процедуры": [
                ("Арома Медитация",      150),
                ("Колл Пар",             210),
                ("Парение",              750),
                ("Спа церемония",        750),
                ("Догрев",               480),
                ("Дет. Пар",             500),
                ("Дет. Спа",             500),
                ("Доп Парение",          900),
                ("Доп спа",              900),
                ("Слияние душ",         2400),
                ("Массаж 60",           1750),
                ("Массаж 45",           1400),
                ("Массаж 30",            900),
            ],
        },
    },
    "техпер": {
        "Ирий": {
            "ставки": [
                ("Смена тех.пер",       2200),
                ("Сенная парная",        200),
                ("Самовар",              150),
                ("Чан 1-й",              450),
                ("Чан 2-й",              300),
                ("Чан 3-й",              300),
                ("Подготовка 1-4",       250),
                ("Подготовка 5-6",       300),
                ("Подготовка 7-10",      400),
                ("Ранний выход",         400),
            ],
        },
        "Правь": {
            "ставки": [
                ("Смена тех.пер",       2200),
                ("Самовар",              150),
                ("Чан 1-й",              450),
                ("Чан 2-й",              300),
                ("Чан 3-й",              300),
                ("Подготовка 1-4",       250),
                ("Подготовка 5-6",       300),
                ("Подготовка 7-10",      400),
                ("Ранний выход",         400),
            ],
        },
    },
    "менеджер": {
        "Ирий":  {"ставки": [("Смена менеджера", 0)]},
        "Правь": {"ставки": [("Смена менеджера", 0)]},
    },
}

# ─────────────────────────── ЧЕК-ЛИСТЫ (отключены) ───────────────
CHECKLISTS = {
    "техпер": [],
    "м1":     [],
    "м2":     [],
    "мв":     [],
    "менеджер": [],
}

# Оставлено для обратной совместимости (не используется при пустых чек-листах)
_CHECKLISTS_ARCHIVE = {
    "техпер": [
        ("📋 План дня", [
            "Проверить рабочий чат",
            "Проверить время первой программы, кол-во гостей, чан, купель, особые пожелания",
            "Определить зоны приоритетной подготовки",
        ]),
        ("🌳 Улица и вход", [
            "Вход, дорожки, парковка, палуба — чисто и безопасно",
            "Убрать мусор, листья, снег, воду",
            "Подходы к чану и купели: сухо, чисто",
            "Урны и мусорные баки — при необходимости заменить пакеты",
        ]),
        ("🔥 Печь и топочная", [
            "Визуальный осмотр: дверца, поддув, шиберы, дымоход",
            "Убрать остывшую золу в железное ведро",
            "Проверить дрова, щепки, газовый баллон",
            "Разжечь печь схемой «#», разогнать до активного горения",
            "Нет дыма в парной, бак с водой наполнен",
        ]),
        ("🛁 Чан", [
            "Чистота, уровень воды, безопасность подхода",
            "Запас дров и щепок, начать растопку",
            "Температура 39–42 °C",
            "Убрать мусор вокруг чана",
        ]),
        ("💧 Купель и фильтрация", [
            "Прозрачность воды, нет запаха и мусора",
            "Убрать листья сачком, пропылесосить дно",
            "Лестница, поручни, пол вокруг — сухо",
            "Фильтрация: Backwash 2 мин → Rinse 10 сек → Filter",
        ]),
        ("🛋 Гостевые зоны", [
            "Полы, столы, посуда, чайник, вода, салфетки",
            "Текстиль, тапочки, полотенца по кол-ву гостей",
            "Душевые и санузлы: чистота полная",
            "Корзина для белья пуста, пакеты в мусорных вёдрах чистые",
        ]),
        ("🗑 Мусор и передача", [
            "Собрать и вынести мусор из гостевых зон",
            "Сообщить о нехватке или поломке немедленно",
        ]),
    ],
    "м1": [
        ("👤 Личная готовность", [
            "Переодеться: халат и термобельё сухие, без запаха; обувь чистая",
            "Убрать личные вещи из гостевых зон",
            "Проверить программу, кол-во гостей, допы, аллергии, предпочтения",
        ]),
        ("🏠 Приёмка бани", [
            "Пройти путь гостя: вход → гостевая → душ/санузел → чан → купель → парная",
            "Текстиль, тапочки, вода и чай на месте",
            "Чан и купель визуально готовы",
            "Если проблема — сообщить тех.перу и поставить срок устранения",
        ]),
        ("🌡 Парная", [
            "Печь в рабочем режиме, бак с горячей водой заполнен",
            "Чистота пола, полков, сливов, окон, двери",
            "Веники, травы, хвоя, лёд, шайбы, ароматы, вода — подготовлены",
            "Простыни, валики, подушки по кол-ву гостей",
            "Вёдра, ковши, миски, опахало — чистые и на месте",
        ]),
        ("🧴 Запасы в топочной", [
            "Соль — 4 банки",
            "Мёд — 2 банки",
            "Мыло — 1 целая и 1 начатая банка",
            "Сухие простыни для SPA — 4 шт.",
            "Шапочки — 5 шт.",
        ]),
        ("💨 Первый пар", [
            "Полки застелены простынями по кол-ву гостей",
            "Горячая вода для подачи готова",
            "Можжевельник и эвкалипт в ковшике",
            "Первый пар — в шапочках",
        ]),
        ("✅ Финальное подтверждение (за 30 мин до гостя)", [
            "Парная готова: температура и влажность соответствуют программе",
            "Чан и купель готовы",
            "Гостевая зона готова",
        ]),
    ],
    "м2": [  # Объединённый чек-лист Тех.пер + М-1
        ("📋 План дня", [
            "Проверить рабочий чат",
            "Проверить расписание, кол-во гостей, особые пожелания",
        ]),
        ("🌳 Улица и вход", [
            "Вход, дорожки, парковка, палуба — чисто",
            "Убрать мусор, листья, снег",
        ]),
        ("🔥 Печь и топочная", [
            "Осмотр печи, разжечь и разогнать до рабочего режима",
            "Нет дыма в парной, бак наполнен",
        ]),
        ("🛁 Чан и купель", [
            "Чан: чистота, температура 39–42 °C, запас дров",
            "Купель: прозрачность воды, фильтрация работает",
        ]),
        ("🛋 Гостевые зоны", [
            "Полы, посуда, текстиль, тапочки, санузлы",
        ]),
        ("👤 Личная готовность пармастера", [
            "Форма чистая, личные вещи убраны",
            "Программа, гости, аллергии — проверено",
        ]),
        ("🌡 Парная", [
            "Чистота, температура, влажность — готово",
            "Веники, травы, инвентарь подготовлены",
            "Простыни по кол-ву гостей",
        ]),
        ("✅ Подтверждение за 30 мин до гостя", [
            "Баня готова к приёму гостей",
        ]),
    ],
    "мв": [
        ("👤 Личная готовность", [
            "Переодеться в рабочую форму",
            "Проверить программу, гостей, аллергии",
        ]),
        ("🏠 Приёмка бани", [
            "Пройти путь гостя: вход → гостевая → парная → чан → купель",
            "Текстиль, вода, чай на месте",
        ]),
        ("🌡 Парная", [
            "Печь в рабочем режиме, бак с горячей водой",
            "Веники, травы, инвентарь подготовлены",
            "Простыни по кол-ву гостей",
        ]),
        ("✅ Подтверждение за 30 мин до гостя", [
            "Всё готово к программе",
        ]),
    ],
    "менеджер": [],  # Нет чек-листа начала смены
}

# Финальные статусы зон (отключены вместе с чек-листами)
ZONE_STATUSES = {
    "техпер": [],
    "м1":     [],
    "мв":     [],
    "м2":     [],
    "менеджер": [],
}

# ─────────────────────────── СЕССИЯ ───────────────────────────────
def new_session(user: dict):
    return {
        "role":      user["role"],
        "name":      user["name"],
        "branch":    None,
        "date":      datetime.now().strftime("%d.%m.%Y"),
        "start_time": datetime.now().strftime("%H:%M"),
        "ставки":    {},
        "процедуры": {},
        # чек-лист
        "cl_section": 0,
        "cl_item":    0,
        "cl_done":    [],   # [(section_idx, item_idx)]
        # финальные статусы зон
        "zone_statuses": {},
        "zone_idx": 0,
    }

def sess(ctx) -> dict:
    return ctx.user_data.get("s", {})

def reset_sess(ctx, user: dict):
    ctx.user_data["s"] = new_session(user)

# ─────────────────────────── КЛАВИАТУРЫ ───────────────────────────
def kb(buttons: list[tuple], cols=2) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(buttons), cols):
        rows.append([InlineKeyboardButton(t, callback_data=d)
                     for t, d in buttons[i:i+cols]])
    return InlineKeyboardMarkup(rows)

def main_kb(role: str) -> InlineKeyboardMarkup:
    btns = [("➕ Ставки / подготовка", "add_rate")]
    if role in ROLES_WITH_PROCS:
        btns.append(("➕ Процедуры", "add_proc"))
    btns += [
        ("📊 Мой итог", "show"),
        ("👤 Профиль", "profile"),
        ("✅ Закрыть смену", "close"),
    ]
    return kb(btns, cols=2)

def location_kb(branch: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Отправить геолокацию", request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )

# ─────────────────────────── ФОРМАТИРОВАНИЕ ───────────────────────
def _qty_str(qty: float) -> str:
    return str(int(qty)) if qty == int(qty) else str(qty)

def fmt(s: dict, final=False) -> str:
    cat = CATALOGUE.get(s["role"], {}).get(s["branch"], {})
    lines = [
        f"{'📋 ИТОГ СМЕНЫ' if final else '📊 Текущий итог'}",
        f"👤 {s['name']} ({ROLE_SHORT.get(s['role'], s['role'])})",
        f"🏠 {s['branch']}  |  📅 {s['date']}  ⏱ {s.get('start_time','')}",
        "─" * 28,
    ]
    total = 0.0
    rates_dict = dict(cat.get("ставки", []))

    if s["ставки"]:
        lines.append("📌 Ставки / подготовка:")
        for name, qty in s["ставки"].items():
            price = rates_dict.get(name, 0)
            amt = price * qty
            total += amt
            lines.append(f"  {name}: {_qty_str(qty)} × {price:,} = {int(amt):,} ₽")

    if s["процедуры"]:
        procs_dict = dict(cat.get("процедуры", []))
        lines.append("🔥 Процедуры:")
        for name, qty in s["процедуры"].items():
            price = procs_dict.get(name, 0)
            amt = price * qty
            total += amt
            lines.append(f"  {name}: {_qty_str(qty)} × {price:,} = {int(amt):,} ₽")

    if s.get("zone_statuses"):
        lines.append("📍 Статус зон:")
        for zone, status in s["zone_statuses"].items():
            lines.append(f"  {zone}: {status}")

    lines += ["─" * 28, f"💰 ИТОГО: {int(total):,} ₽"]
    return "\n".join(lines)

# ─────────────────────────── /start ───────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    user = get_user(tid)

    if user:
        # Знакомый пользователь — показать профиль и предложить смену
        ctx.user_data["user"] = user
        await update.message.reply_text(
            f"👋 С возвращением, {user['name']}!\n"
            f"Роль: {ROLE_LABELS.get(user['role'], user['role'])}\n\n"
            "Выберите действие:",
            reply_markup=kb([
                ("🚀 Начать смену", "start_shift"),
                ("👤 Мой профиль",  "go_profile"),
                ("🔄 Сменить роль", "change_role"),
            ], cols=1),
        )
        return PROFILE_MENU
    else:
        # Новый пользователь — сначала выбор имени
        await update.message.reply_text(
            "👋 Добро пожаловать в бот Сансара!\n\nВыберите своё имя:",
            reply_markup=kb([(n, f"reg_name_{n}") for n in ALL_NAMES], cols=2),
        )
        return REGISTER_NAME

# ─────────────────────── РЕГИСТРАЦИЯ ──────────────────────────────
async def on_register_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Шаг 1: выбор имени → переход к выбору роли."""
    q = update.callback_query; await q.answer()
    name = q.data.replace("reg_name_", "")
    ctx.user_data["reg_name"] = name
    await q.edit_message_text(
        f"👤 {name}\n\nТеперь выберите роль:",
        reply_markup=kb([(v, f"reg_role_{k}") for k, v in ROLE_LABELS.items()], cols=1),
    )
    return REGISTER_ROLE

async def on_register_role(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Шаг 2: выбор роли → сохранение профиля."""
    q = update.callback_query; await q.answer()
    role = q.data.replace("reg_role_", "")
    name = ctx.user_data.get("reg_name", "")
    tid = update.effective_user.id

    user = {"name": name, "role": role}
    set_user(tid, user)
    ctx.user_data["user"] = user

    await q.edit_message_text(
        f"✅ Профиль создан!\n👤 {name} — {ROLE_LABELS[role]}\n\nВыберите действие:",
        reply_markup=kb([
            ("🚀 Начать смену", "start_shift"),
            ("👤 Мой профиль",  "go_profile"),
        ], cols=1),
    )
    return PROFILE_MENU

# ─────────────────────── ПРОФИЛЬ МЕНЮ ─────────────────────────────
async def on_profile_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    tid = update.effective_user.id
    user = ctx.user_data.get("user") or get_user(tid)

    if q.data == "go_profile":
        await q.edit_message_text(
            f"👤 Профиль\nИмя: {user['name']}\nРоль: {ROLE_LABELS.get(user['role'], user['role'])}",
            reply_markup=kb([
                ("🚀 Начать смену",  "start_shift"),
                ("🔄 Сменить роль",  "change_role"),
                ("✏️ Сменить имя",   "change_name"),
            ], cols=1),
        )
        return PROFILE_MENU

    # Смена роли — без подтверждения
    if q.data == "change_role":
        await q.edit_message_text(
            "Выберите новую роль:",
            reply_markup=kb([(v, f"setrole_{k}") for k, v in ROLE_LABELS.items()], cols=1),
        )
        return PROFILE_MENU

    if q.data.startswith("setrole_"):
        role = q.data.replace("setrole_", "")
        user["role"] = role
        set_user(tid, user)
        ctx.user_data["user"] = user
        await q.edit_message_text(
            f"✅ Роль изменена: {ROLE_LABELS[role]}",
            reply_markup=kb([
                ("🚀 Начать смену", "start_shift"),
                ("👤 Мой профиль",  "go_profile"),
            ], cols=1),
        )
        return PROFILE_MENU

    # Смена имени — требует подтверждения
    if q.data == "change_name":
        await q.edit_message_text(
            f"⚠️ Сейчас: {user['name']}\n\nВыберите новое имя:",
            reply_markup=kb([(n, f"newname_{n}") for n in ALL_NAMES], cols=2),
        )
        return PROFILE_MENU

    if q.data.startswith("newname_"):
        new_name = q.data.replace("newname_", "")
        await q.edit_message_text(
            f"Подтвердите смену имени:\n{user['name']} → {new_name}",
            reply_markup=kb([
                ("✅ Подтвердить", f"confirmname_{new_name}"),
                ("❌ Отмена",       "go_profile"),
            ], cols=1),
        )
        return PROFILE_MENU

    if q.data.startswith("confirmname_"):
        new_name = q.data.replace("confirmname_", "")
        user["name"] = new_name
        set_user(tid, user)
        ctx.user_data["user"] = user
        await q.edit_message_text(
            f"✅ Имя изменено: {new_name}",
            reply_markup=kb([("🚀 Начать смену", "start_shift")], cols=1),
        )
        return PROFILE_MENU

    if q.data == "start_shift":
        ctx.user_data["user"] = user
        await q.edit_message_text(
            "Выберите филиал:",
            reply_markup=kb([("🏠 Ирий", "br_Ирий"), ("🏠 Правь", "br_Правь")]),
        )
        return CHOOSE_BRANCH

    return PROFILE_MENU

# ─────────────────────── ВЫБОР ФИЛИАЛА ────────────────────────────
async def on_branch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    user = ctx.user_data.get("user") or get_user(update.effective_user.id)
    branch = q.data.replace("br_", "")

    reset_sess(ctx, user)
    s = sess(ctx)
    s["branch"] = branch

    # ГЕО ОТКЛЮЧЕНО ДЛЯ ТЕСТИРОВАНИЯ — включить обратно после проверки
    s["start_time"] = datetime.now().strftime("%H:%M")
    checklist = CHECKLISTS.get(user["role"], [])
    if checklist:
        s["cl_section"] = 0
        s["cl_item"] = 0
        s["cl_done"] = []
        await q.edit_message_text(f"✅ Филиал: {branch}\n⏱ Смена открыта в {s['start_time']}")
        return await send_checklist_item(update, ctx)
    else:
        await q.edit_message_text(
            f"✅ {user['name']} · {ROLE_SHORT.get(user['role'], user['role'])} · {branch} · {s['date']}\n\nСмена открыта!",
            reply_markup=main_kb(user["role"]),
        )
        return MAIN_MENU

async def on_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    s = sess(ctx)
    branch = s.get("branch", "")

    target = BRANCHES_GEO.get(branch)
    if not target:
        await update.message.reply_text(
            "Филиал не найден. Напишите /start.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    dist = haversine(loc.latitude, loc.longitude, target[0], target[1])

    if dist <= GEO_RADIUS_OK:
        status_msg = f"✅ Вы на месте ({int(dist)} м от филиала)"
    elif dist <= GEO_RADIUS_WARN:
        status_msg = f"⚠️ Вы немного далеко ({int(dist)} м). Продолжаем."
    else:
        # Уведомляем ОД и отказываем
        if ADMIN_CHAT_ID:
            await ctx.bot.send_message(
                ADMIN_CHAT_ID,
                f"🔴 {s['name']} ({ROLE_SHORT.get(s['role'],s['role'])}) пытается начать смену "
                f"на {branch}, но находится в {int(dist)} м от объекта!"
            )
        await update.message.reply_text(
            f"❌ Вы слишком далеко от {branch} ({int(dist)} м).\n"
            f"Смена не открыта. Обратитесь к ОД.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    s["start_time"] = datetime.now().strftime("%H:%M")
    s["geo_dist"] = int(dist)

    await update.message.reply_text(
        f"{status_msg}\n"
        f"⏱ Смена открыта в {s['start_time']}",
        reply_markup=ReplyKeyboardRemove(),
    )

    # Запускаем чек-лист (если он есть для роли)
    checklist = CHECKLISTS.get(s["role"], [])
    if checklist:
        s["cl_section"] = 0
        s["cl_item"] = 0
        s["cl_done"] = []
        return await send_checklist_item(update, ctx)
    else:
        # Менеджер — сразу в главное меню
        await ctx.bot.send_message(
            update.effective_chat.id,
            f"✅ {s['name']} · {ROLE_SHORT.get(s['role'],s['role'])} · {s['branch']} · {s['date']}\n\n"
            "Смена открыта! Добавляйте по мере работы.",
            reply_markup=main_kb(s["role"]),
        )
        return MAIN_MENU

# ─────────────────────── ЧЕК-ЛИСТ ────────────────────────────────
async def send_checklist_item(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = sess(ctx)
    role = s["role"]
    checklist = CHECKLISTS.get(role, [])

    sec_idx = s["cl_section"]
    item_idx = s["cl_item"]

    if sec_idx >= len(checklist):
        # Чек-лист пройден — финальные статусы зон
        return await start_zone_statuses(update, ctx)

    sec_title, items = checklist[sec_idx]

    if item_idx >= len(items):
        # Следующая секция
        s["cl_section"] += 1
        s["cl_item"] = 0
        return await send_checklist_item(update, ctx)

    item_text = items[item_idx]
    done = (sec_idx, item_idx) in [(d[0], d[1]) for d in s["cl_done"]]

    total_items = sum(len(sec[1]) for sec in checklist)
    done_count = len(s["cl_done"])
    progress = f"{done_count}/{total_items}"

    text = (
        f"*{sec_title}*  [{progress}]\n\n"
        f"{'✅' if done else '☐'} {item_text}"
    )

    btns = [
        ("✅ Выполнено", "cl_done"),
        ("⏭ Пропустить", "cl_skip"),
    ]
    if done_count > 0:
        btns.append(("↩️ Назад", "cl_back"))
    btns.append(("📋 Открыть меню", "cl_open_menu"))

    chat_id = update.effective_chat.id
    await ctx.bot.send_message(
        chat_id, text,
        reply_markup=kb(btns, cols=2),
        parse_mode="Markdown",
    )
    return CHECKLIST

async def on_checklist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    s = sess(ctx)

    if q.data == "cl_done":
        s["cl_done"].append((s["cl_section"], s["cl_item"]))
        s["cl_item"] += 1
        await q.delete_message()
        return await send_checklist_item(update, ctx)

    if q.data == "cl_skip":
        s["cl_item"] += 1
        await q.delete_message()
        return await send_checklist_item(update, ctx)

    if q.data == "cl_back":
        if s["cl_item"] > 0:
            s["cl_item"] -= 1
        elif s["cl_section"] > 0:
            s["cl_section"] -= 1
            s["cl_item"] = len(CHECKLISTS[s["role"]][s["cl_section"]][1]) - 1
        # Убрать last done
        if s["cl_done"]:
            s["cl_done"].pop()
        await q.delete_message()
        return await send_checklist_item(update, ctx)

    if q.data == "cl_open_menu":
        await q.edit_message_text(
            "⚠️ Чек-лист не завершён. Открываю меню смены.\n"
            "Вернуться к чек-листу: /checklist",
            reply_markup=main_kb(s["role"]),
        )
        return MAIN_MENU

    return CHECKLIST

async def cmd_checklist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Возврат к чек-листу командой /checklist."""
    s = sess(ctx)
    if not s or not s.get("branch"):
        await update.message.reply_text("Сначала начните смену: /start")
        return MAIN_MENU
    return await send_checklist_item(update, ctx)

# ─────────────────────── СТАТУСЫ ЗОН ──────────────────────────────
STATUS_ICONS = {"🟢 Готово": "🟢", "🟡 Внимание": "🟡", "🔴 Проблема": "🔴"}

async def start_zone_statuses(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = sess(ctx)
    zones = ZONE_STATUSES.get(s["role"], [])
    s["zone_idx"] = 0
    s["zone_statuses"] = {}

    if not zones:
        return await open_main_menu(update, ctx)

    return await send_zone_status_question(update, ctx)

async def send_zone_status_question(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = sess(ctx)
    zones = ZONE_STATUSES.get(s["role"], [])
    idx = s["zone_idx"]

    if idx >= len(zones):
        return await open_main_menu(update, ctx)

    zone = zones[idx]
    chat_id = update.effective_chat.id
    await ctx.bot.send_message(
        chat_id,
        f"📊 Статус зоны: *{zone}*",
        parse_mode="Markdown",
        reply_markup=kb([
            ("🟢 Готово",    f"zone_ok_{idx}"),
            ("🟡 Внимание",  f"zone_warn_{idx}"),
            ("🔴 Проблема",  f"zone_bad_{idx}"),
        ], cols=3),
    )
    return CHECKLIST

async def on_zone_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    s = sess(ctx)
    zones = ZONE_STATUSES.get(s["role"], [])

    data = q.data  # zone_ok_0 / zone_warn_0 / zone_bad_0
    parts = data.split("_")
    status_key = parts[1]  # ok / warn / bad
    idx = int(parts[2])

    status_map = {"ok": "🟢 Готово", "warn": "🟡 Внимание", "bad": "🔴 Проблема"}
    zone = zones[idx]
    s["zone_statuses"][zone] = status_map[status_key]

    # Уведомить ОД о красном статусе
    if status_key == "bad" and ADMIN_CHAT_ID:
        await ctx.bot.send_message(
            ADMIN_CHAT_ID,
            f"🔴 {s['name']} ({s['branch']}) — проблема в зоне: *{zone}*",
            parse_mode="Markdown",
        )

    s["zone_idx"] = idx + 1
    await q.delete_message()
    return await send_zone_status_question(update, ctx)

async def open_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = sess(ctx)
    total_items = sum(len(sec[1]) for sec in CHECKLISTS.get(s["role"], []))
    done_count = len(s["cl_done"])

    text = (
        f"✅ Чек-лист завершён ({done_count}/{total_items})\n\n"
        f"👤 {s['name']} · {ROLE_SHORT.get(s['role'],s['role'])} · {s['branch']} · {s['date']}\n"
        "Смена открыта!"
    )
    chat_id = update.effective_chat.id
    await ctx.bot.send_message(chat_id, text, reply_markup=main_kb(s["role"]))
    return MAIN_MENU

# ─────────────────────── ГЛАВНОЕ МЕНЮ ─────────────────────────────
async def on_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    s = sess(ctx)

    if q.data == "show":
        await q.edit_message_text(fmt(s), reply_markup=main_kb(s["role"]))
        return MAIN_MENU

    if q.data == "profile":
        user = ctx.user_data.get("user") or get_user(update.effective_user.id)
        await q.edit_message_text(
            f"👤 {user['name']}\nРоль: {ROLE_LABELS.get(user['role'], user['role'])}",
            reply_markup=kb([
                ("🔄 Сменить роль", "change_role"),
                ("↩️ Назад",         "back_main"),
            ], cols=1),
        )
        return MAIN_MENU

    if q.data == "change_role":
        await q.edit_message_text(
            "Выберите новую роль:",
            reply_markup=kb([(v, f"setrole_main_{k}") for k, v in ROLE_LABELS.items()], cols=1),
        )
        return MAIN_MENU

    if q.data.startswith("setrole_main_"):
        role = q.data.replace("setrole_main_", "")
        tid = update.effective_user.id
        user = ctx.user_data.get("user") or get_user(tid)
        user["role"] = role
        set_user(tid, user)
        ctx.user_data["user"] = user
        s["role"] = role
        await q.edit_message_text(
            f"✅ Роль изменена: {ROLE_LABELS[role]}",
            reply_markup=main_kb(role),
        )
        return MAIN_MENU

    if q.data == "back_main":
        await q.edit_message_text(
            f"👤 {s['name']} · {s['branch']}",
            reply_markup=main_kb(s["role"]),
        )
        return MAIN_MENU

    if q.data == "close":
        await q.edit_message_text(
            fmt(s, final=True) + "\n\n⚠️ Подтвердить закрытие смены?",
            reply_markup=kb([
                ("✅ Да, закрыть", "do_close"),
                ("↩️ Назад",        "back_main"),
            ]),
        )
        return CONFIRM_CLOSE

    # Показать список для добавления
    cat = CATALOGUE.get(s["role"], {}).get(s["branch"], {})
    if q.data == "add_rate":
        items = cat.get("ставки", [])
        prefix = "r"
        title = "Выберите ставку / подготовку:"
    else:
        items = cat.get("процедуры", [])
        prefix = "p"
        title = "Выберите процедуру:"

    ctx.user_data["cur_items"] = items
    ctx.user_data["cur_prefix"] = prefix
    btns = _item_buttons(items, prefix, s)
    btns.append(("↩️ Назад", "back"))
    await q.edit_message_text(title, reply_markup=kb(btns, cols=1))
    return ADD_ITEM

def _item_buttons(items, prefix, s):
    bucket = s["ставки"] if prefix == "r" else s["процедуры"]
    btns = []
    for i, (name, price) in enumerate(items):
        qty = bucket.get(name, 0)
        mark = "✅ " if qty else ""
        qty_str = f"  [{_qty_str(qty)}]" if qty else ""
        btns.append((f"{mark}{name}  +{price:,}₽{qty_str}", f"{prefix}_{i}"))
    return btns

# ─────────────────────── ДОБАВИТЬ ПОЗИЦИЮ ─────────────────────────
async def on_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    s = sess(ctx)

    if q.data == "back":
        await q.edit_message_text(
            f"👤 {s['name']} · {s['branch']} · {s['date']}",
            reply_markup=main_kb(s["role"]),
        )
        return MAIN_MENU

    prefix, idx_str = q.data.split("_", 1)
    idx = int(idx_str)
    items = ctx.user_data["cur_items"]
    name, price = items[idx]

    ctx.user_data["cur_name"] = name
    ctx.user_data["cur_price"] = price
    ctx.user_data["cur_prefix"] = prefix

    bucket = s["ставки"] if prefix == "r" else s["процедуры"]
    cur_qty = bucket.get(name, 0)
    cur_str = f" (сейчас: {_qty_str(cur_qty)})" if cur_qty else ""

    await q.edit_message_text(
        f"*{name}*  — {price:,} ₽{cur_str}\n\nВыберите количество:",
        parse_mode="Markdown",
        reply_markup=kb([
            ("+0.5", f"qty_plus05"),
            ("+1",   f"qty_plus1"),
            ("-0.5", f"qty_minus05"),
            ("-1",   f"qty_minus1"),
            ("↩️ Назад к списку", "qty_back"),
        ], cols=2),
    )
    return ADD_QTY

async def on_qty(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    s = sess(ctx)

    if q.data == "qty_back":
        items = ctx.user_data["cur_items"]
        prefix = ctx.user_data["cur_prefix"]
        btns = _item_buttons(items, prefix, s)
        btns.append(("↩️ Назад", "back"))
        title = "Ставки / подготовка:" if prefix == "r" else "Процедуры:"
        await q.edit_message_text(title, reply_markup=kb(btns, cols=1))
        return ADD_ITEM

    name   = ctx.user_data["cur_name"]
    price  = ctx.user_data["cur_price"]
    prefix = ctx.user_data["cur_prefix"]
    bucket = s["ставки"] if prefix == "r" else s["процедуры"]

    delta_map = {
        "qty_plus05":  0.5,
        "qty_plus1":   1.0,
        "qty_minus05": -0.5,
        "qty_minus1":  -1.0,
    }
    delta = delta_map.get(q.data, 0)
    cur = bucket.get(name, 0)
    new_qty = max(0, cur + delta)

    if new_qty == 0:
        bucket.pop(name, None)
    else:
        bucket[name] = new_qty

    qty_str = _qty_str(new_qty)
    amt = price * new_qty

    await q.edit_message_text(
        f"*{name}*\nКоличество: {qty_str}  →  {int(amt):,} ₽\n\nВыберите количество:",
        parse_mode="Markdown",
        reply_markup=kb([
            ("+0.5", "qty_plus05"),
            ("+1",   "qty_plus1"),
            ("-0.5", "qty_minus05"),
            ("-1",   "qty_minus1"),
            ("↩️ Назад к списку", "qty_back"),
        ], cols=2),
    )
    return ADD_QTY

# ─────────────────────── ЗАКРЫТЬ СМЕНУ ────────────────────────────
async def on_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    s = sess(ctx)

    if q.data == "back_main":
        await q.edit_message_text(
            f"👤 {s['name']} · {s['branch']} · {s['date']}",
            reply_markup=main_kb(s["role"]),
        )
        return MAIN_MENU

    # Закрываем смену
    summary = fmt(s, final=True)
    end_time = datetime.now().strftime("%H:%M")
    summary += f"\n⏱ Конец смены: {end_time}"

    await q.edit_message_text(summary + "\n\n✅ Смена закрыта. Спасибо!")
    write_shift(s)

    if ADMIN_CHAT_ID:
        await ctx.bot.send_message(ADMIN_CHAT_ID, summary)

    user = ctx.user_data.get("user") or get_user(update.effective_user.id)
    ctx.user_data.clear()
    ctx.user_data["user"] = user

    await ctx.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Начать новую смену?",
        reply_markup=kb([("🔄 Новая смена", "new_shift")]),
    )
    return ConversationHandler.END

async def on_new_shift(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    user = ctx.user_data.get("user") or get_user(update.effective_user.id)
    await q.edit_message_text(
        f"👋 {user['name']}, выберите филиал:",
        reply_markup=kb([("🏠 Ирий", "br_Ирий"), ("🏠 Правь", "br_Правь")]),
    )
    return CHOOSE_BRANCH

# ─────────────────────── SETUP RV (одноразовая команда) ──────────
JOURNAL_ID = SPREADSHEET_ID  # тот же файл
IMPORT_FORMULA = f'=IMPORTRANGE("{JOURNAL_ID}";"Журнал!A:M")'

RV_FILES = {
    "Сергей":    "1hneRDZVxQRyueRVsz89aW5tQdJJsv3JcQApMitZBvgQ",
    "Станислав": "1phz7eLoXsadKOSRaLt3MseKdOs-1ypJjXKW3pAznwM4",
    "Антон":     "1ujyoy--4bmfUEPXFoEpxjDPNzEBcFjzmMnrvgms3_9E",
    "Игорь":     "1aIEIDPfgxcYK4ZBKhjJLVYNJ6p3eIKE0GWfzAuQh_gY",
    "Анна":      "1gy36p_rIz3Lbc8q1TLqgxrowFXpXgI_qsld230w2D2k",
    "Юлия":      "1zEQ3FovCxf_kfHdEFVvm_YVKRRsi3E85tctEgmwbTSg",
    "Александр": "1tCeemlAlBawXfg3DvjHAes7Hp9PahvnGomqwBIJgzws",
    "Михаил":    "1xeJKH5BWHc_Rk-SnPukLv5x81v823kOIlIkDAOaHnp8",
    "Елена":     "1m2KlHdCdB6A-bQ6CTbZO9YDEuMq1p8duqAjrCXc_7oU",
    "Мария":     "1b6gJD1uq1Q1_TaiNbd9GOLN88izW7es6MRQ9SYEyH24",
    "Елена Х":   "1YsMSQpb0LZL6BIWdQ5F37rQ3DWKHWP5ERKbZpfbk5gg",
    "Оля":       "1Tpg2mke-EdIll0YNnvsT8N-M4eYL3cLQt0egbT55xTY",
}

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Сброс профиля — пользователь проходит регистрацию заново."""
    tid = update.effective_user.id
    users = load_users()
    if str(tid) in users:
        del users[str(tid)]
        save_users(users)
    ctx.user_data.clear()
    await update.message.reply_text(
        "🔄 Профиль сброшен. Выберите своё имя:",
        reply_markup=kb([(n, f"reg_name_{n}") for n in ALL_NAMES], cols=2),
    )
    return REGISTER_NAME

async def cmd_setup_rv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Одноразовая команда: добавляет лист Журнал с IMPORTRANGE во все РВ."""
    await update.message.reply_text("⏳ Настраиваю РВ файлы...")

    creds = None
    if os.path.exists("credentials.json"):
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    elif os.getenv("GOOGLE_CREDENTIALS_JSON"):
        info = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)

    if creds is None:
        await update.message.reply_text("❌ Нет credentials Google.")
        return

    gc = gspread.authorize(creds)
    headers = ["Дата","Имя","Роль","Филиал","Тип","Позиция","Кол-во","Цена","Сумма","Период","День","Месяц","Год"]
    results = []

    for name, sid in RV_FILES.items():
        try:
            sh = gc.open_by_key(sid)
            existing = [ws.title for ws in sh.worksheets()]
            if "Журнал" in existing:
                ws = sh.worksheet("Журнал")
            else:
                ws = sh.add_worksheet(title="Журнал", rows=2000, cols=13, index=0)
            ws.update("A1", [headers], value_input_option="USER_ENTERED")
            ws.update("A2", [[IMPORT_FORMULA]], value_input_option="USER_ENTERED")
            results.append(f"✅ {name}")
        except Exception as e:
            results.append(f"❌ {name}: {e}")

    await update.message.reply_text(
        "Результат:\n" + "\n".join(results) +
        "\n\n📌 Открой каждый РВ в браузере → лист «Журнал» → нажми «Разрешить доступ»"
    )

# ──────────────────────────── MAIN ────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("reset", cmd_reset),
            CallbackQueryHandler(on_new_shift, pattern="^new_shift$"),
        ],
        states={
            REGISTER_NAME: [
                CallbackQueryHandler(on_register_name, pattern="^reg_name_")
            ],
            REGISTER_ROLE: [
                CallbackQueryHandler(on_register_role, pattern="^reg_role_")
            ],
            PROFILE_MENU: [
                CallbackQueryHandler(on_profile_menu,
                    pattern="^(go_profile|change_role|setrole_.+|change_name|newname_.+|confirmname_.+|start_shift)$")
            ],
            CHOOSE_BRANCH: [
                CallbackQueryHandler(on_branch, pattern="^br_")
            ],
            LOCATION_CHECK: [
                MessageHandler(filters.LOCATION, on_location)
            ],
            CHECKLIST: [
                CallbackQueryHandler(on_checklist,   pattern="^cl_"),
                CallbackQueryHandler(on_zone_status, pattern="^zone_"),
            ],
            MAIN_MENU: [
                CallbackQueryHandler(on_main,
                    pattern="^(show|close|add_rate|add_proc|profile|change_role|setrole_main_.+|back_main)$")
            ],
            ADD_ITEM: [
                CallbackQueryHandler(on_add, pattern="^(r_|p_|back$)")
            ],
            ADD_QTY: [
                CallbackQueryHandler(on_qty, pattern="^qty_")
            ],
            CONFIRM_CLOSE: [
                CallbackQueryHandler(on_confirm, pattern="^(do_close|back_main)$")
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CommandHandler("reset", cmd_reset),
            CommandHandler("checklist", cmd_checklist),
            CallbackQueryHandler(on_register_name, pattern="^reg_name_"),
            CallbackQueryHandler(on_register_role, pattern="^reg_role_"),
        ],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("setup_rv", cmd_setup_rv))
    logger.info("Бот Сансара v2 запущен ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
