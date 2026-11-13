"""Local process supervisor; no UI imports, independently integration-testable."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
# Own process group on Windows: a Ctrl+C in the launcher console never hits the children directly.
GROUP = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
# Children started with this variable watch their stdin pipe and shut down gracefully on EOF:
# when the launcher asks them to stop (closes the pipe) or when the launcher process dies.
WATCH_ENV = 'THEMARKET_PARENT_PIPE'
READY_SECONDS = 120
STOP_SECONDS = 25
EDITABLE = {'BOT_TOKEN', 'SUPPORT_CONTACT', 'SHOP_TERMS'}


def read_config(root=ROOT):
    path = root / '.env'
    result = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                key, sep, value = line.partition('=')
                if sep:
                    result[key.strip()] = value.strip()
    return result


def write_private(path, text):
    """Atomically replace `path` with a 0600 file: create, fsync, os.replace, fsync directory."""
    path = Path(path)
    pending = path.with_name(path.name+'.pending')
    try:
        pending.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(pending, os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0), 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(text.encode('utf-8'))
        output.flush()
        os.fsync(output.fileno())
    os.replace(pending, path)
    secure_file(path)
    if os.name != 'nt':
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
        except OSError:
            pass


def secure_file(path):
    """Keep secrets readable by the owner only (no-op on Windows, where ACLs apply)."""
    if os.name == 'nt':
        return
    try:
        if Path(path).stat().st_mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass


def save_config(changes, root=ROOT):
    config = read_config(root)
    for key, value in changes.items():
        if key not in EDITABLE:
            raise ValueError('Unsupported configuration field')
        config[key] = ' '.join(value.strip().splitlines())
    write_private(root / '.env', ''.join(k+'='+v+'\n' for k,v in config.items()))
    return config


def ensure_config(root=ROOT):
    """Repair an older .env: add a panel login/password when missing. Never touches STOCK_KEY."""
    path = root / '.env'
    if not path.exists():
        return read_config(root)
    config = read_config(root)
    missing = {}
    if not config.get('ADMIN_USER'):
        missing['ADMIN_USER'] = 'owner'
    if len(config.get('ADMIN_PASSWORD','')) < 16:
        missing['ADMIN_PASSWORD'] = secrets.token_urlsafe(24)
    if missing:
        config.update(missing)
        write_private(path, ''.join(k+'='+v+'\n' for k,v in config.items()))
    else:
        secure_file(path)
    return config


def local_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class InstanceBusy(RuntimeError):
    """Another process already holds this instance lock."""


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file = self.path.open('a+b')
        self.file.seek(0)
        if self.path.stat().st_size == 0:
            self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close(); self.file = None
            raise InstanceBusy('Уже запущен другой экземпляр. Откройте существующее окно или остановите старый процесс.') from None
        return self

    def release(self):
        if self.file:
            self.file.close(); self.file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


def watch_parent(timeout=STOP_SECONDS, stream=None):
    """Run inside a child: when the parent's pipe closes (stop requested or launcher died),
    raise SIGINT in this process for a graceful shutdown; hard-exit if that takes too long."""
    stream = stream if stream is not None else sys.stdin

    def watch():
        try:
            while stream.read(1024):
                pass
        except (OSError, ValueError):
            pass
        try:
            if hasattr(signal, 'pthread_kill'):
                # POSIX: deliver to the main thread so a blocking sleep/select is interrupted.
                signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)
            else:
                # Windows: Python's SIGINT handler also wakes the main thread's waits.
                signal.raise_signal(signal.SIGINT)
        except (OSError, ValueError):
            os._exit(0)
        time.sleep(timeout)
        os._exit(1)

    thread = threading.Thread(target=watch, name='parent-watchdog', daemon=True)
    thread.start()
    return thread


class Services:
    def __init__(self, python, emit, root=ROOT):
        self.python, self.emit, self.root = str(python), emit, Path(root)
        self.children = {}
        self.config = {}
        self.url = ''
        self.stopping = False
        self.lock = threading.RLock()

    def clean(self, text):
        for key in ('BOT_TOKEN','ADMIN_PASSWORD','STOCK_KEY'):
            if self.config.get(key):
                text = text.replace(self.config[key],'[скрыто]')
        return text

    def launch(self, name, command, env):
        env = {**env, WATCH_ENV:'1'}
        child = subprocess.Popen([self.python,'-u',*command],cwd=self.root,env=env,stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',
            creationflags=HIDDEN|GROUP)
        self.children[name] = child
        threading.Thread(target=self.read_output,args=(name,child),daemon=True).start()
        return child

    def read_output(self, name, child):
        with child.stdout as output:
            for line in output:
                text = self.clean(line.rstrip())
                self.emit('log',f'[{name}] {text}')
                if 'BOT_READY @' in text:
                    self.emit('bot','Работает: '+text.split('BOT_READY ',1)[1])
                elif 'BOT_OFFLINE' in text:
                    self.emit('bot','Нет связи с Telegram — повторяем подключение')
        code = child.wait()
        if not self.stopping:
            self.emit(name,'Остановлен' if code==0 else f'Ошибка запуска (код {code}), см. журнал')

    def start(self, with_bot=True):
        with self.lock:
            if any(c.poll() is None for c in self.children.values()):
                raise RuntimeError('Сначала нажмите «Остановить всё».')
            self.config = read_config(self.root)
            if with_bot:
                missing = [k for k in ('BOT_TOKEN','SUPPORT_CONTACT','SHOP_TERMS') if not self.config.get(k)]
                if missing:
                    raise ValueError('Заполните токен бота, контакт поддержки и условия покупки.')
            if not self.config.get('ADMIN_PASSWORD') or not self.config.get('STOCK_KEY'):
                raise ValueError('В файле .env нет пароля панели или ключа стока. Перезапустите окно запуска: настройки будут восстановлены.')
            self.stopping = False
            env = os.environ.copy()
            env.update(self.config)
            env.update(PYTHONIOENCODING='utf-8',PYTHONUTF8='1')
            # An occupied port belongs to another process; never terminate it.
            preferred = int(self.config.get('PORT','8080'))
            port = None
            for candidate in range(preferred,min(preferred+20,65536)):
                with socket.socket() as probe:
                    try: probe.bind(('127.0.0.1',candidate))
                    except OSError: continue
                    port = candidate; break
            if port is None:
                raise RuntimeError('Нет свободного порта для панели.')
            env['PORT'] = str(port)
            self.url = f'http://127.0.0.1:{port}'
            self.emit('panel','Запускается…')
            panel = self.launch('panel',['run.py','panel'],env)
            deadline = time.monotonic()+READY_SECONDS
            while True:
                if panel.poll() is not None:
                    raise RuntimeError('Панель завершилась при запуске. Причина показана в журнале.')
                try:
                    # Public health endpoint: the panel signs staff in via its login page (3.0+),
                    # so an authenticated probe would be refused and the start would time out.
                    with local_opener().open(self.url+'/healthz',timeout=2) as response:
                        if json.load(response).get('ok'):
                            break
                except (OSError,ValueError):
                    if time.monotonic() > deadline:
                        self.stop()
                        raise RuntimeError(f'Панель не ответила за {READY_SECONDS//60} минуты. Проверьте журнал.') from None
                    time.sleep(.3)
            self.emit('panel','Работает: '+self.url)
            self.emit('url',self.url)
            if with_bot:
                self.emit('bot','Подключается к Telegram…')
                self.launch('bot',['run.py','bot'],env)
            else:
                self.emit('bot','Не запущен — режим только панели')

    @staticmethod
    def request_stop(child):
        """Graceful stop on every OS: close the control pipe; the child's watchdog raises SIGINT in
        itself (uvicorn and asyncio shut down cleanly). Works without a console, unlike CTRL_BREAK."""
        try:
            if child.stdin and not child.stdin.closed:
                child.stdin.close()
        except OSError:
            pass

    def stop(self, timeout=STOP_SECONDS):
        with self.lock:
            self.stopping = True
            running = [child for child in self.children.values() if child.poll() is None]
            # Bot first: it may still be delivering messages through the shared database.
            for name in ('bot','panel'):
                child = self.children.get(name)
                if child and child.poll() is None:
                    self.request_stop(child)
            deadline = time.monotonic()+timeout
            for child in running:
                try: child.wait(timeout=max(.1,deadline-time.monotonic()))
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait(timeout=5)
            for child in self.children.values():
                try:
                    if child.stdin and not child.stdin.closed: child.stdin.close()
                except OSError:
                    pass
            self.children.clear()
            self.emit('panel','Остановлена')
            self.emit('bot','Остановлен')


def prepare(emit, root=ROOT):
    """Repair partial venvs and install only when requirements or imports changed."""
    python = root / '.venv' / ('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    env = os.environ.copy()
    env.update(PYTHONIOENCODING='utf-8',PYTHONUTF8='1')

    def run(args):
        process = subprocess.Popen(args,cwd=root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            encoding='utf-8',errors='replace',env=env,creationflags=HIDDEN)
        with process.stdout as output:
            for line in output: emit('log',line.rstrip())
        if process.wait(): raise RuntimeError('Не удалось подготовить окружение. Подробности в журнале.')

    if not python.exists():
        emit('setup','Создаём окружение Python…')
        run([sys.executable,'-m','venv',str(root/'.venv')])
    pip_ok = subprocess.run([str(python),'-m','pip','--version'],stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,creationflags=HIDDEN).returncode==0
    if not pip_ok:
        emit('setup','Восстанавливаем pip…')
        run([str(python),'-m','ensurepip','--upgrade'])
    # Version ranges here (wheels for the user's Windows Python); Docker installs the exact lock file.
    requirements = 'requirements.txt'
    fingerprint = hashlib.sha256((root/requirements).read_bytes()).hexdigest()
    marker = root/'.venv'/'.telemarket-requirements'
    ready = marker.exists() and marker.read_text()==fingerprint
    if ready:
        ready = subprocess.run([str(python),'-c','import fastapi, uvicorn, aiogram, aiohttp, cryptography, PIL'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=HIDDEN).returncode==0
    if not ready:
        emit('setup','Устанавливаем библиотеки (первый запуск)…')
        run([str(python),'-m','pip','install','--disable-pip-version-check','-r',requirements])
        marker.write_text(fingerprint)
    run([str(python),'run.py','init'])
    ensure_config(root)
    emit('setup','Готово к запуску')
    return python
