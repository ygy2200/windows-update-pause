# -*- coding: utf-8 -*-
"""
真实 HKLM 写入测试

验证「写入注册表后，系统能不能正确读回」——这是 HKCU 沙箱测试覆盖不到的最后一层。

**危险操作，双重保险：**

1. 必须显式设置环境变量 `WU_PAUSE_ALLOW_HKLM_TEST=1` 才会执行。
   不设就跳过（本地误跑也不会动到你的注册表）。
2. 必须是管理员。

设计用途：在 GitHub Actions 的一次性虚拟机上跑，验证真实写入链路。
本地不要跑，除非你清楚自己在做什么。

执行流程：备份原值（内存）→ 写入 10 年 → 逐项校验 → 恢复原值 → 校验恢复。

用法：
    set WU_PAUSE_ALLOW_HKLM_TEST=1
    python test_hklm.py
"""

from __future__ import annotations

import os
import sys
import winreg
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

# README 声称「不禁用任何服务」。这里挑几个最相关的，把这句话变成可断言的事实。
WATCHED_SERVICES = ("wuauserv", "UsoSvc", "WaaSMedicSvc", "BITS", "DoSvc")


def _service_start_types() -> dict[str, object]:
    """读关键服务的启动类型：0=Boot 1=System 2=自动 3=手动 4=禁用。

    读注册表而不是解析 sc.exe：不受控制台语言和编码影响。
    启动类型是配置，不会被系统自己改；服务「运行/停止」会随系统活动变化，
    所以只比对启动类型，比对运行状态会误报。
    """
    out: dict[str, object] = {}
    for name in WATCHED_SERVICES:
        v, _ = core._read_raw(
            winreg.HKEY_LOCAL_MACHINE,
            rf"SYSTEM\CurrentControlSet\Services\{name}", "Start")
        out[name] = v
    return out


def _write_summary(text: str) -> None:
    """写 GitHub Actions 的步骤摘要，让 CI 页面一眼看到结论。"""
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if not p:
        return
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def main() -> int:
    core._ensure_utf8_stdout()
    if os.environ.get("WU_PAUSE_ALLOW_HKLM_TEST") != "1":
        print("SKIP：未设置 WU_PAUSE_ALLOW_HKLM_TEST=1")
        print("      出于安全，不在本机真实注册表上做写入测试。")
        _write_summary("### 真实 HKLM 测试\n\n⏭ 跳过（未设置 WU_PAUSE_ALLOW_HKLM_TEST=1）\n")
        return 0

    if not core.has_admin():
        print("SKIP：当前不是管理员，无法写 HKLM。")
        print("      CI 环境若带提权会自动执行；本地请以管理员运行。")
        _write_summary("### 真实 HKLM 测试\n\n⏭ 跳过（非管理员，无法写 HKLM）\n")
        return 0

    print("=" * 58)
    print("真实 HKLM 写入测试")
    print("=" * 58)

    fails = 0

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal fails
        tail = f"  ({extra})" if extra else ""
        if cond:
            print(f"  OK    {name}{tail}")
        else:
            fails += 1
            print(f"  FAIL  {name}{tail}")

    # ---------- 1. 备份原状（只在内存里，不落盘） ----------
    original = core.read_pause_state()
    print(f"\n原状态：cap={original.cap_days}  "
          f"expiry={original.expiry_utc}  "
          f"status={original.quality_status}/{original.feature_status}")

    svc_before = _service_start_types()
    print("服务启动类型（操作前）：" +
          "  ".join(f"{k}={v}" for k, v in svc_before.items()))

    ok_write = False
    try:
        # ---------- 2. 写入 ----------
        print("\n写入 10 年暂停…")
        core.apply_pause(10)

        # ---------- 3. 读回校验 ----------
        st = core.read_pause_state()
        check("上限已写入且足够", (st.cap_days or 0) >= 36500, f"cap={st.cap_days}")
        check("两个状态位 = 1", st.quality_status == 1 and st.feature_status == 1,
              f"{st.quality_status}/{st.feature_status}")
        check("到期时间可解析", st.expiry_utc is not None, str(st.expiry_utc))
        check("起始时间可解析", st.start_utc is not None)
        check("is_paused 为真", st.is_paused)

        if st.expiry_utc:
            years = (st.expiry_utc - datetime.now(timezone.utc)).days / 365.25
            check("到期约 10 年后", 9.9 < years < 10.1, f"{years:.2f} 年")

        starts = {st.raw.get(n) for n in core.TIME_START_VALUES}
        ends = {st.raw.get(n) for n in core.TIME_END_VALUES}
        check("三组起始时间一致", len(starts) == 1, f"{len(starts)} 种取值")
        check("三组到期时间一致", len(ends) == 1, f"{len(ends)} 种取值")

        # ---------- 4. 健康检查必须认这个状态 ----------
        rep = core.run_health_check()
        core_bad = [f"{c.key}={c.status}" for c in rep.checks
                    if c.category == "core" and c.status != "pass"]
        check("健康检查「暂停有效性」六项全绿", not core_bad,
              ", ".join(core_bad) if core_bad else "all pass")

        ok_write = fails == 0

        # ---------- 4b. 备份能不能真的把系统救回来 ----------
        # 「恢复更新」和「导入备份」是用户最后的保险。此前它只在 HKCU 沙箱里
        # 跑过（test_e2e 的 T10），真实 HKLM 上从未执行过。这里补上。
        print("\n验证备份回滚（真跑 reg export/import）…")
        import tempfile
        bk_dir = Path(tempfile.mkdtemp(prefix="hklm_bk_"))
        bm_live = core.BackupManager(bk_dir)
        bk_live = bm_live.snapshot("hklm-roundtrip")
        check("真实环境中 .reg 导出成功", bk_live.get("reg_ok") is True,
              str(bk_live.get("warnings") or "无警告"))

        core.clear_pause()
        check("清除后确实没有暂停", core.read_pause_state().expiry_utc is None)

        ok_rb, msg_rb = bm_live.restore(bk_live)
        check("真实环境回滚执行成功", ok_rb is True, msg_rb)

        st_rb = core.read_pause_state()
        days_rb = st_rb.remaining_days
        check("回滚后恢复成 10 年的暂停",
              st_rb.is_paused and 3640 <= days_rb <= 3653, f"剩余 {days_rb} 天")

    finally:
        # ---------- 5. 恢复原状 ----------
        print("\n恢复原状…")
        try:
            now = datetime.now(timezone.utc)
            if original.expiry_utc and original.start_utc and original.expiry_utc > now:
                core._apply_until(original.expiry_utc, original.start_utc)
                restored = core.read_pause_state()
                drift = abs((restored.expiry_utc - original.expiry_utc).total_seconds()) \
                    if restored.expiry_utc else 9e9
                check("原暂停状态已恢复（零漂移）", drift < 2, f"漂移 {drift:.0f}s")
            else:
                core.clear_pause()
                restored = core.read_pause_state()
                check("测试写入已清除", restored.expiry_utc is None
                      and restored.cap_days is None)
                check("状态位已归零",
                      restored.quality_status == 0 and restored.feature_status == 0)
        except Exception as e:
            fails += 1
            print(f"  FAIL  恢复原状失败: {e}")

        # ---------- 6. 「不禁用服务」承诺的实证 ----------
        # 注意断言的是「没有被设为禁用」，不是「启动类型一字不变」。
        # 一开始写成后者，结果 py3.12 那条 runner 上 BITS 自己从手动(3)变成了自动(2)
        # ——系统本来就会调整 BITS，那是它的正常活动，不是这个工具干的。
        # 我们真正要防的是「为了阻止更新而把服务禁掉」，即 Start=4。
        svc_after = _service_start_types()
        print("服务启动类型（操作后）：" +
              "  ".join(f"{k}={v}" for k, v in svc_after.items()))
        newly_disabled = {k: (svc_before.get(k), v)
                          for k, v in svc_after.items()
                          if v == 4 and svc_before.get(k) != 4}
        check("操作没有把任何服务设为禁用", not newly_disabled,
              f"{len(svc_after)} 个服务均未被禁用" if not newly_disabled
              else str(newly_disabled))

    print()
    print("=" * 58)
    if fails == 0:
        print("真实 HKLM 往返验证通过" + ("" if ok_write else "（写入阶段有失败项）"))
        _write_summary("### 真实 HKLM 测试\n\n"
                       f"✅ 通过 —— 写入后系统能正确读回，恢复零漂移\n")
    else:
        print(f"真实 HKLM 测试失败 {fails} 项")
        _write_summary(f"### 真实 HKLM 测试\n\n❌ 失败 {fails} 项\n")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
