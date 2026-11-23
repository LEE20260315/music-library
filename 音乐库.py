# -*- coding: utf-8 -*-
"""音乐库工具统一入口。"""
import argparse
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

COMMANDS = {
    '导入': ('导入文件夹.py', 'tools.导入'),
    '拆轨': ('拆分CUE.py', 'tools.拆轨'),
    '校准': ('校准时长.py', 'tools.校准'),
    '检查': ('检查音乐库.py', 'tools.检查'),
    '歌词': ('补歌词.py', 'tools.歌词'),
    '歌詞': ('补歌词.py', 'tools.歌词'),
}

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print('用法：python 音乐库.py <子命令> [参数]')
        print('子命令：' + '、'.join(COMMANDS))
        return 2
    _, module_name = COMMANDS[sys.argv[1]]
    from importlib import import_module
    module = import_module(module_name)
    sys.argv = [os.path.join(os.path.dirname(__file__), COMMANDS[sys.argv[1]][0])] + sys.argv[2:]
    result = module.main()
    return result if isinstance(result, int) else 0

if __name__ == '__main__':
    raise SystemExit(main())
