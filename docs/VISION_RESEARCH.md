# 识图（Vision）功能调研与优化记录（2026-09-24）

本文件沉淀两轮全网调研的结论（逆向 Gemini 项目 × 通用 LLM 网关管线），以及据此对
本项目识图链路实施的优化。所有外部结论均附来源；标注【实证】的为本仓库生产日志/
事故复盘的直接证据。

## 一、调研结论：逆向 Gemini 项目怎么做识图

| 项目 | 上传方式 | token 依赖 | 关键成败因素 | 依赖真实浏览器 |
|---|---|---|---|---|
| [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) | content-push 单次 multipart（Push-ID + X-Tenant-Id），**无 ProcessFile**，裸 ref `[[[ref], filename]]` 直进 f.req | cookie + SNlM0e（可空串）+ cfb2h + f.sid + qKIAYe | cookie 新鲜度、**文件名必须带扩展名**、impersonate chrome145 | 否（curl_cffi） |
| [dsdanielpark/Bard-API](https://github.com/dsdanielpark/Bard-API) / [python-gemini-api](https://github.com/dsdanielpark/Gemini-API) | content-push 单 POST 裸字节、无鉴权 | 仅 Push-ID | 匿名上传已被收紧，基本失效 | 否 |
| [gpt4free Gemini](https://github.com/xtekky/gpt4free) | Scotty 两阶段 resumable（start→X-Goog-Upload-Url→finalize），Semaphore 并发 | cookie + SNlM0e | image_name 必须带扩展名（issue #3064） | 否 |
| [Sophomoresty/gemini-web2api](https://github.com/Sophomoresty/gemini-web2api) | Scotty 两阶段 + X-Client-Pctx；页面 tokens 缓存 600s | cookie + SNlM0e + bl + auth_user | README 明示识图需 cookie | 否（配 cookie-sync 插件半自动） |
| [ikhsan3adi/gemini-web2api](https://github.com/ikhsan3adi/gemini-web2api)（Go） | 同上 + **内置图片压缩** + tls-client | 同上 | 压缩后体积、TLS 指纹 | 否 |
| [xwteam/gemini2api](https://github.com/xwteam/gemini2api) | 附件 base64/URL/多图 | cookie 池 | README 实证 **cookie 约 2h 被 DBSC 强制失效** | 否（插件回传是路线图） |
| [the0cp/gemini-proxy](https://github.com/the0cp/gemini-proxy)、AIStudioProxyAPI 系 | 页面内自动化/上传 | 浏览器登录态本身 | 页面存活 | **是**（Playwright/CDP） |

### 关键发现

1. **ProcessFile 并非普适必需**：HanaokaYuzu 与 Sophomoresty 均以裸 ref 直接进
   StreamGenerate 识图成功。本项目 2026-09-08 事故期间 Google 一度强制 ProcessFile
   【实证 docs/INCIDENT-20260908-vision.md】，但社区主流实现从未加过这一步——两者
   并存说明上游存在「严格/宽松」两种状态。→ 裸 ref 形态应当保留为 ProcessFile 失败
   后的自动降级路径（已实现）。
2. **上传文件名必须带正确扩展名**（g4f #3064：无名上传在 UI 显示 "unknown"、模型
   拒识）。本项目此前对 JPEG 也固定命名 image_N.png【已修复：按 mime 取 .jpg/.png/.webp】。
3. **DBSC 冲击时间线**：2025-02 起 SNlM0e 从部分账号页面消失（HanaokaYuzu issue
   #297）；Chrome 146+ 设备绑定铺开后导出 cookie 数小时失效、异设备不可续期
   （issue #340，官方建议改用 Firefox 提 cookie）；打开的 Gemini 标签页每 ~1000ms
   signaler ping 高频轮转 __Secure-1PSIDTS/1PSIDCC，只传部分 cookie 会被降级为
   guest。社区终局共识：**凭据必须在原设备浏览器内 → CDP 活浏览器桥**（本项目现行路线）。
4. **闲置标签页会杀死页面级凭据**：本项目 2026-09-24 实证【56h 闲置 → at 过期
   （ProcessFile 错误码 7）→ reload 后停发 SNlM0e】；HanaokaYuzu 用上传前后
   _sync_activity 维持会话活跃。→ 页面保活环（已实现，配置门控默认关）。

## 二、调研结论：LLM 网关识图管线工程实践

| 主题 | 业界做法 | 来源 |
|---|---|---|
| 图片预处理 | 魔数嗅探 > Content-Type > 扩展名；长边 1568/2048 重采样；统一转 JPEG（quality 83-85）；HEIC 用 pillow-heif（thumbnails=False） | OWASP 文件上传手册、docs.imgproxy.net/configuration/options、Pillow 安全手册 |
| 下载安全 | scheme 白名单；DNS 解析后查 IP（拦私网/metadata/CGNAT）；钉 IP 防 rebinding；**重定向逐跳重校验 3-5 跳**；超时+大小上限 | OWASP SSRF Prevention Cheat Sheet |
| 图片炸弹 | Pillow MAX_IMAGE_PIXELS 默认 8948 万像素，1-2x 区间仅 warning，生产应升级 error | Pillow security 页 |
| 重试/降级 | 按错误类型分开配重试（LiteLLM RetryPolicy）；熔断=3 连败冷却 60s（allowed_fails/cooldown_time）；响应标记实际通道（cf-aig-step） | docs.litellm.ai/docs/proxy/reliability、Cloudflare AI Gateway fallbacks |
| 错误分类 | 401/欠费=不可重试切渠道；429/5xx=可重试；本项目补充：ProcessFile 码 7=at 过期→刷会话重试；1100=降级 | one-api monitor/channel.go |
| token 保活 | 请求前 lazy refresh + 提前量（google-auth 3m45s）；后台 RotateCookies（60s 节流）；CDP 桥=页面保活环+at 预取 | google-auth 文档、HanaokaYuzu |
| 结果缓存 | 转换结果内存缓存（LiteLLM 10 张/单张 1MB）→ 本项目等价物=上传引用缓存（hash→ref，TTL 15min） | docs.litellm.ai/docs/proxy/image_handling |

## 三、据此实施的优化（全部已落地，820 项测试通过）

| # | 优化 | 模块 | 依据 |
|---|---|---|---|
| 1 | 统一图片预处理管线：EXIF 转正、长边 vision_max_edge_px（默认 2048）降采样、HEIC/AVIF/BMP/TIFF 转码、按预算迭代重压缩；**>4MiB 手机照片从「硬拒」变为「压缩后可识」** | 新增 image_prep.py | 调研二#1、ikhsan3adi Go 版 |
| 2 | 两条链共用归一化入口 _normalize_images：URL 下载 + 魔数嗅探 + 预处理；**修复 bridge 模式收到 http 链接图片直接 TypeError 的 bug** | server/images.py | 本地审查发现的存量 bug |
| 3 | image_url 重定向跟随（≤3 跳、逐跳地址钉扎+私网重校验、环检测）；TCP 连接拒绝/重置重试一次（TLS 失败不重试） | image_fetch.py | OWASP SSRF ④、业界惯例 |
| 4 | bridge 链加固：_bridge_lock 真正接线（串行化+120s 有界等待）；错误带 stage/code；**ProcessFile 码 7 → 自动 reload 标签页重试一次**（09-24 事故自愈）；瞬时失败重试一次 | vision_bridge.py | 调研一#4、调研二错误分类 |
| 5 | **裸 ref 降级**：ProcessFile 持续失败时自动改用无 UUID 的附件形态再试（参考客户端形态） | vision_bridge.py | 调研一#1 |
| 6 | 文件名带正确扩展名（.jpg/.png/.webp/.gif/.bmp） | 两条链 | 调研一#2（g4f #3064） |
| 7 | 模型/思考档位透传进 bridge 页面 payload（此前静默丢弃，恒用默认档） | vision_bridge.py + openai_chat.py | 本地审查 |
| 8 | 直连链熔断：3 连败 → 60s 冷却期内 auto 模式直跳 bridge；成功即复位 | multimodal.py | LiteLLM allowed_fails/cooldown |
| 9 | **bridge→direct 反向救援**：bridge 失败且借出的 token 仍有效时，改走直连链救一次（此前 bridge 模式失败即终局 502） | openai_chat.py | 调研二两级降级链 |
| 10 | 上传引用缓存：sha256(图片) → ref，TTL 15min、上限 64 条 LRU；多轮会话同图不再重复上传 | server/images.py | LiteLLM 转换缓存 |
| 11 | Pillow 炸弹防护升级（DecompressionBombWarning → error）+ 可选 pillow-heif 解码 | image_prep.py | Pillow 安全手册 |
| 12 | **页面保活环**（vision_tab_keepalive_sec，默认 0=关）：定期 reload CDP 标签页维持 DBSC 轮转与 at 新鲜；启用属运维决策 | keepalive.py | 调研一#4、本项目 09-24 根因 |
| 13 | Pillow 进依赖清单；vision-heic 可选 extra | requirements.txt / pyproject.toml | 调研二#1 |

### 已知未做（记录取舍）

- **响应头标记实际通道**（cf-aig-step 等价物）：OpenAI 兼容协议无标准头位，先以日志
  记录（vision bridge chain ok / direct rescue 日志行已可区分）。
- **base64 分块解码到临时文件**：当前单图 ≤20MiB、服务器线程模型，内存峰值可控；
  若未来出现多图大并发再引入 SpooledTemporaryFile。
- **直连链服务端 ProcessFile**（省一次页面往返）：09-08 事故已实证服务端调用会被
  上游拒（错误 7），社区亦无此实现，收益存疑、风险已知，不做。

## 四、运维提示

- 生产容器仍需重建镜像后才吃到以上改动（Pillow 新依赖 + 代码）。
- vision_tab_keepalive_sec 默认关闭；启用即对应 KEY.md 中 2026-09-24
  「防复发组合 B」提议，需人工拍板后改 config.json（第二轮已改为软保活优先：
  at 新鲜只探测不 reload，at 缺失才 reload）。

## 五、第二轮调研与优化（2026-09-25）

第二轮全网检索（HanaokaYuzu/Gemini-API 源码逐行核实、zhu327/gemini-openai-proxy、
new-api 兼容层、官方 files API 对照、DBSC 社区共识）确认：上一轮 13 项中裸 ref 降级、
RotateCookies、入口规范化、熔断等已覆盖社区最佳实践；本轮补齐四项新差距：

| # | 优化 | 模块 | 依据 |
|---|---|---|---|
| 14 | **混合桥链**：图片改在服务端浏览器会话上传（push_id 从 CDP 页面借出），
页面内只跑 ProcessFile + StreamGenerate——CDP evaluate 载荷从 base64 图像字节缩为
纯 ref 字符串，解除 4MiB evaluate 上限（图像按全局 20MiB 上限）；服务端上传失败
自动回落页内 base64 链（回落后自动重压回 4MiB） | vision_bridge.py
（vision_bridge_server_upload 配置，默认开） | HanaokaYuzu：上传端点不依赖 at，仅 SG 需要 |
| 15 | 服务端上传文件名带正确扩展名（此前固定 image.png，JPEG 误报 .png）
| server/images.py _MIME_EXT | g4f #3064（上轮已知但直连链漏改） |
| 16 | **软保活优先的标签页保活环**：探测页面 at 仍新鲜则只做软探测不 reload
（页面自带 signaler 会续转 DBSC）；仅 at 缺失才 reload——规避 09-24 实测的
reload 后 Google 停发 SNlM0e 风险 | keepalive.py _maybe_keep_vision_tab |
HanaokaYuzu _sync_activity、CSA 2026-08 |
| 17 | **多图并发上传**：hybrid 链多图请求并行上传（≤4 并发），对齐 HanaokaYuzu
asyncio.gather | vision_bridge.py _server_upload_all | HanaokaYuzu client.py:1209 |

测试：825 项离线测试全绿（新增 5 项：ref payload、hybrid 成功/回落/配置关闭、
上传扩展名；改写保活环测试为软优先语义）。
- 三次断链的另两项防复发（A：CDP Chrome+socat 纳入 seat supervisor；C：at 缺失
  告警）属宿主机/监控层变更，不在本仓库代码范围。


## 六、第五轮调研与优化（2026-09-28）

新一轮全网核实（HanaokaYuzu master 逐行对照 + PR #247 细节 + g4f 2026 provider 现状）发现
四项前四轮未覆盖的差距，全部落地：

| # | 发现（来源） | 实施 |
|---|---|---|
| 18 | **at 可为空串**：[PR #247](https://github.com/HanaokaYuzu/Gemini-API/pull/247)（v1.20.0, 2026-03）实测 batchexecute 在 at="" 下仍可基本生成——SNlM0e 从页面消失不再等于全链失败 | 页面链 at 缺失时自动降级：at="" + 参考客户端裸附件形态 [[ref], name]（跳过 ProcessFile），成功日志带 "atless rescue" 标记。直接覆盖 09-24「reload 后停发 SNlM0e」事故类的最后救援面 |
| 19 | **兜底 push id**：g4f 至今硬编码 feeds/mcudyrk2a4khkz（2026-05 build 下仍被接受），仅作 qKIAYe 缺失时的 fallback | 页面链 push_id 缺失不再硬失败，使用常量兜底；session 硬门槛改为只查 bl（构建标签） |
| 20 | **payload 演进**：参考客户端新增 inner[80]（思考档 2=extended/1=standard）与请求头 x-goog-ext-525005358-jspb: ["uuid",1]（uuid 与 inner[59] 同源；issue #254 佐证） | 页面链补齐 inner[80]（按 think_mode 映射）与 jspb 头（crypto.randomUUID，降级旧 BRDG 格式） |
| 21 | **_sync_activity 软心跳**：参考客户端在每次上传/生成前发 read-user-preferences batchexecute（bard_activity_enabled）向服务端声明会话活跃——比任何 reload 都软 | vision_bridge.page_activity_ping()：保活环在 at 新鲜探测后于页面内执行同一 RPC（ESY5D），失败仅记日志；从「维持页面」升级为「维持服务端会话」，针对 at 过期根因 |

附件形态对照：HanaokaYuzu 现行 [[url], filename]（本轮 at-less 路径采用）；本项目 HAR 实证
[ref, 1, null, mime, UUID]（ProcessFile 主路径保留）；裸 ref 回退 [ref, 1, null, mime]
（保留）。inner[6]/inner[41] 两版社区实现不一致（[0]/[1]、[2]/[1] 各有出处），维持现状不改。

### 未做（记录取舍）

- 官方 files API 混合架构：需要 API key，超出本项目「逆向网页」范围。
- 服务端 ProcessFile（09-08 已实证被拒）维持不做。

测试：新增 tests/test_vision_round5.py 7 项（at-less 形态、兜底 push id、payload 新字段、
裸 ref 形态不回退、心跳 JS/软失败、保活环接线），全套 870 项通过。
