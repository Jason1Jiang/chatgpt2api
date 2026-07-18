# Gemini Web 生图反代 MVP 开发规格与实施手册

## 文档状态

- 状态：MVP 范围已确认，待后续开发。
- 目标上游：`gemini.google.com` 消费者网页端，不是 Google AI Studio API，也不是 Antigravity / Cloud Code 额度。
- 实现边界：本文档只固定后续开发的产品与技术契约，不表示功能已实现。

## 0. 如何使用本文档

本文档是这项功能的开发事实源。接手的新会话或 subagent 必须先完整阅读本文档和仓库根目录的 `AGENTS.md`，再检查真实代码和 Git 状态，不得只依赖聊天历史、README 摘要或模型记忆。

优先级从高到低：

1. 用户在当前会话中的最新明确指令。
2. 仓库根目录 `AGENTS.md`。
3. 本文档中已确认的 MVP 契约。
4. 实现者根据实际代码作出的最小技术选择。

若代码现状与本文档中的“预计修改位置”不同，应保持产品契约不变，并按实际代码选择最小改动落点。任何需要扩大产品范围、改变公开接口、触碰 Gemini 额度逻辑或写入 GitHub 的决定，都必须先向用户确认。

### 一句话任务

在隔离分支中，让软件 A 继续调用 ChatGPT2API 现有的 OpenAI 兼容图片接口；当模型为 `gemini-web-image` 时，后台使用已导入的 Google 网页登录态访问 `gemini.google.com`，完成文生图或图生图并把最终图片返回软件 A。

### 不可偏离的硬约束

- 文生图和图生图都属于 MVP，不能把图生图推迟到后续版本。
- Google 账号出现在现有账户列表，不新建账号管理页面。
- 调用方继续使用现有图片接口，不新增 Gemini 专用公开端点。
- 只实现 `gemini.google.com` 消费者网页链路。
- 完全不查询、不计算、不存储、不显示、不排序 Gemini 剩余额度或恢复时间。
- 所有功能在新分支和独立 worktree 中实现，不污染当前工作树。
- subagent 严格串行工作，任何时刻最多一个 subagent 处于活动状态。

## 1. 目标

让其他软件继续使用 ChatGPT2API 已有的 OpenAI 兼容图片接口，由后台自动把请求转发到 Gemini 网页端，再将网页生成的图片转换为现有响应格式返回。

用户期望的完整链路：

```text
导入 Google 网页账号
        ↓
账号显示在现有号池管理页
        ↓
软件 A 调用 /v1/images/generations 或 /v1/images/edits
        ↓
ChatGPT2API 选择可用的 Gemini Web 账号
        ↓
后台调用 Gemini 网页端完成文生图或图生图
        ↓
下载生成结果并转成当前 OpenAI 兼容格式
        ↓
图片返回软件 A
```

## 2. 已确认决策

1. Gemini Web 文生图与图生图都是 MVP 必须项。
2. Google 账号直接显示在现有账户列表，不新建独立的 Google 账号管理页。
3. 软件 A 继续使用现有 `/v1/images/generations` 和 `/v1/images/edits` 接口。
4. 使用稳定的公开模型别名 `gemini-web-image`，不让调用方依赖 Gemini 网页内部模型 ID。
5. Google 账号通过 Gemini 网页 Cookie 登录态导入；不使用 Google OAuth Access Token 代替网页会话。
6. 完全不实现 Gemini 剩余额度查询、显示、聚合、排序或恢复时间。
7. Gemini 网页协议、Cookie 维护、文件上传与结果解析全部收敛在一个后台模块内，不泄漏到对外接口或前端。

## 3. MVP 范围

### 3.1 账号导入与管理

在现有“导入账户”对话框增加“Gemini 网页账号”入口，MVP 支持：

- 粘贴 Cookie JSON。
- 上传 Cookie JSON 文件。
- 兼容常见的 Cookie 字典格式和浏览器扩展导出的 Cookie 数组格式。
- 导入时立即验证登录态，并尝试读取 Google 账号邮箱。

账户列表中的 Gemini Web 账号显示：

| 现有列 | Gemini Web 显示内容 |
| --- | --- |
| TOKEN | 显示脱敏的 `Gemini Session` 标识，不显示、复制真实 Cookie |
| 类型 | `Gemini Web` |
| 来源 | `cookie_json` 或后续可选的 `browser_cookie` |
| 状态 | 正常、异常、禁用 |
| 账号信息 | Google 邮箱；读取不到时显示稳定的脱敏会话标识 |
| 创建时间 | 导入时间 |
| 在途 / 成功 / 失败 | 复用现有请求统计 |
| 操作 | 编辑、验证登录态、禁用、删除 |

现有顶部“剩余额度”卡片、账户列表额度列、恢复时间列、额度聚合与额度刷新全部不属于本 MVP 范围。不为 Gemini Web 增加任何额度字段、展示规则、统计规则或验收要求，现有页面保持原样。

Gemini Web 账户记录不提供额度或恢复时间字段。如果现有表格为了保持列结构必须渲染对应单元格，只允许显示空值或现有中性占位符；不得为此发起查询、制造虚假数值、参与顶部聚合，也不把该单元格内容作为验收条件。

### 3.2 文生图

对外接口保持不变：

```http
POST /v1/images/generations
Authorization: Bearer <ChatGPT2API 用户密钥>
Content-Type: application/json
```

MVP 请求示例：

```json
{
  "model": "gemini-web-image",
  "prompt": "生成一只在雨夜街头的橘猫",
  "n": 1,
  "response_format": "b64_json"
}
```

必须支持：

- 有效的文本提示词。
- `n=1`。
- `response_format=b64_json`。
- `response_format=url`，使用当前项目的图片存储与访问 URL，不把 Gemini 临时图片 URL 直接暴露给调用方。
- 返回结构与当前 `/v1/images/generations` 保持一致。
- `gemini-web-image` 出现在现有 `/v1/models` 返回中，便于依赖模型发现的 OpenAI 兼容客户端选择它。

MVP 不保证：

- `n > 1`。
- 流式生图进度。
- 任意像素尺寸、固定分辨率或质量等级。

`size` 和 `quality` 可以保留在请求兼容面，但在未验证 Gemini 网页端有稳定控制字段前，不得宣称具有精确控制能力。

### 3.3 图生图

对外接口保持不变：

```http
POST /v1/images/edits
Authorization: Bearer <ChatGPT2API 用户密钥>
```

MVP 必须支持当前输入形式：

- `multipart/form-data` 图片上传。
- JSON 中的图片 URL 或 Data URL。
- 单张参考图。
- 多张参考图。
- 图片编辑提示词。
- `response_format=b64_json` 和 `response_format=url`。

后台处理链路：

1. 复用现有请求解析逻辑读取图片文件、URL 或 Data URL。
2. 选择一个可用的 Gemini Web 账号。
3. 将参考图上传到 Gemini 网页请求所需的上传链路。
4. 把上传结果与编辑提示词组装为 Gemini 网页生成请求。
5. 只收集 Gemini 实际生成的图片，不把用户输入附件或网页搜索图片当作生成结果。
6. 获取原尺寸图片，交给现有结果格式化与图片存储逻辑。

`mask` 保持现有请求兼容，但是否能在 Gemini 网页端实现精确局部重绘不属于 MVP 验收条件。

## 4. 明确不做的内容

- 不查询 Gemini 剩余生图次数或 Usage Limits。
- 不在顶部统计卡片中加入 Gemini 额度。
- 不展示 Gemini 额度恢复时间。
- 不根据额度对 Google 账号排序。
- 不实现额度保护或额度预警。
- 不使用 Antigravity / Cloud Code 额度或 Google AI Studio API 代替 Gemini 网页端。
- 不新建独立的 Google 账号管理页。
- 不为调用方新增必填的供应方参数。
- 不在 MVP 中保证流式进度、`n > 1` 或精确分辨率。

## 5. 模块与 seam

新增一个深模块 `GeminiWebBackend`，让调用方只需知道少量接口：

```python
class GeminiWebBackend:
    def validate_account(self, account: dict) -> dict: ...
    def generate(self, request: ImageRequest) -> ImageResult: ...
    def edit(self, request: ImageRequest) -> ImageResult: ...
```

模块内部隐藏：

- Cookie 导入、校验、更新与会话复用。
- Gemini 页面初始化和临时 XSRF / 会话参数获取。
- 图片上传。
- Gemini 网页生成请求的组装。
- 流式响应解析。
- 生成图片和普通网页图片的区分。
- 原尺寸图片 URL 获取与下载。
- 网页协议变动的错误归类。

调用 seam：

```text
OpenAI 兼容图片请求
        ↓
按 model 选择图片 Adapter
        ├─ gpt-image-*       → 现有 ChatGPT 实现
        ├─ codex-gpt-image-* → 现有 Codex 实现
        └─ gemini-web-image  → GeminiWebBackend
        ↓
统一 ImageOutput / 图片存储 / OpenAI 兼容响应
```

Gemini 适配不得把 Cookie 字段、网页内部 payload 或解析索引扩散到 `api/ai.py`、前端页面或现有 ChatGPT 实现中。

### 5.1 深模块职责

`GeminiWebBackend` 是一个深模块：调用方只学习账号验证、文生图和图生图三个行为；网页协议的复杂性全部留在实现内部。删除该模块后，这些复杂性应该重新散落到多个调用方，否则说明模块仍然只是浅层转发。

推荐由构造函数注入账号池、HTTP 传输、时钟和结果下载依赖，让正式实现和测试替身通过同一个 seam。对外不要公开 XSRF token、RPC ID、请求数组索引或临时图片 URL。

测试应主要通过 `GeminiWebBackend` 的接口断言可观察结果和稳定错误，不直接断言内部请求数组的每个位置。Gemini 网页协议 fixture 与正式 HTTP 实现可以构成内部 seam 上的两个 Adapter：

```text
GeminiWebBackend
        ↓ internal seam
Gemini Web transport
        ├─ 正式 HTTP Adapter：真实 Cookie、上传、生成、原图下载
        └─ Fixture Adapter：脱敏响应、超时、限流、协议变化测试
```

### 5.2 规范化输入与输出

Backend 内部使用统一图片请求和结果，不让 Gemini 特有字段进入路由层。最少应表达：

- 请求：`prompt`、零张或多张参考图、`response_format`，以及兼容但不承诺精确生效的 `size` / `quality`。
- 结果：一个或多个已下载图片的二进制内容、MIME 类型和可选修订提示词。
- 错误：账号不可用、当前请求受限、内容策略拒绝、协议解析失败和总时长超时。

账号选择、换号、槽位获取与释放可以由账号池模块完成，但必须由图片调用链统一编排，不能让路由层自行操作 Cookie 或拼装 Gemini 请求。

## 6. 账号数据契约

Gemini Web 账号需要稳定的账号标识，不能使用会轮换的 Cookie 作为账户列表主键。

建议的最小持久化结构：

```json
{
  "account_id": "gemini_web:<stable-fingerprint>",
  "provider": "gemini_web",
  "type": "Gemini Web",
  "source_type": "cookie_json",
  "email": "user@gmail.com",
  "status": "正常",
  "credentials": {
    "cookies": "<encrypted-or-protected-cookie-bundle>"
  },
  "created_at": "2026-07-17 12:00:00",
  "success": 0,
  "fail": 0
}
```

兼容规则：

- 现有 ChatGPT 账号仍可以使用 `access_token` 作为识别值。
- 管理端选中、删除、禁用和刷新应优先使用 `account_id`，对旧数据回退到 `access_token`。
- Gemini Web Cookie 不得填入或伪装成 `access_token`。
- 现有“导出全部 Token”只导出 ChatGPT Token，不得将 Google Cookie 导出到 TXT。Gemini 凭据导出不属于 MVP。
- 管理接口和日志只返回脱敏会话标识，不返回 Cookie 内容。

## 7. 账号选择与运行时行为

- 只从 `provider=gemini_web` 且 `status=正常` 的账号中选择。
- MVP 采用简单轮询，不读取、比较或预测额度。
- 每个账号使用独立会话和并发槽位，不得在账号之间共享 Cookie 或临时会话参数。
- Cookie 失效时标记账号异常，释放槽位并尝试下一账号。
- 遇到 Gemini 网页请求限制时，只为当前请求换号重试；不查询、不持久化、不显示剩余额度。
- 当所有账号都无法完成请求时，返回统一上游错误。
- 生成请求必须有总时长上限，不能因连接保活或网页心跳无限等待。
- 无论成功、失败、换号或超时，都必须释放账号槽位。
- 一次调用在一轮换号中最多尝试每个候选账号一次，并记录本次已尝试的稳定 `account_id`，不得循环重新选中同一账号。
- 整个调用只使用一个基于单调时钟的绝对截止时间；账号验证、上传、生成、轮询、换号和原图下载共享剩余预算，换号不得重置计时。
- 调用取消、Cookie 异常、协议解析失败、上传失败、下载失败和超时都必须通过 `finally` 或等价结构释放已取得的槽位。
- 总时长的具体秒数优先复用项目现有图片超时配置；若需新增配置，应提供安全默认值，但不能把单个 HTTP 请求的 read timeout 当作整个任务截止时间。

## 8. 错误契约

| 场景 | 预期处理 |
| --- | --- |
| 调用方未通过 ChatGPT2API 密钥验证 | 保持现有 `401` |
| Cookie 失效 | 标记对应 Google 账号异常，换号；全部失效时返回 `503 no_available_account` |
| Gemini 当前请求受限 | 换号重试；全部受限时返回 `429 upstream_rate_limited` |
| 提示词被上游拒绝 | 返回 `400 content_policy_violation` |
| Gemini 网页协议变动或响应无法解析 | 返回 `502 upstream_protocol_error` |
| 总时长超时 | 返回 `504 upstream_timeout` |
| 图生图缺少输入图片 | 保持现有 `400 image is required` |

上游账号与 Cookie 错误不得误报为调用方 API Key 错误。

错误响应继续使用项目现有的 OpenAI 兼容错误 envelope；表中的稳定错误码放入现有错误 code 字段或项目已有的等价位置，不为 Gemini 单独发明另一套响应结构。

## 9. 安全约束

- Cookie 等同于 Google 网页登录凭据，不得出现在日志、错误信息、前端账号列表、图片历史或 Git 跟踪文件中。
- 前端的复制 Token 按钮对 Gemini Web 账号必须隐藏或禁用。
- 后端记录账号时只使用邮箱或脱敏指纹。
- 测试 fixture 必须使用虚假 Cookie，不得录制真实凭据。
- 导入界面必须说明这是未公开网页协议，可能因协议变动或风控失效，建议使用独立非主力账号。

## 10. Git 分支与 worktree 隔离

### 10.1 目标

- 开发分支：`codex/gemini-web-image-mvp`
- 建议 worktree：`E:\AIProject\ChatGPT2API-gemini-web-image-mvp`
- 原始 worktree：`E:\AIProject\ChatGPT2API`

开始前必须记录并检查：

```powershell
git status --short --branch
git branch --show-current
git rev-parse HEAD
git remote -v
git worktree list --porcelain
```

主 agent 应先核验本地 `main`、`origin/main` 和当前 HEAD 的关系，明确记录最终选用的基线提交，然后从该基线创建新分支和独立 worktree。不得为了本功能未经授权拉取、合并或变基本地 `upstream`。若目标分支或路径已经存在，先检查其来源、HEAD 和工作区状态，不得强制删除、重置或覆盖。

### 10.2 原工作树保护

本文档编写时原工作树已有未提交内容，包括规划文档/README 链接，以及用户自己的 `config.json` 和本机启动脚本。实际状态可能变化，因此实现者必须以开始开发时的 `git status` 为准。

- 不得对原工作树执行 `reset`、`restore`、`clean`、`stash` 或覆盖式 checkout。
- 不得把用户的 `config.json`、凭据、本机启动脚本或其他无关改动带入功能分支。
- 如果本文档尚未提交，新分支应从原路径读取本文档，并只把本文档及对应 README 链接重新纳入隔离 worktree。
- 后续所有代码、测试和文档修改都只发生在隔离 worktree。
- 未经用户后续明确授权，不提交、不推送、不创建 PR，也不进行任何 GitHub 写操作。
- 如果用户之后要求推送，只允许写入 `origin` 的 `Jason1Jiang/chatgpt2api`；`upstream` 永远只读。

## 11. 串行 subagent 实施协议

主 agent 负责建立 worktree、拆分阶段、验收结果和最终集成。阶段 2–4 出现边界清晰、可独立验收的实现任务时，主 agent 应主动调用 subagent；若某阶段不适合委派，应在阶段报告中简要说明原因。所有 subagent 必须严格串行：

1. 任意时刻只能有一个活动 subagent。
2. subagent 不得再创建子 agent，也不得并行安排研究或编辑任务。
3. 每个 subagent 只负责一个明确、可验证的阶段，并在同一个隔离 worktree 中工作。
4. 主 agent 必须等待当前 subagent 完成，检查其改动、运行相应测试并处理问题。
5. 只有当前阶段验收通过后，才能启动下一个 subagent。
6. 下一阶段从已验收的工作树状态继续，不重复实现上一阶段，也不擅自扩大范围。
7. 发现需要用户登录、验证码、2FA、真实账号或产品取舍时，必须停在真实人工闸门，不能假装验证完成。

每个 subagent 的回报至少包含：

- 修改了哪些文件和接口。
- 实现了哪些验收项。
- 运行了哪些测试及结果。
- 是否发现协议风险、安全风险或后续阻塞。
- 是否触碰了明确不做的范围；正常答案应为“否”。

## 12. 串行实现阶段

### 阶段 0：主 agent 建立隔离环境

- 检查 `AGENTS.md`、本文档、Git 状态、remotes 和 worktrees。
- 创建或复用经过核验的 `codex/gemini-web-image-mvp` 分支及独立 worktree。
- 在隔离 worktree 中纳入本文档和 README 入口。
- 运行当前基线测试，记录已有失败，避免把旧问题误认作本功能回归。

阶段通过标准：原工作树未被改动，隔离 worktree 的基线和已知测试状态清楚。

### 阶段 1：Gemini 网页最小合同验证

- 使用一个独立测试账号获取脱敏网页请求和响应样本。
- 验证 Cookie 初始化、文生图、参考图上传、图生图和原图下载。
- 记录可被自动测试的脱敏协议 fixture。
- 不把真实 Cookie、完整请求头、账号标识或生成历史写入仓库、日志或 subagent 回报。
- 如果需要登录，用户只在隔离的 Chrome 配置中自行完成登录、验证码和 2FA；agent 不索取密码，也不要求在聊天中粘贴 Cookie。
- 用户完成登录并明确授权后，agent 可以只从该隔离 Chrome 配置读取本次验证所需 Cookie；另一种允许方式是由用户把 Cookie 导出到仓库外的临时 JSON 文件，并只把本地文件路径交给 agent。
- 仓库外临时 Cookie 文件不得被复制到仓库、fixture、日志、测试输出或 subagent 回报中；验证结束后由用户决定是否删除该文件，agent 不擅自删除用户凭据。

阶段通过标准：同一账号能在独立脚本中完成一次文生图和一次图生图。

这是硬闸门。没有真实成功证据时可以继续编写 fixture 驱动的模块和测试，但不得宣称 Gemini Web 链路已验收，最终报告必须明确标记为待人工验证。

### 阶段 2：账号导入与列表集成

- 增加 Gemini Cookie JSON 解析和登录态验证。
- 增加 `provider` 与 `account_id` 数据契约。
- 让 Google 账号显示在现有账户列表。
- 完成禁用、删除和手动验证登录态。
- 将 Gemini Web 账号排除在 Token TXT 导出之外。

### 阶段 3：文生图闭环

- 实现 `GeminiWebBackend.generate()`。
- 增加 `gemini-web-image` 模型路由。
- 接入现有图片结果格式化和存储。
- 完成超时、换号、槽位释放和错误映射。

### 阶段 4：图生图闭环

- 实现 `GeminiWebBackend.edit()`。
- 复用现有图片输入解析。
- 支持单图和多参考图上传。
- 只返回新生成图片，排除输入附件和网页搜索图片。

### 阶段 5：回归与真实验收

- 运行现有 ChatGPT 图片生成、图片编辑、账号管理和图片存储测试。
- 使用独立 Google 测试账号完成真实文生图和图生图。
- 使用软件 A 或通用 OpenAI 图片客户端完成端到端调用。

## 13. 预计修改位置

以下是开发时的主要落点，具体文件可在实现阶段按最小变更调整：

- `api/ai.py`：继续使用已有 Images 路由，不放入 Gemini 网页协议。
- `api/accounts.py`：增加 Gemini Web 账号导入、验证和管理入参。
- `services/account_service.py`：支持 `provider` / `account_id` 以及 Gemini 账号轮询和槽位释放。
- `services/protocol/conversation.py`：在图片调用 seam 按模型选择现有实现或 Gemini Adapter。
- `services/protocol/openai_v1_image_generations.py`：保持文生图兼容契约。
- `services/protocol/openai_v1_image_edit.py`：保持图生图兼容契约。
- `services/gemini_web_backend.py`：新增，承载 Gemini 网页协议实现。
- `services/gemini_web_account_service.py`：可选新增，承载 Cookie 解析、保存、验证和会话维护。
- `utils/helper.py`：登记 `gemini-web-image` 公开模型别名。
- `web/src/app/accounts/components/account-import-dialog.tsx`：增加 Gemini Cookie JSON 导入。
- `web/src/app/accounts/page.tsx`：使用通用账号 ID，显示 Gemini Web 账号并隐藏凭据复制。
- `test/`：增加 Gemini 协议 fixture、账号管理、文生图、图生图、换号与超时回归测试。

该文件清单是导航，不是必须逐项修改的任务表。实现者应先读真实调用链；能通过更少文件满足契约时应保持改动收敛，不能为了匹配清单而创建无用抽象。

## 14. 测试与验收标准

### 自动化测试

- Cookie 字典和浏览器 Cookie 数组格式都能导入。
- 缺少必需 Cookie 时导入失败，且错误中不包含凭据。
- `/api/accounts` 返回 Gemini Web 账号但不返回 Cookie。
- 账户列表能搜索、禁用、验证和删除 Gemini Web 账号。
- `/v1/images/generations` 在 `model=gemini-web-image` 时返回有效图片。
- `/v1/images/edits` 能用单张和多张参考图返回新图片。
- `b64_json` 与 URL 返回都符合当前格式。
- Cookie 失效后换号，全部失效时返回稳定错误。
- 上游请求受限时能换号，不执行额度查询。
- 超时、解析失败和任务取消后账号槽位都能释放。
- 现有 ChatGPT 文生图、图生图和账号管理测试全部保持通过。

### 真实验收

1. 导入一个独立 Google 测试账号，账号出现在现有号池列表。
2. 调用 `/v1/images/generations`，确认请求实际由 Gemini 网页端完成并返回可打开的图片。
3. 调用 `/v1/images/edits` 上传参考图，确认返回的是新生成结果，不是输入图片。
4. 使用通用 OpenAI 图片客户端或实际软件 A 重复上述两个请求。
5. 使 Cookie 失效或禁用账号，确认请求不会死循环，且账号列表不泄漏凭据。

### 完成定义

只有同时满足以下条件，才能报告 MVP 已完成：

- 账号导入、现有列表展示、禁用、验证和删除形成闭环，且任何响应/日志都不泄漏 Cookie。
- 文生图和图生图都通过自动化测试；真实 Gemini Web 验收若受人工登录阻塞，必须明确写为未完成。
- `b64_json` 和项目托管 URL 两种结果都可被调用方读取。
- 换号、限流、Cookie 失效、协议错误、取消和总时长超时都能稳定结束并释放槽位。
- 现有 ChatGPT 图片与账号功能回归测试保持通过。
- 没有实现或修改任何 Gemini 剩余额度逻辑；现有额度 UI 不作为 Gemini 验收内容。
- `git diff --check` 通过，敏感信息检查无命中，原工作树中的用户改动未被污染。
- 最终报告列出实际修改、测试证据、真实验收证据、已知限制和未完成事项，不用推测替代证据。

## 15. 明确的人工闸门

以下情况必须暂停并请用户操作或决策：

- Google 登录、验证码、2FA、设备确认或风控验证。
- 需要选择或提供独立非主力 Google 测试账号。
- Gemini 网页协议已经变化，现有脱敏样本不足以确定新请求结构。
- 需要读取隔离浏览器登录态、使用仓库外临时 Cookie 文件、录制网络数据或把真实凭据写入持久化配置；必须先说明读取范围和保留方式并取得用户明确授权。
- 需要改变公开接口、账户 UI 结构、MVP 范围或错误契约。
- 需要 commit、push、创建 PR 或执行其他 GitHub 写操作。

人工闸门之前应先完成所有不依赖用户的安全工作，并清楚说明用户只需要做什么、完成后如何继续验证。

## 16. 交接给新 agent 的启动方式

新会话只需要收到仓库路径和下面这条指令：

> 在 `E:\AIProject\ChatGPT2API` 中完整阅读 `AGENTS.md` 与 `docs/gemini-web-image-proxy-mvp.md`，严格按文档的隔离分支、串行 subagent 阶段和人工闸门实施。先核对真实 Git/代码状态，再从阶段 0 开始；不要修改 Gemini 额度逻辑，不要提交或推送，除非我另行明确授权。

新 agent 仍必须把本文档当作开发事实源，不能把这句短指令当作完整规格。

## 17. 后续非 MVP 候选项

以下内容只能在 MVP 闭环通过后单独评估，不得在 MVP 开发中自动扩展：

- 从本机 Chrome 配置自动读取 Gemini Cookie。
- `n > 1` 的串行或并行生成。
- 流式进度事件。
- 精确宽高比、尺寸或质量控制。
- 精确 mask 局部重绘。
- Gemini 网页对话续编和历史会话管理。
- Google 账号凭据导出。
- 任何 Gemini 剩余额度相关功能。
