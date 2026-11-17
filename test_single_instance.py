"""单实例保护的跨进程验证。

为什么必须跨进程：
    同一进程里第二次 CreateMutexW 同名互斥体，get_last_error 仍是
    ERROR_ALREADY_EXISTS —— claim() 永远返回 False，测出来的是假象，
    测不到「另一个进程先跑」这个真实场景。

为什么要用独立的测试互斥体名：
    如果直接用生产名，用户本机开着工具时跑这个测试就会全部失败——
    那不是代码坏了，是测试假设了「没人占着锁」。用带 PID 的测试名，
    既不干扰真实实例，也不被真实实例干扰。

用例顺序有讲究：主进程一旦 claim() 就永久持锁（设计如此，句柄不释放），
所以「被别人挡住」的用例必须排在「自己拿到」之前。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import single_instance as si

# 每个测试进程用独立名字，避免与真实实例或并行 CI 互撞
TEST_MUTEX = f"Local\\WindowsUpdatePauseTool_Test_{os.getpid()}"

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    if ok:
        print(f"  OK    {name}" + (f"  ({detail})" if detail else ""))
    else:
        fails += 1
        print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))


def spawn_holder(seconds: float) -> subprocess.Popen:
    """子进程：先占住测试互斥体并回一行结果，再保持一段时间。"""
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {HERE!r})
        import single_instance as si
        print('claim=' + str(si.claim({TEST_MUTEX!r})), flush=True)
        time.sleep({seconds})
    """)
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )


def main() -> int:
    print("=" * 58)
    print("单实例保护测试")
    print("=" * 58)

    # 先打个招呼：真实实例在不在跑，不影响本测试，但值得让读者知道
    real_busy = not si.claim(si.MUTEX_NAME)
    if real_busy:
        print(f"  --    提示：本机已有工具实例在运行（{si.MUTEX_NAME} 被占），")
        print("        以下用例改用独立测试互斥体，不受影响。")
    print()

    # ---------- 阶段 1：别人持有锁 ----------
    holder = spawn_holder(6.0)
    line = holder.stdout.readline().strip()
    check("持锁子进程自己是首个实例", line == "claim=True", line or "(无输出)")

    mine = si.claim(TEST_MUTEX)
    check("已有实例时 claim 返回 False", mine is False, str(mine))

    second = spawn_holder(0.1)
    out_b, _ = second.communicate(timeout=20)
    got_b = [l for l in out_b.splitlines() if l.startswith("claim=")]
    check("第三个进程同样被拦",
          bool(got_b) and got_b[0] == "claim=False", got_b[0] if got_b else "(无输出)")

    act = si.activate_existing()
    check("activate_existing 不抛异常且返回布尔", isinstance(act, bool), str(act))

    holder.wait(timeout=20)

    # ---------- 阶段 2：锁已释放，本进程接管 ----------
    mine2 = si.claim(TEST_MUTEX)
    check("持锁者退出后本进程能接管", mine2 is True, str(mine2))

    again = si.claim(TEST_MUTEX)
    check("接管后再次 claim 返回 False（锁没丢）", again is False, str(again))

    after = spawn_holder(0.1)
    out_c, _ = after.communicate(timeout=20)
    got_c = [l for l in out_c.splitlines() if l.startswith("claim=")]
    check("接管后新进程被拦",
          bool(got_c) and got_c[0] == "claim=False", got_c[0] if got_c else "(无输出)")

    # ---------- 静态约定 ----------
    check("生产互斥体名不含版本号（跨版本互拦）",
          "2." not in si.MUTEX_NAME, si.MUTEX_NAME)
    check("生产互斥体名带 Local\\ 前缀（按登录会话隔离）",
          si.MUTEX_NAME.startswith("Local\\"), si.MUTEX_NAME)
    check("测试用的是独立名字（不污染真实实例）",
          TEST_MUTEX != si.MUTEX_NAME, TEST_MUTEX)

    # ---------- 异常兜底：不能把用户挡在门外 ----------
    orig = si._kernel32

    def boom():
        raise OSError("模拟 ctypes 不可用")

    si._kernel32 = boom
    try:
        check("互斥体不可用时放行（返回 True）",
              si.claim("Local\\WindowsUpdatePauseTool_Test_boom") is True)
    finally:
        si._kernel32 = orig

    print()
    if fails == 0:
        print("单实例保护测试全部通过")
    else:
        print(f"单实例保护测试失败 {fails} 项")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
