# -*- coding: utf-8 -*-
"""音乐库.html 结构自检 —— 改完就运行一次，别靠肉眼。

它专抓"改一处、崩另一处"这类事故，也就是改这个单文件最容易犯的错：
  · $('xxx') 引用了某个 id，但那个元素已经被删掉（我之前删掉两个"关于"面板时就是这类风险）；
  · 内联 JS 语法错误（双击打开只看到空白页，控制台报错一闪而过）；
  · CSS 括号不配平，后面的样式整段失效；
  · <script>/<style> 标签不闭合。

用法：
    python 音乐库.py 检查            # 检查，通过则退出码 0
    python 音乐库.py 检查 --backup   # 检查 + 先把当前文件备份成 音乐库.备份-<时间>.html
"""
import re
import sys
import json
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TARGET = HERE / "音乐库.html"
LYRIC_DIR = HERE / "_lyrics"
BAK_DIR = HERE / "_备份"          # --backup 的副本也放进备份目录，别堆在库根目录

if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

problems = []
notes = []


def fail(msg):
    problems.append(msg)


def ok(msg):
    notes.append(msg)


def read_html():
    if not TARGET.exists():
        fail(f"找不到文件：{TARGET}")
        return None
    raw = TARGET.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        fail("文件带 UTF-8 BOM，浏览器里可能出现多余空行")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        fail(f"不是合法 UTF-8：{e}")
        return None


def check_backup():
    now = datetime.now().strftime("%Y%m%d-%H%M%S")
    BAK_DIR.mkdir(parents=True, exist_ok=True)
    dst = BAK_DIR / f"音乐库.备份-{now}.html"
    shutil.copy2(TARGET, dst)
    ok(f"已备份 -> {dst.name}")


def check_tags(html):
    # 先剥掉 style / script 的内容，再数外层标签：
    # 注释里提到一句 <script> 不该算"未闭合"；真没闭合的标签剥不掉，会残留下来被数到。
    pat = r"(?s)<(style|script)\b[^>]*>.*?</\1>"
    pairs = [p.lower() for p in re.findall(pat, html)]
    stripped = re.sub(pat, "", html)
    for tag in ("style", "script"):
        inside = pairs.count(tag)
        opens = len(re.findall(rf"<{tag}\b", stripped, re.I)) + inside
        closes = len(re.findall(rf"</{tag}>", stripped, re.I)) + inside
        if opens != closes:
            fail(f"<{tag}> 标签不配对：开 {opens} / 闭 {closes}")
        else:
            ok(f"<{tag}> 标签配对（{opens} 组）")


def check_css(html):
    css = "\n".join(re.findall(r"(?s)<style>(.*?)</style>", html))
    if not css.strip():
        fail("没有找到 <style> 内容")
        return
    bal = 0
    line = 1
    for ch in css:
        if ch == "\n":
            line += 1
        elif ch == "{":
            bal += 1
        elif ch == "}":
            bal -= 1
            if bal < 0:
                fail(f"CSS 第 {line} 行附近出现多余的 }}")
                return
    if bal != 0:
        fail(f"CSS 花括号不配平，少 {bal} 个 }}")
    else:
        ok(f"CSS 花括号配平（{len(css.splitlines())} 行样式）")


def check_js(html):
    blocks = re.findall(r"(?s)<script>(.*?)</script>", html)
    if not blocks:
        fail("没有找到内联 <script>")
        return None
    node = shutil.which("node")
    if not node:
        fail("没找到 node，无法做 JS 语法检查（装个 Node.js 或手动跳过）")
        return blocks[0]
    for i, code in enumerate(blocks, 1):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(code)
            tmp = f.name
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True, encoding="utf-8", errors="replace")
        Path(tmp).unlink(missing_ok=True)
        if r.returncode != 0:
            fail(f"内联脚本第 {i} 段语法错误：\n{(r.stderr or '').strip()[:800]}")
        else:
            ok(f"内联脚本第 {i} 段语法通过（{len(code)} 字符）")
    return "\n".join(blocks)


def check_dom_refs(html, js):
    """$('id') / getElementById('id') 引用的 id 必须真的存在。"""
    if not js:
        return
    ids = set(re.findall(r"""\bid\s*=\s*["']([^"']+)["']""", html))
    used = set(re.findall(r"""\$\(\s*['"]([A-Za-z0-9_\-]+)['"]\s*\)""", js))
    used |= set(re.findall(r"""getElementById\(\s*['"]([A-Za-z0-9_\-]+)['"]\s*\)""", js))
    missing = sorted(u for u in used if u not in ids)
    if missing:
        fail("JS 引用了不存在的元素 id：" + "、".join(missing))
    else:
        ok(f"DOM 引用全部存在（{len(used)} 个 id：{'、'.join(sorted(used))}）")


def check_globals(html):
    """必需的全局数据/接口还在不在。"""
    for name in ("LIB_DATA", "LIB_DUR"):
        if name not in html:
            fail(f"页面里没有出现 {name}，对应的 .js 可能没被引用")
        else:
            ok(f"引用 {name} 正常")
    core = ["function openPlayer", "function closePlayer", "function renderLyrics", "function syncLyric"]
    missing = [c for c in core if c not in html]
    if missing:
        fail("核心函数丢失：" + "、".join(missing))
    else:
        ok("核心函数齐全（openPlayer / closePlayer / renderLyrics / syncLyric）")


def check_lyrics(html):
    """离线歌词接线是否完整（缺了只是退化成联网搜，不算致命，但要提示）。"""
    if "window.LIB_LYR" not in html:
        fail("页面没有读取 window.LIB_LYR，离线歌词库不会被用上")
    else:
        ok("已接线 window.LIB_LYR（离线歌词库）")
    if "_lyrics/a" not in html:
        fail("页面没有拼出 _lyrics/a<专辑下标>.js 的分片路径")
    else:
        ok("分片路径 _lyrics/a<专辑下标>.js 正常")
    if LYRIC_DIR.is_dir():
        shards = list(LYRIC_DIR.glob("a*.js"))
        state = LYRIC_DIR / "_state.json"
        hit = miss = 0
        if state.exists():
            try:
                done = json.loads(state.read_text(encoding="utf-8")).get("done", {})
                hit = sum(1 for v in done.values() if v == "hit")
                miss = sum(1 for v in done.values() if v == "miss")
            except Exception as e:
                fail(f"_state.json 读不出来：{e}")
        ok(f"歌词分片 {len(shards)} 个，已抓 {hit + miss} 首（命中 {hit} / 未命中 {miss}）")
    else:
        notes.append("_lyrics/ 还不存在（音乐库.py 歌词 尚未跑过）")


def check_data_json():
    """data.js 只读校验，确认曲库本身没被写坏。"""
    dj = HERE / "data.js"
    if not dj.exists():
        return
    txt = dj.read_text(encoding="utf-8", errors="replace")
    if not txt.startswith("window.LIB_DATA="):
        fail("data.js 开头不是 window.LIB_DATA=")
        return
    body = txt[len("window.LIB_DATA="):].rstrip().rstrip(";")
    try:
        data = json.loads(body)
    except Exception as e:
        fail(f"data.js 不是合法 JSON：{e}")
        return
    albums = data.get("albums", [])
    tracks = sum(len(a.get("tracks", [])) for a in albums)
    ok(f"data.js 正常：{len(albums)} 张专辑 / {tracks} 首曲目")


def main():
    if "--backup" in sys.argv:
        check_backup()
    html = read_html()
    if html:
        check_tags(html)
        check_css(html)
        js = check_js(html)
        check_dom_refs(html, js)
        check_globals(html)
        check_lyrics(html)
        check_data_json()

    for n in notes:
        print("  OK   " + n)
    if problems:
        print()
        for p in problems:
            print("  FAIL " + p)
        print(f"\n{len(problems)} 项未通过 —— 先别用，回去看上面的提示。")
        return 1
    print(f"\n全部通过（{len(notes)} 项检查）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
