"""Native Windows desktop interface. Network and disk work stays off the UI thread."""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .desktop_config import load_settings, save_settings
from .desktop_engine import (
    Cancelled, Connection, DownloadOptions, download_batch, list_objects, validate_connection,
)


def format_size(value: int | float) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return "0 B"


class R2DownloaderApp:
    MAX_ROWS = 5000

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("R2 批量下载器")
        width = min(1220, root.winfo_screenwidth() - 80)
        height = min(850, root.winfo_screenheight() - 90)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(1050, width), min(760, height))
        self.root.configure(background="#F4F7FB")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.closing = False
        self.destroyed = False
        self.objects: list[dict[str, Any]] = []
        self.cached_context: tuple[Any, ...] | None = None
        self.row_by_key: dict[str, str] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.completed = 0
        self.batch_total = 0
        self.transferred = 0
        self.started_at = 0.0
        self.transfer_active = False
        self.fields: dict[str, tk.Variable] = {}
        self.inputs: list[ttk.Widget] = []
        self.action_buttons: list[ttk.Button] = []
        self.status = tk.StringVar(value="填写连接信息后，点击「连接并读取文件」。")
        self.list_info = tk.StringVar(value="尚未连接")
        self.progress_text = tk.StringVar(value="等待开始")
        self.progress_detail = tk.StringVar(value="选择保存位置后，可下载全部文件或列表中的选中项。")
        self._style()
        self._build()
        self._load()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(80, self._poll)

    def _style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=10)
        style.configure(".", font=("Microsoft YaHei UI", 10), background="#F4F7FB", foreground="#18253C")
        style.configure("TFrame", background="#F4F7FB")
        style.configure("Card.TFrame", background="#FFFFFF")
        style.configure("TLabel", background="#F4F7FB", foreground="#18253C")
        style.configure("Card.TLabel", background="#FFFFFF")
        style.configure("Muted.TLabel", foreground="#58677B", background="#FFFFFF", font=("Microsoft YaHei UI", 9))
        style.configure("CardTitle.TLabel", background="#FFFFFF", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Hero.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Subtitle.TLabel", foreground="#58677B", font=("Microsoft YaHei UI", 9))
        style.configure("TEntry", padding=(9, 6), fieldbackground="#FFFFFF", bordercolor="#CBD5E1", lightcolor="#CBD5E1", darkcolor="#CBD5E1")
        style.map("TEntry", bordercolor=[("focus", "#2563EB")])
        style.configure("TButton", padding=(12, 7), background="#EDF2F8", bordercolor="#DAE2EE", foreground="#243750")
        style.map("TButton", background=[("active", "#DEE8F6"), ("disabled", "#EEF1F5")], foreground=[("disabled", "#8995A5")])
        style.configure("Primary.TButton", background="#2563EB", foreground="#FFFFFF", bordercolor="#2563EB", padding=(18, 10), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Primary.TButton", background=[("disabled", "#CCD7EC"), ("active", "#1D4ED8")], foreground=[("disabled", "#6E7F9A"), ("!disabled", "#FFFFFF")])
        style.configure("TCheckbutton", background="#FFFFFF", padding=(0, 3))
        style.configure("TSpinbox", padding=(6, 5), fieldbackground="#FFFFFF")
        style.configure("TNotebook", background="#FFFFFF", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(15, 9), background="#EEF2F8", font=("Microsoft YaHei UI", 10))
        style.map("TNotebook.Tab", background=[("selected", "#FFFFFF")], foreground=[("selected", "#1D4ED8")])
        style.configure("Treeview", rowheight=31, background="#FFFFFF", fieldbackground="#FFFFFF", borderwidth=0, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", padding=(9, 9), background="#F1F5FA", foreground="#58677B", font=("Microsoft YaHei UI", 9))
        style.map("Treeview", background=[("selected", "#DBEAFE")], foreground=[("selected", "#173B7A")])
        style.configure("Horizontal.TProgressbar", background="#2563EB", troughcolor="#E7EDF7", borderwidth=0, thickness=8)

    def _var(self, name: str, default: Any = "", boolean: bool = False) -> tk.Variable:
        variable = tk.BooleanVar(value=default) if boolean else tk.StringVar(value=default)
        self.fields[name] = variable
        return variable

    def _entry(self, frame: ttk.Frame, label: str, name: str, *, secret: bool = False) -> ttk.Entry:
        ttk.Label(frame, text=label, style="Muted.TLabel").pack(anchor="w", pady=(9, 3))
        entry = ttk.Entry(frame, textvariable=self._var(name), show="•" if secret else "")
        entry.pack(fill="x")
        self.inputs.append(entry)
        return entry

    def _button(self, parent: ttk.Frame, text: str, command: Any, **kwargs: Any) -> ttk.Button:
        button = ttk.Button(parent, text=text, command=command, **kwargs)
        self.action_buttons.append(button)
        return button

    def _build(self) -> None:
        header = ttk.Frame(self.root, padding=(24, 18, 24, 12))
        header.grid(row=0, column=0, sticky="ew")
        logo = tk.Label(header, text="R2", font=("Segoe UI", 18, "bold"), background="#2563EB", foreground="white", padx=11, pady=6)
        logo.pack(side="left", padx=(0, 13))
        titles = ttk.Frame(header)
        titles.pack(side="left")
        ttk.Label(titles, text="R2 批量下载器", style="Hero.TLabel").pack(anchor="w")
        ttk.Label(titles, text="连接你的存储桶，把文件下载到本地。", style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Label(header, text="Windows 客户端  1.0", style="Subtitle.TLabel").pack(side="right")

        body = ttk.Frame(self.root, padding=(24, 0, 24, 0))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        side_container = ttk.Frame(body, style="Card.TFrame", width=350)
        side_container.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        side_container.grid_propagate(False)
        side_container.pack_propagate(False)
        self.sidebar_canvas = tk.Canvas(side_container, background="#FFFFFF", highlightthickness=0, width=334)
        side_scroll = ttk.Scrollbar(side_container, orient="vertical", command=self.sidebar_canvas.yview)
        side_scroll.pack(side="right", fill="y")
        self.sidebar_canvas.pack(side="left", fill="both", expand=True)
        self.sidebar_canvas.configure(yscrollcommand=side_scroll.set)
        sidebar = ttk.Frame(self.sidebar_canvas, style="Card.TFrame", padding=(16, 14))
        self.sidebar = sidebar
        sidebar_window = self.sidebar_canvas.create_window((0, 0), window=sidebar, anchor="nw")
        sidebar.bind("<Configure>", lambda event: self.sidebar_canvas.configure(scrollregion=self.sidebar_canvas.bbox("all")))
        self.sidebar_canvas.bind("<Configure>", lambda event: self.sidebar_canvas.itemconfigure(sidebar_window, width=event.width))
        self.root.bind("<MouseWheel>", self._scroll_sidebar, add="+")
        ttk.Label(sidebar, text="连接配置", style="CardTitle.TLabel").pack(anchor="w")
        self.account_entry = self._entry(sidebar, "Account ID / 账户 ID", "account_id")
        self._entry(sidebar, "Access Key ID / 访问密钥 ID", "access_key_id")
        self.secret_entry = self._entry(sidebar, "Secret Access Key / 私有访问密钥", "secret_access_key", secret=True)
        secret_options = ttk.Frame(sidebar, style="Card.TFrame")
        secret_options.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(secret_options, text="显示密钥", variable=self._var("show_secret", False, True), command=self._toggle_secret).pack(side="left")
        remember = ttk.Checkbutton(secret_options, text="记住密钥（本机加密）", variable=self._var("remember_secret", False, True))
        remember.pack(side="right")
        self.inputs.append(remember)
        self._entry(sidebar, "Bucket / 存储桶名称", "bucket")
        self.advanced = ttk.Frame(sidebar, style="Card.TFrame")
        self.advanced_toggle = self._button(sidebar, "高级：自定义 S3 Endpoint ▾", self._toggle_advanced)
        self.advanced_toggle.pack(fill="x", pady=(9, 0))
        self._entry(self.advanced, "S3 Endpoint（可选）", "endpoint_url")
        actions = ttk.Frame(sidebar, style="Card.TFrame")
        actions.pack(fill="x", pady=(11, 0))
        self._button(actions, "连接并读取文件", lambda: self._start("list")).pack(side="left", fill="x", expand=True)
        self._button(actions, "保存配置", self._save).pack(side="right", padx=(7, 0))
        ttk.Separator(sidebar).pack(fill="x", pady=(14, 11))
        ttk.Label(sidebar, text="下载设置", style="CardTitle.TLabel").pack(anchor="w")
        self._entry(sidebar, "目录前缀（留空下载整个存储桶）", "prefix")
        ttk.Label(sidebar, text="保存位置", style="Muted.TLabel").pack(anchor="w", pady=(9, 3))
        destination = ttk.Frame(sidebar, style="Card.TFrame")
        destination.pack(fill="x")
        output_entry = ttk.Entry(destination, textvariable=self._var("output_dir", str(Path.home() / "Downloads" / "R2")))
        output_entry.pack(side="left", fill="x", expand=True)
        self.inputs.append(output_entry)
        self._button(destination, "选择…", self._choose_folder).pack(side="right", padx=(6, 0))
        concurrency = ttk.Frame(sidebar, style="Card.TFrame")
        concurrency.pack(fill="x", pady=(11, 4))
        ttk.Label(concurrency, text="同时下载", style="Muted.TLabel").pack(side="left")
        spin = ttk.Spinbox(concurrency, from_=1, to=16, width=4, textvariable=self._var("workers", "4"))
        spin.pack(side="left", padx=(10, 8))
        self.inputs.append(spin)
        ttk.Label(concurrency, text="个文件", style="Muted.TLabel").pack(side="left")
        for text, name in (("移除本地路径中的目录前缀", "strip_prefix"), ("覆盖已有文件（默认跳过同大小文件）", "overwrite")):
            check = ttk.Checkbutton(sidebar, text=text, variable=self._var(name, False, True))
            check.pack(anchor="w")
            self.inputs.append(check)

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        listing = ttk.Frame(right, style="Card.TFrame", padding=(17, 13, 17, 15))
        listing.grid(row=0, column=0, sticky="nsew")
        toolbar = ttk.Frame(listing, style="Card.TFrame")
        toolbar.pack(fill="x", pady=(0, 11))
        ttk.Label(toolbar, text="存储桶文件", style="CardTitle.TLabel").pack(side="left")
        self.selection_button = self._button(toolbar, "下载所选", lambda: self._start("selected"))
        self.selection_button.pack(side="right")
        self._button(toolbar, "全选", self._select_all).pack(side="right", padx=(6, 6))
        ttk.Label(listing, textvariable=self.list_info, style="Muted.TLabel").pack(anchor="w", pady=(0, 10))
        self.notebook = ttk.Notebook(listing)
        self.notebook.pack(fill="both", expand=True)
        table_frame = ttk.Frame(self.notebook, style="Card.TFrame")
        logs_frame = ttk.Frame(self.notebook, style="Card.TFrame")
        self.notebook.add(table_frame, text="文件列表")
        self.notebook.add(logs_frame, text="运行记录")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table_frame, columns=("key", "size", "status"), show="headings", selectmode="extended")
        for column, title, width, stretch in (("key", "文件 / 对象路径", 330, True), ("size", "大小", 100, False), ("status", "状态", 100, False)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=70, stretch=stretch, anchor="e" if column == "size" else "w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        ybar.grid(row=0, column=1, sticky="ns")
        xbar = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        xbar.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        for tag, color in (("downloaded", "#16724B"), ("failed", "#C03535"), ("started", "#1D4ED8"), ("skipped", "#64748B")):
            self.tree.tag_configure(tag, foreground=color)
        self.tree.bind("<Control-a>", self._select_all)
        self.tree.bind("<Double-1>", self._show_result)
        self.empty_state = ttk.Frame(table_frame, style="Card.TFrame", padding=22)
        self.empty_state.place(relx=0.5, rely=0.45, anchor="center")
        ttk.Label(self.empty_state, text="从连接你的 R2 开始", style="CardTitle.TLabel").pack(pady=(0, 8))
        ttk.Label(self.empty_state, text="填写左侧配置，读取文件后即可批量下载。", style="Muted.TLabel").pack()
        self.logs = tk.Text(logs_frame, height=8, wrap="word", font=("Microsoft YaHei UI", 9), background="#FFFFFF", foreground="#465569", relief="flat", padx=12, pady=12, state="disabled")
        log_scroll = ttk.Scrollbar(logs_frame, orient="vertical", command=self.logs.yview)
        log_scroll.pack(side="right", fill="y")
        self.logs.pack(fill="both", expand=True)
        self.logs.configure(yscrollcommand=log_scroll.set)

        transfer = ttk.Frame(right, style="Card.TFrame", padding=(18, 16))
        transfer.grid(row=1, column=0, sticky="ew", pady=(15, 0))
        ttk.Label(transfer, textvariable=self.progress_text, style="CardTitle.TLabel").pack(anchor="w")
        self.progress = ttk.Progressbar(transfer, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(13, 9))
        ttk.Label(transfer, textvariable=self.progress_detail, style="Muted.TLabel", wraplength=600).pack(anchor="w")
        buttons = ttk.Frame(transfer, style="Card.TFrame")
        buttons.pack(fill="x", pady=(13, 0))
        self._button(buttons, "打开保存位置", self._open_output).pack(side="left")
        self.stop_button = ttk.Button(buttons, text="停止", command=self._stop, state="disabled")
        self.stop_button.pack(side="right", padx=(8, 0))
        self.start_button = self._button(buttons, "开始批量下载", lambda: self._start("download"), style="Primary.TButton")
        self.start_button.pack(side="right")
        footer = ttk.Frame(self.root, padding=(25, 10, 25, 11))
        footer.grid(row=2, column=0, sticky="ew")
        ttk.Label(footer, textvariable=self.status, style="Subtitle.TLabel").pack(anchor="w")

    def _scroll_sidebar(self, event: tk.Event) -> str | None:
        widget = event.widget
        while widget is not None:
            if widget in (self.sidebar, self.sidebar_canvas):
                self.sidebar_canvas.yview_scroll(-int(event.delta / 120), "units")
                return "break"
            widget = getattr(widget, "master", None)
        return None

    def _load(self) -> None:
        saved = load_settings()
        for name, variable in self.fields.items():
            if name in saved and name != "show_secret":
                variable.set(saved[name])
        if saved.get("endpoint_url"):
            self._toggle_advanced()
        if saved.get("_warning"):
            self._log(saved["_warning"])
            self.status.set(saved["_warning"])

    def _values(self) -> dict[str, Any]:
        return {name: variable.get() for name, variable in self.fields.items() if name != "show_secret"}

    def _save(self) -> None:
        try:
            values = self._values()
            values["workers"] = int(str(values["workers"]))
            if not 1 <= values["workers"] <= 16:
                raise ValueError("同时下载数量请填写 1 到 16。")
            save_settings(values, remember_secret=bool(values["remember_secret"]))
            self.status.set("配置已保存。" + ("密钥已在本机加密保存。" if values["remember_secret"] else "下次打开时重新填写私有密钥。"))
        except Exception as exc:
            messagebox.showerror("无法保存配置", str(exc), parent=self.root)

    def _toggle_secret(self) -> None:
        self.secret_entry.configure(show="" if self.fields["show_secret"].get() else "•")

    def _toggle_advanced(self) -> None:
        if self.advanced.winfo_manager():
            self.advanced.pack_forget()
            self.advanced_toggle.configure(text="高级：自定义 S3 Endpoint ▾")
        else:
            self.advanced.pack(fill="x", after=self.advanced_toggle)
            self.advanced_toggle.configure(text="收起自定义 S3 Endpoint ▴")

    def _choose_folder(self) -> None:
        directory = filedialog.askdirectory(title="选择下载保存位置", parent=self.root)
        if directory:
            self.fields["output_dir"].set(directory)

    def _open_output(self) -> None:
        path = Path(str(self.fields["output_dir"].get())).expanduser()
        if not path.is_dir():
            messagebox.showinfo("保存位置", "此文件夹尚不存在，请先选择保存位置并开始下载。", parent=self.root)
            return
        if os.name == "nt":
            os.startfile(str(path.resolve()))

    def _select_all(self, event: tk.Event | None = None) -> str:
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _show_result(self, event: tk.Event | None = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        key = str(self.tree.item(selection[0], "values")[0])
        result = self.results.get(key)
        if result and (result.get("error") or result.get("reason")):
            messagebox.showinfo("文件处理详情", f"{key}\n\n{result.get('error') or result.get('reason')}", parent=self.root)

    @staticmethod
    def _context(connection: Connection, prefix: str) -> tuple[Any, ...]:
        return (connection.account_id, connection.endpoint_url, connection.access_key_id, connection.secret_access_key, connection.bucket, prefix)

    def _start(self, mode: str) -> None:
        if self.busy:
            return
        try:
            values = self._values()
            connection = Connection(**{name: str(values[name]).strip() for name in ("account_id", "access_key_id", "secret_access_key", "bucket", "endpoint_url")})
            validate_connection(connection)
            workers = int(str(values["workers"]))
            if not 1 <= workers <= 16:
                raise ValueError("同时下载数量请填写 1 到 16。")
            options = DownloadOptions(output_dir=str(values["output_dir"]).strip(), prefix=str(values["prefix"]), workers=workers, overwrite=bool(values["overwrite"]), strip_prefix=bool(values["strip_prefix"]))
            if mode != "list" and not options.output_dir:
                raise ValueError("请先选择下载保存位置。")
            selected_objects = None
            context = self._context(connection, options.prefix)
            if mode == "selected":
                if context != self.cached_context:
                    raise ValueError("连接信息或目录前缀已修改，请先重新读取文件列表。")
                selected_keys = {str(self.tree.item(row, "values")[0]) for row in self.tree.selection()}
                selected_objects = [item for item in self.objects if item["Key"] in selected_keys]
                if not selected_objects:
                    raise ValueError("请先在文件列表中选择需要下载的文件，可按 Ctrl 多选。")
            if mode != "list" and options.overwrite:
                if not messagebox.askyesno("确认覆盖已有文件", "本次下载将替换保存位置下的同名文件。确定继续？", parent=self.root):
                    return
        except (ValueError, tk.TclError) as exc:
            self.status.set(str(exc))
            messagebox.showerror("请检查配置", str(exc), parent=self.root)
            return

        self.cancel.clear()
        self.results.clear()
        self.completed = self.transferred = self.batch_total = 0
        self.transfer_active = False
        self.started_at = time.monotonic()
        self._set_busy(True)
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self.progress_text.set("正在连接…")
        self.progress_detail.set("正在读取文件列表，窗口可继续响应；点击「停止」可取消。")
        self.status.set("正在连接 R2…")
        self._log("开始连接并读取文件。" if mode == "list" else "开始批量下载任务。")
        # Config persistence is explicit through Save, never implicit on connection.
        cached = list(self.objects) if context == self.cached_context else None

        def work() -> None:
            try:
                items = selected_objects
                if items is None:
                    items = cached if mode != "list" and cached is not None else list_objects(
                        connection, options.prefix, self.cancel,
                        on_progress=lambda count: self.events.put({"kind": "scan", "count": count}),
                    )
                    self.events.put({"kind": "listed", "objects": items, "context": context})
                if mode == "list":
                    self.events.put({"kind": "listing_done", "count": len(items)})
                else:
                    if self.cancel.is_set():
                        raise Cancelled()
                    self.events.put({"kind": "batch", "total": len(items)})
                    summary = download_batch(connection, items, options, self.cancel, self.events.put)
                    self.events.put({"kind": "finished", "summary": summary})
            except Cancelled:
                self.events.put({"kind": "cancelled"})
            except Exception as exc:
                error = str(exc)
                for credential in (connection.access_key_id, connection.secret_access_key):
                    if credential:
                        error = error.replace(credential, "[已隐藏]")
                self.events.put({"kind": "error", "error": self._friendly_error(error)})
            finally:
                self.events.put({"kind": "idle"})

        threading.Thread(target=work, name="r2-desktop-worker", daemon=True).start()

    @staticmethod
    def _friendly_error(error: str) -> str:
        if "AccessDenied" in error or "Forbidden" in error or "403" in error:
            return "访问被拒绝，请检查 Access Key、Secret Key，以及该凭据对目标存储桶的读取权限。\n" + error
        if "SignatureDoesNotMatch" in error or "InvalidAccessKeyId" in error:
            return "凭据校验失败，请检查 Access Key ID、Secret Access Key 是否配套。\n" + error
        if "NoSuchBucket" in error:
            return "未找到存储桶，请检查 Bucket 名称与账户 / Endpoint。\n" + error
        if "Could not connect" in error or "timeout" in error.lower():
            return "连接失败或超时，请检查网络、Account ID 和 S3 Endpoint，然后重试。\n" + error
        return error

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        for widget in self.inputs + self.action_buttons:
            widget.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")

    def _stop(self) -> None:
        if self.busy:
            self.cancel.set()
            self.stop_button.configure(state="disabled")
            self.status.set("正在停止，等待当前网络请求结束并清理临时文件…")
            self.progress_text.set("正在停止…")

    def _log(self, text: str) -> None:
        self.logs.configure(state="normal")
        self.logs.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n")
        if int(self.logs.index("end-1c").split(".")[0]) > 600:
            self.logs.delete("1.0", "100.0")
        self.logs.see("end")
        self.logs.configure(state="disabled")

    def _populate(self, items: list[dict[str, Any]]) -> None:
        self.tree.delete(*self.tree.get_children())
        self.row_by_key.clear()
        for index, item in enumerate(items[:self.MAX_ROWS]):
            key = item["Key"]
            row = self.tree.insert("", "end", iid=str(index), values=(key, format_size(item.get("Size", 0)), "待下载"))
            self.row_by_key[key] = row
        self.empty_state.place_forget()
        total = sum(item.get("Size", 0) for item in items)
        text = f"{len(items):,} 个文件 · {format_size(total)}"
        if len(items) > self.MAX_ROWS:
            text += f"  · 列表显示前 {self.MAX_ROWS:,} 个，批量下载包含全部文件"
        self.list_info.set(text)

    def _handle(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "scan":
            self.status.set(f"正在读取文件列表，已发现 {event['count']:,} 个对象…")
        elif kind == "listed":
            self.objects = event["objects"]
            self.cached_context = event["context"]
            self._populate(self.objects)
        elif kind == "listing_done":
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.progress_text.set("连接成功")
            self.progress_detail.set("选中文件可下载所选；点击「开始批量下载」可下载当前前缀下的全部文件。")
            self.status.set(f"连接成功，共读取 {event['count']:,} 个对象。")
            self._log(self.status.get())
        elif kind == "batch":
            self.transfer_active = True
            self.batch_total = event["total"]
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.progress_text.set(f"下载中  0 / {self.batch_total:,}")
        elif kind == "started":
            key = event["key"]
            if key in self.row_by_key:
                self.tree.set(self.row_by_key[key], "status", "下载中")
                self.tree.item(self.row_by_key[key], tags=("started",))
        elif kind == "progress":
            self.transferred += max(0, event.get("bytes", 0))
        elif kind == "result":
            self.completed += 1
            key = event["key"]
            self.results[key] = event
            state = event["status"]
            label = {"downloaded": "已下载", "skipped": "已跳过", "failed": "失败", "cancelled": "已停止"}.get(state, state)
            if key in self.row_by_key:
                self.tree.set(self.row_by_key[key], "status", label)
                self.tree.item(self.row_by_key[key], tags=(state,))
            self.progress.configure(value=100 * self.completed / max(1, self.batch_total))
            self.progress_text.set(f"下载中  {self.completed:,} / {self.batch_total:,}")
            self._log(f"{label}  {key}" + (f" — {event.get('error') or event.get('reason')}" if event.get("error") or event.get("reason") else ""))
        elif kind == "finished":
            self.transfer_active = False
            summary = event["summary"]
            self.progress.stop()
            if not summary.get("cancelled"):
                self.progress.configure(value=100)
            title = "下载已停止" if self.cancel.is_set() or summary.get("cancelled") else "下载完成"
            self.progress_text.set(title)
            description = f"成功 {summary['downloaded']:,} · 跳过 {summary['skipped']:,} · 失败 {summary['failed']:,}"
            if summary.get("cancelled"):
                description += f" · 未完成 {summary['cancelled']:,}"
            self.progress_detail.set(description)
            self.status.set(f"{title}。" + ("失败详情见「运行记录」，双击失败文件也可查看。" if summary["failed"] else ""))
            self._log(title + "：" + description)
        elif kind == "cancelled":
            self.transfer_active = False
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.progress_text.set("已停止")
            self.status.set("操作已停止，已完成的文件会保留。")
            self.progress_detail.set("可重新开始下载；已有同大小文件会自动跳过。")
        elif kind == "error":
            self.transfer_active = False
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress_text.set("操作失败")
            self.status.set("操作失败，请查看错误提示并重试。")
            self.progress_detail.set("详情已写入运行记录。")
            self._log(event["error"])
            if not self.closing:
                messagebox.showerror("R2 操作失败", event["error"], parent=self.root)
        elif kind == "idle":
            self.transfer_active = False
            self._set_busy(False)
            if self.closing:
                self.destroyed = True
                self.root.destroy()

    def _poll(self) -> None:
        for _ in range(300):
            try:
                self._handle(self.events.get_nowait())
            except queue.Empty:
                break
        if self.busy and self.transfer_active and self.batch_total and not self.cancel.is_set():
            elapsed = max(0.1, time.monotonic() - self.started_at)
            self.progress_detail.set(f"已传输 {format_size(self.transferred)}  ·  平均 {format_size(self.transferred / elapsed)}/s  ·  已处理 {self.completed:,} 个文件")
        if not self.destroyed:
            self.root.after(80, self._poll)

    def _close(self) -> None:
        if self.busy:
            if not messagebox.askyesno("停止并退出", "正在执行任务。是否停止任务，清理临时文件后退出？", parent=self.root):
                return
            self.closing = True
            self._stop()
        else:
            self.destroyed = True
            self.root.destroy()


def main() -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    R2DownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
