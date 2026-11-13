"""Windows desktop control center for preparing and running X001."""

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

from x001.adb import DEFAULT_ADB_PATH, ADBController, inspect_device

TITLE = "X001 智能控制中心"
CONSOLE_URL = "http://127.0.0.1:8765"
BACKGROUND = "#F4F7FB"
SURFACE = "#FFFFFF"
INK = "#182230"
MUTED = "#667085"
BORDER = "#E4EAF2"
PRIMARY = "#3157D5"
PRIMARY_DARK = "#2445B5"
GREEN = "#12805C"
AMBER = "#B54708"
RED = "#C4320A"
FONT = "Microsoft YaHei UI"


def project_root() -> Path:
    """Find the source project next to the executable or this module."""
    location = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )
    for candidate in (location, *location.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "src" / "x001" / "web.py"
        ).is_file():
            return candidate
    raise FileNotFoundError(
        "没有找到 X001 项目文件夹。请将启动器放在项目根目录或其 dist 文件夹中。"
    )


def bundled_uv() -> Path | None:
    """Return the uv executable included in the frozen launcher, if present."""
    if getattr(sys, "frozen", False):
        path = Path(getattr(sys, "_MEIPASS", "")) / "uv.exe"
        if path.is_file():
            return path
    found = shutil.which("uv")
    return Path(found) if found else None


def port_available(port: int = 8765) -> bool:
    """The console intentionally uses a stable address instead of random ports."""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def read_health(base_url: str = CONSOLE_URL, timeout: float = 1.5) -> dict | None:
    """Read service and database health without loading the console page."""
    try:
        with urllib.request.urlopen(
            f"{base_url}/api/health", timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except OSError, ValueError:
            return None
    except OSError, ValueError, urllib.error.URLError:
        return None
    return payload if isinstance(payload, dict) else None


def healthy_console(base_url: str = CONSOLE_URL) -> bool:
    health = read_health(base_url)
    return bool(
        health
        and health.get("ok")
        and health.get("service") == "x001"
        and health.get("database_ready")
    )


class Launcher:
    def __init__(self, window: tk.Tk):
        self.window = window
        self.window.title(TITLE)
        self.window.geometry("1040x620")
        self.window.minsize(920, 580)
        self.window.configure(bg=BACKGROUND)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.project: Path | None = None
        self.server: subprocess.Popen[str] | None = None
        self.server_owned = False
        self.server_url = ""
        self.busy = False
        self.closing = False
        self.phase = "idle"
        self.server_stop_requested = False
        self.cancel_requested = threading.Event()
        self.monitor_stop = threading.Event()
        self.monitor_stop.set()
        self.command_process: subprocess.Popen[str] | None = None
        self.device_checking = False
        self.last_error = ""
        self.card_states: dict[str, str] = {}
        self.card_widgets: dict[str, tuple[tk.Label, tk.Label, tk.Label]] = {}

        self._configure_styles()
        self._build()
        try:
            self.project = project_root()
            self._log("已找到项目文件夹，准备自动检查运行环境。")
        except FileNotFoundError as error:
            self.last_error = str(error)
            self.status_label.configure(text="需要放回 X001 项目文件夹", fg=RED)
            self.step_label.configure(text=str(error))
            self.start_button.configure(state=tk.DISABLED)
            self.error_label.configure(text=str(error))
            self.error_frame.grid()
        self.window.after(120, self._drain_events)
        if self.project is not None:
            self.window.after(450, self.start)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.window)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(
            "Primary.TButton",
            background=PRIMARY,
            foreground="white",
            borderwidth=0,
            focusthickness=0,
            padding=(22, 13),
            font=(FONT, 11, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[
                ("disabled", "#91A3D7"),
                ("active", PRIMARY_DARK),
                ("pressed", PRIMARY_DARK),
            ],
            foreground=[("disabled", "#F4F6FD")],
        )
        style.configure(
            "Secondary.TButton",
            background=SURFACE,
            foreground=INK,
            bordercolor=BORDER,
            borderwidth=1,
            padding=(15, 10),
            font=(FONT, 9),
        )
        style.map(
            "Secondary.TButton",
            background=[("disabled", "#F5F7FA"), ("active", "#F0F3F9")],
            foreground=[("disabled", "#A2ACBA")],
        )
        style.configure(
            "Danger.TButton",
            background=SURFACE,
            foreground=RED,
            bordercolor="#E8C9C2",
            borderwidth=1,
            padding=(15, 10),
            font=(FONT, 9),
        )
        style.map("Danger.TButton", background=[("active", "#FFF4F1")])
        style.configure(
            "X001.Horizontal.TProgressbar",
            troughcolor="#E9EEF7",
            background=PRIMARY,
            bordercolor="#E9EEF7",
            lightcolor=PRIMARY,
            darkcolor=PRIMARY,
            thickness=5,
        )

    def _build(self) -> None:
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(1, weight=1)

        header = tk.Frame(
            self.window,
            bg=SURFACE,
            padx=24,
            pady=10,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        header.grid(row=0, column=0, sticky="ew")
        logo = tk.Canvas(header, width=32, height=32, bg=SURFACE, highlightthickness=0)
        logo.pack(side=tk.LEFT, padx=(0, 10))
        logo.create_rectangle(0, 0, 32, 32, fill=PRIMARY, outline="")
        logo.create_text(16, 16, text="X", fill="white", font=(FONT, 15, "bold"))
        brand = tk.Frame(header, bg=SURFACE)
        brand.pack(side=tk.LEFT)
        tk.Label(
            brand,
            text="X001 智能控制中心",
            bg=SURFACE,
            fg=INK,
            font=(FONT, 12, "bold"),
        ).pack(anchor="w")
        tk.Label(
            brand,
            text="货源采集与数据管理",
            bg=SURFACE,
            fg=MUTED,
            font=(FONT, 8),
        ).pack(anchor="w", pady=(1, 0))
        tk.Label(
            header,
            text="●  本地安全运行",
            bg="#EAF8F2",
            fg=GREEN,
            padx=10,
            pady=4,
            font=(FONT, 8, "bold"),
        ).pack(side=tk.RIGHT)

        content = tk.Frame(self.window, bg=BACKGROUND, padx=24, pady=12)
        content.grid(row=1, column=0, sticky="nsew")
        for column in range(4):
            content.columnconfigure(column, weight=1, uniform="status")
        content.rowconfigure(6, weight=1)

        hero = tk.Frame(content, bg=BACKGROUND)
        hero.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        tk.Label(
            hero,
            text="一键连接手机与货源操作台",
            bg=BACKGROUND,
            fg=INK,
            font=(FONT, 18, "bold"),
        ).pack(side=tk.LEFT)
        self.state_badge = tk.Label(
            hero,
            text="等待自动检查",
            bg="#E9EEF7",
            fg=MUTED,
            padx=12,
            pady=6,
            font=(FONT, 9, "bold"),
        )
        self.state_badge.pack(side=tk.RIGHT, anchor="center")
        tk.Label(
            content,
            text="自动检查环境、启动本地服务并检测手机；复杂技术信息已收进诊断面板。",
            bg=BACKGROUND,
            fg=MUTED,
            font=(FONT, 9),
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))

        cards = (
            ("runtime", "01  运行环境", "等待检测", "Python 与必要组件"),
            ("web", "02  网页服务", "尚未启动", "固定地址 · 127.0.0.1:8765"),
            ("device", "03  手机连接", "等待检测", "连接、授权与锁屏状态"),
            ("app", "04  目标应用", "等待检测", "货车帮司机"),
        )
        for column, (key, title, value, detail) in enumerate(cards):
            self._status_card(content, column, key, title, value, detail)

        action = tk.Frame(
            content,
            bg=SURFACE,
            padx=18,
            pady=12,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        action.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        action.columnconfigure(0, weight=1)
        text = tk.Frame(action, bg=SURFACE)
        text.grid(row=0, column=0, sticky="ew", padx=(0, 20))
        self.status_label = tk.Label(
            text,
            text="即将开始自动检查",
            bg=SURFACE,
            fg=INK,
            font=(FONT, 12, "bold"),
            anchor="w",
        )
        self.status_label.pack(fill=tk.X)
        self.step_label = tk.Label(
            text,
            text="无需先打开浏览器或手动启动网页服务",
            bg=SURFACE,
            fg=MUTED,
            font=(FONT, 8),
            anchor="w",
        )
        self.step_label.pack(fill=tk.X, pady=(2, 6))
        self.progress = ttk.Progressbar(
            text, mode="indeterminate", style="X001.Horizontal.TProgressbar"
        )
        self.progress.pack(fill=tk.X)
        self.progress.pack_forget()

        buttons = tk.Frame(action, bg=SURFACE)
        buttons.grid(row=0, column=1, sticky="e")
        self.start_button = ttk.Button(
            buttons,
            text="一键启动",
            style="Primary.TButton",
            command=self._primary_action,
        )
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(
            buttons,
            text="取消启动",
            style="Secondary.TButton",
            command=self.stop,
            state=tk.DISABLED,
        )
        self.stop_button.pack(side=tk.LEFT, padx=(10, 0))

        self.error_frame = tk.Frame(
            content,
            bg="#FFF3F0",
            padx=15,
            pady=11,
            highlightthickness=1,
            highlightbackground="#F2D2CA",
        )
        self.error_frame.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        self.error_frame.columnconfigure(0, weight=1)
        self.error_label = tk.Label(
            self.error_frame,
            text="",
            bg="#FFF3F0",
            fg=RED,
            font=(FONT, 9),
            anchor="w",
            justify=tk.LEFT,
        )
        self.error_label.grid(row=0, column=0, sticky="ew")
        tk.Button(
            self.error_frame,
            text="查看原因",
            command=self.show_error_details,
            bg="#FFF3F0",
            fg=RED,
            activebackground="#FFE8E2",
            relief=tk.FLAT,
            cursor="hand2",
            font=(FONT, 9, "bold"),
            padx=8,
        ).grid(row=0, column=1, padx=(12, 0))
        self.error_frame.grid_remove()

        guide = tk.Frame(content, bg="#EDF3FF", padx=14, pady=7)
        guide.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        guide.columnconfigure(0, weight=1)
        self.guide_label = tk.Label(
            guide,
            text=(
                "手机已连接时会自动唤醒并打开货车帮司机；"
                "USB 授权、锁屏密码和采集线路由你确认。"
            ),
            bg="#EDF3FF",
            fg="#37549B",
            font=(FONT, 9),
            anchor="w",
            justify=tk.LEFT,
            wraplength=720,
        )
        self.guide_label.grid(row=0, column=0, sticky="ew")
        self.device_button = tk.Button(
            guide,
            text="重新检测手机",
            command=self.refresh_device,
            bg="#EDF3FF",
            fg=PRIMARY,
            activebackground="#DFE9FF",
            relief=tk.FLAT,
            cursor="hand2",
            font=(FONT, 9, "bold"),
            state=tk.DISABLED,
        )
        self.device_button.grid(row=0, column=1, padx=(12, 0))

        self.diagnostics_frame = tk.Frame(
            content, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER
        )
        self.diagnostics_frame.columnconfigure(0, weight=1)
        self.diagnostics_frame.rowconfigure(0, weight=1)
        self.log_box = scrolledtext.ScrolledText(
            self.diagnostics_frame,
            bg="#111A2E",
            fg="#D5DEEF",
            insertbackground="white",
            relief=tk.FLAT,
            borderwidth=0,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Consolas", 9),
            padx=14,
            pady=12,
        )
        self.log_box.grid(row=0, column=0, sticky="nsew")
        self.diagnostics_visible = False

        footer = tk.Frame(self.window, bg=BACKGROUND, padx=24, pady=6)
        footer.grid(row=2, column=0, sticky="ew")
        self.diagnostics_button = tk.Button(
            footer,
            text="▸  诊断日志",
            command=self.toggle_diagnostics,
            bg=BACKGROUND,
            fg=MUTED,
            activebackground=BACKGROUND,
            relief=tk.FLAT,
            cursor="hand2",
            font=(FONT, 8, "bold"),
            padx=0,
            pady=4,
        )
        self.diagnostics_button.pack(side=tk.LEFT)
        self.copy_button = tk.Button(
            footer,
            text="复制诊断信息",
            command=self.copy_diagnostics,
            bg=BACKGROUND,
            fg=MUTED,
            activebackground=BACKGROUND,
            relief=tk.FLAT,
            cursor="hand2",
            font=(FONT, 8),
            padx=0,
            pady=4,
        )
        tk.Button(
            footer,
            text="打开数据文件夹",
            command=self.open_data_folder,
            bg=BACKGROUND,
            fg=PRIMARY,
            activebackground=BACKGROUND,
            relief=tk.FLAT,
            cursor="hand2",
            font=(FONT, 8, "bold"),
            padx=0,
            pady=4,
        ).pack(side=tk.LEFT, padx=(16, 0))
        tk.Label(
            footer,
            text="X001  ·  数据仅保存在这台电脑",
            bg=BACKGROUND,
            fg=MUTED,
            font=(FONT, 8),
        ).pack(side=tk.RIGHT)

        icon = tk.PhotoImage(width=32, height=32)
        icon.put(PRIMARY, to=(0, 0, 32, 32))
        for index in range(4, 28):
            icon.put("white", to=(index, index, index + 2, index + 2))
            icon.put("white", to=(index, 30 - index, index + 2, 32 - index))
        self.window.iconphoto(True, icon)
        self._window_icon = icon

    def _status_card(
        self,
        parent: tk.Frame,
        column: int,
        key: str,
        title: str,
        value: str,
        detail: str,
    ) -> None:
        card = tk.Frame(
            parent,
            bg=SURFACE,
            padx=13,
            pady=10,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        card.grid(
            row=2,
            column=column,
            sticky="nsew",
            padx=(0 if column == 0 else 5, 0 if column == 3 else 5),
        )
        card.grid_propagate(False)
        card.configure(width=226, height=104)
        heading = tk.Frame(card, bg=SURFACE)
        heading.pack(fill=tk.X)
        dot = tk.Label(heading, text="●", bg=SURFACE, fg="#98A2B3", font=(FONT, 7))
        dot.pack(side=tk.LEFT)
        tk.Label(
            heading,
            text=title,
            bg=SURFACE,
            fg=MUTED,
            font=(FONT, 8, "bold"),
        ).pack(side=tk.LEFT, padx=(6, 0))
        value_label = tk.Label(
            card, text=value, bg=SURFACE, fg=INK, font=(FONT, 10, "bold"), anchor="w"
        )
        value_label.pack(fill=tk.X, pady=(11, 0))
        detail_label = tk.Label(
            card,
            text=detail,
            bg=SURFACE,
            fg=MUTED,
            font=(FONT, 8),
            anchor="w",
            justify=tk.LEFT,
            wraplength=188,
        )
        detail_label.pack(fill=tk.X, pady=(3, 0))
        self.card_widgets[key] = (dot, value_label, detail_label)
        self.card_states[key] = "idle"

    def _set_card(self, key: str, level: str, value: str, detail: str) -> None:
        dot, value_label, detail_label = self.card_widgets[key]
        color = {
            "ok": GREEN,
            "checking": PRIMARY,
            "warning": AMBER,
            "error": RED,
            "idle": "#98A2B3",
        }[level]
        dot.configure(fg=color)
        value_label.configure(text=value, fg=color if level != "idle" else INK)
        detail_label.configure(text=detail)
        self.card_states[key] = level

    def _log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.insert(tk.END, f"[{stamp}] {line.rstrip()}\n")
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > 300:
            self.log_box.delete("1.0", f"{lines - 300}.0")
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)

    def toggle_diagnostics(self) -> None:
        self.diagnostics_visible = not self.diagnostics_visible
        content = self.diagnostics_frame.master
        width = self.window.winfo_width()
        if self.diagnostics_visible:
            self.window.geometry(f"{width}x720")
            content.rowconfigure(6, minsize=150, weight=1)
            self.diagnostics_frame.grid(
                row=6, column=0, columnspan=4, sticky="nsew", pady=(6, 0)
            )
            self.copy_button.pack(side=tk.LEFT, padx=(12, 0))
            self.diagnostics_button.configure(text="▾  收起诊断日志")
        else:
            self.window.geometry(f"{width}x620")
            content.rowconfigure(6, minsize=0, weight=1)
            self.diagnostics_frame.grid_remove()
            self.copy_button.pack_forget()
            self.diagnostics_button.configure(text="▸  诊断日志")

    def copy_diagnostics(self) -> None:
        self.window.clipboard_clear()
        self.window.clipboard_append(self.log_box.get("1.0", tk.END))
        self.copy_button.configure(text="已复制")
        self.window.after(1500, lambda: self.copy_button.configure(text="复制诊断信息"))

    def show_error_details(self) -> None:
        if not self.diagnostics_visible:
            self.toggle_diagnostics()
        messagebox.showinfo(
            "问题与处理建议",
            f"{self.last_error}\n\n建议：根据提示检查手机授权、锁屏、货车帮司机安装情况，"
            "或确认 8765 端口未被其他程序占用。然后点击“重试启动”。",
        )

    def _drain_events(self) -> None:
        while True:
            try:
                event, value = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "log":
                self._log(str(value))
            elif event == "card":
                key, level, label, detail = value
                self._set_card(key, level, label, detail)
            elif event == "progress":
                step, message = value
                self.status_label.configure(text=message, fg=PRIMARY)
                self.step_label.configure(text=f"步骤 {step}/4  ·  请保持窗口打开")
            elif event == "device":
                self._apply_device(value)
            elif event == "ready":
                if self.closing:
                    threading.Thread(target=self._stop_server, daemon=True).start()
                    continue
                self._on_ready(str(value))
            elif event == "failed":
                self._on_failed(str(value))
                if self.closing:
                    self.window.destroy()
                    return
            elif event == "stopped":
                self._on_stopped(str(value))
                if self.closing:
                    self.window.destroy()
                    return
        self.window.after(120, self._drain_events)

    def _primary_action(self) -> None:
        if self.phase == "ready":
            self.open_console()
        else:
            self.start()

    def start(self) -> None:
        if self.busy or self.project is None or self.phase == "ready":
            return
        self.busy = True
        self.phase = "starting"
        self.cancel_requested.clear()
        self.monitor_stop.set()
        self.server_stop_requested = False
        self.last_error = ""
        self.error_frame.grid_remove()
        self.state_badge.configure(text="正在检查", bg="#E9EEF7", fg=PRIMARY)
        self.start_button.configure(text="正在启动…", state=tk.DISABLED)
        self.stop_button.configure(
            text="取消启动", style="Secondary.TButton", state=tk.NORMAL
        )
        self.device_button.configure(state=tk.DISABLED)
        self.progress.pack(fill=tk.X)
        self.progress.start(12)
        self._set_card("runtime", "checking", "正在检查", "验证 Python 与必要组件")
        self._set_card("web", "idle", "等待启动", "固定地址 · 127.0.0.1:8765")
        self._set_card("device", "idle", "等待检测", "将在服务就绪后自动检查")
        self._set_card("app", "idle", "等待检测", "货车帮司机")
        threading.Thread(target=self._prepare_and_start, daemon=True).start()

    def _run_command(self, command: list[str], description: str) -> None:
        self.events.put(("log", f"开始：{description}"))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                command,
                cwd=self.project,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        except OSError as error:
            raise RuntimeError(f"无法运行准备程序：{error}") from error
        self.command_process = process
        if self.cancel_requested.is_set():
            process.terminate()
        assert process.stdout is not None
        try:
            with process.stdout:
                for line in process.stdout:
                    self.events.put(("log", f"组件输出：{line.rstrip()}"))
            code = process.wait()
        finally:
            self.command_process = None
        if self.cancel_requested.is_set():
            raise RuntimeError("启动流程已取消。")
        if code:
            raise RuntimeError(
                f"{description}失败（退出代码 {code}）。请查看诊断日志。"
            )

    def _adb_path(self) -> str | Path:
        assert self.project is not None
        bundled = self.project / "src" / "x001" / "platform-tools" / "adb.exe"
        return bundled if bundled.is_file() else DEFAULT_ADB_PATH

    def _prepare_and_start(self) -> None:
        assert self.project is not None
        try:
            uv = bundled_uv()
            if uv is None:
                raise RuntimeError("未找到运行组件管理器 uv，请使用随附的 X001.exe。")
            self.events.put(("progress", (1, "正在验证运行环境")))
            self._run_command(
                [str(uv), "sync", "--frozen", "--python", "3.14", "--no-progress"],
                "运行环境准备",
            )
            python = self.project / ".venv" / "Scripts" / "python.exe"
            if not python.is_file():
                raise RuntimeError("Python 环境未创建成功，请检查诊断日志后重试。")
            self._set_worker_card(
                "runtime", "ok", "运行环境就绪", "Python 与组件已验证"
            )

            self.events.put(("progress", (2, "正在检查本地数据")))
            self._run_command([str(python), "-m", "x001.initialize"], "本地数据初始化")
            self.events.put(("progress", (3, "正在启动并验证网页服务")))
            self._set_worker_card(
                "web", "checking", "正在启动", "检查服务与数据库健康状态"
            )
            existing = read_health(CONSOLE_URL)
            if existing and existing.get("service") == "x001":
                if not existing.get("database_ready"):
                    raise RuntimeError("已有 X001 服务的数据库不可用，请查看诊断日志。")
                self.server_url = CONSOLE_URL
                self.server_owned = False
                self.events.put(
                    ("log", "发现已运行的 X001 服务，直接接管，不重复启动。")
                )
            else:
                if not port_available(8765):
                    raise RuntimeError(
                        "固定端口 8765 已被占用，可能是旧版 X001 服务或其他程序。"
                        "请关闭占用程序后点击“重试启动”。"
                    )
                self.server_url = CONSOLE_URL
                self.server_owned = True
                flags = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                )
                self.server = subprocess.Popen(
                    [
                        str(python),
                        "-m",
                        "x001.web",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8765",
                    ],
                    cwd=self.project,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=flags,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                )
                threading.Thread(target=self._read_server_output, daemon=True).start()
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if self.cancel_requested.is_set():
                        raise RuntimeError("启动流程已取消。")
                    if self.server.poll() is not None:
                        raise RuntimeError("网页服务意外退出，请查看诊断日志。")
                    if healthy_console(CONSOLE_URL):
                        break
                    self.cancel_requested.wait(0.35)
                else:
                    raise RuntimeError("等待网页服务超时，请查看诊断日志后重试。")
            self._set_worker_card("web", "ok", "网页服务正常", "服务与数据库均已验证")

            self.events.put(("progress", (4, "正在检测手机与目标应用")))
            self._set_worker_card(
                "device", "checking", "正在检测", "检查连接、授权和锁屏状态"
            )
            self._set_worker_card(
                "app", "checking", "正在检测", "检查并尝试启动货车帮司机"
            )
            report = self._detect_device(allow_actions=True)
            self.events.put(("device", report))
            if self.cancel_requested.is_set():
                raise RuntimeError("启动流程已取消。")
            self.events.put(("ready", self.server_url))
            self.events.put(("log", "启动检查完成，正在打开货源操作台。"))
        except (OSError, RuntimeError) as error:
            self.events.put(("log", f"启动未完成：{error}"))
            try:
                self._shutdown_server()
            finally:
                if self.closing or self.cancel_requested.is_set():
                    self.events.put(("stopped", "启动已取消，可以重新开始。"))
                else:
                    self.events.put(("failed", str(error)))

    def _set_worker_card(self, key: str, level: str, label: str, detail: str) -> None:
        self.events.put(("card", (key, level, label, detail)))

    def _detect_device(self, allow_actions: bool) -> dict:
        adb_path = self._adb_path()
        try:
            report = inspect_device(adb_path=adb_path)
            controller = ADBController(adb_path=adb_path, timeout=6)
            if (
                allow_actions
                and report.get("connected")
                and report.get("screen_awake") is False
            ):
                controller.wake()
                self.events.put(("log", "已发送屏幕唤醒指令，不会绕过锁屏密码。"))
                if self.cancel_requested.wait(0.8):
                    return report
                report = inspect_device(adb_path=adb_path)
            if (
                allow_actions
                and report.get("connected")
                and report.get("unlocked") is not False
                and report.get("screen_awake") is not False
                and report.get("package_installed")
                and not report.get("foreground")
            ):
                controller.launch()
                report["launch_attempted"] = True
                self.events.put(("log", "已启动货车帮司机，等待应用进入前台。"))
                if self.cancel_requested.wait(1.5):
                    return report
                refreshed = inspect_device(adb_path=adb_path)
                refreshed["launch_attempted"] = True
                report = refreshed
            return report
        except (
            OSError,
            RuntimeError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            return {
                "connected": False,
                "target_state": "unknown",
                "devices": [],
                "foreground": False,
                "error": str(error),
            }

    def _apply_device(self, report: dict) -> None:
        self.device_checking = False
        if self.phase == "ready":
            self.device_button.configure(state=tk.NORMAL)
        state = report.get("target_state", "missing")
        if report.get("error") and state in {"unknown", "missing"}:
            self._set_card("device", "error", "ADB 检测失败", "查看诊断日志了解原因")
            self._set_card("app", "idle", "等待手机", "完成手机检测后重试")
            self.guide_label.configure(
                text="无法检测手机，请点击“重新检测手机”；详细原因见诊断日志。"
            )
        elif state == "unauthorized":
            self._set_card(
                "device", "warning", "等待 USB 授权", "请在手机上允许这台电脑调试"
            )
            self._set_card("app", "idle", "等待授权", "授权后自动重新检测")
            self.guide_label.configure(
                text="请在手机上允许 USB 调试授权，然后点击“重新检测手机”。"
            )
        elif state == "offline":
            self._set_card(
                "device", "warning", "手机连接离线", "重新插拔 USB 后再次检测"
            )
            self._set_card("app", "idle", "等待手机", "设备恢复在线后可继续")
            self.guide_label.configure(
                text="手机处于离线状态，请重新插拔 USB 线并点击“重新检测手机”。"
            )
        elif not report.get("connected"):
            self._set_card(
                "device", "warning", "未检测到目标手机", "连接手机并开启 USB 调试"
            )
            self._set_card("app", "idle", "等待手机", "货车帮司机")
            serial = report.get("target_serial", "7450df55")
            self.guide_label.configure(
                text=f"请连接采集用手机（目标序列号 {serial}），再重新检测。"
            )
        elif report.get("unlocked") is False or report.get("screen_awake") is False:
            self._set_card(
                "device", "warning", "手机已连接 · 请解锁", "USB 连接与授权正常"
            )
            self._set_card("app", "idle", "等待解锁", "解锁后可自动打开应用")
            self.guide_label.configure(
                text="请在手机上完成解锁，然后点击“重新检测手机”。"
            )
        else:
            self._set_card(
                "device",
                "ok",
                "手机连接正常",
                f"{report.get('target_serial', '设备')} · USB 已授权",
            )
            if report.get("package_installed") is False:
                self._set_card(
                    "app", "error", "未安装货车帮司机", "请安装目标应用后重新检测"
                )
                self.guide_label.configure(
                    text="手机未安装货车帮司机，请安装后点击“重新检测手机”。"
                )
            elif report.get("foreground"):
                self._set_card(
                    "app", "ok", "货车帮司机运行中", "应用已在前台，可选择采集线路"
                )
                self.guide_label.configure(
                    text="手机与应用已就绪。请在货车帮司机中选择线路，再到网页操作台开始采集。"
                )
            else:
                detail = (
                    "已发送启动指令，请确认应用前台"
                    if report.get("launch_attempted")
                    else "请在手机上打开货车帮司机"
                )
                self._set_card("app", "warning", "应用尚未进入前台", detail)
                self.guide_label.configure(
                    text="请确认货车帮司机已打开并选择线路，再开始采集。"
                )
        if self.phase == "ready":
            self._refresh_ready_summary()

    def _refresh_ready_summary(self) -> None:
        phone_ready = self.card_states.get("device") == "ok"
        app_ready = self.card_states.get("app") == "ok"
        if phone_ready and app_ready:
            self.state_badge.configure(text="全部就绪", bg="#EAF8F2", fg=GREEN)
            self.status_label.configure(text="操作台运行中，手机与应用已就绪", fg=GREEN)
            self.step_label.configure(text="选择线路后即可在网页操作台开始采集")
        else:
            self.state_badge.configure(text="手机待处理", bg="#FFF4E8", fg=AMBER)
            self.status_label.configure(text="操作台已启动，手机还需处理", fg=AMBER)
            self.step_label.configure(text="可查看已有数据；完成手机提示后即可开始采集")

    def _on_ready(self, url: str) -> None:
        self.busy = False
        self.phase = "ready"
        self.server_url = url
        self.progress.stop()
        self.progress.pack_forget()
        self.start_button.configure(text="打开操作台", state=tk.NORMAL)
        self.stop_button.configure(
            text="停止服务", style="Danger.TButton", state=tk.NORMAL
        )
        self.device_button.configure(state=tk.NORMAL)
        self._refresh_ready_summary()
        self.open_console()
        self.monitor_stop = threading.Event()
        threading.Thread(
            target=self._monitor, args=(url, self.monitor_stop), daemon=True
        ).start()

    def _on_failed(self, message: str) -> None:
        if self.phase == "failed" and self.last_error == message:
            return
        self.busy = False
        self.phase = "failed"
        self.progress.stop()
        self.progress.pack_forget()
        self.server = None
        self.server_url = ""
        self.last_error = message
        self.state_badge.configure(text="启动未完成", bg="#FFF3F0", fg=RED)
        self.status_label.configure(text="启动未完成，请处理后重试", fg=RED)
        self.step_label.configure(text="核心问题已显示在下方，技术细节见诊断日志")
        self.error_label.configure(text=message)
        self.error_frame.grid()
        self.start_button.configure(text="重试启动", state=tk.NORMAL)
        self.stop_button.configure(state=tk.DISABLED)
        self.device_button.configure(state=tk.DISABLED)
        self.monitor_stop.set()

    def _on_stopped(self, message: str) -> None:
        self.busy = False
        self.phase = "idle"
        self.server = None
        self.server_owned = False
        self.server_url = ""
        self.server_stop_requested = False
        self.progress.stop()
        self.progress.pack_forget()
        self.state_badge.configure(text="服务已停止", bg="#E9EEF7", fg=MUTED)
        self.status_label.configure(text=message or "本地服务已安全停止", fg=MUTED)
        self.step_label.configure(text="网页服务已关闭，采集任务已收到停止信号")
        self.start_button.configure(text="重新一键启动", state=tk.NORMAL)
        self.stop_button.configure(state=tk.DISABLED)
        self.device_button.configure(state=tk.DISABLED)
        self._set_card("web", "idle", "已停止", "固定地址 · 127.0.0.1:8765")
        self._set_card("app", "idle", "未检查", "重新启动后自动检测")
        self.monitor_stop.set()

    def _read_server_output(self) -> None:
        process = self.server
        if process is None or process.stdout is None:
            return
        with process.stdout:
            for line in process.stdout:
                self.events.put(("log", f"网页服务：{line.rstrip()}"))
        code = process.wait()
        if self.server is not process:
            return
        if self.server_stop_requested or self.closing:
            self.events.put(("log", f"网页服务已停止（退出代码 {code}）。"))
        else:
            self.events.put(("log", f"网页服务意外结束，退出代码 {code}。"))
            self.events.put(("failed", "网页服务意外停止，请点击“重试启动”。"))

    def _monitor(self, url: str, stop_event: threading.Event) -> None:
        while not stop_event.wait(4):
            if self.closing or self.server_stop_requested:
                return
            if not healthy_console(url):
                self._shutdown_server()
                self.events.put(
                    ("failed", "网页服务健康检查失败，已停止残留服务，请重试。")
                )
                return
            report = self._detect_device(allow_actions=False)
            self.events.put(("device", report))

    def refresh_device(self) -> None:
        if self.device_checking or self.phase != "ready":
            return
        self.device_checking = True
        self.device_button.configure(state=tk.DISABLED)
        self._set_card("device", "checking", "正在重新检测", "请稍候")
        threading.Thread(target=self._refresh_device_worker, daemon=True).start()

    def _refresh_device_worker(self) -> None:
        report = self._detect_device(allow_actions=True)
        self.events.put(("device", report))

    def open_console(self) -> None:
        if self.server_url:
            try:
                webbrowser.open(self.server_url)
            except OSError as error:
                self._log(f"浏览器无法自动打开：{error}")
                messagebox.showerror("无法打开浏览器", f"请手动访问：{self.server_url}")

    def open_data_folder(self) -> None:
        if self.project is None:
            messagebox.showerror("无法打开", "请将启动器放回 X001 项目文件夹。")
            return
        data = self.project / "data"
        data.mkdir(exist_ok=True)
        os.startfile(data)

    def stop(self) -> None:
        if self.phase == "starting":
            if not messagebox.askyesno("取消启动", "确定取消当前启动流程吗？"):
                return
            self.cancel_requested.set()
            self.status_label.configure(text="正在取消启动…", fg=MUTED)
            self.stop_button.configure(state=tk.DISABLED)
            if self.command_process is not None and self.command_process.poll() is None:
                self.command_process.terminate()
            return
        if self.phase != "ready" or self.busy:
            return
        if not messagebox.askyesno(
            "停止本地服务",
            "停止后网页操作台将无法使用，采集任务会安全停止。\n\n确定停止吗？",
        ):
            return
        self.busy = True
        self.phase = "stopping"
        self.server_stop_requested = True
        self.monitor_stop.set()
        self.stop_button.configure(state=tk.DISABLED)
        self.start_button.configure(state=tk.DISABLED)
        self.device_button.configure(state=tk.DISABLED)
        self.status_label.configure(text="正在停止服务并回收采集任务…", fg=PRIMARY)
        self.step_label.configure(text="请稍候，不要直接结束进程")
        threading.Thread(target=self._stop_server, daemon=True).start()

    def _shutdown_server(self) -> None:
        process = self.server
        if self.server_url:
            request = urllib.request.Request(
                f"{self.server_url}/api/shutdown",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    response.read()
            except urllib.error.HTTPError, OSError, urllib.error.URLError:
                pass
        if process is not None and self.server_owned and process.poll() is None:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        elif process is None and self.server_url:
            deadline = time.monotonic() + 6
            while (
                time.monotonic() < deadline and read_health(self.server_url) is not None
            ):
                time.sleep(0.2)

    def _stop_server(self) -> None:
        try:
            self._shutdown_server()
        except (OSError, subprocess.TimeoutExpired) as error:
            self.events.put(("log", f"停止服务时出现异常：{error}"))
        finally:
            self.events.put(("stopped", "本地服务与采集任务已安全停止。"))

    def close(self) -> None:
        if self.phase == "starting":
            if not messagebox.askyesno(
                "退出 X001",
                "启动尚未完成。关闭窗口会取消启动并清理已启动服务，确定退出吗？",
            ):
                return
            self.closing = True
            self.cancel_requested.set()
            if self.command_process is not None and self.command_process.poll() is None:
                self.command_process.terminate()
            return
        if self.phase in {"ready", "stopping"}:
            if self.phase == "stopping":
                self.closing = True
                return
            if not messagebox.askyesno(
                "退出 X001",
                "退出会关闭网页操作台并安全停止采集任务。确定退出吗？",
            ):
                return
            self.closing = True
            self.busy = True
            self.phase = "stopping"
            self.server_stop_requested = True
            self.monitor_stop.set()
            threading.Thread(target=self._stop_server, daemon=True).start()
            return
        self.window.destroy()


def main() -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except AttributeError, OSError:
            pass
    window = tk.Tk()
    Launcher(window)
    window.mainloop()


if __name__ == "__main__":
    main()
