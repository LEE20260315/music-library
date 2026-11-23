'use strict';
/*
 * 音乐库 · Cloudflare 临时隧道守护
 * ------------------------------------------------------------
 * 作用：把本机的 8099 端口通过 cloudflared quick tunnel 暴露到公网，
 *      并把「本次分配到的 https 网址」写进 _隧道地址.txt，
 *      这样网址变了也不用去翻控制台。
 *
 * quick tunnel 的网址每次启动都会变，这是 Cloudflare 的规则，不是 bug。
 * 想要固定网址必须用命名隧道（需要自有域名托管在 Cloudflare）。
 *
 * 用法：node tunnel.js
 */

const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const CF = path.join(ROOT, '_下载', 'cloudflared', 'cloudflared.exe');
const TARGET = 'http://127.0.0.1:8099';
const ADDR_FILE = path.join(ROOT, '_隧道地址.txt');
const LOG_DIR = path.join(ROOT, '_logs');
const LOG = path.join(LOG_DIR, 'cloudflared.log');

fs.mkdirSync(LOG_DIR, { recursive: true });

function stamp() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

function log(line) {
  const s = '[' + stamp() + '] ' + line + '\n';
  try { fs.appendFileSync(LOG, s); } catch (_) {}
  process.stdout.write(s);
}

function writeAddr(url, note) {
  const text =
    'Cloudflare 临时隧道\n' +
    '==============================\n' +
    url + '\n\n' +
    '生成时间：' + stamp() + '\n' +
    note + '\n\n' +
    '说明：\n' +
    '  · 这个网址每次重启 tunnel.js 都会变，变了就重新看本文件。\n' +
    '  · 手机浏览器直接打开即可，不用装任何 App、不用登录。\n' +
    '  · 本网址是公开的：拿到链接的人都能访问并下载音乐。\n' +
    '  · 想固定网址，需要用「命名隧道」+ 自己的域名。\n\n' +
    '局域网：http://<本机局域网IP>:8099/\n';
  try {
    fs.writeFileSync(ADDR_FILE, text, 'utf8');
    log('网址已写入 ' + path.basename(ADDR_FILE));
  } catch (e) {
    log('写 ' + path.basename(ADDR_FILE) + ' 失败：' + e.message);
  }
}

if (!fs.existsSync(CF)) {
  log('找不到 cloudflared.exe：' + CF);
  process.exit(1);
}

/*
 * 单实例锁：误双击两次不要开出两条隧道白占带宽。
 * 用 _logs/tunnel.pid 记进程号，启动时先看上一个还活着没。
 */
const PID_FILE = path.join(LOG_DIR, 'tunnel.pid');
function isAlive(pid) {
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; }
}
try {
  if (fs.existsSync(PID_FILE)) {
    const old = parseInt(fs.readFileSync(PID_FILE, 'utf8').trim(), 10);
    if (old && old !== process.pid && isAlive(old)) {
      log('已经有一条隧道在跑（PID ' + old + '），本次启动放弃。');
      process.exit(0);
    }
  }
} catch (_) {}
try { fs.writeFileSync(PID_FILE, String(process.pid), 'utf8'); } catch (_) {}

function releasePid() {
  try {
    const cur = parseInt(fs.readFileSync(PID_FILE, 'utf8').trim(), 10);
    if (cur === process.pid) fs.unlinkSync(PID_FILE);
  } catch (_) {}
}

let child = null;
let stopping = false;
let lastUrl = '';
let restarts = 0;

function start() {
  log('启动 cloudflared → ' + TARGET);
  child = spawn(CF, ['tunnel', '--url', TARGET, '--no-autoupdate'], {
    cwd: path.dirname(CF),
    windowsHide: true,
  });

  const onData = buf => {
    const text = buf.toString('utf8');
    // 原样记进日志，方便事后排查（cloudflared 主要写 stderr）
    try { fs.appendFileSync(LOG, text); } catch (_) {}
    const m = text.match(/https:\/\/[a-z0-9][a-z0-9-]*\.trycloudflare\.com/gi);
    if (m) {
      for (const raw of m) {
        const url = raw.replace(/\/+$/, '');
        if (url === lastUrl) continue;
        lastUrl = url;
        log('分配到的网址：' + url);
        writeAddr(url, '这是 cloudflared 本次启动分配到的临时网址。');
      }
    }
    if (/ERR_|failed to|error/i.test(text) && !m) {
      const line = text.trim().split('\n').slice(-1)[0];
      if (line) log('提示：' + line.slice(0, 300));
    }
  };

  child.stdout.on('data', onData);
  child.stderr.on('data', onData);

  child.on('exit', code => {
    if (stopping) { log('cloudflared 已退出（code ' + code + '）'); return; }
    restarts++;
    const wait = Math.min(5 + restarts * 5, 60);
    log('cloudflared 意外退出（code ' + code + '），' + wait + ' 秒后重启，第 ' + restarts + ' 次');
    setTimeout(start, wait * 1000);
  });

  child.on('error', e => {
    log('启动 cloudflared 失败：' + e.message);
  });
}

process.on('SIGINT', () => { stopping = true; releasePid(); if (child) child.kill(); process.exit(0); });
process.on('SIGTERM', () => { stopping = true; releasePid(); if (child) child.kill(); process.exit(0); });

/* 先确认 8099 上跑的是音乐库，再把端口挂到公网。
   以前不看就挂：万一 8099 被别的程序占了（或者服务自己还没起来），
   公网网址指向的就是那个东西——可能是一份源码目录、一个调试面板。 */
const WAIT = 15, TRIES = 20;
function waitReady(tries) {
  const req = http.request(
    { host: '127.0.0.1', port: 8099, path: '/api/ping', method: 'GET', timeout: 5000 },
    res => {
      let body = '';
      res.on('data', c => { body += c; if (body.length > 8192) req.destroy(); });
      res.on('end', () => {
        let j = null;
        try { j = JSON.parse(body); } catch (e) {}
        if (res.statusCode === 200 && j && j.ok === true && typeof j.albums === 'number') {
          log('8099 是音乐库（' + j.albums + ' 张专辑），开始建隧道');
          return start();
        }
        log('8099 上不是音乐库服务（HTTP ' + res.statusCode + '：' + body.slice(0, 80) + '），先不建隧道');
        retry();
      });
    }
  );
  req.on('timeout', () => req.destroy());
  req.on('error', e => { log('音乐库服务还没就绪（' + e.message + '），' + WAIT + ' 秒后再试'); retry(); });
  req.end();
  function retry() {
    if (tries <= 0) {
      log('等了太久，这次不建隧道。先确认 music-server.js 在跑（看门狗会自己拉）。');
      return process.exit(1);
    }
    setTimeout(() => waitReady(tries - 1), WAIT * 1000);
  }
}

waitReady(TRIES);
