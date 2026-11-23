# -*- coding: utf-8 -*-
"""音乐库工具统一入口。"""
import argparse
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

COMMANDS = {
    '导入': ('tools.导入',),
    '拆轨': ('tools.拆轨',),
    '校准': ('tools.校准',),
    '检查': ('tools.检查',),
    '歌词': ('tools.歌词',),
    '歌詞': ('tools.歌词',),
}

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print('用法：python 音乐库.py <子命令> [参数]')
        print('子命令：' + '、'.join(dict.fromkeys(m[0].split('.')[1] for m in COMMANDS.values())))
        return 2
    (module_name,) = COMMANDS[sys.argv[1]]
    from importlib import import_module
    module = import_module(module_name)
    # argv[0] 只用于 argparse 的 usage 显示，写成调用方式本身比内部文件名清楚
    sys.argv = ['音乐库.py ' + sys.argv[1]] + sys.argv[2:]
    result = module.main()
    return result if isinstance(result, int) else 0

if __name__ == '__main__':
    raise SystemExit(main())
