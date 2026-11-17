# -*- coding: utf-8 -*-
"""启动器生成/检查的验证。

只测 --check 的判定逻辑（不测生成，生成要求同目录有 exe）。
这一条很关键：它是「桌面入口是不是旧的」这个问题的唯一自动化判据，
如果它自己坏了，下次发版又会悄悄漂移。

用法：
    python test_launcher.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core  # noqa: E402
import make_launcher as ml  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    if ok:
        print(f"  OK    {name}" + (f"  ({detail})" if detail else ""))
    else:
        fails += 1
        print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))


def write_bat(path: Path, text: str) -> None:
    path.write_bytes(text.replace("\n", "\r\n").encode("gbk", errors="replace"))


def main() -> int:
    core._ensure_utf8_stdout()
    print("=" * 58)
    print("启动器检查逻辑")
    print("=" * 58)

    tmp = Path(tempfile.mkdtemp(prefix="wu_lc_"))
    name = "launcher.bat"

    # 1) 版本与当前程序一致 → 0
    write_bat(tmp / name, f"@echo off\nrem   版本 : {core.APP_VERSION}\n")
    rc = ml.check(tmp, name)
    check("版本一致时返回 0", rc == 0, f"rc={rc}")

    # 2) 版本对不上 → 非 0
    write_bat(tmp / name, "@echo off\nrem   版本 : 1.0.0\n")
    rc = ml.check(tmp, name)
    check("版本过期时返回非 0", rc != 0, f"rc={rc}")

    # 3) 老格式（没有版本行）→ 非 0，提示重新生成
    write_bat(tmp / name, "@echo off\nstart \"\" \"x.exe\"\n")
    rc = ml.check(tmp, name)
    check("没有版本标记时返回非 0", rc != 0, f"rc={rc}")

    # 4) 文件不存在 → 非 0
    rc = ml.check(tmp, "no-such-file.bat")
    check("文件不存在时返回非 0", rc != 0, f"rc={rc}")

    # 5) 版本号是 2.1 而当前是 2.1.7 时必须判为不一致（防“前缀匹配”误判）
    write_bat(tmp / name, "@echo off\nrem   版本 : 2.1\n")
    rc = ml.check(tmp, name)
    check("前缀相同但版本不同也要判为过期", rc != 0, f"rc={rc}")

    print()
    if fails == 0:
        print("启动器检查逻辑全部通过")
    else:
        print(f"启动器检查失败 {fails} 项")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
