"""Focused onboarding and practice views using the incumbent native styles."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox

from relay_diagnostics import connection_steps
from relay_experience import CHAT_TEST_PROMPT, PracticeSession, repair_prompt
from relay_preferences import save_preferences


class ExperienceUI:
    def _build_experience_controls(self) -> None:
        self._selection_lock_id = None
        self._experience_windows = {}
        self._full_geometry = None
        self._compact_mode = self._window_preferences.get("compact_mode", False)
        if self._compact_mode:
            self.root.geometry("640x600")
        self.mode_button = ttk.Button(self.preferences_row, text="紧凑模式",
                                      command=self.toggle_compact_mode, style="Diag.HAR.TButton")
        self.mode_button.grid(row=0, column=5, padx=(8, 0))
        self.queue_button = ttk.Button(self.preferences_row, text="查看队列",
                                       command=self.show_request_queue, style="Diag.HAR.TButton")
        self.queue_button.grid(row=0, column=6, padx=(8, 0))
        self.diagnostics_button.configure(command=self.show_connection_guide)
        self.tree.bind("<ButtonRelease-1>", self._lock_user_selection, add="+")
        self.tree.bind("<KeyRelease>", self._lock_user_selection, add="+")
        self.root.bind("<F1>", lambda _event: self.show_connection_guide())
        self.root.bind("<F2>", lambda _event: self.toggle_compact_mode())
        self.root.bind("<F3>", lambda _event: self.show_practice())

    def _note_decision_interaction(self) -> None:
        self._reset_auto_approve_timers()
        self._lock_current_request()

    def _lock_current_request(self) -> None:
        if self.current_request_id and self._request_rows.get(self.current_request_id, {}).get("state") == "pending":
            self._selection_lock_id = self.current_request_id

    def _lock_user_selection(self, _event=None) -> None:
        selection = self.tree.selection()
        if selection:
            item = str(selection[0])
            if item.startswith("request:"):
                self._selection_lock_id = item.removeprefix("request:")

    def _selection_is_protected(self) -> bool:
        return bool(self.current_request_id and (
            self._selection_lock_id == self.current_request_id
            or self._detail_expanded
            or any(button._pressed or button._native_invoke_pending
                   for button in (self.approve_button, self.reject_button, self.stop_button))))

    def _refresh_experience(self, pending_count: int) -> None:
        if self._request_rows.get(self._selection_lock_id, {}).get("state") != "pending":
            self._selection_lock_id = None
        self.queue_button.configure(text=f"查看队列 {pending_count}" if pending_count else "查看队列")
        self.mode_button.configure(text="完整视图" if self._compact_mode else "紧凑模式")

    def _apply_experience_layout(self) -> None:
        narrow = self.root.winfo_width() < 820
        if narrow:
            self.theme_label.grid_remove()
            self.sound_toggle.grid_remove()
            self.connection_label.grid_configure(row=1, column=0, columnspan=2, padx=0, pady=(4, 0))
        else:
            self.theme_label.grid()
            self.sound_toggle.grid()
            self.connection_label.grid_configure(row=0, column=1, columnspan=1, padx=(12, 0), pady=0)
        if self._compact_mode:
            self.task_panel.grid_remove()
            self.main.columnconfigure(0, weight=1, minsize=0, uniform="")
            self.main.columnconfigure(1, weight=0, minsize=0, uniform="")
            self.decision_panel.grid_configure(column=0, columnspan=2, padx=0)
        else:
            self.task_panel.grid()
            self.decision_panel.grid_configure(column=1, columnspan=1, padx=(8, 0))
        if self._detail_expanded and self._compact_mode:
            self.detail_panel.grid_configure(column=0, columnspan=2)

    def toggle_compact_mode(self) -> None:
        values = {**self._window_preferences, "compact_mode": not self._compact_mode}
        try:
            save_preferences(self._window_preferences_path, values)
        except OSError:
            self._show_modal_message(messagebox.showerror, "无法保存视图", "请检查偏好目录的写入权限。", parent=self.root)
            return
        if not self._compact_mode:
            self._full_geometry = self.root.geometry()
        self._compact_mode = values["compact_mode"]
        self._window_preferences = values
        self.root.geometry("640x600" if self._compact_mode else (self._full_geometry or "980x720"))
        self._apply_responsive_layout(force=True)
        self._content_render_signature = None
        self._refresh_experience(self.store.active_counts()[0])
        if self.current_request_id:
            self._render_detail(self.current_request_id)

    def show_request_queue(self) -> None:
        if self._compact_mode:
            self.toggle_compact_mode()
        self.tree.focus_set()
        pending = next((item for item in self._request_rows.values() if item["state"] == "pending"), None)
        if pending and self.tree.exists(self._request_item_id(pending["request_id"])):
            self.tree.see(self._request_item_id(pending["request_id"]))

    def _copy_experience_text(self, text: str, status=None) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
        except tk.TclError:
            if status is not None:
                status.configure(text="复制失败，请重试。")
            return
        if status is not None:
            status.configure(text="已复制，请粘贴到目标聊天。")

    def _experience_window(self, key: str, title: str):
        existing = self._experience_windows.get(key)
        if existing is not None and existing.winfo_exists():
            if self.root.state() != "withdrawn":
                existing.lift()
            return None
        window = tk.Toplevel(self.root)
        window.title(title)
        window.transient(self.root)
        window.geometry("620x530")
        window.minsize(540, 480)
        if self.root.state() == "withdrawn":
            window.withdraw()
        self._experience_windows[key] = window
        self._auto_approval_modal_depth += 1
        self._reset_auto_approve_timers()
        def destroyed(event):
            if event.widget is window:
                self._experience_windows.pop(key, None)
                self._auto_approval_modal_depth = max(0, self._auto_approval_modal_depth - 1)
                self._reset_auto_approve_timers()
        window.bind("<Destroy>", destroyed, add="+")
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        window.bind("<Escape>", lambda _event: window.destroy())
        return window

    def _experience_body(self, window):
        outer = ttk.Frame(window, padding=20, style="HAR.TFrame")
        outer.pack(fill=tk.BOTH, expand=True)
        footer = ttk.Frame(outer, style="HAR.TFrame")
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 0))
        def fit_actions(event):
            for row in footer.winfo_children():
                children = row.winfo_children()
                stack = sum(item.winfo_reqwidth() + 12 for item in children) > event.width
                for child in children:
                    if child.winfo_manager() == "pack":
                        child.pack_configure(side=tk.TOP if stack else tk.LEFT,
                                             anchor=tk.W, padx=(0, 8), pady=2)
        footer.bind("<Configure>", fit_actions)
        viewport = ttk.Frame(outer, style="HAR.TFrame")
        viewport.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(viewport, highlightthickness=0, borderwidth=0,
                           background=self.palette.window)
        scrollbar = ttk.Scrollbar(viewport, orient=tk.VERTICAL, command=canvas.yview,
                                   style="HAR.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body = ttk.Frame(canvas, style="HAR.TFrame")
        item = canvas.create_window(0, 0, window=body, anchor=tk.NW)
        body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        def resize(event):
            canvas.itemconfigure(item, width=event.width)
            for child in body.winfo_children():
                if isinstance(child, ttk.Label):
                    child.configure(wraplength=max(180, event.width - 12))
        canvas.bind("<Configure>", resize)
        def scroll(event):
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"
        window.bind("<MouseWheel>", scroll)
        window.bind("<Next>", lambda _event: canvas.yview_scroll(1, "pages"))
        window.bind("<Prior>", lambda _event: canvas.yview_scroll(-1, "pages"))
        return body, footer

    def show_connection_guide(self) -> None:
        from relay_broker import collect_install_diagnostics
        window = self._experience_window("guide", "接入检查 · 跑通第一次问询")
        if window is None:
            return
        panel, footer = self._experience_body(window)
        ttk.Label(panel, text="让这个聊天真正收到一次答案", style="Title.HAR.TLabel").pack(anchor=tk.W)
        ttk.Label(panel, text="窗口能打开，还需要在你要使用的聊天中跑通测试。", style="Meta.HAR.TLabel",
                  wraplength=540).pack(anchor=tk.W, pady=(4, 14))
        rows = []
        for _ in range(4):
            heading = ttk.Label(panel, style="CardSection.HAR.TLabel", wraplength=540)
            heading.pack(anchor=tk.W, pady=(8, 2))
            detail = ttk.Label(panel, style="Meta.HAR.TLabel", wraplength=540, justify=tk.LEFT)
            detail.pack(anchor=tk.W)
            rows.append((heading, detail))
        status = ttk.Label(panel, text="", style="Meta.HAR.TLabel", wraplength=540)
        status.pack(anchor=tk.W, pady=(14, 0))
        snapshot = {}
        def refresh():
            snapshot.clear()
            snapshot.update(collect_install_diagnostics(self.server))
            for index, ((name, state, hint), (heading, detail)) in enumerate(zip(connection_steps(snapshot), rows), 1):
                heading.configure(text=f"{index}. {name} · {state}")
                detail.configure(text=hint)
        actions = ttk.Frame(footer, style="HAR.TFrame")
        actions.pack(fill=tk.X)
        test_button = ttk.Button(actions, text="复制聊天测试提示词", style="Diag.HAR.TButton",
                                 command=lambda: self._copy_experience_text(CHAT_TEST_PROMPT, status))
        test_button.pack(side=tk.LEFT)
        ttk.Button(actions, text="复制修复任务", style="Diag.HAR.TButton",
                   command=lambda: self._copy_experience_text(repair_prompt(snapshot), status)).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="刷新", style="Diag.HAR.TButton", command=refresh).pack(side=tk.RIGHT)
        links = ttk.Frame(footer, style="HAR.TFrame")
        links.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(links, text="先体验演示", style="Diag.HAR.TButton", command=self.show_practice).pack(side=tk.LEFT)
        ttk.Button(links, text="详细诊断", style="Diag.HAR.TButton", command=self.show_diagnostics).pack(side=tk.LEFT, padx=6)
        window.bind("<Control-c>", lambda _event: self._copy_experience_text(CHAT_TEST_PROMPT, status))
        window.bind("<F5>", lambda _event: refresh())
        if self.root.state() != "withdrawn":
            test_button.focus_set()
        refresh()

    def show_practice(self) -> None:
        window = self._experience_window("practice", "演示模式 · 不连接真实聊天")
        if window is None:
            return
        session = PracticeSession()
        panel, footer = self._experience_body(window)
        title = ttk.Label(panel, style="Title.HAR.TLabel", wraplength=540)
        title.pack(anchor=tk.W)
        ttk.Label(panel, text="这里不会运行命令、修改偏好或生成真实授权；演示不代表聊天已经接入。",
                  style="Meta.HAR.TLabel", wraplength=540).pack(anchor=tk.W, pady=(6, 22))
        question = ttk.Label(panel, style="DecisionTitle.HAR.TLabel", wraplength=540)
        question.pack(anchor=tk.W)
        choices = ttk.Label(panel, style="DecisionTarget.HAR.TLabel", wraplength=540, justify=tk.LEFT)
        choices.pack(anchor=tk.W, pady=12)
        result = ttk.Label(panel, style="DecisionState.HAR.TLabel", wraplength=540)
        result.pack(anchor=tk.W, pady=14)
        actions = ttk.Frame(panel, style="HAR.TFrame")
        actions.pack(anchor=tk.W, pady=12)
        next_button = ttk.Button(footer, text="下一题", style="Diag.HAR.TButton", state=tk.DISABLED)
        next_button.pack(anchor=tk.W, pady=10)
        def render():
            title.configure(text=session.current["title"])
            question.configure(text=session.current["question"])
            choices.configure(text="\n".join(f"{index}. {label}" for index, label in enumerate(session.current["choices"], 1)))
            result.configure(text="请试着选择同意、拒绝或停止。")
            next_button.configure(state=tk.DISABLED)
        def answer(action):
            result.configure(text=session.answer(action))
            next_button.configure(state=tk.NORMAL)
        for label, action in (("同意", "agree"), ("拒绝", "reject"), ("停止", "stop")):
            button = ttk.Button(actions, text=label, style="Diag.HAR.TButton", command=lambda value=action: answer(value))
            button.pack(side=tk.LEFT, padx=(0, 12))
            if action == "agree" and self.root.state() != "withdrawn":
                button.focus_set()
        def advance():
            if session.advance():
                render()
            else:
                title.configure(text="演示完成 · 再跑通真实聊天")
                question.configure(text="将测试提示词发给 Agent，真实回执才能证明该聊天已接入。")
                choices.configure(text="")
                actions.pack_forget()
                next_button.configure(text="复制聊天测试提示词", command=lambda: self._copy_experience_text(CHAT_TEST_PROMPT, result))
        next_button.configure(command=advance)
        render()
