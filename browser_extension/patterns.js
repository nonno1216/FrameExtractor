// 動画ページの URL かどうか（右クリックした場所から、保存する URL の候補を選ぶのに使う）
// ここに無いサイトでも、アプリ側（yt-dlp）がページの URL から動画を探します
function isVideoUrl(u) {
  let x;
  try {
    x = new URL(u);
  } catch (e) {
    return false;
  }
  const h = x.hostname.replace(/^(www|m|mobile|music)\./, "");
  const p = x.pathname;
  const on = (d) => h === d || h.endsWith("." + d);
  if (h === "youtu.be" || h === "dai.ly" || h === "nico.ms" || h === "v.redd.it" || h === "clips.twitch.tv") return p.length > 1;
  if (on("youtube.com")) return (p === "/watch" && x.searchParams.has("v")) || /^\/(shorts|live|embed)\/[^/]/.test(p);
  if (on("x.com") || on("twitter.com")) return /\/status\/\d+/.test(p);
  if (on("tiktok.com")) return /\/video\/\d+/.test(p);
  if (on("instagram.com")) return /\/(p|reel|reels|tv)\/[^/]+/.test(p);
  if (on("nicovideo.jp")) return /\/watch\/\w+/.test(p);
  if (on("vimeo.com")) return /^\/(video\/)?\d+/.test(p);
  if (on("bilibili.com")) return /\/video\//.test(p);
  if (on("twitch.tv")) return /\/videos\/\d+|\/clip\//.test(p);
  if (on("facebook.com")) return /\/(watch|videos|reel)\b/.test(p);
  if (on("dailymotion.com")) return /\/video\//.test(p);
  if (on("streamable.com")) return /^\/([eo]\/)?[a-z0-9]+$/i.test(p);
  if (on("reddit.com")) return /\/comments\//.test(p);
  return false;
}
