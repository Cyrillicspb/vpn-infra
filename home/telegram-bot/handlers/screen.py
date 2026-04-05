from __future__ import annotations

import logging
import time

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import CallbackQuery

logger = logging.getLogger(__name__)


async def edit_or_answer(cb: CallbackQuery, text: str, kb=None, parse_mode: str = "HTML") -> None:
    """Показать экран через edit_text, а если нельзя — отправить новым сообщением."""
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode=parse_mode)
    except Exception:
        try:
            await cb.message.answer(text, reply_markup=kb, parse_mode=parse_mode)
        except Exception as exc:
            logger.warning("edit_or_answer failed: %s", exc)
    try:
        await cb.answer()
    except Exception:
        pass


def section_text(title: str, hint: str, *, icon: str = "📋", details: list[str] | None = None) -> str:
    lines = [
        f"{icon} <b>{title}</b>",
        "",
        f"<i>{hint}</i>",
    ]
    if details:
        lines.append("")
        lines.append("<b>Что можно сделать:</b>")
        lines.extend(f"• {item}" for item in details)
    return "\n".join(lines)


def breadcrumb_text(items: list[str]) -> str:
    if not items:
        return ""
    return f"<blockquote>{' → '.join(items)}</blockquote>"


def screen_text(
    title: str,
    hint: str,
    *,
    icon: str = "📋",
    details: list[str] | None = None,
    trail: list[str] | None = None,
) -> str:
    parts: list[str] = []
    if trail:
        parts.append(breadcrumb_text(trail))
    parts.append(section_text(title, hint, icon=icon, details=details))
    return "\n\n".join(part for part in parts if part)


def result_text(
    title: str,
    message: str,
    *,
    status: str = "ok",
    next_steps: list[str] | None = None,
    trail: list[str] | None = None,
) -> str:
    icon = {"ok": "✅", "warn": "⚠️", "error": "❌", "info": "ℹ️"}.get(status, "ℹ️")
    parts: list[str] = []
    if trail:
        parts.append(breadcrumb_text(trail))
    parts.append(f"{icon} <b>{title}</b>\n\n{message}")
    if next_steps:
        parts.append("<b>Что дальше:</b>\n" + "\n".join(f"• {item}" for item in next_steps))
    return "\n\n".join(parts)


def return_kb(back_cb: str, home_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb),
        InlineKeyboardButton(text="🏠 Меню", callback_data=home_cb),
    ]])


async def start_prompt(
    cb: CallbackQuery,
    state: FSMContext,
    next_state: State,
    prompt: str,
    return_to: str,
    *,
    home_cb: str,
    parse_mode: str = "HTML",
    extra_data: dict | None = None,
) -> None:
    payload = {
        "_return_to": return_to,
        "_return_home": home_cb,
        "_fsm_ts": time.time(),
    }
    if extra_data:
        payload.update(extra_data)
    await state.update_data(**payload)
    await edit_or_answer(cb, prompt, return_kb(return_to, home_cb), parse_mode=parse_mode)
    await state.set_state(next_state)
