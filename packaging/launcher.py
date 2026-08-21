# -*- coding: utf-8 -*-
"""perfdog.exe 薄启动器 —— 方案 B：exe 只含 Python 运行时 + 本启动器，
collector/ 源码外置（可热更新，改代码无需重新打包）。

等价于 start_perfdog.bat 的行为：cd 到 collector 后运行 `python main.py --web`。
"""
import importlib
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
COLLECTOR_DIR = os.path.join(BASE_DIR, "collector")

if not os.path.isdir(COLLECTOR_DIR):
    print("[!] 未找到外部 collector 目录: %s" % COLLECTOR_DIR)
    print("    请保持 perfdog.exe 与 collector/ 目录位于同一目录。")
    input("按回车退出...")
    sys.exit(1)

if COLLECTOR_DIR not in sys.path:
    sys.path.insert(0, COLLECTOR_DIR)

# main.py 使用相对路径（config.json / output/ 等），必须以其所在目录为工作目录
os.chdir(COLLECTOR_DIR)

# 动态导入 main（避免 PyInstaller 将 main 冻结进 exe，保持外置热更新）
main = importlib.import_module("main")

# 默认与 start_perfdog.bat 一致：无参数时带 --web 启动实时看板
if len(sys.argv) <= 1:
    sys.argv.append("--web")

main.main()
