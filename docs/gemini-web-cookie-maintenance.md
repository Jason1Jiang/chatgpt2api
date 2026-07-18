# Gemini Web 可刷新登录态

本功能维护可刷新的 Gemini Web 登录态，但不保证 Cookie 永久有效，也不绕过 Google 的密码、验证码、2FA、设备确认或风控。

## 工作方式

1. 账号导入或真实请求完成初始化和模型发现后，HTTP Adapter 返回当前会话的完整 Cookie 映射。
2. Gemini Backend 通过账号池 seam 交给 `AccountService`；协议 Adapter 不直接读写账号文件。
3. `AccountService` 按稳定账号 ID 合并 Cookie，再以 Fernet 封装写入现有存储。API、列表、导出、日志和异常不返回凭据。
4. 应用 lifespan 启动后台维护线程，默认每 600 秒验证正常且空闲的 Gemini Web 账号。禁用、异常、正在生成或已经刷新的账号会被跳过。
5. 临时错误只触发指数退避；认证失效会把账号标记为异常。重新导入有效 Cookie 可覆盖旧凭据并恢复账号。

## 密钥

生产和容器环境建议设置 `GEMINI_WEB_COOKIE_KEY`。它必须是 Fernet URL-safe base64 key，且不得提交到仓库、日志或工单。

若未设置环境密钥，程序会在用户目录的 `.chatgpt2api/gemini_web_cookie.key` 创建本地密钥。Windows 会用当前用户的 DPAPI 包装文件内容；其他系统将文件权限限制为 owner-only。可通过 `GEMINI_WEB_COOKIE_KEY_FILE` 指向另一个仓库外路径。密钥与账号数据应分开备份；密钥丢失后，已有 `protected_cookies` 无法解密，只能重新导入 Cookie。

生成新密钥的示例：

```powershell
py -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 刷新间隔

`GEMINI_WEB_REFRESH_INTERVAL_SECONDS` 默认是 `600`，任何小于 `60` 的值都会提升到 `60`。刷新失败时等待时间逐次翻倍，最大一小时；下一次成功后恢复到配置间隔。

## 兼容与迁移

旧的明文 `credentials.cookies` 仍可读取。它会在下一次成功验证、Cookie 刷新或账号写入时迁移为 `credentials.protected_cookies`。认证失败或协议响应不完整不会用空值覆盖最后一次可用 Cookie。
