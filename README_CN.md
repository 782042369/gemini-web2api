<h1 align="center">gemini-web2api</h1>

<p align="center">
  <img src="logo.png" width="180" alt="gemini-web2api logo">
</p>

<p align="center">
  把 Google Gemini 网页端逆向为 OpenAI 兼容 API。<br>
  专注翻译吞吐与图片理解。
</p>

<p align="center">
  <a href="README.md">English</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="license: MIT">
  <img src="https://img.shields.io/badge/python-3.8%2B-3776AB" alt="python: 3.8+">
  <img src="https://img.shields.io/badge/version-1.3.0-3.8" alt="version: 1.3.0">
  <img src="https://img.shields.io/badge/docker-ready-2496ED" alt="docker: ready">
</p>

## 这是什么

一个单进程 Python 服务：直接讲 gemini.google.com 的网页协议，对外暴露成标准 API。不需要 Google API Key、不产生计费——上游就是浏览器使用的同一个网页会话。文本生成匿名可用；挂 cookie 解锁 Pro 与思考模型；当账号会话令牌被浏览器绑定时，通过 Chrome DevTools 桥恢复识图。

```
[OpenAI 客户端 / Gemini CLI / Cherry Studio / curl]
        |  /v1/chat/completions  /v1/responses  /v1beta (原生)
        v
[gemini-web2api]  -- 翻译微批处理、预算控制、重试、XSRF 自愈
        |
        +--> 直连链路: curl_cffi Chrome TLS -> gemini.google.com
        |         (页面 token、Scotty 上传、StreamGenerate)
        |
        +--> 识图桥（可选）: CDP 接入已登录的 Chrome 标签页
                  (借用浏览器专属的 SNlM0e 令牌，在页面内
                   执行 上传 -> StreamGenerate 链路)
```

## 特性

- **OpenAI 兼容**：`/v1/chat/completions`（逐 token 真 SSE 流式）与 `/v1/models`
- **Gemini 原生**：`/v1beta/models` 与 `generateContent` / `streamGenerateContent`（Gemini CLI 可用）
- **Responses API**：`/v1/responses`，兼容 OpenAI Codex 类客户端
- **图片理解**：`image_url` / `input_image`（data URL 与 http 链接），经直连链路或 CDP 识图桥服务——见[识图](#识图图片输入)
- **面向翻译的批处理**：突发单段请求自动合并为一次编号上游调用；多段请求按段拆分并独立预算
- **模型控制**：Flash 3.8 / 3.7 / 3.6、深度思考（2 万字以上输出）、Pro、Auto、Flash Lite，支持 `@think=N` 后缀（0=最深，4=最浅）
- **浏览器级传输**：优先 curl_cffi Chrome TLS 指纹，httpx 与标准库 urllib 兜底
- **会话韧性**：后台保活轮换 cookie 并刷新 XSRF 令牌；上游 XSRF 拒绝时立即失效令牌缓存并重试一次
- **账号池**：多 cookie 文件按请求轮转，账号会话状态隔离
- **隐私**：临时会话与生成后自动删除对话
- **硬限制**：请求体/图片大小上限、有界队列、单请求截止时间（见 [docs/HARDENING.md](docs/HARDENING.md)）

## 快速开始

```bash
pip install -r requirements.txt
PYTHONPATH=src python3 -m gemini_web2api
```

或安装为包后运行：

```bash
pip install .
gemini-web2api
```

服务监听 `http://127.0.0.1:8081`（可用 `config.json` 覆盖）。验证：

```bash
curl http://127.0.0.1:8081/v1/models
curl http://127.0.0.1:8081/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"gemini-3.8-flash","messages":[{"role":"user","content":"Reply with exactly: OK"}]}'
```

Flash 系模型无需 cookie 即可使用；Pro 与思考模型需要——见 [Cookie](#cookiepro-与思考模型)。

## Docker

```bash
docker-compose up -d --build
```

compose 文件绑定 `127.0.0.1:8081:8081`，挂载 `./config.json`、cookie 文件与 `./logs`，异常退出自动重启。

## 客户端配置

| 字段 | 值 |
|------|-----|
| Base URL | `http://127.0.0.1:8081/v1` |
| API Key | `config.json` 中 `api_keys` 的任意值；列表为空时关闭鉴权 |
| 模型 | `gemini-3.8-flash`（默认） |

## 模型

| 模型 | 说明 |
|------|------|
| `gemini-3.8-flash` | 默认，最新全能 Flash |
| `gemini-3.7-flash` / `gemini-3.6-flash` / `gemini-3.5-flash` | 早期 Flash（3.5 为 3.6 别名） |
| `gemini-3.5-flash-thinking` | 深度思考，最长输出（2 万字以上） |
| `gemini-3.5-flash-thinking-lite` | 动态思考，自适应深度 |
| `gemini-3.1-pro` | Pro 档，需要 cookie |
| `gemini-3.1-pro-enhanced` | 实验性增强 Pro |
| `gemini-auto` | 服务端自动选择 |
| `gemini-flash-lite` | 轻量档 |

任意模型可用 `@think=N`（如 `gemini-3.8-flash@think=0`）固定思考深度 0-4。

模型路由由请求体字段（`inner[79]`）配合中性的 per-model ticket 头携带；变体槽位（`inner[80]`）刻意不发送——实测发送它会破坏路由（结论记录在 `src/gemini_web2api/models.py`）。

## Cookie（Pro 与思考模型）

从已登录 gemini.google.com 的浏览器会话导出 cookie，写成 JSON 文件：

```json
{
  "cookie": "__Secure-1PSID=...; __Secure-3PSID=...; SAPISID=...; ...",
  "sapisid": "..."
}
```

在 `config.json` 里用 `cookie_file` 指向它。多个账号可通过 `cookie_files` 组池（按请求轮转）。服务端能轮换的部分会自动轮换；设备绑定（DBSC）的会话需要其浏览器保持存活——识图桥已经覆盖这一要求。

## 识图（图片输入）

由 `config.json` 的 `vision_mode` 选择两条服务路径：

- **直连**（默认 `auto`）：服务用自己的 TLS 会话上传图片（Scotty 两步流）并调用 StreamGenerate。需要账号页面向服务端下发 XSRF 令牌；令牌被浏览器绑定的账号会自动回退。
- **桥**（配置 `vision_bridge_url`）：指向一个带已登录 Gemini 标签页的 Chrome DevTools 端点。服务借用页面的实时令牌（抗僵死：卡死的标签页会被自动替换），并在页面内执行识图链。标签页未登录时请求毫秒级返回 `vision_bridge_not_logged_in`，不再挂死。

| `vision_mode` | 行为 |
|--------------|------|
| `auto` | 有活令牌时直连优先；否则走桥，且作为直连失败兜底 |
| `bridge` | 始终走 CDP 标签页 |
| `direct` | 永不走桥 |

## 配置

全部键位于 `config.json`（完整带注释示例见 `config.example.json`）。

| 键 | 默认值 | 用途 |
|----|--------|------|
| `port` / `host` | `8081` / `0.0.0.0` | 监听地址 |
| `api_keys` | `[]` | Bearer 鉴权列表；为空关闭鉴权 |
| `default_model` | `gemini-3.8-flash` | 请求未带模型时使用 |
| `cookie_file` / `cookie_files` | `null` / `[]` | 单账号 / 账号池 |
| `proxy` | `null` | 全部传输的上游代理 |
| `impersonate` | `chrome145` | curl_cffi TLS 指纹档位 |
| `vision_bridge_url` | `null` | 识图桥的 CDP 端点 |
| `vision_mode` | `auto` | 识图路由（auto/bridge/direct） |
| `keepalive_sec` | `540` | 后台会话保活间隔 |
| `retry_attempts` / `retry_delay_sec` | `3` / `2` | 上游重试策略 |
| `request_timeout_sec` / `slow_retry_sec` | `180` / `60` | 单次尝试上限 |
| `max_concurrent_requests` | `0` | 上游并发上限（0=不限） |
| `translation_batch_max_chars` / `..._segments` | `12000` / `25` | 批量翻译拆分 |
| `microbatch_sec` / `microbatch_max` | `1.5` / `6` | 突发合并窗口与上限 |
| `max_request_body_bytes` / `max_image_bytes` | `16MiB` / `20MiB` | 输入硬限制 |
| `temporary_chats` / `auto_delete_history` | `false` / `false` | 上游隐私开关 |
| `log_file` / `log_retention_days` | `null` / `7` | 日志文件与按天保留 |

## 开发

```bash
make test    # 离线测试套件（无网络；794 条）
make lint    # ruff，规则集见 pyproject.toml
make run     # 源码树开发服务
```

可靠性说明、事故记录与加固清单见 [docs/](docs/)。

## 许可

MIT——见 [LICENSE](LICENSE)。