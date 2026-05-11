"""
Модуль для сохранения результатов в Google Sheets.
Лист 1: "Результаты" — итоги по каждому тесту (одна строка на попытку)
Лист 2: "Ответы"     — все ответы на вопросы
"""

import os
import json
import logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SPREADSHEET_ID = os.environ.get(
    "SPREADSHEET_ID",
    "1a22R_4kPQ3JCFh0uzERPb6YDdj4RfV-H6X_fd06Usm8",
)

_client: gspread.Client | None = None


def _get_client() -> gspread.Client:
    global _client
    if _client is not None:
        return _client

    # Учётные данные берём из переменной окружения (JSON-строка)
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError(
            "Переменная окружения GOOGLE_CREDENTIALS_JSON не задана. "
            "Вставь содержимое credentials.json в Railway."
        )

    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    _client = gspread.authorize(creds)
    return _client


def _get_or_create_sheet(spreadsheet: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    """Возвращает лист по имени, создаёт если не существует."""
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=1000, cols=20)


def _ensure_headers(sheet: gspread.Worksheet, headers: list[str]) -> None:
    """Добавляет шапку, если лист пустой."""
    if sheet.row_count == 0 or not sheet.row_values(1):
        sheet.append_row(headers, value_input_option="RAW")


def save_result(
    user_id: int,
    username: str,
    lesson_title: str,
    lesson_idx: int,
    score: int,
    total: int,
    open_count: int,
    answers: list[dict],
) -> None:
    """
    Сохраняет итог теста в лист «Результаты»
    и каждый ответ — в лист «Ответы».
    """
    client = _get_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    pct = f"{round(score / total * 100)}%" if total > 0 else "—"

    # ── Лист «Результаты» ──────────────────────────────────────────────
    results_sheet = _get_or_create_sheet(spreadsheet, "Результаты")
    _ensure_headers(
        results_sheet,
        [
            "Дата", "User ID", "Имя/Ник",
            "№ урока", "Название урока",
            "Верных (выбор)", "Всего (выбор)", "Результат %",
            "Открытых ответов",
        ],
    )
    results_sheet.append_row(
        [
            now,
            user_id,
            username,
            lesson_idx,
            lesson_title,
            score,
            total,
            pct,
            open_count,
        ],
        value_input_option="RAW",
    )

    # ── Лист «Ответы» ──────────────────────────────────────────────────
    answers_sheet = _get_or_create_sheet(spreadsheet, "Ответы")
    _ensure_headers(
        answers_sheet,
        [
            "Дата", "User ID", "Имя/Ник",
            "№ урока", "Название урока",
            "№ вопроса", "Тип", "Вопрос", "Ответ", "Верно?",
        ],
    )
    rows = []
    for i, a in enumerate(answers, 1):
        correct_str = (
            "Да" if a["correct"] is True
            else "Нет" if a["correct"] is False
            else "Открытый"
        )
        rows.append([
            now,
            user_id,
            username,
            lesson_idx,
            lesson_title,
            i,
            "Выбор" if a["type"] == "choice" else "Открытый",
            a["question"],
            a["answer"],
            correct_str,
        ])

    if rows:
        answers_sheet.append_rows(rows, value_input_option="RAW")

    logger.info(f"Сохранено в Sheets: {username} | Урок {lesson_idx} | {score}/{total}")
