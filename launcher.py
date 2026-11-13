"""Double-click launcher. Tk runs on the main thread; I/O and processes run off-thread."""
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from manager import ROOT, InstanceLock, Services, prepare, read_config, save_config


class Launcher:
    def __init__(self, window):
        self.window = window
        self.events = queue.Queue()
        self.services = None
        self.busy = True
        self.closing = False
        window.title('TheMarket — запуск магазина')
        window.geometry('820x790'); window.minsize(700,640)
        window.configure(bg='#10151c')
        style = ttk.Style(); style.theme_use('clam')
        style.configure('TFrame',background='#10151c')
        style.configure('TLabel',background='#10151c',foreground='#e8edf4',font=('Segoe UI',10))
        style.configure('Title.TLabel',font=('Segoe UI',24,'bold'))
        style.configure('Muted.TLabel',foreground='#a6b3c2')
        style.configure('TButton',font=('Segoe UI',10),padding=9)
        style.configure('Accent.TButton',background='#a2f27b',foreground='#14230d')
        body = ttk.Frame(window,padding=24); body.pack(fill='both',expand=True)
        ttk.Label(body,text='TheMarket',style='Title.TLabel').pack(anchor='w')
        ttk.Label(body,text='Панель и Telegram-бот в одном окне. Без команд в терминале.',style='Muted.TLabel').pack(anchor='w',pady=(3,16))
        self.setup = tk.StringVar(value='Проверяем окружение…')
        ttk.Label(body,textvariable=self.setup).pack(anchor='w',pady=(0,12))
        self.fields = {}
        for key,label,secret in [('BOT_TOKEN','Токен бота от @BotFather',True),('SUPPORT_CONTACT','Контакт поддержки (например, @support)',False),('SHOP_TERMS','Условия покупки, сроки и возврат',False)]:
            ttk.Label(body,text=label).pack(anchor='w',pady=(7,3))
            value = tk.StringVar()
            entry = ttk.Entry(body,textvariable=value,show='•' if secret else '',font=('Segoe UI',11))
            entry.pack(fill='x'); self.fields[key]=value
        controls = ttk.Frame(body); controls.pack(fill='x',pady=18)
        self.start_button = ttk.Button(controls,text='▶ Запустить всё',style='Accent.TButton',command=lambda:self.start(True),state='disabled')
        self.start_button.pack(side='left',padx=(0,8))
        self.panel_button = ttk.Button(controls,text='Только панель',command=lambda:self.start(False),state='disabled')
        self.panel_button.pack(side='left',padx=(0,8))
        self.stop_button = ttk.Button(controls,text='■ Остановить всё',command=self.stop,state='disabled')
        self.stop_button.pack(side='left')
        self.retry_button = ttk.Button(controls,text='↻ Повторить подготовку',command=self.retry)
        self.failed = False
        self.panel_status = tk.StringVar(value='Панель: остановлена')
        self.bot_status = tk.StringVar(value='Бот: остановлен')
        ttk.Label(body,textvariable=self.panel_status,wraplength=740).pack(anchor='w',pady=3)
        ttk.Label(body,textvariable=self.bot_status,wraplength=740).pack(anchor='w',pady=3)
        links = ttk.Frame(body); links.pack(fill='x',pady=(10,12))
        self.open_button = ttk.Button(links,text='Открыть панель',command=self.open_panel,state='disabled');self.open_button.pack(side='left',padx=(0,8))
        self.password_button = ttk.Button(links,text='Логин и пароль',command=self.credentials,state='disabled');self.password_button.pack(side='left',padx=(0,8))
        ttk.Button(links,text='Открыть папку',command=open_folder).pack(side='left')
        ttk.Label(body,text='Журнал запуска',style='Muted.TLabel').pack(anchor='w')
        logs_frame = ttk.Frame(body);logs_frame.pack(fill='both',expand=True,pady=(5,6))
        self.logs = tk.Text(logs_frame,bg='#0b1016',fg='#b6c6d6',font=('Consolas',9),height=10,wrap='word',state='disabled',relief='flat',padx=10,pady=10)
        scroll = ttk.Scrollbar(logs_frame,command=self.logs.yview);self.logs.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');self.logs.pack(fill='both',expand=True)
        ttk.Label(body,text='При закрытии этого окна магазин остановится. Товары и настройки сохранятся.',style='Muted.TLabel').pack(anchor='w')
        window.protocol('WM_DELETE_WINDOW',self.close)
        window.after(80,self.drain)
        self.background(self.initialize)

    def emit(self,kind,value):self.events.put((kind,value))

    def background(self,task):
        def work():
            try:task()
            except Exception as exc:self.emit('error',str(exc))
            finally:self.emit('idle',None)
        threading.Thread(target=work,daemon=True).start()

    def initialize(self):
        try:
            python = prepare(self.emit)
        except Exception:
            self.emit('setup_failed',None)
            raise
        self.services = Services(python,self.emit)
        self.emit('config',read_config())

    def retry(self):
        if self.busy or self.services:return
        self.failed=False;self.busy=True;self.setup.set('Проверяем окружение…');self.buttons()
        self.background(self.initialize)

    def start(self,with_bot):
        if not self.services:return
        if self.busy:return
        changes={k:v.get() for k,v in self.fields.items()}
        if with_bot and any(not v.strip() for v in changes.values()):
            messagebox.showerror('Нужны настройки','Заполните все три поля. Токен можно получить у @BotFather.');return
        save_config(changes)
        self.busy=True;self.buttons()
        self.background(lambda:self.services.start(with_bot))

    def stop(self):
        if self.busy or not self.services:return
        self.busy=True;self.buttons();self.background(self.services.stop)

    def buttons(self):
        running=bool(self.services and any(p.poll() is None for p in self.services.children.values()))
        for b in (self.start_button,self.panel_button):b.configure(state='normal' if self.services and not running and not self.busy else 'disabled')
        self.stop_button.configure(state='normal' if running and not self.busy else 'disabled')
        self.password_button.configure(state='normal' if self.services else 'disabled')
        panel=self.services.children.get('panel') if self.services else None
        self.open_button.configure(state='normal' if panel and panel.poll() is None and not self.busy else 'disabled')
        if self.failed and not self.services:
            if not self.retry_button.winfo_ismapped():self.retry_button.pack(side='left',padx=(8,0))
            self.retry_button.configure(state='disabled' if self.busy else 'normal')
        elif self.retry_button.winfo_ismapped():self.retry_button.pack_forget()

    def open_panel(self):
        if self.services and self.services.url:webbrowser.open(self.services.url)

    def credentials(self):
        config=read_config()
        dialog=tk.Toplevel(self.window);dialog.title('Вход в панель');dialog.geometry('490x190');dialog.transient(self.window)
        ttk.Label(dialog,text='Логин: '+config.get('ADMIN_USER','owner'),padding=15).pack(fill='x')
        ttk.Label(dialog,text='Пароль (можно выделить и скопировать):',padding=(15,0)).pack(anchor='w')
        field=ttk.Entry(dialog,font=('Consolas',11));field.insert(0,config.get('ADMIN_PASSWORD',''));field.configure(state='readonly');field.pack(fill='x',padx=15,pady=10)
        ttk.Button(dialog,text='Скопировать пароль',command=lambda:(dialog.clipboard_clear(),dialog.clipboard_append(config.get('ADMIN_PASSWORD','')))).pack(pady=6)

    def drain(self):
        while True:
            try:kind,value=self.events.get_nowait()
            except queue.Empty:break
            if kind=='log':
                self.logs.configure(state='normal');self.logs.insert('end',value+'\n')
                if int(self.logs.index('end-1c').split('.')[0])>1200:self.logs.delete('1.0','200.0')
                self.logs.see('end');self.logs.configure(state='disabled')
            elif kind=='setup':self.setup.set(value)
            elif kind=='setup_failed':self.failed=True
            elif kind=='panel':self.panel_status.set('Панель: '+value)
            elif kind=='bot':self.bot_status.set('Бот: '+value)
            elif kind=='config':
                for k,v in self.fields.items():v.set(value.get(k,''))
            elif kind=='url':webbrowser.open(value)
            elif kind=='idle':self.busy=False
            elif kind=='error':
                self.setup.set('Ошибка — подробности ниже')
                self.logs.configure(state='normal');self.logs.insert('end',value+'\n');self.logs.configure(state='disabled')
                if not self.closing:messagebox.showerror('TheMarket',value)
            elif kind=='closed':self.window.destroy();return
        self.buttons()
        self.window.after(150,self.drain)

    def close(self):
        if self.closing:return
        if self.busy:
            messagebox.showinfo('Подождите','Дождитесь завершения текущего запуска или установки библиотек.');return
        self.closing=True;self.busy=True;self.setup.set('Останавливаем процессы…');self.buttons()
        def shutdown():
            if self.services:self.services.stop()
            self.emit('closed',None)
        threading.Thread(target=shutdown,daemon=True).start()


def open_folder(path=ROOT):
    """Open the shop folder in the system file manager."""
    if os.name=='nt':os.startfile(path)  # Windows only
    else:webbrowser.open(Path(path).resolve().as_uri())


if __name__=='__main__':
    root=tk.Tk()
    root.withdraw()
    instance=InstanceLock(ROOT/'data'/'.launcher.lock')
    try:instance.acquire()
    except RuntimeError as exc:
        root.withdraw();messagebox.showinfo('TheMarket',str(exc));root.destroy()
    else:
        try:
            Launcher(root)
            root.deiconify()
            root.update_idletasks()
            root.mainloop()
        finally:instance.release()
