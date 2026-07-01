#!/usr/bin/env python3
"""
Сансара — бот учёта смен v5.2
- Доп продажа для м1/м2/мв: сотрудник вводит сумму текстом -> 10% комиссия
- Геолокация при открытии смены (Ирий: Каскадная 138а, Правь: Крупской 66)
"""
import os, json, logging, datetime, math
import pytz

import gspread
from google.oauth2.service_account import Credentials
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove)
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, filters)

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
SCHEDULE_ROLES = {"м1", "м2", "техпер"}
DAY_COLS       = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
SALE_ROLES     = {"м1", "м2", "мв"}
SALE_PERCENT   = 0.10

# Геолокация филиалов (Ростов-на-Дону)
# Ирий  - ул. Каскадная, 138а
# Правь - ул. Крупской, 66
BRANCH_COORDS = {
    "Ирий":  (47.298467, 39.779242),
    "Правь": (47.214073, 39.655383),
}
GEO_RADIUS_M = int(os.getenv("GEO_RADIUS", "500"))

ACTIVE_SHIFTS: dict = {}
TODAY_WORKED_NAMES: dict = {}
LATE_ALERTS: dict = {}

def now_msk() -> datetime.datetime:
    return datetime.datetime.now(MSK)

def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def branch_has_geo(branch: str) -> bool:
    return branch in BRANCH_COORDS

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

def ensure_schedule_ws(sh):
    titles = [w.title for w in sh.worksheets()]
    if "Расписание" not in titles:
        ws = sh.add_worksheet("Расписание", rows=50, cols=10)
        ws.update([["Имя", "Роль", "Филиал", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]], "A1")
        ws.append_row(["Сергей", "м1", "Ирий", "10:00", "10:00", "", "10:00", "10:00", "", ""])
        logger.info("Создан лист Расписание")
    return sh.worksheet("Расписание")

def get_schedule_today(sh) -> list:
    try:
        ws = ensure_schedule_ws(sh)
        rows = ws.get_all_values()
        if not rows:
            return []
        headers = rows[0]
        today_wd = now_msk().weekday()
        day_name = DAY_COLS[today_wd]
        if day_name not in headers:
            return []
        day_idx = headers.index(day_name)
        result = []
        for row in rows[1:]:
            if not any(row):
                continue
            if len(row) <= day_idx:
                continue
            name = row[0].strip(); role = row[1].strip()
            branch = row[2].strip() if len(row) > 2 else ""
            shift_time = row[day_idx].strip()
            if not name or not shift_time or role not in SCHEDULE_ROLES:
                continue
            result.append({"name": name, "role": role, "branch": branch, "time": shift_time})
        return result
    except Exception as e:
        logger.error(f"get_schedule_today: {e}")
        return []

# ── Пользователи ──────────────────────────────────────────────────────────────

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
    local = _load_local()
    if str(tid) in local:
        return local[str(tid)]
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
                    return user
    except Exception as e:
        logger.error(f"get_user sheets fallback: {e}")
    return None

def get_tid_by_name(name: str) -> int | None:
    users = _load_local()
    for tid_str, data in users.items():
        if data.get("name", "").strip() == name.strip():
            try:
                return int(tid_str)
            except ValueError:
                pass
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
        for entry in s.get("доп_продажи", []):
            sale = entry["sale"]
            commission = entry["commission"]
            rows.append([date_str, s["name"], tg_id, s["role"], s["branch"],
                         start_time, end_time, "доп продажа",
                         f"Продажа {int(sale)} руб", 1, commission, commission,
                         period, day, month, year])
        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")
            logger.info(f"Журнал: {len(rows)} строк для {s['name']}")
    except Exception as e:
        logger.error(f"write_shift: {e}")

def _parse_row(row: list) -> dict | None:
    if len(row) < 8:
        return None
    name = row[1]
    if row[2] in ROLE_CODES:
        tg_id  = ""
        role   = row[2]; branch = row[3]
        if row[4] in ("ставка", "процедура", "доп продажа"):
            rtype = row[4]; pos = row[5]
            qi, pi = 6, 7
            start = end = ""
        else:
            start = row[4]; end = row[5] if len(row) > 5 else ""
            rtype = row[6] if len(row) > 6 else ""
            pos   = row[7] if len(row) > 7 else ""
            qi, pi = 8, 9
    else:
        tg_id  = row[2]; role = row[3] if len(row) > 3 else ""
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
            return "Google Sheets не подключены."
        ws = sh.worksheet("Журнал")
        all_rows = ws.get_all_values()
        if not all_rows:
            return f"Нет данных за {date_str}."
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
                    "ставки": {}, "процедуры": {}, "продажи": [],
                }
            if p["rtype"] == "ставка":
                employees[key]["ставки"][p["pos"]] = (p["qty"], p["price"])
            elif p["rtype"] == "процедура":
                employees[key]["процедуры"][p["pos"]] = (p["qty"], p["price"])
            elif p["rtype"] == "доп продажа":
                employees[key]["продажи"].append(p["amount"])
        if not employees:
            return f"Нет данных за {date_str}."
        lines = [f"Отчёт за {date_str}", ""]
        grand_total = 0.0
        for e in employees.values():
            role_short = ROLE_SHORT.get(e["role"], e["role"])
            id_str     = f" [ID: {e['tg_id']}]" if e["tg_id"] else ""
            time_str   = f"Время: {e['start']}" + (f" - {e['end']}" if e["end"] else "")
            lines.append(f"👤 {e['name']}{id_str} ({role_short}) | {e['branch']}")
            lines.append(time_str)
            emp_total = 0.0
            if e["ставки"]:
                lines.append("  Ставки:")
                for pos, (qty, price) in e["ставки"].items():
                    amt = qty * price; emp_total += amt
                    q = str(int(qty)) if qty == int(qty) else str(qty)
                    lines.append(f"    {pos}: {q} x {int(price)} = {int(amt)} руб")
            if e["процедуры"]:
                lines.append("  Процедуры:")
                for pos, (qty, price) in e["процедуры"].items():
                    amt = qty * price; emp_total += amt
                    q = str(int(qty)) if qty == int(qty) else str(qty)
                    lines.append(f"    {pos}: {q} x {int(price)} = {int(amt)} руб")
            if e["продажи"]:
                lines.append("  Доп продажи (10%):")
                for amt in e["продажи"]:
                    emp_total += amt
                    lines.append(f"    {int(amt)} руб")
            lines.append(f"  Итого: {int(emp_total)} руб")
            lines.append("")
            grand_total += emp_total
        lines.append("-" * 30)
        lines.append(f"ОБЩАЯ СУММА: {int(grand_total)} руб")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"get_report: {e}")
        return f"Ошибка: {e}"

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

# ── UI ────────────────────────────────────────────────────────────────────────

def kb(buttons: list, cols: int = 2) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(buttons), cols):
        rows.append([InlineKeyboardButton(t, callback_data=d) for t, d in buttons[i:i+cols]])
    return InlineKeyboardMarkup(rows)

def names_kb():   return kb([(n, f"rn_{n}") for n in ALL_NAMES], cols=3)
def roles_kb():   return kb([(v, f"rr_{k}") for k, v in ROLE_LABELS.items()], cols=1)
def branch_kb():  return kb([("Ирий", "br_Ирий"), ("Правь", "br_Правь")])

def main_kb(role: str) -> InlineKeyboardMarkup:
    btns = [("Ставки / подготовка", "add_rate")]
    if role in ROLES_WITH_PROCS:
        btns.append(("Процедуры", "add_proc"))
    if role in SALE_ROLES:
        btns.append(("Доп продажа (10%)", "add_sale"))
    btns += [("Мой итог", "show"), ("Профиль", "profile"), ("Закрыть смену", "close")]
    return kb(btns, cols=2)

def profile_kb():
    return kb([("Сменить роль", "change_role"), ("Сменить имя", "change_name"),
               ("Начать смену", "start_shift")], cols=1)

def geo_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("Отправить геолокацию", request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )

def new_session(user: dict, tid: int) -> dict:
    now = now_msk()
    return {
        "role": user["role"], "name": user["name"], "branch": None,
        "tid": tid,
        "date": now.strftime("%d.%m.%Y"),
        "start_time": now.strftime("%H:%M"),
        "ставки": {}, "процедуры": {}, "доп_продажи": [],
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
        f"👤 {s.get('name', '—')} ({ROLE_SHORT.get(s.get('role', ''), '—')})",
        f"📍 {s.get('branch', '—')} | {s.get('date', '—')} {s.get('start_time', '')}",
        "-" * 30,
    ]
    total = 0.0
    rates_dict = dict(cat.get("ставки", []))
    if s.get("ставки"):
        lines.append("Ставки / подготовка:")
        for name, qty in s["ставки"].items():
            price = rates_dict.get(name, 0)
            amt = price * qty; total += amt
            lines.append(f"  {name}: {_qty_str(qty)} x {price} = {int(amt)} руб")
    procs_dict = dict(cat.get("процедуры", []))
    if s.get("процедуры"):
        lines.append("Процедуры:")
        for name, qty in s["процедуры"].items():
            price = procs_dict.get(name, 0)
            amt = price * qty; total += amt
            lines.append(f"  {name}: {_qty_str(qty)} x {price} = {int(amt)} руб")
    if s.get("доп_продажи"):
        lines.append("Доп продажи (10%):")
        for entry in s["доп_продажи"]:
            comm = entry["commission"]; total += comm
            lines.append(f"  Продажа {int(entry['sale'])} руб -> {int(comm)} руб")
    lines += ["-" * 30, f"ИТОГО: {int(total)} руб"]
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

def _open_shift(user, tid, branch, ctx):
    s = new_session(user, tid)
    s["branch"] = branch
    ctx.user_data["s"] = s
    ACTIVE_SHIFTS[tid] = {
        "name": user["name"], "role": user["role"],
        "branch": branch, "start_time": s["start_time"], "date": s["date"],
    }
    return s

# ── Запланированные задачи ─────────────────────────────────────────────────────

async def daily_report_job(context):
    if not ADMIN_CHAT_ID:
        return
    date_str = now_msk().strftime("%d.%m.%Y")
    logger.info(f"daily_report_job: {date_str}")
    report = get_report(date_str)
    try:
        msg = f"Автоотчёт за {date_str}\n\n{report}"
        for i in range(0, len(msg), 4000):
            await context.bot.send_message(ADMIN_CHAT_ID, msg[i:i+4000])
    except Exception as e:
        logger.error(f"daily_report_job: {e}")

async def late_check_job(context):
    if not ADMIN_CHAT_ID:
        return
    now = now_msk()
    if not (9 <= now.hour < 18):
        return
    today = now.strftime("%d.%m.%Y")
    try:
        sh = _open_sheet()
        if sh is None:
            return
        scheduled = get_schedule_today(sh)
        if not scheduled:
            return
        active_names = {v["name"] for v in ACTIVE_SHIFTS.values() if v.get("date") == today}
        worked_names = TODAY_WORKED_NAMES.get(today, set())
        alerted      = LATE_ALERTS.get(today, set())
        late_people  = []
        for entry in scheduled:
            name = entry["name"]
            if name in alerted or name in active_names or name in worked_names:
                continue
            try:
                sch_h, sch_m = map(int, entry["time"].split(":"))
                total_min = sch_h * 60 + sch_m + 30
                grace_h, grace_m = divmod(total_min, 60)
                if (now.hour, now.minute) >= (grace_h % 24, grace_m):
                    late_people.append(entry)
            except Exception:
                continue
        if not late_people:
            return
        if today not in LATE_ALERTS:
            LATE_ALERTS[today] = set()
        for entry in late_people:
            LATE_ALERTS[today].add(entry["name"])
        lines = [f"Не вышли на смену ({today}):"]
        for entry in late_people:
            role_s = ROLE_SHORT.get(entry["role"], entry["role"])
            branch_s = f" | {entry['branch']}" if entry["branch"] else ""
            lines.append(f"  👤 {entry['name']} ({role_s}){branch_s} — план {entry['time']}")
        await context.bot.send_message(ADMIN_CHAT_ID, "\n".join(lines))
        for entry in late_people:
            emp_tid = get_tid_by_name(entry["name"])
            if emp_tid is None:
                continue
            try:
                await context.bot.send_message(
                    emp_tid,
                    f"⏰ {entry['name']}, твоя смена должна была начаться в {entry['time']}!\n"
                    f"Не забудь открыть смену в боте — нажми /start"
                )
            except Exception as e:
                logger.error(f"late_check: не удалось написать {entry['name']}: {e}")
    except Exception as e:
        logger.error(f"late_check_job: {e}")

async def post_init(application: Application) -> None:
    jq = application.job_queue
    if jq is None:
        logger.warning("JobQueue недоступен")
        return
    jq.run_daily(daily_report_job, time=datetime.time(23, 55, 0, tzinfo=MSK_TZ), name="daily_report")
    jq.run_repeating(late_check_job, interval=1800, first=60, name="late_check")
    logger.info("Задачи по расписанию зарегистрированы")

# ── Команды ───────────────────────────────────────────────────────────────────

async def cmd_start(update, ctx):
    tid = update.effective_user.id
    logger.info(f"[/start] user={tid}")
    user = ctx.user_data.get("user") or get_user(tid)
    if user:
        ctx.user_data["user"] = user
        await update.message.reply_text(
            f"С возвращением, {user['name']}!\nРоль: {ROLE_LABELS.get(user['role'], user['role'])}",
            reply_markup=kb([("Начать смену", "start_shift"), ("Мой профиль", "go_profile")], cols=1),
        )
    else:
        await update.message.reply_text("Добро пожаловать!\n\nВыберите своё имя:", reply_markup=names_kb())

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
    await update.message.reply_text("Профиль сброшен.\n\nВыберите своё имя:", reply_markup=names_kb())

async def cmd_myid(update, ctx):
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"Ваш Chat ID: `{cid}`\n\nДобавьте ADMIN_CHAT_ID в Railway Variables.",
        parse_mode="Markdown",
    )

async def cmd_report(update, ctx):
    tid = update.effective_user.id
    if ADMIN_CHAT_ID and tid != ADMIN_CHAT_ID:
        await update.message.reply_text("Команда доступна только администратору.")
        return
    args = ctx.args
    date_str = args[0] if args else now_msk().strftime("%d.%m.%Y")
    await update.message.reply_text(f"Формирую отчёт за {date_str}...")
    report = get_report(date_str)
    for i in range(0, len(report), 4000):
        await update.message.reply_text(report[i:i+4000])

async def cmd_salary(update, ctx):
    tid = update.effective_user.id
    if ADMIN_CHAT_ID and tid != ADMIN_CHAT_ID:
        await update.message.reply_text("Команда доступна только администратору.")
        return
    now = now_msk()
    args = ctx.args
    if args:
        try:
            parts = args[0].split(".")
            month, year = int(parts[0]), int(parts[1])
        except Exception:
            await update.message.reply_text("Формат: /salary MM.YYYY")
            return
    else:
        month, year = now.month, now.year
    label = f"{month:02d}.{year}"
    await update.message.reply_text(f"Формирую зарплатный отчёт за {label}...")
    try:
        sh = _open_sheet()
        if sh is None:
            await update.message.reply_text("Google Sheets не подключены.")
            return
        ws = sh.worksheet("Журнал")
        all_rows = ws.get_all_values()
        totals: dict = {}
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
                                "ставки": 0.0, "процедуры": 0.0,
                                "продажи": 0.0, "shifts": set()}
            totals[name]["total"] += p["amount"]
            if p["rtype"] == "ставка":
                totals[name]["ставки"] += p["amount"]
            elif p["rtype"] == "процедура":
                totals[name]["процедуры"] += p["amount"]
            elif p["rtype"] == "доп продажа":
                totals[name]["продажи"] += p["amount"]
            totals[name]["shifts"].add(row[0])
        if not totals:
            await update.message.reply_text(f"Нет данных за {label}.")
            return
        lines = [f"Начисления за {label}", ""]
        grand = 0.0
        for name, info in sorted(totals.items(), key=lambda x: -x[1]["total"]):
            role_s = ROLE_SHORT.get(info["role"], info["role"])
            lines.append(f"👤 {name} ({role_s}) — {len(info['shifts'])} смен")
            if info["ставки"]:
                lines.append(f"   Ставки:      {int(info['ставки'])} руб")
            if info["процедуры"]:
                lines.append(f"   Процедуры:   {int(info['процедуры'])} руб")
            if info["продажи"]:
                lines.append(f"   Доп продажи: {int(info['продажи'])} руб")
            lines.append(f"   Итого: {int(info['total'])} руб")
            lines.append("")
            grand += info["total"]
        lines.append("-" * 30)
        lines.append(f"ВСЕГО К ВЫПЛАТЕ: {int(grand)} руб")
        text = "\n".join(lines)
        for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i:i+4000])
    except Exception as e:
        logger.error(f"cmd_salary: {e}", exc_info=True)
        await update.message.reply_text(f"Ошибка: {e}")

async def cmd_stats(update, ctx):
    tid = update.effective_user.id
    if ADMIN_CHAT_ID and tid != ADMIN_CHAT_ID:
        await update.message.reply_text("Команда доступна только администратору.")
        return
    now = now_msk()
    args = ctx.args
    if args:
        try:
            parts = args[0].split(".")
            month, year = int(parts[0]), int(parts[1])
        except Exception:
            await update.message.reply_text("Формат: /stats MM.YYYY")
            return
    else:
        month, year = now.month, now.year
    label = f"{month:02d}.{year}"
    await update.message.reply_text(f"Формирую статистику за {label}...")
    try:
        sh = _open_sheet()
        if sh is None:
            await update.message.reply_text("Google Sheets не подключены.")
            return
        ws = sh.worksheet("Журнал")
        all_rows = ws.get_all_values()
        procs: dict = {}; rates: dict = {}; sales_total = 0.0
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
            if p["rtype"] == "процедура":
                bucket = procs
            elif p["rtype"] == "доп продажа":
                sales_total += p["amount"]
                continue
            else:
                bucket = rates
            pos = p["pos"]
            if pos not in bucket:
                bucket[pos] = {"qty": 0.0, "revenue": 0.0}
            bucket[pos]["qty"]     += p["qty"]
            bucket[pos]["revenue"] += p["amount"]
        if not procs and not rates and not sales_total:
            await update.message.reply_text(f"Нет данных за {label}.")
            return
        lines = [f"Статистика за {label}", ""]
        if procs:
            total_r = sum(v["revenue"] for v in procs.values())
            lines.append(f"Процедуры — выручка: {int(total_r)} руб")
            for pos, d in sorted(procs.items(), key=lambda x: -x[1]["revenue"]):
                lines.append(f"  {pos}: {_qty_str(d['qty'])} шт -> {int(d['revenue'])} руб")
            lines.append("")
        if rates:
            total_r = sum(v["revenue"] for v in rates.values())
            lines.append(f"Ставки — выплаты: {int(total_r)} руб")
            for pos, d in sorted(rates.items(), key=lambda x: -x[1]["revenue"]):
                lines.append(f"  {pos}: {_qty_str(d['qty'])} шт -> {int(d['revenue'])} руб")
            lines.append("")
        if sales_total:
            lines.append(f"Доп продажи (комиссия 10%): {int(sales_total)} руб")
        text = "\n".join(lines)
        for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i:i+4000])
    except Exception as e:
        logger.error(f"cmd_stats: {e}", exc_info=True)
        await update.message.reply_text(f"Ошибка: {e}")

async def cmd_schedule(update, ctx):
    tid = update.effective_user.id
    if ADMIN_CHAT_ID and tid != ADMIN_CHAT_ID:
        await update.message.reply_text("Команда доступна только администратору.")
        return
    try:
        sh = _open_sheet()
        if sh is None:
            await update.message.reply_text("Google Sheets не подключены.")
            return
        scheduled = get_schedule_today(sh)
        today = now_msk().strftime("%d.%m.%Y")
        wd_name = DAY_COLS[now_msk().weekday()]
        if not scheduled:
            await update.message.reply_text(
                f"Расписание на сегодня ({today}, {wd_name}) пусто.\n\n"
                f"Заполни лист Расписание в Google Sheets:\n"
                f"Имя | Роль | Филиал | Пн..Вс (время ЧЧ:ММ)"
            )
            return
        active_names = {v["name"] for v in ACTIVE_SHIFTS.values() if v.get("date") == today}
        worked_names = TODAY_WORKED_NAMES.get(today, set())
        alerted      = LATE_ALERTS.get(today, set())
        lines = [f"Расписание на {today} ({wd_name}):"]
        for e in scheduled:
            role_s = ROLE_SHORT.get(e["role"], e["role"])
            branch_s = f" | {e['branch']}" if e["branch"] else ""
            if e["name"] in active_names:
                status = "на смене"
            elif e["name"] in worked_names:
                status = "отработал"
            elif e["name"] in alerted:
                status = f"опоздал (план {e['time']})"
            else:
                status = f"план {e['time']}"
            lines.append(f"  {e['name']} ({role_s}){branch_s} — {status}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logger.error(f"cmd_schedule: {e}", exc_info=True)
        await update.message.reply_text(f"Ошибка: {e}")

# ── Обработчики геолокации и текста ──────────────────────────────────────────

async def handle_location(update, ctx):
    if not ctx.user_data.get("waiting_geo"):
        return
    location = update.message.location
    branch   = ctx.user_data.pop("pending_branch", None)
    user     = ctx.user_data.pop("pending_user", None)
    ctx.user_data.pop("waiting_geo", None)
    tid      = update.effective_user.id

    if not branch or not user:
        await update.message.reply_text("Ошибка. Нажмите /start", reply_markup=ReplyKeyboardRemove())
        return

    target_lat, target_lon = BRANCH_COORDS[branch]
    dist = haversine(location.latitude, location.longitude, target_lat, target_lon)

    if dist > GEO_RADIUS_M:
        await update.message.reply_text(
            f"Вы в {int(dist)} м от {branch}.\n"
            f"Допустимый радиус: {GEO_RADIUS_M} м.\n"
            f"Убедитесь что вы на месте и нажмите /start снова.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    s = _open_shift(user, tid, branch, ctx)
    await update.message.reply_text(
        f"Геолокация OK ({int(dist)} м)\n"
        f"Смена открыта!\n"
        f"👤 {user['name']} | {ROLE_SHORT.get(user['role'], '')} | {branch}\n"
        f"📅 {s['date']} {s['start_time']} МСК",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text("Выберите действие:", reply_markup=main_kb(user["role"]))

    if ADMIN_CHAT_ID:
        try:
            role_s = ROLE_SHORT.get(user["role"], user["role"])
            await update.get_bot().send_message(
                ADMIN_CHAT_ID,
                f"🟢 Смена открыта\n👤 {user['name']} ({role_s})\n"
                f"📍 {branch} | {s['date']} {s['start_time']} МСК\n"
                f"Расстояние: {int(dist)} м",
            )
        except Exception as e:
            logger.error(f"Уведомление об открытии смены: {e}")

async def handle_text(update, ctx):
    if ctx.user_data.get("waiting_geo"):
        return
    if not ctx.user_data.get("waiting_sale"):
        return

    text = update.message.text.strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите сумму числом, например: 5000")
        return

    commission = round(amount * SALE_PERCENT)
    s = ctx.user_data.get("s", {})
    if "доп_продажи" not in s:
        s["доп_продажи"] = []
    s["доп_продажи"].append({"sale": amount, "commission": commission})
    ctx.user_data.pop("waiting_sale", None)

    count = len(s["доп_продажи"])
    total_comm = sum(e["commission"] for e in s["доп_продажи"])
    await update.message.reply_text(
        f"Продажа {int(amount)} руб -> комиссия {commission} руб\n"
        f"Продаж за смену: {count} | Ваша комиссия: {total_comm} руб",
        reply_markup=main_kb(s.get("role", "")),
    )

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
            await q.edit_message_text("Ошибка. Нажмите /start",
                                      reply_markup=kb([("Начать заново", "restart")]))
        except Exception:
            pass

async def _handle(q, ctx, data: str, tid: int):
    if data == "restart":
        user = get_user(tid)
        if user:
            ctx.user_data["user"] = user
            await q.edit_message_text(f"👋 {user['name']}!",
                reply_markup=kb([("Начать смену", "start_shift"), ("Профиль", "go_profile")], cols=1))
        else:
            await q.edit_message_text("Выберите своё имя:", reply_markup=names_kb())
        return

    if data.startswith("rn_"):
        name = data[3:]
        if ctx.user_data.get("changing_name"):
            ctx.user_data.pop("changing_name", None)
            user = ctx.user_data.get("user") or get_user(tid) or {}
            user["name"] = name
            set_user(tid, user); ctx.user_data["user"] = user
            await q.edit_message_text(f"Имя изменено: {name}", reply_markup=profile_kb())
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
            set_user(tid, user); ctx.user_data["user"] = user
            await q.edit_message_text(f"Роль изменена: {ROLE_LABELS[role]}", reply_markup=profile_kb())
        else:
            name = ctx.user_data.get("reg_name", "")
            if not name:
                await q.edit_message_text("Выберите своё имя:", reply_markup=names_kb())
                return
            user = {"name": name, "role": role}
            set_user(tid, user); ctx.user_data["user"] = user
            ctx.user_data.pop("reg_name", None)
            await q.edit_message_text(
                f"Профиль создан!\n👤 {name}\n🎭 {ROLE_LABELS[role]}",
                reply_markup=kb([("Начать смену", "start_shift"), ("Профиль", "go_profile")], cols=1),
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
        ctx.user_data["changing_name"] = True; ctx.user_data.pop("changing_role", None)
        await q.edit_message_text("Выберите новое имя:", reply_markup=names_kb())
        return

    if data == "change_role":
        ctx.user_data["changing_role"] = True; ctx.user_data.pop("changing_name", None)
        await q.edit_message_text("Выберите новую роль:", reply_markup=roles_kb())
        return

    if data == "start_shift":
        ctx.user_data.pop("changing_name", None); ctx.user_data.pop("changing_role", None)
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

        if branch_has_geo(branch):
            ctx.user_data["pending_branch"] = branch
            ctx.user_data["pending_user"]   = user
            ctx.user_data["waiting_geo"]    = True
            await q.edit_message_text(
                f"📍 {branch}\nДля открытия смены подтвердите геолокацию:"
            )
            await q.get_bot().send_message(
                q.message.chat_id,
                "Нажмите кнопку ниже:",
                reply_markup=geo_kb(),
            )
        else:
            s = _open_shift(user, tid, branch, ctx)
            await q.edit_message_text(
                f"Смена открыта!\n👤 {user['name']} | {ROLE_SHORT.get(user['role'], '')} | {branch}\n"
                f"📅 {s['date']} {s['start_time']} МСК",
                reply_markup=main_kb(user["role"]),
            )
            if ADMIN_CHAT_ID:
                try:
                    role_s = ROLE_SHORT.get(user["role"], user["role"])
                    await q.get_bot().send_message(
                        ADMIN_CHAT_ID,
                        f"🟢 Смена открыта\n👤 {user['name']} ({role_s})\n"
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
            reply_markup=kb([("Сменить роль в смене", "sr_shift"), ("Назад", "back_main")], cols=1),
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
        set_user(tid, user); ctx.user_data["user"] = user; s["role"] = role
        await q.edit_message_text(f"Роль изменена: {ROLE_LABELS[role]}", reply_markup=main_kb(role))
        return

    if data == "back_main":
        if not s.get("branch"):
            await q.edit_message_text("Выберите действие:",
                reply_markup=kb([("Начать смену", "start_shift"), ("Профиль", "go_profile")], cols=1))
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
        title = "Ставки / подготовка:" if data == "add_rate" else "Процедуры:"
        if not items:
            await q.edit_message_text("Нет позиций.", reply_markup=kb([("Назад", "back_main")]))
            return
        ctx.user_data["cur_items"] = items; ctx.user_data["cur_prefix"] = prefix
        await q.edit_message_text(title,
            reply_markup=kb(_item_buttons(items, prefix, s) + [("Назад", "back_main")], cols=1))
        return

    if data == "cancel_sale":
        ctx.user_data.pop("waiting_sale", None)
        await q.edit_message_text(
            f"👤 {s.get('name', '')} | {s.get('branch', '')} | {s.get('date', '')}",
            reply_markup=main_kb(s.get("role", "")),
        )
        return

    if data == "add_sale":
        if not s.get("branch"):
            await q.edit_message_text("Смена не открыта. Нажмите /start")
            return
        ctx.user_data["waiting_sale"] = True
        await q.edit_message_text(
            "Доп продажа\n\n"
            "Введите сумму продажи в рублях — получите 10% комиссию.\n\n"
            "Пример: 5000",
            reply_markup=kb([("Назад", "cancel_sale")]),
        )
        return

    if data.startswith("r_") or data.startswith("p_"):
        prefix, idx = data.split("_", 1)[0], int(data.split("_", 1)[1])
        items = ctx.user_data.get("cur_items", [])
        if idx >= len(items):
            return
        name, price = items[idx]
        ctx.user_data["cur_name"] = name; ctx.user_data["cur_price"] = price
        ctx.user_data["cur_prefix"] = prefix
        bucket = s.get("ставки", {}) if prefix == "r" else s.get("процедуры", {})
        cur_qty = bucket.get(name, 0)
        cur_str = f" (сейчас: {_qty_str(cur_qty)})" if cur_qty else ""
        await q.edit_message_text(
            f"{name} — {price} руб{cur_str}\n\nВыберите количество:",
            reply_markup=kb([("+1","qty_p1"),("+0.5","qty_p05"),
                             ("-1","qty_m1"),("-0.5","qty_m05"),("К списку","qty_back")], cols=2),
        )
        return

    if data.startswith("qty_"):
        if data == "qty_back":
            items = ctx.user_data.get("cur_items", [])
            prefix = ctx.user_data.get("cur_prefix", "r")
            title = "Ставки / подготовка:" if prefix == "r" else "Процедуры:"
            await q.edit_message_text(title,
                reply_markup=kb(_item_buttons(items, prefix, s) + [("Назад", "back_main")], cols=1))
            return
        name  = ctx.user_data.get("cur_name", "")
        price = ctx.user_data.get("cur_price", 0)
        prefix = ctx.user_data.get("cur_prefix", "r")
        if not name:
            await q.edit_message_text("Назад", reply_markup=kb([("Назад", "back_main")]))
            return
        bucket = s.get("ставки", {}) if prefix == "r" else s.get("процедуры", {})
        delta = {"qty_p1": 1.0, "qty_p05": 0.5, "qty_m1": -1.0, "qty_m05": -0.5}.get(data, 0)
        new_qty = max(0.0, bucket.get(name, 0) + delta)
        if new_qty == 0:
            bucket.pop(name, None)
        else:
            bucket[name] = new_qty
        await q.edit_message_text(
            f"{name}\nКоличество: {_qty_str(new_qty)} -> {int(price * new_qty)} руб\n\nВыберите количество:",
            reply_markup=kb([("+1","qty_p1"),("+0.5","qty_p05"),
                             ("-1","qty_m1"),("-0.5","qty_m05"),("К списку","qty_back")], cols=2),
        )
        return

    if data == "close":
        if not s.get("branch"):
            await q.edit_message_text("Смена не открыта. Нажмите /start")
            return
        total_items = len(s.get("ставки", {})) + len(s.get("процедуры", {})) + len(s.get("доп_продажи", []))
        warning = "" if total_items > 0 else "\n\nСтавки/процедуры не добавлены!"
        await q.edit_message_text(
            fmt(s, final=True) + warning + "\n\nПодтвердить закрытие?",
            reply_markup=kb([("Да, закрыть", "do_close"), ("Назад", "back_main")]),
        )
        return

    if data == "do_close":
        if not s.get("branch"):
            await q.edit_message_text("Смена не открыта.")
            return
        s["end_time"] = now_msk().strftime("%H:%M")
        summary = fmt(s, final=True) + f"\nКонец: {s['end_time']} МСК"
        await q.edit_message_text(summary + "\n\nСмена закрыта. Спасибо!")
        write_shift(s)
        ACTIVE_SHIFTS.pop(tid, None)
        today = s.get("date", now_msk().strftime("%d.%m.%Y"))
        wname = s.get("name", "")
        if wname:
            if today not in TODAY_WORKED_NAMES:
                TODAY_WORKED_NAMES[today] = set()
            TODAY_WORKED_NAMES[today].add(wname)
        if ADMIN_CHAT_ID:
            try:
                await q.get_bot().send_message(ADMIN_CHAT_ID, f"🔴 Смена закрыта\n\n{summary}")
            except Exception as e:
                logger.error(f"Уведомление о закрытии: {e}")
        user = ctx.user_data.get("user") or get_user(tid)
        ctx.user_data.clear()
        if user:
            ctx.user_data["user"] = user
        await q.get_bot().send_message(
            q.message.chat_id, "Хотите начать новую смену?",
            reply_markup=kb([("Новая смена", "start_shift"), ("Профиль", "go_profile")], cols=1),
        )
        return

    logger.warning(f"[cb] необработанный data={data!r}")

# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    sync_users_from_sheets()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("reset",    cmd_reset))
    app.add_handler(CommandHandler("myid",     cmd_myid))
    app.add_handler(CommandHandler("report",   cmd_report))
    app.add_handler(CommandHandler("salary",   cmd_salary))
    app.add_handler(CommandHandler("stats",    cmd_stats))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def err_handler(update, ctx):
        logger.error(f"[GLOBAL ERROR] {ctx.error}", exc_info=ctx.error)
    app.add_error_handler(err_handler)

    webhook_url = os.getenv("WEBHOOK_URL", "")
    port = int(os.getenv("PORT", "8080"))
    logger.info("Бот Сансара v5.2 запускается...")
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
