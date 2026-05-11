import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from datetime import datetime
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
    QUIZ_CHOICE_QUESTION,
    QUIZ_OPEN_QUESTION,
) = range(4)

ADMIN_ID = int(os.environ.get("ADMIN_ID", "1305122122"))


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


def lessons_keyboard(current_lesson: int) -> InlineKeyboardMarkup:
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

    buttons.append([InlineKeyboardButton("📝 Пройти тест по этому уроку", callback_data=f"start_quiz_{current_lesson}")])
    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(buttons)


def main_menu_keyboard(progress: dict) -> InlineKeyboardMarkup:
    total = len(content.LESSONS)
    done = len(progress.get("quiz_scores", {}))
    buttons = []
    for i, lesson in enumerate(content.LESSONS):
        score_info = progress["quiz_scores"].get(i)
        if score_info is not None:
            label = f"✅ Урок {i+1}: {lesson['title']} ({score_info['score']}/{score_info['total']})"
        else:
            label = f"📖 Урок {i+1}: {lesson['title']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"lesson_{i}")])

    buttons.append([InlineKeyboardButton(f"📊 Мой прогресс ({done}/{total})", callback_data="my_progress")])
    return InlineKeyboardMarkup(buttons)


# ─────────────────────────────────────────────
# Хендлеры
# ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    progress = get_user_progress(context)
    user = update.effective_user
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "Добро пожаловать в курс обучения бизнес-ассистентов.\n\n"
        "Здесь ты найдёшь учебные материалы и тесты по каждому уроку. "
        "Результаты сохраняются автоматически.\n\n"
        "Выбери урок:",
        reply_markup=main_menu_keyboard(progress),
    )
    return MAIN_MENU


async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    progress = get_user_progress(context)
    await query.edit_message_text(
        "Выбери урок:",
        reply_markup=main_menu_keyboard(progress),
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

    header = f"📚 Урок {lesson_idx + 1} из {total}: {lesson['title']}\n"
    header += "─" * 35 + "\n\n"

    text = header + lesson["text"]

    # Telegram ограничивает сообщение 4096 символами
    if len(text) > 4096:
        text = text[:4090] + "…"

    await query.edit_message_text(
        text,
        reply_markup=lessons_keyboard(lesson_idx),
        parse_mode="HTML",
    )
    return VIEWING_LESSON


async def my_progress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    progress = get_user_progress(context)
    scores = progress.get("quiz_scores", {})
    total_lessons = len(content.LESSONS)

    lines = ["📊 <b>Твой прогресс</b>\n"]
    for i, lesson in enumerate(content.LESSONS):
        if i in scores:
            s = scores[i]
            lines.append(f"✅ Урок {i+1}: {lesson['title']} — {s['score']}/{s['total']}")
        else:
            lines.append(f"⭕ Урок {i+1}: {lesson['title']} — не пройден")

    done = len(scores)
    lines.append(f"\n<b>Пройдено: {done}/{total_lessons}</b>")

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]),
        parse_mode="HTML",
    )
    return MAIN_MENU


# ─────────────────────────────────────────────
# Викторина
# ─────────────────────────────────────────────

async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    lesson_idx = int(query.data.split("_")[2])
    progress = get_user_progress(context)
    progress["current_lesson"] = lesson_idx
    progress["current_quiz_q"] = 0
    progress["current_quiz_answers"] = []

    lesson = content.LESSONS[lesson_idx]
    questions = lesson.get("questions", [])

    if not questions:
        await query.edit_message_text(
            "❗ Для этого урока ещё нет вопросов.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
            ]),
        )
        return MAIN_MENU

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

    header = (
        f"📝 <b>Тест: {lesson['title']}</b>\n"
        f"Вопрос {question_idx + 1} из {total_q}\n"
        "─" * 30 + "\n\n"
        f"{q['question']}"
    )

    if q["type"] == "choice":
        buttons = [
            [InlineKeyboardButton(opt, callback_data=f"answer_{i}")]
            for i, opt in enumerate(q["options"])
        ]
        markup = InlineKeyboardMarkup(buttons)

        if edit and update.callback_query:
            await update.callback_query.edit_message_text(
                header, reply_markup=markup, parse_mode="HTML"
            )
        else:
            msg = update.callback_query or update.message
            chat_id = update.effective_chat.id
            await context.bot.send_message(chat_id, header, reply_markup=markup, parse_mode="HTML")

        return QUIZ_CHOICE_QUESTION

    else:  # open
        prompt = header + "\n\n✏️ <i>Напиши свой ответ в чат:</i>"
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(prompt, parse_mode="HTML")
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

    feedback = "✅ Верно!" if is_correct else f"❌ Неверно. Правильный ответ: <b>{q['options'][q['correct']]}</b>"
    if q.get("explanation"):
        feedback += f"\n\n💡 {q['explanation']}"

    await query.edit_message_text(feedback, parse_mode="HTML")

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
        "📨 Ответ принят! Преподаватель проверит его позже.",
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
    result_text = (
        f"🏁 <b>Тест завершён!</b>\n\n"
        f"Урок: <b>{lesson['title']}</b>\n"
    )
    if total_choice > 0:
        pct = round(correct / total_choice * 100)
        result_text += f"Тест с выбором: <b>{correct}/{total_choice}</b> ({pct}%)\n"
    if open_count > 0:
        result_text += f"Открытых ответов: <b>{open_count}</b> (проверит преподаватель)\n"

    result_text += "\nРезультаты сохранены! Продолжай обучение 💪"

    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id,
        result_text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]),
        parse_mode="HTML",
    )
    return MAIN_MENU


def build_admin_summary(user, lesson, lesson_idx, correct, total_choice, answers) -> str:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    lines = [
        f"📬 <b>Новый результат теста</b>",
        f"👤 {user.first_name} (@{user.username or '—'}, ID: {user.id})",
        f"📚 Урок {lesson_idx + 1}: {lesson['title']}",
        f"🕐 {now}",
        "",
    ]
    if total_choice > 0:
        lines.append(f"<b>Тест с выбором: {correct}/{total_choice}</b>")
    for i, a in enumerate(answers, 1):
        mark = "✅" if a["correct"] is True else ("❌" if a["correct"] is False else "📝")
        lines.append(f"\n{mark} Вопрос {i}: {a['question']}")
        lines.append(f"   Ответ: {a['answer']}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Запуск
# ─────────────────────────────────────────────

def main():
    token = os.environ["BOT_TOKEN"]
    app = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(show_lesson, pattern=r"^lesson_\d+$"),
                CallbackQueryHandler(main_menu_handler, pattern="^main_menu$"),
                CallbackQueryHandler(my_progress, pattern="^my_progress$"),
            ],
            VIEWING_LESSON: [
                CallbackQueryHandler(show_lesson, pattern=r"^lesson_\d+$"),
                CallbackQueryHandler(start_quiz, pattern=r"^start_quiz_\d+$"),
                CallbackQueryHandler(main_menu_handler, pattern="^main_menu$"),
            ],
            QUIZ_CHOICE_QUESTION: [
                CallbackQueryHandler(handle_choice_answer, pattern=r"^answer_\d+$"),
            ],
            QUIZ_OPEN_QUESTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_open_answer),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        per_message=False,
    )

    app.add_handler(conv_handler)
    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
