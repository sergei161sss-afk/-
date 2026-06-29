#!/usr/bin/env python3
"""
Сансара — бот учёта смен v3
Без ConversationHandler — единый on_callback для всех кнопок.
После закрытия смены: запись в Журнал + сводка администратору.
"""
import os, json, math, logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN          = "8960632739:AAFnJBzn-89ctfWOiImkk24bbjpTWZqGZPk"
SPREADSHEET_ID = "1YLGV-Lprd5HZ7wwph728zPgISaVjPflhjlleZbV2Sco"
SCOPES         = ["https://www.googleapis.com/auth/spreadsheets"]
ADMIN_CHAT_ID  = int(os.getenv("ADMIN_CHAT_ID", "0")) or None

# ─── Google Sheets ────────────────────────────────────────────────────────────

def _get_creds():
    if os.path.exists("credentials.json"):
        return Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return None

def write_shift(s: dict):
    try:
        creds = _get_creds()
        if creds is None:
            logger.warning("Google Sheets не подключены — пропускаем запись.")
            return
        gc = gspread.authorize(creds)
        ws = gc.open_by_key(SPREADSHEET_ID).worksheet("Журнал")
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
        if s.get("процедуры"):
            procs_dict = dict(cat.get("процедуры", []))
            for pos, qty in s["процедуры"].items():
                price = procs_dict.get(pos, 0)
                rows.append([date_str, s["name"], s["role"], s["branch"], "процедура",
                             pos, qty, price, price * qty, period, day, month, year])
        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            logger.info(f"Sheets: {len(rows)} строк для {s['name']}")
    except Exception as e:
        logger.error(f"Ошибка записи в Sheets: {e}")

# ─── Данные ───────────────────────────────────────────────────────────────────

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

# ─── Users ────────────────────────────────────────────────────────────────────

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

# ─── Keyboards ────────────────────────────────────────────────────────────────

def kb(buttons: list, cols: int = 2) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(buttons), cols):
        rows.append([InlineKeyboardButton(t, callback_data=d) for t, d in buttons[i:i+cols]])
    return InlineKeyboardMarkup(rows)

def names_kb() -> InlineKeyboardMarkup:
    return kb([(n, f"rn_{n}") for n in ALL_NAMES], cols=3)

def roles_kb() -> InlineKeyboardMarkup:
    return kb([(v, f"rr_{k}") for k, v in ROLE_LABELS.items()], cols=1)

def branch_kb() -> InlineKeyboardMarkup:
    return kb([("Ирий", "br_Ирий"), ("Правь", "br_Правь")])

def main_kb(role: str) -> InlineKeyboardMarkup:
    btns = [("+ Ставки / подготовка", "add_rate")]
    if role in ROLES_WITH_PROCS:
        btns.append(("+ Процедуры", "add_proc"))
    btns += [("Мой итог", "show"), ("Профиль", "profile"), ("Закрыть смену", "close")]
    return kb(btns, cols=2)

# ─── Session ──────────────────────────────────────────────────────────────────

def new_session(user: dict) -> dict:
    return {
        "role": user["role"], "name": user["name"], "branch": None,
        "date": datetime.now().strftime("%d.%m.%Y"),
        "start_time": datetime.now().strftime("%H:%M"),
        "ставки": {}, "процедуры": {},
    }

def sess(ctx) -> dict:
    if "s" not in ctx.user_data:
        ctx.user_data["s"] = {}
    return ctx.user_data["s"]

def _qty_str(q: float) -> str:
    return str(int(q)) if q == int(q) else str(q)

def fmt(s: dict, final: bool = False) -> str:
    cat = CATALOGUE.get(s.get("role", ""), {}).get(s.get("branch", ""), {})
    lines = [
        "ИТОГ СМЕНЫ" if final else "Текущий итог",
        f"{s.get('name', '')} ({ROLE_SHORT.get(s.get('role', ''), '')})",
        f"{s.get('branch', '')} | {s.get('date', '')} {s.get('start_time', '')}",
        "-" * 28,
    ]
    total = 0.0
    rates_dict = dict(cat.get("ставки", []))
    if s.get("ставки"):
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

def _item_buttons(items, prefix, s):
    bucket = s["ставки"] if prefix == "r" else s["процедуры"]
    btns = []
    for i, (name, price) in enumerate(items):
        qty = bucket.get(name, 0)
        mark = "[v] " if qty else ""
        qty_str = f" [{_qty_str(qty)}]" if qty else ""
        btns.append((f"{mark}{name} +{price}{qty_str}", f"{prefix}_{i}"))
    return btns

# ─── Handlers ─────────────────────────────────────────────────────────────────

async def cmd_start(update, ctx):
    tid = update.effective_user.id
    logger.info(f"[/start] user={tid} chat={update.effective_chat.id}")
    user = get_user(tid)
    if user:
        ctx.user_data["user"] = user
        await update.message.reply_text(
            f"С возвращением, {user['name']}!\nРоль: {ROLE_LABELS.get(user['role'], user['role'])}\n\nВыберите действие:",
            reply_markup=kb([("Начать смену", "start_shift"), ("Мой профиль", "go_profile")], cols=1),
        )
    else:
        await update.message.reply_text("Добро пожаловать в бот Сансара!\n\nВыберите своё имя:", reply_markup=names_kb())

async def cmd_reset(update, ctx):
    tid = update.effective_user.id
    u = load_users()
    u.pop(str(tid), None)
    save_users(u)
    ctx.user_data.clear()
    logger.info(f"[/reset] user={tid}")
    await update.message.reply_text("Профиль сброшен. Выберите своё имя:", reply_markup=names_kb())

async def cmd_myid(update, ctx):
    await update.message.reply_text(f"Ваш chat ID: `{update.effective_chat.id}`", parse_mode="Markdown")

async def on_callback(update, ctx):
    q = update.callback_query
    await q.answer()
    data = q.data
    tid = update.effective_user.id
    logger.info(f"[cb] user={tid} data={data}")

    # ── Регистрация: выбор имени ──
    if data.startswith("rn_"):
        name = data[3:]
        ctx.user_data["reg_name"] = name
        await q.edit_message_text(f"Имя: {name}\n\nВыберите роль:", reply_markup=roles_kb())
        return

    # ── Регистрация: выбор роли ──
    if data.startswith("rr_"):
        role = data[3:]
        name = ctx.user_data.get("reg_name") or ""
        if not name:
            await q.edit_message_text("Выберите своё имя:", reply_markup=names_kb())
            return
        user = {"name": name, "role": role}
        set_user(tid, user)
        ctx.user_data["user"] = user
        await q.edit_message_text(
            f"Профиль создан!\n{name} — {ROLE_LABELS[role]}\n\nВыберите действие:",
            reply_markup=kb([("Начать смену", "start_shift"), ("Мой профиль", "go_profile")], cols=1),
        )
        return

    # ── Меню профиля ──
    if data == "go_profile":
        user = ctx.user_data.get("user") or get_user(tid)
        await q.edit_message_text(
            f"Профиль\nИмя: {user['name']}\nРоль: {ROLE_LABELS.get(user['role'], user['role'])}",
            reply_markup=kb([("Начать смену", "start_shift"), ("Сменить роль", "change_role"), ("Сменить имя", "change_name")], cols=1),
        )
        return

    if data == "change_name":
        await q.edit_message_text("Выберите новое имя:", reply_markup=names_kb())
        ctx.user_data["changing_name"] = True
        return

    if data.startswith("rn_") and ctx.user_data.get("changing_name"):
        name = data[3:]
        user = ctx.user_data.get("user") or get_user(tid)
        user["name"] = name
        set_user(tid, user)
        ctx.user_data["user"] = user
        ctx.user_data.pop("changing_name", None)
        await q.edit_message_text(f"Имя изменено: {name}", reply_markup=kb([("Начать смену", "start_shift")], cols=1))
        return

    if data == "change_role":
        await q.edit_message_text("Выберите новую роль:", reply_markup=roles_kb())
        ctx.user_data["changing_role"] = True
        return

    if data.startswith("rr_") and ctx.user_data.get("changing_role"):
        role = data[3:]
        user = ctx.user_data.get("user") or get_user(tid)
        user["role"] = role
        set_user(tid, user)
        ctx.user_data["user"] = user
        ctx.user_data.pop("changing_role", None)
        await q.edit_message_text(f"Роль изменена: {ROLE_LABELS[role]}", reply_markup=kb([("Начать смену", "start_shift"), ("Мой профиль", "go_profile")], cols=1))
        return

    # ── Начало смены ──
    if data == "start_shift":
        user = ctx.user_data.get("user") or get_user(tid)
        if not user:
            await q.edit_message_text("Выберите своё имя:", reply_markup=names_kb())
            return
        ctx.user_data["user"] = user
        await q.edit_message_text("Выберите филиал:", reply_markup=branch_kb())
        return

    if data.startswith("br_"):
        branch = data[3:]
        user = ctx.user_data.get("user") or get_user(tid)
        s = new_session(user)
        s["branch"] = branch
        ctx.user_data["s"] = s
        await q.edit_message_text(
            f"{user['name']} / {ROLE_SHORT.get(user['role'], user['role'])} / {branch} / {s['date']}\n\nСмена открыта!",
            reply_markup=main_kb(user["role"]),
        )
        return

    # ── Основное меню смены ──
    s = sess(ctx)

    if data == "show":
        await q.edit_message_text(fmt(s), reply_markup=main_kb(s.get("role", "")))
        return

    if data == "profile":
        user = ctx.user_data.get("user") or get_user(tid)
        await q.edit_message_text(
            f"{user['name']}\nРоль: {ROLE_LABELS.get(user['role'], user['role'])}",
            reply_markup=kb([("Сменить роль в смене", "setrole_shift"), ("Назад", "back_main")], cols=1),
        )
        return

    if data == "setrole_shift":
        await q.edit_message_text("Выберите новую роль:", reply_markup=kb([(v, f"sr_{k}") for k, v in ROLE_LABELS.items()], cols=1))
        return

    if data.startswith("sr_"):
        role = data[3:]
        user = ctx.user_data.get("user") or get_user(tid)
        user["role"] = role
        set_user(tid, user)
        ctx.user_data["user"] = user
        s["role"] = role
        await q.edit_message_text(f"Роль изменена: {ROLE_LABELS[role]}", reply_markup=main_kb(role))
        return

    if data == "back_main":
        await q.edit_message_text(
            f"{s.get('name', '')} / {s.get('branch', '')} / {s.get('date', '')}",
            reply_markup=main_kb(s.get("role", "")),
        )
        return

    if data in ("add_rate", "add_proc"):
        cat = CATALOGUE.get(s.get("role", ""), {}).get(s.get("branch", ""), {})
        if data == "add_rate":
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
        return

    if data == "back":
        await q.edit_message_text(
            f"{s.get('name', '')} / {s.get('branch', '')} / {s.get('date', '')}",
            reply_markup=main_kb(s.get("role", "")),
        )
        return

    if data.startswith("r_") or data.startswith("p_"):
        prefix, idx_str = data.split("_", 1)
        idx = int(idx_str)
        items = ctx.user_data.get("cur_items", [])
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
        return

    if data.startswith("qty_"):
        if data == "qty_back":
            items = ctx.user_data.get("cur_items", [])
            prefix = ctx.user_data.get("cur_prefix", "r")
            btns = _item_buttons(items, prefix, s) + [("Назад", "back")]
            title = "Ставки / подготовка:" if prefix == "r" else "Процедуры:"
            await q.edit_message_text(title, reply_markup=kb(btns, cols=1))
            return
        name = ctx.user_data.get("cur_name", "")
        price = ctx.user_data.get("cur_price", 0)
        prefix = ctx.user_data.get("cur_prefix", "r")
        bucket = s["ставки"] if prefix == "r" else s["процедуры"]
        delta_map = {"qty_plus1": 1.0, "qty_plus05": 0.5, "qty_minus1": -1.0, "qty_minus05": -0.5}
        delta = delta_map.get(data, 0)
        new_qty = max(0, bucket.get(name, 0) + delta)
        if new_qty == 0:
            bucket.pop(name, None)
        else:
            bucket[name] = new_qty
        amt = price * new_qty
        await q.edit_message_text(
            f"{name}\nКоличество: {_qty_str(new_qty)} → {int(amt)} руб\n\nВыберите количество:",
            reply_markup=kb([("+1", "qty_plus1"), ("+0.5", "qty_plus05"), ("-1", "qty_minus1"), ("-0.5", "qty_minus05"), ("Назад к списку", "qty_back")], cols=2),
        )
        return

    # ── Закрытие смены ──
    if data == "close":
        await q.edit_message_text(
            fmt(s, final=True) + "\n\nПодтвердить закрытие смены?",
            reply_markup=kb([("Да, закрыть", "do_close"), ("Назад", "back_main")]),
        )
        return

    if data == "do_close":
        end_time = datetime.now().strftime("%H:%M")
        s["end_time"] = end_time
        summary = fmt(s, final=True) + f"\nКонец смены: {end_time}"
        await q.edit_message_text(summary + "\n\nСмена закрыта. Спасибо!")
        write_shift(s)
        if ADMIN_CHAT_ID:
            try:
                await ctx.bot.send_message(ADMIN_CHAT_ID, summary)
            except Exception as e:
                logger.error(f"Не удалось отправить сводку админу: {e}")
        user = ctx.user_data.get("user") or get_user(tid)
        ctx.user_data.clear()
        ctx.user_data["user"] = user
        await ctx.bot.send_message(
            update.effective_chat.id,
            "Начать новую смену?",
            reply_markup=kb([("Новая смена", "start_shift")]),
        )
        return

    logger.warning(f"[cb] unhandled data={data}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CallbackQueryHandler(on_callback))
    async def err(update, ctx):
        logger.error(f"[ERROR] {ctx.error}", exc_info=ctx.error)
    app.add_error_handler(err)
    logger.info("Бот Сансара v3 запущен")
    webhook_url = os.getenv("WEBHOOK_URL", "")
    port = int(os.getenv("PORT", "8080"))
    if webhook_url:
        logger.info(f"Webhook mode: {webhook_url}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )
    else:
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
