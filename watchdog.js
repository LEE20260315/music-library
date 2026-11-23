'use strict';
/*
 * 音乐库 · 看门狗
 * ------------------------------------------------------------
 * 为什么需要它：2026-11-23 早上 8099 服务自己退了，隧道还在跑，
 * 于是 Cloudflare 一直回 502，手机上看着就像"网站坏了"。
 * 那台机器内存常年吃紧（16GB 里只剩 1.7GB），进程被清理掉不是怪事。
 *
 * 这个看门狗只做一件事：每隔一会儿探一下 8099，探不通就把它拉起来。
 * 只管 8099 这个 HTTP 服务；隧道那边由 tunnel.js 自己负责重启。
 *
 * 用法：node watchdog.js
 */

const { spawn, execFile } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const NODE = process.execPath;
const SERVER = path.join(ROOT, 'music-server.js');
const PORT = 8099;
const LOG_DIR = path.join(ROOT, '_logs');
const LOG = path.join(LOG_DIR, 'watchdog.log');
const PID_FILE = path.join(LOG_DIR, 'watchdog.pid');

const INTERVAL = 30 * 1000;   // 每 30 秒探一次
const MAX_FAILS = 2;          // 连续两次探不通才动手，避免偶发抖动就重启
const GRACE = 12 * 1000;      // 刚拉起后给 12 秒启动时间

fs.mkdirSync(LOG_DIR, { recursive: true });

function stamp() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

function log(line) {
  try { fs.appendFileSync(LOG, '[' + stamp() + '] ' + line + '\n'); } catch (_) {}
}

/* 日志别无限长：超过 1MB 就留最后 2000 行重写一次 */
function trimLog() {
  try {
    const st = fs.statSync(LOG);
    if (st.size < 1024 * 1024) return;
    const lines = fs.readFileSync(LOG, 'utf8').split('\n');
    fs.writeFileSync(LOG, lines.slice(-2000).join('\n'), 'utf8');
  } catch (_) {}
}

/* ---------- 单实例锁 ---------- */
function isAlive(pid) {
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; }
}
try {
  if (fs.existsSync(PID_FILE)) {
    const old = parseInt(fs.readFileSync(PID_FILE, 'utf8').trim(), 10);
    if (old && old !== process.pid && isAlive(old)) {
      log('已经有一个看门狗在跑（PID ' + old + '），本次启动放弃。');
      process.exit(0);
    }
  }
} catch (_) {}
try { fs.writeFileSync(PID_FILE, String(process.pid), 'utf8'); } catch (_) {}

/* ---------- 探测 ---------- */
function probe(cb) {
  const req = http.request(
    { host: '127.0.0.1', port: PORT, path: '/', method: 'GET', timeout: 8000 },
    res => { res.resume(); cb(null, res.statusCode); }
  );
  req.on('timeout', () => { req.destroy(); cb(new Error('timeout')); });
  req.on('error', e => cb(e));
  req.end();
}

/* ---------- 找 8099 的占用者（有时端口被占着但进程已经僵了） ---------- */
const SERVER_PID_FILE = path.join(LOG_DIR, 'server.pid');
function ourServerPid() {
  try { return parseInt(fs.readFileSync(SERVER_PID_FILE, 'utf8').trim(), 10) || 0; } catch (e) { return 0; }
}

/* 只收自己家的进程。done(blocked)：true 表示端口被陌生进程占着，别去重启。
   以前这里不看身份，端口上有谁就 taskkill 谁——万一 8099 被别的程序先占了
   （同事另一个 node 项目、某个调试工具），看门狗会替别人收尸，那是不该动的。 */
function killPortHolder(done) {
  execFile('netstat', ['-ano'], { windowsHide: true }, (e, out) => {
    if (e) return done(false);
    const pids = new Set();
    for (const line of String(out).split('\n')) {
      if (!/:8099\s/.test(line)) continue;
      if (!/LISTENING/i.test(line)) continue;
      const m = line.trim().match(/(\d+)\s*$/);
      if (m) pids.add(m[1]);
    }
    if (!pids.size) return done(false);
    const mine = ourServerPid();
    let left = pids.size, blocked = false, killed = 0;
    const step = () => { if (--left === 0) setTimeout(() => done(blocked), killed ? 1200 : 0); };
    for (const pid of pids) {
      const n = Number(pid);
      if (n === process.pid) { step(); continue; }
      if (!mine || n !== mine) {
        blocked = true;
        log('端口 8099 被 PID ' + pid + ' 占着，但它不是本服务（server.pid 记的是 ' + (mine || '空') + '），不动它');
        step();
        continue;
      }
      killed++;
      log('端口 8099 的 PID ' + pid + ' 是本服务留下的，结束它');
      execFile('taskkill', ['/F', '/PID', pid], { windowsHide: true }, step);
    }
  });
}

/* ---------- 拉起服务器 ---------- */
let child = null;
let startingSince = 0;

function startServer(reason) {
  if (child && isAlive(child.pid)) {
    log('已有子进程 PID ' + child.pid + '，不重复启动');
    return;
  }
  log('启动 music-server.js' + (reason ? '（' + reason + '）' : ''));
  startingSince = Date.now();
  child = spawn(NODE, [SERVER, String(PORT)], {
    cwd: ROOT,
    windowsHide: true,
    stdio: 'ignore'
  });
  child.on('exit', code => {
    log('music-server.js 退出，code=' + code);
    child = null;
    /* 5 秒后由下一轮探测自然重试，这里不递归调用避免雪崩 */
  });
  child.on('error', e => {
    log('启动失败：' + e.message);
    child = null;
  });
}

/* ---------- 主循环 ---------- */
let fails = 0;
let lastOk = 0;

function tick() {
  trimLog();
  probe((err, code) => {
    /* 刚拉起来那几秒探不通是正常的，别急着判死 */
    if (startingSince && Date.now() - startingSince < GRACE) {
      log('启动中，等待…');
      return;
    }

    if (!err && code >= 200 && code < 500) {
      if (fails > 0) log('服务已恢复（HTTP ' + code + '）');
      fails = 0;
      lastOk = Date.now();
      return;
    }

    fails++;
    const why = err ? err.message : ('HTTP ' + code);
    log('探测失败 ' + fails + '/' + MAX_FAILS + '：' + why);

    if (fails < MAX_FAILS) return;
    fails = 0;
    startingSince = 0;

    /* 两次都不通：先把可能僵住的占用者收掉，再重新拉。
       端口被别人占着时不硬上——起也起不来，还会把日志刷满。 */
    killPortHolder(blocked => {
      if (blocked) {
        log('8099 被非本服务的进程占着，本轮不重启（手动看一下是谁在用这个端口）');
        return;
      }
      startServer('探测连续失败');
    });
  });
}

process.on('SIGINT', () => { if (child) child.kill(); try { fs.unlinkSync(PID_FILE); } catch (_) {} process.exit(0); });
process.on('SIGTERM', () => { if (child) child.kill(); try { fs.unlinkSync(PID_FILE); } catch (_) {} process.exit(0); });

/* 启动时先探一次：通了就不管它（别把已经在跑的服务顶掉） */
probe((err, code) => {
  if (!err && code >= 200 && code < 500) {
    log('看门狗启动，8099 已在正常服务（HTTP ' + code + '），只做监视。');
  } else {
    log('看门狗启动，8099 不通（' + (err ? err.message : 'HTTP ' + code) + '），立即拉起。');
    startServer('开机时服务没在跑');
  }
});

setInterval(tick, INTERVAL);
log('看门狗已就绪，每 ' + (INTERVAL / 1000) + ' 秒检查一次。');
