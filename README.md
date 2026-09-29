# 广州工程技术职业学院 网上选课抢课脚本（gzvtc-course-grab）

> 🏫 教务系统 `stmcrp.gzvtc.edu.cn`

纯 Python 标准库实现，**零第三方依赖**，需要 **Python 3.11+**。命令行运行，自动登录、自动穿透限流、按关键字组池循环抢课。

## ⚠️ 特色

- 🎨 **100% vibe coding** —— 代码、注释与文档全部由 AI 对话生成，无架构设计与需求评审
- 🙈 **0% 人工审查** —— 无 code review、无 CI、无系统性测试；`selfTest.py` / `smokeTestAll.py` 为同一轮对话中生成的离线断言，覆盖面有限，通过不代表逻辑正确
- 🧊 **零依赖** —— 仅使用标准库（`urllib` / `threading` / `tomllib`），无需安装第三方包
- 🔨 **32 路并发重试** —— 应对 "code":"6666" 随机限流，列表与闸门的获取耗时由串行 ~67 秒降至 1 秒内
- 🎯 **多关键字各抢一门** —— 各关键字轮转交错尝试，类型达成后停止
- 🌙 **轮询等待与自动补登录** —— 非选课时段保持轮询，检测到开放后自动继续
- ⚠️ **不保证正确 / 稳定 / 安全** —— 边界输入、并发时序、凭据处理均未验证
- 🚨 **会操作真实系统** —— 可能出现误选或账号受限；使用前请阅读源码、先执行 `--dry-run`，并确认符合学校相关规定

> 本项目定位为可读、可改、可运行的学习用草稿，请勿视为可直接信赖的成品软件。

---

## 1. 目录结构

```
gzvtc-course-grab/
├── main.py             # 主脚本（抢课）
├── config.example.toml # 配置模板（复制成 config.toml 再改）
├── selfTest.py         # 离线自检：响应码映射 + 选课池逻辑，不联网
├── smokeTestAll.py     # 离线冒烟：假客户端跑完整循环，验证退出条件，不联网
├── LICENSE             # GPL-3.0 许可证全文
└── .gitignore          # 忽略 config.toml / cookies.json / .venv / debug/ / __pycache__
```

本地产物（均已被 `.gitignore` 忽略）：

```
config.toml            # 本地配置，由模板复制而来
cookies.json           # 会话缓存，首次登录后自动生成
debug/list.json        # --dry-run 导出的课程列表，便于离线核对
.venv/                 # 可选的本地虚拟环境（脚本零依赖，非必需）
```

---

## 2. 快速开始

```powershell
git clone https://github.com/diwan233/gzvtc-course-grab.git
cd gzvtc-course-grab

# 1) 复制配置模板，填写 account.studyNumber / account.password
Copy-Item config.example.toml config.toml
python selfTest.py            # 2) 离线自检（不联网，验证逻辑）
python smokeTestAll.py        # 3) 离线冒烟（不联网，跑完整循环）
python main.py --dry-run      # 4) 试运行：仅输出选课池，不发选课请求（列表另存 debug/list.json）
python main.py                # 5) 正式抢课
```

正式运行前建议先执行一遍 `--dry-run`，确认能拉到真实课程列表、池子符合预期。

常用参数：

| 参数 | 说明 |
|---|---|
| `--dry-run` | 不选课，仅输出选课池（安全预演）；拉到的列表另存到 `debug/list.json` |
| `-t 32` | 临时覆盖并发路数 |
| `-k 网球` | 临时覆盖关键字（可多次） |
| `--relogin` | 忽略 cookies.json，强制重新登录 |
| `-c my.toml` | 指定配置文件 |
| `-v` | 每轮重试都打印日志（排查用） |

---

## 3. 配置文件（config.toml）

配置文件不纳入版本库：仓库中仅提供模板 `config.example.toml`，首次使用需先复制一份：

```powershell
Copy-Item config.example.toml config.toml
```

这样账号凭据不会被误提交，升级代码时 `git pull` 也不会与本地配置冲突。
如需使用其它文件名，可通过 `python main.py -c 文件名.toml` 指定。

配置用 **TOML**（Python 3.11+ 标准库 `tomllib` 解析），逐项说明直接写在 `config.toml` 的 `#` 注释里，下面只做速查。

「默认」列 = 配置文件未写该项时脚本内置的值（定义在 `main.py` 的 `_fill_defaults()`）。

| 段 | 键 | 默认 | 说明 |
|---|---|---|---|
| `account` | `studyNumber` / `password` | — | 登录凭据，**必须自行填写**；留空则脚本直接报错退出 |
| `target` | `keywords` | `[]` | 课程关键字（**可多个**）。**每个关键字匹配到的课全部入池**；多关键字时按关键字轮转交错尝试 |
| | `belonging` | `""` | 课程归属筛选，空=全部 |
| | `semester` | `""` | 学期字符串，**固定填写**，换学期手动改 |
| | `onlyWithVacancy` | `false` | false=所有匹配的课都进池（有空位的排前，满员排最后）；true=只留有空位的 |
| `session` | `cookieFile` | `cookies.json` | 会话缓存文件 |
| | `reuseCookies` | `true` | 优先复用缓存会话，跳过登录 |
| | `cookieMaxAgeMinutes` | `0` | 0=不按时间过期，交给服务端判定（失效自动重登） |
| `retry` | `busyGapMs` | `80` | 两轮并发之间的间隔 |
| | `listIntervalMs` | `1500` | 池内无可抢的班时，隔多久再试（毫秒） |
| | `refreshList` | `true` | 是否重新拉列表刷新余量；`false` = 只在启动时拉一次 |
| | `refreshListEveryCycles` | `1` | 每多少轮池循环才重新拉一次列表（1=每轮；3=第 3/6/9… 轮；5=第 5/10/15… 轮） |
| | `timeoutSeconds` | `6` | 单请求超时；服务端高峰期常挂起，调小可提高吞吐 |
| | `maxRunMinutes` | `0` | 0=不限时 |
| | `closedWaitSeconds` | `30` | 非选课时段的探测间隔（秒） |
| `concurrency` | `threads` | `8` | **并发路数**：同一请求同时发几路 |
| | `maxTotalAttempts` | `2000` | 单个请求的总尝试上限（所有线路合计） |
| `behavior` | `requireAllKeywords` | `true` | **退出条件**：true=每个关键字（类型）都抢到一门才退出；false=抢到任意一门即退出 |
| | `waitWhenClosed` | `true` | 非选课时轮询等待开放；false=直接结束 |
| | `treatSelectedAsSuccess` | `true` | 服务端回 `selected`（你已选过此门课）时，是否也算该类型已达成 |
| | `dryRun` / `verbose` / `forceRelogin` | `false` | 同命令行开关 |

### 模板与内置默认的差异

`config.example.toml` 是一套可直接使用的配置，其中有 4 个键刻意取了与内置默认不同的值：

| 键 | 内置默认 | 模板取值 | 理由 |
|---|---|---|---|
| `concurrency.threads` | `8` | `32` | 服务端不限单用户并发，32 路吞吐更高 |
| `concurrency.maxTotalAttempts` | `2000` | `3200` | 与 32 路匹配（3200 / 32 = 100 轮） |
| `retry.closedWaitSeconds` | `30` | `10` | 非选课时段探测更及时 |
| `retry.refreshListEveryCycles` | `1` | `5` | 列表接口同样受 6666 拦截，池子较大时无需每轮刷新 |

`account` 段与 `target.keywords` 属每人各不相同的取值，模板留空或给出示例，需自行填写。

---

## 4. 工作流程（固定）

```
1. 会话      cookies.json 有缓存 → 直接复用，跳过登录
             无缓存/超龄/失效 → 直接 POST 登录表单换 JSESSIONID，并写回 cookies.json
2. 等开放    GET 选课页判断当前能否选课（依据服务端标记，见 §5-9）
             非选课时段 → 轮询等待，开放后自动继续；--dry-run 则不等待
3. 拉列表    直接 GET /student/wx/courseOptionCheck/list
             被欢迎页挡住时 → 才按需放行 GET /wx/go，然后重取（见 §5-4）
4. 建池      多关键字匹配 → 所有命中**任一**关键字的课都进池
             排序: 先按关键字轮转交错(尤克里里/瑜伽/尤克里里/瑜伽…)，
                   同一关键字内部再按空位多的排前、满员的排最后
5. 池循环    池内逐个 check，失败立即跳转下一个
             一轮走完 → 按刷新节奏决定是否重拉列表（见 refreshListEveryCycles）
             **重拉成功 → 清空「已废弃」记录**，上轮被剔除的班重新入池评估
             → 重建池 → 再来
6. 结束      **每个类型都抢到一门** → 完成退出（`requireAllKeywords=true`）
             ok → 该类型达成；selected(可配置) → 该类型视为达成；学分上限 → 停止
```

每轮日志格式固定（不分「详细轮 / 简略轮」）：

```
  [i] 刷新课程列表(每 5 轮一次)          ← 只在该轮的刷新节奏命中时才有
  [OK] 列表已刷新: 68 门                 ← 刷新失败则是 [!] 刷新失败(...), 沿用上一份 N 门
  [i] 清空废弃记录(2 个班重新入池评估)     ← 仅当确实有废弃记录时
===== 第 2 轮池循环 | 池内 1 门 (列表 68 门, 已废弃 0 个) =====
    关键字匹配: 人工智能=1/1 门
    入池分布(按关键字轮转交错): 人工智能×1
    目标进度 0/1: 人工智能⏳
  [1/1][人工智能] -> 人数已满, 保留在池中等下一轮, 下一个
--- 第 2 轮走完: 人数已满×1 | 下次刷新在第 5 轮 ---
```

- **所有列表请求都会记录**：启动时是 `[OK] 列表直接可用, 无需 /go 闸门`；后续刷新为上述三行
- `已废弃 N 个` = 本轮被排除的班（时间冲突 / 已停开 / 已开课）；重拉列表后归零
- 轮末行汇总本轮结果，并预告下次重拉列表的轮次；非刷新轮同样输出
- `verbose=true` 仅影响更细粒度的「6666 重试」日志，不影响上述格式

---

## 5. 关键问题（均为实测结论，改代码前请先读）

### 5-1. `{"code":"6666"}` 是**随机性限流**，必须重试
实测连发 8 次仅 1 次通过，**不可当作"选课失败"而放弃**。
脚本将其与超时/网络错误一并判为"非业务响应"，立即开始下一轮并发重试。

### 5-2. 列表接口会返回**降级响应**，同样必须继续重试
服务端高峰期会返回两种"HTTP 成功但无数据"的响应（HTTP 200、不含 6666）：

1. 空壳：`{"pageNum":1,"pageSize":0,"size":0,"total":0,"list":[]}`
2. 占位行：`list` 里只有 1 条 `courseName` 为 `null` 的旧数据（学期还是 `2020-2021学年第一学期`）

**实测页面自身的请求也会得到这两种响应**（页面课程列表随之从 86 条变为 0 条）。
脚本以 `list_ready()` 为判据，将其一律视为"尚未加载完成"，继续重试。

### 5-3. 登录 POST **不能跟随重定向**
登录成功会 302 到 `/welcome`，而 `/welcome` 本身常被 6666 拦。
若自动跟随，会将"登录成功"误判为"被限流"，导致反复重复提交登录。
脚本使用不跟随重定向的 opener，仅检查 302 的 `Location`。

### 5-4. "欢迎页闸门" `/go` 是**按需**的（时有时无，既不可无条件跳转，也不应每次空跑）

闸门**并非每次都需要**，取决于服务端当时的会话/账号状态。2026-09-28 实测三组对照：

| 时刻 | 会话 | 直接打 `list` 的结果 |
|---|---|---|
| 13:45 | 全新登录 | ❌ 欢迎页 HTML（`Content-Type: text/html`，含`进入学生门户`/`已注册`/`欢迎`）|
| 14:13 | 全新登录 | ✅ 直接 JSON，106 条课程 |
| 14:14 | 全新登录（`--relogin` 复现） | ✅ 直接 JSON，106 条课程 |

因此既不可假定"必须过 /go"，也不可假定"可以跳过"——同一账号两种情形均出现过。

脚本的解法是**按需放行**（实现于 `load_courses()`）：

1. 先直接请求 `list` → 得到 JSON 则直接使用（此情形下**不发送任何 /go 请求**）
2. 得到的是欢迎页 HTML（由 `_is_gate_page()` 识别）→ 才放行 `/go`，然后重新获取
3. 放行成功后由 `save_cookies()` 写入磁盘 → 下次可直接跳过

两种服务端行为均已覆盖，且都不会产生多余请求。实测：真实启动到取得 106 条列表耗时 **9~23 秒**；
走闸门时 32 次尝试约 15 秒，高峰期实测需 128 次、约 194 秒。

> 复现方式：`python main.py --dry-run --relogin`（全新登录）与 `python main.py --dry-run`（复用缓存会话），
> 在**选课开放时段**观察日志中是否出现「列表被欢迎页挡住 → 放行 /go 闸门后重取」。

### 5-5. 登录成功是 **302 且响应体常为空**（曾导致脚本"一直登不上"）
判定"是否为有效响应"时**不能只看响应文本是否非空**：
`bool("") == False`，会将成功的 302 误判为"未取得响应"而无限重试。
脚本 `_is_business(text, code)` 将 **3xx 一律视为合法业务响应**。
排查方法：对失败日志做**分类计数**（`6666×31, 网络错误×1, 空响应×2`），
笼统的"全被限流 / 失败"会掩盖真实原因。

### 5-6. 会话 Cookie 是 HttpOnly
浏览器 JS 无法读取（`document.cookie` 为空），因此纯脚本方案须自行提交登录表单，
或从浏览器导入已有的该 Cookie。

### 5-7. urllib 的 CookieJar/opener 不是线程安全的
每线程一套 opener，Cookie 统一存于共享 dict 并加锁读写。

### 5-8. 并发实测效果
串行时单次失败的往返可能阻塞 6 秒；改为 32 路并发后，**列表/闸门耗时从 ~67 秒降至 1 秒内**。

### 5-9. 非选课时段：选课页会被整页换成一张「当前时间不可选课」提示页

**该判断由服务端渲染决定，而非前端 JS 计算**——实测非选课时段
`GET /student/wx/courseOptionCheck` 返回一张 **~1.2KB 的极简页**：

```html
<div class="msg">
  <p><i class="iconfont icon-info-circle"></i></p>
  <p class="mcrp-font-title-color">当前时间不可选课</p>
</div>
```

该页面不含 Vue 与列表脚本（无 `pulldownRefresh` / `courseList`），
可选课时返回的才是完整选课页。因此 `page_state()` 仅凭这两个标记即可准确分类。

**同时，列表接口也会随之“塌缩”**：14:29 仍返回 106 门，14:31 起稳定只返回 1 门。
因此“池内 0 门”不等于“关键字写错了”——应先确认是否处于非选课时段。

脚本的处理方式（默认开启）：

1. 拉列表前先 `GET` 选课页判断状态；**非选课时段既不空转也不报错退出**，而是每
   `closedWaitSeconds` 秒重新探测一次，探测到开放后自动继续抢课
2. 池循环中若发现池为空，也会顺带探测一次——若选课窗口关闭，同样保持等待
3. 探测结果带缓存（`closedWaitSeconds` 内不重复探测），不会因每轮空池而增加请求
4. `--dry-run` **不等待**：仅快速查看池子，阻塞无意义
5. **长时等待会自动补登录**：长时间等待后 JSESSIONID 会过期，探测到登录页即自动重新登录
6. 关闭该行为：`behavior.waitWhenClosed = false`

实测日志：

```
[=] 当前时间不可选课 —— 30 秒后再探测 (Ctrl+C 退出)
...
[OK] 选课已开放(等了 12 分 30 秒), 开始抢课
```

---

## 6. 接口契约（实测抓取）

| 接口 | 方法 | 说明 |
|---|---|---|
| `/student/wx/login` | POST | 表单字段 `studyNumber` / `password`；成功 302，`Location` 含 `welcome` 或 `index` |
| `/student/wx/go` | GET | 放行欢迎页闸门（**按需**，被欢迎页挡住时才调用） |
| `/student/wx/courseOptionCheck/list` | GET | 参数 `pageNum` / `pageSize` / `currentSemester` / `belonging`；返回 PageHelper JSON |
| `/student/wx/courseOptionCheck/check?id=<id>` | GET | **选课**，返回纯文本 |
| `/student/wx/courseOptionCheck/noCheck?id=<id>` | GET | 退课 |

请求头（从浏览器录制，脚本已原样模拟）：

```http
Accept: application/json, text/javascript, */*; q=0.01
X-Requested-With: XMLHttpRequest
Referer: https://stmcrp.gzvtc.edu.cn/student/wx/courseOptionCheck
```

列表 JSON 关键字段：`id`(教学班id) / `courseName` / `teachingClassName` / `studentCount`("16/17") /
`courseMemo`(时间地点) / `courseScore` / `teacherNames` / `belongingId`。

### 选课响应码 → 处置（`VERDICT`）

| 返回 | 处置 | 含义 |
|---|---|---|
| `ok` | **success** | 选课成功 |
| `full` | retry | 人数已满，保留在池中继续重试 |
| `fail` | retry | 满员或已选过，保留在池中继续重试 |
| `selected` | selected | 你已选过此门课 |
| `busy` | drop | 时间冲突，剔除出池 |
| `stop` | drop | 该课已停开，剔除出池 |
| `start` | drop | 该课已开课，剔除出池 |
| `morescore` | fatal | 本学期学分超上限，整体停止 |
| `moreallscore` | fatal | 三年学分超上限，整体停止 |
| `timeout`（脚本哨兵） | retry | 本轮未取得任何响应 |
| 其他/未知码 | retry | 未知响应一律保留在池中重试，不丢弃 |

---

## 7. 选课完成后的处理

- **成功（`ok`）**：该**类型达成** → `confirm_selected()` 复核人数（如 `16/17 → 17/17`）
  → 打印退课接口地址 → 若仍有其它类型未抢到则**继续抢**，全部达成后退出码 0。
- **`selected`（已选过）**：
  - `treatSelectedAsSuccess=true`（默认）→ 该类型视为达成，继续抢剩下的类型；
    全部达成后退出码 0，并提示这是服务端判定“该教学班已在你名下”，**并非本次新抢到**。
  - 改为 false 后 → 剔除该班并继续抢其它课程。
- **`full` / `fail` / `timeout` / 未知码**：保留在池中，立即跳转下一个，下一轮再来。
- **`busy` / `stop` / `start`**：永久剔除出池（重试无意义）。
- **学分上限**：整体停止（退出码 3）。
- **中途结束/到时限**：打印每个类型的达成情况（`[✅] 尤克里里 …` / `[❌] 微生物 (未抢到)`）。
- 退出码：`0` 成功/正常结束、`1` 会话或列表失败、`2` 配置错误、`3` 学分上限、`130` 手动中断。

---

## 8. 多关键字（抢多门课）

`target.keywords` 支持任意多个关键字，例如：

```toml
keywords = ["尤克里里", "瑜伽", "微生物"]
```

行为：

- **每个关键字单独匹配**（按 `courseName` 子串包含），匹配到的课**一门不落全部入池**
- 满员的班**不剔除**，只排到该关键字队列的最后（因其也可能在中途有人退课）
  - 如需彻底排除满员班，将 `onlyWithVacancy` 设为 `true`
- 池内顺序 = **按关键字轮转交错**，保证各目标课的抢课机会均等：
  `尤克里里A → 瑜伽A → 微生物A → 尤克里里B → 瑜伽B → 微生物B → …`
- 每轮日志会打印 **“关键字匹配: 尤克里里=5/5 门, 瑜伽=3/3 门, 微生物=2/2 门”**，
  可直接看到每个关键字匹配到几门、入池几门，以及入池后的轮转分布
- 每次尝试都带 `[关键字]` 标记，便于分辨正在抢哪门课程
- **每个类型只抢一门**，成功即停
- 临时覆盖（不改配置）：`python main.py -k 尤克里里 -k 瑜伽 -k 人工智能`

### 退出条件（重要）

默认 `behavior.requireAllKeywords = true`：**每个关键字各抢到一门才退出**。

- 某个关键字抢到后，其所有班级均不再重复尝试，池中只保留尚未达成的关键字
- 每轮会打印进度：`目标进度 2/3: 尤克里里✅  瑜伽✅  微生物⏳`
- 全部达成后打印每个类型最终抢到的班级，退出码 0
- 若某个关键字在列表里一门都没匹配到，会明确告警 `⚠️ 关键字『X』本轮列表里没匹配到任何班级`
- 如需恢复“抢到任意一门即停”，将 `requireAllKeywords` 设为 `false`
- 配合 `retry.maxRunMinutes` 可设最长运行时间（0=不限时，一直抢到全部达成为止）

---

## 9. 已知注意事项

- **32 路并发对 `check` 意味着同一教学班同时发 32 个选课请求**。服务端会去重；
  需降低并发时，调小 `concurrency.threads`。
- `pageSize=200` 为脚本使用的值；服务端对分页参数的回显与请求不一致
  （有时回显 `pageSize:0`），降级判定即针对此现象设计。
- 换学期须修改 `target.semester`（学期字符串由配置固定提供）。
- `config.toml` 与 `cookies.json` 含账号/会话凭据，分发代码时不应包含这两个文件。

---

## 10. 许可证

**GNU General Public License v3.0**（`GPL-3.0-only`）© 2026 diwan233，全文见 [LICENSE](LICENSE)。

强著佐权（copyleft）：可以自由使用、修改、再分发，亦可用于商业用途，但**衍生作品必须同样以 GPL-3.0
开源**——不得闭源、不得换用其它许可证、不得附加额外限制。分发时须附上完整源码，并保留版权
声明与许可证全文。

本程序不含任何担保，作者不承担任何责任。
