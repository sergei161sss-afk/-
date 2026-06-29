#!/usr/bin/env python3
"""
Сансара — бот учёта смен и процедур v2
"""
import os, json, math, logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters,
)

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN          = "8960632739:AAFnJBzn-89ctfWOiImkk24bbjpTWZqGZPk"
ADMIN_CHAT_ID  = None
SPREADSHEET_ID = "1YLGV-Lprd5HZ7wwph728zPgISaVjPflhjlleZbV2Sco"
SCOPES         = ["https://www.googleapis.com/auth/spreadsheets"]

def _get_creds():
    if os.path.exists("credentials.json"):
        return Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return None

def _get_journal():
    creds = _get_creds()
    if creds is None:
        return None
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID).worksheet("Журнал")

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
        cat = CATALOGUE.get(s["role"], {}).get(s["branch"], {})
        rows = []
        rates_dict = dict(cat.get("ставки", []))
        for pos, qty in s["ставки"].items():
            price = rates_dict.get(pos, 0)
            rows.append([date_str, s["name"], s["role"], s["branch"], "ставка",
                         pos, qty, price, price * qty, period, day, month, year])
        if s.get("процедуры") and "процедуры" in cat:
            procs_dict = dict(cat["процедуры"])
            for pos, qty in s["процедуры"].items():
                price = procs_dict.get(pos, 0)
                rows.append([date_str, s["name"], s["role"], s["branch"], "процедура",
                             pos, qty, price, price * qty, period, day, month, year])
        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            logger.info(f"Sheets: {len(rows)} строк для {s['name']}")
    except Exception as e:
        logger.error(f"Ошибка записи в Sheets: {e}")

BRANCHES_GEO = {"Ирий": (47.2333, 39.7489), "Правь": (47.2367, 39.6942)}
GEO_RADIUS_OK   = 150
GEO_RADIUS_WARN = 500

def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

USERS_FILE = "users.json"

def load_users() -> dict:
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_users(u: dict):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(u, f, ensure_ascii=False, indent=2)

def get_user(tid: int):
    return load_users().get(str(tid))

def set_user(tid: int, data: dict):
    u = load_users()
    u[str(tid)] = data
    save_users(u)

(REGISTER_NAME, REGISTER_ROLE,
 CHOOSE_BRANCH, LOCATION_CHECK, CHECKLIST,
 MAIN_MENU, ADD_ITEM, ADD_QTY, CONFIRM_CLOSE,
 PROFILE_MENU) = range(10)

ALL_NAMES = [
    "Сергей", "Станислав", "Антон", "Игорь",
    "Анна", "Юлия", "Александр", "Михаил", "Елена",
    "Мария", "Елена Х", "Оля",
]

ROLE_LABELS = {
    "м1":       "М-1 (Пармастер, полная смена)",
    "мв":       "М-В (Вызывной пармастер)",
    "м2":       "М-2 (Пармастер + Тех.пер)",
    "техпер":   "Тех.пер",
    "менеджер": "Менеджер продаж",
}
ROLE_SHORT = {
    "м1": "М-1", "мв": "М-В", "м2": "М-2",
    "техпер": "Тех.пер", "менеджер": "Менеджер",
}
ROLES_WITH_PROCS = {"м1", "мв", "м2"}

CATALOGUE = {
    "м1": {
        "Ирий": {
            "ставки": [
                ("Смена М1", 1500), ("Сенная парная", 200), ("Самовар", 150),
                ("Чан 1-й", 450), ("Чан 2-й", 300), ("Чан 3-й", 300),
                ("Подготовка 1-4", 250), ("Подготовка 5-6", 300),
                ("Подготовка 7-10", 400), ("Ранний выход", 400),
            ],
            "процедуры": [
                ("Первый пар", 150), ("Арома Медитация", 210),
                ("Колл Пар крыло", 300), ("Колл Пар лед и пламя", 360),
                ("Парение", 1140), ("Парение в 4 руки", 1000),
                ("Спа церемония", 600), ("Догрев", 480),
                ("Доп Парение", 1500), ("Доп спа", 900),
                ("Дет. Пар", 500), ("Дет. Спа", 500),
                ("Слияние душ", 2400), ("Царевич", 2200), ("ИП СХ", 2000),
                ("Массаж 60", 1750), ("Массаж 45", 1400), ("Массаж 30", 900),
            ],
        },
        "Правь": {
            "ставки": [
                ("Смена М1", 1500), ("Самовар", 150),
                ("Подготовка 1-4", 250), ("Подготовка 5-6", 300),
                ("Подготовка 7-10", 400), ("Ранний выход", 400),
            ],
            "процедуры": [
                ("Арома Медитация", 150), ("Колл Пар", 210),
                ("Парение", 750), ("Спа церемония", 750), ("Догрев", 480),
                ("Дет. Пар", 500), ("Дет. Спа", 500),
                ("Доп Парение", 900), ("Доп спа", 900),
                ("Слияние душ", 2400), ("Массаж 60", 1750),
                ("Массаж 45", 1400), ("Массаж 30", 900),
            ],
        },
    },
    "мв": {
        "Ирий": {
            "ставки": [
                ("Ставка за выход М-В", 1500), ("Чан 1-й", 450),
                ("Чан 2-й", 300), ("Чан 3-й", 300),
            ],
            "процедуры": [
                ("Первый пар", 150), ("Арома Медитация", 210),
                ("Колл Пар крыло", 300), ("Колл Пар лед и пламя", 360),
                ("Парение", 1140), ("Парение в 4 руки", 1000),
                ("Спа церемония", 600), ("Догрев", 480),
                ("Доп Парение", 1500), ("Доп спа", 900),
                ("Дет. Пар", 500), ("Дет. Спа", 500),
                ("Слияние душ", 2400), ("Царевич", 2200), ("ИП СХ", 2000),
                ("Массаж 60", 1750), ("Массаж 45", 1400), ("Массаж 30", 900),
            ],
        },
        "Правь": {
            "ставки": [
                ("Ставка за выход М-В", 1500), ("Чан 1-й", 450), ("Чан 2-й", 300),
            ],
            "процедуры": [
                ("Арома Медитация", 150), ("Колл Пар", 210),
                ("Парение", 750), ("Спа церемония", 750), ("Догрев", 480),
                ("Дет. Пар", 500), ("Дет. Спа", 500),
                ("Доп Парение", 900), ("Доп спа", 900),
                ("Слияние душ", 2400), ("Массаж 60", 1750),
                ("Массаж 45", 1400), ("Массаж 30", 900),
            ],
        },
    },
    "м2": {
        "Ирий": {
            "ставки": [
                ("Смена М2", 2200), ("Сенная парная", 200), ("Самовар", 150),
                ("Чан 1-й", 450), ("Чан 2-й", 300), ("Чан 3-й", 300),
                ("Подготовка 1-4", 250), ("Подготовка 5-6", 300),
                ("Подготовка 7-10", 400), ("Ранний выход", 400),
            ],
            "процедуры": [
                ("Первый пар", 150), ("Арома Медитация", 210),
                ("Колл Пар крыло", 300), ("Колл Пар лед и пламя", 360),
                ("Парение", 1140), ("Парение в 4 руки", 1000),
                ("Спа церемония", 600), ("Догрев", 480),
                ("Доп Парение", 1500), ("Доп спа", 900),
                ("Дет. Пар", 500), ("Дет. Спа", 500),
                ("Слияние душ", 2400), ("Царевич", 2200), ("ИП СХ", 2000),
                ("Массаж 60", 1750), ("Массаж 45", 1400), ("Массаж 30", 900),
            ],
        },
        "Правь": {
            "ставки": [
                ("Смена М2", 2200), ("Самовар", 150),
                ("Подготовка 1-4", 250), ("Подготовка 5-6", 300),
                ("Подготовка 7-10", 400), ("Ранний выход", 400),
            ],
            "процедуры": [
                ("Арома Медитация", 150), ("Колл Пар", 210),
                ("Парение", 750), ("Спа церемония", 750), ("Догрев", 480),
                ("Дет. Пар", 500), ("Дет. Спа", 500),
                ("Доп Парение", 900), ("Доп спа", 900),
                ("Слияние душ", 2400), ("Массаж 60", 1750),
                ("Массаж 45", 1400), ("Массаж 30", 900),
            ],
        },
    },
    "техпер": {
        "Ирий": {
            "ставки": [
                ("Смена тех.пер", 2200), ("Сенная парная", 200), ("Самовар", 150),
                ("Чан 1-й", 450), ("Чан 2-й", 300), ("Чан 3-й", 300),
                ("Подготовка 1-4", 250), ("Подготовка 5-6", 300),
                ("Подготовка 7-10", 400), ("Ранний выход", 400),
            ],
        },
        "Правь": {
            "ставки": [
                ("Смена тех.пер", 2200), ("Самовар", 150),
                ("Чан 1-й", 450), ("Чан 2-й", 300), ("Чан 3-й", 300),
                ("Подготовка 1-4", 250), ("Подготовка 5-6", 300),
                ("Подготовка 7-10", 400), ("Ранний выход", 400),
            ],
        },
    },
    "менеджер": {
        "Ирий":  {"ставки": [("Смена менеджера", 0)]},
        "Правь": {"ставки": [("Смена менеджера", 0)]},
    },
}

def new_session(user: dict) -> dict:
    return {
        "role": user["role"], "name": user["name"], "branch": None,
        "date": datetime.now().strftime("%d.%m.%Y"),
        "start_time": datetime.now().strftime("%H:%M"),
        "ставки": {}, "процедуры": {},
    }

def sess(ctx) -> dict:
    return ctx.user_data.get("s", {})

def reset_sess(ctx, user: dict):
    ctx.user_data["s"] = new_session(user)

def kb(buttons: list, cols: int = 2) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(buttons), cols):
        rows.append([InlineKeyboardButton(t, callback_data=d) for t, d in buttons[i:i+cols]])
    return InlineKeyboardMarkup(rows)

def main_kb(role: str) -> InlineKeyboardMarkup:
    btns = [("+ Ставки / подготовка", "add_rate")]
    if role in ROLES_WITH_PROCS:
        btns.append(("+ Процедуры", "add_proc"))
    btns += [("Мой итог", "show"), ("Профиль", "profile"), ("Закрыть смену", "close")]
    return kb(btns, cols=2)

def name_kb() -> ReplyKeyboardMarkup:
    rows = []
    for i in range(0, len(ALL_NAMES), 3):
        rows.append([KeyboardButton(n) for n in ALL_NAMES[i:i+3]])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)

def _qty_str(q: float) -> str:
    return str(int(q)) if q == int(q) else str(q)

def fmt(s: dict, final: bool = False) -> str:
    cat = CATALOGUE.get(s["role"], {}).get(s["branch"], {})
    lines = [
        "ИТОГ СМЕНЫ" if final else "Текущий итог",
        f"{s['name']} ({ROLE_SHORT.get(s['role'], s['role'])})",
        f"{s['branch']} | {s['date']} {s.get('start_time', '')}",
        "-" * 28,
    ]
    total = 0.0
    rates_dict = dict(cat.get("ставки", []))
    if s["ставки"]:
        lines.append("Ставки / подготовка:")
        for name, qty in s["ставки"].items():
            price = rates_dict.get(name, 0)
            amt = price * qty
            total += amt
            lines.append(f"  {name}: {_qty_str(qty)} x {price} = {int(amt)} руб")
    if s.get("процедуры"):
        procs_dict = dict(cat.get("процедуры", []))
        lines.append("Процедуры:")
        for name, qty in s["процедуры"].items():
            price = procs_dict.get(name, 0)
            amt = price * qty
            total += amt
            lines.append(f"  {name}: {_qty_str(qty)} x {price} = {int(amt)} руб")
    lines += ["-" * 28, f"ИТОГО: {int(total)} руб"]
    return "\n".join(lines)

async def cmd_start(update, ctx):
    tid = update.effective_user.id
    user = get_user(tid)
    if user:
        ctx.user_data["user"] = user
        await update.message.reply_text(
            f"С возвращением, {user['name']}!\nРоль: {ROLE_LABELS.get(user['role'], user['role'])}\n\nВыберите действие:",
            reply_markup=kb([("Начать смену", "start_shift"), ("Мой профиль", "go_profile"), ("Сменить роль", "change_role")], cols=1),
        )
        return PROFILE_MENU
    else:
        await update.message.reply_text(
            "Добро пожаловать в бот Сансара!\n\nВыберите своё имя:",
            reply_markup=name_kb(),
        )
        return REGISTER_NAME

async def on_register_name(update, ctx):
    name = update.message.text.strip()
    if name not in ALL_NAMES:
        await update.message.reply_text("Выберите имя из кнопок ниже.", reply_markup=name_kb())
        return REGISTER_NAME
    logger.info(f"[on_register_name] name={name} user={update.effective_user.id}")
    ctx.user_data["reg_name"] = name
    await update.message.reply_text(f"{name}\n\nТеперь выберите роль:", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text("Выберите роль:", reply_markup=kb([(v, f"reg_role_{k}") for k, v in ROLE_LABELS.items()], cols=1))
    return REGISTER_ROLE

async def on_register_role(update, ctx):
    q = update.callback_query
    await q.answer()
    role = q.data.replace("reg_role_", "")
    name = ctx.user_data.get("reg_name", "")
    tid = update.effective_user.id
    user = {"name": name, "role": role}
    set_user(tid, user)
    ctx.user_data["user"] = user
    await q.edit_message_text(
        f"Профиль создан!\n{name} — {ROLE_LABELS[role]}\n\nВыберите действие:",
        reply_markup=kb([("Начать смену", "start_shift"), ("Мой профиль", "go_profile")], cols=1),
    )
    return PROFILE_MENU

async def cmd_reset(update, ctx):
    tid = update.effective_user.id
    users = load_users()
    if str(tid) in users:
        del users[str(tid)]
        save_users(users)
    ctx.user_data.clear()
    await update.message.reply_text("Профиль сброшен. Выберите своё имя:", reply_markup=name_kb())
    return REGISTER_NAME

async def on_profile_menu(update, ctx):
    q = update.callback_query
    await q.answer()
    tid = update.effective_user.id
    user = ctx.user_data.get("user") or get_user(tid)

    if q.data == "go_profile":
        await q.edit_message_text(
            f"Профиль\nИмя: {user['name']}\nРоль: {ROLE_LABELS.get(user['role'], user['role'])}",
            reply_markup=kb([("Начать смену", "start_shift"), ("Сменить роль", "change_role"), ("Сменить имя", "change_name")], cols=1),
        )
        return PROFILE_MENU

    if q.data == "change_role":
        await q.edit_message_text("Выберите новую роль:", reply_markup=kb([(v, f"setrole_{k}") for k, v in ROLE_LABELS.items()], cols=1))
        return PROFILE_MENU

    if q.data.startswith("setrole_"):
        role = q.data.replace("setrole_", "")
        user["role"] = role
        set_user(tid, user)
        ctx.user_data["user"] = user
        await q.edit_message_text(f"Роль изменена: {ROLE_LABELS[role]}", reply_markup=kb([("Начать смену", "start_shift"), ("Мой профиль", "go_profile")], cols=1))
        return PROFILE_MENU

    if q.data == "change_name":
        await q.edit_message_text(f"Сейчас: {user['name']}\n\nВыберите новое имя:", reply_markup=kb([(n, f"newname_{n}") for n in ALL_NAMES], cols=2))
        return PROFILE_MENU

    if q.data.startswith("newname_"):
        new_name = q.data.replace("newname_", "")
        await q.edit_message_text(f"Подтвердить смену имени:\n{user['name']} -> {new_name}", reply_markup=kb([("Подтвердить", f"confirmname_{new_name}"), ("Отмена", "go_profile")], cols=1))
        return PROFILE_MENU

    if q.data.startswith("confirmname_"):
        new_name = q.data.replace("confirmname_", "")
        user["name"] = new_name
        set_user(tid, user)
        ctx.user_data["user"] = user
        await q.edit_message_text(f"Имя изменено: {new_name}", reply_markup=kb([("Начать смену", "start_shift")], cols=1))
        return PROFILE_MENU

    if q.data == "start_shift":
        ctx.user_data["user"] = user
        await q.edit_message_text("Выберите филиал:", reply_markup=kb([("Ирий", "br_Ирий"), ("Правь", "br_Правь")]))
        return CHOOSE_BRANCH

    return PROFILE_MENU

async def on_branch(update, ctx):
    q = update.callback_query
    await q.answer()
    user = ctx.user_data.get("user") or get_user(update.effective_user.id)
    branch = q.data.replace("br_", "")
    reset_sess(ctx, user)
    s = sess(ctx)
    s["branch"] = branch
    s["start_time"] = datetime.now().strftime("%H:%M")
    await q.edit_message_text(
        f"{user['name']} / {ROLE_SHORT.get(user['role'], user['role'])} / {branch} / {s['date']}\n\nСмена открыта!",
        reply_markup=main_kb(user["role"]),
    )
    return MAIN_MENU

async def on_location(update, ctx):
    loc = update.message.location
    s = sess(ctx)
    branch = s.get("branch", "")
    target = BRANCHES_GEO.get(branch)
    if not target:
        await update.message.reply_text("Филиал не найден. /start", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    dist = haversine(loc.latitude, loc.longitude, target[0], target[1])
    if dist > GEO_RADIUS_WARN:
        await update.message.reply_text(f"Слишком далеко от {branch} ({int(dist)} м).", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    s["start_time"] = datetime.now().strftime("%H:%M")
    await update.message.reply_text(f"OK ({int(dist)} м). Смена открыта в {s['start_time']}", reply_markup=ReplyKeyboardRemove())
    await ctx.bot.send_message(update.effective_chat.id, f"{s['name']} / {s['branch']} / {s['date']}\nСмена открыта!", reply_markup=main_kb(s["role"]))
    return MAIN_MENU

def _item_buttons(items, prefix, s):
    bucket = s["ставки"] if prefix == "r" else s["процедуры"]
    btns = []
    for i, (name, price) in enumerate(items):
        qty = bucket.get(name, 0)
        mark = "[v] " if qty else ""
        qty_str = f" [{_qty_str(qty)}]" if qty else ""
        btns.append((f"{mark}{name} +{price}{qty_str}", f"{prefix}_{i}"))
    return btns

async def on_main(update, ctx):
    q = update.callback_query
    await q.answer()
    s = sess(ctx)

    if q.data == "show":
        await q.edit_message_text(fmt(s), reply_markup=main_kb(s["role"]))
        return MAIN_MENU

    if q.data == "profile":
        user = ctx.user_data.get("user") or get_user(update.effective_user.id)
        await q.edit_message_text(f"{user['name']}\nРоль: {ROLE_LABELS.get(user['role'], user['role'])}", reply_markup=kb([("Сменить роль", "change_role"), ("Назад", "back_main")], cols=1))
        return MAIN_MENU

    if q.data == "change_role":
        await q.edit_message_text("Выберите новую роль:", reply_markup=kb([(v, f"setrole_main_{k}") for k, v in ROLE_LABELS.items()], cols=1))
        return MAIN_MENU

    if q.data.startswith("setrole_main_"):
        role = q.data.replace("setrole_main_", "")
        tid = update.effective_user.id
        user = ctx.user_data.get("user") or get_user(tid)
        user["role"] = role
        set_user(tid, user)
        ctx.user_data["user"] = user
        s["role"] = role
        await q.edit_message_text(f"Роль изменена: {ROLE_LABELS[role]}", reply_markup=main_kb(role))
        return MAIN_MENU

    if q.data == "back_main":
        await q.edit_message_text(f"{s['name']} / {s['branch']} / {s['date']}", reply_markup=main_kb(s["role"]))
        return MAIN_MENU

    if q.data == "close":
        await q.edit_message_text(fmt(s, final=True) + "\n\nПодтвердить закрытие смены?", reply_markup=kb([("Да, закрыть", "do_close"), ("Назад", "back_main")]))
        return CONFIRM_CLOSE

    cat = CATALOGUE.get(s["role"], {}).get(s["branch"], {})
    if q.data == "add_rate":
        items = cat.get("ставки", [])
        prefix = "r"
        title = "Ставки / подготовка:"
    else:
        items = cat.get("процедуры", [])
        prefix = "p"
        title = "Процедуры:"
    ctx.user_data["cur_items"] = items
    ctx.user_data["cur_prefix"] = prefix
    btns = _item_buttons(items, prefix, s) + [("Назад", "back")]
    await q.edit_message_text(title, reply_markup=kb(btns, cols=1))
    return ADD_ITEM

async def on_add(update, ctx):
    q = update.callback_query
    await q.answer()
    s = sess(ctx)
    if q.data == "back":
        await q.edit_message_text(f"{s['name']} / {s['branch']} / {s['date']}", reply_markup=main_kb(s["role"]))
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
        f"{name} — {price} руб{cur_str}\n\nВыберите количество:",
        reply_markup=kb([("+1", "qty_plus1"), ("+0.5", "qty_plus05"), ("-1", "qty_minus1"), ("-0.5", "qty_minus05"), ("Назад к списку", "qty_back")], cols=2),
    )
    return ADD_QTY

async def on_qty(update, ctx):
    q = update.callback_query
    await q.answer()
    s = sess(ctx)
    if q.data == "qty_back":
        items = ctx.user_data["cur_items"]
        prefix = ctx.user_data["cur_prefix"]
        btns = _item_buttons(items, prefix, s) + [("Назад", "back")]
        title = "Ставки / подготовка:" if prefix == "r" else "Процедуры:"
        await q.edit_message_text(title, reply_markup=kb(btns, cols=1))
        return ADD_ITEM
    name = ctx.user_data["cur_name"]
    price = ctx.user_data["cur_price"]
    prefix = ctx.user_data["cur_prefix"]
    bucket = s["ставки"] if prefix == "r" else s["процедуры"]
    delta_map = {"qty_plus1": 1.0, "qty_plus05": 0.5, "qty_minus1": -1.0, "qty_minus05": -0.5}
    delta = delta_map.get(q.data, 0)
    new_qty = max(0, bucket.get(name, 0) + delta)
    if new_qty == 0:
        bucket.pop(name, None)
    else:
        bucket[name] = new_qty
    amt = price * new_qty
    await q.edit_message_text(
        f"{name}\nКоличество: {_qty_str(new_qty)} -> {int(amt)} руб\n\nВыберите количество:",
        reply_markup=kb([("+1", "qty_plus1"), ("+0.5", "qty_plus05"), ("-1", "qty_minus1"), ("-0.5", "qty_minus05"), ("Назад к списку", "qty_back")], cols=2),
    )
    return ADD_QTY

async def on_confirm(update, ctx):
    q = update.callback_query
    await q.answer()
    s = sess(ctx)
    if q.data == "back_main":
        await q.edit_message_text(f"{s['name']} / {s['branch']} / {s['date']}", reply_markup=main_kb(s["role"]))
        return MAIN_MENU
    summary = fmt(s, final=True)
    end_time = datetime.now().strftime("%H:%M")
    summary += f"\nКонец смены: {end_time}"
    await q.edit_message_text(summary + "\n\nСмена закрыта. Спасибо!")
    write_shift(s)
    if ADMIN_CHAT_ID:
        await ctx.bot.send_message(ADMIN_CHAT_ID, summary)
    user = ctx.user_data.get("user") or get_user(update.effective_user.id)
    ctx.user_data.clear()
    ctx.user_data["user"] = user
    await ctx.bot.send_message(update.effective_chat.id, "Начать новую смену?", reply_markup=kb([("Новая смена", "new_shift")]))
    return ConversationHandler.END

async def on_new_shift(update, ctx):
    q = update.callback_query
    await q.answer()
    user = ctx.user_data.get("user") or get_user(update.effective_user.id)
    if not user:
        await q.edit_message_text("Выберите своё имя:", reply_markup=kb([(n, f"reg_name_{n}") for n in ALL_NAMES], cols=2))
        return REGISTER_NAME
    await q.edit_message_text(f"{user['name']}, выберите филиал:", reply_markup=kb([("Ирий", "br_Ирий"), ("Правь", "br_Правь")]))
    return CHOOSE_BRANCH

IMPORT_FORMULA = f'=IMPORTRANGE("{SPREADSHEET_ID}";"Журнал!A:M")'
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

async def cmd_setup_rv(update, ctx):
    await update.message.reply_text("Настраиваю РВ файлы...")
    creds = _get_creds()
    if creds is None:
        await update.message.reply_text("Нет credentials Google.")
        return
    gc = gspread.authorize(creds)
    headers = ["Дата","Имя","Роль","Филиал","Тип","Позиция","Кол-во","Цена","Сумма","Период","День","Месяц","Год"]
    results = []
    for name, sid in RV_FILES.items():
        try:
            sh = gc.open_by_key(sid)
            existing = [ws.title for ws in sh.worksheets()]
            ws = sh.worksheet("Журнал") if "Журнал" in existing else sh.add_worksheet(title="Журнал", rows=2000, cols=13, index=0)
            ws.update("A1", [headers], value_input_option="USER_ENTERED")
            ws.update("A2", [[IMPORT_FORMULA]], value_input_option="USER_ENTERED")
            results.append(f"OK {name}")
        except Exception as e:
            results.append(f"ERR {name}: {e}")
    await update.message.reply_text("Результат:\n" + "\n".join(results))

def main():
    app = Application.builder().token(TOKEN).build()
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("reset", cmd_reset),
            CallbackQueryHandler(on_new_shift, pattern="^new_shift$"),
        ],
        states={
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_register_name)],
            REGISTER_ROLE: [CallbackQueryHandler(on_register_role, pattern="^reg_role_")],
            PROFILE_MENU: [CallbackQueryHandler(on_profile_menu, pattern="^(go_profile|change_role|setrole_.+|change_name|newname_.+|confirmname_.+|start_shift)$")],
            CHOOSE_BRANCH: [CallbackQueryHandler(on_branch, pattern="^br_")],
            LOCATION_CHECK: [MessageHandler(filters.LOCATION, on_location)],
            MAIN_MENU: [CallbackQueryHandler(on_main, pattern="^(show|close|add_rate|add_proc|profile|change_role|setrole_main_.+|back_main)$")],
            ADD_ITEM: [CallbackQueryHandler(on_add, pattern="^(r_\\d+|p_\\d+|back)$")],
            ADD_QTY: [CallbackQueryHandler(on_qty, pattern="^qty_")],
            CONFIRM_CLOSE: [CallbackQueryHandler(on_confirm, pattern="^(do_close|back_main)$")],
        },
        fallbacks=[CommandHandler("start", cmd_start), CommandHandler("reset", cmd_reset)],
        per_user=True,
        per_chat=True,
    )
    async def err(update, ctx):
        logger.error(f"[ERROR] {ctx.error}", exc_info=ctx.error)
    app.add_handler(conv)
    app.add_handler(CommandHandler("setup_rv", cmd_setup_rv))
    app.add_error_handler(err)
    logger.info("Бот Сансара v2 запущен")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
