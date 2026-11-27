# -*- coding: utf-8 -*-
"""质检.py — 元数据内容层校验（与 检查.py 并列：检查验结构，质检验内容）。

只读扫描 data.js / _covers / _lyrics，把可疑项分级写进 _logs/质检报告.md，
不改动任何数据。退出码：发现必修项且带 --strict 时返回 1，否则 0。

用法:
    python 音乐库.py 质检                # 出报告
    python 音乐库.py 质检 --strict       # 有必修项时退出码 1（可接 CI / 入库前闸）
    python 音乐库.py 质检 --out 路径.md   # 自定义报告路径
"""
import argparse
import collections
import datetime
import hashlib
import json
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(BASE_DIR)
DATA_PATH = os.path.join(BASE_DIR, "data.js")
COVERS = os.path.join(BASE_DIR, "_covers")
LYRDIR = os.path.join(BASE_DIR, "_lyrics")
REPORT_PATH = os.path.join(BASE_DIR, "_logs", "质检报告.md")

LIB_PREFIX = "window.LIB_DATA="
TS_PAT = re.compile(r"\[\d{1,2}:\d{2}")

# 待人工复核条目（自动规则无法安全判定人声碟/合唱/歌剧与串歌的边界）。
# 每项: (专辑下标, 曲目范围, 说明)。核对后可从清单移除；不记入必修项。
REVIEW_LIST = [
    (1388, "全碟 13 首", "沈洋《重低音》低男中音人声专辑，挂中文词疑似串歌，需逐首对原唱"),
    (1150, "曲0", "Britten《Noye's Fludde》儿童歌剧合唱，歌词是否原配待核"),
    (976, "曲17 Enigmatic Soul", "Two Steps From Hell 人声合唱/配乐，歌词归属待核"),
    (600, "曲6", "Marta Gomez 西语女声挂英西混词，是否串歌待核"),
    (805, "曲18 A Riverside Town - Jiang Cheng Ji", "老虎鱼试音碟人声曲，歌词归属待核"),
    (1147, "曲3 Grieg Solveig's Song", "格里格声乐独唱曲，与同碟器乐舞曲分开复核"),
    (1257, "全碟", "张学友人声碟，英文名挂中文词，已确认本人正确歌词，无需处理"),
    (1489, "全碟", "蔡琴《机遇》人声原声，英文名挂中文词，已确认本人正确歌词，无需处理"),
    # —— 齐秦碟：他人民歌/老歌串歌已剔除；以下为碟内槽位错位与存疑，需结合音频核对曲目名后重抓 ——
    (1670, "曲0/4/9", "《出没》：曲0 挂《大约在冬季》词；曲4 冷月、曲9 今天明天原唱归属待核"),
    (1673, "整碟 10 首", "《狼 I》：分片曲目槽位与曲名错位，词为齐秦他碟曲目，需对音频核对曲目名后重抓"),
    (1674, "整碟 12 首", "《让我孤独的时候还能够想着你》：曲目名跨多碟混编、歌词槽位错位，需核对音频后修曲目名/重抓"),
    (1675, "整碟 10 首", "《柔情主义-内地版》：歌词碟内错位，需核对音频后重抓"),
    (1676, "曲0/1/2/5/8/9 错位；曲3/4/6 存疑", "《丝路》：错位条目为齐秦他碟词；寒雨/回来/丝路三首原唱待核。曲10 悬崖、曲11 KALA 已核对正确保留"),
    (1677, "整碟 12 首", "《纯情歌》闽南语专辑：国语老歌词错位挂入闽南语曲目，需核对音频后重抓；曲8 恨世生、曲11 纯情歌存疑"),
    (1678, "曲2/3/5/6/9 错位；曲0/4/7/8 存疑", "《狼 黄金自选辑》：曲1 这一次我绝不放手、曲10 往事随风、曲11 不如这样吧、曲12 夜夜夜夜、曲13 怀念的播音员已核对保留"),
    (1679, "曲1/2/3/6/8/10/11 错位；曲0/4/5/7/9 存疑", "《狼·冬极致模拟版》：曲目名混编、歌词错位，需结合音频核对后重抓"),
]
SHARD_RE = re.compile(
    r"^window\.LIB_LYR=window\.LIB_LYR\|\|\{\};window\.LIB_LYR\[(\d+)\]=(\{.*\});$"
)


def norm(s):
    return re.sub(r"\s+", "", str(s or "")).lower()


def load_lib():
    with open(DATA_PATH, encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw.startswith(LIB_PREFIX) or not raw.endswith(";"):
        raise SystemExit("data.js 格式不符合预期（应有 window.LIB_DATA= 前缀与结尾 ;）")
    return json.loads(raw[len(LIB_PREFIX):-1])["albums"]


def scan_intro(albums):
    """介绍字段问题。返回 dict 各分组。"""
    same_ai, short_placeholder = [], []
    intro_groups = collections.defaultdict(list)
    ai_groups = collections.defaultdict(list)
    for i, a in enumerate(albums):
        intro = (a.get("intro") or "").strip()
        ai = (a.get("artistInfo") or "").strip()
        tag = "[%d] %s - %s" % (i, a.get("artist"), a.get("album"))
        if intro and ai and intro == ai:
            same_ai.append(tag)
        if intro and len(intro) < 20:
            short_placeholder.append((tag, intro))
        if intro:
            intro_groups[norm(intro)].append(i)
        if ai:
            ai_groups[norm(ai)].append(i)

    def cross_artist(groups):
        out = []
        for v in groups.values():
            if len(v) < 2:
                continue
            if len({norm(albums[i].get("artist")) for i in v}) > 1:
                out.append(v)
        out.sort(key=lambda v: -len(v))
        return out

    return {
        "same_ai": same_ai,
        "short": short_placeholder,
        "intro_cross": cross_artist(intro_groups),
        "ai_cross": cross_artist(ai_groups),
    }


def scan_covers(albums):
    """封面问题。跨艺人同图列必修；同人多碟共用列提示。"""
    hash_map = collections.defaultdict(list)
    cf_empty, cv_empty, cf_missing, no_orig = [], [], [], []
    for i, a in enumerate(albums):
        tag = "[%d] %s - %s" % (i, a.get("artist"), a.get("album"))
        cf = (a.get("coverFile") or "").strip()
        cv = (a.get("cover") or "").strip()
        rel = a.get("rel") or ""
        if not cf:
            cf_empty.append(tag)
        else:
            p = os.path.join(COVERS, cf)
            if not os.path.exists(p):
                cf_missing.append((tag, cf))
            else:
                h = hashlib.md5(open(p, "rb").read()).hexdigest()
                hash_map[h].append(i)
        if not cv:
            cv_empty.append(tag)
        elif not os.path.exists(os.path.join(ROOT, rel, cv)):
            no_orig.append((tag, rel + "/" + cv))

    cross, same = [], []
    for v in hash_map.values():
        if len(v) < 2:
            continue
        artists = {norm(albums[i].get("artist")) for i in v}
        (cross if len(artists) > 1 else same).append(v)
    cross.sort(key=lambda v: -len(v))
    return {
        "cross": cross, "same": same,
        "cf_empty": cf_empty, "cv_empty": cv_empty,
        "cf_missing": cf_missing, "no_orig": no_orig,
    }


def scan_lyrics(albums):
    """歌词分片与 state 问题。"""
    shards = {}
    if os.path.isdir(LYRDIR):
        for n in os.listdir(LYRDIR):
            if not re.fullmatch(r"a\d+\.js", n):
                continue
            try:
                m = SHARD_RE.match(
                    open(os.path.join(LYRDIR, n), encoding="utf-8-sig").read().strip())
            except OSError:
                continue
            if m:
                try:
                    shards[int(m.group(1))] = json.loads(m.group(2))
                except ValueError:
                    pass

    state_path = os.path.join(LYRDIR, "_state.json")
    done = {}
    if os.path.exists(state_path):
        try:
            done = json.load(open(state_path, encoding="utf-8")).get("done", {})
        except (OSError, ValueError):
            done = {}

    no_ts, too_short, share_albums = [], [], collections.defaultdict(list)
    for ai, obj in shards.items():
        if ai >= len(albums):
            continue
        al = albums[ai]
        per_album = collections.defaultdict(list)
        for ti_s, lrc in obj.items():
            ti = int(ti_s)
            if isinstance(lrc, list):
                lrc = "\n".join(str(x) for x in lrc)
            if not isinstance(lrc, str):
                lrc = str(lrc)
            lines_l = [x for x in lrc.splitlines() if x.strip()]
            if not TS_PAT.search(lrc):
                no_ts.append((ai, ti, al.get("album"), len(lrc)))
            if len(lines_l) < 3:
                too_short.append((ai, ti, al.get("album"), lrc[:60]))
            h = hashlib.md5(re.sub(r"\[.*?\]", "", lrc).encode()).hexdigest()
            tracks = al.get("tracks") or []
            name = tracks[ti].get("n", "?") if ti < len(tracks) else "?"
            per_album[h].append((ti, name))
        for items in per_album.values():
            if len(items) > 1:
                share_albums[ai].append(items)

    stale, hit_no_shard = [], 0
    for key, st in done.items():
        try:
            ai, ti = map(int, key.split(":"))
        except ValueError:
            stale.append(key)
            continue
        if ai >= len(albums) or ti >= len(albums[ai].get("tracks") or []):
            stale.append(key)
        elif st == "hit" and not (ai in shards and str(ti) in shards[ai]):
            hit_no_shard += 1

    return {
        "shards": shards, "done": done,
        "no_ts": no_ts, "too_short": too_short,
        "share_albums": dict(share_albums),
        "stale": stale, "hit_no_shard": hit_no_shard,
    }


def write_report(albums, intro, covers, lyr, out_path):
    lines = []
    w = lines.append
    total = sum(len(a.get("tracks") or []) for a in albums)
    w("# 元数据质检报告")
    w("")
    w("- 生成时间: " + datetime.datetime.now().isoformat(timespec="seconds"))
    w("- 库规模: %d 张专辑 / %d 首曲目" % (len(albums), total))
    w("- 口径: 只读扫描，未改动任何数据；「同人多碟」指同一艺人多张碟共用同一段简介/封面，属正常。")
    w("")

    w("## 一、专辑/艺人介绍")
    w("")
    w("### 1.1 intro 与 artistInfo 完全相同（两栏重复填充）: %d 张" % len(intro["same_ai"]))
    w("")
    for t in intro["same_ai"][:40]:
        w("- " + t)
    if len(intro["same_ai"]) > 40:
        w("- …（其余 %d 张略）" % (len(intro["same_ai"]) - 40))
    w("")
    w("### 1.2 intro 短于 20 字（疑似填了编号/口号/专辑名）: %d 张" % len(intro["short"]))
    w("")
    for t, s in intro["short"]:
        w("- %s => `%s`" % (t, s))
    w("")
    w("### 1.3 intro 被跨艺人多张专辑共用: %d 组" % len(intro["intro_cross"]))
    w("")
    for v in intro["intro_cross"][:20]:
        w("- %d 张: " % len(v) + " | ".join(
            "%s《%s》" % (albums[i].get("artist"), albums[i].get("album")) for i in v[:5]))
    w("")
    w("### 1.4 artistInfo 被跨艺人多张专辑共用: %d 组" % len(intro["ai_cross"]))
    w("")
    for v in intro["ai_cross"][:20]:
        w("- %d 张: " % len(v) + " | ".join(
            "%s《%s》" % (albums[i].get("artist"), albums[i].get("album")) for i in v[:5]))
    w("")

    w("## 二、封面")
    w("")
    w("### 2.1 跨艺人共用同一张封面图（字节相同）: %d 组" % len(covers["cross"]))
    w("")
    for v in covers["cross"]:
        w("- %d 张:" % len(v))
        for i in v:
            w("  - [%d] %s - %s  (%s)" % (
                i, albums[i].get("artist"), albums[i].get("album"), albums[i].get("coverFile")))
    w("")
    w("### 2.2 同人多碟共用封面（一般正常，CD1/CD2 或同碟不同母带）: %d 组" % len(covers["same"]))
    w("")
    w("- coverFile 为空: %d；cover 原图名为空: %d；缩略图缺失: %d；目录原图缺失: %d"
      % (len(covers["cf_empty"]), len(covers["cv_empty"]),
         len(covers["cf_missing"]), len(covers["no_orig"])))
    for t in covers["cv_empty"][:20]:
        w("- cover 空: " + t)
    w("")

    w("## 三、歌词")
    w("")
    hits = sum(1 for v in lyr["done"].values() if v == "hit")
    misses = sum(1 for v in lyr["done"].values() if v == "miss")
    w("- 分片专辑数: %d；state 已处理: %d（hit %d / miss %d）"
      % (len(lyr["shards"]), len(lyr["done"]), hits, misses))
    w("- 分片内无时间戳（无法高亮对齐）: %d 首" % len(lyr["no_ts"]))
    w("- 歌词少于 3 行（器乐标记/截断/讲话文本）: %d 首" % len(lyr["too_short"]))
    w("- 同一张专辑内多首曲目共用同一段文本: %d 张专辑" % len(lyr["share_albums"]))
    w("- state 越界残留记录（专辑/曲目增删后对不上）: %d 条" % len(lyr["stale"]))
    w("- state 记 hit 但分片缺该曲: %d 首" % lyr["hit_no_shard"])
    w("")
    w("### 3.1 少于 3 行的歌词样例")
    w("")
    for ai, ti, alb, s in lyr["too_short"][:25]:
        w("- a%d 曲%d《%s》: `%s`" % (ai, ti, alb, s.replace("\n", " / ")))
    w("")
    w("### 3.2 同专辑多曲共用文本样例")
    w("")
    for ai, items in list(lyr["share_albums"].items())[:15]:
        for its in items:
            w("- a%d《%s》: " % (ai, albums[ai].get("album"))
              + "、".join("曲%d %s" % (t, n) for t, n in its))
    w("")
    w("### 3.3 待人工复核（规则边界，未自动剔除）")
    w("")
    for ai, scope, note in REVIEW_LIST:
        if ai < len(albums):
            al = albums[ai]
            w("- a%d《%s》%s %s: %s" % (ai, al.get("album"), al.get("artist"), scope, note))
        else:
            w("- a%d %s: %s" % (ai, scope, note))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def must_fix_count(intro, covers, lyr):
    return (len(intro["same_ai"]) + len(intro["short"])
            + len(intro["intro_cross"]) + len(intro["ai_cross"])
            + len(covers["cross"])
            + len(lyr["no_ts"]) + len(lyr["too_short"])
            + len(lyr["share_albums"]) + len(lyr["stale"]) + lyr["hit_no_shard"])


def main():
    ap = argparse.ArgumentParser(description="元数据内容层质检，输出报告到 _logs/质检报告.md")
    ap.add_argument("--out", default=REPORT_PATH, help="自定义报告路径")
    ap.add_argument("--strict", action="store_true", help="发现必修项时退出码 1")
    args = ap.parse_args()

    albums = load_lib()
    intro = scan_intro(albums)
    covers = scan_covers(albums)
    lyr = scan_lyrics(albums)
    write_report(albums, intro, covers, lyr, args.out)

    n = must_fix_count(intro, covers, lyr)
    print("质检完成：必修相关项 %d 个，报告 -> %s" % (n, args.out))
    if args.strict and n:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
