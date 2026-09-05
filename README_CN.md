# gemini-web2api

<p align="center">
  <img src="logo.png" width="200" alt="gemini-web2api logo">
</p>

[English](README.md)

将 Google Gemini 网页端转换为 OpenAI 兼容 API. 零成本, 跨平台, 模块化 Python 包.

## 特性

- **可选密钥**: `api_keys` 为空时免密, 填入密钥后按 OpenAI Bearer Key 校验
- **OpenAI 兼容**: 直接替换 `/v1/chat/completions` 和 `/v1/models`
- **工具调用**: 完整的 Function Calling 支持 (OpenAI 格式)
- **多模型**: Flash (3.6), 扩展思考 (2万字+输出), Pro, Auto, Lite
- **思考深度**: 通过 `@think=N` 后缀调节 (0=最深, 4=最浅)
- **联网搜索**: 内置互联网访问 (Gemini 原生搜索能力)
- **跨平台**: 纯 Python, 传输层优雅降级 (`curl_cffi` Chrome 指纹 → `httpx` → 标准库 `urllib`)
- **流式输出**: 基于 `httpx` / `curl_cffi` 的 SSE Streaming 支持
- **Codex CLI**: Responses API (`/v1/responses`) 兼容 OpenAI Codex
- **Gemini CLI**: Google 原生 API (`/v1beta/models`) 兼容 Gemini CLI

## 快速开始

源码运行:

```bash
pip install -r requirements.txt
PYTHONPATH=src python -m gemini_web2api
```

或安装为包:

```bash
pip install .
gemini-web2api
```

服务启动在 `http://localhost:8081/v1`.

## 客户端配置

### Cherry Studio / ChatBox / 任何 OpenAI 兼容客户端

| 字段 | 值 |
|------|-----|
| Base URL | `http://localhost:8081/v1` |
| API Key | `config.json` 中的任意 `api_keys`；未配置时随便填 |
| Model | `gemini-3.5-flash-thinking` |

### curl

```bash
curl http://localhost:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{"model":"gemini-3.5-flash","messages":[{"role":"user","content":"你好!"}]}'
```

### OpenAI Python SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8081/v1", api_key="sk-your-key")
resp = client.chat.completions.create(
    model="gemini-3.5-flash-thinking",
    messages=[{"role": "user", "content": "解释量子计算"}]
)
print(resp.choices[0].message.content)
```

### Gemini CLI

```bash
export GEMINI_API_KEY=none
export GOOGLE_GEMINI_BASE_URL=http://localhost:8081
gemini
```

支持 Google 原生 API 端点:
- `GET /v1beta/models` — 模型列表
- `POST /v1beta/models/{model}:generateContent` — 非流式生成
- `POST /v1beta/models/{model}:streamGenerateContent` — 流式生成 (SSE)

## 可用模型

| 模型 | 说明 | 输出量 |
|------|------|--------|
| `gemini-3.8-flash` | 全能模型 (最新) | ~1.2万字 |
| `gemini-3.7-flash` | 全能模型 | ~1.2万字 |
| `gemini-3.6-flash` | 全能模型 (上一代) | ~1.2万字 |
| `gemini-3.5-flash` | gemini-3.6-flash 别名 | ~1.2万字 |
| `gemini-3.5-flash-thinking` | 扩展思考, 最长输出 | **~2万字** |
| `gemini-3.5-flash-thinking-lite` | 自适应思考深度 | ~1.5万字 |
| `gemini-3.1-pro` | 高级数学与代码 (需 cookie) | ~1.2万字 |
| `gemini-auto` | 自动选择模型 | 不定 |
| `gemini-flash-lite` | 最快响应, 轻量 | ~1万字 |

### 思考深度

在模型名后追加 `@think=N`:

```
gemini-3.5-flash-thinking@think=0   # 最深 (默认)
gemini-3.5-flash-thinking@think=2   # 中等
gemini-3.5-flash-thinking@think=4   # 最浅
```

## 可选: Cookie 配置 (Pro 模型)

匿名访问对所有模型有效, 但 `gemini-3.1-pro` 在无认证时会路由到 Flash. 要获得真正的 Pro 路由, 需要 **Gemini Advanced (付费订阅)** 账号的 cookie:

```bash
python -m gemini_web2api --cookie-file cookie.txt
```

### 如何获取 Cookie

1. 打开 Chrome, 访问 [gemini.google.com](https://gemini.google.com) 并登录 **Gemini Advanced** 付费账号
2. 打开开发者工具 (F12) → Application → Cookies → `https://gemini.google.com`
3. 复制以下 cookie 值: `SID`, `HSID`, `SSID`, `APISID`, `SAPISID`, `__Secure-1PSID`
4. 创建 `cookie.txt`, 格式如下:

```
SID=你的SID值; HSID=你的HSID值; SSID=你的SSID值; APISID=你的APISID值; SAPISID=你的SAPISID值; __Secure-1PSID=你的1PSID值
```

或使用 JSON 格式:
```json
{"cookie": "SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx", "sapisid": "你的SAPISID值"}
```

**替代方案 (浏览器扩展)**: 使用任意 "Export Cookies" 扩展导出 `gemini.google.com` 的 cookie, 然后转换为上述单行格式.

### 登录账号路径与 XSRF Token

如果已登录的 Gemini 页面 URL 带账号序号, 例如:

```
https://gemini.google.com/u/1/app/...
```

请把 `auth_user` 设置为该序号。登录态的 Gemini Web 请求还可能需要页面里的 XSRF token。该 token 在渲染后的 Gemini 页面源码中名为 `SNlM0e`; 在 `config.json` 中填入 `xsrf_token` 后, 服务会把它作为 `at` 表单字段提交。

示例:

```json
{
  "cookie_file": "/app/cookie.txt",
  "auth_user": "1",
  "xsrf_token": "AOOh0P...",
  "gemini_bl": "boq_assistant-bard-web-server_YYYYMMDD.xx_p0"
}
```

如果登录态请求返回 HTTP 400 且错误中包含 `xsrf`, 请刷新 Gemini Web 后更新 `xsrf_token`, 并确认 `auth_user` 与浏览器 URL 中的 `/u/<序号>/` 一致.

Pro 路由需要 **Gemini Advanced** (付费订阅). 免费 Google 账号的 cookie 可以登录认证, 但会静默回退到 Flash.

## 配置文件

在同目录创建 `config.json`:

```json
{
  "port": 8081,
  "host": "0.0.0.0",
  "retry_attempts": 3,
  "retry_delay_sec": 2,
  "request_timeout_sec": 180,
  "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
  "auth_user": null,
  "xsrf_token": null,
  "api_keys": ["sk-your-key"],
  "cookie_file": null,
  "proxy": null,
  "log_requests": true,
  "temporary_chats": false,
  "cookie_files": [],
  "max_concurrent_requests": 0,
  "auto_delete_history": false
}
```

将 `temporary_chats` 设置为 `true` 后，请求会使用 Gemini 网页版的临时聊天，
不会将对话保存在账号历史记录中。

`api_keys` 为空数组 `[]` 时不校验密钥；填入一个或多个密钥后, `/v1/*` 接口需要 `Authorization: Bearer <key>` 或 `x-api-key: <key>`.

## 并发性能

Gemini 网页端对单个账号大约只稳定支持 3-4 路并发流, 超出的请求会被上游
降速排队 (实测 6 路并发短文本翻译完成时间为 3.5s/10.5s/19.1s). 相关配置:

- `max_concurrent_requests` — 限制同时发往上游的请求数 (0 = 不限制), 超出
  的请求在本地 FIFO 排队. 实测每个 Google 账号的较优值是 **4**; 增加
  cookie 账号后可按比例调大.
- `cookie_files` — 多个 Google 账号 cookie 文件列表, 每个请求轮询选用.
  每个账号有独立的并发预算, N 个账号 ≈ N 倍吞吐. JSON cookie 格式支持
  按账号覆盖 `"auth_user"`:
  ```json
  {"cookie": "...", "sapisid": "...", "auth_user": "1"}
  ```

内置的其它优化: 相同并发请求会合并为一次上游调用 (in-flight 合并),
上游连接池化复用 (httpx keep-alive), 重试使用带抖动的指数退避, HTTP
服务端启用 HTTP/1.1 keep-alive 让客户端复用 TCP 连接.

## 隐私: 自动清理历史

每个 API 请求都会在 Gemini Web 账号历史里创建一条一次性对话. 设置
`auto_delete_history: true` 后, 每次响应完成后会立即通过上游
DeleteConversation RPC 删除该对话 (fire-and-forget, 不增加响应延迟).
流式与非流式端点均生效, 合并请求只删一次. 替代方案:
`temporary_chats: true` 从一开始就不保存对话.

## Docker 部署

```bash
cp config.example.json config.json
docker build -t gemini-web2api .
docker run -d --name gemini-web2api -p 8081:8081 -v ./config.json:/app/config.json gemini-web2api
```

或使用 Docker Compose:

```bash
cp config.example.json config.json
docker compose up -d
```

如需挂载 Cookie 文件:

```bash
docker run -d --name gemini-web2api -p 8081:8081 -v ./config.json:/app/config.json -v ./cookie.txt:/app/cookie.txt gemini-web2api
```

此时 `config.json` 中设置 `"cookie_file": "/app/cookie.txt"`.

> **注意**: 如果 Docker 默认 bridge 网络下出现空回复 (`content: null`), 请切换到 host 网络: `docker run --network host ...` 或在 compose 文件中添加 `network_mode: host`. 这是 Gemini 上游拒绝来自 Docker NAT IP 段的请求导致的.

## 代理配置

如果无法直接访问 `gemini.google.com` (连接超时), 需要配置代理:

**方式 1: 命令行参数**
```bash
python -m gemini_web2api --proxy http://127.0.0.1:7890
```

**方式 2: config.json**
```json
{"proxy": "http://127.0.0.1:7890"}
```

**方式 3: 环境变量** (自动检测)
```bash
set HTTPS_PROXY=http://127.0.0.1:7890
python -m gemini_web2api
```

支持 Clash, V2Ray, Shadowsocks 等任何 HTTP 代理.

## 图片输入

Chat Completions 和 Responses API 支持 OpenAI 风格的多模态消息。图片可以使用
HTTP(S) URL 或 base64 data URL:

```python
resp = client.chat.completions.create(
    model="gemini-3.8-flash",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "描述这张图片"},
            {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
        ]
    }]
)
```

## 已知限制

- **图片上传可能需要 Cookie**: 多模态输入使用 Gemini 网页端图片上传接口。匿名上传失败时, 请配置 Gemini cookie。
- **Pro/Ultra 非真实路由**: 无付费订阅 cookie 时, `gemini-3.1-pro` 实际路由到 Flash 模型. "Pro" 只是 UI 偏好标签.
- **单轮对话**: 每次请求是独立对话, 多轮上下文通过在 prompt 中包含历史消息模拟.
- **频率限制**: Google 可能限制高频请求, server 会自动重试但持续高负载可能被封.

## 系统要求

- Python 3.8+
- `httpx` 与 `curl_cffi` (`pip install -r requirements.txt`); 两者都缺失时自动降级到标准库 `urllib`
- 需要能访问 `gemini.google.com` (部分地区需代理)

## 项目结构

```
src/gemini_web2api/
├── __main__.py            # CLI 入口 (python -m gemini_web2api)
├── config.py              # 默认值 / JSON 加载 / 校验
├── logs.py                # 统一日志门面 (stderr)
├── models.py              # 模型目录 (MODE_CATEGORY 映射)
├── upstream/              # Gemini Web 协议客户端
│   ├── transport.py       #   curl_cffi / httpx / urllib 传输梯队
│   ├── cookies.py         #   多账号 cookie 池, 逐请求轮转
│   ├── protocol.py        #   请求头 / f.req 载荷 / 端点
│   ├── parser.py          #   wrb.fr 响应解析
│   ├── concurrency.py     #   FIFO 并发上限
│   ├── history.py         #   会话删除
│   └── generate.py        #   重试 / 同请求合并 / 流式管线
├── keepalive.py           # 会话自续期 (RotateCookies, SNlM0e)
├── multimodal.py          # 两步 Scotty 图片上传
├── tools.py               # OpenAI 工具调用解析
├── batching.py            # 微攒批 / 批量翻译
└── server/                # HTTP API 层
    ├── base.py            #   路由 / 鉴权 / SSE / 线程服务器
    ├── images.py          #   共享图片上传助手
    ├── openai_chat.py     #   /v1/chat/completions
    ├── openai_responses.py#   /v1/responses (Codex CLI)
    └── google.py          #   /v1beta generateContent (Gemini CLI)
```

## 开发

```bash
make test     # 单元测试 (无网络, 上游全 mock)
make lint     # ruff 静态检查
make run      # 源码启动开发服务器
```

CI 每次推送先跑 lint + 测试, 通过后才发布 Docker 镜像.

## 工作原理

逆向 Google Gemini 网页端的 StreamGenerate 协议, 将 OpenAI API 格式与 Gemini 内部 protobuf-like 格式互转. 模型选择通过请求 payload 的 `[79]` 字段控制, 映射自 Gemini 前端 JS 源码中的 `MODE_CATEGORY` 枚举.

## 致谢

- [linux.do](https://linux.do) 社区
- 开源 API 代理生态

## License

MIT
