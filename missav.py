# -*- coding: utf-8 -*-
"""MissAV (missav.ai / missav.fans など) からの動画取得。

yt-dlp には MissAV の抽出器がなく、ページも Cloudflare の裏にあるため、
ブラウザと同じ TLS 指紋 (curl-cffi) でページを取って動画の場所を調べ、
HLS (m3u8) を直接ダウンロードする。

流れ：
  1. ミラー (missav.ai / missav.fans など) からページ HTML を取る
  2. ページ内の難読化スクリプト (または動画 UUID) から画質ごとの m3u8 を得る
  3. セグメントを並列で取って ffmpeg で mp4 にまとめる
"""
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

try:
    from curl_cffi import requests as cffi_requests
    CFFI_OK = True
except Exception:
    cffi_requests = None
    CFFI_OK = False

MISSAV_RE = re.compile(r"https?://(?:[^/]+\.)?missav\d*\.[^/]+/", re.IGNORECASE)
MIRRORS = ("missav.ai", "missav.ws", "missav.live", "missav.fans",
           "missav.media", "missav123.com", "missav01.com")
CDN_HOSTS = ("surrit.mrstcdn.store", "surrit.com")
UUID_RE = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")
PAGE_IMPERSONATE = "chrome110"
CDN_IMPERSONATE = ("firefox135", "firefox133")
# セグメント取得の並列数。CDN 側の 1 接続あたりの速度が頭打ちになるため、
# 並列を増やすのが速度改善に効く。増やしすぎると 429 / ブロックの原因になる。
MAX_WORKERS = 16
# surrit は「直接アクセス」(Sec-Fetch-Site: none) を WAF で弾き、
# ページのプレーヤーからの「クロスサイトの子リソース」だけを通すため、
# プレーヤーの fetch と同じヘッダを付ける。
CROSS_SITE_HEADERS = {
    "Accept": "*/*",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}


class MissAVError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


def is_missav_url(url):
    return isinstance(url, str) and bool(MISSAV_RE.match(url.strip()))


# ---------------- Cookie ----------------
def _cookies_for(cookie, domains):
    """cookie=("browser", 名前) / ("file", パス) / None から対象ドメインの Cookie を辞書で返す。
    取れなければ空辞書（Cookie なしでも試す）。"""
    jar = {}
    try:
        if not cookie:
            return jar
        if cookie[0] == "file":
            with open(cookie[1], encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) >= 7 and any(d in parts[0] for d in domains):
                        jar[parts[5]] = parts[6]
        elif cookie[0] == "browser":
            from yt_dlp.cookies import extract_cookies_from_browser
            for c in extract_cookies_from_browser(cookie[1]):
                if any(d in c.domain for d in domains):
                    jar[c.name] = c.value
    except Exception:
        return {}
    return jar


# ---------------- ページ取得 ----------------
def _fetch_page(url, cookies=None):
    """ミラーを順に試してページ HTML を取る。返り値 (html, 使ったドメイン)。"""
    if not CFFI_OK:
        raise MissAVError("curl-cffi が入っていません。\nコマンドプロンプトで\n  pip install curl-cffi\n"
                          "を実行してください。\n（exe版は build.bat で作り直してください）")
    path = urlparse(url).path
    if not path or path == "/":
        raise MissAVError("動画ページの URL ではありません。")
    hosts = []
    for h in [urlparse(url).netloc, *MIRRORS]:
        if h and h not in hosts:
            hosts.append(h)
    for host in hosts:
        try:
            r = cffi_requests.get(f"https://{host}{path}", impersonate=PAGE_IMPERSONATE,
                                  timeout=20, cookies=cookies or None)
            if r.status_code == 200 and ("seek" in r.text or "m3u8" in r.text):
                return r.text, host
        except Exception:
            continue
    raise MissAVError("MissAVのページを開けませんでした。Cloudflare のブロックの可能性があります。\n"
                      "ブラウザでそのページを一度開いてから、しばらく待って再試行してください。")


def _extract_uuid(html):
    """動画 UUID を探す。プレーヤー設定の seek サムネイル URL の手前にある。"""
    for sc in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL):
        i = sc.find("seek")
        if i >= 38:
            cand = sc[i - 38:i - 2]
            if UUID_RE.fullmatch(cand):
                return cand
    m = UUID_RE.search(html)
    return m.group(0) if m else None


def _page_title(html, video_id):
    m = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else video_id
    title = re.sub(r"\s*[|\-–—]\s*MissAV.*$", "", title).strip() or title
    return title or video_id


# ---------------- 画質ごとの m3u8 ----------------
_B62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _from_base(tok, radix):
    n = 0
    for ch in tok:
        n = n * radix + _B62.index(ch)
    return n


def _unpack_packed(html):
    """Dean Edwards 式に難読化されたスクリプトから m3u8 の URL を取り出す。
    返り値 [(ラベル, URL)]。見つからなければ []。"""
    out = []
    pat = re.compile(r"return p\}\('((?:\\'|[^'])*)',(\d+),\d+,'((?:\\'|[^'])*)'\.split\('\|'\)")
    for payload, radix, words in pat.findall(html):
        try:
            radix = int(radix)
            word_list = words.split("|")

            def repl(m):
                tok = m.group(0)
                try:
                    n = int(tok, radix) if radix <= 36 else _from_base(tok, radix)
                except ValueError:
                    return tok
                return word_list[n] if 0 <= n < len(word_list) else tok

            decoded = re.sub(r"\b\w+\b", repl, payload.replace("\\'", "'"))
        except Exception:
            continue
        for label, u in re.findall(r"(\w+)='(https://[^']+\.m3u8)'", decoded):
            out.append((label, u))
    return out


def _height_of(url):
    m = re.search(r"(\d{3,4})x(\d{3,4})", url)
    return int(m.group(2)) if m else None


def _parse_master(text, master_url):
    base = master_url.rsplit("/", 1)[0] + "/"
    lines = [ln.strip() for ln in text.splitlines()]
    out = []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"RESOLUTION=\d+x(\d+)", line)
            height = int(m.group(1)) if m else None
            if i + 1 < len(lines) and lines[i + 1] and not lines[i + 1].startswith("#"):
                u = lines[i + 1]
                out.append((height, u if u.startswith("http") else base + u))
    return out


def _fetch_master(uuid, host, cookies=None):
    headers = {"Referer": f"https://{host}/", "Origin": f"https://{host}", **CROSS_SITE_HEADERS}
    for cdn in CDN_HOSTS:
        master_url = f"https://{cdn}/{uuid}/playlist.m3u8"
        for tgt in CDN_IMPERSONATE:
            try:
                r = cffi_requests.get(master_url, impersonate=tgt, timeout=15,
                                      headers=headers, cookies=cookies or None)
                if r.status_code == 200 and "#EXTM3U" in r.text:
                    return _parse_master(r.text, master_url)
            except Exception:
                continue
    return []


def _fetch_variant_segments(variant_url, headers, cookies=None):
    for tgt in CDN_IMPERSONATE:
        try:
            r = cffi_requests.get(variant_url, impersonate=tgt, timeout=20,
                                  headers=headers, cookies=cookies or None)
            if r.status_code == 200 and "#EXT" in r.text:
                base = variant_url.rsplit("/", 1)[0] + "/"
                return [(s if s.startswith("http") else base + s)
                        for s in (ln.strip() for ln in r.text.splitlines())
                        if s and not s.startswith("#")]
        except Exception:
            continue
    raise MissAVError("画質ごとの再生リストを取得できませんでした。")


# ---------------- HLS ダウンロード ----------------
_tls = threading.local()


def _session_for(tgt):
    """スレッドごとに curl_cffi の Session を使い回す（keep-alive で
    TLS ハンドシェイクを毎回やらないため）。Session はスレッドセーフでないので
    スレッドローカルに持つ。"""
    sessions = getattr(_tls, "sessions", None)
    if sessions is None:
        sessions = _tls.sessions = {}
    sess = sessions.get(tgt)
    if sess is None:
        sess = cffi_requests.Session(impersonate=tgt)
        sessions[tgt] = sess
    return sess


def _fetch_segment(seg_url, headers, cookies=None):
    last = None
    for _ in range(2):
        for tgt in CDN_IMPERSONATE:
            try:
                r = _session_for(tgt).get(seg_url, headers=headers,
                                          timeout=30, cookies=cookies or None)
                if r.status_code == 200:
                    return r.content
                last = f"HTTP {r.status_code}"
            except Exception as e:
                last = str(e)
    raise MissAVError(f"動画の一部を取得できませんでした（{last}）")


def _safe_name(title, video_id, ext="mp4"):
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "", title).strip()[:80] or video_id
    return f"{name} [{video_id}].{ext}"


def _unique_path(dest, filename):
    path = os.path.join(dest, filename)
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(filename)
    for i in range(2, 100):
        path = os.path.join(dest, f"{stem} ({i}){ext}")
        if not os.path.exists(path):
            return path
    return path


def download(url, dest, max_height=None, progress_cb=None, cancel_cb=None,
             msg_cb=None, ffmpeg=None, cookie=None, _max_segments=None):
    """MissAV の動画を1件ダウンロードして保存先パスを返す。
    progress_cb(取得バイト数, 推定合計バイト数 or None)、cancel_cb() が真なら Cancelled。"""
    if not CFFI_OK:
        raise MissAVError("curl-cffi が入っていません。\nコマンドプロンプトで\n  pip install curl-cffi\n"
                          "を実行してください。\n（exe版は build.bat で作り直してください）")
    video_id = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or "video"
    cookies = _cookies_for(cookie, ("missav", "mrstcdn", "surrit"))
    if msg_cb:
        msg_cb("MissAVのページを確認中…")
    html, host = _fetch_page(url, cookies)
    title = _page_title(html, video_id)
    uuid = _extract_uuid(html)
    if not uuid:
        raise MissAVError("動画の場所が見つかりませんでした。ページの形式が変わった可能性があります。")
    headers = {"Referer": f"https://{host}/", "Origin": f"https://{host}", **CROSS_SITE_HEADERS}

    streams = []
    for _label, u in _unpack_packed(html):
        if "/playlist.m3u8" in u:
            continue
        streams.append((_height_of(u), u))
    if not streams:
        if msg_cb:
            msg_cb("画質を確認中…")
        streams = _fetch_master(uuid, host, cookies)
    if not streams:
        raise MissAVError("動画の再生リストを取得できませんでした。")

    known = sorted([s for s in streams if s[0]], key=lambda s: s[0])
    if max_height:
        under = [s for s in known if s[0] <= max_height]
        pick = under[-1] if under else (known[0] if known else streams[0])
    else:
        pick = known[-1] if known else streams[0]
    height, variant_url = pick
    if msg_cb:
        msg_cb(f"ダウンロード中…（{height}p）" if height else "ダウンロード中…")
    seg_urls = _fetch_variant_segments(variant_url, headers, cookies)
    if _max_segments:
        seg_urls = seg_urls[:_max_segments]
    total = len(seg_urls)
    if total == 0:
        raise MissAVError("動画の断片が見つかりませんでした。")

    os.makedirs(dest, exist_ok=True)
    out_path = _unique_path(dest, _safe_name(title, video_id))
    parts_dir = tempfile.mkdtemp(prefix=".missav_", dir=dest)
    try:
        state = {"done": 0, "bytes": 0, "lock": threading.Lock()}

        def seg_path(i):
            return os.path.join(parts_dir, f"{i:05d}.ts")

        def fetch_to_file(item):
            i, u = item
            data = _fetch_segment(u, headers, cookies)
            tmp = seg_path(i) + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, seg_path(i))
            with state["lock"]:
                state["done"] += 1
                state["bytes"] += len(data)
                if progress_cb:
                    est = None
                    if state["done"] >= 4:
                        est = int(state["bytes"] / state["done"] * total)
                    progress_cb(state["bytes"], est)

        # 全セグメントを一度に投入し、終わったものから次を取る。
        # 以前の 16 個ずつのバッチだと遅いセグメントを毎回待っていた。
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(fetch_to_file, (i, u)): i
                    for i, u in enumerate(seg_urls)}
            try:
                for fut in as_completed(futs):
                    if cancel_cb and cancel_cb():
                        for f in futs:
                            f.cancel()
                        raise Cancelled()
                    fut.result()  # セグメント取得の失敗はここで伝播する
            except Exception:
                for f in futs:
                    f.cancel()
                raise
        if cancel_cb and cancel_cb():
            raise Cancelled()

        if msg_cb:
            msg_cb("仕上げ中…（映像を結合）")
        list_path = os.path.join(parts_dir, "filelist.txt")
        with open(list_path, "w", encoding="utf-8") as lf:
            for i in range(total):
                lf.write("file '" + seg_path(i).replace("'", "'\\''") + "'\n")
        if ffmpeg:
            proc = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                 "-i", list_path, "-c", "copy", out_path],
                capture_output=True, text=True)
            if proc.returncode == 0 and os.path.exists(out_path):
                return out_path
        # ffmpeg が無い・失敗 → 断片をそのままつないだ .ts で保存（再生できる）
        ts_out = out_path[:-4] + ".ts" if out_path.endswith(".mp4") else out_path + ".ts"
        with open(ts_out, "wb") as out:
            for i in range(total):
                with open(seg_path(i), "rb") as sf:
                    shutil.copyfileobj(sf, out, 1024 * 1024)
        return ts_out
    finally:
        shutil.rmtree(parts_dir, ignore_errors=True)
