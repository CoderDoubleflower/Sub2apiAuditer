# Sub2apiAuditer

面向 **sub2api 提示词审计（Prompt Audit）** 的轻量协议转换服务。

Sub2apiAuditer 接收 sub2api 发出的 OpenAI Chat Completions 审计请求，根据管理网页中配置的 **Base URL、API Key、Model ID 和审核提示词**调用第三方大模型网关，再把模型判定稳定转换为 sub2api 可识别的格式：

```text
Safety: Safe|Controversial|Unsafe
Categories: None|Violent|Jailbreak|...
```

第三方模型不需要原生支持 Qwen3Guard。只要它提供 OpenAI 兼容的 `/chat/completions` 接口，并能根据提示词给出 JSON 或明确的安全判定即可。

## 主要能力

- 内置中文管理网页，视觉风格与 sub2api 管理端保持一致。
- 支持作为 iframe 嵌入 sub2api 自定义页面，适配明暗主题和动态高度。
- 可在网页中配置上游 Base URL、API Key、Model ID、审核提示词、超时和最大输出 Token。
- 提供 `/v1/models` 和 `/v1/chat/completions`，兼容 sub2api 节点探测与正式审核。
- 固定使用网页配置的实际审核模型，不受 sub2api 请求中的 `model` 字段影响。
- 兼容 JSON、`flagged/confidence`、Markdown JSON 代码块和 Qwen3Guard 原生文本。
- 把模型判定归一化为标准 OpenAI Chat Completions 响应。
- 最近 100 条已完成处理详情持久化到 SQLite；重启容器后自动恢复。
- 每条日志记录四个关键时间点和三个阶段耗时。
- 内置统计页：吞吐、成功率、平均/P50/P95/最大延迟、阶段耗时、判定分布、错误分布和最慢请求。
- 使用 Starlette、Uvicorn 和 httpx 异步 I/O，复用上游 HTTP 连接池。
- 官方 Compose 直接使用 `ghcr.io/coderdoubleflower/sub2apiauditer:latest`。
- 支持 Docker Compose、健康检查和两层可选令牌鉴权。

## 工作流程

```text
sub2api
   │ POST /v1/chat/completions
   ▼
Sub2apiAuditer
   │ 读取请求、提取待审核文本
   │ 注入自定义审核策略和固定 JSON 输出协议
   │ 将 model 替换为网页配置的实际 Model ID
   ▼
第三方 OpenAI 兼容大模型网关
   │ JSON / flagged / Qwen3Guard 文本
   ▼
Sub2apiAuditer
   │ 解析和归一化
   │ Safety: ... / Categories: ...
   ▼
sub2api
```

## 管理网页

打开：

```text
http://服务器地址:8080/
```

页面包含三个页签：

1. **运行统计**：显示最近 100 条日志窗口的性能、吞吐、判定和错误统计。
2. **处理日志**：显示最近 100 条请求的四个时间点、阶段耗时、结果与错误详情。
3. **节点配置**：管理上游网关、模型、密钥和提示词，并执行连通性与格式测试。

页面使用与 sub2api 相同的青绿色主色、圆角卡片、统计卡片、页签、表格、状态徽章和深色背景体系。前端不依赖 CDN 或第三方脚本。

### 嵌入 sub2api 自定义页面

最简单的方式是在自定义页面中使用 iframe：

```html
<iframe
  id="sub2api-auditer-frame"
  src="https://auditer.example.com/?embedded=1&theme=system#statistics"
  style="width:100%;height:1100px;border:0;border-radius:16px"
  loading="lazy"
></iframe>
```

可用参数：

| 参数 | 说明 |
|---|---|
| `embedded=1` | 启用嵌入布局，缩小外边距并隐藏底部说明 |
| `theme=system` | 跟随浏览器主题 |
| `theme=light` | 强制浅色主题 |
| `theme=dark` | 强制深色主题 |
| `#statistics` | 默认打开统计页 |
| `#logs` | 默认打开日志页 |
| `#config` | 默认打开配置页 |

如果 iframe 与 sub2api 同源，页面会自动读取并监听父页面 `<html>` 上的 `.dark` 类。跨域嵌入时，可以由父页面主动同步主题：

```js
const frame = document.getElementById('sub2api-auditer-frame')
frame.contentWindow.postMessage(
  { type: 'sub2api-theme', theme: 'dark' },
  '*'
)
```

页面还会向父窗口发送动态高度：

```js
window.addEventListener('message', (event) => {
  if (event.data?.type !== 'sub2api-auditer:resize') return
  const frame = document.getElementById('sub2api-auditer-frame')
  frame.style.height = `${Math.max(700, event.data.height)}px`
})
```

管理令牌不会写入 iframe URL。启用 `ADMIN_TOKEN` 后，在页面右上角的“管理令牌”对话框中输入；令牌只保存在当前标签页的 `sessionStorage`。

> 服务响应允许被 iframe 嵌入。公开部署时建议由反向代理限制可嵌入来源，并使用 HTTPS。

## 处理日志、SQLite 与时间定义

服务默认且最多保留最近 **100 条**处理详情。

运行过程中，正在处理的 Trace 先保存在内存中；当 Auditer 已经把 HTTP 响应完整发送给 sub2api 后，才把这一条完整记录一次性写入 SQLite。Docker Compose 默认数据库路径为：

```text
/data/auditer.db
```

宿主机对应：

```text
./data/auditer.db
```

SQLite 使用 `WAL` 日志模式与 `synchronous=NORMAL`。这样既能跨容器重启恢复最近 100 条记录，又避免在“收到请求 / 转发上游 / 收到 LLM 回复”等每个阶段同步写磁盘。

需要注意：

- 正常完成或正常返回错误的请求，会在响应发送完成后持久化。
- 如果进程被强制杀死、宿主机断电等情况发生在请求仍“处理中”，这一条尚未完成的 Trace 可能来不及写入 SQLite。
- SQLite 只保存处理元数据，不保存完整 Prompt、API Key 或完整模型原始输出。
- 数据库会自动删除第 101 条及更老的完成记录，始终只保留最近 100 条。
- `DELETE /api/logs` 会同时清空内存窗口和 SQLite 中的处理记录。

每条记录包含以下四个时间点：

| 字段 | 含义 |
|---|---|
| `received_at` | Auditer 收到 sub2api HTTP 请求、开始处理的时间 |
| `forwarded_at` | 请求解析完成并即将调用上游 LLM 的时间 |
| `llm_replied_at` | Auditer 完整接收上游 LLM 响应体的时间，不是首字节时间 |
| `sub2api_replied_at` | Auditer 将响应体完整发送给 sub2api 后的时间 |

据此计算：

```text
前处理耗时 = forwarded_at      - received_at
上游耗时   = llm_replied_at     - forwarded_at
回写耗时   = sub2api_replied_at - llm_replied_at
总耗时     = sub2api_replied_at - received_at
```

展示时间使用 UTC 墙钟时间并精确到毫秒；请求运行期间的耗时均由 `time.perf_counter_ns()` 单调时钟计算，系统时间/NTP 调整不会制造负延迟。完成后会把已经计算好的阶段耗时一并持久化，所以重启后仍可显示原始耗时。

对于在某个阶段之前失败的请求，后续时间点会保持为空。例如连接上游失败时，没有 `llm_replied_at`；服务仍会记录错误码、HTTP 状态和已经发生的阶段。

为减少敏感数据风险，处理日志**不会保存**：

- 完整提示词或用户请求正文；
- 上游 API Key、管理令牌或审计访问令牌；
- 完整上游模型输出。

日志只保存 Trace ID、Request ID、模型名、输入规模、上游响应规模、状态、判定、分类、错误和性能时间点。

### 日志 API

```http
GET /api/logs?limit=100
Authorization: Bearer <ADMIN_TOKEN>
```

返回示例：

```json
{
  "items": [
    {
      "id": "aud-0123456789abcdef",
      "source": "sub2api",
      "received_at": "2026-09-03T14:00:00.000Z",
      "forwarded_at": "2026-09-03T14:00:00.002Z",
      "llm_replied_at": "2026-09-03T14:00:00.187Z",
      "sub2api_replied_at": "2026-09-03T14:00:00.188Z",
      "preprocess_ms": 1.842,
      "upstream_ms": 184.991,
      "response_ms": 0.367,
      "total_ms": 187.200,
      "status": "success",
      "http_status": 200,
      "upstream_http_status": 200,
      "safety": "Safe",
      "categories": []
    }
  ],
  "capacity": 100
}
```

清空处理记录和统计窗口：

```http
DELETE /api/logs
Authorization: Bearer <ADMIN_TOKEN>
```

## 统计页

统计数据直接从当前最近 100 条窗口实时聚合。服务启动时会先从 SQLite 恢复持久化完成记录，因此容器正常重启后统计页不会从零开始。

包括：

- 当前窗口请求数、成功数、失败数、处理中数量；
- 最近一分钟 RPM；
- 成功率；
- 总耗时平均值、P50、P95、最大值；
- 上游 LLM 平均耗时和 P95；
- 前处理、上游、回写三个阶段的平均耗时；
- Safe、Controversial、Unsafe 和未分类数量；
- 错误码分布；
- 最近 30 个完成请求的延迟折线；
- 最慢的 5 个请求。

统计 API：

```http
GET /api/statistics
Authorization: Bearer <ADMIN_TOKEN>
```

Docker 默认启动一个 Uvicorn worker，适合保持内存窗口、SQLite 恢复结果和统计视图一致。当前实现不面向多个独立副本共享同一个 SQLite 文件；需要水平扩容时，应改用集中式数据库或指标系统。

## 模型输出格式

推荐让上游模型返回：

```json
{
  "safety": "Unsafe",
  "categories": ["Jailbreak", "PII"],
  "reason": "尝试绕过安全限制"
}
```

服务将其转换为：

```text
Safety: Unsafe
Categories: Jailbreak, PII
```

并包装成标准 OpenAI Chat Completions envelope。

还兼容：

### `flagged / confidence`

```json
{
  "flagged": true,
  "confidence": 0.92,
  "category": "cyber abuse",
  "reason": "检测到攻击意图"
}
```

### Markdown JSON 代码块

````text
```json
{
  "safety": "Safe",
  "categories": []
}
```
````

### Qwen3Guard 原生文本

```text
Safety: Controversial
Categories: Copyright Violation
```

支持的标准分类：

- `Violent`
- `Non-violent Illegal Acts`
- `Sexual Content or Sexual Acts`
- `PII`
- `Suicide & Self-Harm`
- `Unethical Acts`
- `Politically Sensitive Topics`
- `Copyright Violation`
- `Jailbreak`

服务内置常见中英文别名归一化。无法可靠识别模型判定时，不会静默当成 Safe，而是返回：

```text
HTTP 502
error.code = audit_model_invalid_response
```

## Docker Compose 部署

Compose 默认直接拉取 GitHub Container Registry 中的多架构镜像：

```text
ghcr.io/coderdoubleflower/sub2apiauditer:latest
```

支持 `linux/amd64` 和 `linux/arm64`。

部署：

```bash
git clone https://github.com/CoderDoubleflower/Sub2apiAuditer.git
cd Sub2apiAuditer
cp .env.example .env

mkdir -p data
sudo chown -R 10001:10001 data
chmod 700 data

docker compose pull
docker compose up -d
```

运行后当前目录中的持久化数据为：

```text
Sub2apiAuditer/
├── docker-compose.yml
├── .env
└── data/
    ├── config.json
    ├── auditer.db
    ├── auditer.db-wal   # 运行时可能存在
    └── auditer.db-shm   # 运行时可能存在
```

`data/` 已在 `.gitignore` 中忽略。`config.json` 包含完整上游 API Key，应当按敏感配置文件保护；`auditer.db` 不保存完整 Prompt 或 API Key。

编辑 `.env`。生产环境建议设置两个不同的长随机令牌：

```dotenv
ADMIN_TOKEN=用于保护管理接口的长随机字符串
AUDITER_TOKEN=用于保护sub2api审核调用的另一个长随机字符串
```

查看日志：

```bash
docker compose logs -f sub2api-auditer
```

健康检查：

```bash
curl http://127.0.0.1:8080/healthz
curl -i http://127.0.0.1:8080/readyz
```

更新：

```bash
git pull
docker compose pull
docker compose up -d
```

Compose 配置了 `pull_policy: always`，执行 `docker compose up -d` 时也会尝试检查 `latest`，但显式执行 `docker compose pull` 更容易确认是否成功拉到新镜像。

## sub2api 配置

在 sub2api 的提示词审计节点中填写：

| 字段 | 推荐值 |
|---|---|
| 协议 | `OpenAI Compatible` |
| Base URL | `http://sub2api-auditer:8080` |
| Model | `sub2api-auditer` |
| Token | 与 `.env` 中的 `AUDITER_TOKEN` 相同 |
| Timeout | 略大于 Auditer 网页中的上游超时 |
| Input Limit | 按实际审核模型上下文设置 |

如果 sub2api 和 Auditer 在同一个 Docker 网络中，Base URL 使用容器服务名：

```text
http://sub2api-auditer:8080
```

如果 sub2api 运行在宿主机，而 Auditer 映射在本机 8080：

```text
http://127.0.0.1:8080
```

`/v1/models` 会返回：

- `sub2api-auditer`；
- 网页中配置的实际上游 Model ID。

因此 sub2api 节点可长期固定填写 `sub2api-auditer`，以后只通过 Auditer 网页切换实际模型。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `AUDITER_PORT` | `8080` | Docker 对外映射端口 |
| `CONFIG_PATH` | `/data/config.json` | 网页配置持久化文件 |
| `TRACE_DB_PATH` | `/data/auditer.db` | 最近 100 条完成处理详情的 SQLite 数据库 |
| `ADMIN_TOKEN` | 空 | 保护 `/api/config`、`/api/test`、日志和统计接口 |
| `AUDITER_TOKEN` | 空 | 保护 `/v1/models` 和 `/v1/chat/completions` |
| `LOG_LEVEL` | `info` | Uvicorn 日志级别 |
| `MAX_REQUEST_BODY_BYTES` | `2097152` | Auditer 入站请求体上限 |
| `MAX_INPUT_CHARS` | `200000` | 单次待审核文本字符上限 |
| `LOG_CAPACITY` | `100` | 日志/SQLite 窗口条数，限制为 10–100 |
| `HTTP_MAX_CONNECTIONS` | `200` | 上游 httpx 最大连接数 |
| `HTTP_MAX_KEEPALIVE` | `50` | 上游 keep-alive 连接数 |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Uvicorn 信任代理头的来源 |

首次启动还可通过以下变量提供默认上游配置：

- `UPSTREAM_BASE_URL`
- `UPSTREAM_API_KEY`
- `UPSTREAM_MODEL`
- `UPSTREAM_TIMEOUT_SECONDS`
- `UPSTREAM_MAX_TOKENS`
- `AUDIT_PROMPT`

网页保存配置后，以 `CONFIG_PATH` 指向的 JSON 文件为准。

## HTTP API

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| `GET` | `/` | 无 | 管理网页 |
| `GET` | `/healthz` | 无 | 进程健康检查 |
| `GET` | `/readyz` | 无 | 配置就绪检查 |
| `GET` | `/api/config` | `ADMIN_TOKEN` | 读取脱敏配置 |
| `PUT` | `/api/config` | `ADMIN_TOKEN` | 保存配置 |
| `GET` | `/api/status` | `ADMIN_TOKEN` | 运行状态 |
| `POST` | `/api/test` | `ADMIN_TOKEN` | 网页测试审核 |
| `GET` | `/api/logs` | `ADMIN_TOKEN` | 最近处理日志 |
| `DELETE` | `/api/logs` | `ADMIN_TOKEN` | 清空内存与 SQLite 日志 |
| `GET` | `/api/statistics` | `ADMIN_TOKEN` | 日志窗口统计 |
| `GET` | `/v1/models` | `AUDITER_TOKEN` | sub2api 节点探测 |
| `POST` | `/v1/chat/completions` | `AUDITER_TOKEN` | sub2api 审计请求 |

同时提供 `/models` 与 `/chat/completions` 兼容别名。

## 性能设计

Auditer 的固定处理开销主要包括 JSON 读取、文本提取、请求体组装、模型结果解析和 OpenAI envelope 生成。页面中的“接收 → 转发”和“LLM → sub2api”阶段会直接显示这些开销，便于判断瓶颈究竟来自 Python 适配层还是上游模型。

实现采用：

- 全异步 HTTP 请求处理；
- 进程级 httpx 连接池复用；
- 配置不可变内存快照，热路径不读磁盘；
- 处理中 Trace 使用内存结构；
- 响应完整发送给 sub2api 后，再由 Starlette `BackgroundTask` 一次性写 SQLite；
- SQLite 使用 WAL + `synchronous=NORMAL`；
- SQLite 只保留最近 100 条完成记录；
- 极短临界区的内存锁；
- 请求体与上游响应体增量限长读取；
- 静态网页资源内存缓存；
- 不自动跟随上游重定向；
- 不继承宿主机 `HTTP_PROXY` / `HTTPS_PROXY`；
- 不对上游失败执行隐式重试，避免重复费用和额外尾延迟。

因此 SQLite 持久化不位于 Auditer 向 sub2api 返回响应之前的关键路径。实际端到端延迟通常主要由审核模型推理和网络往返决定；应以统计页中三个阶段的实测数据判断，不应仅凭实现语言推断瓶颈。

## 安全说明

- 配置读取接口只返回 `has_api_key` 和脱敏值，不返回完整上游 API Key。
- API Key 留空保存时保留原值；勾选清除才会删除。
- 配置文件通过临时文件、`fsync` 和原子替换写入，并尝试设置为 `0600`。
- Docker 容器使用非 root 用户运行。
- 不记录完整请求正文、Prompt、API Key 或完整模型输出。
- SQLite 处理日志只包含请求元数据、时间点、耗时、判定与错误信息。
- 上游响应大小受限，防止异常响应占用过多内存。
- 生产环境必须设置 `ADMIN_TOKEN` 和 `AUDITER_TOKEN`，并使用 HTTPS 或仅在可信内网开放。
- 允许 iframe 是本项目的明确用途；公网部署时可在 Nginx/Caddy 层覆盖 CSP，只允许你的 sub2api 域名嵌入。

## 本地开发

需要 Python 3.11 或更高版本：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
pytest -q
sub2api-auditer --host 127.0.0.1 --port 8080
```

如需本地启用 SQLite 日志持久化：

```bash
export TRACE_DB_PATH=./data/auditer.db
```

前端为原生 HTML/CSS/JavaScript，不需要 Node 构建步骤。

## 许可证与来源

本项目使用 MIT License。仓库最初 fork 自 Petsitter，现已重构为专门的 sub2api Prompt Audit 适配器；保留原项目许可证声明。
