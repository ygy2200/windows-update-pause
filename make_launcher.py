# -*- coding: utf-8 -*-
"""生成一个指向本程序的启动器 .bat。

解决的问题：桌面入口是个固定路径的 .bat，内容永远不变，所以**从文件本身看不出
它指向哪一版**。工具升级几次之后，用户（和我自己）都会怀疑桌上那个是不是旧的。

现在启动器自带版本号注释，一眼可核对。

用法：
    python make_launcher.py <输出目录> [文件名]
    python make_launcher.py --check <输出目录> [文件名]

例：
    python make_launcher.py "C:\\Users\\you\\Desktop"
    python make_launcher.py --check "C:\\Users\\you\\Desktop"

--check 用来确认桌面那个启动器还跟得上当前版本：它读启动器里的版本注释，
与 core.APP_VERSION 比对，不一致就以非零退出码返回。发版后跑一次即可，
不用靠记性。

为什么用 GBK 写：cmd 按系统代码页（简体中文 Windows 是 936）读 .bat，
UTF-8 的中文会显示成乱码。ASCII 部分不受影响，但中文提示会坏。
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

DEFAULT_NAME = "暂停更新40年.bat"
VERSION_LINE = re.compile(r"rem\s+版本\s*:\s*(\S+)")

TEMPLATE = """@echo off
rem ================================================================
rem   {app_name}
rem   版本 : {version}
rem   生成 : {built}
rem   指向 : {exe}
rem ================================================================
rem   想确认用的是不是最新版：打开本文件看上面「版本」一行，
rem   与程序窗口标题栏或 --version 输出对照即可。
rem ================================================================
set "EXE={exe}"
if not exist "%EXE%" goto missing
start "" "%EXE%"
exit /b 0

:missing
echo.
echo   找不到程序文件：
echo     %EXE%
echo.
echo   程序目录可能被移动、重命名或删除了。
echo   重新放回原位置，或用新目录下的本启动器。
echo.
pause
exit /b 1
"""


def build(exe: Path) -> str:
    return TEMPLATE.format(
        app_name=core.APP_NAME,
        version=core.APP_VERSION,
        built=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        exe=exe,
    ).replace("\n", "\r\n")


def check(out_dir: Path, name: str) -> int:
    """启动器里的版本注释是否跟当前程序一致。不一致说明该重新生成了。"""
    target = out_dir / name
    if not target.exists():
        print(f"启动器不存在：{target}")
        return 1
    try:
        text = target.read_bytes().decode("gbk", errors="replace")
    except OSError as e:
        print(f"读不出来：{e}")
        return 1

    m = VERSION_LINE.search(text)
    if not m:
        print("启动器里没有版本标记（老格式），建议重新生成")
        return 1

    found = m.group(1)
    if found != core.APP_VERSION:
        print(f"启动器版本 {found} ≠ 程序版本 {core.APP_VERSION} —— 需要重新生成")
        return 1

    print(f"启动器版本 {found}，与程序一致")
    return 0


def main() -> int:
    core._ensure_utf8_stdout()

    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2

    do_check = False
    if args[0] in ("--check", "-c"):
        do_check = True
        args = args[1:]
        if not args:
            print(__doc__)
            return 2

    out_dir = Path(args[0]).expanduser()
    name = args[1] if len(args) > 1 else DEFAULT_NAME

    if not out_dir.is_dir():
        print(f"输出目录不存在：{out_dir}")
        return 2

    if do_check:
        return check(out_dir, name)

    # 启动器要指向 exe。用脚本自身所在目录，而不是 sys.executable——
    # 源码运行时后者是 python.exe 的路径，会指向一个完全无关的地方。
    # 找不到 exe 就明确报错，不生成一个指向空气的启动器。
    exe = Path(__file__).resolve().parent / "WindowsUpdatePause.exe"
    if not exe.exists():
        print(f"找不到 exe：{exe}")
        print("请先打包（见 README），再在同目录生成启动器。")
        return 1

    target = out_dir / name
    content = build(exe)

    # 先写临时文件再替换：避免中途失败留下一个半截的 .bat
    fd, tmp = tempfile.mkstemp(prefix=".wu_launcher_", suffix=".tmp",
                               dir=str(out_dir))
    os.close(fd)
    try:
        Path(tmp).write_bytes(content.encode("gbk", errors="replace"))
        os.replace(tmp, target)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print(f"写入失败：{e}")
        return 1

    print(f"已生成：{target}")
    print(f"  版本 : {core.APP_VERSION}")
    print(f"  指向 : {exe}")
    print(f"  大小 : {target.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
