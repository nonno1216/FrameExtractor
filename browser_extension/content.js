// 右クリックした場所（またはマウスの下）の動画を調べる
// タイムライン上の動画は、ページの URL と違うため
let last = { url: null, src: null };
let mouse = { x: -1, y: -1 };

const YT_LINK = 'a[href*="/watch?"], a[href*="/shorts/"]';
const YT_CARD = "ytd-rich-item-renderer, ytd-video-renderer, ytd-compact-video-renderer, ytd-grid-video-renderer, " +
  "ytd-playlist-video-renderer, ytd-reel-item-renderer, ytd-video-preview, yt-lockup-view-model, ytm-shorts-lockup-view-model";

function linkIn(box) {
  if (!box) return null;
  const a = [...box.querySelectorAll("a[href]")].find((a) => isVideoUrl(a.href));
  return a ? a.href : null;
}

function findVideoUrl(el) {
  if (el && !(el instanceof Element)) el = el.parentElement;
  if (!el) return null;
  const host = location.hostname;
  if (/(^|\.)(x|twitter)\.com$/.test(host)) {
    // ツイートの投稿時刻のリンクが、そのツイートの URL
    const art = el.closest("article");
    const time = art && art.querySelector('a[href*="/status/"] time');
    const a = time && time.closest("a");
    const m = a && a.href.match(/^(https:\/\/[^/]+\/[^/]+\/status\/\d+)/);
    return m ? m[1] : null;
  }
  const link = el.closest("a[href]");
  if (link && isVideoUrl(link.href)) return link.href;
  if (/(^|\.)youtube\.com$/.test(host)) {
    const card = el.closest(YT_CARD);
    const a = card && card.querySelector(YT_LINK);
    return a ? a.href : null;
  }
  if (/(^|\.)(instagram|tiktok|facebook|reddit)\.com$/.test(host)) {
    return linkIn(el.closest("article, shreddit-post, [data-e2e='recommend-list-item-container']"));
  }
  return null;
}

// 動画ファイルそのものの URL（blob: は保存できないので除く）
function videoSrc(el, x, y) {
  let v = el instanceof Element ? el.closest("video") : null;
  if (!v && x >= 0) v = document.elementsFromPoint(x, y).find((e) => e.tagName === "VIDEO");
  if (!v) return null;
  const s = v.currentSrc || v.src || (v.querySelector("source[src]") || {}).src;
  return s && /^https?:/.test(s) ? s : null;
}

function inspect(el, x, y) {
  return { url: findVideoUrl(el), src: videoSrc(el, x, y) };
}

document.addEventListener("contextmenu", (e) => {
  last = inspect(e.target, e.clientX, e.clientY);
}, true);

document.addEventListener("mousemove", (e) => {
  mouse = { x: e.clientX, y: e.clientY };
}, { capture: true, passive: true });

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!msg) return;
  if (msg.type === "getVideoUrl") reply(last);
  if (msg.type === "getHoverVideo") {
    const el = mouse.x >= 0 ? document.elementFromPoint(mouse.x, mouse.y) : null;
    reply(inspect(el, mouse.x, mouse.y));
  }
});
