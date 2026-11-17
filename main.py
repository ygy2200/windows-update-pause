# -*- coding: utf-8 -*-
"""
Windows 更新暂停工具 v2 - 图形界面

设计要点：
- 只读模式下也能完整自检，写操作才需要管理员
- 顶部权限条：非管理员时给一个「以管理员身份重启」按钮，避免每次操作弹 UAC
- 自检按「暂停有效性 / 系统环境 / 基础」分组，核心项红=暂停不生效，环境项黄=不影响暂停
- 所有操作先备份再写，失败可见可回滚
"""

from __future__ import annotations

import ctypes
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QListWidget, QListWidgetItem, QMessageBox, QSizePolicy, QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, FluentIcon, InfoBar,
    InfoBarPosition, PrimaryPushButton, PushButton, ScrollArea,
    SubtitleLabel, TitleLabel, TransparentToolButton, setTheme, setThemeColor, Theme,
)

import core

def _resolve_app_dir() -> Path:
    """定位「数据目录」。打包成单文件 exe 后 __file__ 指向临时解压目录
    （sys._MEIPASS），程序退出即被清理——备份绝不能写在那里。
    因此打包模式必须用 exe 自身所在目录。

    注意：这里刻意不写「临时文件再删掉」的探针。本机的 sitecustomize 会把
    Path.unlink 劫持到回收站工具，那工具输出 UTF-8 却被按 GBK 解码，
    会抛 UnicodeDecodeError。可写性改由 BackupManager 用 mkdir 判断。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _resolve_app_dir()

STATUS_COLOR_LIGHT = {
    "pass": "#0F7B0F",
    "warn": "#9D5D00",
    "fail": "#C42B1C",
    "skip": "#6E6E6E",
}
STATUS_COLOR_DARK = {
    "pass": "#6CCB5F",
    "warn": "#F0C040",
    "fail": "#FF99A4",
    "skip": "#A0A0A0",
}
STATUS_TEXT = {"pass": "通过", "warn": "警告", "fail": "失败", "skip": "跳过"}
# 用单字符标记，宽度可控，不会因中文宽度被挤成竖排
STATUS_GLYPH = {"pass": "✓", "warn": "!", "fail": "×", "skip": "–"}

EDITION_CN = {
    "Professional": "专业版",
    "ProfessionalWorkstation": "专业工作站版",
    "ProfessionalEducation": "专业教育版",
    "Core": "家庭版",
    "CoreSingleLanguage": "家庭单语言版",
    "CoreCountrySpecific": "家庭中文版",
    "Enterprise": "企业版",
    "EnterpriseS": "企业版 LTSC",
    "Education": "教育版",
    "IoTEnterprise": "IoT 企业版",
}


def is_dark() -> bool:
    app = QApplication.instance()
    if app is None:
        return False
    return app.palette().window().color().lightness() < 128


def status_color(status: str) -> str:
    return (STATUS_COLOR_DARK if is_dark() else STATUS_COLOR_LIGHT).get(status, "#888888")


LEVEL_COLOR_LIGHT = {
    "full": "#0F7B0F", "inferred": "#9D5D00",
    "unknown": "#C42B1C", "legacy": "#5D5D5D",
}
LEVEL_COLOR_DARK = {
    "full": "#6CCB5F", "inferred": "#F0C040",
    "unknown": "#FF99A4", "legacy": "#A0A0A0",
}


def level_color(level: str) -> str:
    """支持等级徽章的颜色，必须跟主题走——写死的深绿在深色背景上看不清。"""
    table = LEVEL_COLOR_DARK if is_dark() else LEVEL_COLOR_LIGHT
    return table.get(level, table["unknown"])


def make_app_icon() -> QIcon:
    """不用外部资源，直接画一个图标，打包时省事。"""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#0F6CBD"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setPen(QColor("#FFFFFF"))
    f = QFont("Segoe UI", 26, QFont.Bold)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "||")
    p.end()
    return QIcon(pm)


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


def badge(text: str, color: str) -> QLabel:
    lb = QLabel(text)
    lb.setStyleSheet(
        f"QLabel {{ color: {color}; border: 1px solid {color};"
        f" border-radius: 10px; padding: 2px 10px; font-size: 12px; }}"
    )
    lb.setAlignment(Qt.AlignCenter)
    lb.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    return lb


class CheckRow(QWidget):
    def __init__(self, check: core.CheckResult, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 3, 0, 3)
        lay.setSpacing(8)

        dot = QLabel("●")
        dot.setStyleSheet(f"color: {status_color(check.status)}; font-size: 13px;")
        dot.setFixedWidth(14)
        lay.addWidget(dot, 0, Qt.AlignTop)

        name = QLabel(check.name)
        name.setFixedWidth(128)
        name.setStyleSheet("font-size: 13px;")
        lay.addWidget(name, 0, Qt.AlignTop)

        detail = QLabel(check.detail)
        detail.setWordWrap(True)
        detail.setStyleSheet("font-size: 12px;")
        lay.addWidget(detail, 1)

        tag = QLabel(STATUS_GLYPH.get(check.status, ""))
        tag.setFixedWidth(16)
        tag.setToolTip(STATUS_TEXT.get(check.status, ""))
        tag.setStyleSheet(
            f"color: {status_color(check.status)}; font-size: 13px; font-weight: 600;")
        tag.setAlignment(Qt.AlignCenter)
        lay.addWidget(tag, 0, Qt.AlignTop)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.info: core.SystemInfo = core.read_system_info()
        self.state: core.PauseState = core.read_pause_state()
        self.report: core.HealthReport = core.run_health_check()
        self.backups = core.BackupManager(APP_DIR)
        self._busy = False

        self._build_shell()
        self.refresh_all(first=True)

    # ---------------- 骨架 ----------------

    def _build_shell(self) -> None:
        self.setWindowTitle(f"{core.APP_NAME} v{core.APP_VERSION}")
        self.setWindowIcon(make_app_icon())
        self.resize(980, 930)
        self.setMinimumSize(860, 680)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 14)
        outer.setSpacing(12)

        # 标题行
        head = QHBoxLayout()
        head.setSpacing(10)
        title = TitleLabel(core.APP_NAME)
        head.addWidget(title)
        head.addStretch(1)

        self.ver_label = CaptionLabel(f"v{core.APP_VERSION}")
        head.addWidget(self.ver_label, 0, Qt.AlignBottom)
        outer.addLayout(head)

        # 权限条
        self.perm_bar = QFrame()
        self.perm_bar.setStyleSheet(
            "QFrame { background: rgba(157,93,0,0.12); border: 1px solid rgba(157,93,0,0.45);"
            " border-radius: 8px; }")
        pb = QHBoxLayout(self.perm_bar)
        pb.setContentsMargins(12, 8, 10, 8)
        pb.setSpacing(10)
        self.perm_text = BodyLabel("")
        pb.addWidget(self.perm_text, 1)
        self.btn_elevate = PushButton("以管理员身份重启")
        self.btn_elevate.clicked.connect(self.on_elevate)
        pb.addWidget(self.btn_elevate, 0)
        outer.addWidget(self.perm_bar)

        # 系统信息卡
        self.sys_card = CardWidget()
        self.sys_card.setBorderRadius(10)
        self.sys_layout = QVBoxLayout(self.sys_card)
        self.sys_layout.setContentsMargins(18, 14, 18, 14)
        self.sys_layout.setSpacing(6)
        outer.addWidget(self.sys_card)

        # 暂停状态卡
        self.pause_card = CardWidget()
        self.pause_card.setBorderRadius(10)
        self.pause_layout = QVBoxLayout(self.pause_card)
        self.pause_layout.setContentsMargins(18, 14, 18, 16)
        self.pause_layout.setSpacing(10)
        outer.addWidget(self.pause_card)

        # 自检卡
        self.check_card = CardWidget()
        self.check_card.setBorderRadius(10)
        self.check_outer = QVBoxLayout(self.check_card)
        self.check_outer.setContentsMargins(18, 14, 18, 12)
        self.check_outer.setSpacing(8)

        self.check_head = QHBoxLayout()
        self.check_head.setSpacing(8)
        self.check_title = SubtitleLabel("自检")
        self.check_head.addWidget(self.check_title)
        self.check_stat = CaptionLabel("")
        self.check_head.addWidget(self.check_stat, 0, Qt.AlignBottom)
        self.check_head.addStretch(1)

        self.btn_recheck = PushButton("重新自检")
        self.btn_recheck.setIcon(FluentIcon.SYNC)
        self.btn_recheck.clicked.connect(self.on_recheck)
        self.check_head.addWidget(self.btn_recheck)

        self.btn_repair = PushButton("一键修复")
        self.btn_repair.setIcon(FluentIcon.BROOM)
        self.btn_repair.clicked.connect(self.on_repair)
        self.check_head.addWidget(self.btn_repair)
        self.check_outer.addLayout(self.check_head)

        self.check_scroll = ScrollArea()
        self.check_scroll.setWidgetResizable(True)
        self.check_scroll.setFrameShape(QFrame.NoFrame)
        self.check_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.check_scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self.check_body = QWidget()
        self.check_body.setStyleSheet("background: transparent;")
        self.check_body_layout = QVBoxLayout(self.check_body)
        self.check_body_layout.setContentsMargins(0, 2, 6, 2)
        self.check_body_layout.setSpacing(2)
        self.check_scroll.setWidget(self.check_body)
        self.check_outer.addWidget(self.check_scroll, 1)
        outer.addWidget(self.check_card, 1)

        # 底栏
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.foot = CaptionLabel("")
        foot.addWidget(self.foot, 1)
        self.btn_open_dir = TransparentToolButton(FluentIcon.FOLDER)
        self.btn_open_dir.setToolTip("打开工具目录（备份与日志）")
        self.btn_open_dir.clicked.connect(self.on_open_dir)
        foot.addWidget(self.btn_open_dir)
        outer.addLayout(foot)

    # ---------------- 渲染 ----------------

    def refresh_all(self, first: bool = False) -> None:
        self.info = core.read_system_info()
        self.state = core.read_pause_state()
        self.report = core.run_health_check()
        self._render_perm()
        self._render_system()
        self._render_pause()
        self._render_checks()

        self._render_footer()
        if not first:
            self.check_card.update()

    def _render_footer(self) -> None:
        """底部信息条。完整路径太长，在高 DPI 或长目录名下会顶到窗口右缘，
        所以只显示末两级，完整路径与日志位置放 tooltip。"""
        d = self.backups.dir
        short = f"{d.parent.name}\\{d.name}"
        self.foot.setText(f"备份 → {short}　·　自检 {self.report.total} 项")

        tip = f"备份完整路径：{d}"
        audit = core.get_audit_path()
        if audit is not None:
            tip += f"\n操作日志：{audit}"
        self.foot.setToolTip(tip)

    def _render_perm(self) -> None:
        if core.has_admin():
            self.perm_bar.setVisible(False)
            return
        self.perm_bar.setVisible(True)
        self.perm_text.setText(
            "当前为只读模式：可以查看状态和跑自检，修改需要管理员权限。"
        )

    def _render_system(self) -> None:
        clear_layout(self.sys_layout)
        info = self.info

        top = QHBoxLayout()
        top.setSpacing(10)
        name = SubtitleLabel(info.os_label)
        top.addWidget(name)
        top.addWidget(badge(info.level_label, level_color(info.matrix_level)), 0, Qt.AlignVCenter)
        top.addStretch(1)
        self.sys_layout.addLayout(top)

        meta = BodyLabel(
            f"build {info.full_build}"
            + (f" · {EDITION_CN.get(info.edition, info.edition or '')}" if info.edition else "")
        )
        self.sys_layout.addWidget(meta)

        note = CaptionLabel(
            info.matrix_note
            or ("该版本引擎要求完整键组（上限 + 三对起止时间 + 状态位），"
                "只改上限会被钳制回 35 天"
                if info.needs_full_keyset else "该版本引擎对暂停时长的限制较宽松")
        )
        note.setWordWrap(True)
        self.sys_layout.addWidget(note)

    def _render_pause(self) -> None:
        clear_layout(self.pause_layout)
        st = self.state

        head = QHBoxLayout()
        head.setSpacing(12)

        if st.is_paused:
            txt, col = "已暂停", status_color("pass")
        elif st.expiry_utc is not None:
            txt, col = "已到期", status_color("warn")
        else:
            txt, col = "未暂停", status_color("skip")

        big = QLabel(txt)
        big.setStyleSheet(f"font-size: 24px; font-weight: 600; color: {col};")
        head.addWidget(big, 0, Qt.AlignVCenter)

        if st.expiry_utc is not None:
            sub = BodyLabel(
                f"到期 {core.fmt_local(st.expiry_utc)}　·　剩余 {st.remaining_days} 天"
                + (f"（约 {st.remaining_years} 年）" if st.remaining_days > 400 else "")
            )
            head.addWidget(sub, 0, Qt.AlignBottom)
        head.addStretch(1)
        self.pause_layout.addLayout(head)

        cap_txt = (f"暂停上限 {st.cap_days} 天" if st.cap_days is not None
                   else "暂停上限 未设置（系统默认约 35 天）")
        detail = CaptionLabel(
            f"{cap_txt}　·　质量更新状态位 {st.quality_status}　·　功能更新状态位 {st.feature_status}"
        )
        self.pause_layout.addWidget(detail)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.btn_pause40 = PrimaryPushButton("暂停 40 年")
        self.btn_pause40.setIcon(FluentIcon.PAUSE)
        self.btn_pause40.clicked.connect(lambda: self.on_pause(40))
        btns.addWidget(self.btn_pause40)

        self.btn_custom = PushButton("自定义年数")
        self.btn_custom.clicked.connect(self.on_pause_custom)
        btns.addWidget(self.btn_custom)

        self.btn_resume = PushButton("恢复更新")
        self.btn_resume.setIcon(FluentIcon.PLAY)
        self.btn_resume.clicked.connect(self.on_resume)
        btns.addWidget(self.btn_resume)

        btns.addStretch(1)

        self.btn_backups = PushButton("备份 / 回滚")
        self.btn_backups.setIcon(FluentIcon.HISTORY)
        self.btn_backups.clicked.connect(self.on_backups)
        btns.addWidget(self.btn_backups)

        self.pause_layout.addLayout(btns)

        writable = core.has_admin()
        for b in (self.btn_pause40, self.btn_custom, self.btn_resume):
            b.setEnabled(writable)

    def _render_checks(self) -> None:
        clear_layout(self.check_body_layout)
        rep = self.report

        self.check_stat.setText(
            f"{rep.total} 项　·　{rep.passed} 通过"
            + (f"　·　{rep.warned} 警告" if rep.warned else "")
            + (f"　·　{rep.failed} 失败" if rep.failed else "")
            + (f"　·　{rep.skipped} 跳过" if rep.skipped else "")
        )
        self.check_stat.setStyleSheet(
            f"color: {status_color('fail') if rep.failed else status_color('pass')};"
        )
        self.btn_repair.setEnabled(core.has_admin() and rep.needs_repair)
        self.btn_repair.setToolTip(
            "" if rep.needs_repair else "没有需要修复的项，或缺少管理员权限"
        )

        order = [("core", "暂停有效性"), ("env", "系统环境"), ("system", "基础能力")]
        for cat, label in order:
            items = [c for c in rep.checks if c.category == cat]
            if not items:
                continue
            head = CaptionLabel(label)
            head.setStyleSheet(
                f"color: {status_color('skip')}; font-weight: 600; margin-top: 6px;"
            )
            self.check_body_layout.addWidget(head)
            for c in items:
                self.check_body_layout.addWidget(CheckRow(c))
        self.check_body_layout.addStretch(1)

    # ---------------- 操作 ----------------

    def _toast(self, level: str, title: str, content: str) -> None:
        fn = {"ok": InfoBar.success, "warn": InfoBar.warning, "err": InfoBar.error}[level]
        fn(title, content, duration=4200, position=InfoBarPosition.TOP_RIGHT,
           parent=self.window())

    def _require_admin(self) -> bool:
        if core.has_admin():
            return True
        self._toast("warn", "只读模式", "修改需要管理员权限，请点顶部「以管理员身份重启」")
        return False

    def _do_write(self, tag: str, fn) -> None:
        """统一写入口：备份 -> 执行 -> 刷新 -> 提示。"""
        if self._busy:
            return
        self._busy = True
        self.setCursor(Qt.WaitCursor)
        try:
            bk = self.backups.snapshot(tag)
            result = fn()
            self.refresh_all()
            text = self._result_text(result, bk)
            core.write_audit(tag, text)
            self._toast("ok", "已完成", text)
        except PermissionError as e:
            core.write_audit(tag + " 失败", "权限不足")
            self._toast("err", "权限不足", str(e))
        except Exception as e:
            core.write_audit(tag + " 失败", str(e))
            detail = traceback.format_exc(limit=2).replace("\n", " ")
            self._toast("err", "操作失败", f"{e}")
            print(detail, file=sys.stderr)
        finally:
            self._busy = False
            self.setCursor(Qt.ArrowCursor)

    @staticmethod
    def _result_text(result, bk) -> str:
        name = Path(bk["txt"]).name
        if isinstance(result, dict) and "end" in result:
            end_local = core.parse_utc(result["end"])
            when = core.fmt_local(end_local) if end_local else result["end"]
            mode = result.get("mode")
            if mode == "preserve":
                return (f"已按原到期时间修复：{when}（共 {result['days']} 天），"
                        f"上限补到 {result['cap']} 天，备份 {name}")
            if mode == "reset40":
                return f"原暂停已失效，重设为 40 年：{when}，备份 {name}"
            return f"暂停至 {when}（{result['days']} 天），备份已存 {name}"
        return f"已完成，备份 {name}"

    def on_pause(self, years: int) -> None:
        if not self._require_admin():
            return
        ans = QMessageBox.question(
            self, "确认",
            f"将把 Windows 更新暂停 {years} 年。\n\n"
            "写入前会自动备份当前注册表值，随时可回滚。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        self._do_write(f"暂停 {years} 年", lambda: core.apply_pause(years))

    def on_pause_custom(self) -> None:
        if not self._require_admin():
            return
        val, ok = QInputDialog.getDouble(
            self, "自定义暂停", "暂停年数（1-100）：", 40.0, 1.0, 100.0, 1)
        if not ok:
            return
        self.on_pause(int(val))

    def on_resume(self) -> None:
        if not self._require_admin():
            return
        ans = QMessageBox.question(
            self, "确认",
            "将清除全部暂停状态，恢复自动更新。\n\n"
            "注意：恢复后系统会开始下载这段时间错过的更新。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        self._do_write("恢复更新", core.clear_pause)

    def on_recheck(self) -> None:
        self.refresh_all()
        rep = self.report
        if rep.failed:
            self._toast("err", "自检有问题", f"{rep.failed} 项失败，详见列表")
        elif rep.warned:
            self._toast("warn", "自检有警告", f"{rep.warned} 项警告，详见列表")
        else:
            self._toast("ok", "自检通过", f"{rep.passed} 项全部正常")

    def on_repair(self) -> None:
        if not self._require_admin():
            return
        ans = QMessageBox.question(
            self, "确认修复",
            "将按自检结果重新写入暂停配置：\n"
            "· 有未过期的暂停时间 → 保持原到期时间重新写入\n"
            "· 没有 → 按 40 年重设\n\n"
            "写入前会自动备份。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ans != QMessageBox.Yes:
            return

        def run():
            r = core.repair()
            return r
        self._do_write("自检修复", run)

    def on_elevate(self) -> None:
        try:
            if getattr(sys, "frozen", False):
                exe, params = sys.executable, ""
            else:
                exe = sys.executable
                params = f'"{APP_DIR / "main.py"}"'
            r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
            if r > 32:
                QApplication.quit()
            else:
                self._toast("err", "提权失败", f"ShellExecute 返回 {r}")
        except Exception as e:
            self._toast("err", "提权失败", str(e))

    def on_backups(self) -> None:
        dlg = BackupDialog(self.backups, self)
        dlg.exec()
        self.refresh_all()

    def on_open_dir(self) -> None:
        try:
            import subprocess
            subprocess.Popen(["explorer.exe", str(APP_DIR)],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            self._toast("err", "打不开目录", str(e))


class BackupDialog(QDialog):
    def __init__(self, bm: core.BackupManager, parent=None):
        super().__init__(parent)
        self.bm = bm
        self.setWindowTitle("备份与回滚")
        self.resize(680, 420)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        lay.addWidget(BodyLabel("每次修改前都会自动备份。选中一份可以回滚到当时的状态。"))

        self.list = QListWidget()
        self.list.setAlternatingRowColors(True)
        lay.addWidget(self.list, 1)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.info = CaptionLabel("")
        btns.addWidget(self.info, 1)

        self.btn_open = PushButton("打开所在目录")
        self.btn_open.clicked.connect(self.open_dir)
        btns.addWidget(self.btn_open)

        self.btn_restore = PrimaryPushButton("回滚到选中备份")
        self.btn_restore.clicked.connect(self.restore)
        btns.addWidget(self.btn_restore)

        self.btn_close = PushButton("关闭")
        self.btn_close.clicked.connect(self.accept)
        btns.addWidget(self.btn_close)
        lay.addLayout(btns)

        self.items = bm.list_backups()
        if not self.items:
            self.info.setText("还没有备份。执行一次修改后会自动生成。")
            self.btn_restore.setEnabled(False)
        else:
            for it in self.items:
                text = f"{it['mtime'].strftime('%Y-%m-%d %H:%M:%S')}    {it['timestamp']}"
                self.list.addItem(QListWidgetItem(text))
            self.list.setCurrentRow(0)
            self.info.setText(f"共 {len(self.items)} 份备份")

    def open_dir(self) -> None:
        try:
            import subprocess
            subprocess.Popen(["explorer.exe", str(self.bm.dir)],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass

    def restore(self) -> None:
        row = self.list.currentRow()
        if row < 0 or row >= len(self.items):
            return
        it = self.items[row]
        if not core.has_admin():
            QMessageBox.warning(self, "需要管理员", "回滚需要管理员权限。")
            return
        ans = QMessageBox.question(
            self, "确认回滚",
            f"将导入 {it['timestamp']} 的备份，覆盖当前的暂停配置。\n\n"
            "回滚前会先备份当前状态，可以撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        try:
            self.bm.snapshot("回滚前")
            ok, msg = self.bm.restore(it)
            if ok:
                QMessageBox.information(self, "完成", "已回滚。\n" + msg)
                self.accept()
            else:
                QMessageBox.warning(self, "部分失败", msg)
        except Exception as e:
            QMessageBox.critical(self, "失败", str(e))


def _install_excepthook() -> None:
    """未捕获异常落盘并弹窗。
    打包成 --windowed 后没有控制台，崩溃只会静默消失，用户完全不知道发生了什么。"""
    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log_path = None
        try:
            log_path = APP_DIR / "logs" / "crash.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n=== {} ===\n{}\n".format(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))
        except Exception:
            log_path = None
        try:
            QMessageBox.critical(
                None, "程序出错",
                "{}\n\n详细信息已写入：\n{}".format(exc, log_path or "（日志写入失败）"))
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook


class _Tee:
    """同时写多个流，任一流失效不影响其余。"""
    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, s):
        for st in self._streams:
            try:
                st.write(s)
                st.flush()
            except Exception:
                pass

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:
                pass


def _cli_output_redirect():
    """命令行模式一律落盘一份。

    不能靠「sys.stdout is None」判断：--windowed 打包后这个值不可靠
    （有时是 None，有时是无效句柄），只判断它会导致双击运行 --selftest 时
    输出彻底丢失、用户什么都看不到。所以改成无条件写文件 + Tee 兼顾控制台。"""
    path = APP_DIR / "logs" / "cli-output.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "w", encoding="utf-8")
    except OSError:
        return None
    sys.stdout = _Tee(sys.__stdout__, f)
    sys.stderr = _Tee(sys.__stderr__, f)
    return path


def _print_status() -> None:
    """纯文本状态，方便脚本调用或重定向。"""
    import core as c
    info = c.read_system_info()
    st = c.read_pause_state()
    rep = c.run_health_check()

    print(f"{c.APP_NAME} v{c.APP_VERSION}")
    print("-" * 52)
    print(f"系统版本 : {info.os_label}  build {info.full_build}")
    print(f"支持状态 : {info.level_label}" + (f"  ({info.matrix_note})" if info.matrix_note else ""))
    if st.is_paused:
        print(f"暂停状态 : 已暂停，到期 {c.fmt_local(st.expiry_utc)}，剩 {st.remaining_days} 天")
    elif st.expiry_utc is not None:
        print(f"暂停状态 : 已过期（{c.fmt_local(st.expiry_utc)}）")
    else:
        print("暂停状态 : 未暂停")
    print(f"暂停上限 : {st.cap_days if st.cap_days is not None else '未设置'}")
    print(f"自检结果 : {rep.total} 项，{rep.passed} 通过 / "
          f"{rep.warned} 警告 / {rep.failed} 失败 / {rep.skipped} 跳过")
    for ck in rep.checks:
        if ck.status in ("fail", "warn"):
            print(f"  [{ck.status.upper():4s}] {ck.name}: {ck.detail}")


def main() -> int:
    args = sys.argv[1:]

    # ---- 命令行模式：不拉起 Qt ----
    cli_verbs = ("--version", "-v", "--selftest", "--status")
    if any(a in cli_verbs for a in args):
        redirected = _cli_output_redirect()
        code = 0
        try:
            if "--version" in args or "-v" in args:
                print(f"{core.APP_NAME} v{core.APP_VERSION}")
            if "--status" in args:
                _print_status()
            if "--selftest" in args:
                code = core._selftest()
        finally:
            # 只有真正没有控制台时才弹窗（双击场景）；控制台/管道运行直接看输出
            if (redirected and sys.__stdout__ is None
                    and os.environ.get("WU_PAUSE_NO_POPUP") != "1"):
                try:
                    sys.stdout.flush()
                    ctypes.windll.user32.MessageBoxW(
                        0,
                        f"自检/查询已完成，退出码 {code}。\n\n结果已写入：\n{redirected}",
                        f"{core.APP_NAME} v{core.APP_VERSION}",
                        0x40,
                    )
                except Exception:
                    pass
        return code

    # ---- GUI 模式 ----
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(core.APP_NAME)
    setTheme(Theme.AUTO)
    setThemeColor("#0F6CBD")
    core.set_audit_log(APP_DIR / "logs" / "operation.log")
    # 放在 MainWindow 之前：构造阶段出错也能被兜住
    _install_excepthook()

    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
