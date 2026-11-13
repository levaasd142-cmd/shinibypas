"""Explicit interface translations; customer and product data pass through unchanged."""
import json
import re
from contextvars import ContextVar
from pathlib import Path

language = ContextVar('shop_language', default='ru')
_language_context = language
EN = json.loads(Path(__file__).with_name('translations_en.json').read_text(encoding='utf-8'))

# Errors built with f-strings cannot be looked up literally; translate them by shape.
PATTERNS = [
    (re.compile(r'Ожидаем подтверждения: (\d+)/(\d+)\. Повтори проверку позже\.'),
     'Waiting for confirmations: {0}/{1}. Please check again later.'),
    (re.compile(r'Максимум (\d+) символов с учётом эмодзи'),
     'Maximum {0} characters including emoji'),
]


def tr(text, *values, lang=None, language=None):
    """Translate an interface string. `language=` is kept for older callers."""
    selected = lang or language or _language_context.get()
    translated = EN.get(text, text) if selected == 'en' else text
    return translated.format(*values) if values else translated


def order_status(status):
    labels = {
        'pending': ('Ожидает оплаты', 'Awaiting payment'),
        'expired': ('Истёк', 'Expired'),
        'fulfilled': ('Выполнен', 'Completed'),
        'awaiting_details': ('Ждём твои данные', 'Waiting for your details'),
        'queued': ('В очереди', 'Queued'),
        'in_progress': ('В работе', 'In progress'),
        'processing': ('В работе', 'In progress'),
        'needs_review': ('На проверке', 'Under review'),
    }
    pair = labels.get(status)
    return pair[_language_context.get() == 'en'] if pair else status


def error_text(message):
    message = str(message)
    if _language_context.get() != 'en':
        return message
    translated = EN.get(message)
    if translated is not None:
        return translated
    for pattern, template in PATTERNS:
        match = pattern.fullmatch(message)
        if match:
            return template.format(*match.groups())
    if any('Ѐ' <= char <= 'ӿ' for char in message):
        return 'Unable to complete this action. Please try again or contact support.'
    return message
