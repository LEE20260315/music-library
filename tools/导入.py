# -*- coding: utf-8 -*-
r"""从任意文件夹入库：整轨碟按 CUE 拆，多文件专辑直接复制进库。

    python 音乐库.py 导入 --probe "D:\...\Jay"          # 只分析，输出 JSON
    python 音乐库.py 导入 --probe "D:\...\周杰伦日版"      # 上级目录：一批专辑
    python 音乐库.py 导入 --import-root "D:\...\Jay"          # 真入库（拆或复制）
    python 音乐库.py 导入 --import-root "D:\...\周杰伦日版" --all   # 批量

设计原则：
  · 只用标准库 + ffmpeg/ffprobe，和 拆轨.py 复用同一套解析。
  · 不改 data.js：入库仍旧走页面/服务那条已验证的路径，这里只负责把文件放到
    无损/<艺人>/<目录名>/ 下并把 rel 报出来。
  · 复制用 copy2 保留时间戳；源目录默认不动（用户自己决定删）。
"""
import argparse, json, os, re, shutil, subprocess, sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, '无损')

from tools import 拆轨 as cq          # 复用 read_text / parse_cue / probe / safe / fmt_khz

AUDIO_EXT = cq.AUDIO_EXT
PLAYABLE = {'.flac', '.wav', '.m4a', '.mp3', '.aac', '.ogg', '.opus'}
IMG_EXT = {'.jpg', '.jpeg', '.png', '.webp'}


def listdir(p):
    try:
        return sorted(os.listdir(p))
    except Exception:
        return []


def audio_files(d):
    return [n for n in listdir(d) if os.path.splitext(n)[1].lower() in AUDIO_EXT
            and os.path.isfile(os.path.join(d, n))]


def cue_files(d):
    return [n for n in listdir(d) if n.lower().endswith('.cue')]


def first_image(d):
    for n in listdir(d):
        if os.path.splitext(n)[1].lower() in IMG_EXT:
            return n
    return ''


def read_tags(path):
    """从文件标签里捞 艺人/专辑/年份：整轨碟的 CUE 常常没有 REM DATE，标签里有。"""
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format_tags',
                            '-of', 'json', path], capture_output=True, timeout=60)
        tags = (json.loads(r.stdout.decode('utf-8', 'replace')).get('format') or {}).get('tags') or {}
    except Exception:
        return {}
    low = {k.lower(): v for k, v in tags.items()}
    return low


def year_of(*vals):
    for v in vals:
        m = re.search(r'(19\d{2}|20\d{2})', str(v or ''))
        if m:
            return m.group(1)
    return ''


def probe_dir(d):
    """返回一张专辑的描述；不是专辑就返回 None。"""
    files = audio_files(d)
    if not files:
        return None
    cues = cue_files(d)
    dname = os.path.basename(d.rstrip('\\/'))

    # 1) 整轨碟：一个音频 + 一个 cue
    if len(files) == 1 and cues:
        cue_path = os.path.join(d, cues[0])
        cue_text = cq.read_text(cue_path)
        album, tracks = cq.parse_cue(cue_text)
        src = os.path.join(d, files[0])
        tag = read_tags(src)
        # 顶层 PERFORMER 常是空的（EAC 抓轨默认留空），真正的艺人写在每轨的 PERFORMER 上
        artist = (album.get('performer') or '').strip()
        if not artist or re.search(r'未知|unknown|various', artist, re.I):
            artists = [t['performer'] for t in tracks if t.get('performer')]
            artist = (artists[0] if artists else '') or (tag.get('albumartist') or tag.get('artist') or '') or '群星'
        title = (album.get('title') or '').strip() or (tag.get('album') or '').strip() or \
                os.path.splitext(files[0])[0]
        # 年份：CUE 的 REM DATE / 文件标签 / 目录名，三处都不许瞎猜
        rem = re.search(r'^\s*REM\s+DATE\s+(\d{4})', cue_text, re.I | re.M)
        yr = year_of(rem.group(1) if rem else '', tag.get('date'), dname)
        try:
            info = cq.probe(src)
            quality = '%dbit / %s' % (info['bps'], cq.fmt_khz(info['sr']))
            dur = info['dur']
        except SystemExit:
            quality, dur = '', 0.0
        return {'kind': 'whole-disc', 'dir': d, 'dirname': dname, 'artist': cq.safe(artist),
                'album': cq.safe(title), 'year': yr, 'quality': quality, 'tracks': len(tracks),
                'duration': round(dur, 1), 'cue': cues[0], 'audio': files[0],
                'playable': all(os.path.splitext(f)[1].lower() in PLAYABLE for f in files),
                'cover': first_image(d), 'bytes': sum(os.path.getsize(os.path.join(d, f)) for f in files)}

    # 2) 多文件专辑
    if len(files) >= 2 and len(cues) <= 1:
        # 子目录里还有音频 → 这是一个"一批专辑"的父目录，不是专辑
        sub_has_audio = any(audio_files(os.path.join(d, s)) for s in listdir(d)
                            if os.path.isdir(os.path.join(d, s)))
        # 只看子目录本身有没有音频（GP 的子目录）
        if not sub_has_audio:
            tag = read_tags(os.path.join(d, files[0]))
            artist = (tag.get('albumartist') or tag.get('artist') or '').strip() or '群星'
            title = (tag.get('album') or '').strip() or dname
            yr = year_of(tag.get('date'), dname)
            return {'kind': 'tracks', 'dir': d, 'dirname': dname, 'artist': cq.safe(artist),
                    'album': cq.safe(title), 'year': yr, 'quality': '',
                    'tracks': len(files), 'duration': 0.0, 'cue': cues[0] if cues else '',
                    'audio': '', 'playable': all(os.path.splitext(f)[1].lower() in PLAYABLE for f in files),
                    'cover': first_image(d), 'bytes': sum(os.path.getsize(os.path.join(d, f)) for f in files)}
    return None


def probe_tree(d, depth=0):
    """看 dir 本身是不是一张专辑；不是就往下找一层（父目录里放着一批专辑）。"""
    one = probe_dir(d)
    if one:
        return {'albums': [one], 'parent': None}
    subs = [s for s in listdir(d) if os.path.isdir(os.path.join(d, s)) and not s.startswith('.')]
    albs = []
    for s in subs:
        a = probe_dir(os.path.join(d, s))
        if a:
            albs.append(a)
    if albs:
        return {'albums': albs, 'parent': d}
    return {'albums': [], 'parent': d}


def target_dirname(a):
    return ((('(%s) ' % a['year']) if a['year'] else '') + a['album'] +
            ' [' + (a['quality'].split(' / ')[0].replace('bit', '') if a['quality'] else '?') + '-' +
            ((a['quality'].split(' / ')[1].replace('kHz', '')) if a['quality'] else '?') + ']')


STAGE = os.path.join(ROOT, '_导入中')     # 复制中的暂存区，放在 无损/ 外面，扫描永远看不见


def do_one(a, dry=False):
    """把一张专辑真正落进 无损/。返回 (rel, kind, 写入文件数, 说明)。"""
    rel = '无损/' + a['artist'] + '/' + target_dirname(a)
    dest = os.path.join(ROOT, rel)
    if a['kind'] == 'whole-disc':
        if dry:
            return rel, 'whole-disc', a['tracks'], '将按 CUE 拆成 %d 首（约 %.0f MB FLAC）' % (
                a['tracks'], a['bytes'] * 0.6 / 1048576)
        if os.path.isdir(dest) and os.listdir(dest):
            return rel, 'skip', 0, '目标目录已有文件，跳过'
        created = not os.path.isdir(dest)
        try:
            cq.split_one(a['dir'], artist=a['artist'], album=a['album'], year=a['year'])
        except BaseException:
            # 拆到一半失败会留下半张专辑，而下一次导入看到「目录非空」就永远跳过它。
            # 只清本次新建的目录；原本就存在的目录不碰。
            if created:
                shutil.rmtree(dest, ignore_errors=True)
            raise
        n = len([x for x in listdir(dest) if os.path.splitext(x)[1].lower() == '.flac'])
        return rel, 'whole-disc', n, '拆出 %d 首' % n
    # 多文件：先全部复制到暂存目录，齐了再改名过去（同卷 rename 是原子的）
    if dry:
        return rel, 'copy', a['tracks'], '将复制 %d 个文件（约 %.0f MB）' % (a['tracks'], a['bytes'] / 1048576)
    if os.path.isdir(dest) and os.listdir(dest):
        return rel, 'skip', 0, '目标目录已有文件，跳过'
    stage = os.path.join(STAGE, '%d-%s' % (os.getpid(), re.sub(r'[^\w\-]+', '_', a['album'])[:24]))
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)
    try:
        n = 0
        for f in audio_files(a['dir']):
            shutil.copy2(os.path.join(a['dir'], f), os.path.join(stage, f))
            n += 1
        cov = a['cover']
        if cov:
            shutil.copy2(os.path.join(a['dir'], cov), os.path.join(stage, 'folder.jpg'))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(stage, dest)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return rel, 'copy', n, '复制 %d 个文件' % n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--probe')
    ap.add_argument('--import-root', dest='imp')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--only', help='只处理目录名含这个子串的那张')
    ap.add_argument('--skip', help='跳过这些目录名（\\u0001 分隔）；库里已有同名专辑时由调用方传进来')
    a = ap.parse_args()

    if a.probe:
        r = probe_tree(a.probe)
        print(json.dumps({'ok': True, 'parent': r['parent'], 'albums': r['albums']}, ensure_ascii=False))
        return

    if not a.imp:
        print(json.dumps({'ok': False, 'error': '要么 --probe，要么 --import-root'}, ensure_ascii=False))
        return

    tree = probe_tree(a.imp)
    albs = tree['albums']
    if a.only:
        albs = [x for x in albs if a.only in x['dirname']]
    if a.skip:
        sk = {s for s in a.skip.split('\u0001') if s}
        albs = [x for x in albs if x['dirname'] not in sk]
    if not albs:
        print(json.dumps({'ok': False, 'error': '这个目录里没认出专辑（没有音频，或结构太深）'}, ensure_ascii=False))
        return
    done = []
    for x in albs:
        try:
            rel, kind, n, msg = do_one(x, dry=a.dry_run)
            done.append({'dirname': x['dirname'], 'rel': rel, 'kind': kind, 'files': n,
                         'msg': msg, 'artist': x['artist'], 'album': x['album'], 'year': x['year']})
        except SystemExit as e:
            done.append({'dirname': x['dirname'], 'rel': '', 'kind': 'fail', 'files': 0,
                         'msg': '拆分失败（见上）', 'artist': x['artist'], 'album': x['album'], 'year': x['year']})
        except Exception as e:
            done.append({'dirname': x['dirname'], 'rel': '', 'kind': 'fail', 'files': 0,
                         'msg': str(e)[:200], 'artist': x['artist'], 'album': x['album'], 'year': x['year']})
    print(json.dumps({'ok': True, 'items': done}, ensure_ascii=False))


if __name__ == '__main__':
    main()
