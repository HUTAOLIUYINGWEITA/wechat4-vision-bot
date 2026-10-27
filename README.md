# wechat4-vision-bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D4?logo=windows)
![WeChat](https://img.shields.io/badge/WeChat-4.x-07C160?logo=wechat)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python)
![No UI Automation](https://img.shields.io/badge/automation-pixel%20%2B%20OCR-8957e5)

**微信 4.x 的纯视觉自动回复机器人 —— 像素 + OCR 驱动，不依赖 UIAutomation 控件树。**

> **English abstract** — A WeChat **4.x (Windows)** auto-reply agent built purely on **vision**:
> `PrintWindow`/`ImageGrab` screenshots, **Tesseract OCR** (`chi_sim`), controlled clicks and clipboard
> paste, plus an OpenAI-compatible LLM endpoint for reply generation. Unlike `wxauto` / `pywinauto`
> (which target WeChat 3.x), it does **not** depend on WeChat's UI control tree — WeChat 4.x is a Qt
> rewrite, so control-tree automation breaks. Safety first: whitelists, rate limits, text dedupe,
> circuit breaker, foreground/window checks, and a **disabled-by-default** remote-command path.
> MIT licensed. **No credentials, chat logs or screenshots are included in this repository.**

---

## 📑 目录

- [这是什么](#这是什么)
- [适用条件](#适用条件)
- [为什么不直接用 wxauto / pywinauto](#为什么不直接用-wxauto--pywinauto)
- [架构（文字版）](#架构文字版)
- [仓库结构](#仓库结构)
- [安装与配置](#安装与配置)
- [开关与急停](#开关与急停)
- [安全设计（本项目最值钱的部分）](#安全设计本项目最值钱的部分)
- [🔴 安全警告：远程指令默认关闭](#-安全警告远程指令默认关闭)
- [已知限制](#已知限制)
- [自测](#自测)
- [整理与来源说明](#整理与来源说明)
- [免责声明](#免责声明)
- [License](#license)

---

## 这是什么

一句话：**在你的 Windows 电脑上盯着微信 4.x，看到白名单会话来了新消息，就用 LLM 生成一条像真人随手回的回复，并用「像素 + OCR + 受控点击」的方式真发出去。**

它解决的是一个很实际的问题：微信 4.x 把界面换成了 Qt 重写，老一代基于控件树（UIAutomation）的
自动化库（`wxauto`、`pywinauto` 的那套思路）在 4.x 上直接失效。本项目干脆绕开控件树：

- **看得见**：`PrintWindow` 抓微信窗口（被遮挡也能抓，不抢焦点），检测未读红点 + 会话列表差分；
- **认得清**：Tesseract OCR（`chi_sim`）识别会话行、输入框、标题；
- **发得出**：受控点击会话行 + 剪贴板/`SendInput` 输入 + 三重校验 + `Enter`；
- **拦得住**：白名单、限流、去重、连续失败熔断、急停（三处入口）、注入前前台校验。

> 项目里的角色名「小耳朵」是整理自一个个人自用项目的机器人称呼/触发词，保留是为了兼容旧配置；
> 你可以通过 `config.json → remote.triggerWord` 和 `persona.txt` 换成任意名字。

## 适用条件

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11（全部键鼠注入逻辑依赖 Win32 API，不支持 macOS/Linux） |
| 微信 | **微信 4.x Windows 版**（实测窗口类名 `Qt51514QWindowIcon`，进程名 `Weixin.exe`；微信 3.x 不适用） |
| 缩放 | **DPI 缩放实测 125%**。代码用 `GetDpiForWindow` 动态换算，不写死像素；但**不同缩放/字号需要重新标定**（见[已知限制](#已知限制)） |
| OCR | **Tesseract 5 + `chi_sim` 中文简体语言包**（外部依赖，不是 pip 包） |
| Python | 3.10+（实测 3.13），依赖见 `requirements.txt` |
| 模型 | 任何兼容 `POST {baseUrl}/chat/completions` 的 OpenAI 风格接口（默认示例用 DeepSeek） |

## 为什么不直接用 wxauto / pywinauto

1. **微信 4.x 是 Qt 重写**：窗口自绘，控件树（`UIAutomation` / `pywinauto` 依赖的 MSAA/UIA）里拿不到
   「会话列表 / 输入框 / 发送按钮」这些有意义的控件节点，老库的定位方式整体失效。
2. **wxauto 那批库明确针对微信 3.x**，4.x 上连窗口都定位不对，更别说输入和发送。
3. **控件树方案在 4.x 上要追着版本改**：每次微信更新布局/换 Qt 版本都可能全崩；
   本项目走「像素 + OCR + 受控点击」，改版时最多重新标定几个阈值。
4. **安全可控**：本项目每一步都有前台校验、窗口存活校验、白名单、限流、去重、熔断；
   拿不到控件树反而是好事——它只能"看见并点一个坐标"，每一步都可以被日志审计。

## 架构（文字版）

```
                        ┌──────────────────────────────────────────────────────┐
                        │                    watcher.py 主循环                  │
                        │            每 intervalSeconds 秒一轮；先看 STOP        │
                        └──────────────────────┬───────────────────────────────┘
                                               │
        ┌──────────────────────────────────────▼───────────────────────────────────────┐
        │ ① 检测层（纯本地像素，0 token）                                                │
        │   PrintWindow 抓微信窗口（被遮挡也能抓，不抢焦点）                              │
        │   ├─ 会话列表「未读红点」检测（badge 面积/颜色）                                │
        │   ├─ 会话列表文本区稳定基线差分（新会话置顶 / 时间戳变化，settleScans 稳定判定） │
        │   └─ 任务栏层兜底：PrintWindow 抓任务栏 → 定绿色微信图标 → 图标角标红点          │
        │   命中 → 存截图 + 写 signal.json + 记判据原因                                   │
        └──────────────────────────────────────┬───────────────────────────────────────┘
                                               │ 新未读（白名单命中才继续）
        ┌──────────────────────────────────────▼───────────────────────────────────────┐
        │ ② 生成层（genreply.py，进程内直连，不唤醒外部 Agent）                          │
        │   .credentials.json（provider / baseUrl / model / apiKey）                     │
        │   + persona.txt（人设）+ 硬性安全规则                                          │
        │   → POST {baseUrl}/chat/completions（max_tokens <= 120，temperature=0.8）      │
        │   → 清洗回复 + 禁用词拦截（转账/密码/验证码/链接/二维码/密钥/发文件…）           │
        └──────────────────────────────────────┬───────────────────────────────────────┘
                                               │ 候选回复
        ┌──────────────────────────────────────▼───────────────────────────────────────┐
        │ ③ 护栏层（sendguard.py）                                                      │
        │   白名单外绝不发 / 同会话 30s 只发 1 条 / 同会话连续 5 条暂停 30 分钟            │
        │   / 同一句话 600s 内不重复 / 每条成功发送落 logs/sent-YYYYMMDD.jsonl             │
        └──────────────────────────────────────┬───────────────────────────────────────┘
                                               │ 允许发送
        ┌──────────────────────────────────────▼───────────────────────────────────────┐
        │ ④ 执行层（reply.py / sendfile.py）                                            │
        │   AttachThreadInput 切前台（失败才用 ALT 兜底，且必须释放 ALT）                 │
        │   → Ctrl+F → 输入会话名                                                       │
        │   → 整屏截图（Qt 下拉是 popup，PrintWindow 抓不到！）                           │
        │   → OCR 下拉面板，按分组/图标判定「真正的会话行」（跳过 💥搜索网络结果 / 命令行）│
        │   → 受控单击该行 → 校验聊天标题（exact / 近似唯一；群聊带 (N) 时校验类型）      │
        │   → 清空输入框 → 长文本走剪贴板粘贴 / 短文本走 SendInput                        │
        │   → 三重校验（字符数比例 + 去重字符覆盖 + 序列相似度 + 重复标点）               │
        │   → 发送前复检（前台不是游戏、用户没动键鼠、标题没变）→ Enter                   │
        │   → 前后截图 + 像素差校验 → 写 reply_state.json / logs/sent-*.jsonl             │
        └──────────────────────────────────────────────────────────────────────────────┘

        横切模块：
        · busyguard.py  忙闲守护：前台进程是游戏 → 硬拦截（只按进程名，窗口标题绝不参与）
        · breaker      连续发送失败 3 次 → 写 STOP + 熔断记录 + 进程退出（不自启）
        · panic        三处急停：桌面快捷方式 / 全局热键 Ctrl+Alt+Shift+S / 微信发「小耳朵 急停」
        · toggle.ps1   总开关：on = 起进程 + autoSend；off = 杀进程；panic = 立即急停
```

## 仓库结构

```
wechat4-vision-bot/
├── watcher.py               # 主循环：检测 + 生成 + 护栏 + 调 reply.py 发送 + 熔断/急停/远程指令状态机
├── reply.py                 # 执行层：打开会话、输入、三重校验、Enter、截图与发送记录
├── sendfile.py              # 执行层扩展：把文件通过剪贴板（CF_HDROP）发到指定会话
├── genreply.py              # 生成层：读 .credentials.json + persona.txt，进程内直连 chat/completions
├── sendguard.py             # 护栏：白名单/限流/连续条数/同句去重/发送记录落盘
├── busyguard.py             # 忙闲守护：前台是游戏进程就绝不注入键鼠
├── toggle.ps1               # 总开关：on / off / stop / panic / start / status
├── toggle-ui.ps1            # 交互式开关面板（数字菜单）
├── xiaoxiao-ear-toggle.cmd  # 双击入口：用 powershell 跑 toggle-ui.ps1
├── selftest_rows.py         # 离线回归：会话行选择精度（构造用例；真实截图语料可选）
├── selftest_stage1.py       # 离线自测：输入校验/面板判别/熔断 mock/三处急停/py_compile
├── config.example.json      # 配置模板（复制为 config.json 再改；不含任何密码/真实群名）
├── persona.example.txt      # 人设模板（复制为 persona.txt 再改）
├── requirements.txt         # pip 依赖（Tesseract 是外部依赖，不在 pip 里）
├── LICENSE                  # MIT
├── docs/
│   ├── 架构.md              # 模块职责、数据流、状态文件、日志字段
│   └── 踩坑记录.md          # 真实踩过的坑（项目最值钱的部分）
└── tools/
    └── setup_password.py    # 交互式生成远程指令两级密码的 salt + sha256
```

## 安装与配置

### 1) Python 依赖

```powershell
cd wechat4-vision-bot
python -m pip install -r requirements.txt
```

### 2) Tesseract OCR + 中文包（外部依赖）

- Windows 安装包：<https://github.com/UB-Mannheim/tesseract/wiki>
- 安装时**必须勾选 Chinese (Simplified) 语言包**，或手动把 `chi_sim.traineddata` 放进
  `<Tesseract安装目录>\tessdata\`。
- 程序会自动在这些位置找 tesseract：
  1. `config.json → ocr.tesseract`（显式路径）
  2. 环境变量 `TESSERACT_BIN`
  3. `%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe`
  4. `%ProgramFiles%\Tesseract-OCR\tesseract.exe` / `%ProgramFiles(x86)%\...`
  5. `PATH` 里的 `tesseract`

装完验证一下（应能打印版本并识别出「微信」两个汉字）：

```powershell
tesseract --list-langs          # 输出里必须有 chi_sim
```

### 3) 配置文件

```powershell
copy config.example.json config.json
```

`config.json` 已被 `.gitignore` 忽略，**绝不要提交**。关键字段：

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 总开关。`false` 时 watcher 完全不动（连截图都不做） |
| `autoSend` | `false` | `false` 只拟稿不发送；`true` 才真的发 |
| `whitelist` | `["文件传输助手"]` | **只允许自动回复的会话**（群名/联系人名，占位示例） |
| `quietHours` | `[]` | 静默时段（如 `[23, 8]` 表示 23 点~次日 8 点不动作） |
| `guard.*` | 见上 | 30 秒间隔 / 连续 5 条暂停 / 同句 600s 去重等，护栏核心 |
| `busyGuard.gameProcesses` | 坦克世界/cs2 等 | 前台是这些进程时**绝不注入键鼠** |
| `detect.windowClass` | `Qt51514QWindowIcon` | 微信主窗口类名；微信升级后变了就改这里 |
| `remote.enabled` | `false` | 🔴 远程指令（微信消息 → 操作电脑），默认关闭，见下方警告 |

### 4) 人设

```powershell
copy persona.example.txt persona.txt
```

`persona.txt` 同样被 gitignore。genreply.py 会在这份人设后面**再追加一段硬性安全规则**
（禁止谈转账/密码/链接/密钥等），所以人设里只写「怎么说话」即可。

### 5) 模型凭据 `.credentials.json`

在仓库根目录新建 `.credentials.json`（**已被 gitignore，禁止提交**）：

```json
{
  "provider": "deepseek-official",
  "baseUrl": "https://api.deepseek.com",
  "model": "deepseek-chat",
  "apiKey": "sk-你的key"
}
```

任何兼容 `POST {baseUrl}/chat/completions` 的服务都可以（`Authorization: Bearer <key>`）。
`apiKey` 在日志和异常里都会被 `mask_key()` 打成 `sk-***`。

### 6) 先跑离线自测（不碰屏幕、不发消息）

```powershell
python selftest_stage1.py        # 输入校验/面板判别/熔断 mock/急停/py_compile，应全 PASS
python selftest_rows.py          # 会话行选择精度；没有 screenshots/ 时只跑构造用例
python watcher.py --status       # 看配置/窗口/OCR/凭据是否就绪
python watcher.py --guard-selftest   # 发送护栏自检
```

### 7) 第一次真机试跑（务必看日志）

```powershell
# 只拟稿不发送：dry-run 验证「定位会话 + 清空输入框 + 输入内容 + 三重校验」
python reply.py --dry-run --chat "文件传输助手" --text "【测试】dry-run 请忽略"
```

确认无误后，从最小的开关开始：

```powershell
.\toggle.ps1 start     # 后台启动（但 config 里 enabled=false 时只是空转）
.\toggle.ps1 on        # 真上岗：enabled=true + autoSend=true + 删 STOP + 起进程
.\toggle.ps1 status    # 看进程/开关/STOP 是否一致（不要只看 enabled）
```

> 建议第一周把 `whitelist` 只放「文件传输助手」，先观察日志再放真实会话。

## 开关与急停

```powershell
.\toggle.ps1 on        # 真上岗：enabled=true + autoSend=true + 删 STOP + 后台起 watcher
.\toggle.ps1 off       # 真停车：enabled=false + autoSend=false + 杀掉 watcher 进程 + 清 lock
.\toggle.ps1 stop      # 只杀 watcher 进程（不改开关配置）
.\toggle.ps1 panic     # 急停：写 STOP + 立即杀进程 + 清 lock（不等下一轮循环）
.\toggle.ps1 start     # 只后台启动（与 on 共用启动逻辑）
.\toggle.ps1 status    # 状态：进程命令行校验 + 开关/进程一致性检查 + 最近信号/日志
```

双击 `xiaoxiao-ear-toggle.cmd` 可以打开交互式数字菜单（1 开 / 2 关 / 3 急停 / 4 状态 / 5 退出）。

**三处急停入口**（任意一处触发都会写 `STOP` 文件并让 watcher 立即退出；`STOP` 存在期间不会自启）：

1. **桌面快捷方式**（推荐自己建一个）：目标设为
   `powershell -NoProfile -ExecutionPolicy Bypass -File "<仓库路径>\toggle.ps1" panic`，
   取个显眼的名字（如「急停.lnk」）放桌面。按一下立刻停车。
2. **全局热键 `Ctrl + Alt + Shift + S`**：watcher 每轮用 `GetAsyncKeyState` 检测四键同按
   （注意：这是**检测**，不注册系统热键，所以只在 watcher 运行时有效）。
3. **微信口令**：白名单会话发来含触发词「小耳朵」+「急停/停止」的消息 → 立即写 STOP + 退出
   （不会回消息，也不会执行其它动作）。

恢复：先排查原因，再 `.\toggle.ps1 on`（`on` 会删 STOP；`start` 不会）。

## 安全设计（本项目最值钱的部分）

这个项目本质上是「一个能看见屏幕、能点鼠标、能敲键盘的程序」，所以安全设计比功能重要。

### 1) 白名单（第一道闸）

- 只有 `whitelist` 里逐字命中的会话才允许自动回复/发送；`blacklist`（默认含微信团队/公众号等）
  永远只记录不动作。
- 会话名匹配做了 OCR 容错：归一化后 exact 优先，OCR 读错个别字时走「覆盖率 + 相似度 + 唯一性」
  近似档；命中歧义一律 abort（宁可少回一条，绝不回错人）。
- 执行层还有一层**双重校验**：点开会话后 OCR 校验聊天标题 == 目标会话名；
  群聊标题带 `(N)` 而目标是私聊（或反之）直接 abort。

### 2) 限流与去重（防止刷屏/复读）

- 同一会话 **30 秒内只发 1 条**（`guard.perChatMinIntervalSeconds`）
- 同一会话**连续发满 5 条** → 暂停 30 分钟（`perChatMaxBurst` / `burstPauseSeconds`）
- 同一句话 **600 秒内不重复发**（`dedupeSameText` / `dedupeWindowSeconds`）
- 每条成功发送都落 `logs/sent-YYYYMMDD.jsonl`（时间/会话/回复原文/未读指纹），可审计、可回放
- 有重试上限（`maxRetryAttempts=3`）和重试间隔（`retryAfterSeconds=60`），失败不会无限重试

### 3) 熔断（连续失败 → 直接停车）

- 连续发送失败 **3 次**（`breaker.consecutiveFailures`）→ 写 `breaker_tripped.json` + 写 `STOP` +
  `os._exit` **结束自身进程**，**不会自启**；恢复必须人工排查后 `toggle.ps1 on`。
- 发送成功一次即清零（`resetOnSuccess`）。
- 用途：微信改版/窗口异常/OCR 失效时，让它停下来，而不是对着错误状态反复注入键鼠。

### 4) 注入前前台校验（防止抢用户的操作）

- **游戏前台硬拦截**：前台进程命中 `busyGuard.gameProcesses` → 直接 `skip`，不切窗口、不按键、不点鼠标。
  判定**只按进程名**，窗口标题绝不参与（否则「浏览器里看坦克世界视频」会被误判成打游戏）。
- 切前台用 `AttachThreadInput`（零键鼠注入）；失败才用 `ALT` 技巧兜底，
  且 `key_down(VK_MENU) → SetForegroundWindow → key_up(VK_MENU)` 外层 `try/finally` 双保险，
  日志明确写「本次使用 ALT=True/False；ALT 已释放=True」。
- 发送过程中持续复检：前台变成游戏 / 用户动了键鼠 / 光标被移动 → 立即 abort。
- 鼠标只点微信窗口矩形内，越界一律 abort；**绝不点「搜索网络结果/搜一搜」**。

### 5) Esc 规矩与窗口存活守护

- **Esc 只在「已确证搜索层打开」时才允许按**：微信 4.x 里 Esc 会落在主窗口上 →
  窗口被收进托盘（用户会以为"程序把微信关了"）。确认证据超过 8 秒即视为过期，一个 Esc 都不发。
- 任何 `Esc` / 点击之后，都校验微信主窗口仍存在（`IsWindow` + `IsWindowVisible` + 尺寸合理）：
  窗口丢了 → `[ALERT] wechat-window-lost` → abort，不连点、不继续注入。

### 6) 输入完整性（三重校验，防止半截话/乱码）

长文本走 `SendInput` 逐字注入会丢字/重字（实测出现 `：：`、`『『`、`（（`、`row--pproximate` 这类乱码），
所以：

- **文本长度 > 12 或含全角标点/引号/括号 → 一律走剪贴板 `Ctrl+V`**，短文本才 `SendInput`；
  两条路互相兜底重试一次，仍不过 → `type-verify-failed` abort，**绝不按 Enter**。
- 校验用三重判据：字符数比例 ∈ `[0.85, 1.25]`、去重字符集覆盖 ≥ 0.90、`SequenceMatcher` 相似度 ≥ 0.75，
  另加「相邻重复标点」检查（期望文本里没有 `：：`/`『『`/`((` 这类时，实际 OCR 出现就判失败）。
- 剪贴板默认**不还原**（`reply.restoreClipboard=false`，用户明确同意）：剪贴板里会残留最后发的一条文本，
  属预期行为；想还原可改 `true`。
- 发送完默认**保持微信在前台**（`reply.restoreForeground=false`），不主动把前台还给原窗口。

### 7) 动作边界

- 每次动作整体**硬超时 20 秒**：到点看门狗线程立即释放所有按键并中止。
- 不标已读、不删除消息、只发一条；`sendfile.py` 只读取源文件，绝不修改/移动/删除，
  并且对敏感文件名（credential/key/token/password/密码/令牌/`.pem`/`.pfx`）**一律拒绝发送**。
- 有 `STOP` 文件 → watcher 立即退出；每次循环都先检查。

## 🔴 安全警告：远程指令默认关闭

> **`config.json → remote.enabled` 默认是 `false`，公开仓库里两级密码的 `salt` / `sha256` 全是空字符串。
> 本项目不含任何密码。首次使用必须自己用 `tools/setup_password.py` 生成。**

远程指令功能是：白名单会话发来「触发词 + 密码」的消息 → watcher 把它转成一次
**对本地 Agent 会话的 prompt 投递**（需要你自备并配置一个本地 Agent 会话，见 `remote.targetSessionId` /
`remote.targetSessionTitle`；本项目默认不附带 Agent）。

> 实现说明：投递端目前适配的是本地 **DeepSeek Harness（DSH）** 的 Web API
> （`wake.baseUrl` 默认 `http://127.0.0.1:19387`，读取 `%USERPROFILE%\.dsh\.credentials.yaml`
> 的 browser-session 密钥本地铸造 Cookie，再调 `session/prompt`）。没有 DSH 环境时这部分用不了，
> **保持 `remote.enabled=false` 即可**；自动回复主链路不依赖它。也就是说：

> **开启之后 = 微信消息可以驱动这台电脑（经你配置的本地 Agent）。**
> 能收到你微信消息的人，只要拿到密码，就能指使这个 Agent 干活。

开启前请务必：

1. 用 `python tools/setup_password.py` 生成**强密码**（两级：L1 只读/低风险，L2 高危含执行命令/改文件）；
   建议 12 位以上、字母+数字+符号混合，且**与任何其它账号密码不同**。
2. **白名单只放你自己的可信会话**（比如「文件传输助手」和你自己的小号），绝不要放群聊。
3. 理解 L1/L2 的分级：L1 只允许发文件/截图/报状态/读文本；L2 才允许执行命令、改/删文件、安装、启动停止程序。
   L1 请求里出现高危动作词会被要求补 L2 密码。
4. 知道密码的人（包括看到你聊天记录的人）等于拿到了操作权；本项目只存 `sha256(salt+密码)`，
   **日志/审计/配置里都不会出现明文**，但**请不要把密码发在群里**。
5. 不想用就保持 `remote.enabled=false`——代码保留、功能关闭，这是默认状态。

两级密码的算法（与 `watcher.py` 一致，`tools/setup_password.py` 已按此实现）：

```
sha256_hex = sha256(bytes.fromhex(salt) + password.encode("utf-8")).hexdigest()
```

## 已知限制

1. **OCR 字噪**：下拉小字里的长中文名 tesseract 容易读错/读丢字（尤其 13 字以上的群名）。
   本项目用「2.0x 放大 + psm6/psm11 两遍 + 原始对照 + 候选行 psm7 精读 + 近似唯一档」尽量兜住，
   但 OCR 本质上不保证 100%；只要校验不过就 abort 不发，宁可漏回不可发错。
2. **依赖窗口不最小化**：发送动作要求微信主窗口可见（不能最小化/不能收进托盘）。
   任务栏层只能做「检测兜底」，真正发送前会把窗口切出来；窗口被隐藏时直接 `skip` 并记日志。
3. **电脑睡眠/锁屏即失效**：睡眠、锁屏（安全桌面）、远程桌面断开时，截图和键鼠注入都不可用。
4. **DPI / 字号变化要重新标定**：代码按 DPI 比例换算，但微信自身字体大小、系统缩放改了之后，
   OCR 阈值/行距/热区都可能偏，需要重新标定（`selftest_rows.py` 可复现）。
5. **手机端消息 ≠ 真实未读**：如果你电脑登录的微信和手机不是同一个账号，手机给文件传输助手/群发的消息
   不会同步到电脑端，检测层永远看不到未读。端到端真发请用同一账号的设备，或用
   `watcher.py --inject-unread` 测试钩子。
6. **只支持简体中文 OCR（chi_sim）**，繁体/日文/英文会话名需要换语言包并重新标定。
7. **微信大版本升级可能全崩**：窗口类名、搜索下拉结构、输入框布局一变，就需要改
   `detect.windowClass` / 重新标定；本项目不承诺跟进所有微信版本。
8. **同一条消息最多回一次**；生成失败/护栏拒绝/发送失败都不会无限重试（有上限和熔断）。
9. **只支持 Windows 上的微信 4.x PC 版**；企业微信、Mac 版、微信网页版都不在范围内。

## 自测

| 脚本 | 用途 | 是否碰屏幕 |
|---|---|---|
| `python selftest_stage1.py` | 输入校验 + 面板判别 + 熔断 mock + 三处急停 + py_compile | ❌ 不碰（纯离线） |
| `python selftest_rows.py` | 会话行选择精度回归（构造用例；真实截图语料可选） | ❌ 不碰 |
| `python watcher.py --status` | 配置/窗口/OCR/凭据就绪检查 | 只读窗口信息 |
| `python watcher.py --guard-selftest` | 发送护栏（限流/去重/连续条数）自测 | ❌ 不发消息 |
| `python watcher.py --remote-selftest` | 远程指令状态机离线验证（正确密码走环境变量 `REMOTE_SELFTEST_PWD1/2`） | ❌ 不发消息 |
| `python reply.py --dry-run --chat X --text Y` | 真机验证「定位会话 + 输入 + 三重校验」，**不按 Enter** | ✅ 会切微信窗口 |

> 公开仓库**不附带 `screenshots/`**（都是真实屏幕截图，含聊天隐私）。`selftest_stage1.py` 的 T2 组
> 在缺少截图时会自动 `[SKIP]`，不影响其余用例。想复现真实语料回归，自己截几张搜索下拉的图放 `screenshots/` 即可
>（文件名含 `dropdown` 会被 `selftest_rows.py` 当语料）。

## 整理与来源说明

本项目整理自一个个人自用项目（机器人叫「小耳朵」）。公开版做了这些清理：

- 删除所有运行期数据：`.credentials.json`、`config.json`（真身）、`persona.txt`（真身）、
  `logs/`、`screenshots/`、`tmp-ocr/`、各种 `*_state.json`、`STOP`、`*.bak-*` 备份、进度交接文档；
- **两级密码的 salt/sha256 全部清空**（源码/配置/注释里都没有任何密码或哈希），
  并提供 `tools/setup_password.py` 让使用者自己生成；
- 把源码里残留的本机绝对路径（Python 安装路径、项目路径、tesseract 路径）改成基于脚本目录的相对路径 /
  环境变量 / 自动探测；
- 把注释、示例、自测用例里的真实群名/昵称替换成占位名；
- 默认关闭所有会「动电脑」的开关（`enabled` / `autoSend` / `remote.enabled` 全为 false）。

## 免责声明

- 本项目仅供**个人学习与自用**。使用前请自行确认是否符合你所在地区法律法规以及
  **微信/腾讯的用户协议**；使用非官方自动化工具可能导致账号被限制或封禁，**风险自负**。
- 请只在**你自己的电脑、你自己的账号**上使用；不要用它骚扰他人、批量群发、发送垃圾信息或
  进行任何违法活动。
- 本项目按「现状」提供，不承诺可用性、不承诺适配所有微信版本，作者/贡献者不对使用本项目造成的
  任何直接或间接损失负责（详见 MIT License）。
- 开启远程指令功能（`remote.enabled=true`）意味着你把本机操作权部分暴露给能给你发微信的人，
  请自行评估风险并设置强口令。

## License

[MIT](LICENSE) © 2026 wechat4-vision-bot contributors

## Credits

- **主要设计与实现**：**DeepSeek Agent**（AI 编程助手）
  本项目从架构设计、全部代码编写、真机调试、到 `docs/踩坑记录.md` 的整理，均由 DeepSeek Agent 完成。
  包括但不限于：微信 4.x 搜索下拉的定位策略、OCR 放大合并、受控点击与标题校验、
  剪贴板发送与三重校验、注入前前台校验、Esc 规矩与窗口存活守护、熔断与三处急停。
- **需求、技术决策与实机环境**：**HUTAOLIUYINGWEITA**
  提出需求并逐条拍板技术路线（自动化边界、护栏强度、两级密码、远程指令取舍），
  提供测试机器，并在每一轮真机验证中给出关键反馈 —— 多个"看起来能用其实是坏的"问题
  都是在他的实测反馈下才被定位。

> 这是一次人机协作的实践：**AI 负责实现细节，人负责判断取舍**。
