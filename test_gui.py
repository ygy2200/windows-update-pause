# -*- coding: utf-8 -*-
"""
GUI 冒烟测试

用 Qt 的 offscreen 平台跑，不需要真实显示器，所以本地和 CI 都能执行。

覆盖：窗口构造、状态刷新、备份对话框、最小尺寸、浅色/深色主题切换、
     底部信息条、以及线程级异常（后台 subprocess 解码失败这类问题会在这里暴露）。

没装 PySide6 时自动跳过（核心逻辑测试不依赖 GUI，不该因此失败）。

用法：
    python test_gui.py
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import core  # noqa: E402


def main() -> int:
    core._ensure_utf8_stdout()

    print("=" * 58)
    print("GUI 冒烟测试（offscreen）")
    print("=" * 58)

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as e:
        print(f"SKIP：未安装 PySide6（{e}）")
        print("      GUI 测试需要：pip install PySide6 PySide6-Fluent-Widgets")
        return 0

    try:
        import main as gui
    except ImportError as e:
        print(f"SKIP：GUI 依赖不完整（{e}）")
        return 0

    fails = 0
    thread_errors: list[str] = []
    threading.excepthook = lambda a: thread_errors.append(repr(a.exc_value))

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal fails
        tail = f"  ({extra})" if extra else ""
        if cond:
            print(f"  OK    {name}{tail}")
        else:
            fails += 1
            print(f"  FAIL  {name}{tail}")

    app = QApplication.instance() or QApplication([])

    # ---------- 构造 ----------
    try:
        w = gui.MainWindow()
        check("主窗口构造成功", True, f"{w.width()}x{w.height()}")
    except Exception:
        print("  FAIL  主窗口构造抛异常")
        traceback.print_exc()
        return 1

    check("窗口标题含版本号", core.APP_VERSION in w.windowTitle(), w.windowTitle())

    # ---------- 状态刷新 ----------
    try:
        w.refresh_all()
        check("refresh_all 不崩", True)
    except Exception:
        check("refresh_all 不崩", False)
        traceback.print_exc()

    check("自检项数 > 0", w.report.total > 0, f"{w.report.total} 项")
    check("自检项都有名称", all(c.name for c in w.report.checks))
    check("自检项状态合法",
          all(c.status in ("pass", "warn", "fail", "skip") for c in w.report.checks))

    foot = w.foot.text()
    check("底部信息条非空", bool(foot.strip()), foot)
    check("底部提示含完整路径",
          str(w.backups.dir) in w.foot.toolTip(), "tooltip 有路径")

    # ---------- 备份对话框 ----------
    try:
        d = gui.BackupDialog(w.backups, w)
        n = d.list.count()
        check("备份对话框可打开", True, f"{n} 条记录")
        if n == 0:
            check("空列表时回滚按钮禁用", not d.btn_restore.isEnabled())
        d.deleteLater()
    except Exception:
        check("备份对话框可打开", False)
        traceback.print_exc()

    # ---------- 尺寸 ----------
    try:
        w.resize(860, 680)
        app.processEvents()
        check("缩到最小尺寸不崩", True, f"{w.width()}x{w.height()}")
    except Exception:
        check("缩到最小尺寸不崩", False)
        traceback.print_exc()

    # ---------- 主题 ----------
    try:
        from qfluentwidgets import setTheme, Theme
        for t, nm in ((Theme.LIGHT, "浅色"), (Theme.DARK, "深色")):
            setTheme(t)
            app.processEvents()
            w.refresh_all()
            app.processEvents()
        check("浅色/深色切换后刷新正常", True)
    except Exception:
        check("浅色/深色切换后刷新正常", False)
        traceback.print_exc()

    # ---------- 图标 ----------
    try:
        icon = gui.make_app_icon()
        check("应用图标非空", not icon.isNull())
    except Exception:
        check("应用图标非空", False)
        traceback.print_exc()

    # ---------- 线程异常 ----------
    app.processEvents()
    check("无线程级异常", len(thread_errors) == 0,
          "; ".join(thread_errors[:3]) if thread_errors else "clean")

    print()
    print("=" * 58)
    if fails == 0:
        print("GUI 冒烟测试全部通过")
    else:
        print(f"GUI 冒烟测试失败 {fails} 项")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
