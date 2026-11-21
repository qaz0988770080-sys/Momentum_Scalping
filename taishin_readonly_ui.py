"""Local Tk acceptance UI. The read-only SDK lives in an isolated child process."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

PREFIX = "@@MOMENTUM_READONLY@@"

class ReadOnlyWindow:
    def __init__(self, root):
        self.root = root
        self.base = Path(__file__).resolve().parent
        self.proc = None
        self.events = queue.Queue()
        self.connected = False
        self.ready = False
        self.busy = False
        self.deadline = None
        self.closed = False
        root.title("Momentum 台新獨立專案 — 唯讀驗收")
        root.geometry("880x650")
        root.minsize(780, 600)
        outer = ttk.Frame(root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="台新 Core · 唯讀連線驗收", font=("", 17, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(outer, text="只登入及讀取資料；沒有買進、賣出、改單或撤單功能。").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(6, 15))
        ttk.Label(outer, text="環境").grid(row=2, column=0, sticky="w")
        self.environment = ttk.Combobox(outer, state="readonly", width=34,
            values=["台新正式環境（唯讀查詢）", "台新驗證環境（僅測登入）"])
        self.environment.current(0)
        self.environment.grid(row=2, column=1, sticky="w", pady=5)
        ttk.Label(outer, text="登入 ID").grid(row=3, column=0, sticky="w")
        self.username = ttk.Entry(outer, width=36)
        self.username.grid(row=3, column=1, sticky="w", pady=5)
        ttk.Label(outer, text="登入密碼").grid(row=4, column=0, sticky="w")
        self.password = ttk.Entry(outer, show="●", width=36)
        self.password.grid(row=4, column=1, sticky="w", pady=5)
        self.login_button = ttk.Button(outer, text="唯讀登入", command=self.login)
        self.login_button.grid(row=3, column=2, padx=15)
        self.logout_button = ttk.Button(outer, text="登出", command=self.logout)
        self.logout_button.grid(row=4, column=2, padx=15)
        ttk.Label(outer, text="證券帳戶").grid(row=5, column=0, sticky="w")
        self.accounts = ttk.Combobox(outer, state="readonly", width=34)
        self.accounts.grid(row=5, column=1, sticky="w", pady=(10, 15))
        actions = ttk.Frame(outer)
        actions.grid(row=6, column=0, columnspan=4, sticky="w", pady=(0, 12))
        self.query_buttons = []
        for label, action in [("查庫存完成狀態", "inventory"),
                              ("觀察委託回報", "orders"), ("觀察成交回報", "matches")]:
            button = ttk.Button(actions, text=label, command=lambda a=action: self.query(a))
            button.pack(side="left", padx=(0, 12))
            self.query_buttons.append(button)
        self.status_text = tk.StringVar(value="正在啟動獨立唯讀程序……")
        ttk.Label(outer, textvariable=self.status_text, wraplength=790).grid(
            row=7, column=0, columnspan=4, sticky="ew", pady=(0, 10))
        logs = ttk.Frame(outer)
        logs.grid(row=8, column=0, columnspan=4, sticky="nsew")
        self.log = tk.Text(logs, wrap="word", height=15, state="disabled", font=("", 10))
        scroll = ttk.Scrollbar(logs, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        ttk.Label(outer, text="帳密只在本機記憶體傳遞；驗收紀錄不保存帳號、密碼、持股或回報原文。").grid(
            row=9, column=0, columnspan=4, sticky="w", pady=(10, 0))
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(8, weight=1)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.update_buttons()
        self.start_worker()
        root.after(100, self.poll)

    def log_text(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", "[" + time.strftime("%H:%M:%S") + "] " + text + "\n")
        # Do not retain an unbounded account history, even in the local UI.
        if int(self.log.index("end-1c").split(".")[0]) > 1000:
            self.log.delete("1.0", "300.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def update_buttons(self):
        self.login_button.configure(state="normal" if self.ready and not self.connected
                                    and not self.busy else "disabled")
        self.logout_button.configure(state="normal" if self.connected and not self.busy else "disabled")
        self.environment.configure(state="readonly" if not self.connected and not self.busy else "disabled")
        for i, button in enumerate(self.query_buttons):
            allowed = self.connected and not self.busy and (i != 0 or self.environment.current() == 0)
            button.configure(state="normal" if allowed else "disabled")

    def start_worker(self):
        try:
            sdk = self.base / "vendor"
            if not (sdk / "MasterTradePy" / "api.py").is_file():
                raise RuntimeError("缺少本專案的獨立 SDK，請先準備 vendor 目錄。")
            self.proc = subprocess.Popen(
                [sys.executable, "-u", str(self.base / "taishin_readonly_worker.py"),
                 "--sdk-root", str(sdk), "--parent-pid", str(os.getpid())],
                cwd=self.base, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            process = self.proc
            def reader():
                try:
                    for line in process.stdout:
                        if line.startswith(PREFIX):
                            self.events.put((process, json.loads(line[len(PREFIX):])))
                except Exception:
                    pass
                self.events.put((process, dict(kind="exited")))
            threading.Thread(target=reader, daemon=True).start()
        except Exception as exc:
            self.status_text.set(str(exc))

    def send(self, data, *, timeout=20):
        if self.proc is None or self.proc.poll() is not None or self.proc.stdin is None:
            raise RuntimeError("唯讀程序未就緒，請重新開啟視窗。")
        self.proc.stdin.write(json.dumps(data, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        self.busy = True
        self.deadline = time.monotonic() + timeout
        self.update_buttons()

    def login(self):
        if not self.username.get().strip() or not self.password.get():
            messagebox.showinfo("本機登入", "請在這個視窗輸入台新 ID 與密碼。")
            return
        data = dict(cmd="login", username=self.username.get().strip(),
                    password=self.password.get(),
                    environment="formal" if self.environment.current() == 0 else "validation")
        try:
            self.send(data, timeout=45)
            self.status_text.set("等待台新登入、憑證及交易主機就緒；不會送單。")
            self.log_text("開始唯讀登入。")
        except Exception as exc:
            self.status_text.set(str(exc))
        finally:
            data.clear()
            self.username.delete(0, "end")
            self.password.delete(0, "end")

    def query(self, action):
        try:
            self.send(dict(cmd=action, account_index=self.accounts.current()))
            self.status_text.set("等待這次查詢的完整回覆……")
        except Exception as exc:
            self.status_text.set(str(exc))

    def poll(self):
        if self.closed:
            return
        while True:
            try:
                process, event = self.events.get_nowait()
            except queue.Empty:
                break
            # An old worker's exit event must not disable a newly opened worker.
            if process is not self.proc:
                continue
            self.handle(event)
        if self.busy and self.deadline is not None and time.monotonic() > self.deadline:
            self.stop_worker()
            self.status_text.set("本次操作逾時；連線已結束，未送出任何委託。")
            self.log_text("逾時不視為查詢完成；可重新唯讀登入。")
            self.start_worker()
        self.root.after(100, self.poll)

    def handle(self, event):
        kind = event.get("kind")
        self.busy = False
        self.deadline = None
        if kind == "ready":
            self.ready = True
            self.status_text.set("唯讀程序已就緒，請在本機輸入台新帳密。")
            self.log_text("此獨立程序沒有送單功能。")
        elif kind == "login":
            self.connected = bool(event.get("connected"))
            self.accounts["values"] = event.get("accounts", [])
            if self.accounts["values"]:
                self.accounts.current(0)
            self.status_text.set("唯讀登入成功；委託回補完整性尚待驗證，正式送單仍關閉。")
            self.log_text("交易主機已就緒；取得遮罩證券帳戶清單。")
        elif kind == "inventory":
            self.status_text.set("庫存查詢完成；可賣量與整體交易對帳仍未驗證。")
            self.log_text("庫存筆數：" + str(event["row_count"]) + "；" + event["completion_evidence"])
            for row in event.get("rows", []):
                self.log_text("商品 " + row["symbol"] + "，現股 " + str(row["quantity_shares"]) +
                              " 股，零股欄位 " + str(row["odd_shares"]) + " 股。")
        elif kind in {"orders", "matches"}:
            self.status_text.set("回報觀測完成；此快取尚不能宣告券商清單完整。")
            self.log_text("觀測筆數：" + str(event["row_count"]) + "；" + event["error"])
        elif kind == "error":
            self.connected = bool(event.get("connected", self.connected))
            self.status_text.set("本次操作未通過：" + event["error"])
            self.log_text("階段 " + str(event.get("action")) + "：" + event["error"])
        elif kind == "exited":
            self.ready = self.connected = False
            self.status_text.set("唯讀程序已結束，請關閉後重新開啟。")
        self.update_buttons()

    def stop_worker(self):
        process, self.proc = self.proc, None
        if process is not None:
            try:
                if process.poll() is None:
                    try:
                        process.stdin.write('{"cmd":"logout"}\n')
                        process.stdin.flush()
                        process.wait(timeout=1)
                    except Exception:
                        process.kill()
                        process.wait(timeout=2)
            finally:
                if process.stdin:
                    process.stdin.close()
                if process.stdout:
                    process.stdout.close()
        self.ready = self.connected = self.busy = False
        self.deadline = None
        self.update_buttons()

    def logout(self):
        self.stop_worker()
        self.accounts.set("")
        self.accounts["values"] = []
        self.status_text.set("已登出。")
        self.log_text("唯讀連線已結束。")
        self.start_worker()

    def close(self):
        self.closed = True
        self.username.delete(0, "end")
        self.password.delete(0, "end")
        self.stop_worker()
        self.root.destroy()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()
    root = tk.Tk()
    app = ReadOnlyWindow(root)
    result = {}
    if args.smoke_test:
        root.withdraw()
        def check():
            result["ready"] = app.ready
            result["login_enabled"] = str(app.login_button["state"]) == "normal"
            result["query_disabled"] = all(str(b["state"]) == "disabled" for b in app.query_buttons)
            result["worker_alive"] = app.proc is not None and app.proc.poll() is None
            app.close()
        root.after(2500, check)
    root.mainloop()
    if args.smoke_test:
        print(json.dumps(dict(ui_smoke_ok=all(result.values()), checks=result)))
        return 0 if result and all(result.values()) else 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
