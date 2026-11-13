from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from cryptography.fernet import Fernet
from PIL import Image
from emoji_library import EmojiPreviews, import_pack, pack_name
from store import Store


class EmojiLibraryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'shop.db',Fernet.generate_key())

    def tearDown(self):
        self.temp.cleanup()

    async def test_pack_import_and_assignments_survive_restart(self):
        sticker=SimpleNamespace(custom_emoji_id='12345',emoji='🎬',thumbnail=SimpleNamespace(file_id='preview'),
                                file_id='file',is_animated=True,is_video=False)
        pack=SimpleNamespace(name='BrandPack',title='Brand pack',sticker_type='custom_emoji',stickers=[sticker])
        bot=AsyncMock()
        bot.get_sticker_set.return_value=pack
        with patch('emoji_library.Bot') as constructor:
            constructor.return_value.__aenter__.return_value=bot
            name,title,items=await import_pack('secret','https://t.me/addemoji/BrandPack')
        self.store.save_emoji_pack(name,title,items,'owner')
        pid=self.store.save_product(dict(name='Product',category='Video',description='',instruction='',
                                        price=100,mode='manual',active=True))
        for target in ('product:'+str(pid),'category:Video','menu:catalog'):
            self.store.assign_emoji(target,'12345','owner')
        self.assertEqual(self.store.products()[0]['emoji_id'],'12345')
        self.assertEqual(self.store.emoji_assignments()['category:Video'],'12345')
        self.assertEqual(self.store.emoji_packs()[0]['items'][0]['file_id'],'preview')
        self.store.assign_emoji('product:'+str(pid),'','owner')
        self.assertEqual(self.store.products()[0]['emoji_id'],'')
        with self.assertRaises(ValueError):
            self.store.assign_emoji('menu:catalog','999','owner')
        with self.assertRaises(ValueError):
            self.store.assign_emoji('menu:missing','12345','owner')

    async def test_pack_name_rejects_other_hosts_and_sticker_links(self):
        self.assertEqual(pack_name('BrandPack'),'BrandPack')
        self.assertEqual(pack_name('https://t.me/addemoji/BrandPack?x=1'),'BrandPack')
        for invalid in ('https://evil.test/addemoji/BrandPack','https://t.me/addstickers/BrandPack','1234','../../pack'):
            with self.assertRaises(ValueError):
                pack_name(invalid)

    async def test_preview_download_is_png_and_cached(self):
        buffer=BytesIO()
        Image.new('RGBA',(100,100),(200,30,20,255)).save(buffer,format='WEBP')
        bot=AsyncMock()
        bot.get_file.return_value=SimpleNamespace(file_size=100,file_path='stickers/preview.webp')
        async def download(path,destination,timeout):
            destination.write(buffer.getvalue())
        bot.download_file.side_effect=download
        cache=EmojiPreviews(Path(self.temp.name)/'cache')
        with patch('emoji_library.Bot') as constructor:
            constructor.return_value.__aenter__.return_value=bot
            path=await cache.get('secret','12345','preview')
            cached=await cache.get('secret','12345','preview')
        self.assertEqual(path,cached)
        self.assertTrue(path.read_bytes().startswith(b'\x89PNG'))
        self.assertEqual(bot.download_file.await_count,1)


if __name__=='__main__':
    unittest.main()
