# -*- coding: utf-8 -*-
"""perfdog.exe 启动器 —— 兼容两种打包模式：

- onefile（推荐分发）：整个程序打进单个 exe，启动时解压到临时目录（sys._MEIPASS），
  工作目录固定在 exe 所在目录（output/ 数据持久保存，config.json 首次自动复制过去）。
- onedir（开发热更新）：exe 仅含 Python 运行时 + 本启动器，collector/ 源码外置
  （改代码无需重新打包），行为与 start_perfdog.bat 一致（默认 --web）。

两种模式默认行为相同：等价 `cd collector && python main.py --web`。
"""
import importlib
import os
import shutil
import sys

EXE_DIR = os.path.dirname(os.path.abspath(sys.executable))

if getattr(sys, "_MEIPASS", None):
    # ---- onefile：资源解压在临时目录，源码打包在 _MEIPASS/collector ----
    BUNDLE_DIR = sys._MEIPASS
    COLLECTOR_DIR = os.path.join(BUNDLE_DIR, "collector")
    # 首次运行：把配置模板复制到 exe 目录（用户可改，持久生效）
    for cfg in ("config.json", "config.app.json"):
        src = os.path.join(COLLECTOR_DIR, cfg)
        dst = os.path.join(EXE_DIR, cfg)
        if os.path.isfile(src) and not os.path.isfile(dst):
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass
else:
    # ---- onedir：collector 源码外置在 exe 同级 ----
    COLLECTOR_DIR = os.path.join(EXE_DIR, "collector")

if not os.path.isdir(COLLECTOR_DIR):
    print("[!] 未找到 collector 目录: %s" % COLLECTOR_DIR)
    print("    请保持 perfdog.exe 与 collector/ 目录位于同一目录（onedir 模式）。")
    input("按回车退出...")
    sys.exit(1)

# main.py 使用相对路径（config.json / output/），工作目录固定在 exe 所在目录
os.chdir(EXE_DIR)

if COLLECTOR_DIR not in sys.path:
    sys.path.insert(0, COLLECTOR_DIR)

# 动态导入 main（避免 PyInstaller 把 main 冻结进 exe 的收集阶段，保证热更新/双模式一致）
main = importlib.import_module("main")

# 默认与 start_perfdog.bat 一致：无参数时带 --web 启动实时看板
if len(sys.argv) <= 1:
    sys.argv.append("--web")

main.main()
