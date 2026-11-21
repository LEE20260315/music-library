#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补歌词.py — 从 lrclib.net 抓同步歌词，落成本地分片 JS（离线可用）。

用法:
  python 补歌词.py                  # 全量
  python 补歌词.py --limit 40      # 只处理 40 首未处理项
  python 补歌词.py --jobs 4        # 全局并发，默认 4（上限 4）
  python 补歌词.py --album 123     # 调试单张专辑（专辑下标）
  python 补歌词.py --min-score 3   # 匹配分阈值，默认 3

Ctrl+C 随时中断：进度落盘 _lyrics/_state.json，重跑自动续。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LYR_DIR = os.path.join(BASE_DIR, "_lyrics")
STATE_PATH = os.path.join(LYR_DIR, "_state.json")
REPORT_PATH = os.path.join(LYR_DIR, "_report.md")
DATA_PATH = os.path.join(BASE_DIR, "data.js")

API = "https://lrclib.net/api/search"
LIB_PREFIX = "window.LIB_DATA="
SHARD_PREFIX = "window.LIB_LYR=window.LIB_LYR||{};"
HDRS = {
    "User-Agent": "bu-geci/1.0 (offline music library lyrics fetcher)",
    "Accept": "application/json",
}
TS_PAT = re.compile(r"\[\d{1,2}:\d{2}")
SHARD_RE = re.compile(r"^" + re.escape(SHARD_PREFIX) + r"window\.LIB_LYR\[(\d+)\]=(\{.*\});$")
NOISE_PAT = re.compile(
    r"[\(\[\{（【]\s*(?:feat|ft|featuring|with|remaster(?:ed)?|re-?record(?:ed)?|"
    r"live|deluxe|bonus|version|ver\.?|mono|stereo|single|edit|expanded|"
    r"anniversary|edition|explicit|radio|remix|mix|instrumental|acoustic|demo|outtake)"
    r"[^\)\]\}）】]*[\)\]\}）】]",
    re.I,
)
FEAT_PAT = re.compile(r"\s*(?:feat|ft)\.?\s+.*$", re.I)


def norm(s):
    """归一化：小写、去 (feat.)/(Remastered) 等括号噪声、去 feat. 尾巴、去 ' - '、去标点。"""
    if not s:
        return ""
    s = str(s).strip().lower()
    s = NOISE_PAT.sub(" ", s)
    s = FEAT_PAT.sub(" ", s)
    s = s.replace(" - ", " ").replace("—", " ").replace("–", " ")
    s = re.sub(r"[^\w]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def score_cand(c, tn, an, aln):
    """曲名完全相同 +2 / 互相包含 +1；艺人同上；专辑名互相包含 +1。"""
    s = 0
    ct = norm(c.get("trackName"))
    ca = norm(c.get("artistName"))
    cal = norm(c.get("albumName"))
    if tn and ct:
        if ct == tn:
            s += 2
        elif ct in tn or tn in ct:
            s += 1
    if an and ca:
        if ca == an:
            s += 2
        elif ca in an or an in ca:
            s += 1
    if aln and cal and (cal in aln or aln in cal):
        s += 1
    return s


def name_ok(c, tn):
    ct = norm(c.get("trackName"))
    return bool(ct and tn and (ct == tn or ct in tn or tn in ct))


def decide(results, tn, an, aln, min_score):
    """选最高分且 >= 阈值的候选；instrumental / 纯歌词 / 无时间戳一律不算命中。

    额外要求「曲名必须有证据」：艺人(2)+专辑(1)=3 也能到阈值，
    那等于把同专辑里另一首歌的歌词错配给这首，比没有歌词更糟。
    """
    best_s, best_lrc = -1, None
    for c in results or []:
        if c.get("instrumental"):
            continue
        lrc = (c.get("syncedLyrics") or "").strip()
        if not lrc or not TS_PAT.search(lrc):
            continue
        if not name_ok(c, tn):
            continue
        s = score_cand(c, tn, an, aln)
        if s > best_s:
            best_s, best_lrc = s, lrc
    if best_s >= min_score:
        return "hit", best_lrc
    return "miss", None


def fetch_search(track, artist, album):
    qs = urllib.parse.urlencode(
        {"track_name": track, "artist_name": artist, "album_name": album}
    )
    req = urllib.request.Request(API + "?" + qs, headers=HDRS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def process_track(item, min_score):
    """单首：429 退避 30s（同首最多 3 次）；其它网络异常重试 2 次后记 miss，不抛出。

    带专辑名搜不到时，去掉专辑名再搜一次：lrclib 里很多冷门专辑没有专辑元数据，
    带上专辑名反而把结果筛空，这是命中率上不去的主要原因之一。
    """
    ai, ti, t, a, al = item
    rl = net = 0
    while True:
        try:
            results = fetch_search(t, a, al)
            status, lrc = decide(results, norm(t), norm(a), norm(al), min_score)
            if status == "miss" and al:
                results = fetch_search(t, a, "")
                status, lrc = decide(results, norm(t), norm(a), "", min_score)
            return ai, ti, status, lrc
        except urllib.error.HTTPError as e:
            if e.code == 429:
                rl += 1
                if rl >= 3:
                    return ai, ti, "miss", None
                time.sleep(30)
            else:
                net += 1
                if net >= 3:
                    return ai, ti, "miss", None
                time.sleep(2)
        except (urllib.error.URLError, OSError, ValueError):
            net += 1
            if net >= 3:
                return ai, ti, "miss", None
            time.sleep(2)


def load_lib():
    with open(DATA_PATH, encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw.startswith(LIB_PREFIX) or not raw.endswith(";"):
        raise SystemExit("data.js 格式不符合预期（应有 window.LIB_DATA= 前缀与结尾 ;）")
    return json.loads(raw[len(LIB_PREFIX):-1])["albums"]


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                st = json.load(f)
            if isinstance(st.get("done"), dict):
                return {
                    "done": {str(k): str(v) for k, v in st["done"].items()},
                    "updated": st.get("updated", ""),
                }
        except (OSError, ValueError):
            pass
    return {"done": {}, "updated": ""}


def save_state(state):
    state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATE_PATH)


def load_shards():
    """启动时读回已有分片，作为命中歌词的事实来源。"""
    hits = {}
    if not os.path.isdir(LYR_DIR):
        return hits
    for name in os.listdir(LYR_DIR):
        if not re.fullmatch(r"a\d+\.js", name):
            continue
        try:
            with open(os.path.join(LYR_DIR, name), encoding="utf-8-sig") as f:
                line = f.read().strip()
        except OSError:
            continue
        m = SHARD_RE.match(line)
        if not m:
            continue
        try:
            obj = json.loads(m.group(2))
        except ValueError:
            continue
        if isinstance(obj, dict):
            hits[int(m.group(1))] = {
                str(k): v for k, v in obj.items() if isinstance(v, str)
            }
    return hits


def write_shard(ai, tracks_hits):
    parts = []
    for ti in sorted(tracks_hits, key=int):
        parts.append(
            json.dumps(str(ti), ensure_ascii=False)
            + ":"
            + json.dumps(tracks_hits[ti], ensure_ascii=False)
        )
    line = SHARD_PREFIX + "window.LIB_LYR[" + str(ai) + "]={" + ",".join(parts) + "};"
    path = os.path.join(LYR_DIR, "a%d.js" % ai)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(line)
    os.replace(tmp, path)


def checkpoint(state, hits, touched):
    save_state(state)
    for ai in touched:
        if hits.get(ai):
            write_shard(ai, hits[ai])


def write_report(state, albums):
    done = state["done"]
    total = sum(len(a.get("tracks") or []) for a in albums)
    processed = len(done)
    hits_n = sum(1 for v in done.values() if v == "hit")
    miss_n = processed - hits_n
    rate = (hits_n / processed * 100.0) if processed else 0.0
    gaps = []
    for ai, a in enumerate(albums):
        tracks = a.get("tracks") or []
        m = sum(1 for ti in range(len(tracks)) if done.get("%d:%d" % (ai, ti)) == "miss")
        if m:
            gaps.append((m, ai, a, len(tracks)))
    gaps.sort(key=lambda x: (-x[0], x[1]))
    shard_n = 0
    if os.path.isdir(LYR_DIR):
        shard_n = sum(1 for n in os.listdir(LYR_DIR) if re.fullmatch(r"a\d+\.js", n))
    md = [
        "# 歌词抓取报告",
        "",
        "- 生成时间: " + datetime.now().isoformat(timespec="seconds"),
        "- 总曲目数: %d" % total,
        "- 已处理: %d" % processed,
        "- 命中: %d" % hits_n,
        "- 未命中: %d" % miss_n,
        "- 命中率: %.1f%%" % rate,
        "- 已写分片: %d 个（_lyrics/a<专辑下标>.js）" % shard_n,
        "",
        "## 缺口最多的 20 张专辑（未命中曲目数）",
        "",
    ]
    if gaps:
        md.append("| 专辑下标 | 艺人 | 专辑 | 未命中/曲目数 |")
        md.append("|---:|---|---|---:|")
        for m, ai, a, n_tracks in gaps[:20]:
            artist = str(a.get("artist") or "").replace("|", "/")
            album = str(a.get("album") or "").replace("|", "/")
            md.append("| %d | %s | %s | %d/%d |" % (ai, artist, album, m, n_tracks))
    else:
        md.append("暂无（已处理曲目全部命中）")
    tmp = REPORT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    os.replace(tmp, REPORT_PATH)


def fmt_mmss(seconds):
    return "%02d:%02d" % (seconds // 60, seconds % 60)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="从 lrclib.net 抓同步歌词到本地分片 JS")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="只处理 N 首未处理项后退出")
    ap.add_argument("--jobs", type=int, default=4, metavar="N",
                    help="全局并发（默认 4，上限 4）")
    ap.add_argument("--album", type=int, default=None, metavar="IDX",
                    help="只处理该专辑下标（调试用）")
    ap.add_argument("--min-score", type=int, default=3, metavar="N",
                    help="匹配分阈值（默认 3）")
    args = ap.parse_args()
    args.jobs = max(1, min(args.jobs, 4))

    albums = load_lib()
    total_tracks = sum(len(a.get("tracks") or []) for a in albums)
    os.makedirs(LYR_DIR, exist_ok=True)
    for stale in os.listdir(LYR_DIR):
        if stale.endswith(".tmp"):
            try:
                os.remove(os.path.join(LYR_DIR, stale))
            except OSError:
                pass

    state = load_state()
    done = state["done"]
    hits = load_shards()

    if args.album is not None:
        if not (0 <= args.album < len(albums)):
            raise SystemExit("专辑下标越界: %d（共 %d 张）" % (args.album, len(albums)))
        scope = [(args.album, ti)
                 for ti in range(len(albums[args.album].get("tracks") or []))]
    else:
        scope = [(ai, ti) for ai, a in enumerate(albums)
                 for ti in range(len(a.get("tracks") or []))]

    skipped = 0
    items = []
    for ai, ti in scope:
        if "%d:%d" % (ai, ti) in done:
            skipped += 1
            continue
        a = albums[ai]
        t = (a.get("tracks") or [])[ti]
        items.append((ai, ti, t.get("n") or "", a.get("artist") or "", a.get("album") or ""))
    if args.limit is not None and args.limit >= 0:
        items = items[: args.limit]

    print("库: %d 张专辑 / %d 首曲目" % (len(albums), total_tracks))
    print("跳过已处理 %d 项，本次待处理 %d 项（--jobs %d）" % (skipped, len(items), args.jobs), flush=True)
    if not items:
        checkpoint(state, hits, set())
        write_report(state, albums)
        print("没有待处理项。")
        return 0

    t0 = time.monotonic()
    processed = hits_run = 0
    printed_at = -1
    touched = set()
    interrupted = False
    ex = ThreadPoolExecutor(max_workers=args.jobs)
    fmap = {ex.submit(process_track, it, args.min_score): it for it in items}
    try:
        for fut in as_completed(fmap):
            it = fmap[fut]
            try:
                ai, ti, status, lrc = fut.result()
            except Exception:
                ai, ti, status, lrc = it[0], it[1], "miss", None
            processed += 1
            done["%d:%d" % (ai, ti)] = status
            if status == "hit":
                hits_run += 1
                hits.setdefault(ai, {})[str(ti)] = lrc
                touched.add(ai)
            if processed % 50 == 0:
                checkpoint(state, hits, touched)
                touched.clear()
                pct = round(hits_run * 100.0 / processed)
                print("已处理 %d/%d  命中 %d (%d%%)  用时 %s"
                      % (processed, len(items), hits_run, pct,
                         fmt_mmss(time.monotonic() - t0)), flush=True)
                printed_at = processed
    except KeyboardInterrupt:
        interrupted = True
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    checkpoint(state, hits, touched)
    write_report(state, albums)
    if processed != printed_at:
        pct = round(hits_run * 100.0 / processed) if processed else 0
        print("已处理 %d/%d  命中 %d (%d%%)  用时 %s"
              % (processed, len(items), hits_run, pct,
                 fmt_mmss(time.monotonic() - t0)), flush=True)
    if interrupted:
        print("收到 Ctrl+C：进度已保存到 _lyrics/_state.json，重跑自动续。", flush=True)
        os._exit(130)
    g_hits = sum(1 for v in done.values() if v == "hit")
    g_rate = (g_hits * 100.0 / len(done)) if done else 0.0
    print("本次完成。累计已处理 %d 项，命中 %d（%.1f%%）；分片目录: %s"
          % (len(done), g_hits, g_rate, LYR_DIR), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
