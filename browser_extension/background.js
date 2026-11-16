// 「動画を保存」→ 起動中のアプリへ URL の候補を送る。アプリが起動していなければ frameextractor:// で起動する
// アプリは候補を順に試し、最初に保存できたものを使う
importScripts("patterns.js");

const APP = "http://127.0.0.1:47815";
const PROTO = "frameextractor";
const MAX_URLS = 6;

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "save-video",
      title: "動画を保存（動画コマ書き出し）",
      contexts: ["page", "video", "audio", "link", "image", "frame"]
    });
  });
});

async function askContent(tabId, frameId, type) {
  try {
    return await chrome.tabs.sendMessage(tabId, { type }, { frameId: frameId || 0 });
  } catch (e) {
    return null; // 拡張機能を入れる前から開いていたタブなど
  }
}

function candidates(list) {
  const out = [];
  for (const u of list) {
    if (u && /^https?:/.test(u) && !out.includes(u)) out.push(u);
  }
  return out.slice(0, MAX_URLS);
}

function badge(tabId, text, color) {
  chrome.action.setBadgeBackgroundColor({ tabId, color });
  chrome.action.setBadgeText({ tabId, text });
  setTimeout(() => chrome.action.setBadgeText({ tabId, text: "" }), 2500);
}

async function sendToApp(job) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), 2000);
  try {
    const r = await fetch(APP + "/download", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-FrameExtractor": "1" },
      body: JSON.stringify(job),
      signal: ctl.signal
    });
    return r.ok;
  } catch (e) {
    return false;
  } finally {
    clearTimeout(t);
  }
}

async function save(urls, referer, tab) {
  if (!urls.length) {
    badge(tab.id, "×", "#b3261e");
    return;
  }
  const job = { urls, referer };
  if (await sendToApp(job)) {
    badge(tab.id, "OK", "#1f6f5c");
    return;
  }
  // アプリが起動していない → 起動して URL を渡す（初回はブラウザが確認を出す）
  badge(tab.id, "…", "#66726c");
  const q = urls.map((u) => "url=" + encodeURIComponent(u));
  if (referer) q.push("ref=" + encodeURIComponent(referer));
  chrome.tabs.update(tab.id, { url: `${PROTO}://download?${q.join("&")}` });
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "save-video" || !tab) return;
  const r = (await askContent(tab.id, info.frameId, "getVideoUrl")) || {};
  const link = info.linkUrl && isVideoUrl(info.linkUrl) ? info.linkUrl : null;
  const frame = info.frameUrl && info.frameUrl !== info.pageUrl ? info.frameUrl : null;
  // 優先順：動画ページへのリンク → 右クリックした動画のページ → 埋め込みプレーヤー → このページ → 動画ファイルそのもの
  const urls = candidates([link, r.url, frame, info.pageUrl, info.srcUrl, r.src, info.linkUrl]);
  save(urls, info.pageUrl, tab);
});

// ショートカット（Alt+Shift+S）：マウスの下の動画を保存。右クリックがサイト独自のメニューになるときに
chrome.commands.onCommand.addListener(async (command, tab) => {
  if (command !== "save-video") return;
  if (!tab) [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https?:/.test(tab.url || "")) return;
  const r = (await askContent(tab.id, 0, "getHoverVideo")) || {};
  save(candidates([r.url, tab.url, r.src]), tab.url, tab);
});

// ツールバーのボタン：今のページの動画を保存
chrome.action.onClicked.addListener((tab) => {
  if (tab && /^https?:/.test(tab.url || "")) save([tab.url], tab.url, tab);
});
