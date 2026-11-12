"""Small Windows GUI for preparing and starting the X001 web console."""

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
from pathlib import Path
from tkinter import messagebox, scrolledtext

TITLE = "X001 货源数据中心"
URL_ROOT = "http://127.0.0.1"
BACKGROUND = "#f3f6fa"
INK = "#172b4d"
MUTED = "#65758b"
BLUE = "#2864dc"
GREEN = "#13825b"
RED = "#b54747"


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


def available_port(preferred: int = 8765) -> int:
    """Use the default port when free, otherwise request an ephemeral one."""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])


class Launcher:
    def __init__(self, window: tk.Tk):
        self.window = window
        self.window.title(TITLE)
        self.window.geometry("960x680")
        self.window.minsize(860, 620)
        self.window.configure(bg=BACKGROUND)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.project: Path | None = None
        self.server: subprocess.Popen[str] | None = None
        self.server_url = ""
        self.busy = False
        self.closing = False
        self.server_stop_requested = False
        self.cancel_requested = threading.Event()
        self.command_process: subprocess.Popen[str] | None = None

        self._build()
        try:
            self.project = project_root()
            self.path_label.configure(text=str(self.project / "data"))
            self._log("已找到项目文件夹。")
        except FileNotFoundError as error:
            self.status_label.configure(text="需要放回项目文件夹", fg=RED)
            self._log(str(error))
            self.start_button.configure(state=tk.DISABLED)
        self.window.after(120, self._drain_events)

    def _build(self) -> None:
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(1, weight=1)

        header = tk.Frame(self.window, bg="#152d50", padx=34, pady=20)
        header.grid(row=0, column=0, sticky="ew")
        brand = tk.Frame(header, bg="#152d50")
        brand.pack(side=tk.LEFT)
        tk.Label(
            brand,
            text="X001",
            bg="#152d50",
            fg="white",
            font=("Microsoft YaHei UI", 22, "bold"),
        ).pack(anchor="w")
        tk.Label(
            brand,
            text="货源数据中心  ·  本地运行",
            bg="#152d50",
            fg="#c5d5eb",
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(1, 0))
        tk.Label(
            header,
            text="数据只保存在这台电脑",
            bg="#152d50",
            fg="#d8e6fa",
            font=("Microsoft YaHei UI", 10),
        ).pack(side=tk.RIGHT, anchor="center")

        content = tk.Frame(self.window, bg=BACKGROUND, padx=30, pady=23)
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=11, uniform="columns")
        content.columnconfigure(1, weight=9, uniform="columns")
        content.rowconfigure(2, weight=1)

        tk.Label(
            content,
            text="欢迎使用 X001",
            bg=BACKGROUND,
            fg=INK,
            font=("Microsoft YaHei UI", 21, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(
            content,
            text="准备好手机后，点击下方按钮即可启动操作台。",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Microsoft YaHei UI", 10),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 17))

        left = tk.Frame(
            content,
            bg="white",
            padx=22,
            pady=19,
            highlightthickness=1,
            highlightbackground="#e2e8f0",
        )
        left.grid(row=2, column=0, sticky="nsew", padx=(0, 10))
        left.columnconfigure(0, weight=1)
        tk.Label(
            left,
            text="开始之前",
            bg="white",
            fg=INK,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).grid(row=0, column=0, sticky="w")
        instructions = (
            ("01", "连接并解锁 Android 手机"),
            ("02", "打开货车帮司机，选择要采集的线路"),
            ("03", "保持手机解锁，点击“启动操作台”"),
        )
        for index, (number, text) in enumerate(instructions, start=1):
            step = tk.Frame(left, bg="white")
            step.grid(row=index, column=0, sticky="ew", pady=(13, 0))
            tk.Label(
                step,
                text=number,
                bg="#eaf0ff",
                fg=BLUE,
                width=4,
                pady=6,
                font=("Microsoft YaHei UI", 9, "bold"),
            ).pack(side=tk.LEFT, padx=(0, 10))
            tk.Label(
                step,
                text=text,
                bg="white",
                fg=INK,
                font=("Microsoft YaHei UI", 9),
                anchor="w",
            ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.status_label = tk.Label(
            left,
            text="尚未启动",
            bg="white",
            fg=MUTED,
            font=("Microsoft YaHei UI", 10, "bold"),
            anchor="w",
        )
        self.status_label.grid(row=4, column=0, sticky="ew", pady=(21, 10))
        buttons = tk.Frame(left, bg="white")
        buttons.grid(row=5, column=0, sticky="ew")
        buttons.columnconfigure((0, 1), weight=1)
        self.start_button = tk.Button(
            buttons,
            text="启动操作台",
            command=self.start,
            bg=BLUE,
            fg="white",
            activebackground="#1e50b2",
            activeforeground="white",
            relief=tk.FLAT,
            cursor="hand2",
            padx=10,
            pady=11,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.open_button = tk.Button(
            buttons,
            text="打开操作台",
            command=self.open_console,
            state=tk.DISABLED,
            bg="#eaf0ff",
            fg=BLUE,
            disabledforeground="#a0aec0",
            relief=tk.FLAT,
            cursor="hand2",
            padx=8,
            pady=11,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.open_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        self.stop_button = tk.Button(
            left,
            text="停止本地服务",
            command=self.stop,
            state=tk.DISABLED,
            bg="white",
            fg=RED,
            disabledforeground="#a0aec0",
            relief=tk.FLAT,
            cursor="hand2",
            pady=8,
            font=("Microsoft YaHei UI", 9),
        )
        self.stop_button.grid(row=6, column=0, sticky="w", pady=(6, 0))

        right = tk.Frame(
            content,
            bg="white",
            padx=19,
            pady=19,
            highlightthickness=1,
            highlightbackground="#e2e8f0",
        )
        right.grid(row=2, column=1, sticky="nsew", padx=(10, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)
        tk.Label(
            right,
            text="运行状态",
            bg="white",
            fg=INK,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            right,
            text="首次启动会自动安装所需组件，可能需要一些时间和网络连接。",
            bg="white",
            fg=MUTED,
            justify=tk.LEFT,
            wraplength=325,
            font=("Microsoft YaHei UI", 9),
        ).grid(row=1, column=0, sticky="w", pady=(7, 11))
        self.log_box = scrolledtext.ScrolledText(
            right,
            height=12,
            bg="#f7f9fc",
            fg="#394b63",
            relief=tk.FLAT,
            borderwidth=0,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Consolas", 9),
            padx=11,
            pady=10,
        )
        self.log_box.grid(row=3, column=0, sticky="nsew")

        footer = tk.Frame(content, bg=BACKGROUND)
        footer.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(15, 0))
        footer.columnconfigure(0, weight=1)
        tk.Label(
            footer,
            text="数据目录",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self.path_label = tk.Label(
            footer,
            text="等待定位项目文件夹",
            bg=BACKGROUND,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        )
        self.path_label.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        tk.Button(
            footer,
            text="打开数据文件夹",
            command=self.open_data_folder,
            bg=BACKGROUND,
            fg=BLUE,
            relief=tk.FLAT,
            cursor="hand2",
            font=("Microsoft YaHei UI", 9),
        ).grid(row=0, column=1, rowspan=2, padx=(12, 0))

    def _log(self, line: str) -> None:
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.insert(tk.END, line.rstrip() + "\n")
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > 180:
            self.log_box.delete("1.0", f"{lines - 180}.0")
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)

    def _drain_events(self) -> None:
        while True:
            try:
                event, value = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "log":
                self._log(value)
            elif event == "status":
                self.status_label.configure(text=value, fg=BLUE if self.busy else GREEN)
            elif event == "ready":
                if self.closing:
                    threading.Thread(target=self._stop_server, daemon=True).start()
                    continue
                self.busy = False
                self.server_url = value
                self.status_label.configure(text="操作台已就绪", fg=GREEN)
                self.start_button.configure(state=tk.DISABLED)
                self.open_button.configure(state=tk.NORMAL)
                self.stop_button.configure(state=tk.NORMAL)
                self.open_console()
            elif event == "failed":
                self.busy = False
                self.status_label.configure(text="启动失败，请查看运行状态", fg=RED)
                self.start_button.configure(state=tk.NORMAL)
                self.stop_button.configure(state=tk.DISABLED)
                self.open_button.configure(state=tk.DISABLED)
                if self.server is not None and self.server.poll() is not None:
                    self.server = None
                    self.server_url = ""
                if self.closing:
                    self.window.destroy()
                    return
            elif event == "stopped":
                self.busy = False
                self.server = None
                self.server_url = ""
                self.server_stop_requested = False
                self.status_label.configure(text="本地服务已停止", fg=MUTED)
                self.start_button.configure(state=tk.NORMAL)
                self.open_button.configure(state=tk.DISABLED)
                self.stop_button.configure(state=tk.DISABLED)
                if self.closing:
                    self.window.destroy()
                    return
        self.window.after(120, self._drain_events)

    def start(self) -> None:
        if self.busy or self.project is None:
            return
        self.busy = True
        self.cancel_requested.clear()
        self.start_button.configure(state=tk.DISABLED)
        self.open_button.configure(state=tk.DISABLED)
        self.status_label.configure(text="正在准备运行环境…", fg=BLUE)
        threading.Thread(target=self._prepare_and_start, daemon=True).start()

    def _run_command(self, command: list[str], description: str) -> None:
        self.events.put(("status", description))
        self.events.put(("log", f"> {description}"))
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
            for line in process.stdout:
                self.events.put(("log", line.rstrip()))
            code = process.wait()
        finally:
            self.command_process = None
        if self.cancel_requested.is_set():
            raise RuntimeError("启动流程已取消。")
        if code:
            raise RuntimeError(f"{description}失败，退出代码 {code}。请查看运行日志。")

    def _prepare_and_start(self) -> None:
        assert self.project is not None
        try:
            uv = bundled_uv()
            if uv is None:
                raise RuntimeError(
                    "未找到 uv。请使用随附的启动器，或先安装 uv 后重新启动。"
                )
            self._run_command(
                [str(uv), "sync", "--frozen", "--python", "3.14", "--no-progress"],
                "正在安装运行环境（首次启动可能较久）…",
            )
            python = self.project / ".venv" / "Scripts" / "python.exe"
            if not python.is_file():
                raise RuntimeError("Python 环境未创建成功，请检查运行日志后重试。")
            self._run_command(
                [str(python), "-m", "x001.initialize"],
                "正在初始化本地数据…",
            )
            port = available_port()
            self.server_url = f"{URL_ROOT}:{port}"
            self.events.put(("status", "正在启动本地操作台…"))
            self.events.put(("log", f"正在启动网页服务：{self.server_url}"))
            self.server_stop_requested = False
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            self.server = subprocess.Popen(
                [
                    str(python),
                    "-m",
                    "x001.web",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
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
                if self.server.poll() is not None:
                    raise RuntimeError("网页服务意外退出，请查看运行日志。")
                try:
                    with urllib.request.urlopen(self.server_url, timeout=1) as response:
                        if response.status == 200:
                            response.read()
                            break
                except OSError, urllib.error.URLError:
                    time.sleep(0.3)
            else:
                raise RuntimeError("等待网页服务超时，请查看运行日志后重试。")
            self.events.put(("ready", self.server_url))
            self.events.put(("log", "操作台已就绪，正在打开浏览器。"))
        except (OSError, RuntimeError) as error:
            self.events.put(("log", f"启动失败：{error}"))
            if self.server is not None and self.server.poll() is None:
                self.server.terminate()
                self.server.wait(timeout=5)
                self.server = None
            self.events.put(("stopped", "") if self.closing else ("failed", str(error)))

    def _read_server_output(self) -> None:
        process = self.server
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self.events.put(("log", line.rstrip()))
        code = process.wait()
        if self.server_stop_requested or self.closing:
            message = (
                "网页服务已正常停止。"
                if code == 0
                else f"网页服务已停止（退出代码 {code}）。"
            )
            self.events.put(("log", message))
        else:
            self.events.put(("log", f"网页服务已结束，退出代码 {code}。"))
            self.events.put(("failed", "网页服务已停止"))

    def open_console(self) -> None:
        if self.server_url:
            try:
                import webbrowser

                webbrowser.open(self.server_url)
            except OSError as error:
                self._log(f"浏览器无法自动打开：{error}")

    def open_data_folder(self) -> None:
        if self.project is None:
            messagebox.showerror("无法打开", "请将启动器放回 X001 项目文件夹。")
            return
        data = self.project / "data"
        data.mkdir(exist_ok=True)
        os.startfile(data)

    def stop(self) -> None:
        if self.server is None or self.server.poll() is not None or self.busy:
            return
        if not messagebox.askyesno(
            "停止本地服务",
            "停止后网页操作台将无法使用。正在运行的采集任务也会收到中断信号。\n\n确定停止吗？",
        ):
            return
        self.busy = True
        self.server_stop_requested = True
        self.stop_button.configure(state=tk.DISABLED)
        self.status_label.configure(text="正在停止服务…", fg=BLUE)
        threading.Thread(target=self._stop_server, daemon=True).start()

    def _stop_server(self) -> None:
        process = self.server
        if process is not None and process.poll() is None:
            try:
                payload = b"{}"
                request = urllib.request.Request(
                    f"{self.server_url}/api/stop",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(request, timeout=2):
                        pass
                except urllib.error.HTTPError as error:
                    if error.code != 400:
                        self.events.put(("log", f"停止采集任务时收到错误：{error}"))
                except OSError, urllib.error.URLError:
                    pass
                request = urllib.request.Request(
                    f"{self.server_url}/api/shutdown",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(request, timeout=2) as response:
                        response.read()
                except (
                    urllib.error.HTTPError,
                    OSError,
                    urllib.error.URLError,
                ) as error:
                    if process.poll() is None:
                        self.events.put(("log", f"停止网页服务时收到错误：{error}"))
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            finally:
                self.events.put(("stopped", ""))

    def close(self) -> None:
        if self.busy:
            if not messagebox.askyesno(
                "正在准备",
                "准备任务尚未完成。关闭窗口会取消启动流程，确定退出吗？",
            ):
                return
            self.closing = True
            self.cancel_requested.set()
            if self.command_process is not None and self.command_process.poll() is None:
                self.command_process.terminate()
            return
        elif self.server is not None and self.server.poll() is None:
            if not messagebox.askyesno(
                "退出 X001",
                "退出会关闭网页操作台，并向正在运行的采集任务发送中断信号。确定退出吗？",
            ):
                return
            self.closing = True
            self.busy = True
            threading.Thread(target=self._stop_server, daemon=True).start()
            return
        self.window.destroy()


def main() -> None:
    window = tk.Tk()
    Launcher(window)
    window.mainloop()


if __name__ == "__main__":
    main()
