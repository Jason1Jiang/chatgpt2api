# Gemini Web 最小协议安全探测

本工具只用于工单 #12 的协议验证。它不会读取 Chrome、Edge 或其他浏览器的 Cookie、profile、localStorage、session store，也不会扫描默认目录寻找凭据。

## 当前协议状态

`services/gemini_web_backend.py` 中的正式 HTTP Adapter 已按当前消费者网页协议收敛到以下内部链路：

- 使用同一 HTTP 会话依次完成 Google 预检与 `/app` 初始化，从页面提取 `SNlM0e`、构建标签、会话 ID、语言和文件 Push-ID。
- 当初始化只缺少 `SNlM0e` 时，可调用 Google Cookie 轮换端点在内存会话中刷新短期 Cookie；探测工具不会把刷新值写回输入文件。Backend 只通过账号池 seam 返回刷新值，持久化与加密不属于本工单。
- 初始化后通过 `otAQ7b` batch RPC 动态发现当前账号可用模型，并从账号层级能力计算模型选择头；未指定模型的网页请求可能只返回文本，因此不能跳过这一步或硬编码易漂移的模型 ID。
- 参考图通过 `content-push.googleapis.com/upload` 的 multipart `file` 字段上传，请求携带 `X-Tenant-Id` 与 Push-ID，响应正文作为文件引用。
- 生成前发送 `ESY5D` 活动预热；文本与文件引用随后组装成当前 69 槽 `StreamGenerate` 请求。响应按 Google 长度帧解析，只接受生成图位置，不接受输入附件或网页搜索图。
- 生成图的预览引用只用于取得候选元数据；原图先通过 `c8o8Fe` batch RPC 取得内部引用，再在同一会话中完成两次文本 URL 解引用，最后才下载实际图片。

以上请求与解析形状已有合成协议帧和 fake HTTP Adapter 回归测试。2026-07-17 使用用户明确授权的仓库外 Cookie、参考图与输出目录完成了同一账号真实探测：文生图和参考图编辑各下载 1 张 PNG 原图，编辑结果与输入不同；成功摘要已通过 `assert_scrubbed_fixture()` 后固化为 `test/fixtures/gemini_web/live_protocol_summary.scrubbed.json`。真实图片、Cookie 和账号标识没有进入仓库。

易变解析点集中在 `HttpGeminiWebTransport`：初始化页面键名、`otAQ7b` 的模型/账号层级位置、模型选择头、69 槽请求索引、Google 长度帧、候选中的生成图位置、`c8o8Fe` 请求与返回位置。路由、账号列表和 OpenAI 兼容层不得依赖这些内部字段。

## 无凭据 dry-run

```powershell
.\.venv\Scripts\python.exe scripts\gemini_web_protocol_probe.py dry-run
```

此模式只使用 `test/fixtures/gemini_web/` 的虚假凭据 fixture，不访问 Gemini，不创建输出文件。标准输出只有脱敏 JSON 摘要。

## 人工闸门后的真实探测

用户必须自行准备：

1. 一个独立、非主力 Google 测试账号的 Cookie JSON，保存到仓库外临时文件。
2. 一张仓库外的 PNG、JPEG、GIF 或 WebP 参考图。
3. 一个仓库外输出目录。

用户只需提供 Cookie 文件的本地路径并明确授权读取该路径；不要在聊天中粘贴 Cookie。工具不会接触密码、验证码或 2FA。主任务取得这一次明确授权后，才可运行：

```powershell
.\.venv\Scripts\python.exe scripts\gemini_web_protocol_probe.py live `
  --cookie-file '<仓库外 Cookie JSON 的绝对路径>' `
  --reference-image '<仓库外参考图的绝对路径>' `
  --output-dir '<仓库外输出目录的绝对路径>' `
  --acknowledge-live-probe
```

真实模式固定使用同一账号依次执行登录态验证、文生图、参考图上传与图生图、原图下载。缺少显式 `--acknowledge-live-probe` 会直接拒绝执行；三个路径中任一个位于仓库内也会拒绝执行。

若正常探测失败，可在用户明确授权增强版脱敏诊断后追加：

```powershell
  --diagnose-scrubbed-protocol
```

诊断文件固定为 `gemini-web-protocol-diagnostic.scrubbed.json`，仅包含完成阶段、公开错误类别、字段存在性和协议结构计数。它不包含字段值、提示词、URL、响应片段、Cookie、完整请求头或账号标识。诊断成功只代表所有调用阶段完成；工单验收仍以正常探测实际写出两组图片与成功摘要为准。

输出目录只包含两次生成的图片和 `gemini-web-protocol-summary.scrubbed.json`。摘要仅记录布尔值、计数和 MIME 类型，不记录 Cookie、完整请求头、原始响应、邮箱、账号 ID、XSRF、RPC ID、上传 token、下载 URL、提示词或文件路径。图生图输出会与输入文件按内存摘要比较；完全相同则探测失败。Backend 仍只接收 `generated_image` 候选，因此现有 fixture 可验证输入附件和网页搜索图不会作为输出。

工具不保存原始网络 capture。若 Adapter 返回 `upstream_protocol_error`，只能使用上述已授权的计数诊断或另行取得用户授权；不能把失败响应打印或写入仓库。

## fixture 进入仓库前

任何由真实流程形成的摘要在复制进仓库前，必须先通过 `assert_scrubbed_fixture()` 和仓库敏感扫描。工具会拒绝真实形态的 Cookie、Authorization、账号邮箱、XSRF/RPC/upload/download 引用；真实图片和仓库外 Cookie 文件永远不得复制进 fixture。
