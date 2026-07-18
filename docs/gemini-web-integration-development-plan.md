# Gemini Web 图片能力整合开发计划

## 文档状态

- 状态：已确认，正在按 GitHub Issues `#10`–`#20` 严格串行实施。
- 用户已于 2026-07-18 批准票据拆分，并授权每个工单使用一个独立 subagent，依次实施、验收和关闭；任一时刻只能处理当前 frontier，当前票未验收关闭前不得开始后续票。
- 该授权覆盖已批准工单各自明确列出的本地修改、提交、测试、构建、部署、owner GitHub 写入和真实验收步骤，但必须等对应工单进入 frontier 后才能执行，不能跨票提前操作或据此扩大范围。
- Google 登录、验证码、2FA、设备确认、指定仓库外 Cookie 文件等需要用户提供输入或现场操作的事项仍是人工闸门；未获得具体输入时不得伪造完成证据。
- 当前事实源优先级：用户最新明确指令、仓库根目录 `AGENTS.md`、本计划、Gemini Web MVP 与协议文档、真实代码和 Git 状态。

## 1. 目标

把隔离工作树中已经实现但尚未提交的 Gemini Web 图片能力安全整合进 ChatGPT2API，并让本机 InfiniteCanvas 继续通过现有 OpenAI 兼容接口使用该能力。

整合后的稳定公开模型名只有：

```text
gemini-web-image
```

InfiniteCanvas、其他 OpenAI 兼容客户端和 ChatGPT2API 均直接使用这个名字，不增加 `Nano Banana 2` 或其他显示别名。

## 2. 已锁定决策

1. 对外模型名和 InfiniteCanvas 显示名统一为 `gemini-web-image`。
2. InfiniteCanvas 使用 OpenAI 调用格式，不直连 Google Gemini API。
3. InfiniteCanvas 继续调用 ChatGPT2API 已有接口：
   - `GET /v1/models`
   - `POST /v1/images/generations`
   - `POST /v1/images/edits`
4. Gemini 网页协议、Cookie、模型动态发现、上传、候选解析、原图下载、账号轮换和错误映射全部收敛在 `GeminiWebBackend` 深模块内。
5. Cookie 继续通过 ChatGPT2API 账户页粘贴或上传 JSON 导入。
6. 不增加侧边浏览器、Chrome profile、CDP、浏览器扩展或专用浏览器登录导入。
7. 首次导入后，由 ChatGPT2API 自动加密保存并后台维护刷新 Cookie。
8. 只有登录态彻底失效时，用户才需要重新导入 Cookie。
9. 不查询、不计算、不显示、不汇总 Gemini 剩余额度或恢复时间。
10. InfiniteCanvas 优先采用零代码配置接入；只有真实验收发现现有模型选择或请求链不兼容时，才另行提出最小代码改动并等待授权。

## 3. 当前基线

### 3.1 ChatGPT2API 原工作树

```text
路径：E:\AIProject\ChatGPT2API
分支：main
HEAD：b03073377d3f4863e117cfa3a23f13e072025b1a
```

必须保留的用户改动：

```text
M  README.md
M  config.json
?? docs/gemini-web-image-proxy-mvp.md
?? scripts/start-chatgpt2api.ps1
```

### 3.2 Gemini 功能工作树

```text
路径：E:\AIProject\ChatGPT2API-gemini-web-image-mvp
分支：codex/gemini-web-image-mvp
HEAD：b03073377d3f4863e117cfa3a23f13e072025b1a
```

功能仍全部位于未提交工作树中。分支本身没有功能提交，不能通过普通 `git merge` 获得这些改动。

### 3.3 InfiniteCanvas

```text
路径：E:\AIProject\InfiniteCanvas
分支：main
```

当前已有用户改动，后续不得覆盖或顺带提交：

```text
M  docs/content/docs/canvas/canvas-shortcuts.mdx
M  web/bun.lock
?? scripts/
```

## 4. 模块与 seam

### 4.1 外部 seam

InfiniteCanvas 只依赖 ChatGPT2API 的 OpenAI 兼容图片接口：

```text
InfiniteCanvas
    ↓ OpenAI 图片接口
ChatGPT2API 图片协议模块
    ↓ model == gemini-web-image
GeminiWebBackend
    ↓ 内部 transport seam
Gemini 消费者网页端
```

InfiniteCanvas 不需要了解：

- Cookie 名称或值。
- Gemini 网页 RPC ID。
- 网页请求数组索引。
- Google 上传 token。
- 临时或原图下载 URL。
- Google 内部模型 ID。
- Gemini 账号选择、换号、刷新或加密方式。

### 4.2 GeminiWebBackend 接口

调用方只需要账号验证、文生图和参考图编辑三个行为。实现内部隐藏网页协议复杂度，并通过正式 HTTP Adapter 与 fixture Adapter 形成真实内部 seam。

该模块负责：

- 网页初始化与 Cookie 会话。
- 当前账号模型动态发现。
- 参考图上传。
- StreamGenerate 请求和长度帧解析。
- 生成图、输入附件和网页搜索图的区分。
- 原图引用解析与下载。
- 全调用总截止时间。
- 账号轮换、错误归类和槽位释放。

### 4.3 InfiniteCanvas 现有适配能力

现场代码已经满足：

- 能从 OpenAI `/v1/models` 拉取模型。
- `gemini-web-image` 会被识别为图片模型。
- 生成图像页面通过 `/v1/images/generations` 和 `/v1/images/edits` 调用。
- 生成配置节点复用同一图片请求模块。
- 批量生成会拆成多个独立 `n=1` 请求，符合 Gemini Web MVP 契约。

因此计划中不预设 InfiniteCanvas 代码改动，只安排配置和端到端验收。

## 5. 明确不做

- 不把公开模型名改成 `nano-banana-2`。
- 不增加模型显示别名。
- 不让 InfiniteCanvas 使用原生 Gemini 调用格式访问 ChatGPT2API。
- 不读取侧边浏览器或日常 Chrome/Edge profile。
- 不实现专用浏览器登录导入。
- 不从浏览器 Cookie Jar、localStorage 或 session store 提取认证信息。
- 不保存 Google 密码、验证码或 2FA。
- 不实现 Gemini 额度查询、排序、保护或预警。
- 不修改 InfiniteCanvas Agent 或 Canvas Agent 协议。
- 不把当前原工作树的 `config.json` 或本机启动脚本带入功能提交。
- 不向 upstream 提交、推送、创建 Issue、PR、评论或 Release。

## 6. 整合策略

当前两个 ChatGPT2API 分支指向同一个提交，功能改动不在 Git 历史中。推荐采用“规划提交、功能提交、干净 integration 分支”的整合方式。

### 阶段 0：重新确认现场状态

执行前只读确认：

- 两个 ChatGPT2API 工作树的分支、HEAD 和 `git status --short`。
- InfiniteCanvas 的分支、HEAD 和脏工作树状态。
- `origin` 精确指向 `Jason1Jiang/chatgpt2api`。
- `upstream` 仍是 `basketikun/chatgpt2api` 且只读。
- 原工作树四项用户改动未发生变化。
- 功能工作树完整差异仍与本计划相符。

如状态发生变化，停止并更新计划，不按旧快照强行继续。

### 阶段 1：固定用户规划文档归属

在工单 `#11` 中，在 `main` 上只提交：

- README 中已经存在的 Gemini Web MVP 文档链接。
- `docs/gemini-web-image-proxy-mvp.md`。
- 本开发计划在确认后的最终版本。

明确排除：

- `config.json`。
- `scripts/start-chatgpt2api.ps1`。

建议提交信息：

```text
docs: record Gemini Web integration plan
```

### 阶段 2：捕获 Gemini 深模块与协议实现

主要范围：

- `services/gemini_web_backend.py`
- `scripts/gemini_web_protocol_probe.py`
- `docs/gemini-web-image-protocol-probe.md`
- Gemini 协议 fixture、fixture Adapter 和 Backend 测试

建议提交信息：

```text
feat: add Gemini Web image backend
```

验收重点：

- Backend 接口保持小而稳定。
- 网页易变字段不泄漏到路由、账户页或 InfiniteCanvas。
- 正式 HTTP Adapter 与 fixture Adapter 通过同一 seam。
- 候选筛选不会返回输入附件或网页搜索图。

### 阶段 3：账号、加密与登录态维护

主要范围：

- `services/gemini_web_credentials.py`
- `services/account_service.py`
- `api/accounts.py`
- `api/app.py`
- `.env.example`
- `.gitignore`
- `pyproject.toml`
- `uv.lock`
- `docs/gemini-web-cookie-maintenance.md`
- 账号管理、加密和后台维护测试

建议提交信息：

```text
feat: add protected Gemini Web account sessions
```

提交 `uv.lock` 前必须确认：

- `cryptography` 依赖正确。
- 锁文件中既有 greenlet wheel 记录的变化是否为正常重算。
- 不混入与本功能无关的依赖漂移。

### 阶段 4：OpenAI 图片接口接入

主要范围：

- `api/image_inputs.py`
- `services/protocol/openai_v1_image_generations.py`
- `services/protocol/openai_v1_image_edit.py`
- `services/protocol/openai_v1_models.py`
- Gemini 图片生成、编辑、模型发现和可靠性测试
- 直接相关的既有图片回归测试

建议提交信息：

```text
feat: expose Gemini Web through OpenAI image APIs
```

`test/test_multi_image_results.py` 若只属于既有测试稳定化，应单独提交或在提交前证明它是本功能必需项。

### 阶段 5：ChatGPT2API 前端接入

主要范围：

- Gemini Cookie JSON 导入入口。
- 稳定账号 ID。
- 登录态验证、禁用、恢复和删除。
- Gemini 凭据复制与 Token 导出禁用。
- Gemini 账号从额度合计中排除。

建议提交信息：

```text
feat(web): add Gemini Web account controls
```

### 阶段 6：验收工具与最终文档

主要范围：

- `scripts/gemini_web_mvp_acceptance.py`
- README Cookie 维护入口。
- MVP 文档状态从“待开发”更新为与实际提交、测试和部署状态一致。
- 脱敏验收 fixture 与安全说明。

建议提交信息：

```text
test: add Gemini Web acceptance workflow
```

### 阶段 7：干净 integration 分支

从已包含规划提交的 `main` 创建：

```text
codex/gemini-web-image-integration
```

按顺序整合阶段 2 至阶段 6 的功能提交。首选逐提交 cherry-pick，以便：

- 避开原工作树的 `config.json` 和本机脚本。
- 逐模块验证。
- 清楚定位失败提交。
- 保持 README 和 MVP 文档归属唯一。

全部验证通过且工单 `#18` 成为当前 frontier 后，再让本地 `main` 快进到 integration 分支。

## 7. 禁止进入提交的内容

- `config.json`。
- `scripts/start-chatgpt2api.ps1`，至少不得进入 Gemini 功能提交。
- 真实 Cookie、`cookies.json` 或 Cookie 值。
- Authorization、Google 账号密钥或真实账号标识。
- 浏览器 profile、密码、验证码和 2FA。
- 本地 `gemini_web_cookie.key` 或用户目录密钥。
- 真实账号数据文件。
- 私有 probe 目录中的真实图片和其他账号材料。
- 仓库外脱敏摘要，除非用户另行批准复制且再次通过脱敏检查。
- 构建产物、缓存、临时诊断文件和未脱敏日志。
- InfiniteCanvas 当前已有的用户改动。

## 8. InfiniteCanvas 配置计划

ChatGPT2API 合并、安装依赖、构建并重启后，在 InfiniteCanvas 中进行以下配置：

1. 打开“配置与用户偏好”。
2. 新增或编辑一个渠道。
3. 渠道名称建议填写 `ChatGPT2API`。
4. 调用格式选择 `OpenAI`。
5. Base URL 填写：

   ```text
   http://127.0.0.1:8000
   ```

6. API Key 填写现有 ChatGPT2API 用户密钥。
7. 点击“拉取模型”。
8. 进入“模型”Tab。
9. 将 `gemini-web-image` 加入“生图模型可选项”。
10. 根据需要将其设为默认生图模型。

如果 `/v1/models` 没有出现 `gemini-web-image`，先检查 ChatGPT2API 是否至少存在一个状态为“正常”的 Gemini Web 账号，不在 InfiniteCanvas 中伪造模型列表。

## 9. 预期使用路径

### 9.1 首次账号导入

```text
进入 ChatGPT2API 账户页
    ↓
选择“导入 Gemini 网页账号”
    ↓
粘贴或上传 Cookie JSON
    ↓
后端立即验证登录态
    ↓
生成稳定 account_id
    ↓
Cookie 通过 Fernet 加密保存
    ↓
账户列表显示状态为“正常”
```

### 9.2 InfiniteCanvas 文生图

```text
在生成图像页或生成配置节点选择 gemini-web-image
    ↓
InfiniteCanvas 调用 POST /v1/images/generations
    ↓
ChatGPT2API 选择正常且空闲的 Gemini Web 账号
    ↓
GeminiWebBackend 初始化网页会话并动态发现模型
    ↓
调用 Gemini Web StreamGenerate
    ↓
解析候选并下载原图
    ↓
转换为 OpenAI b64_json 或项目托管 URL
    ↓
InfiniteCanvas 显示并保存生成图片
```

### 9.3 InfiniteCanvas 参考图编辑

```text
图片节点或生成配置节点提供参考图
    ↓
InfiniteCanvas 调用 POST /v1/images/edits
    ↓
ChatGPT2API 上传一张或多张参考图
    ↓
Gemini Web 生成新图片
    ↓
Backend 排除输入附件和网页搜索图
    ↓
InfiniteCanvas 获得并展示新生成结果
```

### 9.4 批量生成

InfiniteCanvas 会把多张生成拆成多个独立 `n=1` 请求。ChatGPT2API 根据账号并发槽和账号池状态执行或等待，不要求 Gemini Backend 支持单请求 `n > 1`。

### 9.5 Cookie 自动维护

首次导入后：

1. 真实请求或后台维护完成认证时取得轮换 Cookie。
2. AccountService 合并新 Cookie。
3. 凭据继续以加密形式保存。
4. 默认每 600 秒检查正常且空闲的 Gemini Web 账号。
5. 临时失败执行退避。
6. 禁用、异常、在途和正在刷新的账号被跳过。

只有 Google 彻底撤销登录态、触发设备确认或风控、用户主动退出、修改密码、加密密钥丢失等情况，才需要重新导入 Cookie。

## 10. 验证矩阵

### 10.1 Git 与安全

- 每个提交使用明确文件白名单。
- 检查 staged 文件中不存在 `config.json`、本机启动脚本和私有账号材料。
- `git diff --check` 通过。
- 凭据模式扫描无命中。
- 写远程前再次确认目标精确为 `Jason1Jiang/chatgpt2api`。

### 10.2 Gemini 自动化测试

- 运行全部 `test_gemini_web_*` 模块。
- 重跑此前记录的核心子集和相关隔离回归模块。
- 验证账号导入、Backend、生成、编辑、协议探针、可靠性、加密和后台维护。
- Python compileall 通过。

### 10.3 既有 ChatGPT2API 回归

- ChatGPT 和 Codex 图片生成与编辑。
- `/v1/models`。
- 账号管理、账号导出和图片存储。
- 总时限、504 映射和槽位释放。
- `test_v1_images_edits` 缺少既有 assets 的问题继续单独记录，不误判为 Gemini 回归。

### 10.4 ChatGPT2API 前端

- 生产 Web build。
- Cookie JSON 粘贴与文件上传。
- 账号搜索、验证、禁用、恢复和删除。
- Gemini 凭据不能复制或导出。
- Gemini 账号不进入额度汇总。

### 10.5 InfiniteCanvas 配置验收

- ChatGPT2API 渠道使用 OpenAI 格式。
- `/v1/models` 能拉取 `gemini-web-image`。
- `gemini-web-image` 能加入生图模型可选项。
- 生成图像页面能选择该模型。
- 生成配置节点能选择该模型。
- 请求体实际发送 `model: gemini-web-image`。
- 文生图和参考图编辑均返回可显示图片。
- 批量生成拆成多个 `n=1` 请求。
- 其他已有模型和渠道不受影响。

### 10.6 可靠性与错误契约

- 全调用共享一个基于单调时钟的绝对截止时间。
- 初始化、上传、生成、换号和下载不会重置总时限。
- 每轮最多尝试每个稳定账号一次。
- Cookie 失效标记账号异常并换号。
- 上游限流换号但不查询额度。
- 成功、失败、取消、上传失败、下载失败和超时均释放槽位。
- 验证 400、401、429、502、503 和 504 的稳定 OpenAI 错误 envelope。

### 10.7 真实验收

真实验收已纳入工单 `#19` 的实施授权，但只能在 `#19` 成为当前 frontier 后执行；涉及用户凭据或登录态时，仍需用户提供具体输入并通过对应人工闸门：

- 读取用户指定的仓库外 Cookie JSON。
- 执行真实 Gemini 文生图和参考图编辑。
- 通过原生 OpenAI HTTP 调用验收。
- 通过官方 OpenAI Python 客户端验收。
- 验证加密存储重载和后台维护。
- 执行授权范围内的精确凭据泄漏扫描。

真实验收仍不得读取浏览器 profile，也不得把 Cookie、图片或原始响应写入仓库。

## 11. 部署与激活

代码进入 `main` 不代表当前服务已自动获得新能力。部署与激活只能在工单 `#19` 成为当前 frontier 后执行：

1. 安装或同步新增的 `cryptography` 依赖。
2. 构建 ChatGPT2API 生产 Web 前端。
3. 确认 Cookie 加密密钥策略：
   - 生产或容器环境建议配置 `GEMINI_WEB_COOKIE_KEY`。
   - Windows 本机未配置时使用当前用户 DPAPI 包装的本地密钥文件。
4. 重启 ChatGPT2API 服务。
5. 验证健康检查、账户列表和 `/v1/models`。
6. 再配置 InfiniteCanvas 并执行端到端验收。

不得在工单 `#19` 成为当前 frontier 前替换当前 `127.0.0.1:8000` 实例。

## 12. GitHub 写入规则

- 唯一允许未来写入的仓库：

  ```text
  https://github.com/Jason1Jiang/chatgpt2api
  ```

- `https://github.com/basketikun/chatgpt2api` 永久只读。
- “提交到 GitHub”“推送”或“发布”默认解释为直接提交并推送 owner 的 `origin`。
- 不创建 PR，除非用户明确要求。
- 当前串行工单授权不改变操作顺序：本地规划提交属于 `#11`，功能提交属于 `#12`–`#17`，更新 `main` 只属于 `#18`，部署只属于 `#19`，推送 owner `origin` 只属于 `#20`；不得跨票提前执行或把任一步骤推定为票据外授权。

## 13. 当前授权与人工闸门

2026-07-18 的用户授权允许按 `#10`–`#20` 严格串行完成各票明确范围内的实施、验收和关闭，包括在对应票中创建本地提交、运行测试与构建、更新本地 `main`、部署、执行 owner GitHub 写入和真实 Gemini 验收。该授权不允许跳过阻塞关系，不允许并行处理多票，也不允许把某票的操作提前到其他票。

以下事项仍必须暂停并等待用户提供输入或作出新的范围决定：

- `uv.lock` 出现无法解释的无关漂移。
- 需要修改已锁定的公开模型名、接口、账号 UI、错误契约或其他已批准票据范围。
- 需要读取仓库外 Cookie 文件，但用户尚未指定精确文件及读取范围。
- Google 登录、验证码、2FA、设备确认或风控。
- 发现写入目标不是 owner `Jason1Jiang/chatgpt2api`，或 GitHub 写入范围超出已发布工单的实施、验收和关闭。

## 14. 完成定义

只有同时满足以下条件，才能报告整合完成：

- 功能改动已经进入清晰、可审计的提交历史。
- 原工作树的 `config.json`、启动脚本和其他用户改动完整保留。
- `gemini-web-image` 出现在模型发现和 InfiniteCanvas 生图模型列表中。
- InfiniteCanvas 生成图像页面和生成配置节点均能完成文生图。
- InfiniteCanvas 参考图编辑返回新生成图片。
- Cookie 导入、加密存储、自动维护、禁用、恢复和重新导入形成闭环。
- API、前端、导出和日志不泄漏 Cookie。
- 总时限、换号、稳定错误映射和槽位释放通过验证。
- 既有 ChatGPT/Codex 图片与账号功能回归通过。
- 生产 Web build 和部署验证通过。
- 未实现任何 Gemini 额度查询或展示。
- 未读取浏览器 profile，未增加浏览器登录导入。
- 未向 upstream 执行任何写操作。

## 15. 后续门槛

工单 `#10`–`#16` 已形成现场复核、规划基线和五个独立功能提交；工单 `#17` 负责把它们组装为干净、可审计的 integration 历史。integration 验收不等于更新 `main`、部署或发布：本地 `main` 快进只属于 `#18`，当前环境与 InfiniteCanvas 的真实验收只属于 `#19`，owner `origin` 发布只属于 `#20`。
