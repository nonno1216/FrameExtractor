# -*- coding: utf-8 -*-
"""動画コマ書き出し - 動画を画像・GIF・WebP・MP4・音声などに書き出すソフト"""
import base64
import http.server
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox
from types import SimpleNamespace

import customtkinter as ctk

import cv2
import numpy as np

import engine as E
import missav as MISSAV  # MissAV 専用（yt-dlp 非対応のため直接 HLS を取得する）

try:
    import yt_dlp
    YT_OK = True
except Exception:
    YT_OK = False

try:
    from tkinterdnd2 import DND_FILES, DND_TEXT, TkinterDnD
    DND_OK = True
except Exception:
    DND_OK = False

if DND_OK:
    class Window(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.TkdndVersion = TkinterDnD._require(self)
else:
    Window = ctk.CTk


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# ---- デザイン（(ライト, ダーク) の組で指定）----
if sys.platform.startswith("win"):
    FONT_FAMILY = "Yu Gothic UI"
elif sys.platform == "darwin":
    FONT_FAMILY = "Hiragino Sans"
else:
    FONT_FAMILY = "Noto Sans CJK JP"
BG = ("#eef1ef", "#141817")
CARD = ("#ffffff", "#1f2523")
INK = ("#1d2321", "#e8ecea")
SUB = ("#66726c", "#9aa5a0")
ACC = ("#1f6f5c", "#2a8a73")
ACC_H = ("#185a4a", "#34a085")
SEC = ("#e4e9e6", "#2f3835")
SEC_H = ("#d5dcd8", "#3b4642")
SEC_B = ("#d0d8d3", "#3b4642")
LINE = ("#d5dad7", "#3a4440")
ENTRY = ("#f5f7f6", "#171c1a")
PREV_BG = ("#e0e5e2", "#0d100f")
SUBCARD = ("#f1f5f3", "#262d2a")
DIS = ("#a3ada8", "#66706b")

# ---- 選択肢 ----
FMT_PNG, FMT_JPG = "PNG（高画質）", "JPG（軽い）"
FMT_GIF, FMT_WEBP = "GIF（アニメ・256色）", "アニメWebP（透明OK・軽い）"
FMT_MP4, FMT_SHEET = "MP4（動画トリミング）", "一覧シート（1枚のPNG）"
FMT_MP3, FMT_M4A = "音声のみ（MP3）", "音声のみ（M4A）"
FMT_EXT = {FMT_PNG: "png", FMT_JPG: "jpg", FMT_GIF: "gif", FMT_WEBP: "webp", FMT_MP4: "mp4",
           FMT_SHEET: "sheet", FMT_MP3: "mp3", FMT_M4A: "m4a"}
FMT_ALL = list(FMT_EXT)
FMT_ALPHA = [FMT_PNG, FMT_WEBP, FMT_SHEET]  # 透明を保てる形式
FMT_TIMED = (FMT_MP4, FMT_GIF, FMT_WEBP, FMT_MP3, FMT_M4A)  # 速度・逆再生が使える形式

STEP_SCENE = "場面の切り替わりだけ"
STEPS = ["全コマ", "2コマに1枚", "5コマに1枚", "10コマに1枚", "30コマに1枚", STEP_SCENE]
SPEEDS = {"0.25倍": 0.25, "0.5倍": 0.5, "等速": 1.0, "2倍": 2.0, "4倍": 4.0}
TRIMS = {"しない": "none", "各コマごと（サイズが変わる）": "each", "全コマ共通（サイズ固定）": "union"}
CUT_METHODS = {"背景差分（カメラ固定の動画）": "diff", "単色背景（グリーンバックなど）": "chroma",
               "AI（rembg が必要）": "ai"}
COLS = ["自動", "2列", "3列", "4列", "5列", "6列", "8列", "10列"]
VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".wmv", ".webm", ".m4v", ".mts", ".m2ts", ".flv", ".ts")

# ---- URLからの動画取得 ----
DL_DIR = os.path.join(os.path.expanduser("~"), "Videos", "FrameExtractor")
BROWSERS = {"Chrome": "chrome", "Edge": "edge", "Firefox": "firefox", "Brave": "brave"}
CK_NONE = "ログイン情報を使わない"
CK_FILE = "cookies.txt を使う…"
QUALITIES = {"最高画質": None, "1080p まで": 1080, "720p まで": 720, "480p まで": 480}


def build_format(h, audio=True):
    f = f"[height<={h}]" if h else ""
    if audio:
        parts = [f"bestvideo{f}[vcodec^=avc1]+bestaudio[ext=m4a]", f"bestvideo{f}[ext=mp4]+bestaudio[ext=m4a]",
                 f"bestvideo{f}+bestaudio", f"best{f}[ext=mp4]", f"best{f}"]
    else:
        parts = [f"bestvideo{f}[vcodec^=avc1]", f"best{f}[vcodec^=avc1]", f"bestvideo{f}[ext=mp4]",
                 f"best{f}[ext=mp4]", f"bestvideo{f}", f"best{f}"]
    return "/".join(parts)


# ---- ブラウザの右クリックから保存（拡張機能 → このアプリ）----
EXT_PORT = 47815  # 拡張機能はここへ URL を送る（127.0.0.1 のみ）
PROTO = "frameextractor"  # アプリが起動していないときは frameextractor://download?url=... で起動される
EXT_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "FrameExtractor", "browser_extension")


def is_web_url(u):
    return isinstance(u, str) and u.lower().startswith(("http://", "https://")) and len(u) < 4096


def make_job(urls, referer=None):
    """拡張機能からの依頼。urls は保存を試す順の候補（最初に保存できたものを使う）"""
    urls = list(dict.fromkeys(u for u in urls if is_web_url(u)))[:6]
    if not urls:
        return None
    return {"urls": urls, "referer": referer if is_web_url(referer) else None}


def url_from_args(argv):
    """frameextractor://download?url=...&url=...&ref=... で起動されたときの依頼を取り出す"""
    for a in argv[1:]:
        if a.lower().startswith(PROTO + ":"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(a).query)
            return make_job(qs.get("url") or [], (qs.get("ref") or [None])[0])
    return None


def send_to_running(job):
    """すでに起動しているアプリへ依頼を渡す。渡せたら True"""
    req = urllib.request.Request(f"http://127.0.0.1:{EXT_PORT}/download", data=json.dumps(job).encode(),
                                 headers={"Content-Type": "application/json", "X-FrameExtractor": "1"})
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def register_protocol():
    """frameextractor:// でこのアプリが起動するように登録する（現在のユーザーのみ）"""
    if os.name != "nt":
        return
    try:
        import winreg
        if getattr(sys, "frozen", False):
            cmd = f'"{sys.executable}" "%1"'
        else:
            exe = sys.executable
            w = os.path.join(os.path.dirname(exe), "pythonw.exe")
            cmd = f'"{w if os.path.exists(w) else exe}" "{os.path.abspath(__file__)}" "%1"'
        base = "Software\\Classes\\" + PROTO
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "URL:動画コマ書き出し")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + "\\shell\\open\\command") as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, cmd)
    except Exception:
        pass


class _ExtServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = False  # Windowsで2つ目のアプリが同じポートを取らないように
    daemon_threads = True


class _ExtHandler(http.server.BaseHTTPRequestHandler):
    q = None  # App.q

    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/ping":
            self._reply(200, {"ok": True, "app": "FrameExtractor"})
        else:
            self._reply(404, {"ok": False})

    def do_POST(self):
        # 普通のWebページからは送れないようにする（独自ヘッダーはCORSで止まる・Originは拡張機能だけ許可）
        origin = self.headers.get("Origin", "")
        if (self.path != "/download" or self.headers.get("X-FrameExtractor") != "1"
                or (origin and not origin.startswith(("chrome-extension://", "moz-extension://", "extension://")))):
            self._reply(403, {"ok": False})
            return
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 65536)
            d = json.loads(self.rfile.read(n).decode("utf-8"))
            urls = d.get("urls") if isinstance(d.get("urls"), list) else [d.get("url")]
            job = make_job(urls, d.get("referer"))
        except Exception:
            job = None
        if not job:
            self._reply(400, {"ok": False, "error": "bad url"})
            return
        self.q.put(("ext_url", job))
        self._reply(200, {"ok": True})


# ---- 設定の記憶 ----
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".frame_extractor.json")


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def pick(c):
    """(ライト, ダーク) の色から、今のモードの色を選ぶ"""
    return c[1] if ctk.get_appearance_mode() == "Dark" else c[0]


class _Skip(Exception):
    """AIの結果待ちのとき、プレビューの合成を飛ばすための印"""


class QueueCb:
    """エンジンからの進捗を、画面スレッドへ渡す"""

    def __init__(self, app):
        self.app, self.t = app, 0.0

    def cancelled(self):
        return self.app.cancel

    def progress(self, frac, label, n=None):
        now = time.time()
        if now - self.t >= 0.05 or frac >= 1.0:
            self.t = now
            self.app.q.put(("pct", frac, label, n))


class App:
    def __init__(self, root, settings=None):
        self.root = root
        st = settings or {}
        root.title("動画コマ書き出し")
        root.after(250, self._set_icon)
        self.F = lambda size, bold=False: ctk.CTkFont(family=FONT_FAMILY, size=size,
                                                      weight="bold" if bold else "normal")
        root.geometry("1100x760")
        root.minsize(920, 640)
        root.configure(fg_color=BG)

        self.path, self.batch = None, []
        self.cap, self.meta = None, None
        self.fps, self.total, self.vw, self.vh = 30.0, 0, 0, 0
        self.pos = 0
        self.photo, self.img_box = None, None
        self.running = self.cancel = self.downloading = False
        self.q = queue.Queue()
        self._pending = self._timer = self._thr_timer = self._rs_timer = None
        self.bg, self.thr, self.sens = None, int(st.get("thr", 30)), int(st.get("sens", 5))
        self.last_frame = None
        self.crop, self._drag0 = None, None
        self.ai_ready = self.ai_loading = self.ai_busy = self.ai_pending = False
        self.ai_cache = {}
        self.cookie_file = None
        self.dl_queue = []  # 右クリックで送られてきた、順番待ちの依頼
        self.user_out = st.get("out_dir") if st.get("out_dir") and os.path.isdir(st.get("out_dir")) else None

        def choice(key, values, default):
            return st.get(key) if st.get(key) in values else default

        self.start_var = tk.StringVar(value="0")
        self.end_var = tk.StringVar(value="0")
        self.step_var = tk.StringVar(value=choice("step", STEPS, STEPS[0]))
        self.fmt_var = tk.StringVar(value=choice("fmt", FMT_ALL, FMT_PNG))
        self.res_var = tk.StringVar(value=choice("res", list(E.RESOLUTIONS), "元のサイズ"))
        self.size_var = tk.StringVar(value="")
        self.gif_var = tk.StringVar(value="")
        self.cut_var = tk.BooleanVar(value=False)
        self.cut_method_var = tk.StringVar(value=choice("cut_method", list(CUT_METHODS), list(CUT_METHODS)[0]))
        self.ai_model_var = tk.StringVar(value=choice("ai_model", list(E.AI_MODELS), list(E.AI_MODELS)[0]))
        self.trim_var = tk.StringVar(value=choice("trim", list(TRIMS), "しない"))
        self.thr_txt = tk.StringVar(value=str(self.thr))
        self.sens_txt = tk.StringVar(value=str(self.sens))
        self.crop_var = tk.BooleanVar(value=False)
        self.crop_txt = tk.StringVar(value="")
        self.audio_var = tk.BooleanVar(value=bool(st.get("audio", True)))
        self.speed_var = tk.StringVar(value=choice("speed", list(SPEEDS), "等速"))
        self.rev_var = tk.BooleanVar(value=False)
        self.cols_var = tk.StringVar(value=choice("cols", COLS, COLS[0]))
        self.q_var = tk.StringVar(value=choice("q", list(QUALITIES), "最高画質"))
        self.ck_var = tk.StringVar(value=choice("ck", [CK_NONE] + list(BROWSERS), CK_NONE))
        self.out_var = tk.StringVar(value=self.user_out or "")
        self.name_var = tk.StringVar(value="（未選択）")
        self.info_var = tk.StringVar(value="")
        self.time_var = tk.StringVar(value="")
        self.count_var = tk.StringVar(value="")
        self.msg_var = tk.StringVar(value="")
        self.batch_var = tk.StringVar(value="")

        self.build_ui()
        for v in (self.start_var, self.end_var, self.step_var, self.res_var, self.fmt_var, self.crop_var,
                  self.speed_var, self.audio_var, self.rev_var, self.cols_var):
            v.trace_add("write", lambda *a: self.update_count())
        for v in (self.fmt_var, self.step_var):
            v.trace_add("write", lambda *a: self.layout_dyn())
        self.cut_var.trace_add("write", lambda *a: self.on_cut_toggle())
        self.crop_var.trace_add("write", lambda *a: (self.draw_crop(), self.update_crop_text()))
        self.layout_dyn()
        self.update_count()
        if DND_OK:
            try:
                root.drop_target_register(DND_FILES, DND_TEXT)
                root.dnd_bind("<<Drop>>", self.on_drop)
            except Exception:
                pass
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start_ext_server()
        root.after(50, self.poll)

    # ---------- 部品 ----------
    def lbl(self, parent, text="", var=None, size=13, bold=False, sub=False, **kw):
        return ctk.CTkLabel(parent, text=text, textvariable=var, font=self.F(size, bold),
                            text_color=SUB if sub else INK, **kw)

    def btn(self, parent, text, cmd, primary=False, **kw):
        colors = (dict(fg_color=ACC, hover_color=ACC_H, text_color="#ffffff") if primary
                  else dict(fg_color=SEC, hover_color=SEC_H, text_color=INK))
        return ctk.CTkButton(parent, text=text, command=cmd, font=self.F(13, primary), corner_radius=10,
                             text_color_disabled=DIS, **colors, **kw)

    def menu(self, parent, var, values, width=250, command=None):
        return ctk.CTkOptionMenu(parent, variable=var, values=values, width=width, font=self.F(13), command=command,
                                 dropdown_font=self.F(13), fg_color=SEC, button_color=SEC_B,
                                 button_hover_color=SEC_H, text_color=INK, dropdown_fg_color=CARD,
                                 dropdown_text_color=INK, dropdown_hover_color=SEC, corner_radius=10)

    def entry(self, parent, var, width=80):
        return ctk.CTkEntry(parent, textvariable=var, width=width, font=self.F(13), fg_color=ENTRY,
                            border_color=LINE, text_color=INK, corner_radius=8)

    def switch(self, parent, text, var):
        return ctk.CTkSwitch(parent, text=text, variable=var, font=self.F(13), text_color=INK, progress_color=ACC,
                             button_color=("#ffffff", "#e8ecea"))

    def card(self, parent, title):
        c = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16)
        c.pack(fill="x", pady=(0, 10))
        self.lbl(c, text=title, size=14, bold=True).pack(anchor="w", padx=16, pady=(12, 6))
        return c

    def row(self, parent, pady=3):
        r = ctk.CTkFrame(parent, fg_color="transparent")
        r.pack(fill="x", padx=16, pady=pady)
        return r

    # ---------- 画面 ----------
    def build_ui(self):
        root = self.root
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(root, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=24, pady=(16, 2))
        self.lbl(head, text="動画コマ書き出し", size=22, bold=True).pack(side="left")
        self.lbl(head, text="  動画を画像・GIF・MP4・音声に", sub=True).pack(side="left", pady=(6, 0))
        seg = ctk.CTkSegmentedButton(head, values=["ライト", "ダーク"], font=self.F(12), command=self.on_mode,
                                     selected_color=ACC, selected_hover_color=ACC_H, unselected_color=SEC,
                                     unselected_hover_color=SEC_H, text_color=("#ffffff", "#ffffff"))
        seg.set("ダーク" if ctk.get_appearance_mode() == "Dark" else "ライト")
        seg.pack(side="right")

        # 左：動画とプレビュー
        left = ctk.CTkFrame(root, fg_color=CARD, corner_radius=16)
        left.grid(row=1, column=0, sticky="nsew", padx=(24, 10), pady=(10, 22))
        top = ctk.CTkFrame(left, fg_color="transparent")
        top.pack(fill="x", padx=18, pady=(16, 4))
        self.btn(top, "動画を選ぶ", self.pick_video, primary=True, width=130, height=36).pack(side="left")
        info = ctk.CTkFrame(top, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True, padx=14)
        self.lbl(info, var=self.name_var, bold=True, anchor="w").pack(fill="x")
        self.lbl(info, var=self.info_var, size=12, sub=True, anchor="w").pack(fill="x")

        ur = ctk.CTkFrame(left, fg_color="transparent")
        ur.pack(fill="x", padx=18, pady=(6, 0))
        self.url_entry = ctk.CTkEntry(ur, placeholder_text="動画のURLを貼り付け", font=self.F(13),
                                      fg_color=ENTRY, border_color=LINE, text_color=INK, corner_radius=8)
        self.url_entry.pack(side="left", fill="x", expand=True)
        self.url_entry.bind("<Return>", lambda e: self.start_download(False))
        self.btn(ur, "貼り付け", self.paste_url, width=70).pack(side="left", padx=(6, 0))
        ur2 = ctk.CTkFrame(left, fg_color="transparent")
        ur2.pack(fill="x", padx=18, pady=(6, 0))
        self.menu(ur2, self.q_var, list(QUALITIES), width=110).pack(side="left")
        self.dl_btn = self.btn(ur2, "取得", lambda: self.start_download(False), width=64)
        self.dl_btn.pack(side="left", padx=6)
        self.save_btn = self.btn(ur2, "MP4で保存…", lambda: self.start_download(True), primary=True, width=116)
        self.save_btn.pack(side="left")
        ur3 = ctk.CTkFrame(left, fg_color="transparent")
        ur3.pack(fill="x", padx=18, pady=(6, 0))
        self.lbl(ur3, text="ログイン", size=12, sub=True).pack(side="left", padx=(0, 8))
        self.menu(ur3, self.ck_var, [CK_NONE] + list(BROWSERS) + [CK_FILE], width=230,
                  command=self.on_cookie_change).pack(side="left")
        self.btn(ur3, "右クリック保存の設定…", self.setup_extension, width=150).pack(side="left", padx=(8, 0))
        self.lbl(left, size=11, sub=True, anchor="w", justify="left", wraplength=380,
                 text="「MP4で保存…」は保存先を選んで音声つきMP4で保存します。「取得」は既定のフォルダに保存します。"
                      "どちらも取得後にプレビューへ読み込まれます。ブラウザでYouTube・Xなどの動画を右クリックして"
                      "保存することもできます（「右クリック保存の設定…」から）。").pack(fill="x", padx=20, pady=(2, 0))

        nav = ctk.CTkFrame(left, fg_color="transparent")
        nav.pack(side="bottom", fill="x", padx=18, pady=(2, 16))
        self.btn(nav, "◀", lambda: self.step_pos(-1), width=44).pack(side="left")
        self.btn(nav, "▶", lambda: self.step_pos(1), width=44).pack(side="left", padx=6)
        self.btn(nav, "このコマを保存", self.save_current_frame, primary=True, width=120).pack(side="left", padx=6)
        self.lbl(nav, var=self.time_var, size=12, sub=True).pack(side="right")
        self.slider = ctk.CTkSlider(left, from_=0, to=1, command=self.on_slide,
                                    button_color=ACC, button_hover_color=ACC_H, progress_color=ACC)
        self.slider.set(0)
        self.slider.pack(side="bottom", fill="x", padx=22, pady=(8, 4))

        box = ctk.CTkFrame(left, fg_color=PREV_BG, corner_radius=12, width=320, height=180)
        box.pack(fill="both", expand=True, padx=18, pady=8)
        box.pack_propagate(False)
        self.canvas = tk.Canvas(box, highlightthickness=0, bd=0, bg=pick(PREV_BG), cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, padx=4, pady=4)
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

        # 右：設定（スクロール）＋ 書き出し（下に固定）
        right = ctk.CTkFrame(root, fg_color="transparent", width=440)
        right.grid(row=1, column=1, sticky="ns", padx=(10, 24), pady=(10, 22))
        right.pack_propagate(False)
        foot = ctk.CTkFrame(right, fg_color=CARD, corner_radius=16)
        foot.pack(side="bottom", fill="x", pady=(8, 0))
        sf = ctk.CTkScrollableFrame(right, fg_color="transparent")
        sf.pack(side="top", fill="both", expand=True)

        # --- 範囲 ---
        c = self.card(sf, "範囲")
        r = self.row(c)
        self.lbl(r, text="開始", width=36, anchor="w").pack(side="left")
        self.entry(r, self.start_var).pack(side="left")
        self.lbl(r, text="秒", sub=True).pack(side="left", padx=6)
        self.btn(r, "今の位置", self.set_start, width=84).pack(side="left", padx=4)
        r = self.row(c, pady=(3, 4))
        self.lbl(r, text="終了", width=36, anchor="w").pack(side="left")
        self.entry(r, self.end_var).pack(side="left")
        self.lbl(r, text="秒", sub=True).pack(side="left", padx=6)
        self.btn(r, "今の位置", self.set_end, width=84).pack(side="left", padx=4)
        self.btn(r, "最後まで", self.to_end, width=84).pack(side="left", padx=4)
        self.lbl(c, var=self.batch_var, size=11, sub=True, anchor="w", justify="left",
                 wraplength=380).pack(fill="x", padx=16, pady=(0, 10))

        # --- 出力設定 ---
        c = self.card(sf, "出力設定")
        r = self.row(c)
        self.lbl(r, text="形式", width=64, anchor="w").pack(side="left")
        self.fmt_cb = self.menu(r, self.fmt_var, FMT_ALL)
        self.fmt_cb.pack(side="left")
        r = self.row(c)
        self.lbl(r, text="画像サイズ", width=64, anchor="w").pack(side="left")
        self.menu(r, self.res_var, list(E.RESOLUTIONS)).pack(side="left")
        self.lbl(c, var=self.size_var, size=12, sub=True, anchor="w").pack(fill="x", padx=84)
        r = self.row(c)
        self.lbl(r, text="コマの選び方", width=64, anchor="w").pack(side="left")
        self.menu(r, self.step_var, STEPS).pack(side="left")

        # 形式などによって出し入れする項目
        self.dyn = ctk.CTkFrame(c, fg_color="transparent")
        self.dyn.pack(fill="x")
        self.g_scene = ctk.CTkFrame(self.dyn, fg_color="transparent")
        r = ctk.CTkFrame(self.g_scene, fg_color="transparent")
        r.pack(fill="x", padx=16, pady=2)
        self.lbl(r, text="切り替わりの感度", size=12).pack(side="left")
        self.lbl(r, var=self.sens_txt, size=12, bold=True).pack(side="right")
        sl = self.slider_w(self.g_scene, 1, 10, 9, self.on_sens)
        sl.set(self.sens)
        sl.pack(fill="x", padx=18)
        self.g_sheet = ctk.CTkFrame(self.dyn, fg_color="transparent")
        r = ctk.CTkFrame(self.g_sheet, fg_color="transparent")
        r.pack(fill="x", padx=16, pady=3)
        self.lbl(r, text="一覧の列数", width=64, anchor="w").pack(side="left")
        self.menu(r, self.cols_var, COLS, width=120).pack(side="left")
        self.g_anim = ctk.CTkFrame(self.dyn, fg_color="transparent")
        r = ctk.CTkFrame(self.g_anim, fg_color="transparent")
        r.pack(fill="x", padx=16, pady=3)
        self.lbl(r, text="速度", width=64, anchor="w").pack(side="left")
        self.menu(r, self.speed_var, list(SPEEDS), width=110).pack(side="left")
        self.switch(r, "逆再生", self.rev_var).pack(side="left", padx=14)
        self.g_audio = ctk.CTkFrame(self.dyn, fg_color="transparent")
        r = ctk.CTkFrame(self.g_audio, fg_color="transparent")
        r.pack(fill="x", padx=16, pady=3)
        self.switch(r, "音声を含める", self.audio_var).pack(side="left")

        # 画面の一部だけ
        r = self.row(c, pady=(10, 2))
        self.switch(r, "画面の一部だけ（プレビューをドラッグ）", self.crop_var).pack(side="left")
        r = self.row(c, pady=(0, 2))
        self.btn(r, "範囲をリセット", self.reset_crop, width=110).pack(side="left")
        self.lbl(r, var=self.crop_txt, size=12, sub=True).pack(side="left", padx=10)

        # 背景を透明に
        self.cut_chk = self.switch(c, "背景を透明にする", self.cut_var)
        self.cut_chk.pack(anchor="w", padx=16, pady=(12, 4))
        self.cut_opts = ctk.CTkFrame(c, fg_color=SUBCARD, corner_radius=12)
        r = ctk.CTkFrame(self.cut_opts, fg_color="transparent")
        r.pack(fill="x", padx=12, pady=(10, 2))
        self.menu(r, self.cut_method_var, list(CUT_METHODS), width=300, command=self.on_method_change).pack(side="left")
        self.method_row = r
        self.ai_row = ctk.CTkFrame(self.cut_opts, fg_color="transparent")
        self.lbl(self.ai_row, text="AIの速さ", size=12).pack(side="left")
        self.menu(self.ai_row, self.ai_model_var, list(E.AI_MODELS), width=250,
                  command=self.on_ai_model_change).pack(side="left", padx=8)
        r = ctk.CTkFrame(self.cut_opts, fg_color="transparent")
        r.pack(fill="x", padx=12, pady=(6, 0))
        self.lbl(r, text="切り抜きの強さ", size=12).pack(side="left")
        self.lbl(r, var=self.thr_txt, size=12, bold=True).pack(side="right")
        self.thr_scale = self.slider_w(self.cut_opts, 5, 100, 95, self.on_thr)
        self.thr_scale.set(self.thr)
        self.thr_scale.pack(fill="x", padx=14, pady=2)
        self.lbl(self.cut_opts, text="小さい＝広く残す／大きい＝背景を強く消す", size=11, sub=True,
                 anchor="w").pack(fill="x", padx=14)
        self.cut_btns = ctk.CTkFrame(self.cut_opts, fg_color="transparent")
        self.cut_btns.pack(fill="x", padx=12, pady=6)
        self.bg_btn1 = self.btn(self.cut_btns, "今の位置を背景に", self.set_bg_current, width=150)
        self.bg_btn2 = self.btn(self.cut_btns, "自動で推定し直す", self.start_bg_estimate, width=140)
        r = ctk.CTkFrame(self.cut_opts, fg_color="transparent")
        r.pack(fill="x", padx=12, pady=(0, 2))
        self.lbl(r, text="余白のトリミング", size=12).pack(side="left")
        self.menu(r, self.trim_var, list(TRIMS), width=220).pack(side="left", padx=8)
        self.cut_hint = self.lbl(self.cut_opts, size=11, sub=True, anchor="w", justify="left", wraplength=360, text="")
        self.cut_hint.pack(fill="x", padx=14, pady=(2, 10))
        self.refresh_cut_ui()

        self.lbl(c, var=self.count_var, bold=True, anchor="w").pack(fill="x", padx=16, pady=(10, 0))
        self.lbl(c, var=self.gif_var, size=11, sub=True, anchor="w", justify="left",
                 wraplength=380).pack(fill="x", padx=16, pady=(0, 12))

        # --- 保存先 + 書き出し（下に固定）---
        r = ctk.CTkFrame(foot, fg_color="transparent")
        r.pack(fill="x", padx=16, pady=(14, 6))
        self.btn(r, "保存先を選ぶ", self.pick_dir, width=110).pack(side="left")
        self.entry(r, self.out_var, width=100).pack(side="left", fill="x", expand=True, padx=(8, 0))
        r = ctk.CTkFrame(foot, fg_color="transparent")
        r.pack(fill="x", padx=16, pady=6)
        self.run_btn = self.btn(r, "書き出し開始", self.start_export, primary=True, height=42, state="disabled")
        self.run_btn.pack(side="left", fill="x", expand=True)
        self.stop_btn = self.btn(r, "中止", self.stop_export, width=70, height=42, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.bar = ctk.CTkProgressBar(foot, progress_color=ACC, height=8)
        self.bar.set(0)
        self.bar.pack(fill="x", padx=16, pady=(6, 2))
        self.lbl(foot, var=self.msg_var, size=12, sub=True, anchor="w", justify="left",
                 wraplength=390).pack(fill="x", padx=16, pady=(2, 12))

    def slider_w(self, parent, a, b, steps, cmd):
        return ctk.CTkSlider(parent, from_=a, to=b, number_of_steps=steps, command=cmd,
                             button_color=ACC, button_hover_color=ACC_H, progress_color=ACC)

    def layout_dyn(self):
        for g in (self.g_scene, self.g_sheet, self.g_anim, self.g_audio):
            g.pack_forget()
        f = self.fmt_var.get()
        if self.step_var.get() == STEP_SCENE:
            self.g_scene.pack(fill="x")
        if f == FMT_SHEET:
            self.g_sheet.pack(fill="x")
        if f in FMT_TIMED:
            self.g_anim.pack(fill="x")
        if f == FMT_MP4:
            self.g_audio.pack(fill="x")

    def on_mode(self, v):
        ctk.set_appearance_mode("dark" if v == "ダーク" else "light")
        self.root.after(50, self.render_preview)

    def _set_icon(self):
        try:
            self.root.iconbitmap(resource_path("app.ico"))
        except Exception:
            pass

    # ---------- 動画の読み込み ----------
    def pick_video(self):
        ps = filedialog.askopenfilenames(
            title="動画を選択（複数選ぶとまとめて処理できます）",
            filetypes=[("動画", "*.mp4 *.mov *.avi *.mkv *.wmv *.webm *.m4v *.mts *.m2ts *.flv *.ts"),
                       ("すべて", "*.*")])
        if ps:
            self.load_paths(list(ps))

    def on_drop(self, event):
        try:
            items = list(self.root.tk.splitlist(event.data))
        except Exception:
            return
        if items and items[0].lower().startswith(("http://", "https://")):
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, items[0])
            self.msg_var.set("URLを入れました。「取得」か「MP4で保存…」を押してください。")
            return
        files = [p for p in items if os.path.isfile(p)]
        vids = [p for p in files if p.lower().endswith(VIDEO_EXTS)] or files
        if vids and not self.running:
            self.load_paths(vids)

    def load_paths(self, paths):
        if self.load_video(paths[0]):
            self.batch = list(paths)
            self.refresh_batch()

    def refresh_batch(self):
        n = len(self.batch)
        if n > 1:
            self.batch_var.set(f"{n}本の動画が選ばれています。書き出しでは、各動画の全体を同じ設定でまとめて処理します"
                               "（範囲指定はプレビュー中の動画だけに使います）。")
            self.run_btn.configure(text=f"{n}本をまとめて書き出し")
        else:
            self.batch_var.set("")
            self.run_btn.configure(text="書き出し開始")
        self.update_count()

    def load_video(self, p):
        try:
            meta = E.probe(p)
        except Exception as e:
            messagebox.showerror("読み込めません", f"{e}\nMP4などの形式でお試しください。")
            return False
        cap = E.open_video(p)
        if cap is None:
            messagebox.showerror("読み込めません", "この動画は開けませんでした。")
            return False
        if self.cap:
            self.cap.release()
        self.cap, self.path, self.meta = cap, p, meta
        self.fps, self.total, self.vw, self.vh = meta.fps, meta.total, meta.vw, meta.vh
        base = os.path.splitext(os.path.basename(p))[0]
        nm = os.path.basename(p)
        self.name_var.set(nm if len(nm) <= 34 else nm[:16] + "…" + nm[-16:])
        self.info_var.set(f"{self.vw}x{self.vh} / {self.fps:.3f} fps / {self.total} コマ / {self.total / self.fps:.2f} 秒")
        steps = max(1, self.total - 1)
        self.slider.configure(from_=0, to=steps, number_of_steps=steps)
        self.slider.set(0)
        self.start_var.set("0")
        self.to_end()
        self.out_var.set(self.user_out or os.path.join(os.path.dirname(p), base + "_frames"))
        self.msg_var.set("")
        self.bar.set(0)
        self.bg, self.crop, self.batch = None, None, [p]
        self.ai_cache = {}
        self.refresh_batch()
        self.show_frame(0)
        if self.cut_var.get():
            self.prepare_bg()
        self.update_crop_text()
        self.update_run()
        return True

    # ---------- プレビュー ----------
    def show_frame(self, idx):
        if not self.cap:
            return
        idx = max(0, min(self.total - 1, idx))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = self.cap.read()
        if not ok:
            return
        self.pos, self.last_frame = idx, frame
        self.time_var.set(f"現在位置：{idx / self.fps:.2f} 秒（{idx} コマ目）")
        self.render_preview()

    def make_cutter(self):
        """プレビュー・1コマ保存用。まだ準備中なら None"""
        if not self.cut_var.get():
            return None
        m = CUT_METHODS[self.cut_method_var.get()]
        if m == "ai":
            return E.Cutter("ai", self.thr, None, self.ai_model()) if (E.ai_available() and self.ai_ready) else None
        return E.Cutter(m, self.thr, self.bg) if self.bg is not None else None

    def render_preview(self):
        c = self.canvas
        c.configure(bg=pick(PREV_BG))
        cw, ch = c.winfo_width(), c.winfo_height()
        frame = self.last_frame
        if frame is None:
            c.delete("all")
            if cw > 20:
                c.create_text(cw // 2, ch // 2, fill=pick(SUB), font=(FONT_FAMILY, 12),
                              text="動画を選ぶか、ここにドロップ" if DND_OK else "動画を選ぶと、ここにプレビューが出ます")
            return
        if cw < 40 or ch < 40:
            return
        h, w = frame.shape[:2]
        scale = min((cw - 8) / w, (ch - 8) / h)
        sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
        small = cv2.resize(frame, (sw, sh))
        cutter = self.make_cutter()
        if cutter:
            try:
                if cutter.method == "ai":
                    mask = self.ai_cache.get(self.pos)  # AIは時間がかかるので、裏で計算して、できたら表示する
                    if mask is None:
                        self.request_ai_mask()
                        raise _Skip()
                else:
                    mask = cutter.mask(frame)
                a = cv2.resize(mask, (sw, sh))[:, :, None].astype(np.float32) / 255.0
                yy, xx = np.indices((sh, sw))
                chk = np.repeat(np.where(((yy // 12 + xx // 12) % 2) == 0, 205, 150).astype(np.float32)[:, :, None], 3, 2)
                small = (small.astype(np.float32) * a + chk * (1.0 - a)).astype(np.uint8)
            except _Skip:
                pass
            except Exception as e:
                self.msg_var.set(f"プレビューの切り抜きに失敗：{e}")
        ok, buf = cv2.imencode(".png", small)
        self.photo = tk.PhotoImage(data=base64.b64encode(buf.tobytes()))
        ox, oy = (cw - sw) // 2, (ch - sh) // 2
        self.img_box = (ox, oy, sw, sh)
        c.delete("all")
        c.create_image(ox, oy, anchor="nw", image=self.photo)
        self.draw_crop()

    def on_canvas_resize(self, e):
        if self._rs_timer is None:
            self._rs_timer = self.root.after(120, self._apply_resize)

    def _apply_resize(self):
        self._rs_timer = None
        self.render_preview()

    def on_slide(self, val):
        if self.running:
            return
        self._pending = int(round(float(val)))
        if not self._timer:
            self._timer = self.root.after(40, self._apply_slide)

    def _apply_slide(self):
        self._timer = None
        if self._pending is not None:
            self.show_frame(self._pending)

    def step_pos(self, d):
        if not self.cap or self.running:
            return
        n = max(0, min(self.total - 1, self.pos + d))
        self.slider.set(n)
        self.show_frame(n)

    # ---------- 画面の一部だけ（ドラッグで範囲指定） ----------
    def _clamp(self, x, y):
        ox, oy, sw, sh = self.img_box
        return min(max(x, ox), ox + sw), min(max(y, oy), oy + sh)

    def on_press(self, e):
        if self.crop_var.get() and self.img_box and self.last_frame is not None:
            self._drag0 = self._clamp(e.x, e.y)

    def _set_crop(self, p0, p1):
        ox, oy, sw, sh = self.img_box
        x0, x1 = sorted((p0[0], p1[0]))
        y0, y1 = sorted((p0[1], p1[1]))
        self.crop = ((x0 - ox) / sw, (y0 - oy) / sh, (x1 - ox) / sw, (y1 - oy) / sh)

    def on_drag(self, e):
        if self._drag0 is not None:
            self._set_crop(self._drag0, self._clamp(e.x, e.y))
            self.draw_crop()

    def on_release(self, e):
        if self._drag0 is None:
            return
        p0, p1 = self._drag0, self._clamp(e.x, e.y)
        self._drag0 = None
        if abs(p1[0] - p0[0]) < 8 or abs(p1[1] - p0[1]) < 8:
            self.crop = None  # クリックだけなら解除
        else:
            self._set_crop(p0, p1)
        self.draw_crop()
        self.update_crop_text()
        self.update_count()

    def draw_crop(self):
        c = self.canvas
        c.delete("crop")
        if not (self.crop_var.get() and self.crop and self.img_box):
            return
        ox, oy, sw, sh = self.img_box
        X0, Y0, X1, Y1 = ox + self.crop[0] * sw, oy + self.crop[1] * sh, ox + self.crop[2] * sw, oy + self.crop[3] * sh
        for r in ((ox, oy, ox + sw, Y0), (ox, Y1, ox + sw, oy + sh), (ox, Y0, X0, Y1), (X1, Y0, ox + sw, Y1)):
            c.create_rectangle(*r, fill="black", stipple="gray50", outline="", tags="crop")
        c.create_rectangle(X0, Y0, X1, Y1, outline="#4fd1a5", width=2, tags="crop")

    def reset_crop(self):
        self.crop = None
        self.draw_crop()
        self.update_crop_text()
        self.update_count()

    def current_crop(self):
        return self.crop if (self.crop_var.get() and self.crop) else None

    def update_crop_text(self):
        if self.crop and self.meta:
            w, h = E.region_size(self.meta, self.crop)
            self.crop_txt.set(f"切り取り：{w}×{h}")
        else:
            self.crop_txt.set("範囲を指定していません" if self.crop_var.get() else "")

    # ---------- 範囲 ----------
    def set_start(self):
        if self.cap:
            self.start_var.set(f"{self.pos / self.fps:.2f}")

    def set_end(self):
        if self.cap:
            self.end_var.set(f"{self.pos / self.fps:.2f}")

    def to_end(self):
        if self.cap:
            self.end_var.set(f"{self.total / self.fps:.2f}")

    def get_range(self):
        try:
            s, e = float(self.start_var.get()), float(self.end_var.get())
        except ValueError:
            return None
        return max(0, int(round(s * self.fps))), min(self.total, int(round(e * self.fps)))

    def step_value(self):
        t = self.step_var.get()
        return 1 if t in ("全コマ", STEP_SCENE) else int(t.split("コマ")[0])

    def cols_value(self):
        t = self.cols_var.get()
        return 0 if t == "自動" else int(t[:-1])

    # ---------- 背景の切り抜き ----------
    def refresh_cut_ui(self):
        m = CUT_METHODS[self.cut_method_var.get()]
        self.bg_btn1.pack_forget()
        self.bg_btn2.pack_forget()
        self.ai_row.pack_forget()
        if m == "diff":
            self.bg_btn1.configure(text="今の位置を背景に")
            self.bg_btn1.pack(side="left")
            self.bg_btn2.pack(side="left", padx=6)
            self.cut_hint.configure(text="カメラが動かない動画向けです。背景は自動で推定します。うまく切れないときは、"
                                         "キャラがいないコマで「今の位置を背景に」を押してください。市松模様の部分が透明になります。")
        elif m == "chroma":
            self.bg_btn1.configure(text="今のコマの周囲の色を背景に")
            self.bg_btn1.pack(side="left")
            self.cut_hint.configure(text="コマの外周の色を背景として抜きます。グリーンバックや単色の背景向けです。")
        else:
            self.ai_row.pack(fill="x", padx=12, pady=(4, 0), after=self.method_row)
            self.cut_hint.configure(text=("AIで人物・キャラクターを切り抜きます。カメラが動く動画にも使えますが、時間がかかります。"
                                          "遅いときは「AIの速さ」を高速にするか、GPU版（install_ai_gpu.bat）を試してください。"
                                          "初回はモデルのダウンロードが入ります。" if E.ai_available() else
                                          "rembg が入っていません。pip install \"rembg[cpu]\" で入れてから、ソフトを再起動してください"
                                          "（exe版は build_ai.bat で作ると、AI入りになります）。"))

    def on_method_change(self, v=None):
        self.bg = None
        self.ai_cache = {}
        self.refresh_cut_ui()
        self.prepare_bg()
        self.render_preview()
        self.update_run()

    def on_cut_toggle(self):
        if self.cut_var.get():
            self.cut_opts.pack(fill="x", padx=12, pady=(0, 6), after=self.cut_chk)
            self.fmt_cb.configure(values=FMT_ALPHA)
            if self.fmt_var.get() not in FMT_ALPHA:
                self.fmt_var.set(FMT_PNG)
            self.prepare_bg()
        else:
            self.cut_opts.pack_forget()
            self.fmt_cb.configure(values=FMT_ALL)
            self.msg_var.set("")
        self.render_preview()
        self.update_run()

    def prepare_bg(self):
        """切り抜き方法に合わせて、背景を用意する"""
        if not self.cap:
            return
        m = CUT_METHODS[self.cut_method_var.get()]
        if m == "diff":
            self.start_bg_estimate()
        elif m == "chroma" and self.last_frame is not None:
            self.set_bg_current()
        elif m == "ai":
            self.start_ai_prepare()

    def ai_model(self):
        return E.AI_MODELS[self.ai_model_var.get()]

    def on_ai_model_change(self, v=None):
        self.ai_cache = {}
        self.ai_ready = False
        if self.ai_loading:
            self.ai_pending = True  # 準備中のモデルが終わったら、選び直したモデルを準備する
        else:
            self.start_ai_prepare()
        self.render_preview()
        self.update_run()

    def start_ai_prepare(self):
        """AIモデルの準備（初回はダウンロード）を、裏で行う"""
        if self.ai_ready or self.ai_loading or not E.ai_available():
            return
        self.ai_loading = True
        model = self.ai_model()
        self.msg_var.set("AIモデルを準備中…（初回はダウンロードで数分かかります。そのままお待ちください）")
        self.update_run()

        def job():
            try:
                E.ai_prepare(model)
                self.q.put(("ai_ready", True, E.ai_device(model), model))
            except Exception as e:
                self.q.put(("ai_ready", False, str(e), model))

        threading.Thread(target=job, daemon=True).start()

    def request_ai_mask(self):
        """今のコマのAI切り抜きを、裏で計算する"""
        if self.ai_busy or self.last_frame is None:
            return
        self.ai_busy = True
        idx, frame, model = self.pos, self.last_frame, self.ai_model()
        self.msg_var.set("AIで切り抜き中…")

        def job():
            try:
                h, w = frame.shape[:2]
                self.q.put(("ai_mask", idx, E.ai_mask(frame, E.ai_wsize(w, h), model), ""))
            except Exception as e:
                self.q.put(("ai_mask", idx, None, str(e)))

        threading.Thread(target=job, daemon=True).start()

    def on_thr(self, val):
        self.thr = int(float(val))
        self.thr_txt.set(str(self.thr))
        if self._thr_timer is None:
            self._thr_timer = self.root.after(80, self._apply_thr)

    def _apply_thr(self):
        self._thr_timer = None
        self.render_preview()

    def on_sens(self, val):
        self.sens = int(float(val))
        self.sens_txt.set(str(self.sens))

    def start_bg_estimate(self):
        if not self.cap:
            return
        self.bg = None
        self.msg_var.set("背景を推定中…（数秒かかります）")
        self.update_run()
        path, total = self.path, self.total
        wsize = E.work_size(self.vw, self.vh)

        def job():
            try:
                bg = E.estimate_background(path, total, wsize)
            except Exception:
                bg = None
            self.q.put(("bg", bg, path))

        threading.Thread(target=job, daemon=True).start()

    def set_bg_current(self):
        if self.last_frame is None:
            return
        wsize = E.work_size(self.vw, self.vh)
        if CUT_METHODS[self.cut_method_var.get()] == "chroma":
            self.bg = E.solid_bg(E.border_color(self.last_frame), wsize)
        else:
            self.bg = cv2.resize(self.last_frame, wsize, interpolation=cv2.INTER_AREA)
        self.msg_var.set("背景を設定しました。")
        self.render_preview()
        self.update_run()

    # ---------- 表示の更新 ----------
    def update_count(self):
        n_files = len(self.batch)
        out_txt = ""
        if self.cap:
            cw, ch = E.region_size(self.meta, self.current_crop())
            w, h = E.calc_size(cw, ch, E.RESOLUTIONS[self.res_var.get()])
            self.size_var.set(f"→ {w}×{h}")
        f = self.fmt_var.get()
        r = self.get_range() if self.cap else None
        step = self.step_value()
        scene = self.step_var.get() == STEP_SCENE
        if n_files > 1:
            out_txt = f"{n_files}本をまとめて書き出します（各動画の全体）"
        elif not r or r[1] <= r[0]:
            out_txt = ""
        else:
            n = -(-(r[1] - r[0]) // step)
            dur = (r[1] - r[0]) / self.fps / SPEEDS[self.speed_var.get()]
            if scene and f in FMT_ALPHA + [FMT_JPG]:
                out_txt = "書き出す枚数：場面を検出して決まります"
            elif f in (FMT_MP4, FMT_MP3, FMT_M4A):
                out_txt = f"書き出す長さ：約 {dur:.2f} 秒"
            elif f == FMT_SHEET:
                out_txt = f"一覧シート1枚（{n} コマ）"
            elif f in (FMT_GIF, FMT_WEBP):
                out_txt = f"アニメ1つ（{n} コマ・約 {dur:.2f} 秒）"
            else:
                out_txt = f"書き出す枚数：{n} 枚"
        self.count_var.set(out_txt)
        hints = {
            FMT_GIF: "GIFは256色までで、透明は使えません。サイズを小さくして間引くと軽くなります。",
            FMT_WEBP: "透明に対応していて、GIFよりきれいで軽く作れます。",
            FMT_MP4: "H.264 / AAC で書き出します。再エンコードするため少し時間がかかります。",
            FMT_SHEET: "選んだコマを格子状に並べた1枚の画像にします（スプライトシートにも使えます）。",
            FMT_MP3: "選んだ範囲の音声だけを保存します。",
            FMT_M4A: "選んだ範囲の音声だけを保存します。",
        }
        hint = hints.get(f, "")
        if f == FMT_GIF and self.cap and 1000 * step / self.fps / SPEEDS[self.speed_var.get()] < 20:
            hint += "　※速すぎるため実際より遅く再生されます。"
        self.gif_var.set(hint)
        self.update_run()

    def cut_ready(self):
        if not self.cut_var.get():
            return True
        m = CUT_METHODS[self.cut_method_var.get()]
        if m == "ai":
            return E.ai_available() and self.ai_ready
        return len(self.batch) > 1 or self.bg is not None

    def update_run(self):
        r = self.get_range() if self.cap else None
        ok = bool(self.cap and (len(self.batch) > 1 or (r and r[1] > r[0])) and self.out_var.get()
                  and not self.running and not self.downloading and self.cut_ready())
        self.run_btn.configure(state="normal" if ok else "disabled")

    # ---------- 書き出し ----------
    def pick_dir(self):
        d = filedialog.askdirectory(title="保存先フォルダを選択")
        if d:
            self.out_var.set(d)
            self.user_out = d
            self.update_run()

    def build_options(self):
        cut = None
        if self.cut_var.get():
            bg = None if len(self.batch) > 1 else self.bg
            cut = E.CutSpec(CUT_METHODS[self.cut_method_var.get()], self.thr, bg, self.ai_model())
        return SimpleNamespace(
            ext=FMT_EXT[self.fmt_var.get()], step=self.step_value(), scene=self.step_var.get() == STEP_SCENE,
            sens=self.sens, target=E.RESOLUTIONS[self.res_var.get()], crop=self.current_crop(), cut=cut,
            trim=TRIMS[self.trim_var.get()] if cut else "none", audio=self.audio_var.get(),
            speed=SPEEDS[self.speed_var.get()], reverse=self.rev_var.get(), cols=self.cols_value())

    def start_export(self):
        paths = list(self.batch) if len(self.batch) > 1 else [self.path]
        o = self.build_options()
        out = self.out_var.get()
        ranges = None
        if len(paths) == 1:
            r = self.get_range()
            if not r or r[1] <= r[0]:
                messagebox.showerror("範囲エラー", "開始と終了の範囲が正しくありません。")
                return
            ranges = [r]
        try:
            os.makedirs(out, exist_ok=True)
        except OSError as e:
            messagebox.showerror("保存先エラー", f"保存先フォルダを作れません。\n{e}")
            return
        need_ff = o.ext in ("mp3", "m4a") or (o.ext == "mp4" and (o.audio or o.reverse))
        if need_ff and not E.FFMPEG:
            messagebox.showerror("ffmpeg が必要です",
                                 "ffmpeg が見つかりません。\nコマンドプロンプトで\n  pip install imageio-ffmpeg\nを実行してください。\n"
                                 "（exe版は build.bat で作り直してください）")
            return
        if o.scene and o.ext in ("gif", "webp", "mp4", "mp3", "m4a"):
            messagebox.showinfo("場面の検出", "「場面の切り替わりだけ」は、画像か一覧シートの書き出し用です。\n"
                                            "コマの選び方を変えてください。")
            return
        if o.ext in ("gif", "webp") and not E.PIL_OK:
            messagebox.showerror("書き出せません", "Pillow が入っていません。\npip install pillow を実行してください。")
            return
        if o.cut and o.cut.method == "ai" and not E.ai_available():
            messagebox.showerror("AIを使えません", "rembg が入っていません。\npip install \"rembg[cpu]\" を実行してください。")
            return
        if ranges and not o.scene and o.ext in ("gif", "webp", "sheet"):
            n = -(-(ranges[0][1] - ranges[0][0]) // o.step)
            cw, ch = E.region_size(self.meta, o.crop)
            w, h = E.calc_size(cw, ch, o.target)
            if n * w * h > 250_000_000 and not messagebox.askyesno(
                    "確認", f"{n} コマ（{w}×{h}）は、とても大きくなり、作成に時間とメモリがかかります。\n続けますか？\n"
                            "（画像サイズを小さくするか、間引きを増やすと軽くなります）"):
                return
        self.running, self.cancel = True, False
        self.bar.set(0)
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        threading.Thread(target=self.export_worker, args=(paths, ranges, o, out), daemon=True).start()

    def stop_export(self):
        self.cancel = True

    def export_worker(self, paths, ranges, o, out):
        try:
            total, errors = E.run_batch(paths, ranges, o, out, QueueCb(self))
            self.q.put(("done", total, self.cancel, o.ext, errors, len(paths)))
        except Exception as e:
            self.q.put(("error", str(e)))

    def save_current_frame(self):
        if self.last_frame is None or not self.out_var.get():
            self.msg_var.set("先に動画を選んでください。")
            return
        o = self.build_options()
        o.cutter = self.make_cutter()
        if self.cut_var.get() and o.cutter is None:
            self.msg_var.set("切り抜きの準備中です。少し待ってからもう一度押してください。")
            return
        try:
            dest = E.save_single(self.last_frame, self.pos, self.total, o, self.out_var.get(),
                                 os.path.splitext(os.path.basename(self.path))[0])
            self.msg_var.set(f"1コマ保存しました：{dest}")
        except Exception as e:
            messagebox.showerror("保存できません", str(e))

    # ---------- URLから取得 ----------
    def paste_url(self):
        try:
            t = self.root.clipboard_get().strip()
        except Exception:
            return
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, t)

    def on_cookie_change(self, v):
        if v == CK_FILE:
            p = filedialog.askopenfilename(title="cookies.txt を選択",
                                           filetypes=[("cookies.txt", "*.txt"), ("すべて", "*.*")])
            if p:
                self.cookie_file = p
                self.msg_var.set(f"cookies.txt を設定しました：{os.path.basename(p)}")
            else:
                self.ck_var.set(CK_NONE)
        elif v in BROWSERS:
            self.msg_var.set(f"{v} のログイン状態を使います。取得に失敗するときは、{v} を完全に閉じてからお試しください。")
        else:
            self.msg_var.set("")

    def start_download(self, ask_folder=False, job=None):
        if job:
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, job["urls"][0])
        url = self.url_entry.get().strip()
        if not url or self.downloading or self.running:
            return
        dest = DL_DIR
        if ask_folder:
            dest = filedialog.askdirectory(title="動画の保存先フォルダを選択")
            if not dest:
                return
        if MISSAV.is_missav_url(url):
            if not MISSAV.CFFI_OK:
                messagebox.showerror("取得できません",
                                     "curl-cffi が入っていません。\nコマンドプロンプトで\n  pip install curl-cffi\nを実行してください。\n"
                                     "（exe版は build.bat で作り直してください）")
                return
        elif not YT_OK:
            messagebox.showerror("取得できません",
                                 "yt-dlp が入っていません。\nコマンドプロンプトで\n  pip install yt-dlp\nを実行してください。\n"
                                 "（exe版は build.bat で作り直してください）")
            return
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as e:
            messagebox.showerror("保存先エラー", f"動画の保存フォルダを作れません。\n{e}")
            return
        ck, cookie = self.ck_var.get(), None
        if ck in BROWSERS:
            cookie = ("browser", BROWSERS[ck])
        elif ck == CK_FILE:
            if not self.cookie_file or not os.path.exists(self.cookie_file):
                messagebox.showinfo("cookies.txt", "cookies.txt が選ばれていません。もう一度「ログイン」から選んでください。")
                return
            cookie = ("file", self.cookie_file)
        self.downloading, self.cancel = True, False
        self.dl_btn.configure(state="disabled")
        self.save_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.bar.set(0)
        self.msg_var.set("動画の情報を取得中…")
        self.update_run()
        urls = job["urls"] if job and job["urls"][0] == url else [url]
        referer = job.get("referer") if job else None
        threading.Thread(target=self.dl_worker, args=(urls, QUALITIES[self.q_var.get()], dest, cookie, referer),
                         daemon=True).start()

    def dl_worker(self, urls, height, dest, cookie=None, referer=None):
        def hook(d):
            if self.cancel:
                raise RuntimeError("cancelled")
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                self.q.put(("dl", d.get("downloaded_bytes") or 0, total, d.get("speed")))
            elif d.get("status") == "finished":
                self.q.put(("dlmsg", "仕上げ中…（映像と音声を結合）"))

        t0 = time.time()
        first_err = None
        # MissAV は yt-dlp 非対応のため専用処理。失敗したら残りの候補を通常の取得で試す
        missavs = [u for u in urls if MISSAV.is_missav_url(u)]
        if missavs:
            try:
                path = self._run_missav(missavs, height, dest, cookie, t0)
                self.q.put(("dl_done", path))
                return
            except MISSAV.Cancelled:
                self.q.put(("dl_cancel",))
                return
            except Exception as e:
                first_err = e
                urls = [u for u in urls if not MISSAV.is_missav_url(u)]
                if not urls:
                    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(first_err)).strip()
                    self.q.put(("dl_error", msg[:400]))
                    return
        audio = bool(E.FFMPEG)
        opts = {
            "format": build_format(height, audio),
            "outtmpl": os.path.join(dest, "%(title).80s [%(id)s].%(ext)s"),
            "noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True,
            "no_color": True, "progress_hooks": [hook], "restrictfilenames": False, "windowsfilenames": True,
        }
        if cookie and cookie[0] == "browser":
            opts["cookiesfrombrowser"] = (cookie[1],)
        elif cookie and cookie[0] == "file":
            opts["cookiefile"] = cookie[1]
        if audio:
            opts["merge_output_format"] = "mp4"
            opts["ffmpeg_location"] = E.FFMPEG
        if referer:
            opts["http_headers"] = {"Referer": referer}  # 動画ファイルを直接取るとき、元のページからと伝える
        for i, url in enumerate(urls):  # 右クリックからは候補が複数届く。保存できるまで順に試す
            if i:
                self.q.put(("dlmsg", f"別の方法で取得を試しています…（{i + 1}/{len(urls)}）"))
            try:
                path = self._dl_one(url, opts, dest, t0)
                self.q.put(("dl_done", path))
                return
            except Exception as e:
                if self.cancel:
                    self.q.put(("dl_cancel",))
                    return
                first_err = first_err or e
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(first_err)).strip()
        self.q.put(("dl_error", msg[:400]))

    def _run_missav(self, urls, height, dest, cookie, t0):
        """MissAV の候補を順に試して保存先パスを返す。"""
        def progress(downloaded, total):
            el = max(0.1, time.time() - t0)
            self.q.put(("dl", downloaded, total, downloaded / el))

        first_err = None
        for i, url in enumerate(urls):
            if i:
                self.q.put(("dlmsg", f"別の方法で取得を試しています…（{i + 1}/{len(urls)}）"))
            try:
                return MISSAV.download(
                    url, dest, max_height=height, progress_cb=progress,
                    cancel_cb=lambda: self.cancel, msg_cb=lambda m: self.q.put(("dlmsg", m)),
                    ffmpeg=E.FFMPEG, cookie=cookie)
            except MISSAV.Cancelled:
                raise
            except Exception as e:
                first_err = first_err or e
        raise first_err

    def _dl_one(self, url, opts, dest, t0):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and info.get("entries"):
                info = next((e for e in info["entries"] if e), None)
            path = None
            if info:
                rd = info.get("requested_downloads")
                path = rd[0].get("filepath") if rd else ydl.prepare_filename(info)
        if not path or not os.path.exists(path):
            cands = [os.path.join(dest, f) for f in os.listdir(dest)
                     if f.lower().endswith((".mp4", ".mkv", ".webm", ".mov"))]
            cands = [f for f in cands if os.path.getmtime(f) >= t0 - 2]
            if cands:
                path = max(cands, key=os.path.getmtime)
        if not path or not os.path.exists(path):
            raise RuntimeError("保存されたファイルが見つかりませんでした")
        return path

    def end_download(self):
        self.downloading = False
        self.dl_btn.configure(state="normal")
        self.save_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.update_run()
        self.root.after(300, self.next_queued)

    # ---------- ブラウザの右クリックから ----------
    def start_ext_server(self):
        _ExtHandler.q = self.q
        try:
            self.ext_srv = _ExtServer(("127.0.0.1", EXT_PORT), _ExtHandler)
        except OSError:
            self.ext_srv = None  # 別のアプリ（または2つ目の起動）がポートを使っている
            return
        threading.Thread(target=self.ext_srv.serve_forever, daemon=True).start()

    def enqueue_url(self, job):
        """拡張機能から届いた依頼を、既定のフォルダへ保存する（取得中なら順番待ち）"""
        if self.downloading or self.running:
            self.dl_queue.append(job)
            if self.downloading:
                self.msg_var.set(f"順番待ちに追加しました（あと {len(self.dl_queue)} 件）")
            return
        self.start_download(job=job)

    def next_queued(self):
        if self.dl_queue and not (self.downloading or self.running):
            self.start_download(job=self.dl_queue.pop(0))

    def setup_extension(self):
        src = resource_path("browser_extension")
        if not os.path.isdir(src):
            messagebox.showerror("見つかりません", "browser_extension フォルダが見つかりません。\n"
                                                 "frame_extractor.py と同じ場所に置いてから、exe を作り直してください。")
            return
        try:
            shutil.copytree(src, EXT_DIR, dirs_exist_ok=True)
        except Exception as e:
            messagebox.showerror("コピーできません", f"拡張機能をコピーできませんでした。\n{e}")
            return
        register_protocol()
        self.root.clipboard_clear()
        self.root.clipboard_append("chrome://extensions")
        messagebox.showinfo(
            "右クリックで動画を保存",
            "ブラウザに拡張機能を入れると、YouTube・X（Twitter）・TikTok・ニコニコ動画など、いろいろなサイトの動画を"
            "右クリック →「動画を保存」でこのアプリに保存できます（Chrome / Edge / Brave）。\n\n"
            "1. ブラウザのアドレス欄に chrome://extensions を貼り付けて開く\n"
            "　（コピー済みです。Edge は edge://extensions）\n"
            "2. 「デベロッパーモード」をオンにする\n"
            "3. 「パッケージ化されていない拡張機能を読み込む」を押し、\n"
            "　 このあと開くフォルダ（browser_extension）を選ぶ\n"
            "　（入れ済みのときは、拡張機能の「更新（↻）」を押すだけで新しくなります）\n\n"
            f"・保存先：{DL_DIR}\n"
            "・保存後はこのアプリのプレビューに読み込まれます。アプリを閉じていても自動で起動します"
            "（初回だけブラウザが「開きますか？」と聞くので、許可してください）。\n"
            "・右クリックでサイト独自のメニューが出るとき（YouTube の再生中の動画など）は、"
            "もう一度右クリックするか、動画にマウスを乗せて Alt+Shift+S を押してください。\n"
            "・画質とログインは、このアプリで選んでいるものを使います。ログインが必要なサイトは"
            "「ログイン」でブラウザを選んでください。")
        try:
            os.startfile(EXT_DIR)
        except Exception:
            pass

    # ---------- 進捗の受け取り ----------
    def poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                k = m[0]
                if k == "pct":
                    self.bar.set(min(1.0, m[1]))
                    cnt = f" {m[3]} 枚目" if m[3] and self.fmt_var.get() in (FMT_PNG, FMT_JPG) else ""
                    self.msg_var.set(f"{m[2]}…{cnt} {m[1] * 100:.0f}%")
                elif k == "done":
                    self.finish_export(m)
                elif k == "error":
                    self.end_export()
                    self.msg_var.set(f"エラー：{m[1]}")
                    messagebox.showerror("書き出せませんでした", m[1])
                elif k == "dl":
                    pct = f"{m[1] / m[2] * 100:.0f}%" if m[2] else f"{m[1] / 1048576:.1f} MB"
                    spd = f"（{m[3] / 1048576:.1f} MB/秒）" if m[3] else ""
                    if m[2]:
                        self.bar.set(min(1.0, m[1] / m[2]))
                    wait = f"　順番待ち {len(self.dl_queue)} 件" if self.dl_queue else ""
                    self.msg_var.set(f"ダウンロード中… {pct} {spd}{wait}")
                elif k == "dlmsg":
                    self.msg_var.set(m[1])
                elif k == "dl_done":
                    self.end_download()
                    self.load_paths([m[1]])
                    self.msg_var.set(f"保存しました：{m[1]}")
                    self.bar.set(0)
                elif k == "ext_url":
                    self.enqueue_url(m[1])
                elif k == "dl_cancel":
                    self.dl_queue.clear()
                    self.end_download()
                    self.msg_var.set("取得を中止しました。")
                    self.bar.set(0)
                elif k == "dl_error":
                    self.end_download()
                    self.msg_var.set("取得できませんでした。")
                    self.bar.set(0)
                    extra = ("\n\n・ログイン情報を読み取れませんでした。ブラウザを完全に閉じてからもう一度試すか、"
                             "Firefox を選ぶか、cookies.txt を使ってください。" if "cookie" in m[1].lower() else "")
                    messagebox.showerror("取得できませんでした",
                                         m[1] + extra + "\n\n・URLが正しいか確認してください。\n"
                                         "・ログインが必要な動画や、取得できないサイトもあります。\n"
                                         "・サイトの仕様変更で失敗することがあります。build.bat で作り直すと最新の取得部品に更新されます。")
                elif k == "ai_ready":
                    self.ai_loading = False
                    if m[1]:
                        if m[3] == self.ai_model():
                            self.ai_ready = True
                            self.msg_var.set(f"AIの準備ができました（{m[2]}で処理します）。")
                            self.render_preview()
                    else:
                        self.msg_var.set("AIを準備できませんでした。")
                        messagebox.showerror("AIを準備できませんでした",
                                             m[2][:300] + "\n\n・インターネットに接続されているか確認してください（初回はモデルのダウンロードが入ります）。")
                    if self.ai_pending or (m[1] and m[3] != self.ai_model()):
                        self.ai_pending = False
                        self.start_ai_prepare()
                    self.update_run()
                elif k == "ai_mask":
                    self.ai_busy = False
                    if m[2] is not None:
                        if len(self.ai_cache) >= 8:
                            self.ai_cache.pop(next(iter(self.ai_cache)))
                        self.ai_cache[m[1]] = m[2]
                        self.msg_var.set("")
                    else:
                        self.msg_var.set(f"AIの切り抜きに失敗：{m[3][:200]}")
                    if m[2] is not None:
                        self.render_preview()
                elif k == "bg":
                    if m[2] == self.path:
                        self.bg = m[1]
                        self.msg_var.set("背景を推定しました。" if m[1] is not None else "背景を推定できませんでした。")
                        self.render_preview()
                        self.update_run()
        except queue.Empty:
            pass
        self.root.after(50, self.poll)

    def end_export(self):
        self.running = False
        self.stop_btn.configure(state="disabled")
        self.update_run()
        self.root.after(300, self.next_queued)

    def finish_export(self, m):
        _, n, cancelled, ext, errors, N = m
        self.end_export()
        names = {"png": f"{n} 枚", "jpg": f"{n} 枚", "sheet": f"一覧シート {n} 枚", "gif": f"GIF {n} 件",
                 "webp": f"WebP {n} 件", "mp4": f"MP4 {n} 件", "mp3": f"MP3 {n} 件", "m4a": f"M4A {n} 件"}
        text = names.get(ext, f"{n} 件") + "を保存しました。"
        if cancelled:
            self.msg_var.set("中止しました。")
            return
        if errors and n == 0:
            self.msg_var.set("書き出せませんでした。")
            messagebox.showerror("書き出せませんでした", "\n".join(f"・{x}：{y}" for x, y in errors[:5]))
            return
        self.bar.set(1.0)
        if errors:
            self.msg_var.set(f"完了（{len(errors)} 本は失敗）：{text}")
            messagebox.showwarning("一部の動画で失敗しました",
                                   "\n".join(f"・{a}：{b}" for a, b in errors[:5]))
        else:
            self.msg_var.set("完了：" + text)
        if messagebox.askyesno("完了", text + "\n保存先フォルダを開きますか？"):
            try:
                os.startfile(self.out_var.get())
            except Exception:
                pass

    # ---------- 終了 ----------
    def save_settings(self):
        d = {"fmt": self.fmt_var.get(), "res": self.res_var.get(), "step": self.step_var.get(),
             "q": self.q_var.get(), "ck": self.ck_var.get() if self.ck_var.get() != CK_FILE else CK_NONE,
             "speed": self.speed_var.get(), "cols": self.cols_var.get(), "thr": self.thr, "sens": self.sens,
             "cut_method": self.cut_method_var.get(), "ai_model": self.ai_model_var.get(), "trim": self.trim_var.get(), "audio": self.audio_var.get(),
             "out_dir": self.user_out or "", "mode": "dark" if ctk.get_appearance_mode() == "Dark" else "light"}
        if d["fmt"] not in FMT_ALL:
            d["fmt"] = FMT_PNG
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def on_close(self):
        self.cancel = True
        self.save_settings()
        if self.cap:
            self.cap.release()
        self.root.destroy()


if __name__ == "__main__":
    if "--check-ai" in sys.argv:  # build_ai.bat が、exe の中にAIが入ったかを確かめるために使う
        E.check_ai_to_file()
        sys.exit(0)
    ext_job = url_from_args(sys.argv)  # ブラウザの右クリックから起動されたとき
    if ext_job and send_to_running(ext_job):
        sys.exit(0)
    register_protocol()
    settings = load_settings()
    ctk.set_appearance_mode(settings.get("mode", "system"))
    root = Window()
    app = App(root, settings)
    if ext_job:
        root.after(600, lambda: app.enqueue_url(ext_job))
    root.mainloop()
