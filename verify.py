# -*- coding: utf-8 -*-
"""发版前后自检清单。

为什么有这个脚本：2026-11-17 连续三轮暴露同一类问题——
「交付物不止 exe 一个，但只维护了其中一个」。
发布链路上的东西包括：GitHub Release、桌面入口、项目目录、数据目录、README。
每一轮我都在发完 Release 就认为「发布完成」，而桌面入口、数据目录、文档
各自会在后面几轮里慢慢暴露问题。

所以把「该检查什么」写成代码，跑一次就知道全链路是否一致，而不是靠记性。

用法：
    python verify.py              # 全量（含联网检查 Release）
    python verify.py --offline    # 跳过需要联网的项

退出码 0 = 全部通过；非 0 = 有项目没过。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import core  # noqa: E402

EXE_NAME = "WindowsUpdatePause.exe"
REPO = "ygy2200/windows-update-pause"
PROXY = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
# 只匹配数字和点，别把徽章尾部的 "-blue" 一起吃进来
BADGE_RE = re.compile(r"badge/version-(\d+(?:\.\d+)*)")

passed = 0
failed = 0
skipped = 0


def ok(name: str, detail: str = "") -> None:
    global passed
    passed += 1
    print(f"  OK    {name}" + (f"  ({detail})" if detail else ""))


def bad(name: str, why: str, how: str = "") -> None:
    global failed
    failed += 1
    print(f"  FAIL  {name}")
    print(f"        原因: {why}")
    if how:
        print(f"        怎么办: {how}")


def skip(name: str, why: str) -> None:
    global skipped
    skipped += 1
    print(f"  --    {name}  ({why})")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def section(title: str) -> None:
    print()
    print(title)


# ---------------- A. 代码与文档 ----------------

def check_code_and_docs() -> None:
    section("A. 代码与文档")

    # 版本号三处一致：代码、README 徽章、git tag
    readme = HERE / "README.md"
    badge = ""
    if readme.exists():
        m = BADGE_RE.search(readme.read_text(encoding="utf-8"))
        badge = m.group(1) if m else ""
    if not badge:
        bad("README 版本徽章", "没找到 badge/version-X.Y.Z", "补上徽章或检查格式")
    elif badge != core.APP_VERSION:
        bad("README 版本徽章", f"徽章 {badge} ≠ 程序 {core.APP_VERSION}",
            f"把徽章改成 {core.APP_VERSION}")
    else:
        ok("README 版本徽章与程序一致", badge)

    try:
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(HERE),
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=20)
        lines = [l for l in dirty.stdout.splitlines() if l.strip()]
        if lines:
            bad("git 工作区干净", f"{len(lines)} 个未提交改动",
                "提交后再发版，或确认这些改动是有意保留的")
        else:
            ok("git 工作区干净")
    except Exception as e:
        skip("git 工作区干净", f"git 不可用（{e}）")

    try:
        tag = subprocess.run(["git", "describe", "--tags", "--abbrev=0"], cwd=str(HERE),
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=20)
        t = tag.stdout.strip()
        if not t:
            skip("git tag 与版本对应", "还没有 tag")
        elif t.lstrip("v") != core.APP_VERSION:
            bad("git tag 与版本对应", f"最新 tag {t} ≠ 程序 {core.APP_VERSION}",
                f"打 tag v{core.APP_VERSION}")
        else:
            ok("git tag 与版本对应", t)
    except Exception as e:
        skip("git tag 与版本对应", f"git 不可用（{e}）")


# ---------------- B. 打包产物 ----------------

def check_build() -> None:
    section("B. 打包产物")

    exe = HERE / EXE_NAME
    if not exe.exists():
        bad("打包产物存在", f"{exe} 不存在", "先打包")
        return
    ok("打包产物存在", f"{exe.stat().st_size} 字节")

    # 让 exe 自己报版本——这是打包产物唯一可信的自证方式
    try:
        r = subprocess.run([str(exe), "--version"], capture_output=True, timeout=90)
        text = r.stdout.decode("utf-8", errors="replace")
        if core.APP_VERSION in text:
            ok("exe 自证版本", core.APP_VERSION)
        else:
            bad("exe 自证版本", f"输出里没有 {core.APP_VERSION}",
                "这个 exe 是旧构建，重新打包")
    except Exception as e:
        bad("exe 自证版本", f"运行失败：{e}", "重新打包后再试")


# ---------------- C. 桌面入口 ----------------

def check_desktop() -> None:
    section("C. 桌面入口")

    src = HERE / EXE_NAME
    desk = Path.home() / "Desktop"
    dst = desk / EXE_NAME

    if not dst.exists():
        skip("桌面有可双击的程序", "桌面上没有该 exe（可以不部署）")
        return

    ok("桌面有可双击的程序", f"{dst.stat().st_size} 字节")

    if src.exists():
        if src.stat().st_size != dst.stat().st_size:
            bad("桌面那份是最新的", f"大小 {dst.stat().st_size} ≠ 源 {src.stat().st_size}",
                "python deploy.py \"%USERPROFILE%\\Desktop\"")
        elif sha256(src) != sha256(dst):
            bad("桌面那份是最新的", "大小相同但内容不同",
                "python deploy.py \"%USERPROFILE%\\Desktop\"")
        else:
            ok("桌面那份是最新的", "大小与哈希均一致")

    # 遗留的 .bat 跳板：能留但会让人困惑
    stale = [p.name for p in desk.glob("*.bat")
             if "更新" in p.name or "暂停" in p.name]
    if stale:
        skip("桌面没有遗留的 .bat 跳板", "还有：" + "、".join(stale))


# ---------------- D. 数据目录 ----------------

def check_data_dir() -> None:
    section("D. 数据目录")

    data = Path(os.environ.get("LOCALAPPDATA", "")) / "WindowsUpdatePauseTool"
    if not data.exists():
        bad("数据目录存在", f"{data} 不存在", "运行一次程序即可创建")
        return
    ok("数据目录存在", str(data))

    try:
        probe = data / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        ok("数据目录可写")
    except OSError as e:
        bad("数据目录可写", str(e), "换个账户或检查磁盘权限")
        return

    # 交付时那份原始快照：丢了就没法知道系统最初什么状态
    bm = core.BackupManager(data)
    items = bm.list_backups()
    if not items:
        skip("交付时的原始快照还在", "备份目录是空的")
        return
    good = [i for i in items if i.get("reg_ok")]
    if good:
        ok("备份列表可用", f"{len(items)} 份，其中 {len(good)} 份完整")
    else:
        bad("备份列表可用", "没有任何一份备份是完整的",
            "检查 backup 目录里的 .reg 是否与 snapshot 同名同时间戳")


# ---------------- E. 发布（联网） ----------------

def _token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def check_release() -> None:
    section("E. GitHub Release")

    token = _token()
    if not token:
        skip("Release 检查", "拿不到 GitHub 凭据")
        return

    opener = urllib.request.build_opener(urllib.request.ProxyHandler(PROXY))
    H = {"Authorization": "Bearer " + token,
         "Accept": "application/vnd.github+json"}

    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/releases/tags/v{core.APP_VERSION}",
            headers=H)
        with opener.open(req, timeout=45) as r:
            rel = json.load(r)
    except Exception as e:
        bad("v%s 的 Release 存在" % core.APP_VERSION, str(e), "先发版")
        return

    ok("v%s 的 Release 存在" % core.APP_VERSION, rel.get("html_url", ""))

    assets = [a for a in rel.get("assets", []) if a["name"] == EXE_NAME]
    if not assets:
        bad("Release 附带 exe", "附件里没有 " + EXE_NAME, "重新上传附件")
        return

    local = HERE / EXE_NAME
    if not local.exists():
        skip("附件与本地一致", "本地没有构建产物")
        return

    remote = assets[0]["size"]
    if remote != local.stat().st_size:
        bad("附件与本地一致", f"附件 {remote} 字节 ≠ 本地 {local.stat().st_size} 字节",
            "重新上传附件")
    else:
        ok("附件与本地一致", f"{remote} 字节")


# ---------------- main ----------------

def main() -> int:
    print("=" * 62)
    print(f"{core.APP_NAME} v{core.APP_VERSION}  发布自检")
    print("=" * 62)

    offline = "--offline" in sys.argv

    check_code_and_docs()
    check_build()
    check_desktop()
    check_data_dir()
    if offline:
        skip("E. GitHub Release", "--offline")
    else:
        check_release()

    print()
    print("=" * 62)
    print(f"通过 {passed}　失败 {failed}　跳过 {skipped}")
    if failed:
        print()
        print("有项目没过。上面对每一项都写了「怎么办」，按那个做即可。")
    print("=" * 62)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
