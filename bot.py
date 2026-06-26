#!/usr/bin/env python3
"""
Сансара — бот учёта смен и процедур
Запуск: python bot.py
"""
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = "8960632739:AAFnJBzn-89ctfWOiImkk24bbjpTWZqGZPk"

# Telegram chat_id администратора.
# Узнать свой ID: написать боту @userinfobot
# После итога смены туда придёт копия.
ADMIN_CHAT_ID = None  # например: 123456789

# ─────────────────────────── СОСТОЯНИЯ ────────────────────────────
CHOOSE_ROLE, CHOOSE_NAME, CHOOSE_BRANCH, MAIN_MENU, \
    ADD_ITEM, CONFIRM_CLOSE = range(6)

# ───────────────────────────── ПЕРСОНАЛ ───────────────────────────
MASTERS = [
    "Анна", "Антон", "Юлия К", "Игорь",
    "Михаил", "Станислав", "Александр", "Сергей", "Денис",
]
TEHPERS = ["Елена Х", "Мария", "Оля"]

# ─────────────────────────── СПРАВОЧНИК ───────────────────────────
CATALOGUE = {
    "мастер": {
        "Ирий": {
            "ставки": [
                ("Смена М1",             1500),
                ("Смена М2",             2200),
                ("Сенная парная",         200),
                ("Самовар",               150),
                ("Чан 1-й",               450),
                ("Чан 2-й",               300),
                ("Чан 3-й",               300),
                ("Подготовка 1-4",        250),
                ("Подготовка 5-6",        300),
                ("Подготовка 7-10",       400),
                ("Ранний выход",          400),
            ],
            "процедуры": [
                ("Первый пар",            150),
                ("Арома Медитация",       210),
                ("Колл Пар крыло",        300),
                ("Колл Пар лед и пламя",  360),
                ("Парение",              1140),
                ("Парение в 4 руки",     1000),
                ("Спа церемония",         600),
                ("Догрев",                480),
                ("Доп Парение",          1500),
                ("Доп спа",               900),
                ("Дет. Пар",              500),
                ("Дет. Спа",              500),
                ("Слияние душ",          2400),
                ("Царевич",              2200),
                ("ИП СХ",               2000),
                ("Массаж 60",            1750),
                ("Массаж 45",            1400),
                ("Массаж 30",             900),
            ],
        },
        "Правь": {
            "ставки": [
                ("Смена М1",             1500),
                ("Смена М2",             2200),
                ("Самовар",               150),
                ("Подготовка 1-4",        250),
                ("Подготовка 5-6",        300),
                ("Подготовка 7-10",       400),
                ("Ранний выход",          400),
            ],
            "процедуры": [
                ("Арома Медитация",       150),
                ("Колл Пар",             210),
                ("Парение",               750),
                ("Спа церемония",         750),
                ("Догрев",                480),
                ("Дет. Пар",              500),
                ("Дет. Спа",              500),
                ("Доп Парение",           900),
                ("Доп спа",               900),
                ("Слияние душ",          2400),
                ("Массаж 60",            1750),
                ("Массаж 45",            1400),
                ("Массаж 30",             900),
            ],
        },
    },
    "техпер": {
        "Ирий": {
            "ставки": [
                ("Смена тех.пер",        2200),
                ("Сенная парная",         200),
                ("Самовар",               150),
                ("Чан 1-й",               450),
                ("Чан 2-й",               300),
                ("Чан 3-й",               300),
                ("Подготовка 1-4",        250),
                ("Подготовка 5-6",        300),
                ("Подготовка 7-10",       400),
                ("Ранний выход",          400),
            ],
        },
        "Правь": {
            "ставки": [
                ("Смена тех.пер",        2200),
                ("Самовар",               150),
                ("Подготовка 1-4",        250),
                ("Подготовка 5-6",        300),
                ("Подготовка 7-10",       400),
                ("Ранний выход",          400),
            ],
        },
    },
}

# ─────────────────────────── СЕССИЯ ───────────────────────────────
def new_session():
    return {
        "role": None, "name": None, "branch": None,
        "date": datetime.now().strftime("%d.%m.%Y"),
        "ставки": {},     # {name: count}
        "процедуры": {},  # {name: count}
    }

def sess(ctx) -> dict:
    if "s" not in ctx.user_data:
        ctx.user_data["s"] = new_session()
    return ctx.user_data["s"]

def reset(ctx):
    ctx.user_data["s"] = new_session()

# ─────────────────────────── КЛАВИАТУРЫ ───────────────────────────
def kb(buttons: list[tuple], cols=2) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(buttons), cols):
        rows.append([InlineKeyboardButton(t, callback_data=d)
                     for t, d in buttons[i:i+cols]])
    return InlineKeyboardMarkup(rows)

def main_kb(role):
    btns = [("➕ Ставки / подготовка", "add_rate")]
    if role == "мастер":
        btns.append(("➕ Процедуры", "add_proc"))
    btns += [("📊 Мой итог", "show"), ("✅ Закрыть смену", "close")]
    return kb(btns, cols=2)

# ─────────────────────────── ФОРМАТИРОВАНИЕ ───────────────────────
def fmt(s: dict, final=False) -> str:
    role_label = "Пармастер" if s["role"] == "мастер" else "Тех.Пер"
    cat = CATALOGUE[s["role"]][s["branch"]]
    lines = [
        f"{'📋 ИТОГ СМЕНЫ' if final else '📊 Текущий итог'}",
        f"👤 {s['name']} ({role_label})",
        f"🏠 {s['branch']}  |  📅 {s['date']}",
        "─" * 28,
    ]
    total = 0
    rates_dict = dict(cat["ставки"])

    if s["ставки"]:
        lines.append("📌 Ставки / подготовка:")
        for name, qty in s["ставки"].items():
            price = rates_dict.get(name, 0)
            amt = price * qty
            total += amt
            lines.append(f"  {name}: {qty} × {price:,} = {amt:,} ₽")

    if s["процедуры"]:
        procs_dict = dict(cat.get("процедуры", []))
        lines.append("🔥 Процедуры:")
        for name, qty in s["процедуры"].items():
            price = procs_dict.get(name, 0)
            amt = price * qty
            total += amt
            lines.append(f"  {name}: {qty} × {price:,} = {amt:,} ₽")

    lines += ["─" * 28, f"💰 ИТОГО: {total:,} ₽"]
    return "\n".join(lines)

# ─────────────────────────── ХЭНДЛЕРЫ ─────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reset(ctx)
    await update.message.reply_text(
        "👋 Добро пожаловать!\nВыберите роль:",
        reply_markup=kb([
            ("👨‍🍳 Пармастер",   "role_мастер"),
            ("🧹 Тех.Персонал", "role_техпер"),
        ]),
    )
    return CHOOSE_ROLE

async def on_role(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    s = sess(ctx)
    s["role"] = q.data.replace("role_", "")
    names = MASTERS if s["role"] == "мастер" else TEHPERS
    await q.edit_message_text(
        "Выберите имя:",
        reply_markup=kb([(n, f"name_{i}") for i, n in enumerate(names)], cols=2),
    )
    ctx.user_data["names"] = names
    return CHOOSE_NAME

async def on_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    idx = int(q.data.replace("name_", ""))
    s = sess(ctx)
    s["name"] = ctx.user_data["names"][idx]
    await q.edit_message_text(
        f"Привет, {s['name']}! Выберите филиал:",
        reply_markup=kb([("🏠 Ирий", "br_Ирий"), ("🏠 Правь", "br_Правь")]),
    )
    return CHOOSE_BRANCH

async def on_branch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    s = sess(ctx)
    s["branch"] = q.data.replace("br_", "")
    role_label = "Пармастер" if s["role"] == "мастер" else "Тех.Пер"
    await q.edit_message_text(
        f"✅ {s['name']} · {role_label} · {s['branch']} · {s['date']}\n\n"
        f"Смена открыта! Добавляйте по мере работы.",
        reply_markup=main_kb(s["role"]),
    )
    return MAIN_MENU

async def on_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    s = sess(ctx)

    if q.data == "show":
        await q.edit_message_text(fmt(s), reply_markup=main_kb(s["role"]))
        return MAIN_MENU

    if q.data == "close":
        await q.edit_message_text(
            fmt(s, final=True) + "\n\n⚠️ Подтвердить закрытие смены?",
            reply_markup=kb([
                ("✅ Да, закрыть", "do_close"),
                ("↩️ Назад",        "back"),
            ]),
        )
        return CONFIRM_CLOSE

    # Показать список для добавления
    cat = CATALOGUE[s["role"]][s["branch"]]
    if q.data == "add_rate":
        items = cat["ставки"]
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
        cnt = bucket.get(name, 0)
        mark = "✅ " if cnt else ""
        cnt_str = f"  [{cnt}]" if cnt else ""
        btns.append((f"{mark}{name}  +{price:,}₽{cnt_str}", f"{prefix}_{i}"))
    return btns

async def on_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    s = sess(ctx)

    if q.data == "back":
        await q.edit_message_text(
            f"✅ {s['name']} · {s['branch']} · {s['date']}",
            reply_markup=main_kb(s["role"]),
        )
        return MAIN_MENU

    prefix, idx_str = q.data.split("_", 1)
    idx = int(idx_str)
    items = ctx.user_data["cur_items"]
    name, price = items[idx]

    bucket = s["ставки"] if prefix == "r" else s["процедуры"]
    bucket[name] = bucket.get(name, 0) + 1
    cnt = bucket[name]

    btns = _item_buttons(items, prefix, s)
    btns.append(("↩️ Назад", "back"))
    title = "Ставки / подготовка:" if prefix == "r" else "Процедуры:"
    await q.edit_message_text(
        f"✅ {name} → {cnt} шт. ({price * cnt:,} ₽)\n\n{title}",
        reply_markup=kb(btns, cols=1),
    )
    return ADD_ITEM

async def on_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    s = sess(ctx)

    if q.data == "back":
        await q.edit_message_text(
            f"✅ {s['name']} · {s['branch']} · {s['date']}",
            reply_markup=main_kb(s["role"]),
        )
        return MAIN_MENU

    # Закрываем смену
    summary = fmt(s, final=True)
    await q.edit_message_text(summary + "\n\n✅ Смена закрыта. Спасибо!")

    if ADMIN_CHAT_ID:
        await ctx.bot.send_message(chat_id=ADMIN_CHAT_ID, text=summary)

    reset(ctx)
    await ctx.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Начать новую смену?",
        reply_markup=kb([("🔄 Новая смена", "new")]),
    )
    return ConversationHandler.END

async def on_new_shift(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    reset(ctx)
    await q.edit_message_text(
        "Выберите роль:",
        reply_markup=kb([
            ("👨‍🍳 Пармастер",   "role_мастер"),
            ("🧹 Тех.Персонал", "role_техпер"),
        ]),
    )
    return CHOOSE_ROLE

# ──────────────────────────── MAIN ────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CallbackQueryHandler(on_new_shift, pattern="^new$"),
        ],
        states={
            CHOOSE_ROLE:   [CallbackQueryHandler(on_role,    pattern="^role_")],
            CHOOSE_NAME:   [CallbackQueryHandler(on_name,    pattern="^name_")],
            CHOOSE_BRANCH: [CallbackQueryHandler(on_branch,  pattern="^br_")],
            MAIN_MENU:     [CallbackQueryHandler(on_main,    pattern="^(show|close|add_rate|add_proc)$")],
            ADD_ITEM:      [CallbackQueryHandler(on_add,     pattern="^(r_|p_|back)")],
            CONFIRM_CLOSE: [CallbackQueryHandler(on_confirm, pattern="^(do_close|back)$")],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(conv)
    logger.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
