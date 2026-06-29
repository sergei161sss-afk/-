#!/usr/bin/env python3
"""
Сансара — бот учёта смен v4
- Без ConversationHandler (единый on_callback)
- Пользователи синхронизируются в Google Sheets «Пользователи»
  → данные не теряются при редеплое
- После закрытия смены: запись в «Журнал» + сводка администратору
- Webhook-режим на Railway, polling — локально
"""
import os, json, math, logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN          = "8960632739:AAFnJBzn-89ctfWOiImkk24bbjpTWZqGZPk"
SPREADSHEET_ID = "1YLGV-Lprd5HZ7wwph728zPgISaVjPflhjlleZbV2Sco"
SCOPES         = ["https://www.googleapis.com/auth/spreadsheets"]
ADMIN_CHAT_ID  = int(os.getenv("ADMIN_CHAT_ID", "0")) or None
USERS_FILE     = "users.json"

# ─── Google Sheets ────────────────────────────────────────────────────────────

def _get_creds():
    if os.path.exists("credentials.json"):
        return Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return None

def _open_sheet():
    creds = _get_creds()
    if creds is None:
        return None
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)

def _users_ws(sh):
    """Получить/создать лист «Пользователи»."""
    titles = [w.title for w in sh.worksheets()]
    if "Пользователи" not in titles:
        ws = sh.add_worksheet("Пользователи", rows=200, cols=3)
        ws.update("A1:C1", [["telegram_id", "name", "role"]])
        return ws
    return sh.worksheet("Пользователи")

# ─── Хранение пользователей: local JSON + Sheets ──────────────────────────────

def _load_local() -> dict:
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_local(users: dict):
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Не удалось сохранить users.json: {e}")

def sync_users_from_sheets():
    """Загрузить пользователей из Sheets при старте (если local пуст)."""
    local = _load_local()
    if local:
        logger.info(f"Локально {len(local)} пользователей, Sheets не грузим.")
        return
    try:
        sh = _open_sheet()
        if sh is None:
            return
        ws = _users_ws(sh)
        records = ws.get_all_records()
        users = {}
        for r in records:
            tid = str(r.get("telegram_id", "")).strip()
            name = str(r.get("name", "")).strip()
            role = str(r.get("role", "")).strip()
            if tid and name and role:
                users[tid] = {"name": name, "role": role}
        if users:
            _save_local(users)
            logger.info(f"Загружено {len(users)} пользователей из Sheets.")
    except Exception as e:
        logger.error(f"sync_users_from_sheets: {e}")

def get_user(tid: int) -> dict | None:
    return _load_local().get(str(tid))

def set_user(tid: int, data: dict):
    users = _load_local()
    users[str(tid)] = data
    _save_local(users)
    # Асинхронно дублируем в Sheets (best-effort)
    try:
        sh = _open_sheet()
        if sh is None:
            return
        ws = _users_ws(sh)
        values = ws.get_all_values()
        for i, row in enumerate(values):
            if row and str(row[0]).strip() == str(tid):
                ws.update(f"A{i+1}:C{i+1}", [[str(tid), data["name"], data["role"]]])
                return
        ws.append_row([str(tid), data["name"], data["role"]])
    except Exception as e:
        logger.error(f"set_user sheets sync: {e}")

def write_shift(s: dict):
    """Записать смену в лист «Журнал»."""
    try:
        sh = _open_sheet()
        if sh is None:
            logger.warning("Google Sheets не подключены — пропускаем запись.")
            return
        ws = sh.worksheet("Журнал")
        date_str = s["date"]
        parts = date_str.split(".")
        day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
        period = "1-15" if day <= 15 else "16-31"
        cat = CATALOGUE.get(s["role"], {}).get(s["branch"], {})
        rows = []
        rates_dict = dict(cat.get("ставки", []))
        for pos, qty in s.get("ставки", {}).items():
            price = rates_dict.get(pos, 0)
            rows.append([date_str, s["name"], s["role"], s["branch"], "ставка",
                         pos, qty, price, round(price * qty, 2), period, day, month, year])
        procs_dict = dict(cat.get("процедуры", []))
        for pos, qty in s.get("процедуры", {}).items():
            price = procs_dict.get(pos, 0)
            rows.append([date_str, s["name"], s["role"], s["branch"], "процедура",
                         pos, qty, price, round(price * qty, 2), period, day, month, year])
        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            logger.info(f"Sheets «Журнал»: {len(rows)} строк для {s['name']}")
        else:
            logger.warning(f"Смена {s['name']} пустая — в Sheets не записываем.")
    except Exception as e:
        logger.error(f"write_shift: {e}")

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

# ─── Вспомогательные функции ──────────────────────────────────────────────────

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
    return kb([("🏠 Ирий", "br_Ирий"), ("🏠 Правь", "br_Правь")])

def main_kb(role: str) -> InlineKeyboardMarkup:
    btns = [("➕ Ставки / подготовка", "add_rate")]
    if role in ROLES_WITH_PROCS:
        btns.append(("➕ Процедуры", "add_proc"))
    btns += [("📊 Мой итог", "show"), ("👤 Профиль", "profile"), ("🔒 Закрыть смену", "close")]
    return kb(btns, cols=2)

def profile_kb() -> InlineKeyboardMarkup:
    return kb([
        ("🔄 Сменить роль", "change_role"),
        ("✏️ Сменить имя", "change_name"),
        ("🚀 Начать смену", "start_shift"),
    ], cols=1)

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
        "📋 ИТОГ СМЕНЫ" if final else "📊 Текущий итог",
        f"👤 {s.get('name', '—')} ({ROLE_SHORT.get(s.get('role', ''), '—')})",
        f"📍 {s.get('branch', '—')} | {s.get('date', '—')} {s.get('start_time', '')}",
        "─" * 30,
    ]
    total = 0.0
    rates_dict = dict(cat.get("ставки", []))
    if s.get("ставки"):
        lines.append("Ставки / подготовка:")
        for name, qty in s["ставки"].items():
            price = rates_dict.get(name, 0)
            amt = price * qty
            total += amt
            lines.append(f"  {name}: {_qty_str(qty)} × {price} = {int(amt)} руб")
    procs_dict = dict(cat.get("процедуры", []))
    if s.get("процедуры"):
        lines.append("Процедуры:")
        for name, qty in s["процедуры"].items():
            price = procs_dict.get(name, 0)
            amt = price * qty
            total += amt
            lines.append(f"  {name}: {_qty_str(qty)} × {price} = {int(amt)} руб")
    lines += ["─" * 30, f"💰 ИТОГО: {int(total)} руб"]
    return "\n".join(lines)

def _item_buttons(items, prefix, s):
    bucket = s.get("ставки", {}) if prefix == "r" else s.get("процедуры", {})
    btns = []
    for i, (name, price) in enumerate(items):
        qty = bucket.get(name, 0)
        mark = "✅ " if qty else ""
        qty_str = f" [{_qty_str(qty)}]" if qty else ""
        btns.append((f"{mark}{name} +{price}{qty_str}", f"{prefix}_{i}"))
    return btns

# ─── Команды ──────────────────────────────────────────────────────────────────

async def cmd_start(update, ctx):
    tid = update.effective_user.id
    logger.info(f"[/start] user={tid}")
    user = ctx.user_data.get("user") or get_user(tid)
    if user:
        ctx.user_data["user"] = user
        await update.message.reply_text(
            f"👋 С возвращением, {user['name']}!\n"
            f"Роль: {ROLE_LABELS.get(user['role'], user['role'])}",
            reply_markup=kb([("🚀 Начать смену", "start_shift"), ("👤 Мой профиль", "go_profile")], cols=1),
        )
    else:
        await update.message.reply_text(
            "👋 Добро пожаловать в бот Сансара!\n\nВыберите своё имя:",
            reply_markup=names_kb(),
        )

async def cmd_reset(update, ctx):
    tid = update.effective_user.id
    logger.info(f"[/reset] user={tid}")
    users = _load_local()
    users.pop(str(tid), None)
    _save_local(users)
    # Удалить из Sheets тоже
    try:
        sh = _open_sheet()
        if sh:
            ws = _users_ws(sh)
            values = ws.get_all_values()
            for i, row in enumerate(values):
                if row and str(row[0]).strip() == str(tid):
                    ws.delete_rows(i + 1)
                    break
    except Exception as e:
        logger.error(f"cmd_reset sheets: {e}")
    ctx.user_data.clear()
    await update.message.reply_text(
        "🔄 Профиль сброшен.\n\nВыберите своё имя:", reply_markup=names_kb()
    )

async def cmd_myid(update, ctx):
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"Ваш Chat ID: `{cid}`\n\n"
        "Добавьте переменную `ADMIN_CHAT_ID={cid}` в Railway Variables "
        "чтобы получать сводки по сменам.",
        parse_mode="Markdown",
    )

# ─── Единый обработчик кнопок ─────────────────────────────────────────────────

async def on_callback(update, ctx):
    q = update.callback_query
    await q.answer()
    data = q.data
    tid = update.effective_user.id
    logger.info(f"[cb] user={tid} data={data}")

    try:
        await _handle(q, ctx, data, tid)
    except Exception as e:
        logger.error(f"[cb ERROR] user={tid} data={data} err={e}", exc_info=True)
        try:
            await q.edit_message_text(
                "⚠️ Произошла ошибка. Попробуйте /start",
                reply_markup=kb([("🔄 Начать заново", "restart")]),
            )
        except Exception:
            pass

async def _handle(q, ctx, data: str, tid: int):
    # ── restart ──
    if data == "restart":
        user = get_user(tid)
        if user:
            ctx.user_data["user"] = user
            await q.edit_message_text(
                f"👋 С возвращением, {user['name']}!",
                reply_markup=kb([("🚀 Начать смену", "start_shift"), ("👤 Мой профиль", "go_profile")], cols=1),
            )
        else:
            await q.edit_message_text("Выберите своё имя:", reply_markup=names_kb())
        return

    # ── Выбор имени (регистрация или смена имени) ──
    if data.startswith("rn_"):
        name = data[3:]
        if ctx.user_data.get("changing_name"):
            # Смена имени у зарегистрированного пользователя
            ctx.user_data.pop("changing_name", None)
            user = ctx.user_data.get("user") or get_user(tid) or {}
            user["name"] = name
            set_user(tid, user)
            ctx.user_data["user"] = user
            await q.edit_message_text(
                f"✅ Имя изменено: {name}\nРоль: {ROLE_LABELS.get(user.get('role', ''), '—')}",
                reply_markup=profile_kb(),
            )
        else:
            # Первичная регистрация
            ctx.user_data["reg_name"] = name
            await q.edit_message_text(
                f"👤 Имя: {name}\n\nТеперь выберите роль:", reply_markup=roles_kb()
            )
        return

    # ── Выбор роли (регистрация или смена роли) ──
    if data.startswith("rr_"):
        role = data[3:]
        if role not in ROLE_LABELS:
            await q.edit_message_text("Выберите роль:", reply_markup=roles_kb())
            return
        if ctx.user_data.get("changing_role"):
            # Смена роли у зарегистрированного пользователя
            ctx.user_data.pop("changing_role", None)
            user = ctx.user_data.get("user") or get_user(tid) or {}
            user["role"] = role
            set_user(tid, user)
            ctx.user_data["user"] = user
            await q.edit_message_text(
                f"✅ Роль изменена: {ROLE_LABELS[role]}",
                reply_markup=profile_kb(),
            )
        else:
            # Первичная регистрация
            name = ctx.user_data.get("reg_name", "")
            if not name:
                await q.edit_message_text("Выберите своё имя:", reply_markup=names_kb())
                return
            user = {"name": name, "role": role}
            set_user(tid, user)
            ctx.user_data["user"] = user
            ctx.user_data.pop("reg_name", None)
            await q.edit_message_text(
                f"✅ Профиль создан!\n👤 {name}\n🎭 {ROLE_LABELS[role]}\n\nВыберите действие:",
                reply_markup=kb([("🚀 Начать смену", "start_shift"), ("👤 Мой профиль", "go_profile")], cols=1),
            )
        return

    # ── Профиль ──
    if data == "go_profile":
        user = ctx.user_data.get("user") or get_user(tid)
        if not user:
            await q.edit_message_text("Выберите своё имя:", reply_markup=names_kb())
            return
        ctx.user_data["user"] = user
        await q.edit_message_text(
            f"👤 Профиль\n\nИмя: {user['name']}\nРоль: {ROLE_LABELS.get(user['role'], user['role'])}",
            reply_markup=profile_kb(),
        )
        return

    if data == "change_name":
        ctx.user_data["changing_name"] = True
        ctx.user_data.pop("changing_role", None)
        await q.edit_message_text("Выберите новое имя:", reply_markup=names_kb())
        return

    if data == "change_role":
        ctx.user_data["changing_role"] = True
        ctx.user_data.pop("changing_name", None)
        await q.edit_message_text("Выберите новую роль:", reply_markup=roles_kb())
        return

    # ── Начало смены ──
    if data == "start_shift":
        ctx.user_data.pop("changing_name", None)
        ctx.user_data.pop("changing_role", None)
        user = ctx.user_data.get("user") or get_user(tid)
        if not user:
            await q.edit_message_text("Выберите своё имя:", reply_markup=names_kb())
            return
        ctx.user_data["user"] = user
        await q.edit_message_text(
            f"👤 {user['name']} | {ROLE_SHORT.get(user['role'], user['role'])}\n\nВыберите филиал:",
            reply_markup=branch_kb(),
        )
        return

    if data.startswith("br_"):
        branch = data[3:]
        user = ctx.user_data.get("user") or get_user(tid)
        if not user:
            await q.edit_message_text("Выберите своё имя:", reply_markup=names_kb())
            return
        ctx.user_data["user"] = user
        s = new_session(user)
        s["branch"] = branch
        ctx.user_data["s"] = s
        await q.edit_message_text(
            f"✅ Смена открыта!\n"
            f"👤 {user['name']} | {ROLE_SHORT.get(user['role'], '')} | {branch}\n"
            f"📅 {s['date']} {s['start_time']}",
            reply_markup=main_kb(user["role"]),
        )
        return

    # ── Меню смены ──
    s = sess(ctx)

    if data == "show":
        if not s.get("branch"):
            await q.edit_message_text("Смена не открыта. Нажмите /start", reply_markup=None)
            return
        await q.edit_message_text(fmt(s), reply_markup=main_kb(s["role"]))
        return

    if data == "profile":
        user = ctx.user_data.get("user") or get_user(tid)
        if not user:
            await q.edit_message_text("Выберите своё имя:", reply_markup=names_kb())
            return
        role_in_shift = s.get("role", user["role"])
        await q.edit_message_text(
            f"👤 {user['name']}\nРоль: {ROLE_LABELS.get(role_in_shift, role_in_shift)}",
            reply_markup=kb([
                ("🔄 Сменить роль в смене", "sr_shift"),
                ("◀️ Назад", "back_main"),
            ], cols=1),
        )
        return

    if data == "sr_shift":
        await q.edit_message_text(
            "Выберите новую роль для текущей смены:",
            reply_markup=kb([(v, f"srs_{k}") for k, v in ROLE_LABELS.items()], cols=1),
        )
        return

    if data.startswith("srs_"):
        role = data[4:]
        if role not in ROLE_LABELS:
            return
        user = ctx.user_data.get("user") or get_user(tid) or {}
        user["role"] = role
        set_user(tid, user)
        ctx.user_data["user"] = user
        s["role"] = role
        await q.edit_message_text(
            f"✅ Роль изменена: {ROLE_LABELS[role]}",
            reply_markup=main_kb(role),
        )
        return

    if data == "back_main":
        if not s.get("branch"):
            user = ctx.user_data.get("user") or get_user(tid)
            await q.edit_message_text(
                "Выберите действие:",
                reply_markup=kb([("🚀 Начать смену", "start_shift"), ("👤 Профиль", "go_profile")], cols=1),
            )
            return
        await q.edit_message_text(
            f"👤 {s.get('name', '')} | {s.get('branch', '')} | {s.get('date', '')}",
            reply_markup=main_kb(s.get("role", "")),
        )
        return

    if data in ("add_rate", "add_proc"):
        if not s.get("branch"):
            await q.edit_message_text("Смена не открыта. Нажмите /start")
            return
        cat = CATALOGUE.get(s.get("role", ""), {}).get(s.get("branch", ""), {})
        if data == "add_rate":
            items = cat.get("ставки", [])
            prefix = "r"
            title = "➕ Ставки / подготовка:"
        else:
            items = cat.get("процедуры", [])
            prefix = "p"
            title = "➕ Процедуры:"
        if not items:
            await q.edit_message_text("Нет позиций для этой роли/филиала.", reply_markup=kb([("◀️ Назад", "back_main")]))
            return
        ctx.user_data["cur_items"] = items
        ctx.user_data["cur_prefix"] = prefix
        btns = _item_buttons(items, prefix, s) + [("◀️ Назад", "back_main")]
        await q.edit_message_text(title, reply_markup=kb(btns, cols=1))
        return

    if data.startswith("r_") or data.startswith("p_"):
        parts = data.split("_", 1)
        prefix = parts[0]
        idx = int(parts[1])
        items = ctx.user_data.get("cur_items", [])
        if idx >= len(items):
            return
        name, price = items[idx]
        ctx.user_data["cur_name"] = name
        ctx.user_data["cur_price"] = price
        ctx.user_data["cur_prefix"] = prefix
        bucket = s.get("ставки", {}) if prefix == "r" else s.get("процедуры", {})
        cur_qty = bucket.get(name, 0)
        cur_str = f" (сейчас: {_qty_str(cur_qty)})" if cur_qty else ""
        await q.edit_message_text(
            f"📌 {name} — {price} руб{cur_str}\n\nВыберите количество:",
            reply_markup=kb([
                ("+1", "qty_p1"), ("+0.5", "qty_p05"),
                ("-1", "qty_m1"), ("-0.5", "qty_m05"),
                ("◀️ К списку", "qty_back"),
            ], cols=2),
        )
        return

    if data.startswith("qty_"):
        if data == "qty_back":
            items = ctx.user_data.get("cur_items", [])
            prefix = ctx.user_data.get("cur_prefix", "r")
            btns = _item_buttons(items, prefix, s) + [("◀️ Назад", "back_main")]
            title = "➕ Ставки / подготовка:" if prefix == "r" else "➕ Процедуры:"
            await q.edit_message_text(title, reply_markup=kb(btns, cols=1))
            return
        name = ctx.user_data.get("cur_name", "")
        price = ctx.user_data.get("cur_price", 0)
        prefix = ctx.user_data.get("cur_prefix", "r")
        if not name:
            await q.edit_message_text("◀️ Назад", reply_markup=kb([("◀️ Назад", "back_main")]))
            return
        bucket = s.get("ставки", {}) if prefix == "r" else s.get("процедуры", {})
        delta_map = {"qty_p1": 1.0, "qty_p05": 0.5, "qty_m1": -1.0, "qty_m05": -0.5}
        delta = delta_map.get(data, 0)
        new_qty = max(0, bucket.get(name, 0) + delta)
        if new_qty == 0:
            bucket.pop(name, None)
        else:
            bucket[name] = new_qty
        amt = price * new_qty
        await q.edit_message_text(
            f"📌 {name}\nКоличество: {_qty_str(new_qty)} → {int(amt)} руб\n\nВыберите количество:",
            reply_markup=kb([
                ("+1", "qty_p1"), ("+0.5", "qty_p05"),
                ("-1", "qty_m1"), ("-0.5", "qty_m05"),
                ("◀️ К списку", "qty_back"),
            ], cols=2),
        )
        return

    # ── Закрытие смены ──
    if data == "close":
        if not s.get("branch"):
            await q.edit_message_text("Смена не открыта. Нажмите /start")
            return
        total_items = len(s.get("ставки", {})) + len(s.get("процедуры", {}))
        warning = "" if total_items > 0 else "\n\n⚠️ Ставки/процедуры не добавлены!"
        await q.edit_message_text(
            fmt(s, final=True) + warning + "\n\nПодтвердить закрытие?",
            reply_markup=kb([("✅ Да, закрыть", "do_close"), ("◀️ Назад", "back_main")]),
        )
        return

    if data == "do_close":
        if not s.get("branch"):
            await q.edit_message_text("Смена не открыта.")
            return
        end_time = datetime.now().strftime("%H:%M")
        s["end_time"] = end_time
        summary = fmt(s, final=True) + f"\n⏱ Конец: {end_time}"
        await q.edit_message_text(summary + "\n\n✅ Смена закрыта. Спасибо!")
        # Записать в Sheets
        write_shift(s)
        # Уведомить администратора
        if ADMIN_CHAT_ID:
            try:
                await q.get_bot().send_message(ADMIN_CHAT_ID, summary)
            except Exception as e:
                logger.error(f"Не удалось отправить сводку: {e}")
        # Сбросить сессию
        user = ctx.user_data.get("user") or get_user(tid)
        ctx.user_data.clear()
        if user:
            ctx.user_data["user"] = user
        await q.get_bot().send_message(
            update.effective_chat.id if hasattr(q, '_effective_chat') else q.message.chat_id,
            "Хотите начать новую смену?",
            reply_markup=kb([("🚀 Новая смена", "start_shift"), ("👤 Профиль", "go_profile")], cols=1),
        )
        return

    logger.warning(f"[cb] необработанный data={data!r}")

# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    sync_users_from_sheets()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CallbackQueryHandler(on_callback))

    async def err_handler(update, ctx):
        logger.error(f"[GLOBAL ERROR] {ctx.error}", exc_info=ctx.error)
    app.add_error_handler(err_handler)

    webhook_url = os.getenv("WEBHOOK_URL", "")
    port = int(os.getenv("PORT", "8080"))

    logger.info("Бот Сансара v4 запускается...")
    if webhook_url:
        logger.info(f"Webhook: {webhook_url}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
