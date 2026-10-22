"""
DotaVoice - голосовой запуск и закрытие Dota 2.
Автор: https://t.me/tokinnssoft
"""

import copy
import difflib
import json
import queue
import subprocess
import sys
import threading
import time
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import psutil
import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

try:
    import pystray
    from PIL import Image, ImageDraw

    TRAY_OK = True
except Exception:  # noqa: BLE001
    TRAY_OK = False

SetLogLevel(-1)

APP_NAME = "DotaVoice"
VERSION = "1.1"
AUTHOR_URL = "https://t.me/tokinnssoft"
MODEL_DIR_NAME = "vosk-model-small-ru-0.22"
SAMPLE_RATE = 16000
COOLDOWN_SECONDS = 30      # пауза между запусками
CONFIRM_SECONDS = 8        # окно для подтверждения закрытия
DOTA_APP_ID = 570
CONFIRM_WORDS = {"да", "подтверждаю", "давай", "конечно"}

CONFIG_PATH = Path.home() / ".dotavoice" / "config.json"
DEFAULT_CONFIG = {
    "phrases": [
        "далбаеб поехали страдать хуйней",
        "пошли в доту",
        "запусти доту",
        "запускай доту",
        "погнали в доту",
        "поехали в доту",
        "включи доту",
    ],
    "close_phrases": [
        "хватит страдать хуйней",
        "закрой доту",
        "закрывай доту",
        "выключи доту",
        "выруби доту",
    ],
    "threshold": 0.7,
    "device": None,
    "auto_listen": True,
    "to_tray": True,
    "confirm_close": True,
    "notifications": True,
}

STATUS_TEXT = {
    "loading": "загрузка модели",
    "listening": "слушаю",
    "stopped": "остановлено",
    "error": "ошибка",
}
STATUS_DOT = {
    "loading": (240, 180, 0),
    "listening": (61, 220, 132),
    "stopped": (140, 148, 158),
    "error": (255, 62, 84),
}

IS_WINDOWS = sys.platform.startswith("win")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# ---------------------------------------------------------------- утилиты
def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def find_model() -> Path | None:
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / MODEL_DIR_NAME)
    base = (
        Path(sys.executable).parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )
    candidates.append(base / MODEL_DIR_NAME)
    for c in candidates:
        if c.is_dir():
            return c
    return None


def normalize(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def has_dota_word(text: str) -> bool:
    """Есть ли в тексте слово, похожее на «доту/дота» (с запасом на ошибки распознавания)."""
    for w in text.split():
        if "дот" in w or difflib.SequenceMatcher(None, w, "доту").ratio() >= 0.7:
            return True
    return False


def score(text: str, phrases: list[str]) -> float:
    """Похожесть текста на лучшую из фраз (1.0 = точное вхождение)."""
    best = 0.0
    dota_in_text = has_dota_word(text)
    for phrase in phrases:
        p = normalize(phrase)
        if not p:
            continue
        if p in text:
            return 1.0
        # Фразы про «доту» не считаем совпавшими, если в речи нет похожего слова
        # (иначе «закрой окно» похоже на «закрой доту»)
        if "дот" in p and not dota_in_text:
            continue
        if len(text) <= len(p) * 1.6:
            best = max(best, difflib.SequenceMatcher(None, text, p).ratio())
    return best


def classify(text: str, cfg: dict, confirm_active: bool) -> str | None:
    """Возвращает 'launch', 'close', 'confirm' или None."""
    text = normalize(text)
    if not text:
        return None
    words = text.split()
    if confirm_active and len(words) <= 2 and set(words) & CONFIRM_WORDS:
        return "confirm"

    thr = float(cfg["threshold"])
    launch = score(text, cfg["phrases"])
    close = score(text, cfg["close_phrases"])
    # Фразы с матом распознавалка может исказить — ловим по ключевым словам
    if "поехали" in text and "страдать" in text:
        launch = 1.0
    if "хватит" in text and "страдать" in text:
        close = 1.0

    if launch < thr and close < thr:
        return None
    if launch > close + 0.04:
        return "launch"
    if close > launch + 0.04:
        return "close"
    return None  # неоднозначно — лучше ничего не делать


def dota_processes() -> list[psutil.Process]:
    result = []
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if name in ("dota2.exe", "dota2", "dota 2"):
            result.append(proc)
    return result


def is_dota_running() -> bool:
    return bool(dota_processes())


def close_dota() -> bool:
    procs = dota_processes()
    if not procs:
        return False
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=5)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    return True


def launch_dota() -> None:
    url = f"steam://rungameid/{DOTA_APP_ID}"
    if IS_WINDOWS:
        import os

        os.startfile(url)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", url])
    else:
        subprocess.Popen(["xdg-open", url])


def beep() -> None:
    if IS_WINDOWS:
        try:
            import winsound

            winsound.Beep(880, 150)
        except Exception:
            pass


def list_mics() -> list[tuple[str, int | None]]:
    result: list[tuple[str, int | None]] = [("По умолчанию", None)]
    try:
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                result.append((f"{i}: {d['name']}", i))
    except Exception:
        pass
    return result


def make_tray_image(status: str = "stopped"):
    """Иконка-эквалайзер с цветной точкой статуса в правом верхнем углу."""
    bg = (13, 16, 20, 255)
    img = Image.new("RGBA", (64, 64), bg)
    d = ImageDraw.Draw(img)
    green, red = (61, 220, 132), (255, 62, 84)
    for i, h in enumerate([18, 32, 46, 28, 14]):
        t = i / 4
        c = tuple(int(green[k] * (1 - t) + red[k] * t) for k in range(3))
        x = 6 + i * 9
        d.rounded_rectangle([x, 56 - h, x + 6, 56], radius=3, fill=c)
    d.ellipse([30, -2, 66, 34], fill=bg)
    d.ellipse([35, 3, 61, 29], fill=STATUS_DOT.get(status, STATUS_DOT["stopped"]) + (255,))
    return img


# ---------------------------------------------------------- автозапуск
def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    return f'"{exe}" "{Path(__file__).resolve()}" --minimized'


def is_autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable: bool) -> None:
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        if enable:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


# ------------------------------------------------------ наблюдатель за Dota
class DotaWatcher(threading.Thread):
    """Следит за процессом Dota 2 и сообщает о запуске и закрытии."""

    def __init__(self, events: "queue.Queue[tuple[str, str]]"):
        super().__init__(daemon=True)
        self.events = events
        self.stop_event = threading.Event()

    def run(self) -> None:
        last: bool | None = None
        while not self.stop_event.is_set():
            try:
                now = is_dota_running()
            except Exception:  # noqa: BLE001
                now = bool(last)
            if last is None:
                self.events.put(("dota", "init_running" if now else "init_stopped"))
            elif now and not last:
                self.events.put(("dota", "started"))
            elif last and not now:
                self.events.put(("dota", "ended"))
            last = now
            self.stop_event.wait(2.0)


# ------------------------------------------------------ поток распознавания
class Listener(threading.Thread):
    def __init__(self, app: "App"):
        super().__init__(daemon=True)
        self.app = app
        self.stop_event = threading.Event()

    def run(self) -> None:
        ev = self.app.events
        try:
            ev.put(("status", "loading"))
            if self.app.model is None:
                path = find_model()
                if path is None:
                    raise FileNotFoundError(
                        f"Не найдена папка модели «{MODEL_DIR_NAME}»"
                    )
                self.app.model = Model(str(path))

            rec = KaldiRecognizer(self.app.model, SAMPLE_RATE)
            audio: "queue.Queue[bytes]" = queue.Queue()

            def callback(indata, frames, time_info, status):
                audio.put(bytes(indata))

            with sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=4000,
                device=self.app.cfg.get("device"),
                dtype="int16",
                channels=1,
                callback=callback,
            ):
                ev.put(("status", "listening"))
                while not self.stop_event.is_set():
                    try:
                        data = audio.get(timeout=0.3)
                    except queue.Empty:
                        continue
                    if rec.AcceptWaveform(data):
                        text = json.loads(rec.Result()).get("text", "")
                    else:
                        text = json.loads(rec.PartialResult()).get("partial", "")
                    if not text:
                        continue
                    ev.put(("heard", text))
                    action = classify(
                        text, self.app.cfg, time.time() < self.app.pending_until
                    )
                    if action:
                        ev.put((action, text))
                        rec.Reset()
        except Exception as e:  # noqa: BLE001
            ev.put(("error", str(e)))
        finally:
            ev.put(("status", "stopped"))


# --------------------------------------------------------------- интерфейс
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.events: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.model: Model | None = None
        self.listener: Listener | None = None
        self.tray = None
        self.tray_images: dict = {}
        self.status = "stopped"
        self.dota_running = False
        self.tray_hint_shown = False
        self.last_launch = 0.0
        self.last_close_req = 0.0
        self.pending_until = 0.0
        self.mics = list_mics()

        root.title(f"DotaVoice {VERSION} — голосовое управление Dota 2")
        root.geometry("460x780")
        root.minsize(440, 740)

        self.build_ui()
        self.start_tray()
        self.watcher = DotaWatcher(self.events)
        self.watcher.start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_close)
        self.root.bind("<Unmap>", self.on_unmap)
        self.poll()

        if "--minimized" in sys.argv:
            if self.tray and self.cfg.get("to_tray"):
                self.root.after(150, self.hide_to_tray_silent)
            else:
                root.iconify()
        if self.cfg.get("auto_listen"):
            self.root.after(300, self.toggle_listening)

    # ------------------------------------------------ построение окна
    def build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        self.status_dot = tk.Label(top, text="●", fg="#888888", font=("Segoe UI", 16))
        self.status_dot.pack(side="left")
        self.status_label = ttk.Label(top, text="Остановлено", font=("Segoe UI", 11))
        self.status_label.pack(side="left", padx=6)
        self.toggle_btn = ttk.Button(
            top, text="Начать слушать", command=self.toggle_listening
        )
        self.toggle_btn.pack(side="right")

        self.dota_label = ttk.Label(self.root, text="Dota 2: проверяю...", foreground="#666666")
        self.dota_label.pack(anchor="w", padx=14)

        mic_frame = ttk.LabelFrame(self.root, text="Микрофон")
        mic_frame.pack(fill="x", **pad)
        self.mic_var = tk.StringVar()
        self.mic_combo = ttk.Combobox(
            mic_frame,
            textvariable=self.mic_var,
            state="readonly",
            values=[m[0] for m in self.mics],
        )
        self.mic_combo.pack(fill="x", padx=8, pady=8)
        current = 0
        for i, (_, idx) in enumerate(self.mics):
            if idx == self.cfg.get("device"):
                current = i
        self.mic_combo.current(current)
        self.mic_combo.bind("<<ComboboxSelected>>", self.on_mic_change)

        # фразы: две вкладки
        ph_frame = ttk.LabelFrame(self.root, text="Голосовые команды")
        ph_frame.pack(fill="x", **pad)
        nb = ttk.Notebook(ph_frame)
        nb.pack(fill="x", padx=6, pady=6)
        self.editors: dict[str, tk.Listbox] = {}
        for key, title in (("phrases", "Запуск Dota"), ("close_phrases", "Закрытие Dota")):
            tab = ttk.Frame(nb)
            nb.add(tab, text=title)
            self.build_editor(tab, key)

        th_frame = ttk.LabelFrame(self.root, text="Строгость распознавания")
        th_frame.pack(fill="x", **pad)
        self.th_var = tk.DoubleVar(value=float(self.cfg["threshold"]))
        scale = ttk.Scale(
            th_frame, from_=0.5, to=0.95, variable=self.th_var,
            command=lambda v: self.th_value.config(text=f"{float(v):.2f}"),
        )
        scale.pack(side="left", fill="x", expand=True, padx=8, pady=8)
        scale.bind("<ButtonRelease-1>", self.on_threshold_change)
        self.th_value = ttk.Label(th_frame, text=f"{self.th_var.get():.2f}", width=5)
        self.th_value.pack(side="left", padx=(0, 8))

        opt = ttk.Frame(self.root)
        opt.pack(fill="x", **pad)
        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        cb = ttk.Checkbutton(
            opt, text="Запускать вместе с Windows",
            variable=self.autostart_var, command=self.on_autostart_toggle,
        )
        cb.pack(anchor="w")
        if not IS_WINDOWS:
            cb.state(["disabled"])

        self.auto_listen_var = tk.BooleanVar(value=bool(self.cfg.get("auto_listen")))
        ttk.Checkbutton(
            opt, text="Сразу слушать при запуске программы",
            variable=self.auto_listen_var,
            command=lambda: self.set_opt("auto_listen", self.auto_listen_var.get()),
        ).pack(anchor="w")

        self.tray_var = tk.BooleanVar(value=bool(self.cfg.get("to_tray")))
        tcb = ttk.Checkbutton(
            opt, text="Сворачивать в трей (кнопки «свернуть» и «закрыть»)",
            variable=self.tray_var,
            command=lambda: self.set_opt("to_tray", self.tray_var.get()),
        )
        tcb.pack(anchor="w")
        if not TRAY_OK:
            tcb.state(["disabled"])

        self.notify_var = tk.BooleanVar(value=bool(self.cfg.get("notifications", True)))
        ncb = ttk.Checkbutton(
            opt, text="Показывать уведомления (запуск и закрытие Dota)",
            variable=self.notify_var,
            command=lambda: self.set_opt("notifications", self.notify_var.get()),
        )
        ncb.pack(anchor="w")
        if not TRAY_OK:
            ncb.state(["disabled"])

        self.confirm_var = tk.BooleanVar(value=bool(self.cfg.get("confirm_close")))
        ttk.Checkbutton(
            opt, text="Подтверждать закрытие Dota голосом (скажи «да»)",
            variable=self.confirm_var,
            command=lambda: self.set_opt("confirm_close", self.confirm_var.get()),
        ).pack(anchor="w")

        log_frame = ttk.LabelFrame(self.root, text="Журнал")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(log_frame, height=5, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=8, pady=8)

        footer = ttk.Frame(self.root)
        footer.pack(fill="x", padx=12, pady=(0, 10))
        ttk.Label(footer, text="Автор:").pack(side="left")
        link = tk.Label(
            footer, text="t.me/tokinnssoft", fg="#1a73e8", cursor="hand2",
            font=("Segoe UI", 9, "underline"),
        )
        link.pack(side="left", padx=4)
        link.bind("<Button-1>", lambda e: webbrowser.open(AUTHOR_URL))

    def build_editor(self, parent: ttk.Frame, key: str) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=6, pady=(6, 4))
        lb = tk.Listbox(row, height=5, activestyle="none", exportselection=False)
        sb = ttk.Scrollbar(row, command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")
        for p in self.cfg[key]:
            lb.insert("end", p)
        self.editors[key] = lb

        add = ttk.Frame(parent)
        add.pack(fill="x", padx=6, pady=(0, 6))
        entry = ttk.Entry(add)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self.add_phrase(key, entry))
        ttk.Button(add, text="Добавить",
                   command=lambda: self.add_phrase(key, entry)).pack(side="left", padx=4)
        ttk.Button(add, text="Удалить",
                   command=lambda: self.remove_phrase(key)).pack(side="left")

    # ------------------------------------------------ журнал и статус
    def write_log(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        if int(self.log.index("end-1c").split(".")[0]) > 200:
            self.log.delete("1.0", "50.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_status(self, kind: str) -> None:
        mapping = {
            "loading": ("#e6a700", "Загружаю модель..."),
            "listening": ("#2e9e4f", "Слушаю"),
            "stopped": ("#888888", "Остановлено"),
            "error": ("#d93025", "Ошибка"),
        }
        color, text = mapping[kind]
        self.status = kind
        self.status_dot.configure(fg=color)
        self.status_label.configure(text=text)
        self.refresh_tray()

    def refresh_tray(self) -> None:
        """Обновляет иконку и подсказку в трее: статус программы и состояние Dota."""
        running = "запущена" if self.dota_running else "не запущена"
        if self.dota_running:
            self.dota_label.configure(text="Dota 2: запущена", foreground="#2e9e4f")
        else:
            self.dota_label.configure(text="Dota 2: не запущена", foreground="#666666")
        if not self.tray:
            return
        try:
            img = self.tray_images.get(self.status)
            if img is None:
                img = self.tray_images[self.status] = make_tray_image(self.status)
            self.tray.icon = img
            self.tray.title = f"{APP_NAME} — {STATUS_TEXT[self.status]}\nDota 2: {running}"
            self.tray.update_menu()
        except Exception:  # noqa: BLE001
            pass

    def notify(self, msg: str) -> None:
        """Всплывающее уведомление Windows через иконку в трее."""
        if self.tray and self.cfg.get("notifications", True):
            try:
                self.tray.notify(msg, APP_NAME)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------ прослушивание
    def is_listening(self) -> bool:
        return bool(
            self.listener
            and self.listener.is_alive()
            and not self.listener.stop_event.is_set()
        )

    def toggle_listening(self) -> None:
        if self.listener and self.listener.is_alive():
            self.listener.stop_event.set()
            self.toggle_btn.configure(state="disabled", text="Останавливаю...")
            return
        self.listener = Listener(self)
        self.listener.start()
        self.toggle_btn.configure(text="Остановить")

    def poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self.set_status(payload)
                    if payload == "stopped":
                        self.toggle_btn.configure(state="normal", text="Начать слушать")
                    elif payload == "listening":
                        self.toggle_btn.configure(state="normal", text="Остановить")
                elif kind == "heard":
                    if time.time() < self.pending_until:
                        self.status_label.configure(text="Закрыть Dota? Скажи «да»")
                    else:
                        self.status_label.configure(text=f"Слушаю: «{payload[-40:]}»")
                elif kind == "launch":
                    self.on_launch(payload)
                elif kind == "close":
                    self.on_close_request(payload)
                elif kind == "confirm":
                    self.on_confirm()
                elif kind == "dota":
                    self.on_dota_state(payload)
                elif kind == "tray":
                    if payload == "show":
                        self.show_window()
                    elif payload == "toggle":
                        self.toggle_listening()
                    elif payload == "quit":
                        self.quit_app()
                elif kind == "error":
                    self.set_status("error")
                    self.write_log(f"Ошибка: {payload}")
                    self.notify(f"Ошибка: {payload}")
                    if self.root.state() != "withdrawn":
                        messagebox.showerror("DotaVoice", payload)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    # ------------------------------------------------ команды
    def on_launch(self, text: str) -> None:
        now = time.time()
        if now - self.last_launch < COOLDOWN_SECONDS:
            return
        self.last_launch = now
        self.write_log(f"Услышал: «{text}»")
        if is_dota_running():
            self.write_log("Dota 2 уже запущена — повторно не открываю.")
            self.notify("Dota 2 уже запущена")
            return
        try:
            launch_dota()
            self.write_log("Запускаю Dota 2...")
            self.notify("Запускаю Dota 2...")
        except Exception as e:  # noqa: BLE001
            self.write_log(f"Не удалось запустить: {e}")

    def on_close_request(self, text: str) -> None:
        now = time.time()
        if now - self.last_close_req < 3:
            return
        self.last_close_req = now
        self.write_log(f"Услышал: «{text}»")
        if not is_dota_running():
            self.write_log("Dota 2 не запущена — закрывать нечего.")
            self.notify("Dota 2 не запущена")
            return
        if self.cfg.get("confirm_close", True):
            self.pending_until = now + CONFIRM_SECONDS
            beep()
            self.write_log(
                f"Закрыть Dota 2? Скажи «да» в течение {CONFIRM_SECONDS} сек."
            )
            self.notify("Закрыть Dota 2? Скажи «да»")
        else:
            self.do_close()

    def on_confirm(self) -> None:
        if time.time() >= self.pending_until:
            return
        self.pending_until = 0.0
        self.write_log("Подтверждено.")
        self.do_close()

    def on_dota_state(self, state: str) -> None:
        if state == "init_running":
            self.dota_running = True
        elif state == "init_stopped":
            self.dota_running = False
        elif state == "started":
            self.dota_running = True
            self.write_log("Dota 2 запущена.")
            self.notify("Dota 2 запущена")
        elif state == "ended":
            self.dota_running = False
            self.write_log("Dota 2 закрыта.")
            self.notify("Dota 2 закрыта")
        self.refresh_tray()

    def do_close(self) -> None:
        try:
            if close_dota():
                self.write_log("Закрываю Dota 2...")
            else:
                self.write_log("Dota 2 уже не запущена.")
        except Exception as e:  # noqa: BLE001
            self.write_log(f"Не удалось закрыть: {e}")

    # ------------------------------------------------ трей и окно
    def start_tray(self) -> None:
        if not TRAY_OK:
            return
        try:
            put = self.events.put
            menu = pystray.Menu(
                pystray.MenuItem(
                    lambda item: f"Статус: {STATUS_TEXT[self.status]}", None, enabled=False
                ),
                pystray.MenuItem(
                    lambda item: "Dota 2: запущена" if self.dota_running else "Dota 2: не запущена",
                    None, enabled=False,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Открыть", lambda i, it: put(("tray", "show")), default=True),
                pystray.MenuItem(
                    lambda item: "Остановить прослушивание" if self.is_listening() else "Начать слушать",
                    lambda i, it: put(("tray", "toggle")),
                ),
                pystray.MenuItem("Выход", lambda i, it: put(("tray", "quit"))),
            )
            self.tray_images["stopped"] = make_tray_image("stopped")
            self.tray = pystray.Icon(
                APP_NAME, self.tray_images["stopped"], f"{APP_NAME} — остановлено", menu
            )
            self.tray.run_detached()
        except Exception:  # noqa: BLE001
            self.tray = None

    def hide_to_tray_silent(self) -> None:
        self.root.withdraw()

    def hide_to_tray(self) -> None:
        self.root.withdraw()
        if not self.tray_hint_shown:
            self.tray_hint_shown = True
            self.notify("DotaVoice работает в фоне. Иконка — в трее.")

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def on_unmap(self, event) -> None:
        if (
            event.widget is self.root
            and self.tray
            and self.cfg.get("to_tray")
            and self.root.state() == "iconic"
        ):
            self.root.after(50, self.hide_to_tray)

    def on_window_close(self) -> None:
        if self.tray and self.cfg.get("to_tray"):
            self.hide_to_tray()
        else:
            self.quit_app()

    def quit_app(self) -> None:
        self.watcher.stop_event.set()
        if self.listener and self.listener.is_alive():
            self.listener.stop_event.set()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    # ------------------------------------------------ настройки
    def set_opt(self, key: str, value) -> None:
        self.cfg[key] = value
        save_config(self.cfg)

    def add_phrase(self, key: str, entry: ttk.Entry) -> None:
        phrase = normalize(entry.get())
        if not phrase or phrase in self.cfg[key]:
            return
        self.cfg[key].append(phrase)
        self.editors[key].insert("end", phrase)
        entry.delete(0, "end")
        save_config(self.cfg)

    def remove_phrase(self, key: str) -> None:
        sel = self.editors[key].curselection()
        if not sel:
            return
        idx = sel[0]
        self.editors[key].delete(idx)
        del self.cfg[key][idx]
        save_config(self.cfg)

    def on_threshold_change(self, _event=None) -> None:
        self.set_opt("threshold", round(self.th_var.get(), 2))

    def on_mic_change(self, _event=None) -> None:
        self.set_opt("device", self.mics[self.mic_combo.current()][1])
        if self.is_listening():
            self.write_log("Микрофон изменён — перезапусти прослушивание.")

    def on_autostart_toggle(self) -> None:
        try:
            set_autostart(self.autostart_var.get())
            self.write_log(
                "Автозапуск включён." if self.autostart_var.get() else "Автозапуск выключен."
            )
        except Exception as e:  # noqa: BLE001
            self.autostart_var.set(not self.autostart_var.get())
            messagebox.showerror("DotaVoice", f"Не удалось изменить автозапуск:\n{e}")


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
