# Windows 更新暂停工具

把 Windows 更新暂停到指定年份（默认 40 年）的图形工具，随时能一键恢复。

**只写「暂停」状态**——不禁用服务、不改组策略、不删任何文件。所以随时能在系统设置里点「恢复更新」，不会把系统搞坏。

[![test](https://github.com/ygy2200/windows-update-pause/actions/workflows/test.yml/badge.svg)](https://github.com/ygy2200/windows-update-pause/actions/workflows/test.yml)
![version](https://img.shields.io/badge/version-2.1.7-blue)
![platform](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011-lightgrey)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

## 为什么需要它

Windows 11 24H2 之后的更新引擎有个行为：**只改注册表里的 `FlightSettingsMaxPauseDays` 不生效**，引擎会把超过默认 35 天的暂停强制钳制回去。必须同时写「天数上限 + 三对起止时间 + 两个状态位」这一整套键组。

结果是：手动改注册表的人经常发现「改完看着生效了，过几天又变回 5 周」。

这个工具解决三件事：

1. **认版本** —— 按 build 号判断引擎行为，知道该不该写全套
2. **能自检** —— 14 项检查，能分辨「暂停真的失效了」和「只是环境有点异常」
3. **坏了一键修** —— 系统更新把键改坏之后，按当前实际状态重新写，到期时间零漂移

---

## 用法

### 直接下载（推荐）

从 [**Releases**](https://github.com/ygy2200/windows-update-pause/releases/latest) 下载 `WindowsUpdatePause.exe`，双击运行，免安装、免 Python 环境。

首次打开是只读模式——读状态和跑自检不需要权限。点右上角「以管理员身份重启」之后才能修改。

> **首次运行 Windows 会拦一下，这是正常的。**
>
> 这个 exe 没有商业代码签名证书（个人项目，一年几百美元），所以 SmartScreen 会显示「Windows 已保护你的电脑 / 未知发布者」。点「**更多信息**」→「**仍要运行**」即可。
>
> 介意的话有三个选择：
> - 核对哈希：`certutil -hashfile WindowsUpdatePause.exe SHA256`，与 Release 页面标注的值比对
> - 自己从源码打包（见下），全部代码都在仓库里，没有隐藏二进制
> - 只跑源码：`python main.py`，不碰任何预编译文件
>
> 顺便说清它到底碰什么：**只写 11 个注册表值**（7 个在 `WindowsUpdate\UX\Settings`、4 个在 `WindowsUpdate\UpdatePolicy\Settings`），**不禁用任何服务、不改组策略、不联网**。想验证可以直接读 `core.py` 里 `apply_pause()` 的全部写入点，加起来就是这 11 个。

**只允许开一个窗口。** 已经开着的时候再双击，不会出现第二个窗口——程序会把已有窗口提到前台然后自己退出。这是为了避免两个实例同时改同一批注册表、同时往同一个备份目录写文件。

### 从源码运行

仓库**不含打包产物**。exe 是构建产物，不进版本控制——否则每次改一行代码，仓库就多背一份 49MB 的二进制历史。两种方式：

**直接跑脚本**（Python 3.9+）：

```powershell
pip install PySide6 PySide6-Fluent-Widgets
python main.py
```

**自己打包成单文件 exe**：

```powershell
pip install pyinstaller
pyinstaller --onefile --windowed --name WindowsUpdatePause --icon app.ico main.py
```

**关于体积。** 上面的命令直接打大约 100MB——PyInstaller 会从你的环境里把 `scipy` / `numpy` / `PIL` 一并收进来。这三个是 qfluentwidgets 的可选依赖（`[full]` extra），本工具用到的组件不需要。

排除它们可以砍到 **49MB**：

```powershell
pyinstaller --onefile --windowed --name WindowsUpdatePause --icon app.ico `
  --exclude-module scipy --exclude-module numpy --exclude-module PIL `
  --exclude-module setuptools --exclude-module pkg_resources `
  --exclude-module tkinter --exclude-module unittest --exclude-module pydoc `
  main.py
```

Release 里的成品就是这个配置打的，49MB，冷启动约 2.3 秒。CI 的 gui job 恰好只装 `PySide6 + PySide6-Fluent-Widgets` 就能跑，等于替这个结论做了背书——不会因为少了这三个包而漏功能。

还能再小到约 35MB，但需要连 `opengl32sw.dll` 一起砍（19.7MB 的软件 OpenGL 回退）。**不建议**：砍掉后在没有显卡驱动的虚拟机、远程桌面或老机器上可能白屏。

打包产物约 49MB（PySide6 占大头）。默认配置下 PyInstaller 还会把本机装着的 `scipy` / `numpy` / `PIL` 收进去——那是 qfluentwidgets 的 `[full]` extra，本工具用不到，加上 `--exclude-module` 能省掉近一半体积（完整命令见上）。

打包前建议先跑一遍测试，确认环境没问题：

```powershell
python core.py --selftest    # 29 项
python test_e2e.py           # 72 项，写路径闭环 + 失败回滚 + 备份往返
```

### 界面操作

**首次打开会显示只读模式**——这是正常的：读状态、跑自检不需要权限，只有写入需要。点右上角「以管理员身份重启」，之后所有操作都能用，不用每次弹 UAC。

界面三块：

- **系统信息**：识别到哪个版本、是否在已验证列表里、这个版本的引擎要不要求完整键组
- **更新暂停**：当前暂停状态、到期时间、剩余天数、上限；下面是操作按钮
- **自检**：14 项检查，按「暂停有效性 / 系统环境 / 基础能力」分组

---

## 命令行用法

除了开界面，程序也带三个命令行开关，方便脚本调用或排障：

```powershell
WindowsUpdatePause.exe --version    # 只打印版本
WindowsUpdatePause.exe --status     # 打印状态与自检结果，不启动界面
WindowsUpdatePause.exe --selftest   # 跑核心层 29 项自检（22 基础 + 7 故障注入）
```

源码运行时把 `WindowsUpdatePause.exe` 换成 `python main.py`。

`--status` 输出示例：

```
Windows 更新暂停工具 v2.1.3
----------------------------------------------------
系统版本 : Windows 11 26H2  build 26300.9711
支持状态 : 已验证支持  (实测验证通过)
暂停状态 : 已暂停，到期 2026-12-05 15:31:42，剩 17 天
暂停上限 : 5269
自检结果 : 14 项，12 通过 / 1 警告 / 0 失败 / 1 跳过
  [WARN] 写入权限: 非管理员，只能查看不能修改
```

**这三个开关不加载 Qt**，所以启动比 GUI 快得多。

打包版是 `--windowed`，没有控制台，命令行模式的输出会写到 `logs\cli-output.log` 并弹窗提示路径。设环境变量 `WU_PAUSE_NO_POPUP=1` 可跳过弹窗（自动化测试用）。

这也意味着**打包后的 exe 自己可以自证**：`WindowsUpdatePause.exe --selftest` 直接在 exe 内部跑完整 29 项断言，不需要另外装 Python。

---

## 自检 14 项怎么读

**这是整个工具最该看懂的部分。** 分组不是随便分的：

### 暂停有效性（6 项）—— 红项 = 暂停真的失效了

| 检查 | 失败意味着 |
|---|---|
| 暂停时间值完整 | 只写了一部分时间值，引擎会整组忽略 |
| 时间格式合法 | 值写进去但格式不对，等于没写 |
| 起止时间一致 | 功能更新和质量更新到期不同，行为会错乱 |
| 暂停上限充足 | 上限 < 剩余天数，暂停会被强制截断回 35 天 |
| 状态位与时间匹配 | 界面显示已暂停但实际不生效，或反过来 |
| 暂停未过期 | 已到期，更新恢复正常 |

### 系统环境（4 项）—— 黄项不影响暂停本身

| 检查 | 含义 |
|---|---|
| 无组策略冲突 | 有没有别的策略（`NoAutoUpdate`）在同一件事上抢方向盘 |
| 更新医生服务风险 | `WaaSMedicSvc` 在跑，可能周期性「修复」超长暂停 |
| 自动扫描任务 | `UpdateOrchestrator\Schedule Scan` 是否正常，坏了只影响自动触发 |
| 无第三方工具冲突 | 有没有 WUB / StopUpdates10 / hosts 屏蔽之类的残留 |

**关键区别**：环境组的黄项**不会**让暂停失效，只是解释「为什么更新检测不动」。别把两者混为一谈。

### 基础能力（4 项）

系统版本识别、注册表主键可达、暂停状态键可达、写入权限。

---

## 抗「系统一更新就废了」

这是 v2 的设计重点。具体做了六层：

**① 键路径存在性检查，而不是假设它一定在**
启动先确认 `UX\Settings` 和 `UpdatePolicy\Settings` 可达。万一哪天微软改了结构，会明确报「上级键在、目标键不在，疑似结构调整」，而不是静默失败。

**② 时间格式宽松解析**
注册表里写的是 `2026-11-17T11:19:00Z`，但解析器同时接受 `2026-11-17 11:19:00`、无 `Z`、带毫秒、只到分钟五种写法。微软改格式不至于直接崩。

**③ 上限与剩余天数做大小比较**
这是 24H2 之后最容易被钳制的地方。检查会算「上限天数 vs 剩余天数」，小于就直接标红——不靠猜。

**④ 状态位与时间值交叉校验**
`UpdatePolicy\Settings` 的 `PausedQualityStatus` 和 `UX\Settings` 的到期时间是两套数据，必须对得上。只写一半的情况能识别出来。

**⑤ 未知系统版本不阻断**
支持矩阵里没有你的 build 时，标「未验证」而不是拒绝工作。同代 build（相差 ≤3）会保守推断为可用。

**⑥ 一键修复按当前实际情况重算**
不是套模板：有未过期的暂停时间就保持原到期时间重写，没有就重设 40 年。修完立刻重新跑自检验证。

**最坏情况兜底**：每次写入前强制备份，任何一次改动都能回滚。

---

## 支持矩阵

| build | 版本 | 状态 |
|---|---|---|
| 26300 | Windows 11 26H2 | 已验证（实测） |
| 26200 | Windows 11 25H2 | 已验证 |
| 26100 | Windows 11 24H2 | 已验证（需完整键组） |
| 28000 | Windows 11 26H1 | 已验证 |
| 22631 | Windows 11 23H2 | 已验证 |
| 19044 / 19045 | Windows 10 21H2 / 22H2 | 旧版兼容模式 |
| 其他 | — | 未验证，仍可尝试，自检会提示 |

引擎分界线：**build ≥ 26100 必须写完整键组**（上限 + 三对起止时间 + 状态位），否则会被钳制回 35 天。

---

## 改了哪些注册表键

`HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings`

| 值 | 类型 | 写入 |
|---|---|---|
| `FlightSettingsMaxPauseDays` | DWORD | 36500（默认 35） |
| `PauseUpdatesStartTime` | String | 当前 UTC，`yyyy-MM-ddTHH:mm:ssZ` |
| `PauseUpdatesExpiryTime` | String | 到期 UTC |
| `PauseFeatureUpdatesStartTime` / `EndTime` | String | 同上 |
| `PauseQualityUpdatesStartTime` / `EndTime` | String | 同上 |

`HKLM\SOFTWARE\Microsoft\WindowsUpdate\UpdatePolicy\Settings`

| 值 | 类型 | 写入 |
|---|---|---|
| `PausedFeatureStatus` / `PausedQualityStatus` | DWORD | 1 |
| `PausedFeatureDate` / `PausedQualityDate` | String | 当前本地时间，`yyyy-MM-dd HH:mm:ss` |

注意两处的日期格式不一样：UX 下是 ISO8601 带 `T` 和 `Z`（UTC），UpdatePolicy 下是空格分隔的本地时间。写错会被忽略。

**不动的东西**：`wuauserv`、`UsoSvc`、`WaaSMedicSvc` 服务状态，不加 `NoAutoUpdate` 组策略，不删任何文件。

这句话不是自我声明，CI 每次都会验：`test_hklm.py` 在真实写入前后各读一遍 `wuauserv` / `UsoSvc` / `WaaSMedicSvc` / `BITS` / `DoSvc` 的启动类型，断言**没有任何服务被设成「禁用」**（`Start=4`）。

为什么不是「启动类型一字不变」：BITS 本来就会被系统自己调整（实测在一次 CI 里它从手动 3 变成了自动 2，那是 Windows 的正常活动，不是本工具干的）。要防的是「为了阻止更新把服务禁掉」，所以判据是 `Start=4`，而不是值是否变化。两个值都会打印出来，便于人工对照。

---

## 数据目录

备份和日志放在 **程序所在目录** 下（`backup\`）。打包版是 exe 所在目录，源码运行时是 `main.py` 所在目录。

这一点特意处理过：PyInstaller 单文件模式运行时会把自身解压到 `%TEMP%` 下的临时目录，此时 `__file__` 指向那里。如果把备份写在那儿，程序一退出就被系统清理，**等于根本没备份**。所以程序启动时：

1. 打包模式下用 `sys.executable` 所在目录，而不是 `__file__` 所在目录
2. 该目录不可写（例如放在 Program Files）时，退到 `%LOCALAPPDATA%\WindowsUpdatePauseTool\backup`

界面底部会显示当前实际使用的备份目录，可以随时核对。

---

## 回滚

三条路：

1. 主界面「备份 / 回滚」→ 选一份 → 回滚（回滚前会自动再备份当前状态，可撤销）
2. 直接双击 `backup\` 里对应时间戳的 `.reg`
3. 主界面「恢复更新」——清空暂停状态，等价于取消暂停

备份在 `v2\backup\`，成对生成：`snapshot-*.txt`（人可读全量快照）+ `ux-settings-*.reg` + `updatepolicy-settings-*.reg`。

**备份会自报完整性。** `snapshot()` 会检查每个 `.reg` 是否真的生成且非空，结果写进快照头部：

```
完整性 : 完整（两个 .reg 均已生成）
```

如果 `reg.exe` 导出失败，头部会变成「不完整，见下方警告」，并明确写出**无法双击还原**、改用什么办法。以前这里忽略了 `reg.exe` 的返回值，导出失败也照样报「备份成功」，文本里还教你去双击一个不存在的文件。

顺带说明：`.reg` 缺失不影响写入安全。写入失败时的自动回滚用的是内存快照，不依赖这些文件。

**这条链路是真跑过的**，不是写在文档里的承诺：`test_e2e.py` 的 T10 会真的 `reg export` → 清空 → `reg import`，然后比对到期时间是否逐秒一致；CI 上还会在真实 HKLM 上再跑一遍。

---

## 已知限制

**① 恢复后会补齐错过的更新。** 暂停期间不下载，恢复后一次性拉回来，可能几个 GB。挑个有空的时候。

**② 大版本升级后要重设。** 功能更新（如 26H2 → 27H2）会重置 `UX\Settings` 的部分值。升完打开工具看一眼，自检会告警，点一键修复即可。

**③ `WaaSMedicSvc` 可能把暂停改回去。** 自检会提示它是否在运行。发现暂停自己失效时先看这项。

**④ 自检第 12 项（自动扫描任务）在非管理员下是「跳过」。** `System32\Tasks` 目录普通用户不可读，这是权限限制，不是工具缺陷。

**⑤ CI 的真实写入验证有边界。** 它能证明「注册表写入 / 读回 / 恢复」这条链路正确，但证明不了「更新引擎真的因此停止下载更新了」。后者依赖系统内部行为，只能实际用一段时间观察。工具的做法是把暂停状态写正确、并在自检里持续校验它没被改回去。

---

## 开发自检

```powershell
# 核心逻辑层（只读，不碰系统设置）—— 含故障注入
python core.py --selftest

# 界面编译检查
python -m py_compile core.py main.py
```

`core.py --selftest` 跑三组：

### ① 基础断言（22 项）

时间解析 5 种格式、往返无损、系统识别、暂停状态读取、14 项健康检查、备份生成与列表。

### ② 故障注入（7 项）—— 证明自检本身有效

在 `HKCU\Software\_WuPauseFaultTest` 伪造「系统更新把暂停改坏」的现场，验证自检能不能准确认出来。**绝不拿真实 HKLM 做实验**，跑完自动删测试树。

| 场景 | 伪造的故障 | 期望自检报出 |
|---|---|---|
| S1 | 只写一半时间值 | 暂停时间值完整 → 失败 |
| S2 | 上限被重置回 35 天 | 暂停上限充足 → 失败 |
| S3 | 到期时间格式被改坏 | 时间格式合法 → 失败 |
| S4 | 状态位被清零、时间还在 | 状态位与时间匹配 → 警告 |
| S5 | 暂停已过期 | 暂停未过期 → 警告 |
| S6 | 完全正确的写入 | 六项全绿（**防误报**） |
| S7 | 上限不足后点一键修复 | 到期时间零漂移、上限补齐 |

S6 和 S7 是防伪的两道卡：前者防止「什么都报错」，后者防止「修完了却把到期时间改了」。

### ③ 端到端闭环（`test_e2e.py`）

```powershell
python test_e2e.py
```

`--selftest` 只验「读」和「修复」，写路径的完整闭环要靠这个。同样在 HKCU 沙箱跑，不碰真实系统：

| 项 | 验证内容 |
|---|---|
| T1 | 写入后 8 个值逐项校验：上限、两个状态位、起止时间、约 40 年后到期、三组时间一致、剩余天数 |
| T2 | 清除后不留残值，健康检查正确转为「未设置暂停」 |
| T3 | 上限被系统改小后点修复，到期时间零漂移、上限补齐、模式为 preserve |
| T4 | 同一秒内两次备份不互相覆盖 |
| T4b | 备份列表按修改时间倒序（不是文件名字典序） |
| T5 | 测试模式下备份不导出真实 HKLM |
| T6 | 回滚控制流（测试模式跳过实际导入） |
| T7 | 参数校验与异常路径：年数越界拒绝、到期时间早于当前拒绝、起始晚于到期自动回退 |
| T8 | **写入中途失败自动回滚**：第 6 个值写入时抛异常 → 到期时间与上限回到操作前（逐秒一致）、仍处于暂停状态、健康检查全绿；回滚本身失败时消息明确告知哪些没成功 |
| T9 | **数据目录降级**：程序目录不可写时退到 `%LOCALAPPDATA%`，且降级目录能真的写备份（测试把该环境变量指向临时目录，不碰真实的用户目录） |
| T10 | **备份能真的救回来**：`reg export` → 清空 → `reg import` → 到期时间逐秒回到原值、上限与暂停状态一并恢复。路径故意含中文，顺带验证 `reg.exe` 不挑路径 |
| T11 | **导出失败不谎报**：`.reg` 导出失败时 `reg_ok=False`、快照标注「不完整」、不再引导用户双击一个不存在的文件、给出可用替代办法 |

T8 是这一套里最该在意的：11 个注册表值是一个整体，少写一个是「半暂停」——启用标记在、到期时间缺。这种状态比完全没写更糟，因为用户看到「操作失败」会以为什么都没变。所以写入包了一层「先快照、失败就整体退回」，回滚用内存快照而不是 `.reg` 文件（不受外部进程和文件占用影响）。

全部用例都会断言「退出码 0」，可直接接进任何 CI。

### ④ 单实例保护（`test_single_instance.py`）

```powershell
python test_single_instance.py
```

**必须跨进程测**：同一进程里第二次 `CreateMutexW` 同名互斥体，`get_last_error` 仍然是 `ERROR_ALREADY_EXISTS`，测出来是假象，覆盖不到「另一个进程先跑」这个真实场景。所以用子进程持锁、父进程抢。

| 项 | 验证内容 |
|---|---|
| T1 | 持锁子进程能拿到，第三个进程被拦 |
| T2 | 持锁者退出后本进程能接管（**防句柄泄漏**） |
| T3 | 接管后新进程仍被拦（锁没丢） |
| T4 | 互斥体名不含版本号（跨版本互拦）、带 `Local\` 前缀（按登录会话隔离） |
| T5 | 内核态异常时放行——单实例是体验优化，不能变成新的失败点 |

### ⑤ 真实 HKLM 往返（CI 专属）

`test_hklm.py` 在真实注册表上做一次完整往返：**写入 10 年 → 逐项校验 → 恢复原状 → 校验恢复**。

**本地默认不跑。** 双重闸门：必须同时满足「设了 `WU_PAUSE_ALLOW_HKLM_TEST=1`」和「进程是管理员」，否则直接 SKIP 退出 0。所以误跑也不会动到自己的机器。

GitHub Actions 的 `windows-latest` runner 带管理员权限，所以**每次 push 都会真跑一遍**，结果写在 Actions 页面的 Step Summary 里。

这一步证明的是：键组写得对、系统读得回、能干净还原。

### ⑥ GUI 冒烟（offscreen）

```powershell
pip install PySide6 PySide6-Fluent-Widgets
python test_gui.py
```

不需要真实显示器——用 Qt 的 offscreen 平台跑。覆盖：窗口构造、状态刷新、备份对话框、最小尺寸 860×680、浅色/深色主题切换、应用图标、线程级异常。

CI 里这是**独立的一个 job，只装 README 写的那两条依赖**。目的是排除「本地装了多余包所以能跑」的假阳性——验证用户照着文档做真的能起界面。

---

## 高 DPI 说明

界面在 100% / 125% / 150% / 200% 缩放下都验证过布局（自检列表 14 行对齐、按钮不被挤压、无控件溢出）。

底部状态栏只显示末两级目录（如 `v2\backup`），完整路径和日志位置在鼠标悬停的提示里——完整绝对路径在高缩放或长目录名下会顶到窗口右缘。

---

## 操作日志

每次写入前备份、写入、失败，都会在 `logs\operation.log` 追加一行：

```
2026-11-17 20:12:33 | 暂停 40 年 | 暂停至 2066-11-17 20:12:33（14610 天），备份已存 snapshot-20261117-201233.txt
```

日志目录建不出来（比如放在只读盘）时静默禁用，不影响主功能。

---

## 文件说明

| 文件 | 作用 |
|---|---|
| `main.py` | 界面 |
| `core.py` | 注册表读写、版本识别、健康检查、备份。无 GUI 依赖，可单独测 |
| `single_instance.py` | 单实例保护（纯 Win32 命名互斥体，不依赖 Qt） |
| `test_e2e.py` | 端到端闭环测试（HKCU 沙箱，不碰系统） |
| `test_gui.py` | GUI 冒烟测试（offscreen，无需显示器） |
| `test_hklm.py` | 真实注册表往返测试（仅 CI 或显式授权时执行） |
| `test_single_instance.py` | 单实例的跨进程验证 |
| `app.ico` | 图标 |
| `WindowsUpdatePause.exe` | 打包产物（不入库，按上方命令自行生成） |
| `backup\` | 备份（运行时生成） |
| `logs\operation.log` | 操作流水（运行时生成） |
