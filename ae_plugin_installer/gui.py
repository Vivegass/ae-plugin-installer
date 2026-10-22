r"""
AE Plugin Installer — окно с перетаскиванием архивов в стиле After Effects.
Разбор архивов и установка — в core.py, здесь только интерфейс.

Почему установка идёт в отдельном процессе: Windows не пропускает перетаскивание
из Проводника в окно, запущенное от администратора (UIPI). Поэтому окно работает
с обычными правами, а для установки запускает само себя с ключом --worker через UAC
и получает ход установки построчно в JSON по локальному сокету.

Запуск:  python -m ae_plugin_installer [архивы...] [--sandbox ПАПКА]
"""

import ctypes
import html
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, QRunnable, Qt, QThread, QThreadPool, Signal
from PySide6.QtGui import (QColor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton, QSplitter,
                               QStackedWidget, QTextBrowser, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import core

APP_NAME = "AE Plugin Installer"

# палитра After Effects (Spectrum, тёмная тема)
C = {
    "gutter": "#161616", "panel": "#232323", "panel2": "#1e1e1e", "raised": "#2b2b2b",
    "line": "#1b1b1b", "border": "#3c3c3c", "text": "#c6c6c6", "dim": "#8a8a8a",
    "bright": "#ececec", "blue": "#2d8ceb", "hot": "#4b9cf5", "button": "#1473e6",
    "select": "#2f5277", "green": "#3fb66b", "yellow": "#e6b422", "red": "#ec5b62",
}

QSS = f"""
* {{ font-family: "Segoe UI"; font-size: 9pt; color: {C['text']}; }}
QMainWindow, #Central {{ background: {C['gutter']}; }}
#TopBar {{ background: {C['panel']}; border-bottom: 1px solid #101010; }}
#AppTitle {{ color: {C['bright']}; font-size: 10pt; font-weight: 600; }}
#AppSub {{ color: {C['dim']}; }}
#Panel {{ background: {C['panel']}; border: 1px solid #2a2a2a; border-radius: 4px; }}
#Panel[active="true"] {{ border: 1px solid {C['blue']}; }}
#Panel[drag="true"] {{ border: 1px dashed {C['blue']}; background: #1d2633; }}
#PanelHeader {{ border-bottom: 1px solid {C['line']}; }}
#PanelTab {{ color: {C['bright']}; font-weight: 600; }}
#PanelMenu {{ color: #707070; font-size: 11pt; }}
#PanelFooter {{ border-top: 1px solid {C['line']}; }}
QTreeWidget {{ background: {C['panel']}; border: none; outline: 0; }}
QTreeWidget::item {{ height: 24px; }}
QTreeWidget::item:selected {{ background: {C['select']}; color: #ffffff; }}
QTreeWidget::item:hover:!selected {{ background: #2b2b2b; }}
QHeaderView::section {{ background: {C['panel']}; color: {C['dim']}; border: none;
    border-right: 1px solid {C['line']}; border-bottom: 1px solid {C['line']}; padding: 3px 8px; }}
QTextBrowser {{ background: {C['panel2']}; border: none; selection-background-color: {C['select']}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #474747; border-radius: 3px; min-height: 28px; margin: 2px; }}
QScrollBar::handle:vertical:hover {{ background: #5e5e5e; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #474747; border-radius: 3px; min-width: 28px; margin: 2px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QPushButton {{ background: transparent; color: #e3e3e3; border: 2px solid #6e6e6e; border-radius: 14px;
    padding: 4px 16px; font-weight: 600; min-height: 18px; }}
QPushButton:hover {{ border-color: #a0a0a0; background: #2e2e2e; }}
QPushButton:pressed {{ background: #3a3a3a; }}
QPushButton:disabled {{ color: #5c5c5c; border-color: #3a3a3a; }}
QPushButton#Primary {{ background: {C['button']}; border-color: {C['button']}; color: #ffffff; }}
QPushButton#Primary:hover {{ background: #0d66d0; border-color: #0d66d0; }}
QPushButton#Primary:disabled {{ background: #26364a; border-color: #26364a; color: #75839a; }}
QPushButton#Flat {{ border: none; border-radius: 3px; padding: 3px 8px; color: #b4b4b4;
    font-weight: normal; min-height: 16px; }}
QPushButton#Flat:hover {{ background: #333333; color: #ffffff; }}
QPushButton#Flat:disabled {{ color: #555555; background: transparent; }}
QPushButton#Tool {{ border: none; border-radius: 3px; padding: 0; min-width: 26px; max-width: 26px;
    min-height: 24px; max-height: 24px; }}
QPushButton#Tool:hover {{ background: #333333; }}
QPushButton#Tool:pressed {{ background: #3d3d3d; }}
QPushButton#Tool:disabled {{ background: transparent; }}
QComboBox {{ background: #1b1b1b; border: 1px solid {C['border']}; border-radius: 3px;
    padding: 3px 8px; color: {C['bright']}; min-width: 170px; }}
QComboBox:hover {{ border-color: #5a5a5a; }}
QComboBox QAbstractItemView {{ background: {C['raised']}; border: 1px solid {C['border']};
    selection-background-color: {C['button']}; color: {C['bright']}; outline: 0; }}
QProgressBar {{ background: #1b1b1b; border: none; border-radius: 2px; }}
QProgressBar::chunk {{ background: {C['blue']}; border-radius: 2px; }}
QSplitter::handle {{ background: {C['gutter']}; }}
QToolTip {{ background: {C['raised']}; color: {C['bright']}; border: 1px solid {C['border']}; padding: 4px; }}
QMessageBox, QInputDialog {{ background: {C['raised']}; }}
QMessageBox QLabel, QInputDialog QLabel {{ color: #dadada; }}
QLineEdit {{ background: #1b1b1b; border: 1px solid {C['border']}; border-radius: 3px; padding: 4px;
    color: {C['bright']}; selection-background-color: {C['button']}; }}
QLineEdit:focus {{ border-color: {C['blue']}; }}
#DropZone {{ border: 2px dashed #3d3d3d; border-radius: 8px; background: #202020; }}
#DropZone[drag="true"] {{ border-color: {C['blue']}; background: #1d2633; }}
#DropTitle {{ color: {C['bright']}; font-size: 12pt; font-weight: 600; }}
#DropHint {{ color: #7f7f7f; }}
#StatusBar {{ background: #1d1d1d; border-top: 1px solid #101010; }}
#StatusBar QLabel {{ color: {C['dim']}; font-size: 8pt; }}
"""

STATUS = {
    "analyzing": ("Распаковка…", C["blue"]),
    "ready": ("Готов к установке", C["dim"]),
    "empty": ("Нечего устанавливать", C["yellow"]),
    "error": ("Ошибка", C["red"]),
    "waiting": ("В очереди", C["blue"]),
    "installing": ("Установка…", C["blue"]),
    "installed": ("Установлен", C["green"]),
    "partial": ("С ошибками", C["red"]),
}
INSTALLABLE = {"ready", "installed", "partial"}


# ---------------------------------------------------------------- graphics

LOGO_BG, LOGO_FG = "#002a26", "#2ee6b8"   # свой цвет, чтобы не путать с иконками Adobe


def make_logo_pixmap(size):
    """Значок в духе иконок Adobe: тёмная скруглённая плашка и буквы «Pi» (Plugin Installer)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    inset = size * 0.03
    rect = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(LOGO_BG))
    p.drawRoundedRect(rect, size * 0.19, size * 0.19)
    font = QFont("Segoe UI Semibold")
    font.setPixelSize(round(size * (0.62 if size <= 24 else 0.56 if size <= 48 else 0.5)))
    font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 97)
    p.setFont(font)
    p.setPen(QColor(LOGO_FG))
    p.drawText(rect.adjusted(0, -size * 0.02, 0, 0), Qt.AlignmentFlag.AlignCenter, "Pi")
    p.end()
    return pm


def _glyph_import(path):
    path.moveTo(12, 3.5)
    path.lineTo(12, 14.5)
    path.moveTo(7.5, 10)
    path.lineTo(12, 14.5)
    path.lineTo(16.5, 10)
    path.moveTo(4, 14)
    path.lineTo(4, 20)
    path.lineTo(20, 20)
    path.lineTo(20, 14)


def _glyph_add(path):
    path.moveTo(12, 5)
    path.lineTo(12, 19)
    path.moveTo(5, 12)
    path.lineTo(19, 12)


def _glyph_trash(path):
    path.moveTo(4.5, 7)
    path.lineTo(19.5, 7)
    path.moveTo(9.5, 7)
    path.lineTo(9.5, 4.5)
    path.lineTo(14.5, 4.5)
    path.lineTo(14.5, 7)
    path.moveTo(6.5, 7)
    path.lineTo(7.5, 20)
    path.lineTo(16.5, 20)
    path.lineTo(17.5, 7)
    path.moveTo(10.25, 10.5)
    path.lineTo(10.25, 16.5)
    path.moveTo(13.75, 10.5)
    path.lineTo(13.75, 16.5)


def _glyph_clear(path):
    for y, x2 in ((6, 15), (11, 15), (16, 9.5)):
        path.moveTo(4, y)
        path.lineTo(x2, y)
    path.moveTo(13, 14)
    path.lineTo(19, 20)
    path.moveTo(19, 14)
    path.lineTo(13, 20)


def _glyph_folder(path):
    path.moveTo(3.5, 18.5)
    path.lineTo(3.5, 5.5)
    path.lineTo(9.5, 5.5)
    path.lineTo(11.5, 8)
    path.lineTo(20.5, 8)
    path.lineTo(20.5, 18.5)
    path.closeSubpath()
    path.moveTo(3.5, 11)
    path.lineTo(20.5, 11)


GLYPHS = {"import": _glyph_import, "add": _glyph_add, "trash": _glyph_trash, "clear": _glyph_clear,
          "folder": _glyph_folder}


def line_pixmap(kind, size, color, stroke=1.4):
    """Тонкая контурная иконка как в панелях After Effects (рисунок в сетке 24×24)."""
    ratio = 2  # рисуем с запасом для экранов с масштабом 150–200%
    pm = QPixmap(size * ratio, size * ratio)
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    scale = size / 24
    p.scale(scale, scale)
    p.setPen(QPen(QColor(color), stroke / scale, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                  Qt.PenJoinStyle.RoundJoin))
    path = QPainterPath()
    GLYPHS[kind](path)
    p.drawPath(path)
    p.end()
    return pm


def line_icon(kind, size=16):
    icon = QIcon()
    icon.addPixmap(line_pixmap(kind, size, "#bdbdbd"), QIcon.Mode.Normal)
    icon.addPixmap(line_pixmap(kind, size, "#ffffff"), QIcon.Mode.Active)
    icon.addPixmap(line_pixmap(kind, size, "#4e4e4e"), QIcon.Mode.Disabled)
    return icon


def make_icon():
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(make_logo_pixmap(s))
    return icon


def dot_icon(color):
    pm = QPixmap(14, 14)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(3.5, 3.5, 7, 7))
    p.end()
    return QIcon(pm)


def dark_title_bar(widget):
    try:
        val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 20H1+ / старые сборки)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(int(widget.winId()), attr,
                                                          ctypes.byref(val), 4) == 0:
                break
    except Exception:
        pass


def plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)


# ---------------------------------------------------------------- elevated worker

class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


def launch_worker(port, token, elevate):
    """Запускает копию программы в режиме --worker (через UAC, если elevate). Возвращает handle процесса."""
    args = ["--worker", str(port), token]
    if not getattr(sys, "frozen", False):  # из исходников: python -m ae_plugin_installer --worker ...
        args = ["-m", "ae_plugin_installer"] + args
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas" if elevate else "open"
    info.lpFile = sys.executable
    info.lpParameters = subprocess.list2cmdline(args)
    info.lpDirectory = str(core.APP_DIR)
    info.nShow = 0  # SW_HIDE
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == 1223:  # ERROR_CANCELLED
            raise OSError(err, "запрос прав администратора отклонён")
        raise OSError(err, ctypes.FormatError(err).strip())
    return info.hProcess


def process_running(handle):
    return ctypes.windll.kernel32.WaitForSingleObject(wintypes.HANDLE(handle), 0) == 0x102


class RemoteUI:
    """UI для core.execute внутри процесса-установщика: всё отправляет окну."""

    def __init__(self, f):
        self.f = f

    def _send(self, **msg):
        self.f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.f.flush()

    def _reply(self):
        line = self.f.readline()
        if not line:
            raise ConnectionError("окно установщика закрыто")
        return json.loads(line)

    def step(self, text):
        self._send(t="step", msg=text)

    def ok(self, msg):
        self._send(t="ok", msg=msg)

    def info(self, msg):
        self._send(t="info", msg=msg)

    def warn(self, msg):
        self._send(t="warn", msg=msg)

    def error(self, msg):
        self._send(t="error", msg=msg)

    def ask(self, question, default=True):
        self._send(t="ask", msg=question, default=default)
        return bool(self._reply().get("answer", default))

    def manual(self, text):
        self._send(t="manual", msg=text)
        self._reply()

    def remaining(self, texts):
        self._send(t="remaining", items=texts)


def run_worker(port, token):
    try:
        sock = socket.create_connection(("127.0.0.1", int(port)), timeout=30)
        sock.settimeout(None)
    except OSError:
        return 1
    f = sock.makefile("rw", encoding="utf-8", newline="\n")
    ui = RemoteUI(f)
    try:
        ui._send(t="hello", token=token, admin=core.is_admin())
        job = json.loads(f.readline())
        if job.get("sandbox"):
            core.set_log_file(Path(job["sandbox"]) / "install_log.txt")
        roots = [Path(p) for p in job["ae_roots"]]
        for i, pd in enumerate(job["plans"]):
            ui._send(t="archive_start", i=i)
            try:
                ok = core.execute(core.Plan.from_dict(pd), ui, roots, job.get("sandbox"))
            except ConnectionError:
                raise
            except Exception as e:
                ui.error(f"сбой установки: {e}")
                ok = False
            ui._send(t="archive_done", i=i, ok=ok)
        ui._send(t="done")
    except (ConnectionError, OSError, ValueError):
        return 1
    finally:
        sock.close()
    return 0


# ---------------------------------------------------------------- background jobs

@dataclass
class Entry:
    id: int
    path: Path
    tmp: Path | None = None
    plan: core.Plan | None = None
    status: str = "analyzing"
    error: str = ""
    password: str | None = None


class AnalyzeSignals(QObject):
    done = Signal(int, object, str)  # id, plan|None, tmp|error


class AnalyzeTask(QRunnable):
    def __init__(self, entry: Entry):
        super().__init__()
        self.entry_id, self.path, self.password = entry.id, entry.path, entry.password
        self.signals = AnalyzeSignals()

    def run(self):
        tmp = Path(tempfile.mkdtemp(prefix="aeinst_"))
        try:
            core.extract(self.path, tmp, self.password)
            core.extract_nested(tmp, self.password)
            plan = core.analyze(self.path, tmp)
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            self.signals.done.emit(self.entry_id, None, str(e) or e.__class__.__name__)
            return
        self.signals.done.emit(self.entry_id, plan, str(tmp))


class InstallSession(QThread):
    message = Signal(str, object)
    archive_started = Signal(int)
    archive_finished = Signal(int, bool)
    request = Signal(str, str, object)   # "ask"/"manual", текст, {"event", "answer", "default"}
    completed = Signal(str)              # пустая строка — успех, иначе текст ошибки

    def __init__(self, plans, ae_roots, sandbox, elevate):
        super().__init__()
        self.job = {"plans": plans, "ae_roots": ae_roots, "sandbox": sandbox}
        self.elevate = elevate

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv.settimeout(0.5)
        token = secrets.token_hex(16)
        handle = None
        conn = None
        try:
            handle = launch_worker(srv.getsockname()[1], token, self.elevate)
            while conn is None:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    if not process_running(handle):
                        raise RuntimeError("процесс установки завершился, не успев начать")
            conn.settimeout(None)
            f = conn.makefile("rw", encoding="utf-8", newline="\n")
            hello = json.loads(f.readline() or "{}")
            if hello.get("token") != token:
                raise RuntimeError("неизвестный процесс подключился к установщику")
            f.write(json.dumps(self.job, ensure_ascii=False) + "\n")
            f.flush()
            finished = False
            for line in f:
                m = json.loads(line)
                t = m.get("t")
                if t in ("ask", "manual"):
                    holder = {"event": threading.Event(), "answer": m.get("default", True)}
                    self.request.emit(t, m.get("msg", ""), holder)
                    holder["event"].wait()
                    f.write(json.dumps({"answer": holder["answer"]}) + "\n")
                    f.flush()
                elif t == "archive_start":
                    self.archive_started.emit(m["i"])
                elif t == "archive_done":
                    self.archive_finished.emit(m["i"], bool(m["ok"]))
                elif t == "done":
                    finished = True
                    break
                else:
                    self.message.emit(t, m)
            if not finished:
                raise RuntimeError("процесс установки неожиданно прервался")
            self.completed.emit("")
        except OSError as e:
            self.completed.emit(e.strerror or str(e))
        except Exception as e:
            self.completed.emit(str(e))
        finally:
            if conn:
                conn.close()
            srv.close()
            if handle:
                ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))


# ---------------------------------------------------------------- widgets

class Panel(QFrame):
    """Панель как в After Effects: вкладка с названием и «≡», синяя рамка у активной."""

    def __init__(self, title):
        super().__init__()
        self.setObjectName("Panel")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 0, 1, 1)
        lay.setSpacing(0)
        header = QWidget()
        header.setObjectName("PanelHeader")
        header.setFixedHeight(30)
        self.header = QHBoxLayout(header)
        self.header.setContentsMargins(12, 0, 8, 0)
        self.header.setSpacing(6)
        tab = QLabel(title)
        tab.setObjectName("PanelTab")
        menu = QLabel("≡")
        menu.setObjectName("PanelMenu")
        self.header.addWidget(tab)
        self.header.addWidget(menu)
        self.header.addStretch()
        lay.addWidget(header)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        lay.addLayout(self.body, 1)

    def set_flag(self, name, value):
        if self.property(name) != value:
            self.setProperty(name, value)
            repolish(self)


class DropZone(QFrame):
    def __init__(self, on_browse):
        super().__init__()
        self.setObjectName("DropZone")
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(8)
        self.icon = icon = QLabel()
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_drag(False)
        title = QLabel("Перетащите архивы сюда")
        title.setObjectName("DropTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel("zip · rar · 7z · tar — плагины и скрипты After Effects\nс инструкцией по установке внутри")
        hint.setObjectName("DropHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        browse = QPushButton("Выбрать файлы…")
        browse.clicked.connect(on_browse)
        lay.addWidget(icon)
        lay.addWidget(title)
        lay.addWidget(hint)
        lay.addSpacing(6)
        lay.addWidget(browse, 0, Qt.AlignmentFlag.AlignCenter)

    def set_drag(self, on):
        self.icon.setPixmap(line_pixmap("import", 44, C["blue"] if on else "#8c8c8c", 1.6))
        self.setProperty("drag", on)
        repolish(self)


def no_drops(w):
    """Дочерние виджеты не перехватывают перетаскивание — его ловит окно целиком."""
    w.setAcceptDrops(False)
    if hasattr(w, "viewport"):
        w.viewport().setAcceptDrops(False)


# ---------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self, paths, sandbox=None):
        super().__init__()
        self.sandbox = sandbox
        self.entries: dict[int, Entry] = {}
        self.next_id = 1
        self.session: InstallSession | None = None
        self.session_ids: list[int] = []
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(2)
        self._tasks = set()

        self.setWindowTitle(APP_NAME + (" — песочница" if sandbox else ""))
        self.setWindowIcon(make_icon())
        self.resize(1180, 760)
        self.setMinimumSize(860, 560)
        self.setAcceptDrops(True)

        central = QWidget()
        central.setObjectName("Central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_topbar())

        work = QWidget()
        wl = QVBoxLayout(work)
        wl.setContentsMargins(4, 4, 4, 4)
        vsplit = QSplitter(Qt.Orientation.Vertical)
        vsplit.setHandleWidth(4)
        hsplit = QSplitter(Qt.Orientation.Horizontal)
        hsplit.setHandleWidth(4)
        hsplit.addWidget(self._build_archives_panel())
        hsplit.addWidget(self._build_plan_panel())
        hsplit.setSizes([420, 760])
        vsplit.addWidget(hsplit)
        vsplit.addWidget(self._build_log_panel())
        vsplit.setSizes([520, 200])
        wl.addWidget(vsplit)
        root.addWidget(work, 1)
        root.addWidget(self._build_statusbar())

        self.panels = [self.archives_panel, self.plan_panel, self.log_panel]
        QApplication.instance().focusChanged.connect(self._on_focus)
        dark_title_bar(self)
        self._refresh_buttons()
        self.render_plan()
        self.add_paths(paths)

    # ---------- layout

    def _build_topbar(self):
        bar = QWidget()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(44)
        h = QHBoxLayout(bar)
        h.setContentsMargins(12, 0, 12, 0)
        h.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(make_logo_pixmap(28))
        title = QLabel("Plugin Installer")
        title.setObjectName("AppTitle")
        sub = QLabel("для After Effects")
        sub.setObjectName("AppSub")
        h.addWidget(logo)
        h.addWidget(title)
        h.addWidget(sub)
        h.addStretch()
        lbl = QLabel("Версия After Effects:")
        lbl.setObjectName("AppSub")
        self.ae_combo = QComboBox()
        found = core.find_ae_installs()
        for p in reversed(found):
            self.ae_combo.addItem(p.name.replace("Adobe ", ""), [str(p)])
        if len(found) > 1:
            self.ae_combo.addItem("Во все версии", [str(p) for p in found])
        if not found:
            self.ae_combo.addItem("After Effects не найден", [])
            self.ae_combo.setEnabled(False)
        self.ae_combo.currentIndexChanged.connect(lambda _: self.render_plan())
        h.addWidget(lbl)
        h.addWidget(self.ae_combo)
        return bar

    def _build_archives_panel(self):
        p = self.archives_panel = Panel("Архивы")
        self.count_label = QLabel("")
        self.count_label.setObjectName("AppSub")
        p.header.addWidget(self.count_label)

        self.stack = QStackedWidget()
        zone_wrap = QWidget()
        zl = QVBoxLayout(zone_wrap)
        zl.setContentsMargins(12, 12, 12, 12)
        self.drop_zone = DropZone(self.browse)
        zl.addWidget(self.drop_zone)
        self.stack.addWidget(zone_wrap)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Имя", "Статус", "Размер"])
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setUniformRowHeights(True)
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setStretchLastSection(False)
        self.tree.itemSelectionChanged.connect(self.render_plan)
        no_drops(self.tree)
        self.stack.addWidget(self.tree)
        p.body.addWidget(self.stack, 1)

        footer = QWidget()
        footer.setObjectName("PanelFooter")
        f = QHBoxLayout(footer)
        f.setContentsMargins(6, 3, 6, 3)
        f.setSpacing(2)
        # как нижняя панель Project в AE: только значки, подписи — во всплывающих подсказках
        self.btn_add = QPushButton()
        self.btn_remove = QPushButton()
        self.btn_clear = QPushButton()
        for b, glyph, tip, fn in ((self.btn_add, "add", "Добавить архивы…", self.browse),
                                  (self.btn_remove, "trash", "Убрать выбранный архив", self.remove_selected),
                                  (self.btn_clear, "clear", "Очистить список", self.clear_all)):
            b.setObjectName("Tool")
            b.setIcon(line_icon(glyph))
            b.setToolTip(tip)
            b.clicked.connect(fn)
            f.addWidget(b)
        f.addStretch()
        p.body.addWidget(footer)
        return p

    def _build_plan_panel(self):
        p = self.plan_panel = Panel("План установки")
        self.btn_open = QPushButton("Открыть распакованное")
        self.btn_open.setObjectName("Flat")
        self.btn_open.setIcon(line_icon("folder"))
        self.btn_open.setToolTip("Открыть в Проводнике распакованный архив — для ручной установки.\n"
                                 "Это временная папка: она удаляется, когда архив убирают из списка "
                                 "или закрывают программу.")
        self.btn_open.clicked.connect(self.open_extracted)
        p.header.addWidget(self.btn_open)
        self.plan_view = QTextBrowser()
        self.plan_view.setOpenLinks(False)
        self.plan_view.document().setDocumentMargin(14)
        no_drops(self.plan_view)
        p.body.addWidget(self.plan_view, 1)

        footer = QWidget()
        footer.setObjectName("PanelFooter")
        f = QHBoxLayout(footer)
        f.setContentsMargins(12, 8, 12, 8)
        f.setSpacing(8)
        self.plan_summary = QLabel("")
        self.plan_summary.setObjectName("AppSub")
        f.addWidget(self.plan_summary)
        f.addStretch()
        self.btn_install_all = QPushButton("Установить все")
        self.btn_install_all.clicked.connect(self.install_all)
        self.btn_install = QPushButton("Установить")
        self.btn_install.setObjectName("Primary")
        self.btn_install.clicked.connect(self.install_selected)
        f.addWidget(self.btn_install_all)
        f.addWidget(self.btn_install)
        p.body.addWidget(footer)
        return p

    def _build_log_panel(self):
        p = self.log_panel = Panel("Журнал")
        self.progress = QProgressBar()
        self.progress.setFixedSize(160, 4)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("AppSub")
        p.header.addWidget(self.progress_label)
        p.header.addWidget(self.progress)
        btn = QPushButton("Очистить")
        btn.setObjectName("Flat")
        p.header.addWidget(btn)
        self.log_view = QTextBrowser()
        self.log_view.document().setDocumentMargin(10)
        no_drops(self.log_view)
        btn.clicked.connect(self.log_view.clear)
        p.body.addWidget(self.log_view, 1)
        return p

    def _build_statusbar(self):
        bar = QWidget()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(24)
        h = QHBoxLayout(bar)
        h.setContentsMargins(12, 0, 6, 0)
        h.setSpacing(18)
        z = core.find_7z()
        h.addWidget(QLabel(f"7-Zip: {'найден' if z else 'не найден — только zip/tar'}"))
        if self.sandbox:
            h.addWidget(QLabel(f"Песочница: {self.sandbox}"))
        elif core.is_admin():
            h.addWidget(QLabel("Запущено от администратора — перетаскивание из Проводника "
                               "может не работать, используйте «Добавить»"))
        else:
            h.addWidget(QLabel("Права администратора запрашиваются при установке"))
        h.addStretch()
        log_btn = QPushButton("Журнал установок")
        log_btn.setObjectName("Flat")
        log_btn.clicked.connect(self.open_log_file)
        h.addWidget(log_btn)
        return bar

    # ---------- panels highlight / drag & drop

    def _on_focus(self, _old, new):
        for p in self.panels:
            p.set_flag("active", bool(new) and p.isAncestorOf(new))

    def _set_drag(self, on):
        self.archives_panel.set_flag("drag", on and self.stack.currentIndex() == 1)
        self.drop_zone.set_drag(on)

    @staticmethod
    def _local_paths(event):
        md = event.mimeData()
        if not md.hasUrls():
            return []
        return [Path(u.toLocalFile()) for u in md.urls() if u.isLocalFile()]

    def dragEnterEvent(self, e):
        if self._local_paths(e):
            e.acceptProposedAction()
            self._set_drag(True)

    def dragMoveEvent(self, e):
        e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._set_drag(False)

    def dropEvent(self, e):
        self._set_drag(False)
        paths = self._local_paths(e)
        if paths:
            e.acceptProposedAction()
            self.add_paths(paths)

    # ---------- archives

    def browse(self):
        exts = " ".join(f"*{x}" for x in sorted(core.ARCHIVE_EXT))
        files, _ = QFileDialog.getOpenFileNames(self, "Выберите архивы", "",
                                                f"Архивы ({exts});;Все файлы (*)")
        self.add_paths([Path(f) for f in files])

    def add_paths(self, paths):
        known = {e.path for e in self.entries.values()}
        for p in paths:
            p = Path(p).resolve()
            found = sorted(x for x in p.iterdir() if x.is_file() and core.is_archive(x)) if p.is_dir() else [p]
            for a in found:
                if not (a.is_file() and core.is_archive(a)):
                    self.log("warn", f"Не архив, пропускаю: {a.name}")
                    continue
                if a in known:
                    continue
                known.add(a)
                e = Entry(self.next_id, a)
                self.next_id += 1
                self.entries[e.id] = e
                item = QTreeWidgetItem([a.name, "", self._size(a)])
                item.setData(0, Qt.ItemDataRole.UserRole, e.id)
                item.setToolTip(0, str(a))
                self.tree.addTopLevelItem(item)
                if self.tree.currentItem() is None:
                    self.tree.setCurrentItem(item)
                self._start_analyze(e)
        self._sync_list()

    @staticmethod
    def _size(p):
        try:
            mb = p.stat().st_size / 1048576
        except OSError:
            return ""
        return f"{mb:.1f} МБ" if mb >= 0.1 else f"{mb * 1024:.0f} КБ"

    def _start_analyze(self, e: Entry):
        e.status, e.error = "analyzing", ""
        self._update_item(e)
        task = AnalyzeTask(e)
        task.signals.done.connect(self._on_analyzed)
        self._tasks.add(task.signals)
        self.pool.start(task)

    def _on_analyzed(self, entry_id, plan, info):
        self._tasks.discard(self.sender())
        e = self.entries.get(entry_id)
        if e is None:  # архив убрали из списка, пока он распаковывался
            if plan is not None:
                shutil.rmtree(info, ignore_errors=True)
            return
        if plan is None:
            if "пароль" in info:
                pwd, ok = QInputDialog.getText(self, "Пароль", f"Архив «{e.path.name}» защищён паролем:",
                                               QLineEdit.EchoMode.Password)
                if ok and pwd:
                    e.password = pwd
                    self._start_analyze(e)
                    return
            e.status, e.error = "error", info
            self.log("error", f"{e.path.name}: не удалось распаковать — {info}")
        else:
            e.plan, e.tmp = plan, Path(info)
            e.status = "ready" if plan.actions else "empty"
        self._update_item(e)
        self._refresh_buttons()
        if self._current() is e:
            self.render_plan()

    def _item_for(self, entry_id):
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.data(0, Qt.ItemDataRole.UserRole) == entry_id:
                return it
        return None

    def _update_item(self, e: Entry):
        it = self._item_for(e.id)
        if it is None:
            return
        label, color = STATUS[e.status]
        it.setText(1, label)
        it.setIcon(1, dot_icon(color))
        it.setForeground(1, QColor(color if e.status != "ready" else C["text"]))
        it.setToolTip(1, e.error or label)

    def _current(self):
        it = self.tree.currentItem()
        return self.entries.get(it.data(0, Qt.ItemDataRole.UserRole)) if it else None

    def _drop_entry(self, e: Entry):
        if e.tmp:
            shutil.rmtree(e.tmp, ignore_errors=True)
        self.entries.pop(e.id, None)
        it = self._item_for(e.id)
        if it:
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(it))

    def remove_selected(self):
        e = self._current()
        if e and e.id not in self.session_ids:
            self._drop_entry(e)
            self._sync_list()

    def clear_all(self):
        for e in list(self.entries.values()):
            if e.id not in self.session_ids:
                self._drop_entry(e)
        self._sync_list()

    def _sync_list(self):
        n = len(self.entries)
        self.stack.setCurrentIndex(1 if n else 0)
        self.count_label.setText(f"{n}" if n else "")
        self._refresh_buttons()
        self.render_plan()

    def _refresh_buttons(self):
        busy = self.session is not None
        cur = self._current()
        self.btn_install.setEnabled(not busy and cur is not None and cur.status in INSTALLABLE)
        self.btn_install_all.setEnabled(not busy and any(e.status == "ready" for e in self.entries.values()))
        self.btn_remove.setEnabled(not busy and cur is not None)
        self.btn_clear.setEnabled(not busy and bool(self.entries))
        self.btn_open.setEnabled(cur is not None and cur.tmp is not None and cur.tmp.exists())

    def ae_roots(self):
        return [Path(p) for p in (self.ae_combo.currentData() or [])]

    # ---------- plan view

    def render_plan(self):
        self._refresh_buttons()
        e = self._current()
        H = html.escape
        if e is None:
            self.plan_summary.setText("")
            self.plan_view.setHtml(
                f"<div style='color:{C['dim']}; margin-top:40px' align='center'>"
                "Выберите архив слева, чтобы увидеть, что и куда будет установлено.</div>")
            return
        out = [f"<div style='font-size:12pt; font-weight:600; color:{C['bright']}'>{H(e.path.name)}</div>"]
        if e.status == "analyzing":
            out.append(f"<p style='color:{C['dim']}'>Распаковка архива и разбор инструкции…</p>")
            self.plan_summary.setText("")
            self.plan_view.setHtml("".join(out))
            return
        if e.status == "error":
            out.append(f"<p style='color:{C['red']}'>Не удалось распаковать: {H(e.error)}</p>")
            self.plan_summary.setText("")
            self.plan_view.setHtml("".join(out))
            return

        plan = e.plan
        roots = self.ae_roots()
        if plan.instruction:
            rel = plan.instruction.relative_to(plan.root)
            out.append(f"<div style='color:{C['dim']}'>Инструкция: {H(str(rel))}</div>")
        for w in plan.warnings:
            out.append(f"<div style='color:{C['yellow']}; margin-top:4px'>⚠ {H(w)}</div>")

        n_actions = len(plan.actions)
        n_manual = sum(1 for s in plan.steps if not s.actions and s.text)
        for step in plan.steps:
            cells = []
            label = (f"ШАГ {step.num}" if step.num is not None
                     else "ИНСТРУКЦИЯ" if step.text else "ПО ТИПУ ФАЙЛОВ")
            cells.append(f"<div style='color:{C['hot']}; font-size:8pt; font-weight:600'>{label}</div>")
            if step.text:
                text = "<br>".join(H(l) for l in step.text.splitlines() if l.strip())
                cells.append(f"<div style='color:#9d9d9d; margin-top:4px'>{text}</div>")
            for a in step.actions:
                cells.append(self._action_html(a, roots))
            if not step.actions and step.text:
                cells.append(f"<div style='color:{C['yellow']}; margin-top:8px'>✋ Этот шаг нужно сделать вручную</div>")
            out.append("<table width='100%' cellspacing='0' cellpadding='10' "
                       "style='margin-top:10px; background:#272727'>"
                       f"<tr><td>{''.join(cells)}</td></tr></table>")
        self.plan_view.setHtml("".join(out))
        self.plan_view.verticalScrollBar().setValue(0)
        parts = [plural(n_actions, "действие", "действия", "действий") if n_actions else "нет действий"]
        if n_manual:
            parts.append(plural(n_manual, "ручной шаг", "ручных шага", "ручных шагов"))
        self.plan_summary.setText(" · ".join(parts))

    def _action_html(self, a, roots):
        H = html.escape
        icon = {"run": "⚙", "reg": "⛭"}.get(a.kind, "▶")
        color = C["yellow"] if a.kind in ("run", "reg") else C["green"]
        lines = [f"<div style='margin-top:9px'><span style='color:{color}'>{icon}</span>&nbsp; "
                 f"{H(core.action_verb(a))} <b style='color:{C['bright']}'>{H(a.item.name)}</b></div>"]
        if a.kind == "reg":
            body = H(core.read_text(a.item).strip())
            lines.append(f"<pre style='color:{C['dim']}; font-size:8pt; margin-left:18px'>{body}</pre>")
        elif a.kind == "run":
            lines.append(f"<div style='color:{C['dim']}; margin-left:18px'>откроется окно установщика — "
                         "пройдите его до конца, следуя тексту шага</div>")
        else:
            targets = core.action_targets(a, roots)
            if not targets:
                lines.append(f"<div style='color:{C['red']}; margin-left:18px'>→ After Effects не найден</div>")
            for t, exists, odd in targets:
                note = ""
                if a.dest.guessed:
                    note += f" <span style='color:{C['dim']}'>(по типу файла)</span>"
                if exists:
                    what = "файлы будут перезаписаны" if a.item.is_dir() else "будет заменён"
                    note += f" <span style='color:{C['yellow']}'>уже есть — {what}</span>"
                if odd:
                    note += f" <span style='color:{C['red']}'>⚠ папка не похожа на папку Adobe</span>"
                lines.append(f"<div style='margin-left:18px'><span style='color:{C['dim']}'>→</span> "
                             f"<span style='color:{C['hot']}'>{H(str(t))}</span>{note}</div>")
        return "".join(lines)

    # ---------- log

    def log(self, kind, msg):
        H = html.escape
        colors = {"ok": C["green"], "warn": C["yellow"], "error": C["red"], "info": C["dim"],
                  "step": C["bright"], "head": C["hot"]}
        prefix = {"ok": "✓ ", "error": "✗ ", "warn": "", "info": "", "step": "", "head": ""}.get(kind, "")
        weight = "600" if kind in ("step", "head") else "normal"
        indent = "16px" if kind in ("ok", "error", "warn", "info") else "0"
        text = "<br>".join(H(l) for l in msg.splitlines()) or "&nbsp;"
        self.log_view.append(f"<div style='color:{colors.get(kind, C['text'])}; font-weight:{weight}; "
                             f"margin-left:{indent}'>{prefix}{text}</div>")
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def open_extracted(self):
        """Показывает распакованный архив в Проводнике: с выделенной инструкцией, если она есть."""
        e = self._current()
        if not e or not e.tmp or not e.tmp.exists():
            return
        if e.plan and e.plan.instruction and e.plan.instruction.exists():
            subprocess.Popen(["explorer", "/select,", str(e.plan.instruction)])
            return
        folder = e.tmp
        # пропускаем обёртки вида Plugin_v1\Plugin_v1\ — папки, где лежит только одна папка
        while True:
            children = list(folder.iterdir())
            if len(children) != 1 or not children[0].is_dir():
                break
            folder = children[0]
        os.startfile(folder)

    def open_log_file(self):
        f = core.LOG_FILE if not self.sandbox else Path(self.sandbox) / "install_log.txt"
        if f.exists():
            os.startfile(f)
        else:
            self.log("info", f"Журнал установок пока пуст ({f})")

    # ---------- install

    def install_selected(self):
        e = self._current()
        if e and e.status in INSTALLABLE:
            self.start_install([e])

    def install_all(self):
        self.start_install([e for e in self.entries.values() if e.status == "ready"])

    def start_install(self, entries):
        if self.session or not entries:
            return
        roots = self.ae_roots()
        if any(a.dest and a.dest.kind == "ae" for e in entries for a in e.plan.actions) and not roots:
            self.message_box("After Effects не найден",
                             "Часть файлов нужно положить в папку After Effects, но он не найден.", QMessageBox.Icon.Warning)
            return
        if not self.sandbox and core.ae_running():
            if not self.ask_box("After Effects запущен",
                                "After Effects сейчас открыт. Занятые им файлы не заменятся, а новые плагины "
                                "появятся только после перезапуска AE.\n\nВсё равно установить?",
                                yes="Установить", no="Отмена", default_yes=False):
                return
        elevate = not self.sandbox and not core.is_admin()
        self.session_ids = [e.id for e in entries]
        for e in entries:
            e.status = "waiting"
            self._update_item(e)
        self.progress.setRange(0, len(entries))
        self.progress.setValue(0)
        self.progress.show()
        self.progress_label.setText(f"0 / {len(entries)}")
        if elevate:
            self.log("info", "Запрашиваю права администратора…")

        s = self.session = InstallSession([e.plan.to_dict() for e in entries], [str(r) for r in roots],
                                          self.sandbox, elevate)
        s.message.connect(self._on_message)
        s.archive_started.connect(self._on_archive_started)
        s.archive_finished.connect(self._on_archive_finished)
        s.request.connect(self._on_request)
        s.completed.connect(self._on_completed)
        self._refresh_buttons()
        s.start()

    def _session_entry(self, i):
        return self.entries.get(self.session_ids[i]) if 0 <= i < len(self.session_ids) else None

    def _on_archive_started(self, i):
        e = self._session_entry(i)
        if e:
            e.status = "installing"
            self._update_item(e)
            self.log("head", f"══ {e.path.name}")

    def _on_archive_finished(self, i, ok):
        e = self._session_entry(i)
        if e:
            e.status = "installed" if ok else "partial"
            self._update_item(e)
            self.log("ok" if ok else "error", "Установлено" if ok else "Установка завершилась с ошибками")
        self.progress.setValue(i + 1)
        self.progress_label.setText(f"{i + 1} / {len(self.session_ids)}")

    def _on_message(self, kind, m):
        if kind == "step":
            self.log("step", m["msg"].splitlines()[0])
        elif kind == "remaining":
            self.log("warn", "Осталось сделать вручную:")
            for t in m.get("items", []):
                self.log("info", t)
        elif kind in ("ok", "info", "warn", "error"):
            self.log(kind, m["msg"])

    def _on_request(self, kind, text, holder):
        if kind == "manual":
            self.log("warn", "✋ Ждёт ручного шага")
            self.message_box("Сделайте этот шаг вручную", text + "\n\nНажмите «Готово», когда закончите.",
                             QMessageBox.Icon.Information, button="Готово")
            holder["answer"] = True
        else:
            holder["answer"] = self.ask_box(APP_NAME, text, default_yes=holder["answer"])
        holder["event"].set()

    def _on_completed(self, error):
        for i in range(len(self.session_ids)):
            e = self._session_entry(i)
            if e and e.status in ("waiting", "installing"):
                e.status = "partial" if e.status == "installing" else "ready"
                self._update_item(e)
        if error:
            self.log("error", f"Установка не выполнена: {error}")
        else:
            done = sum(1 for i in range(len(self.session_ids))
                       if (e := self._session_entry(i)) and e.status == "installed")
            self.log("head", f"Готово: установлено {done} из {len(self.session_ids)}")
        self.session.wait()
        self.session = None
        self.session_ids = []
        self.progress.hide()
        self.progress_label.setText("")
        self._refresh_buttons()
        self.render_plan()

    # ---------- dialogs

    def message_box(self, title, text, icon=QMessageBox.Icon.Information, button="OK"):
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        box.setIcon(icon)
        b = box.addButton(button, QMessageBox.ButtonRole.AcceptRole)
        b.setObjectName("Primary")
        dark_title_bar(box)
        box.exec()

    def ask_box(self, title, text, yes="Да", no="Нет", default_yes=True):
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        box.setIcon(QMessageBox.Icon.Question)
        y = box.addButton(yes, QMessageBox.ButtonRole.AcceptRole)
        n = box.addButton(no, QMessageBox.ButtonRole.RejectRole)
        (y if default_yes else n).setObjectName("Primary")
        box.setDefaultButton(y if default_yes else n)
        dark_title_bar(box)
        box.exec()
        return box.clickedButton() is y

    def closeEvent(self, event):
        if self.session:
            self.message_box("Идёт установка", "Дождитесь окончания установки.", QMessageBox.Icon.Warning)
            event.ignore()
            return
        self.pool.waitForDone(3000)
        for e in self.entries.values():
            if e.tmp:
                shutil.rmtree(e.tmp, ignore_errors=True)
        event.accept()


# ---------------------------------------------------------------- entry points

def dark_palette():
    pal = QPalette()
    for role, color in (
            (QPalette.ColorRole.Window, C["panel"]), (QPalette.ColorRole.WindowText, C["text"]),
            (QPalette.ColorRole.Base, C["panel2"]), (QPalette.ColorRole.AlternateBase, "#262626"),
            (QPalette.ColorRole.Text, "#d0d0d0"), (QPalette.ColorRole.Button, C["raised"]),
            (QPalette.ColorRole.ButtonText, "#e0e0e0"), (QPalette.ColorRole.Highlight, C["button"]),
            (QPalette.ColorRole.HighlightedText, "#ffffff"), (QPalette.ColorRole.ToolTipBase, C["raised"]),
            (QPalette.ColorRole.ToolTipText, C["bright"]), (QPalette.ColorRole.PlaceholderText, "#6a6a6a"),
            (QPalette.ColorRole.Link, C["blue"])):
        pal.setColor(role, QColor(color))
    return pal


def run_gui(argv):
    paths, sandbox = [], None
    it = iter(argv)
    for a in it:
        if a == "--sandbox":
            sandbox = next(it, None)
        else:
            paths.append(a)
    try:  # своя иконка в панели задач, а не иконка python.exe
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("vvvkt.AEPluginInstaller")
    except Exception:
        pass
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    app.setFont(QFont("Segoe UI", 9))
    app.setStyleSheet(QSS)
    app.setWindowIcon(make_icon())
    w = MainWindow(paths, sandbox)
    w.show()
    return app.exec()


def main(argv=None):
    """Окно; с --worker PORT TOKEN — процесс установки, который окно запускает через UAC."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 3 and argv[0] == "--worker":
        return run_worker(argv[1], argv[2])
    return run_gui(argv)
