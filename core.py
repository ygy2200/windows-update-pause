"""
Windows 更新暂停工具 - 核心逻辑层

设计原则：
1. 不依赖 GUI，可独立测试（python core.py --selftest）
2. 所有注册表读写都有兜底，读不到只降级不崩溃
3. 系统版本用「支持矩阵 + 范围推断」，未知版本标未验证而不是拒绝工作
4. 每次写入前强制备份

关于「系统一更新就废了」的应对：
- 键路径不写死单点，存在性检查会明确报告结构变化
- 时间格式解析支持多种变体
- 健康检查把「什么叫正常」变成可执行断言，漂移立刻可见
- 修复走同一条 apply 路径，历史备份可回滚
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import winreg
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

APP_NAME = "Windows 更新暂停工具"
APP_VERSION = "2.1.3"

# ---------------- 注册表路径 ----------------

UX_PATH = r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
UX_PARENT = r"SOFTWARE\Microsoft\WindowsUpdate\UX"
POL_PATH = r"SOFTWARE\Microsoft\WindowsUpdate\UpdatePolicy\Settings"
POLICY_AU = r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
POLICY_WU = r"SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
CV_PATH = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"

TIME_START_VALUES = (
    "PauseUpdatesStartTime",
    "PauseFeatureUpdatesStartTime",
    "PauseQualityUpdatesStartTime",
)
TIME_END_VALUES = (
    "PauseUpdatesExpiryTime",
    "PauseFeatureUpdatesEndTime",
    "PauseQualityUpdatesEndTime",
)
CAP_VALUE = "FlightSettingsMaxPauseDays"
POL_STATUS_VALUES = ("PausedFeatureStatus", "PausedQualityStatus")
POL_DATE_VALUES = ("PausedFeatureDate", "PausedQualityDate")

# ---------------- 系统支持矩阵 ----------------

# level: full=已验证可用 / inferred=同代推断 / unknown=未验证 / legacy=旧版兼容模式
SUPPORT_MATRIX: dict[int, dict[str, str]] = {
    19044: {"name": "Windows 10 21H2 / LTSC 2021", "level": "legacy",
            "note": "旧版引擎，不钳制超长暂停"},
    19045: {"name": "Windows 10 22H2", "level": "legacy",
            "note": "旧版引擎，免费支持已结束"},
    22621: {"name": "Windows 11 22H2", "level": "legacy", "note": "已停止支持"},
    22631: {"name": "Windows 11 23H2", "level": "full",
            "note": "家庭/专业版已停更，企业版至 2026-11"},
    26100: {"name": "Windows 11 24H2", "level": "full",
            "note": "需完整键组（上限+三对时间+状态位）"},
    26200: {"name": "Windows 11 25H2", "level": "full",
            "note": "需完整键组"},
    26300: {"name": "Windows 11 26H2", "level": "full",
            "note": "本机实测验证通过"},
    28000: {"name": "Windows 11 26H1", "level": "full",
            "note": "新设备专用分支"},
}

LEVEL_LABEL = {
    "full": "已验证支持",
    "inferred": "同代推断支持",
    "unknown": "未验证",
    "legacy": "旧版兼容模式",
}

LEVEL_COLOR = {
    "full": "#0F7B0F",
    "inferred": "#9D5D00",
    "unknown": "#C42B1C",
    "legacy": "#5D5D5D",
}

# 检查项归类：core 决定「暂停到底生效没有」，env 影响更新但不决定暂停，system 是基础能力
CHECK_CATEGORY = {
    "os": "system",
    "ux_key": "system",
    "pol_key": "system",
    "admin": "system",
    "values": "core",
    "time_fmt": "core",
    "consist": "core",
    "cap": "core",
    "status": "core",
    "expiry": "core",
    "policy": "env",
    "medic": "env",
    "task": "env",
    "conflict": "env",
}


# ---------------- 数据结构 ----------------

@dataclass
class SystemInfo:
    product_name: str = ""
    display_version: str = ""
    build: int = 0
    ubr: int = 0
    edition: str = ""
    arch: str = ""
    is_win11: bool = False
    matrix_level: str = "unknown"
    matrix_name: str = ""
    matrix_note: str = ""

    @property
    def full_build(self) -> str:
        return f"{self.build}.{self.ubr}" if self.ubr else str(self.build)

    @property
    def os_label(self) -> str:
        if self.matrix_name:
            return self.matrix_name
        return self.product_name or "未知系统"

    @property
    def level_label(self) -> str:
        return LEVEL_LABEL.get(self.matrix_level, "未验证")

    @property
    def level_color(self) -> str:
        return LEVEL_COLOR.get(self.matrix_level, "#C42B1C")

    @property
    def needs_full_keyset(self) -> bool:
        """26100 及以上的引擎会钳制超长暂停，必须写完整键组"""
        return self.build >= 26100


@dataclass
class PauseState:
    exists: bool = False
    expiry_utc: Optional[datetime] = None
    start_utc: Optional[datetime] = None
    cap_days: Optional[int] = None
    quality_status: Optional[int] = None
    feature_status: Optional[int] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_paused(self) -> bool:
        if self.expiry_utc is None:
            return False
        return self.expiry_utc > datetime.now(timezone.utc)

    @property
    def remaining_days(self) -> int:
        if self.expiry_utc is None:
            return 0
        delta = self.expiry_utc - datetime.now(timezone.utc)
        return max(0, int(delta.total_seconds() // 86400))

    @property
    def remaining_years(self) -> float:
        return round(self.remaining_days / 365.25, 1)


@dataclass
class CheckResult:
    key: str
    name: str
    status: str          # pass / warn / fail / skip
    detail: str = ""
    fixable: bool = False

    @property
    def category(self) -> str:
        """core=决定暂停是否生效  env=影响更新但不决定暂停  system=基础环境"""
        return CHECK_CATEGORY.get(self.key, "system")

    @property
    def category_label(self) -> str:
        return {"core": "暂停有效性", "env": "系统环境", "system": "基础"}[self.category]


@dataclass
class HealthReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == "pass")

    @property
    def warned(self) -> int:
        return sum(1 for c in self.checks if c.status == "warn")

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.status == "fail")

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.checks if c.status == "skip")

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def healthy(self) -> bool:
        return self.failed == 0

    @property
    def needs_repair(self) -> bool:
        return any(c.fixable and c.status in ("fail", "warn") for c in self.checks)


# ---------------- 注册表根（可重定向，用于故障注入测试） ----------------

_ROOT = getattr(winreg, "HKEY_LOCAL_MACHINE")
_PREFIX = ""


def use_test_root(root: int, prefix: str = "") -> None:
    """把全部注册表访问重定向到一棵测试树。
    用于故障注入：在 HKCU 下伪造「系统更新后暂停被改坏」的各种现场，
    验证自检能不能真的认出来——绝不拿真实 HKLM 做实验。"""
    global _ROOT, _PREFIX
    _ROOT, _PREFIX = root, prefix


def reset_root() -> None:
    global _ROOT, _PREFIX
    _ROOT = getattr(winreg, "HKEY_LOCAL_MACHINE")
    _PREFIX = ""


def _full(path: str) -> str:
    return _PREFIX + path if _PREFIX else path


# ---------------- 基础工具 ----------------

def _ensure_utf8_stdout() -> None:
    """把标准流转成 UTF-8。

    英文版 Windows（以及 CI runner）的控制台代码页是 cp1252，
    直接打印中文会抛 UnicodeEncodeError 并让脚本非零退出。
    本机是 GBK 所以本地永远看不到这个问题——跨机器一跑就暴露。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def has_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _require_write() -> None:
    """所有写操作的统一权限闸门。
    测试模式（_PREFIX 非空，写的是 HKCU 沙箱）下豁免，
    这样写路径才能被端到端验证。集中一处，避免各函数各写一份导致漏改。"""
    if not _PREFIX and not has_admin():
        raise PermissionError("需要管理员权限")


def _read_raw(root: int, path: str, name: str) -> tuple[Any, Optional[int]]:
    """读一个注册表值，返回 (值, 类型)。读不到返回 (None, None)。"""
    path = _full(path)
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as k:
            v, t = winreg.QueryValueEx(k, name)
            return v, t
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, None


def _key_exists(root: int, path: str) -> bool:
    path = _full(path)
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ):
            return True
    except OSError:
        return False


def _write_value(root: int, path: str, name: str, value: Any, vtype: int) -> None:
    path = _full(path)
    with winreg.CreateKeyEx(root, path, 0, winreg.KEY_WRITE) as k:
        winreg.SetValueEx(k, name, 0, vtype, value)


def _delete_value(root: int, path: str, name: str) -> None:
    path = _full(path)
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except FileNotFoundError:
        pass


def parse_utc(text: Any) -> Optional[datetime]:
    """宽松解析注册表里的时间字符串。兼容多种写法，解析不出返回 None。"""
    if text is None:
        return None
    if isinstance(text, datetime):
        return text if text.tzinfo else text.replace(tzinfo=timezone.utc)
    s = str(text).strip()
    if not s:
        return None

    # 标准写法 2026-11-17T11:19:00Z
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z?$", s)
    if m:
        try:
            y, mo, d, h, mi, se = (int(x) for x in m.groups())
            return datetime(y, mo, d, h, mi, se, tzinfo=timezone.utc)
        except ValueError:
            return None

    # 只到分钟
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?:\.\d+)?Z?$", s)
    if m:
        try:
            y, mo, d, h, mi = (int(x) for x in m.groups())
            return datetime(y, mo, d, h, mi, 0, tzinfo=timezone.utc)
        except ValueError:
            return None

    # 兜底：交给 fromisoformat
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fmt_utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_local(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 系统信息 ----------------

def read_system_info() -> SystemInfo:
    info = SystemInfo()

    v, _ = _read_raw(_ROOT, CV_PATH, "ProductName")
    info.product_name = str(v) if v else ""

    v, _ = _read_raw(_ROOT, CV_PATH, "DisplayVersion")
    info.display_version = str(v) if v else ""

    v, _ = _read_raw(_ROOT, CV_PATH, "CurrentBuildNumber")
    try:
        info.build = int(str(v))
    except (TypeError, ValueError):
        info.build = 0

    v, _ = _read_raw(_ROOT, CV_PATH, "UBR")
    try:
        info.ubr = int(v)
    except (TypeError, ValueError):
        info.ubr = 0

    v, _ = _read_raw(_ROOT, CV_PATH, "EditionID")
    info.edition = str(v) if v else ""

    info.arch = "x64" if sys.maxsize > 2**32 else "x86"

    # 注册表 ProductName 在 Win11 上仍写 "Windows 10"，用 build 判定才可靠
    info.is_win11 = info.build >= 22000
    if info.is_win11 and "Windows 10" in info.product_name:
        info.product_name = "Windows 11"

    entry = SUPPORT_MATRIX.get(info.build)
    if entry:
        info.matrix_level = entry["level"]
        info.matrix_name = entry["name"]
        info.matrix_note = entry["note"]
    else:
        info.matrix_level = "unknown"
        info.matrix_name = ""
        # 范围推断：同代 build 只差尾数时给个保守判断
        for known_build, e in sorted(SUPPORT_MATRIX.items()):
            if 0 < info.build - known_build <= 3 and info.build >= 26100:
                info.matrix_level = "inferred"
                info.matrix_name = e["name"] + "（推断）"
                info.matrix_note = f"build {info.build} 不在已知列表，按 {known_build} 同代处理"
                break
        if info.matrix_level == "unknown":
            if info.build and info.build < 22000:
                info.matrix_level = "legacy"
                info.matrix_name = f"Windows 10 build {info.build}"
                info.matrix_note = "旧版引擎，本工具的完整键组仍安全"
            else:
                info.matrix_name = f"未知分支 build {info.build}"
                info.matrix_note = "未在支持矩阵内，键组机制可能不同，建议先跑自检"

    return info


# ---------------- 暂停状态 ----------------

def read_pause_state() -> PauseState:
    st = PauseState()

    st.exists = _key_exists(_ROOT, UX_PATH)

    v, _ = _read_raw(_ROOT, UX_PATH, CAP_VALUE)
    try:
        st.cap_days = int(v) if v is not None else None
    except (TypeError, ValueError):
        st.cap_days = None

    v, _ = _read_raw(_ROOT, UX_PATH, "PauseUpdatesStartTime")
    st.start_utc = parse_utc(v)

    v, _ = _read_raw(_ROOT, UX_PATH, "PauseUpdatesExpiryTime")
    st.expiry_utc = parse_utc(v)

    v, _ = _read_raw(_ROOT, POL_PATH, "PausedQualityStatus")
    st.quality_status = int(v) if isinstance(v, int) else None

    v, _ = _read_raw(_ROOT, POL_PATH, "PausedFeatureStatus")
    st.feature_status = int(v) if isinstance(v, int) else None

    for name in TIME_START_VALUES + TIME_END_VALUES + (CAP_VALUE,):
        val, kind = _read_raw(_ROOT, UX_PATH, name)
        st.raw[name] = val

    return st


# ---------------- 健康检查 ----------------

def run_health_check() -> HealthReport:
    report = HealthReport()
    info = read_system_info()
    st = read_pause_state()

    # 1 系统版本
    if info.matrix_level == "full":
        report.checks.append(CheckResult(
            "os", "系统版本识别", "pass",
            f"{info.os_label} build {info.full_build}，{info.level_label}"))
    elif info.matrix_level == "inferred":
        report.checks.append(CheckResult(
            "os", "系统版本识别", "warn",
            f"{info.os_label} build {info.full_build}，{info.matrix_note}"))
    elif info.matrix_level == "legacy":
        report.checks.append(CheckResult(
            "os", "系统版本识别", "pass",
            f"{info.os_label} build {info.full_build}，旧版引擎不需要全键组"))
    else:
        report.checks.append(CheckResult(
            "os", "系统版本识别", "warn",
            f"{info.matrix_note or 'build 未知'}，机制可能不同"))

    # 2 UX 键可达性
    if st.exists:
        report.checks.append(CheckResult(
            "ux_key", "注册表主键可达", "pass", f"HKLM\\{UX_PATH}"))
    else:
        parent_ok = _key_exists(_ROOT, UX_PARENT)
        detail = (f"HKLM\\{UX_PATH} 不存在"
                  + ("（上级键存在，疑似系统结构调整）" if parent_ok else "（上级键也不存在）"))
        report.checks.append(CheckResult(
            "ux_key", "注册表主键可达", "fail", detail, fixable=False))

    # 3 策略键可达性
    if _key_exists(_ROOT, POL_PATH):
        report.checks.append(CheckResult(
            "pol_key", "暂停状态键可达", "pass", f"HKLM\\{POL_PATH}"))
    else:
        report.checks.append(CheckResult(
            "pol_key", "暂停状态键可达", "fail",
            f"HKLM\\{POL_PATH} 不存在，状态位无法写入", fixable=False))

    # 4 暂停值完整性
    present_start = [n for n in TIME_START_VALUES if _read_raw(
        _ROOT, UX_PATH, n)[0] is not None]
    present_end = [n for n in TIME_END_VALUES if _read_raw(
        _ROOT, UX_PATH, n)[0] is not None]

    if len(present_start) == 3 and len(present_end) == 3:
        report.checks.append(CheckResult(
            "values", "暂停时间值完整", "pass", "3 对起止时间齐全"))
    elif not present_start and not present_end:
        report.checks.append(CheckResult(
            "values", "暂停时间值完整", "warn",
            "未设置暂停（当前不暂停更新）", fixable=True))
    else:
        report.checks.append(CheckResult(
            "values", "暂停时间值完整", "fail",
            f"只写了 {len(present_start)}/3 组起始、{len(present_end)}/3 组到期，"
            "部分写入会导致引擎忽略暂停", fixable=True))

    # 5 时间格式合法性
    bad_time = []
    for n in TIME_START_VALUES + TIME_END_VALUES:
        raw, _ = _read_raw(_ROOT, UX_PATH, n)
        if raw is not None and parse_utc(raw) is None:
            bad_time.append(n)
    if not bad_time:
        report.checks.append(CheckResult(
            "time_fmt", "时间格式合法", "pass", "全部可解析为 UTC 时间"))
    else:
        report.checks.append(CheckResult(
            "time_fmt", "时间格式合法", "fail",
            "无法解析: " + ", ".join(bad_time), fixable=True))

    # 6 三对时间一致性
    starts = {}
    for n in TIME_START_VALUES:
        raw, _ = _read_raw(_ROOT, UX_PATH, n)
        p = parse_utc(raw)
        if p:
            starts[n] = p
    ends = {}
    for n in TIME_END_VALUES:
        raw, _ = _read_raw(_ROOT, UX_PATH, n)
        p = parse_utc(raw)
        if p:
            ends[n] = p

    if len(starts) >= 2:
        if len(set(starts.values())) == 1:
            report.checks.append(CheckResult(
                "consist", "起止时间一致", "pass", "三组起止时间同步"))
        else:
            report.checks.append(CheckResult(
                "consist", "起止时间一致", "warn",
                "三组起始时间不一致，功能更新与质量更新到期时间可能不同", fixable=True))
    elif len(ends) >= 2:
        if len(set(ends.values())) == 1:
            report.checks.append(CheckResult(
                "consist", "起止时间一致", "pass", "到期时间一致"))
        else:
            report.checks.append(CheckResult(
                "consist", "起止时间一致", "warn",
                "三组到期时间不一致", fixable=True))
    else:
        report.checks.append(CheckResult(
            "consist", "起止时间一致", "skip", "无暂停时间可比较"))

    # 7 上限是否够用（这是 24H2+ 被钳制的直接原因）
    if st.expiry_utc and st.is_paused:
        remain = st.remaining_days
        if st.cap_days is None:
            report.checks.append(CheckResult(
                "cap", "暂停上限充足", "fail",
                f"未设置 {CAP_VALUE}，系统默认上限约 35 天，"
                f"当前已暂停 {remain} 天，会被钳制", fixable=True))
        elif st.cap_days < remain:
            report.checks.append(CheckResult(
                "cap", "暂停上限充足", "fail",
                f"上限 {st.cap_days} 天 < 剩余 {remain} 天，暂停会被强制截断", fixable=True))
        else:
            report.checks.append(CheckResult(
                "cap", "暂停上限充足", "pass",
                f"上限 {st.cap_days} 天 ≥ 剩余 {remain} 天"))
    else:
        report.checks.append(CheckResult(
            "cap", "暂停上限充足", "skip", "当前未暂停"))

    # 8 状态位与时间值是否匹配
    has_time = st.expiry_utc is not None
    status_on = (st.quality_status == 1 and st.feature_status == 1)
    status_off = (st.quality_status == 0 and st.feature_status == 0)

    if has_time and status_on:
        report.checks.append(CheckResult(
            "status", "状态位与时间匹配", "pass", "已暂停且状态位=1"))
    elif has_time and not status_on:
        report.checks.append(CheckResult(
            "status", "状态位与时间匹配", "warn",
            f"有暂停时间但状态位={st.quality_status}/{st.feature_status}，"
            "设置界面可能显示为未暂停", fixable=True))
    elif not has_time and status_off:
        report.checks.append(CheckResult(
            "status", "状态位与时间匹配", "pass", "未暂停且状态位=0"))
    elif not has_time and status_on:
        report.checks.append(CheckResult(
            "status", "状态位与时间匹配", "warn",
            "状态位=1 但没有暂停时间，界面可能显示已暂停但实际不生效", fixable=True))
    else:
        report.checks.append(CheckResult(
            "status", "状态位与时间匹配", "skip", "状态位缺失"))

    # 9 是否已过期
    if st.expiry_utc is None:
        report.checks.append(CheckResult(
            "expiry", "暂停未过期", "skip", "无到期时间"))
    elif st.is_paused:
        report.checks.append(CheckResult(
            "expiry", "暂停未过期", "pass",
            f"到期 {fmt_local(st.expiry_utc)}，剩余 {st.remaining_days} 天"))
    else:
        report.checks.append(CheckResult(
            "expiry", "暂停未过期", "warn",
            f"已于 {fmt_local(st.expiry_utc)} 到期，更新已恢复", fixable=True))

    # 10 组策略是否接管了更新
    no_auto, _ = _read_raw(_ROOT, POLICY_AU, "NoAutoUpdate")
    if isinstance(no_auto, int) and no_auto == 1:
        report.checks.append(CheckResult(
            "policy", "无组策略冲突", "warn",
            "NoAutoUpdate=1（更新已被策略层面禁用），"
            "与本工具的暂停机制重复，两者会互相干扰", fixable=False))
    else:
        report.checks.append(CheckResult(
            "policy", "无组策略冲突", "pass", "没有 NoAutoUpdate 策略接管"))

    # 11 WaaSMedicSvc 风险
    medic = _service_state("WaaSMedicSvc")
    if medic == "running":
        report.checks.append(CheckResult(
            "medic", "更新医生服务风险", "warn",
            "WaaSMedicSvc 运行中，可能周期性「修复」超长暂停设置。"
            "发现暂停自己失效时先看这一项", fixable=False))
    elif medic == "stopped":
        report.checks.append(CheckResult(
            "medic", "更新医生服务风险", "pass", "WaaSMedicSvc 未运行"))
    else:
        report.checks.append(CheckResult(
            "medic", "更新医生服务风险", "skip", "无法读取服务状态"))

    # 12 自动扫描计划任务（缺失只影响自动触发，不影响暂停本身是否生效）
    task_state = _task_state(r"Microsoft\Windows\UpdateOrchestrator\Schedule Scan")
    if task_state == "exists":
        report.checks.append(CheckResult(
            "task", "自动扫描任务", "pass", "UpdateOrchestrator\\Schedule Scan 正常"))
    elif task_state == "disabled":
        report.checks.append(CheckResult(
            "task", "自动扫描任务", "warn",
            "Schedule Scan 被禁用，自动扫描触发不了（手动检查更新仍可用）"))
    elif task_state == "missing":
        report.checks.append(CheckResult(
            "task", "自动扫描任务", "warn",
            "Schedule Scan 不存在，自动扫描触发不了（手动检查更新仍可用）"))
    else:
        report.checks.append(CheckResult(
            "task", "自动扫描任务", "skip",
            "需要管理员权限确认（Tasks 目录普通用户不可读）"))

    # 13 第三方屏蔽工具冲突
    conflicts = _detect_conflicts()
    if conflicts:
        report.checks.append(CheckResult(
            "conflict", "无第三方工具冲突", "warn",
            "检测到: " + "、".join(conflicts), fixable=False))
    else:
        report.checks.append(CheckResult(
            "conflict", "无第三方工具冲突", "pass", "未发现其他更新屏蔽工具"))

    # 13 写权限
    if has_admin():
        report.checks.append(CheckResult(
            "admin", "写入权限", "pass", "当前以管理员身份运行"))
    else:
        report.checks.append(CheckResult(
            "admin", "写入权限", "warn", "非管理员，只能查看不能修改"))

    return report


def _service_state(name: str) -> str:
    try:
        # 不收 text，拿 bytes 自己解码：subprocess 的文本解码线程在部分
        # 控制台代码页下会抛 UnicodeDecodeError，绕开这条路径最稳
        out = subprocess.run(
            ["sc.exe", "query", name],
            capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = (out.stdout or b"").decode("mbcs", errors="ignore").upper()
        if "RUNNING" in text and "STOPPED" not in text:
            return "running"
        if "STOPPED" in text:
            return "stopped"
        return "unknown"
    except Exception:
        return "unknown"


def _detect_conflicts() -> list[str]:
    """找可能抢方向盘的东西：第三方屏蔽工具的常见痕迹。"""
    found: list[str] = []

    # 常见第三方工具的服务名
    for svc in ("WubService", "WindowsUpdateBlocker", "StopUpdates10", "Wu10Man"):
        if _service_state(svc) in ("running", "stopped"):
            found.append(svc)

    # hosts 屏蔽微软更新域名
    try:
        hosts = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/drivers/etc/hosts"
        if hosts.exists():
            for line in hosts.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                low = s.lower()
                if "microsoft.com" in low or "windowsupdate" in low:
                    found.append("hosts 中存在微软更新域名条目")
                    break
    except Exception:
        pass

    # 组策略里的 DisableWindowsUpdateAccess
    v, _ = _read_raw(_ROOT, POLICY_WU, "DisableWindowsUpdateAccess")
    if isinstance(v, int) and v == 1:
        found.append("DisableWindowsUpdateAccess=1")

    return found


def _task_state(rel_path: str) -> str:
    """检查计划任务状态：exists / missing / disabled / unknown。
    直接读 System32\\Tasks 下的任务 XML，不受系统语言影响。
    注意：该目录普通用户不可读，权限不足时必须返回 unknown，不能谎报 missing。"""
    root = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "Tasks"
    f = root / rel_path

    try:
        f.stat()
    except PermissionError:
        return "unknown"
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unknown"

    try:
        xml = f.read_text(encoding="utf-16", errors="ignore")
    except PermissionError:
        return "unknown"
    except Exception:
        return "unknown"

    if not xml.strip():
        return "unknown"
    if re.search(r"<enabled>\s*false\s*</enabled>", xml, re.IGNORECASE):
        return "disabled"
    return "exists"


# ---------------- 操作审计日志 ----------------

_audit_path: Optional[Path] = None


def set_audit_log(path: Path) -> None:
    """指定操作日志位置。目录建不出来（只读盘）就静默禁用，不影响主功能。"""
    global _audit_path
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        _audit_path = p
    except OSError:
        _audit_path = None


def get_audit_path() -> Optional[Path]:
    return _audit_path


def write_audit(action: str, detail: str = "") -> None:
    """追加一行操作流水。失败不抛，绝不因为写日志影响正事。"""
    if _audit_path is None:
        return
    try:
        line = "{} | {} | {}\r\n".format(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, detail)
        with open(_audit_path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


# ---------------- 备份 ----------------

class BackupManager:
    def __init__(self, base_dir: Path):
        # 用 mkdir 判断可写性，而不是「写个文件再删」——
        # 后者的 unlink 在部分环境会被安全软件钩住并引发无关异常
        target = Path(base_dir) / "backup"
        try:
            target.mkdir(parents=True, exist_ok=True)
            self.dir = target
        except OSError:
            fallback = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                        / "WindowsUpdatePauseTool" / "backup")
            fallback.mkdir(parents=True, exist_ok=True)
            self.dir = fallback

    def snapshot(self, tag: str) -> dict[str, Any]:
        # 同一秒内多次操作必须各自留档，否则后一次会覆盖前一个回滚点
        base_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        ts = base_ts
        seq = 1
        while (self.dir / f"snapshot-{ts}.txt").exists():
            ts = f"{base_ts}-{seq}"
            seq += 1

        reg_ux = self.dir / f"ux-settings-{ts}.reg"
        reg_pol = self.dir / f"updatepolicy-settings-{ts}.reg"
        txt = self.dir / f"snapshot-{ts}.txt"

        self._export(f"HKLM\\{UX_PATH}", reg_ux)
        self._export(f"HKLM\\{POL_PATH}", reg_pol)

        lines: list[str] = []
        lines.append("=" * 58)
        lines.append(f"{APP_NAME} v{APP_VERSION} - 改动前快照")
        lines.append("=" * 58)
        lines.append(f"时间   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"操作   : {tag}")
        lines.append(f"计算机 : {os.environ.get('COMPUTERNAME', '')}")
        lines.append("")

        for path in (UX_PATH, POL_PATH):
            lines.append(f"[HKLM\\{path}]")
            try:
                with winreg.OpenKey(_ROOT, _full(path), 0, winreg.KEY_READ) as k:
                    i = 0
                    while True:
                        try:
                            name, val, vtype = winreg.EnumValue(k, i)
                        except OSError:
                            break
                        kind = {1: "String", 4: "DWord", 7: "MultiString",
                                3: "Binary", 2: "ExpandString"}.get(vtype, str(vtype))
                        lines.append(f"  {name:<32} = {val}   ({kind})")
                        i += 1
            except OSError:
                lines.append("  <键不存在>")
            lines.append("")

        lines.append("还原办法：")
        lines.append(f"  1) 双击导入 {reg_ux.name}")
        lines.append(f"  2) 双击导入 {reg_pol.name}")
        lines.append("  3) 在本软件里点「恢复更新」")
        lines.append("")
        lines.append("两个 .reg 只含上述两个键，不含其他系统设置。")

        txt.write_text("\r\n".join(lines), encoding="utf-8")

        return {"txt": str(txt), "reg_ux": str(reg_ux),
                "reg_pol": str(reg_pol), "timestamp": ts}

    def _export(self, key: str, out: Path) -> bool:
        # 测试模式下不碰真实 HKLM，直接跳过导出
        if _PREFIX:
            return False
        try:
            r = subprocess.run(
                ["reg.exe", "export", key, str(out), "/y"],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return r.returncode == 0
        except Exception:
            return False

    def list_backups(self) -> list[dict[str, Any]]:
        items = []
        for f in self.dir.glob("snapshot-*.txt"):
            ts = f.stem.replace("snapshot-", "")
            items.append({
                "timestamp": ts,
                "txt": str(f),
                "reg_ux": str(self.dir / f"ux-settings-{ts}.reg"),
                "reg_pol": str(self.dir / f"updatepolicy-settings-{ts}.reg"),
                "mtime": datetime.fromtimestamp(f.stat().st_mtime),
            })
        # 按修改时间倒序。文件名带 -1/-2 序号时字典序会把新的排到旧的后面
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items

    def restore(self, item: dict[str, Any]) -> tuple[bool, str]:
        # 测试模式不执行真实导入：reg import 会写 HKLM，沙箱里必须挡住
        if _PREFIX:
            return True, "测试模式：已跳过实际导入"

        ok_all = True
        msgs = []
        for key in ("reg_ux", "reg_pol"):
            p = Path(item.get(key, ""))
            if not p.exists():
                msgs.append(f"{p.name} 不存在，跳过")
                continue
            try:
                r = subprocess.run(
                    ["reg.exe", "import", str(p)],
                    capture_output=True, timeout=20,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode != 0:
                    ok_all = False
                    msgs.append(f"{p.name} 导入失败")
            except Exception as e:
                ok_all = False
                msgs.append(f"{p.name} 导入异常: {e}")
        return ok_all, "；".join(msgs) if msgs else "导入完成"


# ---------------- 写入操作 ----------------

def _apply_until(end_utc: datetime, start_utc: Optional[datetime] = None) -> dict[str, Any]:
    """按「精确到秒的到期时刻」写入完整键组。
    修复场景直接沿用原到期时间，避免「剩余天数→年→天数」的取整漂移。"""
    _require_write()

    now = datetime.now(timezone.utc)
    start = start_utc.astimezone(timezone.utc) if start_utc else now
    end = end_utc.astimezone(timezone.utc)

    if end <= now:
        raise ValueError("到期时间早于当前时间")

    # 起始时间异常（晚于到期）时回退到当前时刻，避免算出负时长
    if start >= end:
        start = now

    start_s = fmt_utc_z(start)
    end_s = fmt_utc_z(end)
    local_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    span_days = max(1, int((end - start).total_seconds() // 86400))
    remain_days = max(1, int((end - now).total_seconds() // 86400))
    # 上限必须明显大于剩余天数，否则会被引擎截断回默认
    cap = max(36500, remain_days + 730)

    _write_value(_ROOT, UX_PATH, CAP_VALUE, cap, winreg.REG_DWORD)

    for n in TIME_START_VALUES:
        _write_value(_ROOT, UX_PATH, n, start_s, winreg.REG_SZ)
    for n in TIME_END_VALUES:
        _write_value(_ROOT, UX_PATH, n, end_s, winreg.REG_SZ)

    for n in POL_STATUS_VALUES:
        _write_value(_ROOT, POL_PATH, n, 1, winreg.REG_DWORD)
    for n in POL_DATE_VALUES:
        _write_value(_ROOT, POL_PATH, n, local_s, winreg.REG_SZ)

    return {"start": start_s, "end": end_s, "days": span_days,
            "remain": remain_days, "cap": cap}


def apply_pause(years: float) -> dict[str, Any]:
    """把更新暂停到 N 年后。必须写完整键组，否则 24H2+ 会钳制。"""
    if years <= 0:
        raise ValueError("年数必须大于 0")
    if years > 100:
        raise ValueError("年数不能超过 100")
    now = datetime.now(timezone.utc)
    return _apply_until(now + timedelta(days=years * 365.25), start_utc=now)


def clear_pause() -> None:
    _require_write()

    for n in TIME_START_VALUES + TIME_END_VALUES + (CAP_VALUE,):
        _delete_value(_ROOT, UX_PATH, n)

    for n in POL_STATUS_VALUES:
        _write_value(_ROOT, POL_PATH, n, 0, winreg.REG_DWORD)
    for n in POL_DATE_VALUES:
        _delete_value(_ROOT, POL_PATH, n)


def repair() -> dict[str, Any]:
    """按当前实际状态修复：有未过期的到期时间就原样沿用（零漂移），否则重设 40 年。"""
    st = read_pause_state()
    now = datetime.now(timezone.utc)
    if st.expiry_utc and st.expiry_utc > now:
        result = _apply_until(st.expiry_utc, start_utc=st.start_utc)
        result["mode"] = "preserve"
        return result
    result = apply_pause(40)
    result["mode"] = "reset40"
    return result


# ---------------- 故障注入：验证自检真的能认出退化 ----------------

def _fault_injection() -> tuple[int, list[str]]:
    """在 HKCU 下伪造「系统更新后暂停被改坏」的现场，验证 run_health_check 能否定位。
    绝不拿真实 HKLM 做实验。返回 (失败数, 逐条日志)。"""
    HK = getattr(winreg, "HKEY_CURRENT_USER")
    BASE = r"Software\_WuPauseFaultTest"
    PREFIX = BASE + "\\"
    lines: list[str] = []
    fails = 0

    def rm_tree(path: str) -> None:
        subs: list[str] = []
        try:
            with winreg.OpenKey(HK, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
                i = 0
                while True:
                    try:
                        subs.append(winreg.EnumKey(k, i))
                    except OSError:
                        break
                    i += 1
        except OSError:
            return
        for s in subs:
            rm_tree(path + "\\" + s)
        try:
            winreg.DeleteKey(HK, path)
        except OSError:
            pass

    def reset_scene() -> None:
        rm_tree(BASE)
        use_test_root(HK, PREFIX)
        winreg.CreateKeyEx(HK, PREFIX + UX_PATH, 0, winreg.KEY_WRITE)
        winreg.CreateKeyEx(HK, PREFIX + POL_PATH, 0, winreg.KEY_WRITE)

    def put(path: str, name: str, value, vtype) -> None:
        _write_value(HK, path, name, value, vtype)

    def full_set(years: float, cap: int, status: int) -> None:
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=years * 365.25)
        s, e = fmt_utc_z(now), fmt_utc_z(end)
        put(UX_PATH, CAP_VALUE, cap, winreg.REG_DWORD)
        for n in TIME_START_VALUES:
            put(UX_PATH, n, s, winreg.REG_SZ)
        for n in TIME_END_VALUES:
            put(UX_PATH, n, e, winreg.REG_SZ)
        for n in POL_STATUS_VALUES:
            put(POL_PATH, n, status, winreg.REG_DWORD)
        for n in POL_DATE_VALUES:
            put(POL_PATH, n, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), winreg.REG_SZ)

    def expect(label: str, want: dict[str, str]) -> None:
        nonlocal fails
        rep = run_health_check()
        got = {c.key: c.status for c in rep.checks}
        bad = [f"{k} 期望 {v} 实际 {got.get(k)}" for k, v in want.items() if got.get(k) != v]
        if bad:
            fails += 1
            lines.append(f"  FAIL  {label} -> " + "; ".join(bad))
        else:
            lines.append(f"  OK    {label}")

    try:
        # S1 系统更新只写了部分时间值
        reset_scene()
        put(UX_PATH, "PauseUpdatesExpiryTime",
            fmt_utc_z(datetime.now(timezone.utc) + timedelta(days=3650)), winreg.REG_SZ)
        expect("S1 只写了一半时间值", {"values": "fail"})

        # S2 上限被重置回默认 35 天，长暂停会被钳制
        reset_scene()
        full_set(40, 35, 1)
        expect("S2 上限被重置回 35 天", {"cap": "fail"})

        # S3 到期时间格式被改坏
        reset_scene()
        full_set(40, 36500, 1)
        put(UX_PATH, "PauseUpdatesExpiryTime", "not-a-date", winreg.REG_SZ)
        expect("S3 到期时间格式被改坏", {"time_fmt": "fail"})

        # S4 状态位被清零但时间值还在
        reset_scene()
        full_set(40, 36500, 0)
        expect("S4 状态位被清零", {"status": "warn"})

        # S5 暂停已到期
        reset_scene()
        now = datetime.now(timezone.utc)
        put(UX_PATH, CAP_VALUE, 36500, winreg.REG_DWORD)
        for n in TIME_START_VALUES:
            put(UX_PATH, n, fmt_utc_z(now - timedelta(days=40)), winreg.REG_SZ)
        for n in TIME_END_VALUES:
            put(UX_PATH, n, fmt_utc_z(now - timedelta(days=1)), winreg.REG_SZ)
        for n in POL_STATUS_VALUES:
            put(POL_PATH, n, 1, winreg.REG_DWORD)
        expect("S5 暂停已到期", {"expiry": "warn"})

        # S6 完全正确，六项必须全绿（防误报）
        reset_scene()
        full_set(40, 36500, 1)
        expect("S6 完整正确写入", {
            "values": "pass", "time_fmt": "pass", "consist": "pass",
            "cap": "pass", "status": "pass", "expiry": "pass",
        })

        # S7 修复保真：上限不足时，修复必须沿用原到期时间，不得有秒级漂移
        reset_scene()
        full_set(40, 35, 1)
        before_exp = read_pause_state().expiry_utc
        repair()
        after = read_pause_state()
        if before_exp and after.expiry_utc:
            drift = abs((after.expiry_utc - before_exp).total_seconds())
        else:
            drift = 9e9
        ok_cap = (after.cap_days or 0) >= 36500
        ok_status = after.quality_status == 1
        if drift < 1 and ok_cap and ok_status:
            lines.append("  OK    S7 修复后到期时间零漂移且上限补齐")
        else:
            fails += 1
            lines.append(
                "  FAIL  S7 修复异常: 漂移 {:.0f}s, cap={}, status={}".format(
                    drift, after.cap_days, after.quality_status))
    finally:
        rm_tree(BASE)
        reset_root()

    return fails, lines


# ---------------- 自检（开发用，验证核心逻辑） ----------------

def _selftest() -> int:
    _ensure_utf8_stdout()
    fails = 0
    print("=" * 60)
    print(f"{APP_NAME} v{APP_VERSION} 核心层自检")
    print("=" * 60)

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal fails
        if cond:
            print(f"  OK    {name}" + (f"  ({extra})" if extra else ""))
        else:
            print(f"  FAIL  {name}" + (f"  ({extra})" if extra else ""))
            fails += 1

    # 时间解析
    cases = [
        ("2026-11-17T11:19:00Z", True),
        ("2026-11-17 11:19:00", True),
        ("2026-11-17T11:19:00", True),
        ("2026-11-17T11:19:00.123Z", True),
        ("2026-11-17T11:19Z", True),
        ("garbage", False),
        ("", False),
        (None, False),
    ]
    for text, expect_ok in cases:
        got = parse_utc(text)
        check(f"parse_utc({text!r})", (got is not None) == expect_ok,
              "" if got is None else got.isoformat())

    # 往返
    now = datetime.now(timezone.utc).replace(microsecond=0)
    check("fmt/parse 往返无损", parse_utc(fmt_utc_z(now)) == now, fmt_utc_z(now))

    # 系统信息
    info = read_system_info()
    check("读到 build 号", info.build > 0, str(info.build))
    check("Win10/11 判定", isinstance(info.is_win11, bool),
          f"{info.product_name} / win11={info.is_win11}")
    check("支持矩阵有档位", info.matrix_level in ("full", "inferred", "unknown", "legacy"),
          f"{info.matrix_level} - {info.matrix_name}")
    check("24H2+ 标记需要全键组",
          info.needs_full_keyset == (info.build >= 26100), str(info.needs_full_keyset))

    # 暂停状态（只读）
    st = read_pause_state()
    check("读到暂停状态", isinstance(st.exists, bool),
          f"paused={st.is_paused} remain={st.remaining_days}d")
    if st.expiry_utc:
        check("到期时间可解析", st.expiry_utc.year >= 2020, fmt_local(st.expiry_utc))

    # 健康检查（只读，不写）
    rep = run_health_check()
    check("健康检查有结果", rep.total >= 10, f"{rep.total} 项")
    check("检查项状态合法",
          all(c.status in ("pass", "warn", "fail", "skip") for c in rep.checks))
    check("每项都有名称与详情", all(c.name and c.detail for c in rep.checks))
    print()
    print(f"  健康检查: {rep.passed} 通过 / {rep.warned} 警告 / "
          f"{rep.failed} 失败 / {rep.skipped} 跳过")
    for c in rep.checks:
        mark = {"pass": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}[c.status]
        print(f"    [{mark}] {c.name}: {c.detail}")

    # 备份管理（写临时目录，不污染真实备份列表）
    try:
        import tempfile
        tmp_base = Path(tempfile.mkdtemp(prefix="wu_selftest_"))
        bm = BackupManager(tmp_base)
        bk = bm.snapshot("selftest")
        check("备份生成成功", Path(bk["txt"]).exists(), bk["timestamp"])
        check("备份 txt 非空", Path(bk["txt"]).stat().st_size > 100,
              f"{Path(bk['txt']).stat().st_size} bytes")
        check("备份列表可读", len(bm.list_backups()) >= 1,
              f"{len(bm.list_backups())} 份")
        check("备份落在临时目录", str(bm.dir).startswith(str(tmp_base)))
    except Exception as e:
        check("备份流程", False, str(e))

    # 故障注入：验证自检本身能认出「系统更新把暂停改坏」的各种现场
    print()
    print("  故障注入（HKCU 沙箱，不碰真实系统）:")
    try:
        f_fails, f_lines = _fault_injection()
        for ln in f_lines:
            print(ln)
        fails += f_fails
    except Exception as e:
        print("  FAIL  故障注入执行异常: " + str(e))
        fails += 1

    print()
    if fails == 0:
        print("核心层自检全部通过")
    else:
        print(f"核心层自检失败 {fails} 项")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(f"{APP_NAME} v{APP_VERSION} 核心层。用 --selftest 跑自检。")
