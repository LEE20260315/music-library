# -*- coding: utf-8 -*-
"""扫描音乐库，算出每首曲目的【真实可播时长】，生成 durations.js。

为什么需要这个工具
------------------
库里大量 FLAC 的 STREAMINFO 里 total_samples 字段被写成了**整张专辑**的时长
（典型症状：2002《River Of Stars》十首曲目 header 都写 3112 秒 / 51:52，
实际每首只有 4~6 分钟）。音频数据本身是完整的，只是头部时长字段错了。

浏览器（Chrome/Edge）直接信 header，把 duration 报成 51:52。于是：
  · 进度条总时长显示成 51:52，能拖到的位置远远超过真实音频长度；
  · 一拖就落到"文件末尾之后"，浏览器立刻判定播放结束（ended），
    听感就是"拖动后没声音 / 跳到下一首了"。

这个脚本只读每个 FLAC 的头部元数据段和文件尾部 192KB，用帧头里的绝对采样号
算出真实时长（误差大多在 0.1 秒、个别差 1~5 秒且偏短，对播放安全），
输出 durations.js 给播放器当"真实时长表"使用。

用法（在本目录打开 PowerShell）：
    python 校准时长.py                # 全库扫描，生成 durations.js
    python 校准时长.py --sample 20    # 只抽 20 个看看结果
    python 校准时长.py --fix --dry-run   # 预览将要修正哪些文件的头部时长
    python 校准时长.py --fix          # 就地修正 FLAC 头部时长（可撤销，见下）
"""
import sys, os, json, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 曲目路径里偶尔有 GBK 表示不了的字符（例如名字里混了 U+FEFF），
# 直接 print 到重定向文件会 UnicodeEncodeError 把整趟扫描废掉。
try:
    sys.stdout.reconfigure(errors='replace')
except Exception:
    pass

ROOT = Path(__file__).resolve().parent          # 音乐库根目录
LIB = ROOT / '无损'
OUT_JS = ROOT / 'durations.js'
BACKUP = ROOT / '.时长修正备份.json'             # 修正前的原始值，便于撤销
TAIL = 192 * 1024

BS_TABLE = {1: 192, 2: 576, 3: 1152, 4: 2304, 5: 4608,
            8: 256, 9: 512, 10: 1024, 11: 2048, 12: 4096, 13: 8192,
            14: 16384, 15: 32768}

# CRC-8 查表：逐位算太慢（尾部 192KB 要扫几万个候选），查表快一个数量级
_CRC8 = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = ((_c << 1) ^ 0x07) & 0xFF if _c & 0x80 else (_c << 1) & 0xFF
    _CRC8.append(_c)


def crc8(buf):
    c = 0
    for b in buf:
        c = _CRC8[c ^ b]
    return c


def utf8_num(buf, i):
    """FLAC 帧头里的数字用的是 UTF-8 式变长编码。"""
    if i >= len(buf):
        return None, i
    b = buf[i]
    if b < 0x80:
        return b, i + 1
    n = 0
    while n < 8 and (b & (0x80 >> n)):
        n += 1
    if n < 2 or n > 7 or i + n > len(buf):
        return None, i
    val = b & (0xFF >> (n + 1))
    for k in range(1, n):
        c = buf[i + k]
        if (c & 0xC0) != 0x80:
            return None, i
        val = (val << 6) | (c & 0x3F)
    return val, i + n


def parse_frame(buf, i, stream_bs):
    """解析 FLAC 帧头，带 CRC-8 校验，防止把随机字节当成帧。

    返回 (绝对起始采样号, 本帧采样数) 或 None。
    """
    if i + 9 > len(buf):
        return None
    if buf[i] != 0xFF or (buf[i + 1] & 0xFE) != 0xF8:
        return None
    variable = bool(buf[i + 1] & 0x01)          # 0=定长帧，1=变长帧
    bs_code = buf[i + 2] >> 4
    sr_code = buf[i + 2] & 0x0F
    if bs_code == 0 or sr_code == 0x0F:
        return None
    if buf[i + 3] & 0x01:
        return None
    num, j = utf8_num(buf, i + 4)
    if num is None:
        return None
    if bs_code == 6:
        if j + 1 > len(buf):
            return None
        bs = buf[j] + 1
        j += 1
    elif bs_code == 7:
        if j + 2 > len(buf):
            return None
        bs = int.from_bytes(buf[j:j + 2], 'big') + 1
        j += 2
    else:
        bs = BS_TABLE.get(bs_code)
    if not bs:
        return None
    if sr_code == 12:
        j += 1
    elif sr_code in (13, 14):
        j += 2
    if j >= len(buf) or crc8(buf[i:j]) != buf[j]:
        return None
    base = num if variable else num * (stream_bs or bs)
    return base, bs


def find_frames(buf, stream_bs):
    """在 buf 里找出所有合法帧头。

    关键：不要逐字节试。帧同步码是 0xFF 后跟 0xF8/0xF9，
    用 bytes.find 直接跳到下一个 0xFF，比 192KB × 每字节一次解析快得多。
    """
    out = []
    i = buf.find(b'\xff', 0)
    while i >= 0 and i + 9 <= len(buf):
        if (buf[i + 1] & 0xFE) == 0xF8:
            r = parse_frame(buf, i, stream_bs)
            if r:
                out.append((i, r[0], r[1]))
        i = buf.find(b'\xff', i + 1)
    return out


def read_head(fh, stream_bs, limit=1 << 20, chunk=1 << 16):
    """跳过元数据块，读音频数据开头，返回第一个合法帧的起始采样号。

    逐段增量读：绝大多数文件第一段 64KB 里就能找到首帧，
    没必要一次性把 1MB 读进来（慢盘上这就是几十毫秒的差别）。
    """
    fh.seek(4)
    while True:
        hdr = fh.read(4)
        if len(hdr) < 4:
            return None
        last = hdr[0] & 0x80
        fh.seek(int.from_bytes(hdr[1:4], 'big'), 1)
        if last:
            break
    buf = b''
    while len(buf) < limit:
        more = fh.read(chunk)
        if not more:
            break
        buf += more
        frames = find_frames(buf, stream_bs)
        if frames:
            return frames[0][1]
    return None


def analyse(path):
    """返回 dict：hdr 头部声明时长、real 真实时长（秒）、sr 采样率、ok 是否可信。"""
    try:
        with path.open('rb') as fh:
            head = fh.read(42)
            if len(head) < 42 or head[:4] != b'fLaC':
                return {'err': 'notflac'}
            b = head[8:42]
            packed = int.from_bytes(b[10:18], 'big')
            sr = (packed >> 44) & 0xFFFFF
            max_bs = int.from_bytes(b[2:4], 'big')
            hdr_total = packed & 0xFFFFFFFFF
            if not sr:
                return {'err': 'nosr'}
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            off = max(0, size - TAIL)
            fh.seek(off)
            tail = fh.read()
            first = read_head(fh, max_bs)
    except OSError as e:
        return {'err': type(e).__name__}

    # 尾部：把能连成链的帧挑出来（位置递增 + 采样号连续），噪声很难连续成立
    cands = find_frames(tail, max_bs)
    chains, cur = [], []
    for c in cands:
        if cur:
            pi, pbase, pbs = cur[-1]
            if 32 <= c[0] - pi <= 65536 and abs(c[1] - (pbase + pbs)) <= 2:
                cur.append(c)
                continue
            chains.append(cur)
        cur = [c]
    if cur:
        chains.append(cur)
    good = [ch for ch in chains if len(ch) >= 3] or chains
    if not good:
        return {'err': 'noframe', 'sr': sr, 'hdr': round(hdr_total / sr, 1)}

    best = max(good, key=lambda ch: ch[-1][0])
    pos, base, bs = best[-1]
    last_end = base + bs
    # 末帧之后还有不足一帧的残余字节，按这串帧的中位帧长折算补上
    if len(best) >= 2:
        gaps = sorted(best[i][0] - best[i - 1][0] for i in range(1, len(best)))
        bss = sorted(best[i][2] for i in range(1, len(best)))
        gap, spf = gaps[len(gaps) // 2], bss[len(bss) // 2]
        remain = size - (off + pos)
        if gap > 0 and spf > 0 and remain > 0:
            last_end += int(remain / gap * spf)

    real = (last_end - (first or 0)) / sr
    hdr = hdr_total / sr
    if real <= 0.5 or real > 6 * 3600:
        return {'err': 'implausible', 'sr': sr, 'hdr': round(hdr, 1), 'real': round(real, 1)}
    # offset：这首曲目第一帧的绝对采样号 / 采样率。
    # 库里这些 FLAC 是从整张专辑里切出来的，帧头保留了专辑里的绝对采样号，
    # 所以第 2 首起浏览器会把 currentTime 报成"在专辑里的位置"（例如 931.3s），
    # 而不是从 0 开始。播放器必须减掉它才能算对进度、拖对地方。
    return {'sr': sr, 'hdr': round(hdr, 1), 'real': round(real, 2),
            'off': round((first or 0) / sr, 2), 'ok': True}


def patch_header(path, real, sr, old_write=None):
    """只改 STREAMINFO 里的 total_samples 这 40 位，音频数据一个字节都不动。"""
    total = int(round(real * sr))
    with path.open('rb') as fh:
        blob = fh.read(26)
    packed = int.from_bytes(blob[18:26], 'big')
    old = packed & 0xFFFFFFFFF
    if old == total:
        return None
    new_packed = (packed & ~0xFFFFFFFFF) | (total & 0xFFFFFFFFF)
    with path.open('r+b') as fh:
        fh.seek(18)
        fh.write(new_packed.to_bytes(8, 'big'))
    return old


def restore_header(path, old_total):
    """把 total_samples 写回备份里的原值（撤销用）。"""
    with path.open('rb') as fh:
        blob = fh.read(26)
    packed = int.from_bytes(blob[18:26], 'big')
    if (packed & 0xFFFFFFFFF) == old_total:
        return None
    new_packed = (packed & ~0xFFFFFFFFF) | (old_total & 0xFFFFFFFFF)
    with path.open('r+b') as fh:
        fh.seek(18)
        fh.write(new_packed.to_bytes(8, 'big'))
    return True


def undo_all():
    """把所有改过的头部时长按备份原样写回。18756 个文件写到一半断电，
    这里也能整批救回来——备份是一个文件配一个原值，不依赖顺序。"""
    if not BACKUP.exists():
        print('没有找到备份文件，没东西可撤销：', BACKUP.name)
        return
    try:
        backup = json.loads(BACKUP.read_text(encoding='utf-8'))
    except Exception as e:
        print('备份文件读不出来：', e)
        return
    n = skipped = 0
    for rel, old in backup.items():
        p = LIB / rel
        if not p.exists():
            skipped += 1
            continue
        try:
            if restore_header(p, int(old)):
                n += 1
                if n % 500 == 0:
                    print(f'  已还原 {n} 个…', flush=True)
        except Exception:
            skipped += 1
    print(f'撤销完成：还原 {n} 个文件，跳过 {skipped} 个（文件不在或写不进去）。', flush=True)
    print('备份文件保留未删，确认无误后可以自己删掉 ' + BACKUP.name, flush=True)


def all_flac():
    out = []
    for dirpath, _, filenames in os.walk(LIB):
        for fn in filenames:
            if fn.lower().endswith('.flac'):
                out.append(Path(dirpath) / fn)
    out.sort()
    return out


def main():
    do_fix = '--fix' in sys.argv
    dry = '--dry-run' in sys.argv

    if '--undo' in sys.argv:
        undo_all()
        return

    if not LIB.is_dir():
        print('找不到无损目录：', LIB)
        return

    files = all_flac()
    print(f'共 {len(files)} 个 FLAC 文件，开始扫描…', flush=True)

    if '--sample' in sys.argv:
        n = int(sys.argv[sys.argv.index('--sample') + 1])
        step = max(1, len(files) // n)
        for f in files[::step][:n]:
            r = analyse(f)
            print(f'  头部 {r.get("hdr")}s  真实 {r.get("real")}s  {f.relative_to(LIB)}', flush=True)
        return

    results, offs, bad, broken = {}, {}, [], []
    metas = {}                                  # rel -> (采样率, 头部声明时长)，--fix 时免去重新解析
    lock = threading.Lock()
    done = [0]

    def work(f):
        r = analyse(f)
        rel = str(f.relative_to(LIB)).replace('\\', '/')
        with lock:
            done[0] += 1
            if r.get('ok'):
                results[rel] = r['real']
                metas[rel] = (r.get('sr'), r.get('hdr'))
                if r.get('off', 0) > 0.05:
                    offs[rel] = r['off']
            else:
                bad.append((rel, r.get('err')))
                # 少数文件在磁盘上就已经只剩几十 KB（解码不到 1 秒），
                # 这类单独标出来，播放器会提示"文件可能已损坏"，而不是假装能放。
                if r.get('err') == 'implausible' and (r.get('real') or 0) < 1.0:
                    broken.append(rel)
            if done[0] % 500 == 0:
                print(f'  {done[0]}/{len(files)}  已识别 {len(results)}  失败 {len(bad)}', flush=True)

    # 慢盘上多线程读文件能明显提速
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, files))

    OUT_JS.write_text(
        '/* 由「校准时长.py」生成，请勿手改。\n'
        '   LIB_DUR：每首曲目的真实可播时长（秒）。很多 FLAC 的头部时长被写成了\n'
        '            整张专辑的长度，播放器用它当进度条上限，才不会一拖就撞到文件末尾。\n'
        '   LIB_OFF：曲目第一帧在"整张专辑时间轴"上的起始秒数。这些文件是从整张专辑\n'
        '            切出来的，帧头保留了专辑里的绝对采样号，浏览器因此把 currentTime\n'
        '            报成 931.3 这种"专辑里的位置"；减掉它才是这首曲目自己的播放位置。 */\n'
        'window.LIB_DUR=' + json.dumps(results, ensure_ascii=False, separators=(',', ':')) + ';\n'
        'window.LIB_OFF=' + json.dumps(offs, ensure_ascii=False, separators=(',', ':')) + ';\n'
        '/* LIB_BAD：磁盘上已经损坏、只剩几十 KB 的文件，播放器会如实提示，不假装能放。 */\n'
        'window.LIB_BAD=' + json.dumps(sorted(set(broken)), ensure_ascii=False, separators=(',', ':')) + ';\n',
        encoding='utf-8')
    print(f'已写出 {OUT_JS.name}：{len(results)} 条时长、{len(offs)} 条起始偏移、'
          f'{len(set(broken))} 条损坏文件，解析失败 {len(bad)} 条', flush=True)

    if bad:
        (ROOT / '.时长识别失败.txt').write_text(
            '\n'.join(f'{r}\t{e}' for r, e in bad), encoding='utf-8')

    if not do_fix:
        print('提示：加 --fix 可以把错误的头部时长直接改正（改完进度条就准了）。', flush=True)
        return

    # ---- 就地修正头部时长 ----
    backup = {}
    if BACKUP.exists():
        try:
            backup = json.loads(BACKUP.read_text(encoding='utf-8'))
        except Exception:
            backup = {}
    fixed = 0
    plan = []                                    # dry-run 的明细：逐条列出来太吵，写进文件
    for f in files:
        rel = str(f.relative_to(LIB)).replace('\\', '/')
        real = results.get(rel)
        if not real:
            continue
        sr, hdr = metas.get(rel, (None, None))
        if not sr or abs((hdr or 0) - real) < 1.0:
            continue
        if dry:
            # 这里原来写的是 r.get("hdr")，而 r 是 main 里不存在的变量（它在 work()
            # 的局部作用域里），--fix --dry-run 必然 NameError。用循环里的 hdr 才是对的值。
            plan.append(f'{hdr:.3f}\t{real:.3f}\t{rel}')
            fixed += 1
            continue
        old = patch_header(f, real, sr)
        if old is not None:
            backup[rel] = old
            fixed += 1
            # 边改边存：18756 个文件要写好一会儿，中途断了、拔盘了，
            # 已改的那部分必须在备份里，否则 --undo 救不回来。
            if fixed % 200 == 0:
                BACKUP.write_text(json.dumps(backup, ensure_ascii=False), encoding='utf-8')
                print(f'  已修正 {fixed} 个…', flush=True)
    if not dry:
        BACKUP.write_text(json.dumps(backup, ensure_ascii=False), encoding='utf-8')
        print(f'已修正 {fixed} 个文件的头部时长，原值备份在 {BACKUP.name}', flush=True)
    else:
        plan_path = ROOT / '.时长预览.txt'
        plan_path.write_text('\n'.join(plan), encoding='utf-8')
        print(f'预览：将修正 {fixed} 个文件（未写入）。明细见 {plan_path.name}', flush=True)


if __name__ == '__main__':
    main()
