"""Telegram pack metadata and local image previews. Bot credentials stay server-side."""
import asyncio
from io import BytesIO
from pathlib import Path
import re
from urllib.parse import urlparse

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from PIL import Image


WELCOME_EMOJIS = {
    'welcome:greeting': ('5343984088493599366', '👋'),
    'welcome:brand': ('5312361253610475399', '🛒'),
    'welcome:actions': ('5456140674028019486', '⚡️'),
}


def pack_name(value):
    value=value.strip()
    if value.startswith(('https://','http://')):
        parsed=urlparse(value)
        if parsed.hostname not in ('t.me','telegram.me'):
            raise ValueError('Вставьте ссылку t.me/addemoji/Название или название набора')
        parts=parsed.path.strip('/').split('/')
        if len(parts)!=2 or parts[0]!='addemoji':
            raise ValueError('Нужна ссылка на набор эмодзи t.me/addemoji/Название')
        value=parts[1]
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',value):
        raise ValueError('Введите название набора из ссылки t.me/addemoji/Название')
    return value


async def import_pack(token, value):
    name=pack_name(value)
    if not token:
        raise ValueError('Укажите токен бота в окне запуска и перезапустите панель')
    try:
        async with Bot(token) as bot:
            pack=await bot.get_sticker_set(name)
        if pack.sticker_type!='custom_emoji':
            raise ValueError('Это набор стикеров. Выберите набор кастомных эмодзи.')
        items=[]
        for sticker in pack.stickers:
            if not sticker.custom_emoji_id:
                continue
            preview=sticker.thumbnail
            items.append({
                'id':sticker.custom_emoji_id,'emoji':sticker.emoji or '◈',
                'file_id':preview.file_id if preview else (sticker.file_id if not sticker.is_animated and not sticker.is_video else ''),
            })
        if not items:
            raise ValueError('В наборе нет доступных кастомных эмодзи')
        return pack.name,pack.title,items
    except (TelegramAPIError,TimeoutError):
        raise ValueError('Telegram не загрузил набор. Проверьте название, токен и подключение.') from None


class EmojiPreviews:
    def __init__(self, directory):
        self.directory=Path(directory)
        self.lock=asyncio.Semaphore(4)

    async def get(self, token, emoji_id, file_id):
        if not re.fullmatch(r'[0-9]{1,20}',emoji_id):
            raise ValueError('Неверный ID эмодзи')
        path=self.directory/(emoji_id+'.png')
        async with self.lock:
            if path.exists():
                return path
            if not token or not file_id:
                raise ValueError('У эмодзи нет доступного превью Telegram')
            try:
                async with Bot(token) as bot:
                    file=await bot.get_file(file_id)
                    if file.file_size and file.file_size>5_000_000:
                        raise ValueError('Превью слишком большое')
                    data=BytesIO()
                    await bot.download_file(file.file_path,destination=data,timeout=15)
                data.seek(0)
                with Image.open(data) as original:
                    image=original.convert('RGBA')
                    image.thumbnail((160,160))
                    output=BytesIO()
                    image.save(output,format='PNG')
                self.directory.mkdir(parents=True,exist_ok=True)
                import uuid
                pending=path.with_suffix('.'+uuid.uuid4().hex+'.pending')
                try:
                    pending.write_bytes(output.getvalue())
                    pending.replace(path)
                finally:
                    pending.unlink(missing_ok=True)
                return path
            except (TelegramAPIError,OSError,TimeoutError):
                raise ValueError('Превью временно недоступно') from None
