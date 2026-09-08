# 识图 1100 事件记录（2026-09-08）

## 现象

- 09-08 ~12:36 起，所有带图片引用的 StreamGenerate 请求被上游拒绝：`BardErrorInfo [1100]`。
- 纯文本生成完全正常；Scotty 两步上传（push.clients6.google.com）完全正常（能拿到 `/contrib_service/ttl_1d/...` 引用）。
- 同一套代码与镜像在当天 10:10 验证通过，此后未变。

## 已排除（穷举实验）

| 变量 | 实验 | 结果 |
|---|---|---|
| at/SNlM0e | 有/无/旧值 | 均拒绝 |
| gemini_bl | 旧(0831)/新(0907) | 均拒绝 |
| uuid 绑定头 x-goog-ext-525005358-jspb | 有/无 | 均拒绝 |
| /u/N 账号前缀 | 无重定向 | 无错位 |
| 上传步骤 | 服务器 pycurl / 浏览器 Chrome fetch | 均成功拿到 ref |
| 生成传输栈 | pycurl / 浏览器页面内 fetch（同 ref 同信封） | 均拒绝 |
| 账号 | gwTorYTj(新) / ZbYUZM8a(旧) | 均拒绝 |
| 完整浏览器信封复刻（f.sid、x-goog-ext-*、inner[3]/[4]、全槽位） | 服务器侧 | 拒绝 |
| 浏览器页面 JS 全链路（Chrome 栈上传+生成） | headless Chrome 内 | 拒绝 |

## 关键发现

1. 真实浏览器 UI 上传图片后会调用 `BardChatUi/data/assistant.lamda.BardFrontendService/ProcessFile`（f.sid=FdrFJe、x-goog-ext-525001261-jspb 头、body=[attachment_entry, null, 1, ["en-US"]]）——服务器直连流程缺这一步。
2. 但 09-08 晚间 `ProcessFile` 对一切调用方返回错误 [7]：包括官方 UI 自动化、页面 JS 复刻、原样重放（仅换新上传 ref）。
3. 官方 Web UI（headless Chrome + 有效 cookie）传图后消息同样无法发出（ProcessFile 失败后无 StreamGenerate）。
4. Google 已从 gemini.google.com 页面移除 SNlM0e（WIZ_global_data 134 键中无此键；HanaokaYuzu/Gemini-API issue #297 佐证）。qKIAYe(push_id)/Ylro7b(pctx)/FdrFJe(f.sid)/cfb2h(bl) 仍可用。

## 结论

Google 于 09-08 中午对文件上传/识图管线做了服务端变更：StreamGenerate 严格要求文件先经 ProcessFile 登记，且 ProcessFile 本身对（本账号/本环境的）程序化调用返回 [7]。连官方 Web UI 在此环境下也无法完成带图发送。属于上游变更，非本项目代码缺陷。

## 待观察/后续

- 若个人浏览器官方 UI 传图正常 → 属账号/环境风控，导出新 cookie（含完整登录态）可解。
- 若个人浏览器官方 UI 传图也失败 → Google 侧灰度故障，等待恢复；恢复后优先补 ProcessFile 步骤再验证。
- 上游动向跟踪：zexadev/gemini-web2api-go（v4.15.0, 09-02）、HanaokaYuzu/Gemini-API。
- 排障基础设施：本次使用的 headless Chrome + CDP 抓包方案（注入 cookie→复刻上传/生成）保存在 /tmp 清理前的工作记录中，可复用。
