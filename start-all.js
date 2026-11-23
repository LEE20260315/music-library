'use strict';
/*
 * 音乐库 · 启动全部（脱离会话的入口）
 * ------------------------------------------------------------
 * 为什么需要这个文件：
 * 用 win_bash / 终端 直接 `node xxx.js &` 起的进程，会挂在那个 shell 的
 * 进程树下。shell 一超时被停掉，子树跟着一起被杀——2026-11-23 早上
 * 8099 服务两次神秘消失就是这么来的（偏偏隧道还活着，于是 502）。
 *
 * 这个脚本自己再 fork 出两个真正独立的子进程（detached + unref），
 * 然后它自己立刻退出。父进程死了也带不走它们。
 *
 * 拉起的两样东西：
 *   1. watchdog.js —— 盯着 8099，探不通就重新拉起 music-server.js
 *   2. tunnel.js   —— Cloudflare 隧道，断了会自己重连
 *
 * 用法：node start-all.js
 */

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const LOG_DIR = path.join(ROOT, '_logs');
fs.mkdirSync(LOG_DIR, { recursive: true });

const LOG = path.join(LOG_DIR, 'start-all.log');

function stamp() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}
function log(s) {
  try { fs.appendFileSync(LOG, '[' + stamp() + '] ' + s + '\n'); } catch (_) {}
}

function launch(script, label) {
  const out = fs.openSync(path.join(LOG_DIR, script.replace(/\.js$/, '.out.log')), 'a');
  const err = fs.openSync(path.join(LOG_DIR, script.replace(/\.js$/, '.out.log')), 'a');
  const child = spawn(process.execPath, [path.join(ROOT, script)], {
    cwd: ROOT,
    detached: true,      // 关键：脱离父进程的进程组
    windowsHide: true,
    stdio: ['ignore', out, err]
  });
  child.unref();         // 关键：父进程不等它，可以直接退出
  log('已启动 ' + label + '（' + script + '），PID ' + child.pid);
  return child.pid;
}

// 双击运行时窗口一闪而过也看不到东西，结果都写进 _logs/start-all.log
log('========== 启动全部 ==========');

const wd = launch('watchdog.js', '看门狗');
const tn = launch('tunnel.js', '隧道');

log('完成。看门狗 PID ' + wd + '，隧道 PID ' + tn);
log('日志：_logs/watchdog.log / _logs/cloudflared.log / _logs/access.log');

process.exit(0);
