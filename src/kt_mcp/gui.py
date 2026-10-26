"""Small Chinese desktop launcher: python -m kt_mcp.gui."""

import json
import logging
import os
import queue
import secrets
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from kt_mcp.desktop import (
    DesktopService,
    DesktopSettings,
    client_configuration,
    load_settings,
    save_settings,
)

MODES = {
    "临时公网地址（免账号）": "quick",
    "固定域名（已有 Tunnel Token）": "named",
    "仅本机 HTTP": "local",
}


class DesktopWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.service = DesktopService()
        self.closing = False
        self.inputs = []
        saved = load_settings()
        self.workspace = tk.StringVar(value=saved.workspace or str(Path.cwd()))
        self.port = tk.StringVar(value=str(saved.port))
        self.mode = tk.StringVar(
            value=next(
                (label for label, key in MODES.items() if key == saved.mode),
                next(iter(MODES)),
            )
        )
        self.origin = tk.StringVar(value=saved.public_origin)
        self.binary = tk.StringVar(value=saved.cloudflared)
        self.token = tk.StringVar(
            value=os.environ.get("KT_MCP_TOKEN") or secrets.token_urlsafe(32)
        )
        self.cf_token = tk.StringVar(value=os.environ.get("TUNNEL_TOKEN", ""))
        self.status = tk.StringVar(value="已停止")
        self.url = tk.StringVar()
        root.title("Kohaku KT MCP · 桌面控制台")
        root.geometry("850x790")
        root.minsize(740, 710)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Hint.TLabel", foreground="#5b6473")
        outer = ttk.Frame(root, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Kohaku KT MCP", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="工作区工具服务 · Cloudflare Tunnel 自动接入",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(2, 14))

        form = ttk.LabelFrame(outer, text="服务设置", padding=12)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self._field(form, 0, "工作目录", self.workspace, self._choose_workspace)
        self._field(form, 1, "HTTP 端口", self.port)
        ttk.Label(form, text="接入方式").grid(
            row=2, column=0, sticky="w", padx=(0, 12), pady=5
        )
        self.mode_box = ttk.Combobox(
            form, textvariable=self.mode, values=list(MODES), state="readonly"
        )
        self.mode_box.grid(row=2, column=1, columnspan=2, sticky="ew", pady=5)
        self.mode_box.bind("<<ComboboxSelected>>", lambda _: self._mode_changed())
        self.named_frame = ttk.Frame(form)
        self.named_frame.grid(row=3, column=0, columnspan=3, sticky="ew")
        self.named_frame.columnconfigure(1, weight=1)
        self._field(self.named_frame, 0, "公网域名", self.origin)
        self._field(self.named_frame, 1, "Tunnel Token", self.cf_token, secret=True)
        ttk.Label(
            self.named_frame,
            text="Cloudflare 控制台先将此域名路由到 http://127.0.0.1:<上方端口>。\n域名填写 https://mcp.example.com，不含 /mcp；此处 Token 为连接器 Token。",
            style="Hint.TLabel",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=3)
        self._field(
            form,
            4,
            "MCP 访问令牌",
            self.token,
            self._new_token,
            secret=True,
            button_text="重新生成",
        )
        self._field(form, 5, "cloudflared", self.binary, self._choose_binary)
        ttk.Label(
            form,
            text="cloudflared 路径可留空：自动检测，缺少时从官方发布下载并校验 SHA256。\n设置自动保存；两种 Token 均不写入设置文件。",
            style="Hint.TLabel",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(5, 0))

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=14)
        self.start_button = ttk.Button(controls, text="启动服务", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            controls, text="停止", command=self._stop, state="disabled"
        )
        self.stop_button.pack(side="left", padx=8)
        ttk.Label(controls, textvariable=self.status).pack(side="right")

        connection = ttk.LabelFrame(outer, text="客户端连接", padding=12)
        connection.pack(fill="x")
        ttk.Entry(connection, textvariable=self.url, state="readonly").pack(fill="x")
        buttons = ttk.Frame(connection)
        buttons.pack(fill="x", pady=(8, 0))
        self.copy_url_button = ttk.Button(
            buttons, text="复制地址", command=lambda: self._copy(self.url.get())
        )
        self.copy_url_button.pack(side="left")
        self.copy_config_button = ttk.Button(
            buttons, text="复制客户端 JSON（含令牌）", command=self._copy_config
        )
        self.copy_config_button.pack(side="left", padx=8)
        ttk.Label(
            connection,
            text="客户端支持 Streamable HTTP 即可，复制完整地址（已包含访问令牌）。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(8, 0))
        ttk.Label(
            outer,
            text="访问令牌可操作工作区文件并执行命令，请仅交给可信客户端。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(10, 8))
        self.logs = ScrolledText(
            outer, height=9, wrap="word", state="disabled", font=("Consolas", 9)
        )
        self.logs.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="Derived from KohakuTerrarium · Kohaku-Lab · KohakuTerrarium License 1.0",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(8, 0))
        self._mode_changed()
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.after(100, self._poll)

    def _field(
        self,
        parent,
        row,
        title,
        variable,
        command=None,
        secret=False,
        button_text="浏览…",
    ):
        ttk.Label(parent, text=title).grid(
            row=row, column=0, sticky="w", padx=(0, 12), pady=5
        )
        entry = ttk.Entry(parent, textvariable=variable, show="•" if secret else "")
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        self.inputs.append(entry)
        if command:
            button = ttk.Button(parent, text=button_text, command=command)
            button.grid(row=row, column=2, padx=(8, 0), pady=5)
            self.inputs.append(button)
        else:
            entry.grid(columnspan=2)

    def _mode_changed(self):
        if MODES[self.mode.get()] == "named":
            self.named_frame.grid()
        else:
            self.named_frame.grid_remove()

    def _choose_workspace(self):
        path = filedialog.askdirectory(
            parent=self.root,
            initialdir=(
                self.workspace.get()
                if Path(self.workspace.get()).is_dir()
                else str(Path.home())
            ),
        )
        if path:
            self.workspace.set(path)

    def _choose_binary(self):
        path = filedialog.askopenfilename(
            parent=self.root, title="选择 cloudflared 可执行文件"
        )
        if path:
            self.binary.set(path)

    def _new_token(self):
        self.token.set(secrets.token_urlsafe(32))

    def _start(self):
        try:
            settings = DesktopSettings(
                workspace=self.workspace.get().strip(),
                port=int(self.port.get()),
                mode=MODES[self.mode.get()],
                public_origin=self.origin.get().strip(),
                cloudflared=self.binary.get().strip(),
            )
            settings.validate()
            self.service.start(settings, self.token.get(), self.cf_token.get().strip())
            self.status.set("正在启动…")
            self.url.set("")
            try:
                save_settings(settings)
            except OSError as exc:
                self._log(f"无法保存设置：{exc}")
        except (ValueError, OSError) as exc:
            messagebox.showerror("无法启动", str(exc), parent=self.root)

    def _stop(self):
        self.status.set("正在停止…")
        self.service.stop()

    def _copy(self, value):
        if value:
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            self._log("已复制到剪贴板")

    def _copy_config(self):
        if self.url.get():
            self._copy(client_configuration(self.url.get()))

    def _log(self, text):
        self.logs.configure(state="normal")
        self.logs.insert("end", text + "\n")
        if int(self.logs.index("end-1c").split(".")[0]) > 1200:
            self.logs.delete("1.0", "201.0")
        self.logs.see("end")
        self.logs.configure(state="disabled")

    def _poll(self):
        for _ in range(200):
            try:
                kind, value = self.service.events.get_nowait()
            except queue.Empty:
                break
            if kind == "state":
                self.status.set(value)
            elif kind == "url":
                self.url.set(value)
            elif kind == "error":
                self._log("错误：" + value)
                if not self.closing:
                    messagebox.showerror("服务错误", value, parent=self.root)
            else:
                self._log(value)
        running = self.service.running
        for widget in self.inputs:
            widget.configure(state="disabled" if running else "normal")
        self.mode_box.configure(state="disabled" if running else "readonly")
        self.start_button.configure(
            state="disabled" if running or self.closing else "normal"
        )
        self.stop_button.configure(
            state=(
                "normal"
                if running and not self.service.cancelled.is_set()
                else "disabled"
            )
        )
        for button in (self.copy_url_button, self.copy_config_button):
            button.configure(
                state=(
                    "normal"
                    if self.url.get() and not self.service.cancelled.is_set()
                    else "disabled"
                )
            )
        if self.closing and not running:
            self.root.destroy()
            return
        self.root.after(100, self._poll)

    def _close(self):
        self.closing = True
        self.status.set("正在关闭服务和 Tunnel…")
        self.service.stop()


class DesktopLogHandler(logging.Handler):
    def __init__(self, service):
        super().__init__(logging.WARNING)
        self.service = service

    def emit(self, record):
        self.service.emit("log", self.format(record))


def main(*, verification_directory: Path | None = None):
    root = tk.Tk()
    window = DesktopWindow(root)
    handler = DesktopLogHandler(window.service)
    logging.getLogger().addHandler(handler)
    if verification_directory is not None:
        if not verification_directory.is_dir():
            raise ValueError("Verification directory must exist")

        def verification_tick():
            if (verification_directory / "stop").is_file():
                window._close()
                return
            snapshot = {
                "url": window.url.get(),
                "status": window.status.get(),
                "geometry": root.winfo_geometry(),
                "viewable": bool(root.winfo_viewable()),
                "logs": window.logs.get("1.0", "end")[-12000:],
            }
            try:
                (verification_directory / "gui-state.json").write_text(
                    json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
                )
            except OSError:
                pass  # A test reader may briefly hold the file on Windows.
            root.after(200, verification_tick)

        root.after(100, window._start)
        root.after(200, verification_tick)
    try:
        root.mainloop()
    finally:
        window.service.stop()
        if window.service.worker:
            window.service.worker.join(timeout=20)
        logging.getLogger().removeHandler(handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
