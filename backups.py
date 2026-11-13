"""Authenticated offline recovery bundles; never overwrite a live database."""
import argparse
import base64
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED, BadZipFile

from cryptography.fernet import Fernet, InvalidToken
from manager import InstanceLock

MAX_BYTES = 256 * 1024 * 1024
# Product photos (<data dir>/product-images/<id>-<hash8>.jpg, as store.PRODUCT_IMAGE_RE) travel
# in the archive too. Only files with that exact name are taken; oversize ones are skipped.
IMAGE_DIR = 'product-images'
IMAGE_NAME = re.compile(r'[0-9]{1,12}-[0-9a-f]{8}\.jpg')
IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGES_MAX_BYTES = 64 * 1024 * 1024
FORMATS = (1, 2)   # 1: database only (before 3.0 photos), 2: database + product photos
NAME = re.compile(r'^(?:themarket|telemarket)-\d{8}T\d{6}Z-[a-f0-9]{8}\.tmbak$')
_lock = threading.Lock()
# Every column holding Fernet(STOCK_KEY) ciphertext: each value must decrypt before a backup
# is accepted. Tables of modules that are not installed yet are skipped.
ENCRYPTED_COLUMNS = (('stock','secret'),('orders','details'),('orders','result'),('outbox','body'),
                     ('payment_methods','api_key'),('broadcasts','content'),('channel_settings','join_url'),
                     ('support_messages','body'),('notify_queue','body'))


class BackupBusy(ValueError):
    pass


def read_key(path):
    try:
        key = Path(path).read_bytes().strip()
        Fernet(key)
        return key
    except (OSError, ValueError):
        raise ValueError('Ключ восстановления отсутствует или повреждён. Нужен исходный файл ключа.') from None


def inspect_database(path, stock_key):
    """Read-only integrity, logical digest, balances and decryption of every secret."""
    cipher = Fernet(stock_key)
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Нарушена целостность базы данных')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Нарушены связи между данными')
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        if not {'products','orders','stock','wallet_ledger','outbox','payment_methods'}.issubset(tables):
            raise ValueError('В архиве отсутствуют обязательные таблицы магазина')
        digest = hashlib.sha256()
        counts = {}
        for table in tables:
            quoted = '"'+table.replace('"','""')+'"'
            columns = [r[1] for r in db.execute('PRAGMA table_info('+quoted+')')]
            digest.update(json.dumps([table, columns], ensure_ascii=False).encode())
            counts[table] = 0
            order = ','.join(str(i+1) for i in range(len(columns)))
            for row in db.execute('SELECT * FROM '+quoted+' ORDER BY '+order):
                values = [{'bytes':base64.b64encode(v).decode()} if isinstance(v,bytes) else v for v in row]
                digest.update(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode()+b'\n')
                counts[table] += 1
        encrypted = 0
        for table,column in ENCRYPTED_COLUMNS:
            if table not in tables:continue
            for (value,) in db.execute('SELECT '+column+' FROM '+table+' WHERE '+column+' IS NOT NULL'):
                try:
                    cipher.decrypt(value).decode('utf-8')
                except (InvalidToken, ValueError, TypeError, UnicodeError):
                    raise ValueError('Не удалось расшифровать данные магазина: ключ стока не подходит или данные повреждены') from None
                encrypted += 1
        from store import FINGERPRINT_PREFIX, fingerprint_key, stock_fingerprint
        derived = fingerprint_key(stock_key)
        for fingerprint,secret in db.execute('SELECT fingerprint,secret FROM stock'):
            plain = cipher.decrypt(secret)
            expected = (stock_fingerprint(stock_key, plain.decode('utf-8'), derived)
                        if fingerprint.startswith(FINGERPRINT_PREFIX) else hashlib.sha256(plain).hexdigest())
            if not hmac.compare_digest(expected, fingerprint):
                raise ValueError('Содержимое стока не совпадает с его контрольной суммой')
        balances = db.execute('SELECT user_id,SUM(delta) FROM wallet_ledger GROUP BY user_id ORDER BY user_id').fetchall()
        if any(total < 0 for _,total in balances):
            raise ValueError('Обнаружен отрицательный баланс покупателя')
        return {'digest':digest.hexdigest(),'tables':counts,'encrypted_values':encrypted,
                'wallets':len(balances),'balance_total':sum(v for _,v in balances),
                'balances_digest':hashlib.sha256(json.dumps(balances).encode()).hexdigest(),
                'purchases':db.execute("SELECT COUNT(*) FROM orders WHERE status='fulfilled'").fetchone()[0],
                'stock_free':db.execute('SELECT COUNT(*) FROM stock WHERE order_id IS NULL').fetchone()[0]}


def unpack(archive, recovery_key, destination):
    """Extract only fixed members after authentication; no ZipFile.extract paths."""
    archive = Path(archive)
    if archive.stat().st_size > MAX_BYTES * 2:
        raise ValueError('Архив слишком большой для этой версии восстановления')
    try:
        payload = Fernet(recovery_key).decrypt(archive.read_bytes())
    except (InvalidToken, ValueError):
        raise ValueError('Архив повреждён или выбран другой ключ восстановления') from None
    images = {}
    try:
        with ZipFile(io.BytesIO(payload)) as bundle:
            names = bundle.namelist()
            base = sorted(name for name in names if not name.startswith(IMAGE_DIR+'/'))
            photos = [name for name in names if name.startswith(IMAGE_DIR+'/')]
            if (base != ['manifest.json','shop.sqlite3','stock.key'] or len(set(names)) != len(names)
                    or not all(IMAGE_NAME.fullmatch(name[len(IMAGE_DIR)+1:]) for name in photos)):
                raise ValueError('Неизвестный состав архива')
            if any(item.file_size > MAX_BYTES for item in bundle.infolist()):
                raise ValueError('Данные архива превышают допустимый размер')
            if (any(bundle.getinfo(name).file_size > IMAGE_MAX_BYTES for name in photos)
                    or sum(bundle.getinfo(name).file_size for name in photos) > IMAGES_MAX_BYTES):
                raise ValueError('Данные архива превышают допустимый размер')
            manifest = json.loads(bundle.read('manifest.json'))
            database = bundle.read('shop.sqlite3')
            stock_key = bundle.read('stock.key')
            for name in photos:images[name[len(IMAGE_DIR)+1:]] = bundle.read(name)
    except (BadZipFile, KeyError, UnicodeError, json.JSONDecodeError):
        raise ValueError('Неверный формат архива') from None
    if (not isinstance(manifest, dict) or manifest.get('format') not in FORMATS
            or hashlib.sha256(database).hexdigest() != manifest.get('database_sha256')):
        raise ValueError('Контрольная сумма базы не совпала')
    expected = manifest.get('images', {}) if manifest['format'] >= 2 else {}
    if (not isinstance(expected, dict) or set(expected) != set(images)
            or any(hashlib.sha256(data).hexdigest() != expected[name] for name, data in images.items())):
        raise ValueError('Контрольная сумма фото товаров не совпала')
    target = Path(destination)/'shop.sqlite3'
    with target.open('xb') as output:
        output.write(database)
    try:
        report = inspect_database(target, stock_key)
    except sqlite3.DatabaseError:
        raise ValueError('База в архиве повреждена') from None
    if report != manifest.get('report'):
        raise ValueError('Балансы, покупки или сток восстановились с отличиями')
    if images:
        folder = Path(destination)/IMAGE_DIR
        folder.mkdir(mode=0o700)
        for name, data in images.items():
            with (folder/name).open('xb') as output:
                output.write(data)
    return manifest, stock_key


def collect_images(data_dir):
    """{name: bytes} of the product photos to back up: regular files named like the panel
    saves them, each up to IMAGE_MAX_BYTES, IMAGES_MAX_BYTES in all (newest first). Skipped
    files are counted, never fail the backup."""
    folder = Path(data_dir)/IMAGE_DIR
    images, skipped, total = {}, 0, 0
    if not folder.is_dir() or folder.is_symlink():
        return images, skipped
    candidates = []
    for path in folder.iterdir():
        if not IMAGE_NAME.fullmatch(path.name) or path.is_symlink() or not path.is_file():
            continue
        candidates.append((path.stat().st_mtime, path.name, path))
    for _, name, path in sorted(candidates, reverse=True):
        size = path.stat().st_size
        if size > IMAGE_MAX_BYTES or total+size > IMAGES_MAX_BYTES:
            skipped += 1
            continue
        try:
            images[name] = path.read_bytes()
        except OSError:
            skipped += 1
            continue
        total += len(images[name])
    return images, skipped


def restore_archive(archive, key_file, output):
    """Produce a NEW directory only; the caller decides when to switch the shop."""
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Папка восстановления уже существует. Укажите новую пустую папку.')
    # Validate before creating the final directory, and publish it in one rename.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.themarket-restore-',dir=output.parent) as temp:
        stage = Path(temp)/'recovered'
        stage.mkdir(mode=0o700)
        manifest, stock_key = unpack(archive,read_key(key_file),stage)
        (stage/'data').mkdir()
        (stage/'shop.sqlite3').rename(stage/'data'/'shop.sqlite3')
        if (stage/IMAGE_DIR).is_dir():
            (stage/IMAGE_DIR).rename(stage/'data'/IMAGE_DIR)
        config = stage/'recovered.env'
        config.write_text('DATABASE_PATH=data/shop.sqlite3\nSTOCK_KEY='+stock_key.decode()+'\n',encoding='utf-8')
        config.chmod(0o600)
        # Windows rename also refuses an existing destination. Never replace it.
        if output.exists():
            raise ValueError('Папка восстановления уже существует')
        stage.rename(output)
    return manifest['report']


def _fsync_directory(path):
    if os.name=='nt':return
    try:
        fd=os.open(path,os.O_RDONLY)
    except OSError:return
    try:os.fsync(fd)
    except OSError:pass
    finally:os.close(fd)


def _write_private(path, data):
    """Create a 0600 file, fsync it; the caller renames it into place."""
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'wb') as output:
        output.write(data);output.flush();os.fsync(output.fileno())


def _sha256_file(path):
    digest=hashlib.sha256()
    with open(path,'rb') as source:
        for chunk in iter(lambda:source.read(1<<20),b''):digest.update(chunk)
    return digest.hexdigest()


def _key_id(key):
    return hashlib.sha256(b'themarket-recovery-key:'+key.strip()).hexdigest()[:24]


class BackupService:
    def __init__(self, database, stock_key, directory=None, key_file=None, interval_hours=None, keep=None):
        self.database = Path(database).resolve()
        self.stock_key = stock_key.encode() if isinstance(stock_key,str) else stock_key
        Fernet(self.stock_key)
        self.directory = Path(directory or os.getenv('BACKUP_DIRECTORY') or self.database.parent/'backups').resolve()
        self.key_file = Path(key_file or os.getenv('BACKUP_RECOVERY_KEY_PATH') or self.database.parent/'backup-recovery.key').resolve()
        if self.key_file.is_relative_to(self.directory):
            raise ValueError('Ключ восстановления должен храниться вне папки резервных копий')
        self.interval = float(interval_hours if interval_hours is not None else os.getenv('BACKUP_INTERVAL_HOURS','24'))*3600
        if not 0 <= self.interval <= 720*3600:
            raise ValueError('Интервал копирования должен быть от 0 до 720 часов')
        try:self.keep = int(keep if keep is not None else os.getenv('BACKUP_KEEP','14'))
        except ValueError:raise ValueError('BACKUP_KEEP должен быть целым числом от 1') from None
        if self.keep < 1:raise ValueError('BACKUP_KEEP должен быть целым числом от 1')
        self.last_error = ''
        self._checked_keys = set()
        try:self.cleanup()
        except Exception:pass

    @contextmanager
    def locked(self):
        if not _lock.acquire(blocking=False):
            raise BackupBusy('Уже выполняется операция с резервной копией')
        lock = None
        try:
            self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            try:lock = InstanceLock(self.directory/'.backup.lock').acquire()
            except RuntimeError:
                raise BackupBusy('Уже выполняется операция с резервной копией') from None
            yield
        finally:
            if lock:lock.release()
            _lock.release()

    def cleanup(self):
        """Remove temp folders left by a crash or a killed process (only when no backup is running)."""
        if not self.directory.is_dir():return 0
        removed = 0
        try:
            with self.locked():
                for path in self.directory.iterdir():
                    if path.name.startswith(('.snapshot-','.verify-')) and path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path,ignore_errors=True);removed += 1
                    elif path.name.endswith(('.json.pending','.tmbak.pending')) and path.is_file():
                        path.unlink(missing_ok=True);removed += 1
                    elif path.name.endswith('.tmbak.json') and not (self.directory/path.name[:-5]).exists():
                        path.unlink(missing_ok=True);removed += 1
        except BackupBusy:
            return 0
        return removed

    def archive_path(self, name):
        if not NAME.fullmatch(name):raise ValueError('Неверное имя копии')
        path = self.directory/name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != self.directory:
            raise ValueError('Копия не найдена')
        return path

    def _sidecar_path(self, path):
        return path.with_name(path.name+'.json')

    def _sidecar(self, path):
        sidecar = self._sidecar_path(path)
        try:
            if sidecar.is_symlink():return None
            data = json.loads(sidecar.read_text(encoding='utf-8'))
            return data if isinstance(data,dict) and data.get('name')==path.name else None
        except (OSError,ValueError):
            return None

    def _record(self, path, result, key):
        data = {'name':path.name,'size':path.stat().st_size,'sha256':_sha256_file(path),'key_id':_key_id(key),'result':result}
        pending = self._sidecar_path(path).with_name(path.name+'.json.pending')
        _write_private(pending,json.dumps(data,ensure_ascii=False).encode())
        os.replace(pending,self._sidecar_path(path))
        _fsync_directory(self.directory)

    def recovery_key(self):
        if not self.key_file.exists():
            if any(self.directory.glob('*.tmbak')):
                raise ValueError('Ключ восстановления потерян. Верните исходный файл ключа; новый ключ не откроет старые копии.')
            self.key_file.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            try:
                with os.fdopen(os.open(self.key_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as output:
                    output.write(Fernet.generate_key()+b'\n');output.flush();os.fsync(output.fileno())
            except FileExistsError:pass
        key = read_key(self.key_file)
        latest = self.catalog()['items']
        if latest:
            path = self.archive_path(latest[0]['name'])
            info = path.stat()
            marker = (_key_id(key),path.name,info.st_size,info.st_mtime_ns)
            sidecar = self._sidecar(path)
            # The key was proven against this archive at creation/verification: no decryption needed.
            if marker not in self._checked_keys and not (sidecar and sidecar.get('key_id')==marker[0] and sidecar.get('size')==info.st_size):
                try:Fernet(key).decrypt(path.read_bytes())
                except InvalidToken:
                    raise ValueError('Ключ не открывает последнюю копию. Проверьте ключ и целостность архива.') from None
            self._checked_keys.add(marker)
        return key

    def catalog(self):
        items = []
        if self.directory.is_dir():
            for path in self.directory.glob('*.tmbak'):
                if not NAME.fullmatch(path.name) or path.is_symlink():continue
                info = path.stat()
                sidecar = self._sidecar(path)
                verified = bool(sidecar and sidecar.get('size')==info.st_size and isinstance(sidecar.get('result'),dict))
                items.append({'name':path.name,'created':info.st_mtime,'size':info.st_size,'verified':verified,
                              'verified_at':sidecar['result'].get('verified_at') if verified else None})
        items.sort(key=lambda item:(item['created'],item['name']),reverse=True)
        return {'items':items,'interval_hours':self.interval/3600,'key_available':self.key_file.is_file(),
                'last_error':self.last_error,'keep':self.keep}

    def _verify(self, path, key):
        with tempfile.TemporaryDirectory(prefix='.verify-',dir=self.directory) as temp:
            manifest,_ = unpack(path,key,temp)
        return {'ok':True,'name':path.name,'created':manifest['created'],'verified_at':time.time(),**manifest['report'],
                'images':len(manifest.get('images') or {})}

    def verify(self, name, full=False):
        """Verify an archive. By default a previous full verification is reused when the file's
        SHA-256 and size still match its sidecar record (cheap: used before every download).
        full=True always decrypts, unpacks and inspects the database (the panel's verify button)."""
        with self.locked():
            path = self.archive_path(name)
            key = read_key(self.key_file)
            if not full:
                sidecar = self._sidecar(path)
                if (sidecar and isinstance(sidecar.get('result'),dict) and sidecar.get('key_id')==_key_id(key)
                        and sidecar.get('size')==path.stat().st_size and sidecar.get('sha256')==_sha256_file(path)):
                    return dict(sidecar['result'])
            try:
                result = self._verify(path,key)
            except ValueError:
                self._sidecar_path(path).unlink(missing_ok=True)
                raise
            try:self._record(path,result,key)
            except OSError:pass
            return result

    def _prune(self):
        """Keep the newest BACKUP_KEEP verified archives; delete everything older than the oldest kept one.
        The newest verified archive is never deleted; unverified newer archives are left alone."""
        items = self.catalog()['items']
        verified = [item for item in items if item['verified']]
        if len(verified) < self.keep:return []
        oldest_kept = verified[self.keep-1]
        cutoff = (oldest_kept['created'],oldest_kept['name'])
        removed = []
        for item in items:
            if (item['created'],item['name']) >= cutoff or item['name']==verified[0]['name']:continue
            path = self.directory/item['name']
            try:
                path.unlink();self._sidecar_path(path).unlink(missing_ok=True);removed.append(item['name'])
            except OSError:
                pass
        return removed

    def _check_space(self):
        size = sum(Path(str(self.database)+suffix).stat().st_size for suffix in ('','-wal') if Path(str(self.database)+suffix).exists())
        folder = self.database.parent/IMAGE_DIR
        if folder.is_dir():  # product photos travel in the archive too (capped)
            size += min(IMAGES_MAX_BYTES,sum(p.stat().st_size for p in folder.iterdir()
                                             if IMAGE_NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink()))
        free = shutil.disk_usage(self.directory).free
        if free < 3*size+16*1024*1024:
            raise ValueError('Недостаточно места на диске для резервной копии: нужно примерно '+str((3*size)//(1024*1024)+16)+' МиБ свободного места')

    def create(self, only_if_due=False):
        with self.locked():
            items = self.catalog()['items']
            if only_if_due and (not self.interval or (items and time.time()-items[0]['created']<self.interval)):
                return None
            key = self.recovery_key()
            self._check_space()
            created = time.time()
            name = 'themarket-'+datetime.fromtimestamp(created,timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]+'.tmbak'
            with tempfile.TemporaryDirectory(prefix='.snapshot-',dir=self.directory) as temp:
                snapshot = Path(temp)/'shop.sqlite3'
                with closing(sqlite3.connect(self.database.as_uri()+'?mode=ro',uri=True,timeout=30)) as source, closing(sqlite3.connect(snapshot)) as target:
                    # One step: a consistent read snapshot; WAL writers are not blocked and nothing restarts.
                    source.backup(target,pages=-1)
                if snapshot.stat().st_size>MAX_BYTES:raise ValueError('База превышает 256 МиБ: нужен потоковый формат копирования')
                report = inspect_database(snapshot,self.stock_key)
                data = snapshot.read_bytes()
                images, skipped = collect_images(self.database.parent)
                manifest = {'format':2,'created':created,'database_sha256':hashlib.sha256(data).hexdigest(),'report':report,
                            'images':{name:hashlib.sha256(image).hexdigest() for name,image in images.items()},
                            'images_skipped':skipped}
                payload = io.BytesIO()
                with ZipFile(payload,'w',ZIP_DEFLATED) as bundle:
                    bundle.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False))
                    bundle.writestr('stock.key',self.stock_key)
                    bundle.writestr('shop.sqlite3',data)
                    for image_name,image in images.items():
                        bundle.writestr(IMAGE_DIR+'/'+image_name,image,compress_type=ZIP_STORED)  # JPEG: already compressed
                del data,images
                pending = Path(temp)/name
                _write_private(pending,Fernet(key).encrypt(payload.getvalue()))
                del payload
                result = self._verify(pending,key)
                final = self.directory/name
                pending.rename(final)
                _fsync_directory(self.directory)
            try:self._record(final,result,key)
            except OSError:pass
            self._checked_keys.add((_key_id(key),final.name,final.stat().st_size,final.stat().st_mtime_ns))
            try:result['pruned'] = self._prune()
            except OSError:result['pruned'] = []
            self.last_error = ''
            return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='TheMarket encrypted backup and offline recovery')
    sub = parser.add_subparsers(dest='command',required=True)
    sub.add_parser('create')
    for action in ('verify','restore'):
        command = sub.add_parser(action)
        command.add_argument('archive',type=Path)
        command.add_argument('--key',required=True,type=Path)
        if action=='restore':command.add_argument('--output',required=True,type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command=='create':
            result = BackupService(os.getenv('DATABASE_PATH','data/shop.sqlite3'),os.environ['STOCK_KEY']).create()
        elif args.command=='restore':
            result = restore_archive(args.archive,args.key,args.output)
        else:
            with tempfile.TemporaryDirectory(prefix='themarket-verify-') as temp:
                result,_ = unpack(args.archive,read_key(args.key),temp)
        print(json.dumps({'ok':True,'result':result},ensure_ascii=True))
    except (ValueError,OSError,sqlite3.DatabaseError,RuntimeError):
        parser.exit(1,'Backup operation failed. Check archive, recovery key, paths and permissions.\n')


if __name__=='__main__':main()
