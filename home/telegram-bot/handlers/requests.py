"""
handlers/requests.py — Вспомогательные колбэки модерации

Основная логика создания запросов — в client.py.
Одобрение/отклонение через inline-кнопки — в admin.py.
Этот файл экспортирует пустой router чтобы сохранить структуру импортов.
"""
from aiogram import Router

router = Router()
