#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""歌词.py — 从 lrclib.net 与网易云音乐抓同步歌词，落成本地分片 JS（离线可用）。

用法:
  python 音乐库.py 歌词                  # 全量
  python 音乐库.py 歌词 --limit 40      # 只处理 40 首未处理项
  python 音乐库.py 歌词 --jobs 4        # 全局并发，默认 4（上限 4）
  python 音乐库.py 歌词 --album 123     # 调试单张专辑（专辑下标）
  python 音乐库.py 歌词 --retry-miss    # 重跑 _state.json 中记为 miss 的曲目
  python 音乐库.py 歌词 --recheck-hit   # 重跑存量 hit，重新校验，不合格降级 miss
  python 音乐库.py 歌词 --clean         # 本地清洗：清越界 state、剔除无效 hit，不联网
  python 音乐库.py 歌词 --min-score 3   # lrclib 匹配分阈值，默认 3

匹配顺序：lrclib（带专辑名搜索 → miss 且有专辑名时去掉专辑名再搜一次）
        → 网易云（search 取第一条曲名证据通过的候选 → 取其歌词）。
校验口径：曲名/艺人相等为强证据；专辑名互不包含则否决降权；歌名实词应出现在
歌词中；歌词时长与音频时长偏差过大降权；纯器乐标记、无时间戳文本不算命中。
Ctrl+C 随时中断：进度落盘 _lyrics/_state.json，重跑自动续。
"""
import argparse
import collections
import hashlib
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

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LYR_DIR = os.path.join(BASE_DIR, "_lyrics")
STATE_PATH = os.path.join(LYR_DIR, "_state.json")
REPORT_PATH = os.path.join(LYR_DIR, "_report.md")
DATA_PATH = os.path.join(BASE_DIR, "data.js")
DUR_PATH = os.path.join(BASE_DIR, "durations.js")
BAK_DIR = os.path.join(BASE_DIR, "_备份")

API = "https://lrclib.net/api/search"
LIB_PREFIX = "window.LIB_DATA="
SHARD_PREFIX = "window.LIB_LYR=window.LIB_LYR||{};"
HDRS = {
    "User-Agent": "bu-geci/1.0 (offline music library lyrics fetcher)",
    "Accept": "application/json",
}
NE_SEARCH = "https://music.163.com/api/search/get"
NE_LYRIC = "https://music.163.com/api/song/lyric"
NE_HDRS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Referer": "https://music.163.com/",
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
TS_FULL = re.compile(r"\[(\d{1,2}):(\d{2}(?:\.\d+)?)\]")
INST_PAT = re.compile(
    r"instrumental|纯音乐|没有填词|无歌词|伴奏|纯音乐，请您欣赏|no lyrics", re.I
)
# 曲名里不参与内容比对的弱词（功能词 + 高频口语/泛词，避免偶然命中误判）
STOP_WORDS = {"the", "a", "an", "of", "and", "to", "in", "on", "for", "my",
              "me", "you", "your", "is", "it", "feat", "ft", "live", "remix",
              "up", "out", "oh", "yeah", "ya", "la", "na", "no", "so", "do",
              "be", "but", "not", "what", "when", "why", "how", "all", "can",
              "will", "just", "like", "know", "let", "get", "got", "go", "going",
              "come", "back", "now", "here", "there", "one", "two", "day", "night",
              "time", "way", "life", "love", "heart", "baby", "girl", "boy",
              "world", "away", "never", "always", "still", "even", "down", "over",
              "under", "again", "more", "most", "much", "very", "really", "good",
              "morning", "little", "school", "its", "im", "dont", "cant", "wont",
              "thats", "theres", "youre", "ive", "id", "ill", "shes", "hes",
              "lets", "cause", "who", "whom", "whose", "which", "that", "this",
              "these", "those", "am", "are", "was", "were", "has", "have", "had",
              "his", "her", "hers", "him", "our", "ours", "their", "theirs",
              "they", "them", "we", "us", "he", "she", "or", "if", "then",
              "than", "too", "own", "same", "could", "would", "should", "may",
              "might", "must", "shall", "about", "after", "before", "between",
              "through", "during", "above", "below", "off", "into", "onto", "upon",
              "within", "without", "along", "across", "among", "toward", "towards",
              "per", "via", "yet", "also", "only", "ever", "every", "each", "both",
              "few", "many", "such", "another", "song", "music", "feat"}


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


def last_ts(lrc):
    """歌词最后一条时间戳（秒）；没有返回 None。"""
    ts = TS_FULL.findall(lrc)
    if not ts:
        return None
    return int(ts[-1][0]) * 60 + float(ts[-1][1])


def title_terms(tn):
    """从归一化曲名提取可比对实词：英文长度≥3 的非停用词；中文连续串。"""
    if not tn:
        return []
    words = [w for w in re.split(r"\s+", tn) if len(w) >= 3 and w not in STOP_WORDS]
    zh = re.findall(r"[\u4e00-\u9fff]{2,}", tn)
    return words + zh


def term_hits(tn, lrc):
    """返回 (命中实词, 全部实词)，供串歌校验逐首比对使用。"""
    terms = title_terms(tn)
    if not terms:
        return [], []
    body = re.sub(r"\[.*?\]", "", lrc).lower()
    body = re.sub(r"[^\w\u4e00-\u9fff]+", " ", body)
    hit = [t for t in terms if t in body]
    return hit, terms


def content_match(tn, lrc):
    """歌名实词是否出现在歌词正文；无实词可比对时不否决（返回 True）。"""
    hit, terms = term_hits(tn, lrc)
    if not terms:
        return True
    return bool(hit)


def is_instrumental(lrc):
    """纯器乐/伴奏标记文本：正文很短且命中器乐关键词。"""
    body = re.sub(r"\[.*?\]", "", lrc).strip()
    return len(body) <= 40 and bool(INST_PAT.search(body))


def load_durations():
    if not os.path.exists(DUR_PATH):
        return {}
    try:
        raw = open(DUR_PATH, encoding="utf-8").read().strip()
        if raw.startswith("window.LIB_DUR="):
            raw = raw[len("window.LIB_DUR="):].rstrip(";")
        return json.loads(raw)
    except (OSError, ValueError):
        return {}


def dur_key(rel, track_p):
    """durations.js 的键：rel 去掉 无损/ 前缀，拼文件名。"""
    r = (rel or "").replace("\\", "/")
    if r.startswith("无损/"):
        r = r[len("无损/"):]
    return r + "/" + track_p


def score_cand(c, tn, an, aln):
    """曲名完全相同 +2 / 互相包含 +1；艺人同上；专辑名互相包含 +1，互不包含 -2。"""
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
    if aln and cal:
        if cal in aln or aln in cal:
            s += 1
        else:
            s -= 2
    return s


def decide(results, tn, an, aln, min_score, dur=None):
    """lrclib 候选择优：选最高分且 >= 阈值的。
    instrumental / 器乐标记 / 纯文本 / 无时间戳一律不算命中；
    歌名实词未入歌词、时长偏差过大则降权或跳过。"""
    best_s, best_lrc = -1, None
    for c in results or []:
        if c.get("instrumental"):
            continue
        lrc = (c.get("syncedLyrics") or "").strip()
        if not lrc or not TS_PAT.search(lrc):
            continue
        if is_instrumental(lrc):
            continue
        s = score_cand(c, tn, an, aln)
        if not content_match(tn, lrc):
            s -= 2
        last = last_ts(lrc)
        if dur and last is not None:
            if last > dur + 30:
                continue
            if dur > 120 and last < dur * 0.4:
                s -= 2
        if s > best_s:
            best_s, best_lrc = s, lrc
    if best_s >= min_score:
        return "hit", best_lrc
    return "miss", None


def http_get_json(url, headers):
    """GET 并解析 JSON；429 退避 30s（同一请求最多 3 次），其它网络异常退避 2s 重试
    2 次，仍失败返回 None；绝不向上抛异常。"""
    rl = net = 0
    while True:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                rl += 1
                if rl >= 3:
                    return None
                time.sleep(30)
            else:
                net += 1
                if net >= 3:
                    return None
                time.sleep(2)
        except (urllib.error.URLError, OSError, ValueError):
            net += 1
            if net >= 3:
                return None
            time.sleep(2)


def fetch_search(track, artist, album):
    """lrclib 搜索；专辑名为空时不带该参数。"""
    params = {"track_name": track, "artist_name": artist}
    if album:
        params["album_name"] = album
    qs = urllib.parse.urlencode(params)
    return http_get_json(API + "?" + qs, HDRS)


def lrclib_decide(t, a, al, tn, an, aln, min_score, dur=None):
    """lrclib 路径：先带专辑名搜；仍 miss 且有专辑名时，去掉专辑名再搜一次。"""
    status, lrc = decide(fetch_search(t, a, al), tn, an, aln, min_score, dur)
    if status == "hit" or not al:
        return status, lrc
    return decide(fetch_search(t, a, ""), tn, an, aln, min_score, dur)


def ne_search(track, artist):
    """网易云搜索：s=曲名+空格+艺人，返回 result.songs[]（失败/无结果返回 []）。"""
    qs = urllib.parse.urlencode(
        {"s": (track + " " + artist).strip(), "type": 1, "limit": 10, "offset": 0}
    )
    data = http_get_json(NE_SEARCH + "?" + qs, NE_HDRS)
    if not isinstance(data, dict):
        return []
    result = data.get("result")
    songs = result.get("songs") if isinstance(result, dict) else None
    return songs if isinstance(songs, list) else []


def ne_lyric(song_id):
    """网易云歌词：返回 lrc.lyric 原文（取不到返回 None）。"""
    qs = urllib.parse.urlencode({"id": song_id, "lv": 1, "kv": 1, "tv": -1})
    data = http_get_json(NE_LYRIC + "?" + qs, NE_HDRS)
    if not isinstance(data, dict):
        return None
    lrc = data.get("lrc")
    if isinstance(lrc, dict) and isinstance(lrc.get("lyric"), str) and lrc["lyric"].strip():
        return lrc["lyric"]
    return None


def ne_artist_ok(song, an):
    """艺人证据：任一 artists[].name 归一化后相等或互相包含即通过；无艺人信息不否决。"""
    if not an:
        return True
    arts = song.get("artists")
    if not isinstance(arts, list) or not arts:
        return True
    for art in arts:
        na = norm(art.get("name")) if isinstance(art, dict) else ""
        if na and (na == an or na in an or an in na):
            return True
    return False


def ne_decide(track, artist, tn, an):
    """网易云路径（每首最多 2 次请求）：search 后只认「曲名证据」——归一化后曲名相等
    或互相包含的候选；优先取第一条同时有艺人证据的，无则退回第一条纯曲名证据的；
    取其歌词并复用 TS_PAT，无 [mm:ss 时间戳不算命中。"""
    if not tn:
        return "miss", None
    songs = ne_search(track, artist)
    pick = None
    for song in songs:
        if not isinstance(song, dict):
            continue
        nname = norm(song.get("name"))
        if not nname or not (nname == tn or nname in tn or tn in nname):
            continue
        if ne_artist_ok(song, an):
            pick = song
            break
        if pick is None:
            pick = song
    if pick is None or pick.get("id") is None:
        return "miss", None
    text = ne_lyric(pick.get("id"))
    if (text and TS_PAT.search(text) and not is_instrumental(text)
            and content_match(tn, text)):
        return "hit", text
    return "miss", None


def process_track(item, min_score):
    """单首完整链路：lrclib（带专辑名 → 无专辑名）→ 网易云；全部网络异常都在
    http_get_json 内消化，不会抛出。返回 (ai, ti, status, lrc, src)。"""
    ai, ti, t, a, al, dur = item
    tn, an, aln = norm(t), norm(a), norm(al)
    status, lrc = lrclib_decide(t, a, al, tn, an, aln, min_score, dur)
    if status == "hit":
        return ai, ti, "hit", lrc, "lrclib"
    status, lrc = ne_decide(t, a, tn, an)
    if status == "hit":
        return ai, ti, "hit", lrc, "netease"
    return ai, ti, "miss", None, ""


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
    """启动时读回已有分片，作为命中歌词的事实来源（保证已有 hit 的内容不会被覆盖丢失）。"""
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


def clean_main(albums, state, hits, durs):
    """本地清洗（不联网）：清越界 state、剔除无效 hit、修正 hit 缺分片。"""
    done = state["done"]
    os.makedirs(BAK_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(BAK_DIR, "_state.json.bak-" + stamp)
    try:
        shutil_copy(STATE_PATH, bak)
    except OSError:
        bak = ""

    # 1) 越界 / 非法 key 删除
    stale = []
    for key in list(done.keys()):
        try:
            ai, ti = map(int, key.split(":"))
        except ValueError:
            stale.append(key)
            continue
        if ai >= len(albums) or ti >= len(albums[ai].get("tracks") or []):
            stale.append(key)
    for key in stale:
        del done[key]

    # 2) 分片条目有效性校验：无时间戳 / 少于 3 行 / 器乐标记 → 降级 miss 并移出分片；
    #    下标越界的条目直接剔除，不回写 state（避免越界 key 被重新加回）。
    invalid = []
    for ai in list(hits.keys()):
        if ai >= len(albums):
            del hits[ai]
            continue
        obj = hits[ai]
        n_tracks = len(albums[ai].get("tracks") or [])
        for ti_s in list(obj.keys()):
            ti = int(ti_s)
            if ti >= n_tracks:
                del obj[ti_s]
                done.pop("%d:%d" % (ai, ti), None)
                continue
            lrc = obj[ti_s]
            if isinstance(lrc, list):
                lrc = "\n".join(str(x) for x in lrc)
            if not isinstance(lrc, str):
                lrc = str(lrc)
            n_lines = len([x for x in lrc.splitlines() if x.strip()])
            bad = (not TS_PAT.search(lrc)) or n_lines < 3 or is_instrumental(lrc)
            if bad:
                del obj[ti_s]
                done["%d:%d" % (ai, ti)] = "miss"
                invalid.append((ai, ti))
        if not obj:
            del hits[ai]

    # 2b) 同专辑多曲共用同一文本：逐首做歌名实词校验。实词全未命中，或命中率不足
    #     一半且无长实词（≥4 字母）命中的，判为串歌（同一段歌词挂到了别的曲子上），
    #     剔除并降级 miss；无可比对实词的不否决。兜底：组内若全部判废，保留命中率
    #     最高的一首，避免把正确歌词整组误删。
    shared_bad = []
    for ai in list(hits.keys()):
        obj = hits[ai]
        tracks = albums[ai].get("tracks") or []
        by_text = collections.defaultdict(list)
        for ti_s, lrc in obj.items():
            if isinstance(lrc, list):
                lrc = "\n".join(str(x) for x in lrc)
            h = hashlib.md5(re.sub(r"\[.*?\]", "", str(lrc)).encode()).hexdigest()
            by_text[h].append((int(ti_s), str(lrc)))
        for items in by_text.values():
            if len(items) < 2:
                continue
            score = {}
            for ti, lrc in items:
                if ti >= len(tracks):
                    continue
                hit, terms = term_hits(norm(tracks[ti].get("n")), lrc)
                score[ti] = (len(hit), len(terms))
            drop = []
            for ti, (nh, nt) in score.items():
                if nt == 0:
                    continue
                strong = any(len(t) >= 4 for t in hit)
                ratio = nh / nt
                if nh == 0 or (ratio < 0.5 and not strong):
                    drop.append(ti)
            if len(drop) == len(score) and score:
                keep = max(score, key=lambda k: (score[k][0], -score[k][1]))
                drop = [t for t in drop if t != keep]
            for ti in drop:
                obj.pop(str(ti), None)
                done["%d:%d" % (ai, ti)] = "miss"
                shared_bad.append((ai, ti))
        if not obj:
            del hits[ai]

    # 3) state 记 hit 但分片缺该曲 → 记 miss
    fixed_missing = 0
    for key, st in list(done.items()):
        if st != "hit":
            continue
        ai, ti = map(int, key.split(":"))
        if not (ai in hits and str(ti) in hits[ai]):
            done[key] = "miss"
            fixed_missing += 1

    # 4) 删掉已空的分片文件
    removed_shards = 0
    if os.path.isdir(LYR_DIR):
        for n in os.listdir(LYR_DIR):
            m = re.fullmatch(r"a(\d+)\.js", n)
            if not m:
                continue
            if int(m.group(1)) not in hits:
                try:
                    os.remove(os.path.join(LYR_DIR, n))
                    removed_shards += 1
                except OSError:
                    pass

    save_state(state)
    # 重写保留下来的分片（剔除条目落盘）
    for ai in hits:
        write_shard(ai, hits[ai])
    write_report(state, albums)

    print("清洗完成（备份: %s）" % (bak or "无"))
    print("- 越界/非法 state 记录删除: %d 条" % len(stale))
    print("- 无效 hit 降级 miss 并移出分片: %d 首" % len(invalid))
    print("- 串歌共用文本剔除降级: %d 首" % len(shared_bad))
    print("- hit 缺分片修正为 miss: %d 首" % fixed_missing)
    print("- 清空后删除的分片文件: %d 个" % removed_shards)
    g_hits = sum(1 for v in done.values() if v == "hit")
    print("- 累计已处理 %d 项，命中 %d / 未命中 %d" % (len(done), g_hits, len(done) - g_hits))
    return 0


def shutil_copy(src, dst):
    import shutil
    shutil.copy2(src, dst)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="从 lrclib 与网易云抓同步歌词到本地分片 JS")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="只处理 N 首未处理项后退出")
    ap.add_argument("--jobs", type=int, default=4, metavar="N",
                    help="全局并发（默认 4，上限 4）")
    ap.add_argument("--album", type=int, default=None, metavar="IDX",
                    help="只处理该专辑下标（调试用）")
    ap.add_argument("--min-score", type=int, default=3, metavar="N",
                    help="lrclib 匹配分阈值（默认 3）")
    ap.add_argument("--retry-miss", action="store_true",
                    help="重跑 _state.json 中记为 miss 的曲目（先 lrclib 再网易云）")
    ap.add_argument("--recheck-hit", action="store_true",
                    help="重跑存量 hit，重新过匹配校验，不合格降级 miss 并移出分片")
    ap.add_argument("--clean", action="store_true",
                    help="本地清洗：清越界 state、剔除无效 hit，不联网")
    args = ap.parse_args()
    args.jobs = max(1, min(args.jobs, 4))

    albums = load_lib()
    total_tracks = sum(len(a.get("tracks") or []) for a in albums)
    durs = load_durations()
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

    if args.clean:
        return clean_main(albums, state, hits, durs)

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
        key = "%d:%d" % (ai, ti)
        if args.recheck_hit:
            if done.get(key) != "hit":
                skipped += 1
                continue
        elif args.retry_miss:
            if done.get(key) != "miss":
                skipped += 1
                continue
        elif key in done:
            skipped += 1
            continue
        a = albums[ai]
        t = (a.get("tracks") or [])[ti]
        dur = durs.get(dur_key(a.get("rel"), t.get("p")))
        items.append((ai, ti, t.get("n") or "", a.get("artist") or "",
                      a.get("album") or "", dur))
    if args.limit is not None and args.limit >= 0:
        items = items[: args.limit]

    print("库: %d 张专辑 / %d 首曲目" % (len(albums), total_tracks))
    if args.recheck_hit:
        print("重校模式：仅重跑存量 hit，本次重校 %d 项 / 跳过 %d 项（--jobs %d）"
              % (len(items), skipped, args.jobs), flush=True)
    elif args.retry_miss:
        print("重试模式：仅重跑 miss 项（命中保持不变），本次重试 %d 项 / 跳过 %d 项（--jobs %d）"
              % (len(items), skipped, args.jobs), flush=True)
    else:
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
    src_hits = {"lrclib": 0, "netease": 0}
    ex = ThreadPoolExecutor(max_workers=args.jobs)
    fmap = {ex.submit(process_track, it, args.min_score): it for it in items}
    try:
        for fut in as_completed(fmap):
            it = fmap[fut]
            try:
                ai, ti, status, lrc, src = fut.result()
            except Exception:
                ai, ti, status, lrc, src = it[0], it[1], "miss", None, ""
            processed += 1
            done["%d:%d" % (ai, ti)] = status
            if status == "hit":
                hits_run += 1
                if src in src_hits:
                    src_hits[src] += 1
                hits.setdefault(ai, {})[str(ti)] = lrc
            else:
                # 重校时旧 hit 被降级：从分片剔除该曲；普通模式此处本就无条目
                if ai in hits:
                    hits[ai].pop(str(ti), None)
                    if not hits[ai]:
                        del hits[ai]
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
    print("本次完成。累计已处理 %d 项，命中 %d（%.1f%%）；本次命中来源: lrclib %d / 网易云 %d；分片目录: %s"
          % (len(done), g_hits, g_rate, src_hits["lrclib"], src_hits["netease"], LYR_DIR), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
