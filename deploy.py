# -*- coding: utf-8 -*-
"""把打包好的 exe 部署到指定目录（通常是桌面），并校验目标是不是最新那份。

为什么需要单独一个脚本：桌面放的是 exe 的**副本**，程序升级后必须重新复制，
否则桌面上那个还是旧版。这一步没法自动——只能在发版时跑一次。
配 --check 就可以随时确认有没有漏。

用法：
    python deploy.py "C:\\Users\\you\\Desktop"
    python deploy.py --check "C:\\Users\\you\\Desktop"

--check 先比大小，不同立即判为过期；大小相同再比 SHA256，避免同尺寸不同内容
的巧合。过期时返回非零。

关于数据目录：程序本身把 backup/logs 放在 %LOCALAPPDATA%\\WindowsUpdatePauseTool，
不跟随 exe。所以往桌面复制一份 exe 不会在桌面上长出任何文件夹。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXE_NAME = "WindowsUpdatePause.exe"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check(out_dir: Path) -> int:
    src = HERE / EXE_NAME
    dst = out_dir / EXE_NAME

    if not dst.exists():
        print(f"目标没有 exe：{dst}")
        print("需要先部署一次。")
        return 1
    if not src.exists():
        print(f"源 exe 不存在：{src}")
        print("请先打包。")
        return 1

    s_size, d_size = src.stat().st_size, dst.stat().st_size
    if s_size != d_size:
        print(f"目标已过期：大小 {d_size} ≠ 源 {s_size}")
        return 1

    if sha256(src) != sha256(dst):
        print("目标已过期：大小相同但内容不同")
        return 1

    print(f"目标与源一致（{s_size} 字节）")
    return 0


def deploy(out_dir: Path) -> int:
    src = HERE / EXE_NAME
    if not src.exists():
        print(f"源 exe 不存在：{src}")
        print("请先打包（见 README）。")
        return 1

    dst = out_dir / EXE_NAME
    size = src.stat().st_size

    try:
        shutil.copy2(src, dst)
    except PermissionError:
        print(f"写不进去：{dst}")
        print("目标可能正在运行——先把它关掉再试。")
        return 1
    except OSError as e:
        print(f"复制失败：{e}")
        return 1

    print(f"已部署：{dst}")
    print(f"  大小 : {size} 字节 ({size / 1024 / 1024:.2f} MB)")
    print(f"  SHA  : {sha256(dst)[:16]}…")

    # 顺手把旁边遗留的 .bat 指出来，但不擅自删——那是用户的文件
    leftovers = sorted(p for p in out_dir.glob("*.bat")
                       if "更新" in p.name or "暂停" in p.name)
    if leftovers:
        print()
        print("注意：这个目录里还有旧的 .bat 入口：")
        for p in leftovers:
            print(f"  {p.name}")
        print("现在可以直接双击 exe 了，那些 .bat 可以删掉。")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    args = sys.argv[1:]
    do_check = False
    if args[0] in ("--check", "-c"):
        do_check = True
        args = args[1:]
        if not args:
            print(__doc__)
            return 2

    out_dir = Path(args[0]).expanduser()
    if not out_dir.is_dir():
        print(f"目标目录不存在：{out_dir}")
        return 2

    return check(out_dir) if do_check else deploy(out_dir)


if __name__ == "__main__":
    sys.exit(main())
