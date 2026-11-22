#!/usr/bin/env node
/* 音乐库 HTTP 服务器 —— 给手机 / 平板 / 远程访问用。

   要点：
   1. Range（206）。手机播 <audio> 必须能分段取，否则拖不动进度条。
      python -m http.server 就是缺这个，所以不用它。
   2. 不改音频内容。服务器只负责把文件原封不动发出去，
      码流与 E 盘上的文件逐字节一致，无损就是真无损。
   3. gzip 文本。data.js 13.9MB 压完约 3.6MB，手机首次打开快好几秒。
   4. ETag / Last-Modified / 长缓存，回访不重复下载。

   播放端（音乐库.html）会先把整首完整下到本地再播，
   所以这里保证 Content-Length 一定带上，客户端才能显示下载进度。

   用法：  node music-server.js [端口]
   默认端口 8099，监听 0.0.0.0（局域网 + Tailscale 都能连）。
*/
'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const { URL } = require('url');

const ROOT = __dirname;
const PORT = parseInt(process.argv[2] || process.env.PORT || '8099', 10);
const INDEX = '音乐库.html';

/* 访问日志：手机端出问题的时候，桌面这边看不到它的控制台，
   只能靠服务器记下「它请求了没有、请求到了没有、拿到多少字节」。
   超过 5MB 自动轮转成 access.1.log，不占地方。 */
const LOGDIR = path.join(ROOT, '_logs');
const LOGFILE = path.join(LOGDIR, 'access.log');
try { fs.mkdirSync(LOGDIR, { recursive: true }); } catch (e) {}
function accessLog(line) {
  try {
    const st = fs.existsSync(LOGFILE) ? fs.statSync(LOGFILE) : null;
    if (st && st.size > 5 * 1024 * 1024) fs.renameSync(LOGFILE, path.join(LOGDIR, 'access.1.log'));
    fs.appendFile(LOGFILE, line + '\n', () => {});
  } catch (e) {}
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.htm': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.mjs': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.md': 'text/markdown; charset=utf-8',
  '.lrc': 'text/plain; charset=utf-8',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.flac': 'audio/flac',
  '.mp3': 'audio/mpeg',
  '.m4a': 'audio/mp4',
  '.aac': 'audio/aac',
  '.wav': 'audio/wav',
  '.ape': 'audio/x-ape',
  '.dsf': 'audio/x-dsf',
  '.dff': 'audio/x-dff',
  '.cue': 'text/plain; charset=utf-8',
  '.pdf': 'application/pdf'
};
const TEXTUAL = new Set(['.html', '.htm', '.js', '.mjs', '.css', '.json', '.txt', '.md', '.lrc', '.svg']);

function mime(p) {
  return MIME[path.extname(p).toLowerCase()] || 'application/octet-stream';
}

/* 解析 Range 头，只支持单段（浏览器播音频就只用单段）。返回 null 表示整体发送。 */
function parseRange(header, size) {
  if (!header) return null;
  const m = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!m) return null;
  let start, end;
  if (m[1] === '') {
    // bytes=-500  —— 最后 500 字节
    const n = parseInt(m[2], 10);
    if (!Number.isFinite(n) || n <= 0) return null;
    start = Math.max(0, size - n);
    end = size - 1;
  } else {
    start = parseInt(m[1], 10);
    end = m[2] === '' ? size - 1 : parseInt(m[2], 10);
  }
  if (!Number.isFinite(start) || !Number.isFinite(end) || start > end || start >= size) return 'invalid';
  return { start, end: Math.min(end, size - 1) };
}

function safeResolve(urlPath) {
  let rel;
  try {
    rel = decodeURIComponent(urlPath);
  } catch (e) {
    return null; // 非法转义
  }
  rel = rel.replace(/^\/+/, '');
  const full = path.resolve(ROOT, rel);
  const rootWithSep = ROOT.endsWith(path.sep) ? ROOT : ROOT + path.sep;
  if (full !== ROOT && !full.startsWith(rootWithSep)) return null; // 防目录穿越
  return full;
}

/* ===== 入库 API =====
   为什么需要它：页面里的「＋ 专辑入库」走的是浏览器的 File System Access API
   （showDirectoryPicker），而 Chrome 157 在 Windows 上有个已知回归——对话框
   弹出来、选完目录却立刻 reject 成 AbortError（Chromium issue 582927035 等）。
   入库这件事本来就不需要经过浏览器：服务器就在这台机器上，直接读写 E:\01-音乐。
   于是把扫描与写入放到服务端，「选目录」这一步整个省掉。
   只允许本机访问：远程（局域网 / Tailscale）打开页面只能听，不能写库。 */
const IMP_SKIP_DIRS = new Set(['_covers', '_lyrics', '.git', '__pycache__',
  'System Volume Information', '$RECYCLE.BIN', '_重复待删', '_原盘', '_损坏删除',
  '_删除清单', 'node_modules']);
/* 能被浏览器直接播的格式才算「能入库」：APE / DSF / DFF / WMA / TTA 这些
   Chrome 解不了，写进 data.js 就是一张点了没声音的专辑，不如当场说清楚。 */
const IMP_AUDIO_RE = /\.(flac|wav|ape|m4a|mp3|dsf|dff|wv|tta|aiff|alac)$/i;
const IMP_PLAY_RE = /\.(flac|wav|m4a|mp3|aac|ogg|opus)$/i;
const IMP_CUE_RE = /\.cue$/i;
const IMP_COVER_RE = /^(folder|cover|front|封面)\.(jpe?g|png|webp)$/i;
const LIB = path.join(ROOT, '无损');

function isLocal(req) {
  const a = (req.socket.remoteAddress || '').replace(/^::ffff:/, '');
  return a === '127.0.0.1' || a === '::1' || a === 'localhost';
}

/* data.js 就一句 window.LIB_DATA={…}; 只剥前缀，不碰内容 */
function readData() {
  const txt = fs.readFileSync(path.join(ROOT, 'data.js'), 'utf8');
  const i = txt.indexOf('{');
  const j = txt.lastIndexOf('}');
  return { data: JSON.parse(txt.slice(i, j + 1)), text: txt };
}

function listDirs(p) {
  let ents;
  try { ents = fs.readdirSync(p, { withFileTypes: true }); } catch (e) { return []; }
  return ents.filter(e => e.isDirectory() && !IMP_SKIP_DIRS.has(e.name)).map(e => e.name).sort();
}

/* 一个专辑目录里的音频与 cue。跟页面一样只下探两层（CD1/CD2 那种再套一层） */
function walkAlbum(dir, depth, prefix, out) {
  let ents;
  try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch (e) { return out; }
  for (const e of ents) {
    if (e.isFile()) {
      if (IMP_AUDIO_RE.test(e.name)) out.audio.push({ name: e.name, path: prefix + e.name });
      else if (IMP_CUE_RE.test(e.name)) out.cues.push(prefix + e.name);
      else if (IMP_COVER_RE.test(e.name) && !out.cover) out.cover = e.name;
    } else if (e.isDirectory() && depth < 2 && !IMP_SKIP_DIRS.has(e.name)) {
      walkAlbum(path.join(dir, e.name), depth + 1, prefix + e.name + '/', out);
    }
  }
  return out;
}

/* 目录名格式见 库维护须知.md：(年份) 专辑名 [位深-采样率] CD1 */
function parseDirName(dirname) {
  const ym = dirname.match(/^\((\d{4})\)\s*/);
  const year = ym ? ym[1] : '';
  const rest = ym ? dirname.slice(ym[0].length) : dirname;
  const qm = rest.match(/\[(\d{2,3})-([\d.]+)\]/);
  const quality = qm ? (+qm[1]) + 'bit / ' + qm[2] + 'kHz' : '';
  let album = rest.replace(/\s*\[\d{2,3}-[\d.]+\]/g, '').trim();
  const dm = album.match(/\s+(CD\s*\d+|Disc\s*\d+)$/i);
  const disc = dm ? dm[1].replace(/\s+/g, '') : '';
  if (dm) album = album.slice(0, dm.index).trim();
  return { year, quality, album: album || dirname, disc };
}

/* FLAC 头 42 字节里的 STREAMINFO，只读文件头不解码 */
function flacInfo(file) {
  let fd;
  try {
    fd = fs.openSync(file, 'r');
    const b = Buffer.alloc(42);
    fs.readSync(fd, b, 0, 42, 0);
    if (b.toString('latin1', 0, 4) !== 'fLaC') return null;
    const sr = (b[18] << 12) | (b[19] << 4) | (b[20] >> 4);
    const bps = (((b[20] & 1) << 4) | (b[21] >> 4)) + 1;
    return sr && bps ? { sr, bps } : null;
  } catch (e) { return null; } finally { if (fd !== undefined) try { fs.closeSync(fd); } catch (e) {} }
}
/* WAV 的 fmt 块：位深与采样率都在头 44 字节里，'fmt ' 从第 12 字节起 */
function wavInfo(file) {
  let fd;
  try {
    fd = fs.openSync(file, 'r');
    const b = Buffer.alloc(64);
    fs.readSync(fd, b, 0, 64, 0);
    if (b.toString('latin1', 0, 4) !== 'RIFF' || b.toString('latin1', 8, 12) !== 'WAVE') return null;
    let off = 12;
    while (off + 8 <= 64) {
      const id = b.toString('latin1', off, off + 4);
      const size = b.readUInt32LE(off + 4);
      if (id === 'fmt ') {
        const sr = b.readUInt32LE(off + 12), bits = b.readUInt16LE(off + 22);
        return sr && bits ? { sr, bps: bits } : null;
      }
      off += 8 + size + (size & 1);
    }
    return null;
  } catch (e) { return null; } finally { if (fd !== undefined) try { fs.closeSync(fd); } catch (e) {} }
}
function fmtKHz(sr) { const k = sr / 1000; return (Number.isInteger(k) ? k : k.toFixed(1)) + 'kHz'; }
function era(y) {
  const n = parseInt(y, 10);
  if (!n) return '未标年';
  if (n < 1960) return '60年代及以前';
  if (n < 2000) return (Math.floor(n / 10) % 10) + '0年代';
  return Math.floor(n / 10) * 10 + '年代';
}

function scanLibrary() {
  const { data } = readData();
  const known = new Set(data.albums.map(a => String(a.rel || '').replace(/\\/g, '/').toLowerCase()));
  const artists = listDirs(LIB);
  const fresh = [], pending = [], unplayable = [];
  let knownCount = 0;
  for (const artist of artists) {
    for (const dirname of listDirs(path.join(LIB, artist))) {
      const rel = '无损/' + artist + '/' + dirname;
      if (known.has(rel.toLowerCase())) { knownCount++; continue; }
      const out = walkAlbum(path.join(LIB, artist, dirname), 0, '', { audio: [], cues: [], cover: '' });
      if (!out.audio.length) continue;                     // 没有音频的目录（残留、空壳）不算
      const mi = parseDirName(dirname);
      let quality = mi.quality;
      if (!quality) {
        const f0 = path.join(LIB, artist, dirname, out.audio[0].name);
        const info = /\.wav$/i.test(f0) ? wavInfo(f0) : flacInfo(f0);
        if (info) quality = info.bps + 'bit / ' + fmtKHz(info.sr);
      }
      const item = {
        artist, dirname, rel, count: out.audio.length, cue: out.cues[0] || '', cover: out.cover,
        album: mi.album, year: mi.year, quality: quality || '', disc: mi.disc
      };
      // 整轨 + CUE：一个音频里装着整张碟，硬写进去会变成 1 首
      item.playable = out.audio.every(a => IMP_PLAY_RE.test(a.name));
      // 「整轨 + CUE」只有一个音频文件：硬写进去会变成 1 首，得先拆。
      // 有 cue 但音频本来就是分开的（如 Round-Up 13 个 wav + 1 个 cue），照常入库。
      if (out.cues.length && out.audio.length <= 1) pending.push(item);
      else if (!item.playable) unplayable.push(item);
      else fresh.push(item);
    }
  }
  return { fresh, pending, unplayable, knownCount, artistCount: artists.length, albums: data.albums.length };
}

function buildEntry(rel) {
  const parts = rel.split('/');
  const artist = parts[1], dirname = parts[2];
  const dir = path.join(LIB, artist, dirname);
  const out = walkAlbum(dir, 0, '', { audio: [], cues: [], cover: '' });
  const mi = parseDirName(dirname);
  const files = out.audio.slice().sort((a, b) => a.name.localeCompare(b.name, 'en', { numeric: true }));
  let quality = mi.quality;
  if (!quality) {
    for (const f of files) {
      const p0 = path.join(dir, f.path);
      const info = /\.wav$/i.test(p0) ? wavInfo(p0) : flacInfo(p0);
      if (info) { quality = info.bps + 'bit / ' + fmtKHz(info.sr); break; }
    }
  }
  return {
    artist, album: mi.album, year: mi.year, quality: quality || '未标注',
    disc: mi.disc, era: era(mi.year), genre: '其他',
    cover: out.cover || '', intro: '', artistInfo: '',
    rel: '无损/' + artist + '/' + dirname,
    tracks: files.map(f => ({
      n: f.name.replace(IMP_AUDIO_RE, '').replace(/^\s*\d{1,3}\s*[-._]\s*/, ''),
      p: f.path
    })),
    coverFile: ''
  };
}

/* 足迹规矩（库维护须知.md）：同一天只留一条，标题 ≤14 字，正文用「；」接在后面 */
function addUpdate(data, date, title, text) {
  data.updates = data.updates || [];
  const t = title.length > 14 ? title.slice(0, 14) : title;
  const day = data.updates.find(u => u.date === date);
  if (day) day.text = String(day.text || '').replace(/[。；;\s]+$/, '') + '；' + text;
  else data.updates.push({ date, title: t, text });
}

function commitImport(rels) {
  const { data } = readData();
  const have = new Set(data.albums.map(a => String(a.rel || '').replace(/\\/g, '/').toLowerCase()));
  const picked = [];
  for (const rel of rels) {
    const key = String(rel).replace(/\\/g, '/').toLowerCase();
    if (have.has(key)) continue;                        // 已经有了，绝不重复追加
    if (!fs.existsSync(path.join(ROOT, rel))) continue;
    picked.push(buildEntry(rel));
  }
  if (!picked.length) throw new Error('没有可写入的专辑（都已在库里或目录已消失）');
  const now = new Date(), p2 = v => String(v).padStart(2, '0');
  const stamp = now.getFullYear() + p2(now.getMonth() + 1) + p2(now.getDate()) + '-' + p2(now.getHours()) + p2(now.getMinutes());
  const day = now.getFullYear() + '-' + p2(now.getMonth() + 1) + '-' + p2(now.getDate());
  const bak = path.join(ROOT, 'data.js.bak-' + stamp);
  fs.copyFileSync(path.join(ROOT, 'data.js'), bak);
  for (const al of picked) data.albums.push(al);       // 只能 push 到末尾：_lyrics 分片按数组下标命名
  data.generated = day;
  const trk = picked.reduce((k, a) => k + a.tracks.length, 0);
  addUpdate(data, day, '新专辑入库 ' + picked.length + ' 张',
    '新增 ' + picked.length + ' 张（' + trk + ' 曲）：' +
    picked.map(a => a.artist + '《' + a.album + '》').slice(0, 4).join('、') +
    (picked.length > 4 ? ' 等' : ''));
  const txt = 'window.LIB_DATA=' + JSON.stringify(data) + ';';
  fs.writeFileSync(path.join(ROOT, 'data.js'), txt, 'utf8');
  const u = data.updates.find(x => x.date === day);
  // 把写入的条目原样回给页面：页面据此就地更新，不必整页重载
  return { added: picked.length, tracks: trk, backup: path.basename(bak), total: data.albums.length, update: u, albums: picked };
}

function sendJson(res, code, obj) {
  const body = Buffer.from(JSON.stringify(obj), 'utf8');
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': body.length, 'Cache-Control': 'no-store' });
  res.end(body);
}

function handleApi(req, res, pathname) {
  const local = isLocal(req);
  if (pathname === '/api/ping') {
    const d = readData();
    return sendJson(res, 200, { ok: true, local, albums: d.data.albums.length });
  }
  if (!local) return sendJson(res, 403, { error: '入库只能在本机做（用 http://127.0.0.1:8099/ 打开）' });
  if (pathname === '/api/import/scan') {
    try { return sendJson(res, 200, scanLibrary()); }
    catch (e) { return sendJson(res, 500, { error: String(e && e.message || e) }); }
  }
  if (pathname === '/api/import/commit' && req.method === 'POST') {
    let body = '';
    req.on('data', c => { body += c; if (body.length > 1e6) req.destroy(); });
    req.on('end', () => {
      let rels;
      try { rels = JSON.parse(body || '{}').rels; } catch (e) { return sendJson(res, 400, { error: '请求体不是 JSON' }); }
      if (!Array.isArray(rels) || !rels.length) return sendJson(res, 400, { error: '没有给出要入库的目录' });
      try { return sendJson(res, 200, commitImport(rels)); }
      catch (e) { return sendJson(res, 500, { error: String(e && e.message || e) }); }
    });
    return;
  }
  return sendJson(res, 404, { error: '未知接口' });
}

const server = http.createServer((req, res) => {
  const started = Date.now();
  const method = req.method;

  let pathname;
  try {
    pathname = new URL(req.url, 'http://x').pathname;
  } catch (e) {
    res.writeHead(400).end('Bad Request');
    return;
  }

  // 入库接口：先于静态文件处理，POST 也只在这里合法
  if (pathname.startsWith('/api/')) return handleApi(req, res, pathname);

  if (method !== 'GET' && method !== 'HEAD') {
    res.writeHead(405, { Allow: 'GET, HEAD', 'Content-Type': 'text/plain; charset=utf-8' });
    return res.end('Method Not Allowed');
  }

  // 根路径直接跳到播放器；目录也跳（省得看到 Python 那种目录列表）
  if (pathname === '/' || pathname === '') {
    res.writeHead(302, { Location: '/' + encodeURIComponent(INDEX) });
    return res.end();
  }

  const full = safeResolve(pathname);
  if (!full) {
    res.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8' });
    return res.end('Forbidden');
  }

  fs.stat(full, (err, st) => {
    if (err || !st.isFile()) {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
      return res.end('404 Not Found');
    }

    const ext = path.extname(full).toLowerCase();
    const etag = 'W/"' + st.size.toString(16) + '-' + Math.floor(st.mtimeMs).toString(16) + '"';
    const lastMod = st.mtime.toUTCString();

    // 条件请求：省流量（手机流量 + 13.9MB 的 data.js 很值钱）
    const inm = req.headers['if-none-match'];
    const ims = req.headers['if-modified-since'];
    if ((inm && inm === etag) || (!inm && ims && new Date(ims).getTime() >= Math.floor(st.mtimeMs / 1000) * 1000)) {
      res.writeHead(304, { ETag: etag, 'Last-Modified': lastMod });
      return res.end();
    }

    const headers = {
      'Content-Type': mime(full),
      'Accept-Ranges': 'bytes',
      ETag: etag,
      'Last-Modified': lastMod
    };

    // 封面和图：长期缓存。音频：允许缓存但每次校验。html/js：每次校验（改了就立刻生效）。
    const decoded = decodeURIComponent(pathname);
    if (/^\/(_covers|无损)\//i.test(decoded)) {
      headers['Cache-Control'] = (ext === '.jpg' || ext === '.png' || ext === '.webp')
        ? 'public, max-age=31536000, immutable'
        : 'public, max-age=86400';
    } else {
      headers['Cache-Control'] = 'no-cache';
    }

    const range = parseRange(req.headers.range, st.size);
    if (range === 'invalid') {
      res.writeHead(416, { 'Content-Range': 'bytes */' + st.size });
      return res.end();
    }

    // 文本压缩：data.js 13.9MB 压完约 3.6MB，手机第一次打开差好几秒
    const acceptGzip = /\bgzip\b/.test(req.headers['accept-encoding'] || '');
    const canGzip = !range && TEXTUAL.has(ext) && st.size > 2048 && acceptGzip;

    const streamOpts = range ? { start: range.start, end: range.end } : {};
    if (range) {
      headers['Content-Range'] = 'bytes ' + range.start + '-' + range.end + '/' + st.size;
      headers['Content-Length'] = range.end - range.start + 1;
      res.writeHead(206, headers);
    } else if (canGzip) {
      headers['Content-Encoding'] = 'gzip';
      headers['Vary'] = 'Accept-Encoding';
      res.writeHead(200, headers);
    } else {
      // 客户端靠这个 Content-Length 算下载进度
      headers['Content-Length'] = st.size;
      res.writeHead(200, headers);
    }

    if (method === 'HEAD') return res.end();

    const src = fs.createReadStream(full, streamOpts);
    src.on('error', () => { try { res.destroy(); } catch (e) {} });

    if (canGzip) {
      const gz = zlib.createGzip({ level: 6 });
      gz.on('error', () => { try { res.destroy(); } catch (e) {} });
      src.pipe(gz).pipe(res);
    } else {
      src.pipe(res);
    }

    res.on('finish', () => {
      const ms = Date.now() - started;
      // 只记有意义的那几行：html/js/音频/封面墙，favicon 之类不记
      const p = decodeURIComponent(req.url.split('?')[0]);
      if (/^\/($|音乐库\.html|data\.js|durations\.js|_covers\/|无损\/)/.test(p)) {
        accessLog([
          new Date().toISOString(),
          (req.headers['x-forwarded-for'] || req.socket.remoteAddress || '-').replace(/^::ffff:/, ''),
          res.statusCode,
          (st.size / 1048576).toFixed(1) + 'MB',
          ms + 'ms',
          (req.headers.range || '-'),
          p.slice(0, 120)
        ].join('\t'));
      }
      if (ms > 1500) console.log('[慢]', p, (st.size / 1048576).toFixed(1) + 'MB', ms + 'ms');
    });
  });
});

server.keepAliveTimeout = 65000;
server.headersTimeout = 70000;

server.listen(PORT, '0.0.0.0', () => {
  console.log('音乐库服务器已启动（原样发送，不做任何转码）');
  console.log('  本机      http://127.0.0.1:' + PORT + '/');
  console.log('  局域网    http://<本机局域网IP>:' + PORT + '/');
  console.log('  Tailscale http://nztz-001.geep-cirius.ts.net/');
  console.log('  目录      ' + ROOT);
});

process.on('uncaughtException', e => console.error('未捕获异常:', e && e.message));
