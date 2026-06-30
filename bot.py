#!/usr/bin/env python3
"""
Сансара — бот учёта смен v5.0
Новое в v5.0:
- Уведомление админу при открытии смены (🟢) и закрытии (🔴)
- Автоотчёт каждый день в 21:00 МСК (JobQueue)
- Контроль опозданий: проверка в 11:00 МСК
- /salary [MM.YYYY] — итоги начислений за месяц по каждому сотруднику
- /stats [MM.YYYY] — статистика по процедурам (топ по выручке)
- Fallback на Google Sheets если users.json потерян при рестарте
"""
import os, json, logging, datetime
import pytz

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
MSK            = datetime.timezone(datetime.timedelta(hours=3))
MSK_TZ         = pytz.timezone("Europe/Moscow")
ROLE_CODES     = {"м1", "мв", "м2", "техпер", "менеджер"}

# Активные смены: tid -> {name, role, branch, start_time, date}
ACTIVE_SHIFTS: dict = {}
# Кто уже завершил смену сегодня: date_str -> set(tid)
TODAY_WORKED: dict = {}

def now_msk() -> datetime.datetime:
    return datetime.datetime.now(MSK)

# ── Google Sheets ─────────────────────────────────────────────────────────────

def _get_creds():
    for fname in ("credentials.json", "credentials.json.json"):
        if os.path.exists(fname):
            return Credentials.from_service_account_file(fname, scopes=SCOPES)
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
    titles = [w.title for w in sh.worksheets()]
    if "Пользователи" not in titles:
        ws = sh.add_worksheet("Пользователи", rows=200, cols=3)
        ws.update([["telegram_id", "name", "role"]], "A1:C1")
        return ws
    return sh.worksheet("Пользователи")

# ── Хранение пользователей ────────────────────────────────────────────────────

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
    local = _load_local()
    if local:
        logger.info(f"Локально {len(local)} пользователей.")
        return
    try:
        sh = _open_sheet()
        if sh is None:
            return
        ws = _users_ws(sh)
        records = ws.get_all_records()
        users = {}
        for r in records:
            tid  = str(r.get("telegram_id", "")).strip()
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
    """Читает пользователя локально, при отсутствии — из Sheets (резервное копирование)."""
    local = _load_local()
    if str(tid) in local:
        return local[str(tid)]
    # Fallback: Sheets
    try:
        sh = _open_sheet()
        if sh is None:
            return None
        ws = _users_ws(sh)
        records = ws.get_all_records()
        for r in records:
            if str(r.get("telegram_id", "")).strip() == str(tid):
                user = {"name": str(r["name"]).strip(), "role": str(r["role"]).strip()}
                if user["name"] and user["role"]:
                    local[str(tid)] = user
                    _save_local(local)
                    logger.info(f"get_user: восстановлен {user['name']} из Sheets")
                    return user
    except Exception as e:
        logger.error(f"get_user sheets fallback: {e}")
    return None

def set_user(tid: int, data: dict):
    users = _load_local()
    users[str(tid)] = data
    _save_local(users)
    try:
        sh = _open_sheet()
        if sh is None:
            return
        ws = _users_ws(sh)
        values = ws.get_all_values()
        for i, row in enumerate(values):
            if row and str(row[0]).strip() == str(tid):
                ws.update([[str(tid), data["name"], data["role"]]], f"A{i+1}:C{i+1}")
                return
        ws.append_row([str(tid), data["name"], data["role"]])
    except Exception as e:
        logger.error(f"set_user sheets sync: {e}")

def write_shift(s: dict):
    """
    Журнал v4.2+:
    date|name|tg_id|role|branch|start_time|end_time|type|pos|qty|price|amount|period|day|month|year
    """
    try:
        sh = _open_sheet()
        if sh is None:
            logger.warning("Google Sheets не подключены.")
            return
        ws = sh.worksheet("Журнал")
        date_str   = s["date"]
        parts      = date_str.split(".")
        day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
        period     = "1-15" if day <= 15 else "16-31"
        start_time = s.get("start_time", "")
        end_time   = s.get("end_time", "")
        tg_id      = str(s.get("tid", ""))
        cat        = CATALOGUE.get(s["role"], {}).get(s["branch"], {})
        rows       = []
        rates_dict = dict(cat.get("ставки", []))
        for pos, qty in s.get("ставки", {}).items():
            price = rates_dict.get(pos, 0)
            rows.append([date_str, s["name"], tg_id, s["role"], s["branch"],
                         start_time, end_time, "ставка",
                         pos, qty, price, round(price * qty, 2),
                         period, day, month, year])
        procs_dict = dict(cat.get("процедуры", []))
        for pos, qty in s.get("процедуры", {}).items():
            price = procs_dict.get(pos, 0)
            rows.append([date_str, s["name"], tg_id, s["role"], s["branch"],
                         start_time, end_time, "процедура",
                         pos, qty, price, round(price * qty, 2),
                         period, day, month, year])
        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            logger.info(f"Журнал: {len(rows)} строк для {s['name']}")
    except Exception as e:
        logger.error(f"write_shift: {e}")

def _parse_row(row: list) -> dict | None:
    """
    Разбирает строку Журнала в любом из трёх форматов:
      v1 (старый): date|name|role|branch|type|pos|qty|price|...
      v2 (v4.1):   date|name|role|branch|start|end|type|pos|qty|price|...
      v3 (v4.2+):  date|name|tg_id|role|branch|start|end|type|pos|qty|price|amount|...
    """
    if len(row) < 8:
        return None

    name = row[1]

    if row[2] in ROLE_CODES:
        tg_id  = ""
        role   = row[2]
        branch = row[3]
        if row[4] in ("ставка", "процедура"):
            rtype = row[4]; pos = row[5]
            qi, pi = 6, 7
            start = end = ""
        else:
            start = row[4]; end = row[5] if len(row) > 5 else ""
            rtype = row[6] if len(row) > 6 else ""
            pos   = row[7] if len(row) > 7 else ""
            qi, pi = 8, 9
    else:
        tg_id  = row[2]
        role   = row[3] if len(row) > 3 else ""
        branch = row[4] if len(row) > 4 else ""
        start  = row[5] if len(row) > 5 else ""
        end    = row[6] if len(row) > 6 else ""
        rtype  = row[7] if len(row) > 7 else ""
        pos    = row[8] if len(row) > 8 else ""
        qi, pi = 9, 10

    try:
        qty   = float(str(row[qi]).replace(",", "."))
        price = float(str(row[pi]).replace(",", "."))
    except (ValueError, IndexError):
        return None

    return dict(name=name, tg_id=tg_id, role=role, branch=branch,
                start=start, end=end, rtype=rtype, pos=pos,
                qty=qty, price=price, amount=qty * price)

def get_report(date_str: str) -> str:
    try:
        sh = _open_sheet()
        if sh is None:
            return "❌ Google Sheets не подключены."
        ws = sh.worksheet("Журнал")
        all_rows = ws.get_all_values()
        if not all_rows:
            return f"📭 Нет данных за {date_str}."

        employees: dict = {}
        for row in all_rows:
            if not row or row[0] != date_str:
                continue
            p = _parse_row(row)
            if p is None:
                continue
            key = (p["name"], p["role"], p["branch"], p["start"])
            if key not in employees:
                employees[key] = {
                    "name": p["name"], "tg_id": p["tg_id"],
                    "role": p["role"], "branch": p["branch"],
                    "start": p["start"], "end": p["end"],
                    "ставки": {}, "процедуры": {},
                }
            bucket = "ставки" if p["rtype"] == "ставка" else "процедуры"
            employees[key][bucket][p["pos"]] = (p["qty"], p["price"])

        if not employees:
            return f"📭 Нет данных за {date_str}."

        lines = [f"📊 Отчёт за {date_str}", ""]
        grand_total = 0.0

        for e in employees.values():
            role_short = ROLE_SHORT.get(e["role"], e["role"])
            id_str     = f" [ID: {e['tg_id']}]" if e["tg_id"] else ""
            time_str   = f"⏰ {e['start']}" + (f" – {e['end']}" if e["end"] else "")
            lines.append(f"👤 {e['name']}{id_str} ({role_short}) | {e['branch']}")
            lines.append(time_str)
            emp_total = 0.0

            if e["ставки"]:
                lines.append("  Ставки:")
                for pos, (qty, price) in e["ставки"].items():
                    amt = qty * price
                    emp_total += amt
                    q = str(int(qty)) if qty == int(qty) else str(qty)
                    lines.append(f"    {pos}: {q} × {int(price)} = {int(amt)} руб")

            if e["процедуры"]:
                lines.append("  Процедуры:")
                for pos, (qty, price) in e["процедуры"].items():
                    amt = qty * price
                    emp_total += amt
                    q = str(int(qty)) if qty == int(qty) else str(qty)
                    lines.append(f"    {pos}: {q} × {int(price)} = {int(amt)} руб")

            lines.append(f"  💰 Итого: {int(emp_total)} руб")
            lines.append("")
            grand_total += emp_total

        lines.append("─" * 30)
        lines.append(f"💵 ОБЩАЯ СУММА: {int(grand_total)} руб")
        return "\n".join(lines)

    except Exception as e:
        logger.error(f"get_report: {e}")
        return f"❌ Ошибка: {e}"

# ── Данные ────────────────────────────────────────────────────────────────────

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
                ("Ставка за выход М-В", 400), ("Чан 1-й", 450),
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
                ("Ставка за выход М-В", 400), ("Чан 1-й", 450), ("Чан 2-й", 300),
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

# ── Вспомогательные функции ───────────────────────────────────────────────────

def kb(buttons: list, cols: int = 2) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(buttons), cols):
        rows.append([InlineKeyboardButton(t, callback_data=d) for t, d in buttons[i:i+cols]])
    return InlineKeyboardMarkup(rows)

def names_kb():   return kb([(n, f"rn_{n}") for n in ALL_NAMES], cols=3)
def roles_kb():   return kb([(v, f"rr_{k}") for k, v in ROLE_LABELS.items()], cols=1)
def branch_kb():  return kb([("🏠 Ирий", "br_Ирий"), ("🏠 Правь", "br_Правь")])

def main_kb(role: str) -> InlineKeyboardMarkup:
    btns = [("➕ Ставки / подготовка", "add_rate")]
    if role in ROLES_WITH_PROCS:
        btns.append(("➕ Процедуры", "add_proc"))
    btns += [("📊 Мой итог", "show"), ("👤 Профиль", "profile"), ("🔒 Закрыть смену", "close")]
    return kb(btns, cols=2)

def profile_kb():
    return kb([("🔄 Сменить роль", "change_role"), ("✏️ Сменить имя", "change_name"),
               ("🚀 Начать смену", "start_shift")], cols=1)

def new_session(user: dict, tid: int) -> dict:
    now = now_msk()
    return {
        "role": user["role"], "name": user["name"], "branch": None,
        "tid": tid,
        "date": now.strftime("%d.%m.%Y"),
        "start_time": now.strftime("%H:%M"),
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
        qs = f" [{_qty_str(qty)}]" if qty else ""
        btns.append((f"{mark}{name} +{price}{qs}", f"{prefix}_{i}"))
    return btns

# ── Запланированные задачи (JobQueue) ─────────────────────────────────────────

async def daily_report_job(context):
    """Автоотчёт каждый день в 21:00 МСК."""
    if not ADMIN_CHAT_ID:
        return
    date_str = now_msk().strftime("%d.%m.%Y")
    logger.info(f"daily_report_job: формирую отчёт за {date_str}")
    report = get_report(date_str)
    try:
        msg = f"🌙 Автоотчёт за {date_str}\n\n{report}"
        for i in range(0, len(msg), 4000):
            await context.bot.send_message(ADMIN_CHAT_ID, msg[i:i+4000])
    except Exception as e:
        logger.error(f"daily_report_job: {e}")

async def late_check_job(context):
    """Проверка в 11:00 МСК — если никто не открыл смену, предупредить."""
    if not ADMIN_CHAT_ID:
        return
    today = now_msk().strftime("%d.%m.%Y")
    active_today  = {tid for tid, v in ACTIVE_SHIFTS.items() if v.get("date") == today}
    worked_today  = TODAY_WORKED.get(today, set())
    if not active_today and not worked_today:
        try:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"⚠️ 11:00 МСК — сегодня ({today}) ещё никто не открыл смену!"
            )
        except Exception as e:
            logger.error(f"late_check_job: {e}")

async def post_init(application: Application) -> None:
    """Регистрируем плановые задачи после инициализации приложения."""
    jq = application.job_queue
    if jq is None:
        logger.warning("JobQueue недоступен — задачи по расписанию не зарегистрированы. "
                       "Установите: python-telegram-bot[job-queue]")
        return
    # Ежедневный отчёт в 21:00 МСК
    jq.run_daily(
        daily_report_job,
        time=datetime.time(23, 55, 0, tzinfo=MSK_TZ),
        name="daily_report",
    )
    # Проверка опозданий в 11:00 МСК
    jq.run_daily(
        late_check_job,
        time=datetime.time(11, 0, 0, tzinfo=MSK_TZ),
        name="late_check",
    )
    logger.info("Задачи по расписанию зарегистрированы: отчёт в 21:00, проверка в 11:00 МСК")

# ── Команды ───────────────────────────────────────────────────────────────────

async def cmd_start(update, ctx):
    tid = update.effective_user.id
    logger.info(f"[/start] user={tid}")
    user = ctx.user_data.get("user") or get_user(tid)
    if user:
        ctx.user_data["user"] = user
        await update.message.reply_text(
            f"👋 С возвращением, {user['name']}!\nРоль: {ROLE_LABELS.get(user['role'], user['role'])}",
            reply_markup=kb([("🚀 Начать смену", "start_shift"), ("👤 Мой профиль", "go_profile")], cols=1),
        )
    else:
        await update.message.reply_text("👋 Добро пожаловать!\n\nВыберите своё имя:", reply_markup=names_kb())

async def cmd_reset(update, ctx):
    tid = update.effective_user.id
    users = _load_local()
    users.pop(str(tid), None)
    _save_local(users)
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
    await update.message.reply_text("🔄 Профиль сброшен.\n\nВыберите своё имя:", reply_markup=names_kb())

async def cmd_myid(update, ctx):
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"Ваш Chat ID: `{cid}`\n\nДобавьте `ADMIN_CHAT_ID` в Railway Variables.",
        parse_mode="Markdown",
    )

async def cmd_report(update, ctx):
    tid = update.effective_user.id
    if ADMIN_CHAT_ID and tid != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Команда доступна только администратору.")
        return
    args = ctx.args
    date_str = args[0] if args else now_msk().strftime("%d.%m.%Y")
    await update.message.reply_text(f"⏳ Формирую отчёт за {date_str}...")
    report = get_report(date_str)
    for i in range(0, len(report), 4000):
        await update.message.reply_text(report[i:i+4000])

async def cmd_salary(update, ctx):
    """
    /salary [MM.YYYY] — начисления за месяц по каждому сотруднику.
    По умолчанию — текущий месяц.
    """
    tid = update.effective_user.id
    if ADMIN_CHAT_ID and tid != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Команда доступна только администратору.")
        return
    now = now_msk()
    args = ctx.args
    if args:
        try:
            parts = args[0].split(".")
            month, year = int(parts[0]), int(parts[1])
        except Exception:
            await update.message.reply_text("❌ Формат: /salary MM.YYYY\nПример: /salary 06.2026")
            return
    else:
        month, year = now.month, now.year

    label = f"{month:02d}.{year}"
    await update.message.reply_text(f"⏳ Формирую зарплатный отчёт за {label}...")

    try:
        sh = _open_sheet()
        if sh is None:
            await update.message.reply_text("❌ Google Sheets не подключены.")
            return
        ws = sh.worksheet("Журнал")
        all_rows = ws.get_all_values()

        totals: dict = {}  # name -> {role, total, ставки, процедуры, shifts}
        for row in all_rows:
            if not row or row[0] == "Дата":
                continue
            p = _parse_row(row)
            if p is None:
                continue
            try:
                d = datetime.datetime.strptime(row[0], "%d.%m.%Y")
                if d.month != month or d.year != year:
                    continue
            except Exception:
                continue
            name = p["name"]
            if name not in totals:
                totals[name] = {"role": p["role"], "total": 0.0,
                                "ставки": 0.0, "процедуры": 0.0, "shifts": set()}
            totals[name]["total"]  += p["amount"]
            if p["rtype"] == "ставка":
                totals[name]["ставки"] += p["amount"]
            else:
                totals[name]["процедуры"] += p["amount"]
            totals[name]["shifts"].add(row[0])

        if not totals:
            await update.message.reply_text(f"📭 Нет данных за {label}.")
            return

        lines = [f"💰 Начисления за {label}", ""]
        grand = 0.0
        for name, d in sorted(totals.items(), key=lambda x: -x[1]["total"]):
            role_s   = ROLE_SHORT.get(d["role"], d["role"])
            n_shifts = len(d["shifts"])
            lines.append(f"👤 {name} ({role_s}) — {n_shifts} смен")
            if d["ставки"]:
                lines.append(f"   Ставки:    {int(d['ставки'])} руб")
            if d["процедуры"]:
                lines.append(f"   Процедуры: {int(d['процедуры'])} руб")
            lines.append(f"   💰 Итого: {int(d['total'])} руб")
            lines.append("")
            grand += d["total"]
        lines.append("─" * 30)
        lines.append(f"💵 ВСЕГО К ВЫПЛАТЕ: {int(grand)} руб")

        text = "\n".join(lines)
        for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i:i+4000])

    except Exception as e:
        logger.error(f"cmd_salary: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def cmd_stats(update, ctx):
    """
    /stats [MM.YYYY] — статистика по процедурам и ставкам.
    По умолчанию — текущий месяц.
    """
    tid = update.effective_user.id
    if ADMIN_CHAT_ID and tid != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ Команда доступна только администратору.")
        return
    now = now_msk()
    args = ctx.args
    if args:
        try:
            parts = args[0].split(".")
            month, year = int(parts[0]), int(parts[1])
        except Exception:
            await update.message.reply_text("❌ Формат: /stats MM.YYYY\nПример: /stats 06.2026")
            return
    else:
        month, year = now.month, now.year

    label = f"{month:02d}.{year}"
    await update.message.reply_text(f"⏳ Формирую статистику за {label}...")

    try:
        sh = _open_sheet()
        if sh is None:
            await update.message.reply_text("❌ Google Sheets не подключены.")
            return
        ws = sh.worksheet("Журнал")
        all_rows = ws.get_all_values()

        procs: dict = {}  # pos -> {qty, revenue}
        rates: dict = {}  # pos -> {qty, revenue}

        for row in all_rows:
            if not row or row[0] == "Дата":
                continue
            p = _parse_row(row)
            if p is None:
                continue
            try:
                d = datetime.datetime.strptime(row[0], "%d.%m.%Y")
                if d.month != month or d.year != year:
                    continue
            except Exception:
                continue
            bucket = procs if p["rtype"] == "процедура" else rates
            pos = p["pos"]
            if pos not in bucket:
                bucket[pos] = {"qty": 0.0, "revenue": 0.0}
            bucket[pos]["qty"]     += p["qty"]
            bucket[pos]["revenue"] += p["amount"]

        if not procs and not rates:
            await update.message.reply_text(f"📭 Нет данных за {label}.")
            return

        lines = [f"📈 Статистика за {label}", ""]

        if procs:
            total_proc_rev = sum(v["revenue"] for v in procs.values())
            lines.append(f"🔥 Процедуры — выручка: {int(total_proc_rev)} руб")
            for pos, d in sorted(procs.items(), key=lambda x: -x[1]["revenue"]):
                q = _qty_str(d["qty"])
                lines.append(f"  {pos}: {q} шт → {int(d['revenue'])} руб")
            lines.append("")

        if rates:
            total_rate_rev = sum(v["revenue"] for v in rates.values())
            lines.append(f"⚡ Ставки — выплаты: {int(total_rate_rev)} руб")
            for pos, d in sorted(rates.items(), key=lambda x: -x[1]["revenue"]):
                q = _qty_str(d["qty"])
                lines.append(f"  {pos}: {q} шт → {int(d['revenue'])} руб")

        text = "\n".join(lines)
        for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i:i+4000])

    except Exception as e:
        logger.error(f"cmd_stats: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")

# ── Единый обработчик кнопок ──────────────────────────────────────────────────

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
            await q.edit_message_text("⚠️ Ошибка. Нажмите /start",
                                      reply_markup=kb([("🔄 Начать заново", "restart")]))
        except Exception:
            pass

async def _handle(q, ctx, data: str, tid: int):
    if data == "restart":
        user = get_user(tid)
        if user:
            ctx.user_data["user"] = user
            await q.edit_message_text(f"👋 {user['name']}!",
                reply_markup=kb([("🚀 Начать смену", "start_shift"), ("👤 Профиль", "go_profile")], cols=1))
        else:
            await q.edit_message_text("Выберите своё имя:", reply_markup=names_kb())
        return

    if data.startswith("rn_"):
        name = data[3:]
        if ctx.user_data.get("changing_name"):
            ctx.user_data.pop("changing_name", None)
            user = ctx.user_data.get("user") or get_user(tid) or {}
            user["name"] = name
            set_user(tid, user)
            ctx.user_data["user"] = user
            await q.edit_message_text(f"✅ Имя изменено: {name}", reply_markup=profile_kb())
        else:
            ctx.user_data["reg_name"] = name
            await q.edit_message_text(f"👤 Имя: {name}\n\nВыберите роль:", reply_markup=roles_kb())
        return

    if data.startswith("rr_"):
        role = data[3:]
        if role not in ROLE_LABELS:
            await q.edit_message_text("Выберите роль:", reply_markup=roles_kb())
            return
        if ctx.user_data.get("changing_role"):
            ctx.user_data.pop("changing_role", None)
            user = ctx.user_data.get("user") or get_user(tid) or {}
            user["role"] = role
            set_user(tid, user)
            ctx.user_data["user"] = user
            await q.edit_message_text(f"✅ Роль изменена: {ROLE_LABELS[role]}", reply_markup=profile_kb())
        else:
            name = ctx.user_data.get("reg_name", "")
            if not name:
                await q.edit_message_text("Выберите своё имя:", reply_markup=names_kb())
                return
            user = {"name": name, "role": role}
            set_user(tid, user)
            ctx.user_data["user"] = user
            ctx.user_data.pop("reg_name", None)
            await q.edit_message_text(
                f"✅ Профиль создан!\n👤 {name}\n🎭 {ROLE_LABELS[role]}",
                reply_markup=kb([("🚀 Начать смену", "start_shift"), ("👤 Профиль", "go_profile")], cols=1),
            )
        return

    if data == "go_profile":
        user = ctx.user_data.get("user") or get_user(tid)
        if not user:
            await q.edit_message_text("Выберите своё имя:", reply_markup=names_kb())
            return
        ctx.user_data["user"] = user
        await q.edit_message_text(
            f"👤 {user['name']}\nРоль: {ROLE_LABELS.get(user['role'], user['role'])}",
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
        s = new_session(user, tid)
        s["branch"] = branch
        ctx.user_data["s"] = s
        # Трекинг активных смен
        ACTIVE_SHIFTS[tid] = {
            "name": user["name"], "role": user["role"],
            "branch": branch, "start_time": s["start_time"], "date": s["date"],
        }
        await q.edit_message_text(
            f"✅ Смена открыта!\n👤 {user['name']} | {ROLE_SHORT.get(user['role'], '')} | {branch}\n"
            f"📅 {s['date']} {s['start_time']} МСК",
            reply_markup=main_kb(user["role"]),
        )
        # Уведомление админу
        if ADMIN_CHAT_ID:
            try:
                role_s = ROLE_SHORT.get(user["role"], user["role"])
                await q.get_bot().send_message(
                    ADMIN_CHAT_ID,
                    f"🟢 Смена открыта\n"
                    f"👤 {user['name']} ({role_s})\n"
                    f"📍 {branch} | {s['date']} {s['start_time']} МСК",
                )
            except Exception as e:
                logger.error(f"Уведомление об открытии смены: {e}")
        return

    s = sess(ctx)

    if data == "show":
        if not s.get("branch"):
            await q.edit_message_text("Смена не открыта. Нажмите /start")
            return
        await q.edit_message_text(fmt(s), reply_markup=main_kb(s["role"]))
        return

    if data == "profile":
        user = ctx.user_data.get("user") or get_user(tid)
        if not user:
            await q.edit_message_text("Выберите своё имя:", reply_markup=names_kb())
            return
        await q.edit_message_text(
            f"👤 {user['name']}\nРоль: {ROLE_LABELS.get(s.get('role', user['role']), '')}",
            reply_markup=kb([("🔄 Сменить роль в смене", "sr_shift"), ("◀️ Назад", "back_main")], cols=1),
        )
        return

    if data == "sr_shift":
        await q.edit_message_text("Выберите роль для текущей смены:",
            reply_markup=kb([(v, f"srs_{k}") for k, v in ROLE_LABELS.items()], cols=1))
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
        await q.edit_message_text(f"✅ Роль изменена: {ROLE_LABELS[role]}", reply_markup=main_kb(role))
        return

    if data == "back_main":
        if not s.get("branch"):
            await q.edit_message_text("Выберите действие:",
                reply_markup=kb([("🚀 Начать смену", "start_shift"), ("👤 Профиль", "go_profile")], cols=1))
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
        prefix = "r" if data == "add_rate" else "p"
        items = cat.get("ставки" if data == "add_rate" else "процедуры", [])
        title = "➕ Ставки / подготовка:" if data == "add_rate" else "➕ Процедуры:"
        if not items:
            await q.edit_message_text("Нет позиций.", reply_markup=kb([("◀️ Назад", "back_main")]))
            return
        ctx.user_data["cur_items"] = items
        ctx.user_data["cur_prefix"] = prefix
        await q.edit_message_text(title,
            reply_markup=kb(_item_buttons(items, prefix, s) + [("◀️ Назад", "back_main")], cols=1))
        return

    if data.startswith("r_") or data.startswith("p_"):
        prefix, idx = data.split("_", 1)[0], int(data.split("_", 1)[1])
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
            reply_markup=kb([("+1","qty_p1"),("+0.5","qty_p05"),
                             ("-1","qty_m1"),("-0.5","qty_m05"),("◀️ К списку","qty_back")], cols=2),
        )
        return

    if data.startswith("qty_"):
        if data == "qty_back":
            items = ctx.user_data.get("cur_items", [])
            prefix = ctx.user_data.get("cur_prefix", "r")
            title = "➕ Ставки / подготовка:" if prefix == "r" else "➕ Процедуры:"
            await q.edit_message_text(title,
                reply_markup=kb(_item_buttons(items, prefix, s) + [("◀️ Назад", "back_main")], cols=1))
            return
        name = ctx.user_data.get("cur_name", "")
        price = ctx.user_data.get("cur_price", 0)
        prefix = ctx.user_data.get("cur_prefix", "r")
        if not name:
            await q.edit_message_text("Назад", reply_markup=kb([("◀️ Назад", "back_main")]))
            return
        bucket = s.get("ставки", {}) if prefix == "r" else s.get("процедуры", {})
        delta = {"qty_p1": 1.0, "qty_p05": 0.5, "qty_m1": -1.0, "qty_m05": -0.5}.get(data, 0)
        new_qty = max(0.0, bucket.get(name, 0) + delta)
        if new_qty == 0:
            bucket.pop(name, None)
        else:
            bucket[name] = new_qty
        await q.edit_message_text(
            f"📌 {name}\nКоличество: {_qty_str(new_qty)} → {int(price * new_qty)} руб\n\nВыберите количество:",
            reply_markup=kb([("+1","qty_p1"),("+0.5","qty_p05"),
                             ("-1","qty_m1"),("-0.5","qty_m05"),("◀️ К списку","qty_back")], cols=2),
        )
        return

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
        s["end_time"] = now_msk().strftime("%H:%M")
        summary = fmt(s, final=True) + f"\n⏱ Конец: {s['end_time']} МСК"
        await q.edit_message_text(summary + "\n\n✅ Смена закрыта. Спасибо!")
        write_shift(s)
        # Убираем из активных смен, добавляем в "работал сегодня"
        ACTIVE_SHIFTS.pop(tid, None)
        today = s.get("date", now_msk().strftime("%d.%m.%Y"))
        if today not in TODAY_WORKED:
            TODAY_WORKED[today] = set()
        TODAY_WORKED[today].add(tid)
        # Уведомление админу о закрытии
        if ADMIN_CHAT_ID:
            try:
                await q.get_bot().send_message(
                    ADMIN_CHAT_ID,
                    f"🔴 Смена закрыта\n\n{summary}",
                )
            except Exception as e:
                logger.error(f"Уведомление о закрытии смены: {e}")
        user = ctx.user_data.get("user") or get_user(tid)
        ctx.user_data.clear()
        if user:
            ctx.user_data["user"] = user
        await q.get_bot().send_message(
            q.message.chat_id, "Хотите начать новую смену?",
            reply_markup=kb([("🚀 Новая смена", "start_shift"), ("👤 Профиль", "go_profile")], cols=1),
        )
        return

    logger.warning(f"[cb] необработанный data={data!r}")

# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    sync_users_from_sheets()
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("reset",   cmd_reset))
    app.add_handler(CommandHandler("myid",    cmd_myid))
    app.add_handler(CommandHandler("report",  cmd_report))
    app.add_handler(CommandHandler("salary",  cmd_salary))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CallbackQueryHandler(on_callback))

    async def err_handler(update, ctx):
        logger.error(f"[GLOBAL ERROR] {ctx.error}", exc_info=ctx.error)
    app.add_error_handler(err_handler)

    webhook_url = os.getenv("WEBHOOK_URL", "")
    port = int(os.getenv("PORT", "8080"))
    logger.info("Бот Сансара v5.0 запускается...")
    if webhook_url:
        full_url = f"{webhook_url.rstrip('/')}/{TOKEN}"
        logger.info(f"Webhook URL: {full_url}")
        app.run_webhook(listen="0.0.0.0", port=port, url_path=TOKEN,
                        webhook_url=full_url, allowed_updates=Update.ALL_TYPES,
                        drop_pending_updates=True)
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
