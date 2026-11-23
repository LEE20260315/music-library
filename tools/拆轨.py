# -*- coding: utf-8 -*-
r"""按 CUE 把整轨音频拆成单曲，放进 无损/<艺人>/<专辑>/。

为什么要有这个脚本：
  页面上的「专辑入库」是按"一个音频文件 = 一首"来认的。整轨碟（一个 wav/ape 里装着
  十几首，靠 .cue 记分界）直接入库会变成只有 1 首的专辑，曲目名还是文件名——既听不了
  （进度条横跨整张盘），歌词也无从对齐。所以这类碟必须先在命令行拆开，再走入库。

用法：
  python 音乐库.py 拆轨 --src "D:\下载\群星 《丹拿2005试音碟》[WAV+CUE]"
  python 音乐库.py 拆轨 --src "..." --artist 群星 --album 丹拿2005试音碟 --year 2005
  python 音乐库.py 拆轨 --scan          # 只看 无损/ 下还有哪些整轨碟没拆
  python 音乐库.py 拆轨 --dry-run       # 只打印要做什么，不动磁盘

做的事：
  1. 读 .cue（GBK / UTF-8 自动识别，GBK 是中文碟的绝大多数）
  2. 逐轨 ffmpeg -ss/-t 切片，转 FLAC（库里 20626 个 flac，wav 只占 51 个，统一成 flac）
  3. 写进 无损/<艺人>/<年份> <专辑> [位深-采样率]/  目录名严格按 库维护须知.md 的格式
  4. 原盘默认原地不动（跨盘搬一份等于把库再撑大一倍，E 盘常年是满的）。
     要腾地方就自己删，或者加 --move-raw 让它搬到 _原盘/ 下。
  5. 不碰 data.js：拆完回页面点「专辑入库」，走的是同一条已验证的写入路径

为什么不用 ffmpeg 的 -f segment 一把梭：segment 只能按等长切，CUE 里的分界不是等长的，
必须按每一轨的 INDEX 01 各自 -ss/-t。整轨 wav 上百 MB，用 -ss 定位后只解这一段，
比先整张转 flac 再切快得多。
"""
import argparse, io, json, os, re, shutil, subprocess, sys, tempfile

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, '无损')
RAW = os.path.join(ROOT, '_原盘')
AUDIO_EXT = {'.wav', '.ape', '.flac', '.wv', '.tta', '.aiff', '.m4a', '.mp3', '.dff', '.dsf'}
SKIP_DIRS = {'_covers', '_lyrics', '.git', '__pycache__', 'System Volume Information', '$RECYCLE.BIN'}


def die(msg, code=1):
    print('错误：' + msg)
    sys.exit(code)


def read_text(path):
    """CUE 没有编码标记，中文碟九成是 GBK，偶有 UTF-8/BOM。按成功率试。"""
    raw = open(path, 'rb').read()
    for enc in ('utf-8-sig', 'gbk', 'utf-16', 'latin-1'):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode('gbk', 'replace')


def parse_cue(text):
    """返回 (album_title, performer, [ {n,title,performer,start,file}, ... ], file_name)"""
    album = {'title': '', 'performer': '', 'file': ''}
    tracks = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.upper().startswith('REM'):
            continue
        m = re.match(r'^FILE\s+"?([^"]+?)"?\s+(\w+)?$', s, re.I)
        if m:
            album['file'] = m.group(1)
            continue
        if re.match(r'^TRACK\s+\d+', s, re.I):
            cur = {'n': len(tracks) + 1, 'title': '', 'performer': '', 'start': None}
            tracks.append(cur)
            continue
        m = re.match(r'^TITLE\s+"?(.*?)"?$', s, re.I)
        if m:
            if cur is None:
                album['title'] = m.group(1).strip()
            else:
                cur['title'] = m.group(1).strip()
            continue
        m = re.match(r'^PERFORMER\s+"?(.*?)"?$', s, re.I)
        if m:
            if cur is None:
                album['performer'] = m.group(1).strip()
            else:
                cur['performer'] = m.group(1).strip()
            continue
        m = re.match(r'^INDEX\s+0?1\s+(\d+):(\d+):(\d+)', s, re.I)
        if m and cur is not None:
            cur['start'] = int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 75.0
    tracks = [t for t in tracks if t['start'] is not None]
    tracks.sort(key=lambda t: t['start'])
    return album, tracks


def probe(path):
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
         '-show_entries', 'stream=sample_rate,bits_per_sample,channels',
         '-show_entries', 'format=duration', '-of', 'json', path],
        capture_output=True, text=True, encoding='utf-8', errors='replace')
    if out.returncode != 0:
        die('ffprobe 读不了这个文件：' + path + '\n' + (out.stderr or '')[:400])
    d = json.loads(out.stdout)
    st = (d.get('streams') or [{}])[0]
    return {
        'sr': int(st.get('sample_rate') or 0),
        'bps': int(st.get('bits_per_sample') or 0) or 16,
        'ch': int(st.get('channels') or 2),
        'dur': float((d.get('format') or {}).get('duration') or 0),
    }


def safe(s):
    """目录/文件名里不能出现 Windows 的保留字符，换掉即可，别删字。"""
    s = re.sub(r'[\\/:*?"<>|]', '-', s)
    return re.sub(r'\s+', ' ', s).strip().rstrip('.')


def fmt_khz(sr):
    k = sr / 1000.0
    return ('%g' % k) + 'kHz'


def find_audio(folder):
    got = []
    for n in sorted(os.listdir(folder)):
        p = os.path.join(folder, n)
        if os.path.isfile(p) and os.path.splitext(n)[1].lower() in AUDIO_EXT:
            got.append(p)
    return got


def find_cue(folder):
    return [os.path.join(folder, n) for n in sorted(os.listdir(folder))
            if n.lower().endswith('.cue')]


def split_one(src, artist=None, album=None, year=None, dry=False, move_raw=False):
    cues = find_cue(src)
    if not cues:
        die('这个目录里没有 .cue：' + src)
    audio = find_audio(src)
    if not audio:
        die('这个目录里没有音频文件：' + src)
    if len(audio) > 1:
        die('这个目录里有 %d 个音频文件，不是整轨碟。\n'
            '多文件的专辑直接回页面点「专辑入库」即可，不需要拆。' % len(audio))

    cue_path = cues[0]
    cue_album, tracks = parse_cue(read_text(cue_path))
    if not tracks:
        die('CUE 里没解析出任何 TRACK，检查一下文件：' + cue_path)

    src_audio = audio[0]
    # CUE 里的 FILE 名优先（大小写/全半角可能不同），找不到就用目录里唯一的那个音频
    named = [p for p in audio if os.path.basename(p).lower() == cue_album['file'].lower()]
    if named:
        src_audio = named[0]

    info = probe(src_audio)
    title = album or cue_album['title'] or os.path.splitext(os.path.basename(src_audio))[0]
    per = artist or cue_album['performer'] or '未知艺人'
    # CUE 的顶层 PERFORMER 常写"未知艺术家"或"群星"，这种别拿来当目录名
    if not artist and re.search(r'未知|unknown|various', per, re.I):
        per = '群星'
    yr = year or ''
    if not yr:
        m = re.search(r'(\d{4})', os.path.basename(os.path.dirname(src)) + ' ' + os.path.basename(src))
        yr = m.group(1) if m else ''

    qtag = '[%d-%s]' % (info['bps'], ('%g' % (info['sr'] / 1000.0)))
    dirname = ((('(%s) ' % yr) if yr else '') + safe(title) + ' ' + qtag)
    dest = os.path.join(LIB, safe(per), dirname)

    print('源盘    : ' + src_audio)
    print('CUE     : ' + cue_path + '  (%d 轨, %s)' % (len(tracks), 'GBK/UTF8 自动识别'))
    print('规格    : %dbit / %s / %dch / %.1f 秒' % (info['bps'], fmt_khz(info['sr']), info['ch'], info['dur']))
    print('艺人    : ' + per)
    print('专辑    : ' + title + (('  (' + yr + ')') if yr else '  (无年份)'))
    print('目标    : ' + dest)
    for t in tracks[:3]:
        print('  样例  : %02d  %s  @ %.2fs' % (t['n'], t['title'] or '(无标题)', t['start']))
    if len(tracks) > 3:
        print('   …共 %d 轨' % len(tracks))

    if dry:
        print('\n--dry-run：什么都没写。')
        return dest

    if os.path.isdir(dest) and os.listdir(dest):
        die('目标目录已经有文件了，先确认是不是拆过：\n' + dest)

    # 先看盘：整轨 wav 拆成 flac 大约缩到 55~65%，按音频总时长估一个上限，
    # 空间不够就当场停下——写到一半没空间会留下一堆半截文件，比不写更麻烦。
    need = info['dur'] * info['sr'] * info['ch'] * (info['bps'] / 8.0) * 0.7
    free = shutil.disk_usage(os.path.dirname(LIB)).free
    if free < need:
        die('目标盘空间不够：约需 %.0f MB，只剩 %.0f MB。\n先腾出空间再拆，现在什么都没写。'
            % (need / 1048576, free / 1048576))
    print('空间    : 约需 %.0f MB，目标盘剩 %.1f GB' % (need / 1048576, free / 2**30))

    os.makedirs(dest, exist_ok=True)
    total = len(tracks)
    fails = []
    for i, t in enumerate(tracks):
        start = t['start']
        end = tracks[i + 1]['start'] if i + 1 < total else info['dur']
        dur = max(0.0, end - start)
        if dur <= 0.05:
            fails.append((t, '时长为 0，跳过'))
            continue
        name = '%02d - %s.flac' % (t['n'], safe(t['title'] or ('曲目 %02d' % t['n'])))
        out = os.path.join(dest, name)
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
               '-ss', '%.3f' % start, '-t', '%.3f' % dur, '-i', src_audio,
               '-map', '0:a:0', '-c:a', 'flac', '-compression_level', '8',
               '-metadata', 'TITLE=' + (t['title'] or ''),
               '-metadata', 'TRACKNUMBER=%d' % t['n'],
               '-metadata', 'ALBUM=' + title,
               '-metadata', 'ARTIST=' + (t['performer'] or per),
               '-metadata', 'ALBUMARTIST=' + per,
               ]
        if yr:
            cmd += ['-metadata', 'DATE=' + yr]
        cmd.append(out)
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if r.returncode != 0 or not os.path.exists(out):
            fails.append((t, (r.stderr or 'ffmpeg 无输出')[:300]))
            print('  %02d/%d 失败：%s' % (t['n'], total, (r.stderr or '')[:200]))
        else:
            print('  %02d/%d  %s  %.1fMB' % (t['n'], total, name[:60], os.path.getsize(out) / 1048576))

    # 封面：目录里第一张图片复制成 folder.jpg，页面优先认这个名字
    for n in sorted(os.listdir(src)):
        p = os.path.join(src, n)
        if os.path.isfile(p) and os.path.splitext(n)[1].lower() in ('.jpg', '.jpeg', '.png', '.webp'):
            try:
                shutil.copy2(p, os.path.join(dest, 'folder.jpg'))
                print('封面    : ' + n + ' → folder.jpg')
            except Exception as e:
                print('封面复制失败：' + str(e))
            break

    if fails:
        print('\n有 %d 轨没拆出来：' % len(fails))
        for t, why in fails:
            print('  %02d %s :: %s' % (t['n'], t['title'], why[:160]))
        print('目标目录保留着，先别删，把这段贴给 agent。')
        return dest

    print('\n拆出 %d 轨 → %s' % (total, dest))
    if move_raw:
        want = os.path.getsize(src_audio) if os.path.isfile(src_audio) else 0
        free = shutil.disk_usage(os.path.dirname(LIB)).free
        if free < want * 1.15:
            print('原盘没搬：_原盘 所在盘只剩 %.1f GB，装不下这 %.0f MB。'
                  '原盘留在原地，确认拆得没问题后自己删掉即可。'
                  % (free / 2**30, want / 1048576))
        else:
            os.makedirs(RAW, exist_ok=True)
            raw_dest = os.path.join(RAW, safe(per) + ' - ' + safe(title))
            if os.path.exists(raw_dest):
                raw_dest += '-%d' % len(os.listdir(RAW))
            try:
                shutil.move(src, raw_dest)
                print('原盘已移到：' + raw_dest)
            except Exception as e:
                print('原盘没搬成（%s）。原盘仍在原地，不影响已拆出的文件。' % e)
    else:
        print('原盘仍在原地：' + src)
        print('（默认不搬，E 盘常年吃紧。确认拆好了就自己删掉它，或加 --move-raw 让脚本搬到 _原盘/。）')
    print('下一步：回到 音乐库.html 点「＋ 专辑入库」，它会把这张盘写进 data.js；')
    print('        再跑 python 音乐库.py 校准 --fix 补真实时长。')
    return dest


def scan():
    """扫 无损/ 下还有哪些整轨碟（一个音频 + 一个 cue）没拆。"""
    hits = []
    for art in sorted(os.listdir(LIB)):
        ap = os.path.join(LIB, art)
        if not os.path.isdir(ap) or art in SKIP_DIRS:
            continue
        for alb in sorted(os.listdir(ap)):
            bp = os.path.join(ap, alb)
            if not os.path.isdir(bp):
                continue
            if find_cue(bp) and len(find_audio(bp)) <= 1:
                hits.append(bp)
    if not hits:
        print('没有待拆分的整轨碟。')
        return
    print('还有 %d 张整轨碟没拆：' % len(hits))
    for h in hits:
        print('  ' + h)
    print('\n逐张跑：python 音乐库.py 拆轨 --src "<上面那条路径>"')


def main():
    ap = argparse.ArgumentParser(description='按 CUE 拆分整轨音频并放进音乐库')
    ap.add_argument('--src', help='整轨碟所在目录（含 .cue 与一个音频文件）')
    ap.add_argument('--artist', help='强制指定艺人（默认取 CUE 的 PERFORMER）')
    ap.add_argument('--album', help='强制指定专辑名（默认取 CUE 的 TITLE）')
    ap.add_argument('--year', help='强制指定年份（默认从 CUE TITLE 或路径里找 4 位数字）')
    ap.add_argument('--dry-run', action='store_true', help='只打印计划，不动磁盘')
    ap.add_argument('--scan', action='store_true', help='列出 无损/ 下所有待拆的整轨碟')
    ap.add_argument('--move-raw', action='store_true', help='拆完把原盘搬到 _原盘/（默认原地不动）')
    a = ap.parse_args()
    if a.scan:
        scan()
        return
    if not a.src:
        die('要么给 --src，要么用 --scan 看有哪些要拆。')
    if not os.path.isdir(a.src):
        die('目录不存在：' + a.src)
    if not shutil.which('ffmpeg'):
        die('找不到 ffmpeg，先装好并加进 PATH。')
    split_one(a.src, a.artist, a.album, a.year, a.dry_run, a.move_raw)


if __name__ == '__main__':
    main()
