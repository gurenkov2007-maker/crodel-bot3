import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    PicklePersistence,
)
from datetime import datetime
from pathlib import Path
import sheets
import content

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Состояния диалога
(
    MAIN_MENU,
    VIEWING_LESSON,
    QUIZ_CONFIRM,
    QUIZ_CHOICE_QUESTION,
    QUIZ_OPEN_QUESTION,
) = range(5)

ADMIN_ID = int(os.environ.get("ADMIN_ID", "1305122122"))
MAX_MSG_LEN = 4096
LOGO_PATH = next(
    (p for p in [Path("logo.png"), Path("logo.jpg"), Path("logo.webp")] if p.exists()),
    None,
)


# ─────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────

def get_user_progress(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Возвращает прогресс пользователя, инициализируя при первом вызове."""
    if "progress" not in context.user_data:
        context.user_data["progress"] = {
            "current_lesson": 0,
            "quiz_answers": {},
            "quiz_scores": {},
        }
    return context.user_data["progress"]


def progress_bar(done: int, total: int, length: int = 10) -> str:
    """Визуальный прогресс-бар: ▓▓▓▓░░░░░░"""
    if total == 0:
        return "░" * length
    filled = round(done / total * length)
    return "▓" * filled + "░" * (length - filled)


def score_emoji(score: int, total: int) -> str:
    """Эмодзи-оценка результата."""
    if total == 0:
        return ""
    pct = score / total * 100
    if pct == 100:
        return "🏆"
    elif pct >= 80:
        return "🌟"
    elif pct >= 50:
        return "👍"
    else:
        return "📚"


async def safe_edit_or_send(query, text, context, reply_markup=None, parse_mode="HTML"):
    """Try edit_message_text, fallback to send_message if message is a photo."""
    try:
        await query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode,
        )
    except BadRequest:
        await context.bot.send_message(
            query.message.chat_id, text,
            reply_markup=reply_markup, parse_mode=parse_mode,
        )


def lessons_keyboard(current_lesson: int, progress: dict) -> InlineKeyboardMarkup:
    """Клавиатура для навигации по урокам."""
    total = len(content.LESSONS)
    buttons = []

    nav = []
    if current_lesson > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"lesson_{current_lesson - 1}"))
    if current_lesson < total - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"lesson_{current_lesson + 1}"))
    if nav:
        buttons.append(nav)

    score_info = progress.get("quiz_scores", {}).get(current_lesson)
    if score_info is not None:
        quiz_label = f"🔄 Пересдать тест ({score_info['score']}/{score_info['total']})"
    else:
        quiz_label = "📝 Пройти тест"
    buttons.append([InlineKeyboardButton(quiz_label, callback_data=f"start_quiz_{current_lesson}")])
    buttons.append([InlineKeyboardButton("🏠 Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(buttons)


def main_menu_keyboard(progress: dict, user_id: int = 0) -> InlineKeyboardMarkup:
    total = len(content.LESSONS)
    scores = progress.get("quiz_scores", {})
    done = len(scores)
    buttons = []
    for i, lesson in enumerate(content.LESSONS):
        score_info = scores.get(i)
        num = f"{i+1:>2}"
        if score_info is not None:
            emoji = score_emoji(score_info["score"], score_info["total"])
            label = f"{emoji} {num}. {lesson['title']}  [{score_info['score']}/{score_info['total']}]"
        else:
            label = f"📖 {num}. {lesson['title']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"lesson_{i}")])

    bar = progress_bar(done, total)
    buttons.append([InlineKeyboardButton(f"📊 Прогресс {bar} {done}/{total}", callback_data="my_progress")])
    buttons.append([InlineKeyboardButton("❓ Помощь", callback_data="help")])

    if user_id == ADMIN_ID:
        buttons.append([
            InlineKeyboardButton("👨‍💼 Админ-панель", callback_data="admin_panel"),
            InlineKeyboardButton("📋 Отчёт", callback_data="admin_report"),
        ])
    return InlineKeyboardMarkup(buttons)


async def send_long_text(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE,
                         reply_markup=None, parse_mode="HTML"):
    """Отправляет длинный текст, разбивая на части по абзацам."""
    if len(text) <= MAX_MSG_LEN:
        await context.bot.send_message(
            chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode
        )
        return

    parts = []
    current = ""
    for paragraph in text.split("\n"):
        if len(current) + len(paragraph) + 1 > MAX_MSG_LEN - 50:
            parts.append(current)
            current = paragraph
        else:
            current = current + "\n" + paragraph if current else paragraph
    if current:
        parts.append(current)

    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        await context.bot.send_message(
            chat_id,
            part,
            reply_markup=reply_markup if is_last else None,
            parse_mode=parse_mode,
        )


# ─────────────────────────────────────────────
# Хендлеры
# ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    progress = get_user_progress(context)
    user = update.effective_user
    done = len(progress.get("quiz_scores", {}))
    total = len(content.LESSONS)

    # Трекаем активность для напоминаний
    context.user_data["user_id"] = user.id
    context.user_data["username"] = user.username or user.first_name
    context.user_data["first_name"] = user.first_name
    context.user_data["last_activity"] = datetime.now().isoformat()

    welcome = (
        f"👋 Привет, {user.first_name}!\n\n"
        "Добро пожаловать в <b>CRODEL Training Bot</b> — "
        "курс обучения бизнес-ассистентов.\n\n"
        f"📚 Уроков: <b>{total}</b>\n"
        f"✅ Пройдено: <b>{done}/{total}</b>\n\n"
        "Выбери урок, чтобы начать:"
    )
    if LOGO_PATH:
        await update.message.reply_photo(photo=open(LOGO_PATH, "rb"))
    await update.message.reply_text(
        welcome,
        reply_markup=main_menu_keyboard(progress, user.id),
        parse_mode="HTML",
    )
    return MAIN_MENU


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка команды /help."""
    text = (
        "📖 <b>Как пользоваться ботом</b>\n"
        + "─" * 30 + "\n\n"
        "🔹 <b>/start</b> — главное меню\n"
        "🔹 <b>/help</b> — эта справка\n"
        "🔹 <b>/reset</b> — сбросить весь прогресс\n\n"
        "📚 <b>Уроки:</b> выбери урок в меню → прочитай материал → "
        "пройди тест.\n\n"
        "📝 <b>Тесты:</b> вопросы с выбором ответа + открытые вопросы. "
        "Результаты сохраняются, можно пересдавать.\n\n"
        "📊 <b>Прогресс:</b> отслеживай свои результаты в меню."
    )
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Меню", callback_data="main_menu")]
            ]),
            parse_mode="HTML",
        )
    elif update.callback_query:
        await update.callback_query.answer()
        await safe_edit_or_send(
            update.callback_query, text, context,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Меню", callback_data="main_menu")]
            ]),
        )
    return MAIN_MENU


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сброс прогресса пользователя."""
    context.user_data.pop("progress", None)
    await update.message.reply_text(
        "🔄 Прогресс сброшен. Начинай заново!\n\nНажми /start чтобы продолжить.",
    )
    return ConversationHandler.END


async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    progress = get_user_progress(context)
    done = len(progress.get("quiz_scores", {}))
    total = len(content.LESSONS)
    await safe_edit_or_send(
        query,
        f"📚 <b>Главное меню</b>\n"
        f"Пройдено: {done}/{total}\n\n"
        f"Выбери урок:",
        context,
        reply_markup=main_menu_keyboard(progress, update.effective_user.id),
    )
    return MAIN_MENU


async def show_lesson(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    lesson_idx = int(query.data.split("_")[1])
    progress = get_user_progress(context)
    progress["current_lesson"] = lesson_idx

    lesson = content.LESSONS[lesson_idx]
    total = len(content.LESSONS)

    header = (
        f"📚 <b>Урок {lesson_idx + 1} из {total}</b>\n"
        f"<b>{lesson['title']}</b>\n"
        + "─" * 30 + "\n\n"
    )

    text = header + lesson["text"]
    keyboard = lessons_keyboard(lesson_idx, progress)

    if len(text) <= MAX_MSG_LEN:
        await safe_edit_or_send(query, text, context, reply_markup=keyboard)
    else:
        try:
            await query.delete_message()
        except Exception:
            pass
        await send_long_text(
            update.effective_chat.id, text, context, reply_markup=keyboard
        )

    return VIEWING_LESSON


async def my_progress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    progress = get_user_progress(context)
    scores = progress.get("quiz_scores", {})
    total_lessons = len(content.LESSONS)
    done = len(scores)

    total_score = sum(s["score"] for s in scores.values())
    total_questions = sum(s["total"] for s in scores.values())

    lines = [
        "📊 <b>Твой прогресс</b>",
        f"{progress_bar(done, total_lessons, 14)}  {done}/{total_lessons} уроков",
        "",
    ]
    for i, lesson in enumerate(content.LESSONS):
        if i in scores:
            s = scores[i]
            emoji = score_emoji(s["score"], s["total"])
            lines.append(f"  {emoji} {i+1}. {lesson['title']} — <b>{s['score']}/{s['total']}</b>")
        else:
            lines.append(f"  ⭕ {i+1}. {lesson['title']}")

    lines.append("")
    if total_questions > 0:
        overall_pct = round(total_score / total_questions * 100)
        lines.append(f"<b>Общий балл: {total_score}/{total_questions} ({overall_pct}%)</b>")
    else:
        lines.append("<b>Пока нет результатов</b>")

    await safe_edit_or_send(
        query, "\n".join(lines), context,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Меню", callback_data="main_menu")]
        ]),
    )
    return MAIN_MENU


# ─────────────────────────────────────────────
# Викторина
# ─────────────────────────────────────────────

async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    lesson_idx = int(query.data.split("_")[2])
    lesson = content.LESSONS[lesson_idx]
    questions = lesson.get("questions", [])

    if not questions:
        await safe_edit_or_send(
            query, "❗ Для этого урока ещё нет вопросов.", context,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Меню", callback_data="main_menu")]
            ]),
        )
        return MAIN_MENU

    choice_count = sum(1 for q in questions if q["type"] == "choice")
    open_count = sum(1 for q in questions if q["type"] == "open")

    info_parts = []
    if choice_count:
        info_parts.append(f"с выбором: {choice_count}")
    if open_count:
        info_parts.append(f"открытых: {open_count}")

    progress = get_user_progress(context)
    score_info = progress.get("quiz_scores", {}).get(lesson_idx)
    prev_result = ""
    if score_info is not None:
        prev_result = f"\n\n📊 Предыдущий результат: <b>{score_info['score']}/{score_info['total']}</b>"

    await safe_edit_or_send(
        query,
        f"📝 <b>Тест: {lesson['title']}</b>\n\n"
        f"Вопросов: <b>{len(questions)}</b> ({', '.join(info_parts)})"
        f"{prev_result}\n\n"
        f"Готов начать?",
        context,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶ Начать тест", callback_data=f"confirm_quiz_{lesson_idx}")],
            [InlineKeyboardButton("◀ Назад к уроку", callback_data=f"lesson_{lesson_idx}")],
        ]),
    )
    return QUIZ_CONFIRM


async def confirm_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    lesson_idx = int(query.data.split("_")[2])
    progress = get_user_progress(context)
    progress["current_lesson"] = lesson_idx
    progress["current_quiz_q"] = 0
    progress["current_quiz_answers"] = []

    lesson = content.LESSONS[lesson_idx]
    questions = lesson.get("questions", [])
    context.user_data["quiz_questions"] = questions
    return await ask_question(update, context, question_idx=0, edit=True)


async def ask_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    question_idx: int,
    edit: bool = False,
) -> int:
    questions = context.user_data["quiz_questions"]
    progress = get_user_progress(context)
    lesson_idx = progress["current_lesson"]
    lesson = content.LESSONS[lesson_idx]
    total_q = len(questions)
    q = questions[question_idx]

    bar = progress_bar(question_idx, total_q, 8)
    header = (
        f"📝 <b>{lesson['title']}</b>\n"
        f"Вопрос {question_idx + 1}/{total_q}  {bar}\n"
        + "─" * 28 + "\n\n"
        f"{q['question']}"
    )

    if q["type"] == "choice":
        letters = ["A", "B", "C", "D", "E", "F"]
        buttons = [
            [InlineKeyboardButton(f"{letters[i]}. {opt}", callback_data=f"answer_{i}")]
            for i, opt in enumerate(q["options"])
        ]
        markup = InlineKeyboardMarkup(buttons)

        if edit and update.callback_query:
            await safe_edit_or_send(update.callback_query, header, context, reply_markup=markup)
        else:
            chat_id = update.effective_chat.id
            await context.bot.send_message(chat_id, header, reply_markup=markup, parse_mode="HTML")

        return QUIZ_CHOICE_QUESTION

    else:  # open
        prompt = header + "\n\n✏️ <i>Напиши свой ответ в чат:</i>"
        if edit and update.callback_query:
            await safe_edit_or_send(update.callback_query, prompt, context)
        else:
            await context.bot.send_message(update.effective_chat.id, prompt, parse_mode="HTML")

        return QUIZ_OPEN_QUESTION


async def handle_choice_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    answer_idx = int(query.data.split("_")[1])
    questions = context.user_data["quiz_questions"]
    progress = get_user_progress(context)
    q_idx = progress["current_quiz_q"]
    q = questions[q_idx]

    chosen_text = q["options"][answer_idx]
    is_correct = (answer_idx == q["correct"])

    progress["current_quiz_answers"].append({
        "question": q["question"],
        "type": "choice",
        "answer": chosen_text,
        "correct": is_correct,
    })

    if is_correct:
        feedback = "✅ <b>Верно!</b>"
    else:
        feedback = f"❌ <b>Неверно.</b>\nПравильный ответ: <b>{q['options'][q['correct']]}</b>"
    if q.get("explanation"):
        feedback += f"\n\n💡 <i>{q['explanation']}</i>"

    await safe_edit_or_send(query, feedback, context)

    progress["current_quiz_q"] = q_idx + 1
    return await next_question_or_finish(update, context)


async def handle_open_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_text = update.message.text.strip()
    questions = context.user_data["quiz_questions"]
    progress = get_user_progress(context)
    q_idx = progress["current_quiz_q"]
    q = questions[q_idx]

    progress["current_quiz_answers"].append({
        "question": q["question"],
        "type": "open",
        "answer": user_text,
        "correct": None,
    })

    await update.message.reply_text(
        "📨 <b>Ответ принят!</b> Преподаватель проверит его позже.",
        parse_mode="HTML",
    )

    progress["current_quiz_q"] = q_idx + 1
    return await next_question_or_finish(update, context)


async def next_question_or_finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    questions = context.user_data["quiz_questions"]
    progress = get_user_progress(context)
    q_idx = progress["current_quiz_q"]

    if q_idx < len(questions):
        return await ask_question(update, context, question_idx=q_idx, edit=False)
    else:
        return await finish_quiz(update, context)


async def finish_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    progress = get_user_progress(context)
    context.user_data["last_activity"] = datetime.now().isoformat()
    lesson_idx = progress["current_lesson"]
    lesson = content.LESSONS[lesson_idx]
    answers = progress["current_quiz_answers"]

    choice_answers = [a for a in answers if a["type"] == "choice"]
    correct = sum(1 for a in choice_answers if a["correct"])
    total_choice = len(choice_answers)
    open_count = len([a for a in answers if a["type"] == "open"])

    progress["quiz_scores"][lesson_idx] = {
        "score": correct,
        "total": total_choice,
        "open_count": open_count,
        "timestamp": datetime.now().isoformat(),
    }

    user = update.effective_user

    # Сохраняем в Google Sheets
    try:
        sheets.save_result(
            user_id=user.id,
            username=user.username or user.first_name,
            lesson_title=lesson["title"],
            lesson_idx=lesson_idx + 1,
            score=correct,
            total=total_choice,
            open_count=open_count,
            answers=answers,
        )
    except Exception as e:
        logger.error(f"Ошибка сохранения в Sheets: {e}")

    # Уведомление администратору
    try:
        summary = build_admin_summary(user, lesson, lesson_idx, correct, total_choice, answers)
        await context.bot.send_message(ADMIN_ID, summary, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка уведомления админа: {e}")

    # Итоговое сообщение пользователю
    emoji = score_emoji(correct, total_choice)
    bar = progress_bar(correct, total_choice, 10) if total_choice > 0 else ""

    result_text = f"🏁 <b>Тест завершён!</b>\n\n"
    result_text += f"📚 {lesson['title']}\n"

    if total_choice > 0:
        pct = round(correct / total_choice * 100)
        result_text += f"\n{bar}  {emoji}\n"
        result_text += f"<b>{correct}/{total_choice}</b> правильных ({pct}%)\n"
        if pct == 100:
            result_text += "\n🎉 Отличный результат!"
        elif pct >= 80:
            result_text += "\n👏 Хорошо! Почти идеально."
        elif pct >= 50:
            result_text += "\n💪 Неплохо, но есть над чем поработать."
        else:
            result_text += "\n📖 Рекомендую перечитать урок и попробовать ещё раз."

    if open_count > 0:
        result_text += f"\n\n📝 Открытых ответов: <b>{open_count}</b> (проверит преподаватель)"

    result_text += "\n\n✅ Результаты сохранены!"

    chat_id = update.effective_chat.id

    # Кнопки после теста
    next_lesson = lesson_idx + 1
    after_buttons = []
    if next_lesson < len(content.LESSONS):
        after_buttons.append([InlineKeyboardButton(
            f"▶ Следующий урок: {content.LESSONS[next_lesson]['title']}",
            callback_data=f"lesson_{next_lesson}",
        )])
    after_buttons.append([
        InlineKeyboardButton("🔄 Пересдать", callback_data=f"start_quiz_{lesson_idx}"),
        InlineKeyboardButton("🏠 Меню", callback_data="main_menu"),
    ])

    await context.bot.send_message(
        chat_id,
        result_text,
        reply_markup=InlineKeyboardMarkup(after_buttons),
        parse_mode="HTML",
    )
    return MAIN_MENU


def build_admin_summary(user, lesson, lesson_idx, correct, total_choice, answers) -> str:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    pct = f" ({round(correct / total_choice * 100)}%)" if total_choice > 0 else ""
    lines = [
        f"📬 <b>Новый результат теста</b>",
        f"👤 {user.first_name} (@{user.username or '—'}, ID: {user.id})",
        f"📚 Урок {lesson_idx + 1}: {lesson['title']}",
        f"📊 Результат: <b>{correct}/{total_choice}</b>{pct}",
        f"🕐 {now}",
        "",
    ]
    for i, a in enumerate(answers, 1):
        mark = "✅" if a["correct"] is True else ("❌" if a["correct"] is False else "📝")
        lines.append(f"{mark} <b>Q{i}:</b> {a['question'][:80]}")
        lines.append(f"    → {a['answer'][:200]}")
    return "\n".join(lines)


async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка неожиданных текстовых сообщений."""
    await update.message.reply_text(
        "🤔 Не понимаю. Используй кнопки для навигации или нажми /start",
    )
    return MAIN_MENU


# ─────────────────────────────────────────────
# Админ-команды
# ─────────────────────────────────────────────

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Панель администратора: статистика по ученикам."""
    query = update.callback_query
    if query:
        await query.answer()

    chat_id = update.effective_chat.id

    if update.effective_user.id != ADMIN_ID:
        await context.bot.send_message(chat_id, "⛔ Доступ только для администратора.")
        return MAIN_MENU

    user_data = context.application.user_data
    if not user_data:
        await context.bot.send_message(chat_id, "📭 Пока нет учеников.")
        return MAIN_MENU

    total_lessons = len(content.LESSONS)
    students = []
    for uid, data in user_data.items():
        name = data.get("first_name", data.get("username", f"ID:{uid}"))
        username = data.get("username", "—")
        progress = data.get("progress", {})
        scores = progress.get("quiz_scores", {})
        done = len(scores)
        last = data.get("last_activity", "—")
        if isinstance(last, str) and last != "—":
            try:
                last = datetime.fromisoformat(last).strftime("%d.%m %H:%M")
            except Exception:
                pass
        students.append((name, username, done, scores, last))

    lines = [
        f"👨‍💼 <b>Панель администратора</b>",
        f"👥 Учеников: <b>{len(students)}</b>",
        f"📚 Уроков в курсе: <b>{total_lessons}</b>",
        "",
    ]
    for name, username, done, scores, last in students:
        total_score = sum(s["score"] for s in scores.values())
        total_q = sum(s["total"] for s in scores.values())
        avg = f"{round(total_score / total_q * 100)}%" if total_q > 0 else "—"
        lines.append(
            f"👤 <b>{name}</b> (@{username})\n"
            f"   📊 Пройдено: {done}/{total_lessons} | Средний балл: {avg}\n"
            f"   🕐 Последняя активность: {last}"
        )
        lines.append("")

    lines.append("\n🏠 /start — вернуться в меню")

    await send_long_text(
        update.effective_chat.id, "\n".join(lines), context, parse_mode="HTML"
    )
    return MAIN_MENU


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Итоговый отчёт по всем ученикам: кто прошёл, баллы, слабые места."""
    query = update.callback_query
    if query:
        await query.answer()

    chat_id = update.effective_chat.id

    if update.effective_user.id != ADMIN_ID:
        await context.bot.send_message(chat_id, "⛔ Доступ только для администратора.")
        return MAIN_MENU

    user_data = context.application.user_data
    if not user_data:
        await context.bot.send_message(chat_id, "📭 Пока нет учеников.")
        return MAIN_MENU

    total_lessons = len(content.LESSONS)
    completed = []    # прошли все уроки
    in_progress = []  # в процессе
    inactive = []     # не начали или давно не заходили

    # Статистика ошибок по урокам
    lesson_errors = {}  # {lesson_idx: {"wrong": int, "total": int}}

    for uid, data in user_data.items():
        name = data.get("first_name", data.get("username", f"ID:{uid}"))
        username = data.get("username", "—")
        progress = data.get("progress", {})
        scores = progress.get("quiz_scores", {})
        done = len(scores)

        total_score = sum(s["score"] for s in scores.values())
        total_q = sum(s["total"] for s in scores.values())
        avg = f"{round(total_score / total_q * 100)}%" if total_q > 0 else "—"

        entry = f"👤 {name} (@{username}) — {done}/{total_lessons} уроков, балл: {avg}"

        # Считаем ошибки по урокам
        for li, s in scores.items():
            li_int = int(li) if isinstance(li, str) else li
            if li_int not in lesson_errors:
                lesson_errors[li_int] = {"wrong": 0, "total": 0}
            lesson_errors[li_int]["wrong"] += s["total"] - s["score"]
            lesson_errors[li_int]["total"] += s["total"]

        if done >= total_lessons:
            completed.append(entry)
        elif done > 0:
            in_progress.append(entry)
        else:
            inactive.append(entry)

    lines = [
        "📋 <b>ИТОГОВЫЙ ОТЧЁТ ПО УЧЕНИКАМ</b>",
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        "",
        f"👥 Всего учеников: <b>{len(user_data)}</b>",
        f"✅ Завершили курс: <b>{len(completed)}</b>",
        f"📖 В процессе: <b>{len(in_progress)}</b>",
        f"⭕ Не начали / неактивны: <b>{len(inactive)}</b>",
        "",
    ]

    if completed:
        lines.append("<b>✅ Завершили курс:</b>")
        lines.extend(completed)
        lines.append("")
    if in_progress:
        lines.append("<b>📖 В процессе:</b>")
        lines.extend(in_progress)
        lines.append("")
    if inactive:
        lines.append("<b>⭕ Неактивны:</b>")
        lines.extend(inactive)
        lines.append("")

    # Слабые места
    if lesson_errors:
        lines.append("<b>📉 Слабые места (больше всего ошибок):</b>")
        sorted_errors = sorted(
            lesson_errors.items(),
            key=lambda x: x[1]["wrong"],
            reverse=True,
        )
        for li, stats in sorted_errors[:5]:
            if stats["wrong"] > 0 and li < total_lessons:
                lesson_title = content.LESSONS[li]["title"]
                lines.append(
                    f"  ❌ {lesson_title} — "
                    f"{stats['wrong']} ошибок из {stats['total']} ответов"
                )

    lines.append("\n🏠 /start — вернуться в меню")

    await send_long_text(
        update.effective_chat.id, "\n".join(lines), context, parse_mode="HTML"
    )
    return MAIN_MENU


# ─────────────────────────────────────────────
# Напоминания (24ч)
# ─────────────────────────────────────────────

async def send_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяет всех учеников и отправляет напоминание если не заходили 24ч."""
    user_data = context.application.user_data
    now = datetime.now()
    total_lessons = len(content.LESSONS)

    for uid, data in user_data.items():
        progress = data.get("progress", {})
        scores = progress.get("quiz_scores", {})
        done = len(scores)

        # Не напоминать тем кто прошёл всё
        if done >= total_lessons:
            continue

        last_str = data.get("last_activity")
        if not last_str:
            continue

        try:
            last = datetime.fromisoformat(last_str)
        except Exception:
            continue

        hours_ago = (now - last).total_seconds() / 3600
        if hours_ago < 24:
            continue

        # Не спамить — напоминаем максимум раз в 24ч
        last_reminder = data.get("last_reminder")
        if last_reminder:
            try:
                lr = datetime.fromisoformat(last_reminder)
                if (now - lr).total_seconds() / 3600 < 24:
                    continue
            except Exception:
                pass

        current = progress.get("current_lesson", 0)
        lesson_title = content.LESSONS[min(current, total_lessons - 1)]["title"]

        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    f"👋 Привет! Ты остановился на уроке <b>{current + 1}</b>: "
                    f"{lesson_title}.\n\n"
                    f"📚 Пройдено: {done}/{total_lessons}\n\n"
                    "Продолжим? Нажми /start 💪"
                ),
                parse_mode="HTML",
            )
            data["last_reminder"] = now.isoformat()
            logger.info(f"Напоминание отправлено: {uid}")
        except Exception as e:
            logger.warning(f"Не удалось отправить напоминание {uid}: {e}")


# ─────────────────────────────────────────────
# Запуск
# ─────────────────────────────────────────────

def main():
    token = os.environ["BOT_TOKEN"]
    persistence = PicklePersistence(filepath="bot_data.pickle")
    app = Application.builder().token(token).persistence(persistence).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("help", help_command),
            CommandHandler("reset", reset_command),
        ],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(show_lesson, pattern=r"^lesson_\d+$"),
                CallbackQueryHandler(main_menu_handler, pattern="^main_menu$"),
                CallbackQueryHandler(my_progress, pattern="^my_progress$"),
                CallbackQueryHandler(help_command, pattern="^help$"),
                CallbackQueryHandler(start_quiz, pattern=r"^start_quiz_\d+$"),
                CallbackQueryHandler(admin_command, pattern="^admin_panel$"),
                CallbackQueryHandler(report_command, pattern="^admin_report$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text),
            ],
            VIEWING_LESSON: [
                CallbackQueryHandler(show_lesson, pattern=r"^lesson_\d+$"),
                CallbackQueryHandler(start_quiz, pattern=r"^start_quiz_\d+$"),
                CallbackQueryHandler(main_menu_handler, pattern="^main_menu$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text),
            ],
            QUIZ_CONFIRM: [
                CallbackQueryHandler(confirm_quiz, pattern=r"^confirm_quiz_\d+$"),
                CallbackQueryHandler(show_lesson, pattern=r"^lesson_\d+$"),
                CallbackQueryHandler(main_menu_handler, pattern="^main_menu$"),
            ],
            QUIZ_CHOICE_QUESTION: [
                CallbackQueryHandler(handle_choice_answer, pattern=r"^answer_\d+$"),
            ],
            QUIZ_OPEN_QUESTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_open_answer),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("help", help_command),
            CommandHandler("reset", reset_command),
            CommandHandler("admin", admin_command),
            CommandHandler("report", report_command),
        ],
        per_message=False,
    )

    app.add_handler(conv_handler)

    # Напоминания каждый час (проверяет кто не заходил 24ч)
    app.job_queue.run_repeating(send_reminders, interval=3600, first=60)

    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
