# -*- coding: utf-8 -*-
"""動画コマ書き出し：処理エンジン（画面に依存しない部分）"""
import importlib.util
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace

import cv2
import numpy as np

try:
    from PIL import Image
    PIL_OK = True
except Exception:
    PIL_OK = False


# ---------------- ffmpeg ----------------
def find_ffmpeg():
    """pip の imageio-ffmpeg に同梱の ffmpeg を優先して使う"""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return shutil.which("ffmpeg")


FFMPEG = find_ffmpeg()
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # Windowsで黒い画面を出さない

RESOLUTIONS = {
    "元のサイズ": None,
    "4K（3840×2160）": (3840, 2160),
    "2K（2560×1440）": (2560, 1440),
    "フルHD 1080p（1920×1080）": (1920, 1080),
    "HD 720p（1280×720）": (1280, 720),
    "SD 480p（854×480）": (854, 480),
    "サムネ（480×270）": (480, 270),
}
IMAGE_EXTS = ("png", "jpg")


def calc_size(w, h, target):
    """縦横比を保ったまま target の枠に収まるサイズ（縦長動画は枠も縦向き）"""
    if not target:
        return w, h
    tw, th = target
    if h > w:
        tw, th = th, tw
    scale = min(tw / w, th / h)
    return max(1, round(w * scale)), max(1, round(h * scale))


# ---------------- 動画 ----------------
def open_video(path):
    """日本語パスでも開けるようにする"""
    cap = cv2.VideoCapture(path)
    if cap.isOpened():
        return cap
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(1024)
            if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024):
                cap = cv2.VideoCapture(buf.value)
                if cap.isOpened():
                    return cap
        except Exception:
            pass
    return None


def probe(path):
    cap = open_video(path)
    if cap is None:
        raise RuntimeError("動画を開けませんでした：" + os.path.basename(path))
    meta = SimpleNamespace(
        fps=cap.get(cv2.CAP_PROP_FPS) or 30.0,
        total=int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
        vw=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        vh=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
    )
    cap.release()
    if meta.total <= 0:
        raise RuntimeError("コマ数を取得できませんでした：" + os.path.basename(path))
    return meta


# ---------------- 背景の切り抜き ----------------
WORK_W = 960  # 判定は縮小して行う（速く、ノイズにも強い）


def work_size(w, h):
    scale = min(1.0, WORK_W / w)
    return max(1, int(w * scale)), max(1, int(h * scale))


def estimate_background(path, total, wsize, n=30):
    """動画全体から等間隔にコマを集め、各画素の中央値を背景とみなす"""
    cap = open_video(path)
    if cap is None:
        return None
    idxs = np.unique(np.linspace(0, max(0, total - 1), min(n, total)).astype(int))
    frames = []
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, f = cap.read()
        if ok:
            frames.append(cv2.resize(f, wsize, interpolation=cv2.INTER_AREA))
    cap.release()
    if not frames:
        return None
    return np.median(np.stack(frames), axis=0).astype(np.uint8)


def border_color(frame, frac=0.04):
    """コマの外周の色（中央値）。グリーンバックなど単色背景の色として使う"""
    h, w = frame.shape[:2]
    t = max(2, int(min(h, w) * frac))
    parts = [frame[:t].reshape(-1, 3), frame[-t:].reshape(-1, 3),
             frame[:, :t].reshape(-1, 3), frame[:, -t:].reshape(-1, 3)]
    return np.median(np.concatenate(parts), axis=0).astype(np.uint8)


def solid_bg(color, wsize):
    return np.full((wsize[1], wsize[0], 3), color, np.uint8)


def make_mask(frame, bg, thresh):
    """背景との差からキャラクター部分のマスク（0〜255）を作る。サイズは bg と同じ"""
    small = cv2.resize(frame, (bg.shape[1], bg.shape[0]), interpolation=cv2.INTER_AREA)
    diff = cv2.absdiff(small, bg).max(axis=2)
    diff = cv2.GaussianBlur(diff, (5, 5), 0)
    mask = (diff > thresh).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    keep = [c for c in contours if cv2.contourArea(c) >= mask.size * 0.002]
    out = np.zeros_like(mask)
    cv2.drawContours(out, keep, -1, 255, thickness=cv2.FILLED)
    return cv2.GaussianBlur(out, (5, 5), 0)


def app_dir():
    """このソフト（exe または .py）があるフォルダ"""
    return os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))


_ai_ok = None


def ai_available():
    """rembg と、AIを動かす onnxruntime の両方が入っているか（結果は覚えておく）"""
    global _ai_ok
    if _ai_ok is None:
        try:
            _ai_ok = (importlib.util.find_spec("rembg") is not None
                      and importlib.util.find_spec("onnxruntime") is not None)
        except Exception:
            _ai_ok = False
        if not _ai_ok and getattr(sys, "frozen", False):  # exe の中は、実際に読み込めるかで確かめる
            try:
                import onnxruntime  # noqa: F401
                import rembg  # noqa: F401
                _ai_ok = True
            except Exception:
                _ai_ok = False
    return _ai_ok


def check_ai_to_file():
    """AIが入っているかを調べて、結果を ai_check.txt に書く（exe のビルド後の確認用）"""
    try:
        import onnxruntime  # noqa: F401
        from rembg import new_session, remove  # noqa: F401
        text = "AI_OK: rembg and onnxruntime are inside this app."
    except Exception as e:
        text = "AI_NG: " + repr(e)[:300]
    try:
        with open(os.path.join(app_dir(), "ai_check.txt"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass
    return text


# AIモデル（rembg）。軽いものほど速い。人物向けなど用途別もある
AI_MODELS = {
    "標準（u2net）": "u2net",
    "高速（u2netp・軽い）": "u2netp",
    "軽量で高品質寄り（silueta）": "silueta",
    "人物向け（u2net_human_seg）": "u2net_human_seg",
}
_ai_sessions = {}
_ai_devices = {}
_ai_lock = threading.Lock()


def ai_wsize(w, h):
    scale = min(1.0, 640 / w)
    return max(1, int(w * scale)), max(1, int(h * scale))


def _providers():
    """使える計算装置を選ぶ。GPU（CUDA / DirectML）があれば優先し、無ければCPU"""
    try:
        import onnxruntime as ort
        avail = ort.get_available_providers()
    except Exception:
        return None, "CPU"
    gpu = [p for p in ("CUDAExecutionProvider", "DmlExecutionProvider") if p in avail]
    name = "CPU" if not gpu else ("GPU（CUDA）" if "CUDAExecutionProvider" in gpu else "GPU（DirectML）")
    return gpu + ["CPUExecutionProvider"], name


def _make_session(new_session, model):
    prov, dev = _providers()
    for p, d in ((prov, dev), (["CPUExecutionProvider"], "CPU")):  # GPUで失敗したらCPUでやり直す
        if p is None:
            continue
        try:
            return new_session(model, providers=p), d
        except TypeError:
            break  # 古い rembg は providers を受け取れない
        except Exception:
            continue
    return new_session(model), "CPU"


def ai_prepare(model="u2net"):
    """AIモデルを読み込む（初回はダウンロード）。時間がかかるので、必ず別スレッドから呼ぶ"""
    with _ai_lock:
        if model not in _ai_sessions:
            # アプリの隣に models フォルダがあれば、そこのモデルを使う（オフライン用）
            md = os.path.join(app_dir(), "models")
            if os.path.isdir(md) and "U2NET_HOME" not in os.environ:
                os.environ["U2NET_HOME"] = md
            from rembg import new_session
            _ai_sessions[model], _ai_devices[model] = _make_session(new_session, model)
    return _ai_devices[model]


def ai_device(model="u2net"):
    return _ai_devices.get(model, "CPU")


def ai_mask(frame, wsize, model="u2net"):
    """rembg（AI）で人物・キャラクターを切り抜く"""
    ai_prepare(model)
    from rembg import remove
    small = cv2.resize(frame, wsize, interpolation=cv2.INTER_AREA)
    rgb = Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
    with _ai_lock:
        m = remove(rgb, session=_ai_sessions[model], only_mask=True)
    return np.array(m.convert("L"))


class Cutter:
    """method: 'diff'（背景差分）/ 'chroma'（単色背景）/ 'ai'"""

    def __init__(self, method, thr, bg=None, model="u2net"):
        self.method, self.thr, self.bg, self.model = method, thr, bg, model

    def mask(self, frame):
        if self.method == "ai":
            h, w = frame.shape[:2]
            return ai_mask(frame, ai_wsize(w, h), self.model)
        return make_mask(frame, self.bg, self.thr)


class CutSpec:
    """切り抜きの設定。bg が None のときは、動画ごとに自動で用意する（まとめて処理用）"""

    def __init__(self, method="diff", thr=30, bg=None, model="u2net"):
        self.method, self.thr, self.bg, self.model = method, thr, bg, model

    def prepared(self, path, meta):
        bg, wsize = self.bg, work_size(meta.vw, meta.vh)
        if self.method == "diff" and bg is None:
            bg = estimate_background(path, meta.total, wsize)
            if bg is None:
                raise RuntimeError("背景を推定できませんでした")
        elif self.method == "chroma" and bg is None:
            cap = open_video(path)
            ok, fr = cap.read() if cap else (False, None)
            if cap:
                cap.release()
            if not ok:
                raise RuntimeError("背景の色を取得できませんでした")
            bg = solid_bg(border_color(fr), wsize)
        return Cutter(self.method, self.thr, bg, self.model)


# ---------------- 切り取り・トリミング ----------------
def crop_frac(arr, fr):
    """fr=(x0,y0,x1,y1)（0〜1の割合）で配列を切り取る。マスクにも同じ割合で使える"""
    h, w = arr.shape[:2]
    x0 = min(max(0, int(round(fr[0] * w))), w - 1)
    y0 = min(max(0, int(round(fr[1] * h))), h - 1)
    x1 = max(x0 + 1, min(w, int(round(fr[2] * w))))
    y1 = max(y0 + 1, min(h, int(round(fr[3] * h))))
    return arr[y0:y1, x0:x1]


def mask_bbox(mask, thr=40, margin=0.01):
    ys, xs = np.where(mask > thr)
    if len(xs) == 0:
        return None
    h, w = mask.shape[:2]
    return (max(0.0, xs.min() / w - margin), max(0.0, ys.min() / h - margin),
            min(1.0, (xs.max() + 1) / w + margin), min(1.0, (ys.max() + 1) / h + margin))


def merge_frac(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def region_size(meta, crop):
    """切り取り後の元動画上でのピクセルサイズ"""
    if not crop:
        return meta.vw, meta.vh
    return (max(1, round((crop[2] - crop[0]) * meta.vw)), max(1, round((crop[3] - crop[1]) * meta.vh)))


def apply_alpha(frame, mask):
    h, w = frame.shape[:2]
    bgra = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = cv2.resize(mask, (w, h), interpolation=cv2.INTER_LINEAR)
    return bgra


def fit(frame, size):
    h, w = frame.shape[:2]
    if size == (w, h):
        return frame
    down = size[0] * size[1] < w * h
    return cv2.resize(frame, size, interpolation=cv2.INTER_AREA if down else cv2.INTER_LANCZOS4)


def process_frame(frame, o, union=None, even=False):
    """1コマに、切り抜き → 切り取り → トリミング → サイズ変更 を順に行う"""
    cutter = getattr(o, "cutter", None)
    mask = cutter.mask(frame) if cutter else None
    if o.crop:
        frame = crop_frac(frame, o.crop)
        if mask is not None:
            mask = crop_frac(mask, o.crop)
    if mask is not None and o.trim != "none":
        fr = union if (o.trim == "union" and union) else (mask_bbox(mask) if o.trim == "each" else None)
        if fr:
            frame, mask = crop_frac(frame, fr), crop_frac(mask, fr)
    h, w = frame.shape[:2]
    size = calc_size(w, h, o.target)
    if even:
        size = (max(2, size[0] - size[0] % 2), max(2, size[1] - size[1] % 2))
    frame = fit(frame, size)
    if mask is not None:
        frame = apply_alpha(frame, mask)
    return frame


# ---------------- コマの選び方（間引き / 場面検出） ----------------
def _hist(frame):
    hsv = cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0, 1, 2], None, [8, 8, 8], [0, 180, 0, 256, 0, 256])
    cv2.normalize(h, h, alpha=1.0, norm_type=cv2.NORM_L1)
    return h


def scene_threshold(sens):
    """感度1〜10 → 差のしきい値（感度が高いほど小さな変化でも拾う）"""
    return 0.46 - 0.04 * max(1, min(10, sens))


def select_frames(cap, meta, f0, f1, o, cb):
    cap.set(cv2.CAP_PROP_POS_FRAMES, f0)
    thr = scene_threshold(o.sens)
    gap = max(1, int(meta.fps * 0.25))
    prev, last = None, -10 ** 9
    for idx in range(f0, f1):
        if cb.cancelled():
            return
        if not cap.grab():
            return
        if o.scene:
            ok, frame = cap.retrieve()
            if not ok:
                continue
            h = _hist(frame)
            take = prev is None or (cv2.compareHist(prev, h, cv2.HISTCMP_BHATTACHARYYA) > thr and idx - last >= gap)
            prev = h
            if take:
                last = idx
                yield idx, frame
        elif (idx - f0) % o.step == 0:
            ok, frame = cap.retrieve()
            if ok:
                yield idx, frame


def iter_processed(path, meta, f0, f1, o, cb, union, label, even=False):
    cap = open_video(path)
    if cap is None:
        raise RuntimeError("動画を開けませんでした")
    n, span = 0, max(1, f1 - f0)
    try:
        for idx, frame in select_frames(cap, meta, f0, f1, o, cb):
            n += 1
            cb.progress((idx - f0 + 1) / span, label, n)
            yield idx, process_frame(frame, o, union, even)
    finally:
        cap.release()


def compute_union(path, meta, f0, f1, o, cb):
    """トリミング「全コマ共通」用：範囲全体でキャラクターが動く範囲を調べる"""
    cap = open_video(path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, f0)
    un, gap, span = None, max(o.step, 3), max(1, f1 - f0)
    for idx in range(f0, f1):
        if cb.cancelled() or not cap.grab():
            break
        if (idx - f0) % gap == 0:
            ok, fr = cap.retrieve()
            if ok:
                m = o.cutter.mask(fr)
                if o.crop:
                    m = crop_frac(m, o.crop)
                un = merge_frac(un, mask_bbox(m))
            cb.progress((idx - f0) / span, "キャラクターの範囲を調べ中")
    cap.release()
    return un


# ---------------- 書き出し ----------------
def _write_bytes(path, buf):
    with open(path, "wb") as f:
        f.write(buf.tobytes())


def write_images(path, meta, f0, f1, o, out, base, cb, union):
    digits = max(5, len(str(meta.total)))
    params = [cv2.IMWRITE_JPEG_QUALITY, 95] if o.ext == "jpg" else []
    n = 0
    for idx, img in iter_processed(path, meta, f0, f1, o, cb, union, "書き出し中"):
        ok, buf = cv2.imencode("." + o.ext, img, params)
        _write_bytes(os.path.join(out, f"{base}_{idx:0{digits}d}.{o.ext}"), buf)
        n += 1
    return n


def write_sheet(path, meta, f0, f1, o, out, base, cb, union):
    cells = [img for _, img in iter_processed(path, meta, f0, f1, o, cb, union, "コマを集め中")]
    if not cells or cb.cancelled():
        return 0
    cw, ch = max(c.shape[1] for c in cells), max(c.shape[0] for c in cells)
    n = len(cells)
    cols = o.cols or math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    alpha = any(c.shape[2] == 4 for c in cells)
    sheet = np.zeros((rows * ch, cols * cw, 4 if alpha else 3), np.uint8)
    for i, c in enumerate(cells):
        if alpha and c.shape[2] == 3:
            c = cv2.cvtColor(c, cv2.COLOR_BGR2BGRA)
            c[:, :, 3] = 255
        r, col = divmod(i, cols)
        y, x = r * ch + (ch - c.shape[0]) // 2, col * cw + (cw - c.shape[1]) // 2
        sheet[y:y + c.shape[0], x:x + c.shape[1]] = c
    ok, buf = cv2.imencode(".png", sheet)
    _write_bytes(os.path.join(out, f"{base}_sheet_{f0:05d}-{f1 - 1:05d}.png"), buf)
    return n


def _delay_ms(meta, o):
    return 1000.0 * o.step / meta.fps / o.speed


def write_gif(path, meta, f0, f1, o, out, base, cb, union):
    if not PIL_OK:
        raise RuntimeError("Pillow が入っていません（pip install pillow）")
    delay = max(20, int(round(_delay_ms(meta, o) / 10)) * 10)  # GIFは最短20ms・10ms単位
    dest = os.path.join(out, f"{base}_{f0:05d}-{f1 - 1:05d}.gif")
    frames = (Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
              for _, img in iter_processed(path, meta, f0, f1, o, cb, union, "GIF用に読み込み中"))
    if o.reverse:
        frames = iter(list(frames)[::-1])
    first = next(frames, None)
    if first is None or cb.cancelled():
        return 0
    cb.progress(1.0, "GIFを書き込み中")
    first.save(dest, save_all=True, append_images=frames, duration=delay, loop=0)
    if cb.cancelled():
        os.remove(dest)
        return 0
    return 1


def write_webp(path, meta, f0, f1, o, out, base, cb, union):
    if not PIL_OK:
        raise RuntimeError("Pillow が入っていません（pip install pillow）")
    delay = max(10, int(round(_delay_ms(meta, o))))
    dest = os.path.join(out, f"{base}_{f0:05d}-{f1 - 1:05d}.webp")
    frames = []
    for _, img in iter_processed(path, meta, f0, f1, o, cb, union, "WebP用に読み込み中"):
        code = cv2.COLOR_BGRA2RGBA if img.shape[2] == 4 else cv2.COLOR_BGR2RGBA
        frames.append(Image.fromarray(cv2.cvtColor(img, code)))
    if not frames or cb.cancelled():
        return 0
    if o.reverse:
        frames.reverse()
    cb.progress(1.0, "WebPを書き込み中")
    frames[0].save(dest, format="WEBP", save_all=True, append_images=frames[1:], duration=delay,
                   loop=0, quality=90, method=4)
    return 1


def _atempo(speed):
    chain, s = [], speed
    while s > 2.0:
        chain.append(2.0)
        s /= 2.0
    while s < 0.5:
        chain.append(0.5)
        s *= 2.0
    if abs(s - 1.0) > 1e-6:
        chain.append(s)
    return ",".join(f"atempo={x:.5f}" for x in chain)


def _run_ffmpeg(cmd, dur_out, label, cb):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    log = []
    try:
        for line in proc.stdout:
            if cb.cancelled():
                proc.terminate()
                break
            line = line.strip()
            m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
            if m:
                if dur_out > 0:
                    cb.progress(min(1.0, int(m.group(1)) / 1e6 / dur_out), label)
            elif line and not re.match(r"^\w+=", line):
                log.append(line)
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    return proc.returncode, log


def write_ffmpeg(path, meta, f0, f1, o, out, base, cb, ext):
    """MP4（映像＋任意で音声）/ MP3 / M4A を ffmpeg で書き出す"""
    if not FFMPEG:
        raise RuntimeError("ffmpeg が見つかりません（pip install imageio-ffmpeg）")
    ss, dur = f0 / meta.fps, (f1 - f0) / meta.fps
    dest = os.path.join(out, f"{base}_{f0:05d}-{f1 - 1:05d}.{ext}")
    tmpdir = tempfile.mkdtemp()  # 日本語パスだと失敗することがあるため、一度英数字の場所に作る
    tmp = os.path.join(tmpdir, "out." + ext)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{ss:.6f}", "-t", f"{dur:.6f}", "-i", path]
    af = []
    if o.reverse:
        af.append("areverse")
    if o.speed != 1:
        af.append(_atempo(o.speed))
    if ext == "mp4":
        vf = ["reverse"] if o.reverse else []
        cw, ch, x, y = meta.vw, meta.vh, 0, 0
        if o.crop:
            x, y = int(round(o.crop[0] * meta.vw)), int(round(o.crop[1] * meta.vh))
            cw, ch = region_size(meta, o.crop)
            cw, ch = max(2, cw - cw % 2), max(2, ch - ch % 2)
            vf.append(f"crop={cw}:{ch}:{x}:{y}")
        if o.step > 1:
            vf.append(f"fps={meta.fps / o.step:.5f}")
        size = calc_size(cw, ch, o.target)
        size = (max(2, size[0] - size[0] % 2), max(2, size[1] - size[1] % 2))
        if size != (cw, ch) or (cw, ch) != (meta.vw, meta.vh):
            vf.append(f"scale={size[0]}:{size[1]}:flags=lanczos")
        if o.speed != 1:
            vf.append(f"setpts=PTS/{o.speed}")
        cmd += ["-map", "0:v:0"]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        if o.audio:
            cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k"]
            if af:
                cmd += ["-af", ",".join(af)]
        else:
            cmd += ["-an"]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-r", f"{meta.fps / o.step:.5f}"]  # 速度を変えても、コマ数（fps）は元のまま
        label = "MP4を書き出し中"
    else:
        cmd += ["-vn", "-map", "0:a:0"]
        if af:
            cmd += ["-af", ",".join(af)]
        cmd += (["-c:a", "libmp3lame", "-q:a", "2"] if ext == "mp3" else ["-c:a", "aac", "-b:a", "192k"])
        label = "音声を書き出し中"
    cmd += ["-progress", "pipe:1", "-nostats", tmp]
    code, log = _run_ffmpeg(cmd, dur / o.speed, label, cb)
    if cb.cancelled():
        shutil.rmtree(tmpdir, ignore_errors=True)
        return 0
    if code != 0 or not os.path.exists(tmp):
        shutil.rmtree(tmpdir, ignore_errors=True)
        text = "\n".join(log[-5:])
        if "matches no streams" in text or "does not contain any stream" in text:
            raise RuntimeError("この動画には音声がありません")
        raise RuntimeError("ffmpeg でエラーになりました。\n" + text)
    shutil.move(tmp, dest)
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 1


def write_mp4_opencv(path, meta, f0, f1, o, out, base, cb, union):
    """ffmpeg が無いときの代わり（音声なし・逆再生なし）"""
    if o.audio or o.reverse:
        raise RuntimeError("音声つき・逆再生のMP4には ffmpeg が必要です（pip install imageio-ffmpeg）")
    dest = os.path.join(out, f"{base}_{f0:05d}-{f1 - 1:05d}.mp4")
    tmpdir = tempfile.mkdtemp()
    tmp = os.path.join(tmpdir, "out.mp4")
    writer, n = None, 0
    try:
        for _, img in iter_processed(path, meta, f0, f1, o, cb, union, "MP4を書き出し中", even=True):
            if writer is None:
                h, w = img.shape[:2]
                writer = cv2.VideoWriter(tmp, cv2.VideoWriter_fourcc(*"mp4v"),
                                         max(1.0, meta.fps / o.step * o.speed), (w, h))
                if not writer.isOpened():
                    raise RuntimeError("MP4の書き出しを開始できませんでした")
            writer.write(img)
            n += 1
    finally:
        if writer is not None:
            writer.release()
    if not cb.cancelled() and n > 0:
        shutil.move(tmp, dest)
    shutil.rmtree(tmpdir, ignore_errors=True)
    return n if not cb.cancelled() else 0


# ---------------- 1本の動画 / まとめて ----------------
def run_job(path, meta, f0, f1, o, out, base, cb):
    """1本の動画を書き出す。戻り値は作ったファイル数（画像は枚数）"""
    o = SimpleNamespace(**vars(o))
    o.cutter = o.cut.prepared(path, meta) if o.cut else None
    union = None
    if o.cutter and o.trim == "union" and o.ext in IMAGE_EXTS + ("sheet", "webp"):
        union = compute_union(path, meta, f0, f1, o, cb)
    if cb.cancelled():
        return 0
    if o.ext in IMAGE_EXTS:
        return write_images(path, meta, f0, f1, o, out, base, cb, union)
    if o.ext == "sheet":
        return write_sheet(path, meta, f0, f1, o, out, base, cb, union)
    if o.ext == "gif":
        return write_gif(path, meta, f0, f1, o, out, base, cb, union)
    if o.ext == "webp":
        return write_webp(path, meta, f0, f1, o, out, base, cb, union)
    if o.ext == "mp4":
        if FFMPEG:
            return write_ffmpeg(path, meta, f0, f1, o, out, base, cb, "mp4")
        return write_mp4_opencv(path, meta, f0, f1, o, out, base, cb, union)
    if o.ext in ("mp3", "m4a"):
        return write_ffmpeg(path, meta, f0, f1, o, out, base, cb, o.ext)
    raise RuntimeError("未対応の形式です：" + str(o.ext))


class _SubCb:
    def __init__(self, cb, i, n):
        self.cb, self.i, self.n = cb, i, n

    def cancelled(self):
        return self.cb.cancelled()

    def progress(self, frac, label, n=None):
        tag = f"（{self.i + 1}/{self.n}本目）" if self.n > 1 else ""
        self.cb.progress((self.i + frac) / self.n, label + tag, n)


def run_batch(paths, ranges, o, out_dir, cb):
    """複数の動画を同じ設定で書き出す。ranges[i] が None なら動画全体。
    戻り値: (作成数の合計, [(動画名, エラー文), ...])"""
    total, errors, N = 0, [], len(paths)
    for i, p in enumerate(paths):
        if cb.cancelled():
            break
        base = os.path.splitext(os.path.basename(p))[0]
        try:
            meta = probe(p)
            f0, f1 = ranges[i] if ranges and ranges[i] else (0, meta.total)
            out = out_dir
            if N > 1 and (o.ext in IMAGE_EXTS):
                out = os.path.join(out_dir, base)
            os.makedirs(out, exist_ok=True)
            total += run_job(p, meta, f0, f1, o, out, base, _SubCb(cb, i, N))
        except Exception as e:
            errors.append((os.path.basename(p), str(e)))
    return total, errors


def save_single(frame, idx, total, o, out_dir, base):
    """プレビュー中の1コマだけを保存する（o.cutter は用意済みのものを渡す）"""
    o = SimpleNamespace(**vars(o))
    if o.trim == "union":
        o.trim = "each"
    img = process_frame(frame, o)
    ext = "jpg" if (o.ext == "jpg" and img.shape[2] == 3) else "png"
    params = [cv2.IMWRITE_JPEG_QUALITY, 95] if ext == "jpg" else []
    ok, buf = cv2.imencode("." + ext, img, params)
    dest = os.path.join(out_dir, f"{base}_{idx:0{max(5, len(str(total)))}d}.{ext}")
    os.makedirs(out_dir, exist_ok=True)
    _write_bytes(dest, buf)
    return dest
