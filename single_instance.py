"""单实例保护。

为什么单独成文件：这是纯 Win32 能力，和 Qt 无关。放在 main.py 里会让测试
不得不先装 PySide6，等于把「能不能测」和「有没有装 GUI 框架」绑死。

为什么用命名互斥体而不是文件锁：
    进程被强杀时内核自动回收互斥体；文件锁会留下一个「看起来还活着」的锁文件，
    用户下次打开会被告知「已经在运行」，然后满硬盘找那个不存在的进程。

`Local\\` 前缀：仅当前登录会话。多用户同时登录时各开一个实例，互不干扰。
"""

from __future__ import annotations

import ctypes

from core import APP_NAME

MUTEX_NAME = "Local\\WindowsUpdatePauseTool_SingleInstance"
ERROR_ALREADY_EXISTS = 183

# 必须持有引用直到进程退出：句柄一旦释放，互斥体就没了，保护失效。
# 用 dict 而不是单个变量：claim() 允许传自定义名字，不同名字各持一个句柄。
_handles: dict[str, int] = {}
_k32 = None


def _kernel32():
    global _k32
    if _k32 is not None:
        return _k32
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.CloseHandle.restype = ctypes.c_int
    _k32 = k
    return k


def claim(name: str = MUTEX_NAME) -> bool:
    """独占单实例。

    返回 True  = 本进程是唯一实例，可以继续；
         False = 已有实例在跑，调用方应当退出（可先调 activate_existing 提窗口）。

    `name` 默认就是生产用的那个名字；测试可以传独立的测试名，
    这样「本机开着工具时跑测试」不会互相干扰。

    任何异常都返回 True —— 单实例是体验优化，不能变成新的失败点。
    """
    try:
        k = _kernel32()
        ctypes.set_last_error(0)
        h = k.CreateMutexW(None, 0, name)
        if not h:
            return True                      # 连互斥体都建不出来，就别拦用户
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            # 关键：这次调用给对象的引用计数 +1，必须还回去。
            # 不还的话本进程就一直握着一个句柄 → 对方退出后互斥体仍存活 →
            # 本进程后续 claim 永远返回 False，等于把自己的锁丢在别人手里。
            k.CloseHandle(h)
            return False
        _handles[name] = h
        return True
    except Exception:
        return True


def activate_existing() -> bool:
    """把已在运行的窗口提到前台。找不到窗口返回 False。"""
    try:
        u = ctypes.windll.user32
        found: list[int] = []
        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(hwnd, _lparam):
            if u.IsWindowVisible(hwnd):
                n = u.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(hwnd, buf, n + 1)
                    if APP_NAME in buf.value:
                        found.append(hwnd)
            return 1

        u.EnumWindows(EnumProc(_cb), 0)
        if not found:
            return False
        h = found[0]
        # 最小化的必须先还原，否则 SW_SHOW 不会把它弄出来
        u.ShowWindow(h, 9 if u.IsIconic(h) else 5)   # SW_RESTORE / SW_SHOW
        u.SetForegroundWindow(h)
        return True
    except Exception:
        return False
