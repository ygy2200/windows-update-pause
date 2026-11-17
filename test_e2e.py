# -*- coding: utf-8 -*-
"""
端到端功能验证

在 HKCU 沙箱里跑完整的「写入 → 校验 → 清除 → 再写入 → 修复」闭环。
不碰真实 HKLM，不需要管理员权限。

用法：
    python test_e2e.py

退出码 0 = 全部通过。
"""

from __future__ import annotations

import os
import string
import sys
import tempfile
import winreg
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

HK = winreg.HKEY_CURRENT_USER
BASE = r"Software\_WuPauseE2E"
PREFIX = BASE + "\\"

fails = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global fails
    tail = f"  ({extra})" if extra else ""
    if cond:
        print(f"  OK    {name}{tail}")
    else:
        fails += 1
        print(f"  FAIL  {name}{tail}")


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


def setup() -> None:
    rm_tree(BASE)
    core.use_test_root(HK, PREFIX)
    winreg.CreateKeyEx(HK, PREFIX + core.UX_PATH, 0, winreg.KEY_WRITE)
    winreg.CreateKeyEx(HK, PREFIX + core.POL_PATH, 0, winreg.KEY_WRITE)


def main() -> int:
    core._ensure_utf8_stdout()
    print("=" * 58)
    print("端到端功能验证（HKCU 沙箱，不碰真实系统）")
    print("=" * 58)

    # ---------------- T1 写入 ----------------
    print("\nT1  apply_pause(40) 写入后逐项校验")
    setup()
    t0 = datetime.now(timezone.utc)
    core.apply_pause(40)
    st = core.read_pause_state()

    check("暂停上限 >= 36500", (st.cap_days or 0) >= 36500, f"cap={st.cap_days}")
    check("质量更新状态位 = 1", st.quality_status == 1, str(st.quality_status))
    check("功能更新状态位 = 1", st.feature_status == 1, str(st.feature_status))
    check("起始时间可解析", st.start_utc is not None)
    check("到期时间可解析", st.expiry_utc is not None)
    if st.expiry_utc:
        years = (st.expiry_utc - t0).days / 365.25
        check("到期约 40 年后", 39.9 < years < 40.1, f"{years:.2f} 年")
        check("剩余天数约 14610", abs(st.remaining_days - 14610) < 5,
              f"{st.remaining_days} 天")
    starts = {st.raw.get(n) for n in core.TIME_START_VALUES}
    ends = {st.raw.get(n) for n in core.TIME_END_VALUES}
    check("三组起始时间一致", len(starts) == 1, str(len(starts)) + " 种取值")
    check("三组到期时间一致", len(ends) == 1, str(len(ends)) + " 种取值")
    check("is_paused = True", st.is_paused)

    rep1 = core.run_health_check()
    core_bad = [f"{c.key}={c.status}" for c in rep1.checks
                if c.category == "core" and c.status != "pass"]
    check("写入后「暂停有效性」六项全绿", not core_bad,
          ", ".join(core_bad) if core_bad else "all pass")

    # ---------------- T2 清除 ----------------
    print("\nT2  clear_pause() 清除后不留残值")
    core.clear_pause()
    st2 = core.read_pause_state()
    check("到期时间已删除", st2.expiry_utc is None)
    check("起始时间已删除", st2.start_utc is None)
    check("上限值已删除", st2.cap_days is None)
    check("质量状态位 = 0", st2.quality_status == 0, str(st2.quality_status))
    check("功能状态位 = 0", st2.feature_status == 0, str(st2.feature_status))
    check("is_paused = False", not st2.is_paused)

    vals2 = {c.key: c.status for c in core.run_health_check().checks}
    check("清除后 values 报「未设置暂停」", vals2.get("values") == "warn",
          str(vals2.get("values")))
    check("清除后 expiry 报 skip", vals2.get("expiry") == "skip",
          str(vals2.get("expiry")))

    # ---------------- T3 修复保真 ----------------
    print("\nT3  上限被系统改小后 repair() 保留原到期时间")
    setup()
    core.apply_pause(40)
    orig = core.read_pause_state()
    core._write_value(HK, core.UX_PATH, core.CAP_VALUE, 35, winreg.REG_DWORD)
    check("人为制造上限不足", core.read_pause_state().cap_days == 35)

    result = core.repair()
    fixed = core.read_pause_state()
    if fixed.expiry_utc and orig.expiry_utc:
        drift = abs((fixed.expiry_utc - orig.expiry_utc).total_seconds())
    else:
        drift = 9e9
    check("修复后到期时间零漂移", drift < 1, f"漂移 {drift:.0f} 秒")
    check("修复后上限已补齐", (fixed.cap_days or 0) >= 36500,
          f"cap={fixed.cap_days}")
    check("修复模式 = preserve", result.get("mode") == "preserve",
          str(result.get("mode")))
    vals3 = {c.key: c.status for c in core.run_health_check().checks}
    check("修复后 cap 项转绿", vals3.get("cap") == "pass", str(vals3.get("cap")))

    # ---------------- T4 备份唯一性 ----------------
    print("\nT4  同一秒内连续两次备份不得互相覆盖")
    tmp = Path(tempfile.mkdtemp(prefix="wu_e2e_"))
    bm = core.BackupManager(tmp)
    a = bm.snapshot("e2e-1")
    b = bm.snapshot("e2e-2")
    check("两次备份时间戳不同", a["timestamp"] != b["timestamp"],
          f"{a['timestamp']} vs {b['timestamp']}")
    check("两份快照文件都在", Path(a["txt"]).exists() and Path(b["txt"]).exists())
    check("备份目录落在指定位置", str(bm.dir).startswith(str(tmp)))

    # ---------------- T5 测试模式隔离 ----------------
    print("\nT5  测试模式下备份不得导出真实 HKLM")
    ghost = tmp / "_should_not_exist.reg"
    ok = bm._export("HKLM\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings", ghost)
    check("_export 在测试模式返回 False", ok is False)
    check("未生成 .reg 文件", not ghost.exists())

    # ---------------- T4b 备份列表排序 ----------------
    print("\nT4b 备份列表按修改时间倒序")
    lst = bm.list_backups()
    check("列表至少 2 条", len(lst) >= 2, f"{len(lst)} 条")
    if len(lst) >= 2:
        check("最新一条排最前", lst[0]["timestamp"] == b["timestamp"],
              f"{lst[0]['timestamp']} / 期望 {b['timestamp']}")

    # ---------------- T6 回滚控制流 ----------------
    print("\nT6  测试模式下回滚不执行真实导入")
    ok6, msg6 = bm.restore(b)
    check("restore 返回成功", ok6 is True, str(msg6))
    check("提示标明测试模式", "测试模式" in msg6, str(msg6))

    # ---------------- T7 参数校验与异常路径 ----------------
    print("\nT7  参数校验与异常路径")
    setup()
    for bad in (0, -1, 101):
        try:
            core.apply_pause(bad)
            check(f"apply_pause({bad}) 应拒绝", False, "没有抛异常")
        except ValueError:
            check(f"apply_pause({bad}) 正确拒绝", True)
        except Exception as e:
            check(f"apply_pause({bad}) 异常类型不对", False, repr(e))

    # 到期时间早于当前：必须拒绝，不能写出一个已过期的暂停
    try:
        core._apply_until(datetime.now(timezone.utc) - timedelta(days=1))
        check("到期时间早于当前应拒绝", False, "没有抛异常")
    except ValueError:
        check("到期时间早于当前正确拒绝", True)

    # 起始晚于到期：应自动回退到当前时刻，不能算出负时长
    now = datetime.now(timezone.utc)
    core._apply_until(now + timedelta(days=10),
                      start_utc=now + timedelta(days=100))
    st7 = core.read_pause_state()
    check("起始晚于到期时自动回退",
          st7.expiry_utc is not None and 9 <= st7.remaining_days <= 10,
          f"剩余 {st7.remaining_days} 天")

    # ---------------- T8 写入失败自动回滚 ----------------
    print("\nT8  写入中途失败是否退回到操作前")

    # 先建立一个已知的「操作前状态」：暂停 1 年
    setup()
    core.apply_pause(1)
    before = core.read_pause_state()
    before_end = before.expiry_utc
    before_cap = before.cap_days
    check("已建立操作前状态（暂停 1 年）",
          before.is_paused and before.remaining_days >= 364,
          f"剩余 {before.remaining_days} 天")

    orig_write = core._write_value

    # T8a：第 6 次写入失败一次 —— 回滚应当成功
    seq = {"n": 0}

    def flaky_once(root, path, name, value, vtype):
        seq["n"] += 1
        if seq["n"] == 6:
            raise OSError("模拟注册表写入被拒绝")
        return orig_write(root, path, name, value, vtype)

    core._write_value = flaky_once
    try:
        core.apply_pause(40)
        check("写入失败时应抛 WriteFailed", False, "没有抛异常")
    except core.WriteFailed as e:
        msg = str(e)
        check("写入失败时抛 WriteFailed", True)
        check("异常说明已回滚", "已自动恢复" in msg, msg.splitlines()[-1][:40])
    except Exception as e:
        check("异常类型应为 WriteFailed", False, repr(e))
    finally:
        core._write_value = orig_write

    after = core.read_pause_state()
    check("回滚后到期时间与操作前一致",
          after.expiry_utc == before_end,
          f"{after.expiry_utc} vs {before_end}")
    check("回滚后上限与操作前一致", after.cap_days == before_cap,
          f"{after.cap_days} vs {before_cap}")
    check("回滚后仍处于暂停状态，不是半写", after.is_paused is True)

    # 健康检查不该因为这次失败留下任何红项
    rep = core.run_health_check()
    core_fails = [c.key for c in rep.checks
                  if c.category == "core" and c.status in ("fail", "warn")]
    check("回滚后暂停有效性检查全绿", not core_fails, str(core_fails))

    # T8b：从第 6 次起持续失败 —— 回滚不完整，必须在消息里说清
    seq2 = {"n": 0}

    def flaky_forever(root, path, name, value, vtype):
        seq2["n"] += 1
        if seq2["n"] >= 6:
            raise OSError("模拟持续拒绝")
        return orig_write(root, path, name, value, vtype)

    core._write_value = flaky_forever
    try:
        core.apply_pause(40)
        check("持续失败时应抛 WriteFailed", False, "没有抛异常")
    except core.WriteFailed as e:
        msg = str(e)
        check("回滚不完整时明确告知", "没成功" in msg, msg.splitlines()[2][:40] if len(msg.splitlines()) > 2 else "")
        check("给出后续处置建议", "一键修复" in msg)
    except Exception as e:
        check("异常类型应为 WriteFailed", False, repr(e))
    finally:
        core._write_value = orig_write

    # T8c：clear_pause 也要走同一套保护
    setup()
    core.apply_pause(1)
    before_clear = core.read_pause_state().expiry_utc
    seq3 = {"n": 0}

    def flaky_clear(root, path, name, value, vtype):
        seq3["n"] += 1
        if seq3["n"] == 2:
            raise OSError("模拟清除时被拒绝")
        return orig_write(root, path, name, value, vtype)

    # clear_pause 只调 _delete_value 和 _write_value(状态位)，
    # 所以要让删除也失败才触发回滚
    orig_delete = core._delete_value
    del_seq = {"n": 0}

    def flaky_delete(root, path, name):
        del_seq["n"] += 1
        if del_seq["n"] == 3:
            raise OSError("模拟删除被拒绝")
        return orig_delete(root, path, name)

    core._delete_value = flaky_delete
    try:
        core.clear_pause()
        check("清除失败时应抛 WriteFailed", False, "没有抛异常")
    except core.WriteFailed:
        check("清除中途失败也走同一套回滚", True)
    except Exception as e:
        check("清除失败异常类型不对", False, repr(e))
    finally:
        core._delete_value = orig_delete

    after_clear = core.read_pause_state()
    check("清除失败后暂停状态被还原",
          after_clear.expiry_utc == before_clear,
          f"{after_clear.expiry_utc} vs {before_clear}")

    # ---------------- T9 数据目录降级 ----------------
    print("\nT9  程序目录不可写时是否退到 LOCALAPPDATA")

    used = {d for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")}
    free_letters = [d for d in string.ascii_uppercase
                    if d not in used and d >= "R"]
    if not free_letters:
        check("找不到可用盘符做测试", False, "全盘符都被占用")
    else:
        ghost = Path(f"{free_letters[0]}:\\不存在的目录\\工具")
        check("假目标确实不可用", not ghost.exists(), str(ghost))

        bm_fb = core.BackupManager(ghost)
        in_local = "WindowsUpdatePauseTool" in str(bm_fb.dir)
        check("不可写时降级到 LOCALAPPDATA", in_local, str(bm_fb.dir))
        check("降级目录名仍是 backup", bm_fb.dir.name == "backup", bm_fb.dir.name)

        made = []
        try:
            bk_fb = bm_fb.snapshot("fallback-test")
            made = [bk_fb["txt"], bk_fb["reg_ux"], bk_fb["reg_pol"]]
            check("降级目录能真的写备份", Path(bk_fb["txt"]).exists(),
                  Path(bk_fb["txt"]).name)
            check("快照内容非空", Path(bk_fb["txt"]).stat().st_size > 100,
                  f"{Path(bk_fb['txt']).stat().st_size} 字节")
        finally:
            # 只删本次生成的三个文件，不动目录里其它东西
            for f in made:
                try:
                    rm_tree(str(f))
                except Exception:
                    pass

    # ---------------- 清理 ----------------
    print("\n清理测试树")
    rm_tree(BASE)
    core.reset_root()
    check("测试树已彻底清除", not core._key_exists(HK, BASE))

    print()
    print("=" * 58)
    if fails == 0:
        print("端到端全部通过")
    else:
        print(f"端到端失败 {fails} 项")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
