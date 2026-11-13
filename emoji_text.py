"""Inline custom emoji inside shop texts.

Token format stored in text fields: ``⟦🔥|5368324170671202286⟧`` — the fallback emoji (what users
without custom-emoji support see, and what the panel's plain inputs show) followed by the Telegram
custom emoji ID (1–20 digits). The brackets U+27E6/U+27E7 practically never occur in normal text.
"""
from html import escape
import re

OPEN, CLOSE = '⟦', '⟧'
TOKEN_RE = re.compile(r'⟦([^⟦⟧|\s<>&A-Za-zЀ-ӿ]{1,16})\|([0-9]{1,20})⟧')
LOOSE_RE = re.compile(r'⟦[^⟦⟧\n]{0,40}⟧')
MAX_PER_FIELD = 50
ID_RE = re.compile(r'[0-9]{5,20}')


def normalize_id(value):
    """The custom emoji ID from whatever the owner pasted ('ID эмодзи: 123…', a token, spaces)."""
    value = (value or '').strip()
    if not value:
        return ''
    if re.fullmatch(r'[0-9]{1,20}', value):
        return value
    m = TOKEN_RE.search(value) or ID_RE.search(value)
    if not m:
        raise FieldError('emoji_id', 'ID эмодзи — это число до 20 цифр. Отправьте кастомный эмодзи боту командой /emoji или выберите его из набора')
    return m.group(2) if m.re is TOKEN_RE else m.group(0)


def token(emoji_id, fallback):
    return OPEN + fallback + '|' + emoji_id + CLOSE


def plain(text):
    """Visible text with every token replaced by its fallback emoji (buttons, invoices, exports)."""
    return TOKEN_RE.sub(lambda m: m.group(1), text or '')


def html(text, custom=True):
    """HTML-escaped text; tokens become <tg-emoji> (or the escaped fallback when custom=False)."""
    text = text or ''
    out, pos = [], 0
    for m in TOKEN_RE.finditer(text):
        out.append(escape(text[pos:m.start()]))
        fallback = escape(m.group(1))
        out.append(f'<tg-emoji emoji-id="{m.group(2)}">{fallback}</tg-emoji>' if custom else fallback)
        pos = m.end()
    out.append(escape(text[pos:]))
    return ''.join(out)


def has_tokens(text):
    return bool(TOKEN_RE.search(text or ''))


def ids(text):
    return [m.group(2) for m in TOKEN_RE.finditer(text or '')]


def validate(text, label, known=None, field=None):
    """Raise ValueError (Russian) for malformed tokens, too many emoji or IDs not in `known`."""
    text = text or ''
    rest = TOKEN_RE.sub('', text)
    if LOOSE_RE.search(rest):
        raise FieldError(field, f'Поле «{label}»: неверная вставка эмодзи. Формат ⟦🔥|ID⟧, ID — до 20 цифр')
    found = ids(text)
    if len(found) > MAX_PER_FIELD:
        raise FieldError(field, f'Поле «{label}»: не больше {MAX_PER_FIELD} кастомных эмодзи')
    if known is not None:
        unknown = [item for item in found if item not in known]
        if unknown:
            raise FieldError(field, f'Поле «{label}»: эмодзи {unknown[0]} нет в загруженных наборах')
    return text


def split_safe(text, end):
    """Move a split position `end` back so it does not fall inside a token."""
    for m in TOKEN_RE.finditer(text, max(0, end - 40), min(len(text), end + 40)):
        if m.start() < end < m.end():
            return m.start()
    return end


def utf16(text):
    return len(text.encode('utf-16-le')) // 2


def with_entities(text, base=0):
    """(plain text, custom_emoji entities) for messages sent without parse_mode."""
    text = text or ''
    out, entities, pos, units = [], [], 0, base
    for m in TOKEN_RE.finditer(text):
        before = text[pos:m.start()]
        out.append(before)
        units += utf16(before)
        fallback = m.group(1)
        entities.append(dict(type='custom_emoji', offset=units, length=utf16(fallback), custom_emoji_id=m.group(2)))
        out.append(fallback)
        units += utf16(fallback)
        pos = m.end()
    out.append(text[pos:])
    return ''.join(out), entities


class FieldError(ValueError):
    """A ValueError tied to an input field (the panel highlights it)."""
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field
