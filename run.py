"""Portable entry point: python run.py init|panel|bot|demo|backup."""
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import sys

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
SECRET_NAMES = ('BOT_TOKEN','ADMIN_PASSWORD','STOCK_KEY')
REQUIRED = {'panel':('ADMIN_PASSWORD','STOCK_KEY'),'bot':('STOCK_KEY','BOT_TOKEN','SUPPORT_CONTACT','SHOP_TERMS')}
TOKEN_PATTERN = re.compile(r'\b\d{5,}:[A-Za-z0-9_-]{30,}\b')


def load_env():
    path = ROOT/'.env'
    if path.exists():
        from manager import secure_file
        secure_file(path)
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                key, sep, value = line.partition('=')
                if sep:
                    os.environ.setdefault(key.strip(),value.strip())


class Redact(logging.Filter):
    """Never write the bot token, panel password or stock key into log files or the launcher."""
    def filter(self, record):
        message = record.getMessage()
        clean = TOKEN_PATTERN.sub('[скрыто]',message)
        for name in SECRET_NAMES:
            value = os.getenv(name,'')
            if len(value) >= 6:
                clean = clean.replace(value,'[скрыто]')
        if clean != message:
            record.msg, record.args = clean, ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            text = TOKEN_PATTERN.sub('[скрыто]',record.exc_text)
            for name in SECRET_NAMES:
                value = os.getenv(name,'')
                if len(value) >= 6:
                    text = text.replace(value,'[скрыто]')
            record.exc_text = text
        return True


def log_directory():
    configured = os.getenv('LOG_DIRECTORY')
    if configured:
        return Path(configured)
    database = Path(os.getenv('DATABASE_PATH','data/shop.sqlite3'))
    return (database if database.is_absolute() else ROOT/database).parent/'logs'


def setup_logging(name, level=logging.INFO):
    """Console (shown by the launcher / docker logs) plus data/logs/<name>.log, 5 MB x 5, redacted."""
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    redact = Redact()
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter); console.addFilter(redact)
    root.addHandler(console)
    try:
        directory = log_directory()
        directory.mkdir(parents=True,exist_ok=True)
        if os.name != 'nt':
            os.chmod(directory,0o700)
        handler = RotatingFileHandler(directory/(name+'.log'),maxBytes=5*1024*1024,backupCount=5,encoding='utf-8',delay=True)
        handler.setFormatter(formatter); handler.addFilter(redact)
        root.addHandler(handler)
    except OSError as exc:
        root.warning('Log file is not available: %s',type(exc).__name__)
    for noisy in ('aiogram.event','httpx'):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return root


def log_level():
    value = os.getenv('LOG_LEVEL','INFO').upper()
    return value if value in ('DEBUG','INFO','WARNING','ERROR') else 'INFO'


def missing_settings(command):
    return [name for name in REQUIRED.get(command,()) if not os.getenv(name,'').strip()]


def start_watchdog():
    """Started by the launcher: exit gracefully when it closes our stdin or dies."""
    if os.getenv('THEMARKET_PARENT_PIPE') == '1':
        from manager import watch_parent
        watch_parent()


def instance_lock(kind):
    from manager import InstanceLock
    database = Path(os.getenv('DATABASE_PATH','data/shop.sqlite3'))
    database = database if database.is_absolute() else ROOT/database
    return InstanceLock(database.resolve().with_suffix('.'+kind+'.lock')).acquire()


def run_panel():
    from manager import InstanceBusy
    setup_logging('panel',log_level())
    missing = missing_settings('panel')
    if missing:
        print('PANEL_ERROR: не заполнены настройки в .env: '+', '.join(missing),flush=True)
        raise SystemExit(1)
    try:
        lock = instance_lock('panel')
    except InstanceBusy:
        print('PANEL_ERROR: панель этого магазина уже запущена в другом окне или процессе. Остановите старый экземпляр.',flush=True)
        raise SystemExit(1) from None
    try:
        migrate_currency()
        import uvicorn
        start_watchdog()
        # Behind a reverse proxy set FORWARDED_ALLOW_IPS to the proxy address (uvicorn reads it).
        uvicorn.run('server:app',host=os.getenv('HOST','127.0.0.1'),port=int(os.getenv('PORT','8080')),
            access_log=False,log_config=None,proxy_headers=True,timeout_graceful_shutdown=10)
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()


BOT_ERRORS = {
    'TokenValidationError':'Неверный формат токена. Скопируйте токен целиком из @BotFather.',
    'TelegramUnauthorizedError':'Telegram отклонил токен. Получите актуальный токен у @BotFather.',
    'TelegramConflictError':'Этот бот уже запущен в другом терминале или на сервере. Остановите старый экземпляр.',
    'TelegramNetworkError':'Нет соединения с Telegram. Проверьте интернет или доступ к api.telegram.org.',
    'InstanceBusy':'Этот бот уже запущен на этом компьютере (другое окно или процесс). Остановите старый экземпляр.',
}


def bot_error_message(exc):
    name = type(exc).__name__
    if name in BOT_ERRORS:
        return BOT_ERRORS[name]
    if isinstance(exc,RuntimeError) and str(exc):
        # bot.py raises RuntimeError('Set NAME before starting the bot') for missing settings.
        match = re.fullmatch(r'Set ([A-Z_]+) before starting the bot',str(exc))
        if match:
            return 'Не заполнена настройка '+match.group(1)+' в .env.'
        text = TOKEN_PATTERN.sub('[скрыто]',str(exc))
        return text[:300]
    return 'Ошибка '+name+'. Проверьте настройки и подключение.'


def run_bot():
    import asyncio
    setup_logging('bot',log_level())
    missing = missing_settings('bot')
    if missing:
        print('BOT_ERROR: не заполнены настройки в .env: '+', '.join(missing),flush=True)
        raise SystemExit(1)
    try:
        migrate_currency()
        from bot import main as bot_main
        start_watchdog()
        asyncio.run(bot_main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.getLogger('bot').error('Bot stopped: %s',type(exc).__name__,exc_info=not getattr(exc,'__module__','').startswith('aiogram'))
        print('BOT_ERROR: '+bot_error_message(exc),flush=True)
        raise SystemExit(1) from None


def migrate_currency():
    if os.getenv('STARS_PER_USD'):
        from store import Store
        migration_store = Store()
        if not migration_store.currency()['active']:
            migration_store.set_currency(os.environ['STARS_PER_USD'],'startup')


def init():
    from cryptography.fernet import Fernet
    path = ROOT/'.env'
    if path.exists():
        from manager import ensure_config
        ensure_config(ROOT)
        print('.env already exists; it was preserved.')
        return
    content = ('ADMIN_USER=owner\nADMIN_PASSWORD='+secrets.token_urlsafe(24)+'\nSTOCK_KEY='+Fernet.generate_key().decode()
               +'\nDATABASE_PATH=data/shop.sqlite3\nSTARS_PER_USD=100\nSHOP_NAME=TheMarket\nBOT_TOKEN=\nSUPPORT_CONTACT=\nSHOP_TERMS=\n')
    try:
        fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0),0o600)
    except FileExistsError:
        print('.env already exists; it was preserved.')
        return
    with os.fdopen(fd,'wb') as output:
        output.write(content.encode('utf-8')); output.flush(); os.fsync(output.fileno())
    print('Created .env. Keep it private. Admin password is in ADMIN_PASSWORD.')


def demo():
    from store import Store
    store = Store()
    if store.products():
        print('Catalog is not empty; demo was not added.')
        return
    for name,cat,price,mode in [('Демо: лицензия на 30 дней','Подписки',250,'auto'),
                                ('Демо: настройка сервиса','Услуги',500,'manual'),
                                ('Демо: цифровой ключ','Ключи',100,'auto')]:
        store.save_product({'name':name,'category':cat,'price':price,'mode':mode,
            'description':'Демонстрационный товар. Замените перед подключением бота.',
            'instruction':'Свяжитесь с поддержкой при необходимости.','active':False})
    print('Added three hidden demo products. No stock or fake orders were generated.')


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else 'panel'
    if command == 'init':
        return init()
    load_env()
    if command == 'panel':
        run_panel()
    elif command == 'bot':
        run_bot()
    elif command == 'backup':
        from backups import main as backup_main
        backup_main(argv[1:])
    elif command == 'demo':
        demo()
    else:
        raise SystemExit('Use: python run.py init | panel | bot | demo | backup create|verify|restore')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
