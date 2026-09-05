# Sub2apiAuditer

将 **sub2api Prompt Audit 审核请求**转发到自定义的 OpenAI-compatible 模型网关，再把模型判定转换成 sub2api 支持的 `Safety / Categories` 格式。

提供中文管理网页、自定义审核策略、最近 100 条日志与统计、SQLite 持久化和 Docker 多架构镜像。网页可嵌入 sub2api 的自定义页面。

```text
sub2api → Auditer /v1/chat/completions
        → 自定义 Base URL + API Key + Model ID + 审核 Prompt
        → 上游模型判定
        → OpenAI envelope + Safety / Categories → sub2api
```

## 1.2.1 修复说明

本版本保留 1.2.0 的 `normalize.py` 与 `protocol.py`，**不收紧结果解析或改变现有判定兼容策略**。修复集中在运行时：

- 只有响应收尾后才将 Trace 视为完成；不因已拿到模型判定而提前淘汰。已完成窗口与进行中记录分开计数。
- 清空与持久化使用独立数据库锁和代次校验，防止旧回调把已清空日志重新写回；清空失败返回真实的 HTTP 500，保留内存记录。
- SQLite 操作显式关闭连接，每个连接设置 `synchronous=NORMAL`；异步 HTTP 路径不执行阻塞式数据库 I/O。
- 最后一次 ASGI 响应体发送完成时立即记录时间，随后才在线程中写 SQLite，避免计时混入线程池排队。
- 增加覆盖上游连接池等待、连接、发送和完整响应读取的总超时；保留 HTTPX 分阶段超时。
- 配置文件优先于环境变量。文件损坏不会回退到环境变量里的旧网关；有效文件也不会被无效的环境默认值阻止加载。
- 畸形鉴权返回 401；取消、客户端断开、响应发送失败均结束 Trace，不伪造发送完成时间。
- 支持根路径、反代剥离前缀及 `BASE_PATH` 保留前缀三种部署；网页显示真实存储模式。
- 镜像构建/发布依赖 Python 和前端测试全部通过；主分支发布串行化，跳过已过时的提交。

## Docker Compose 部署

镜像：

```text
ghcr.io/coderdoubleflower/sub2apiauditer:latest
```

支持 `linux/amd64` 和 `linux/arm64`。每次主分支成功发布同时生成 `sha-<短提交号>` 标签；生产环境可改用已验证的固定标签。

```bash
git clone https://github.com/CoderDoubleflower/Sub2apiAuditer.git
cd Sub2apiAuditer
cp .env.example .env
```

在 `.env` 设置两个不同的长随机 ASCII 令牌：

```dotenv
ADMIN_TOKEN=替换成管理员令牌
AUDITER_TOKEN=替换成审计调用令牌
```

镜像以 UID `10001` 运行。普通 Linux Docker 部署先准备可写目录：

```bash
mkdir -p data
sudo chown -R 10001:10001 data
chmod 700 data
docker compose pull
docker compose up -d
```

打开 `http://服务器地址:8080/`，填写管理令牌，在“节点配置”保存上游 Base URL、API Key、Model ID 和审核提示词。API Key 留空保存保留原密钥，勾选清除才删除。读取配置只返回密钥掩码。

```bash
docker compose ps
docker compose logs -f sub2api-auditer
curl http://127.0.0.1:8080/healthz
curl -i http://127.0.0.1:8080/readyz
```

`/healthz` 检查进程存活，`/readyz` 检查配置能否用于审核，不主动调用上游模型。配置异常时管理网页仍可用于修复，实际审核返回 503。

更新已有部署，不要覆盖已有 `.env`：

```bash
git pull --ff-only
docker compose pull
docker compose up -d
```

旧的 `config.json` 和 1.2.0 的 `auditer.db` 可直接使用，不需要删除数据库。若曾使用 Docker named volume，需先把旧配置迁移到新的 `./data`；绑定目录不会自动复制 named volume 中的数据。

镜像包若设为私有，需要具备该包读取权限的 GHCR 登录；公开包可匿名拉取。不要把访问令牌提交到仓库。

## 数据保存在哪里

Compose 使用 `./data:/data`，相对目录以 Compose 项目目录为准：

```text
Sub2apiAuditer/
├── docker-compose.yml
├── .env
└── data/
    ├── config.json       # 实际上游配置，包含 API Key，按敏感文件保护
    ├── auditer.db        # 最近最多 100 条已结束的处理详情
    ├── auditer.db-wal    # WAL 辅助文件，可能存在
    └── auditer.db-shm    # 共享内存辅助文件，可能存在
```

配置保存后使用内存快照；请求热路径不读配置文件。日志在请求中只更新内存，完成响应发送后再写数据库。进程启动恢复数据库中已保存的窗口，统计随之恢复。

数据库使用 WAL、`synchronous=NORMAL`、5 秒锁等待。它适合小型单实例日志窗口，**不是强持久化审计归档**：异常断电可能丢失最近提交，`SIGKILL` 或崩溃也可能丢失尚未落库的记录，即使该条 HTTP 响应已经发出。正常关闭会等待请求收尾；数据库写入失败会记录服务错误，并通过运行状态显示。

仅保留最近 `LOG_CAPACITY` 条已结束记录，按收到请求的顺序保留最新窗口；进行中请求不因容量而提前删除。界面最多显示最新 100 条（包含进行中记录）。当前进行中计数可大于 100。数据库行数有上限，不意味着数据库和 WAL 文件的字节大小恒定。

“清空日志”会删除当前实例内存窗口及 SQLite 记录，并丢弃清空之前尚未落库的旧快照；不会取消真实审核调用。清空失败返回 `trace_clear_failed`，不会只清掉内存后假装成功。

使用单 Uvicorn worker、单实例。多个进程的内存窗口和清空代次不会自动共享；不要用多个实例共同维护同一份日志数据库。SQLite 数据目录建议位于本机磁盘而不是网络文件系统。

备份时最简单的方式是停止容器后复制整个 `data/` 目录，再启动容器；不要在运行中只拷贝 `auditer.db` 而忽略 WAL。

## sub2api 节点配置

| 字段 | 建议值 |
|---|---|
| 协议 | OpenAI Compatible |
| Base URL | 同一 Docker 网络中使用 `http://sub2api-auditer:8080` |
| Model | `sub2api-auditer` |
| Token | `.env` 中的 `AUDITER_TOKEN` |
| Timeout | 大于 Auditer 上游总超时，并预留网络与本地格式处理余量 |
| Input Limit | 按实际上游模型支持的输入长度设置 |

两个容器必须实际加入同一网络，服务名才能互相解析。sub2api 在宿主机运行时，可以使用映射端口；在其他容器里填写 `127.0.0.1` 指向的是那个容器自己。

`/v1/models` 同时返回固定别名 `sub2api-auditer` 和配置中的实际模型 ID。Auditer 忽略请求中的模型选择，始终调用管理页面配置的模型。以后更换模型，不必同步修改 sub2api 的别名。

上游支持 **OpenAI-compatible Chat Completions**，不是直接支持原生 Anthropic Messages 或原生 Gemini API。Base URL 可以是根地址、`/v1`、版本路径或完整 `/chat/completions` 地址；程序按 `protocol.py` 规则拼接。

## 嵌入 sub2api 自定义页面

独立域名方式：

```html
<iframe
  id="auditer-frame"
  src="https://auditer.example.com/?embedded=1&theme=system#statistics"
  style="width:100%;height:1100px;border:0;border-radius:16px"
></iframe>
```

`#statistics`、`#logs`、`#config` 选择默认页签。支持 `theme=light`、`dark`、`system`。同源 iframe 跟随父页 `<html>` 的 `.dark` 类；跨域父页可发送主题消息。浏览器不允许 iframe 使用 sessionStorage 时，令牌退回页面内存，刷新后需要重输。

### 同源子路径，反代保留前缀

`.env`：

```dotenv
BASE_PATH=/auditer
```

Caddy 示例（合并到你现有站点块，不要替换其他路由）：

```caddyfile
@auditer path /auditer /auditer/*
handle @auditer {
    reverse_proxy 127.0.0.1:8080
}
```

管理网页使用 `/auditer/`，sub2api 节点 Base URL 使用 `https://你的域名/auditer`。应用会把 `/auditer` 重定向到 `/auditer/`，所有 API/资源都位于此前缀下。Docker 的根路径 `/healthz` 仍保留。

### 同源子路径，反代剥离前缀

`BASE_PATH` 保持空，Caddy 示例：

```caddyfile
redir /auditer /auditer/ 308
handle_path /auditer/* {
    reverse_proxy 127.0.0.1:8080
}
```

浏览器仍访问 `/auditer/`。资源和 API 根据浏览器可见的目录使用相对路径，不会误发到主站的 `/api/`。两种模式不要混用。

### 跨域主题和高度同步

以下代码放在父页面，替换成真实 Auditer 域名：

```js
const frame = document.getElementById('auditer-frame');
const auditerOrigin = 'https://auditer.example.com';
frame.addEventListener('load', () => {
  frame.contentWindow.postMessage({ type: 'sub2api-theme', theme: 'dark' }, auditerOrigin);
});
window.addEventListener('message', (event) => {
  if (event.origin !== auditerOrigin || event.source !== frame.contentWindow) return;
  if (event.data?.type !== 'sub2api-auditer:resize') return;
  const height = Number(event.data.height);
  if (Number.isFinite(height)) frame.style.height = `${Math.max(700, Math.min(10000, height))}px`;
});
```

服务允许 iframe，默认 CSP 的 `frame-ancestors *` 较宽。公网反代建议覆盖完整 CSP，把 `frame-ancestors` 限制为自己的 sub2api 域名；不要把管理令牌放在 iframe URL 中。

## 日志与统计口径

| 字段 | 含义 |
|---|---|
| `received_at` | 审核 HTTP 处理入口创建 Trace 的时间 |
| `forwarded_at` | 即将调用 HTTP 客户端发起上游请求；包括后续连接池等待 |
| `llm_replied_at` | 完整接收上游响应体，非首字节时间 |
| `sub2api_replied_at` | 最后一段 ASGI 响应体发送完成，不是 sub2api 的解析确认 |

接口时间点为 UTC；表格按浏览器本地时间显示到毫秒，折线图横轴使用 UTC。耗时使用单调时钟，不受系统校时影响。

前处理 = 收到请求到开始上游调用；上游 = 开始调用到响应体完整接收；回写 = 接收完成到 ASGI 响应发送完成。数据库线程排队及写入发生在计时之后，不计入回写/总耗时。

取消或发送失败时，未发生的时间点留空，总耗时为处理入口到本地结束。日志中的 499 是本地取消/断开标记，不代表真的给断开的客户端发送过 HTTP 499。HTTP 200 只表示 Auditer 返回的 HTTP 状态，不证明 sub2api 接受了判定。

统计包含成功率、平均/P50/P95/最大总耗时、平均阶段耗时、上游 P95、判定分布、错误码分布、最近 30 条延迟和最慢 5 条。RPM 仅统计**当前可见窗口内**最近一分钟的请求，不是无限历史的全量 RPM。

处理日志保存 Trace ID、双方 Request ID、模型、输入字符/字节数、上游响应字节数、判定、分类、错误及各时间点；不保存完整用户 Prompt、API Key 或模型原始正文。网页手动测试可临时显示最多 8000 字符的原始输出，但不会把它存入日志数据库。

## 审核输出兼容

推荐上游返回：

```json
{"safety":"Unsafe","categories":["Jailbreak","PII"],"reason":"简短原因"}
```

Auditer 转为：

```text
Safety: Unsafe
Categories: Jailbreak, PII
```

然后放入标准 OpenAI `choices[0].message.content` 返回给 sub2api。

保留已有兼容规则：Markdown JSON 代码块、部分嵌套 JSON、`flagged/confidence`、Qwen3Guard 原生两行文本以及宽松关键词解析。**这不是严格 Schema 验证，也不保证模型判定的语义正确性。** 本次修复没有增加截断输出拒绝、冲突字段校验或更严格的 Safe 判定。完全无法识别的输出仍按原有行为返回 `502 audit_model_invalid_response`。

标准分类包括 Violent、Non-violent Illegal Acts、Sexual Content or Sexual Acts、PII、Suicide & Self-Harm、Unethical Acts、Politically Sensitive Topics、Copyright Violation、Jailbreak。保留已有中英文别名转换。

## 环境变量

| 变量 | 默认值 | 作用 |
|---|---|---|
| `AUDITER_PORT` | `8080` | Compose 宿主机端口，容器仍为 8080 |
| `CONFIG_PATH` | 本地 `./data/config.json`；Docker `/data/config.json` | 持久化上游配置 |
| `TRACE_DB_PATH` | 本地未设置为内存；Docker `/data/auditer.db` | 日志 SQLite 路径；Docker 数据应放在 `/data` 内 |
| `BASE_PATH` | 空 | 应用挂载前缀，如 `/auditer`；反代剥离前缀时留空 |
| `ADMIN_TOKEN` | 空 | 管理接口令牌；空值表示不鉴权 |
| `AUDITER_TOKEN` | 空 | 审核及模型列表接口令牌；空值表示不鉴权 |
| `LOG_LEVEL` | `info` | Uvicorn 日志级别 |
| `LOG_CAPACITY` | `100` | 已结束记录容量，限制为 10–100 |
| `MAX_REQUEST_BODY_BYTES` | `2097152` | 入站请求体上限，默认 2 MiB |
| `MAX_INPUT_CHARS` | `200000` | 待审核文本字符上限 |
| `HTTP_MAX_CONNECTIONS` | `200` | 上游连接池最大连接数，不是入站请求并发限制 |
| `HTTP_MAX_KEEPALIVE` | `50` | 上游可复用空闲连接上限 |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Uvicorn 信任代理头的来源，应填写真实代理地址 |
| `UPSTREAM_BASE_URL` | 空 | 没有配置文件时的上游默认地址 |
| `UPSTREAM_API_KEY` | 空 | 没有配置文件时的上游密钥 |
| `UPSTREAM_MODEL` | 空 | 没有配置文件时的实际模型 |
| `UPSTREAM_TIMEOUT_SECONDS` | `20` | 上游网络调用总时限，1–120 秒 |
| `UPSTREAM_MAX_TOKENS` | `256` | 最大输出 Token，32–2048 |
| `AUDIT_PROMPT` | 内置审核策略 | 没有配置文件时的审核提示词 |

配置文件存在时完全优先于 `UPSTREAM_*`/`AUDIT_PROMPT`；其他运行时变量仍在启动时读取。配置文件损坏、权限错误或校验失败时，不会回退到另一网关。修复文件并重启，或通过管理网页重新保存配置。

## HTTP API

设置 `BASE_PATH` 后，除 Docker 根路径 `/healthz` 外，下列路径需添加此前缀。

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| GET | `/` | 无 | 管理网页静态入口 |
| GET | `/healthz` | 无 | 进程健康检查 |
| GET | `/readyz` | 无 | 配置就绪检查 |
| GET / PUT | `/api/config` | ADMIN_TOKEN | 读取脱敏配置 / 保存 |
| GET | `/api/status` | ADMIN_TOKEN | 运行与存储状态 |
| POST | `/api/test` | ADMIN_TOKEN | 手动审核测试 |
| GET / DELETE | `/api/logs` | ADMIN_TOKEN | 最近日志 / 清空 |
| GET | `/api/statistics` | ADMIN_TOKEN | 日志窗口统计 |
| GET | `/v1/models` | AUDITER_TOKEN | sub2api 节点探测 |
| POST | `/v1/chat/completions` | AUDITER_TOKEN | 审核转换 |

同时兼容 `/models` 与 `/chat/completions`。使用 `Authorization: Bearer <对应令牌>`。服务不向 sub2api 发送额外反馈请求；sub2api 对结果的后续处理不会反向给 Auditer 返回 HTTP 状态码。

## 性能、安全与开发

采用 Starlette/Uvicorn 异步服务、进程级 httpx 连接池、配置内存快照、固定完成日志窗口及响应后线程写 SQLite。上游非流式，不隐式重试、不跟随重定向、不继承系统 HTTP_PROXY/HTTPS_PROXY。上游响应正文最大 512 KiB。

端到端性能应按日志实测，不以语言或单元测试耗时推断生产 QPS。总超时约束异步上游网络调用；本地同步解析不是可抢占任务。

公网部署必须设置两个访问令牌、使用 HTTPS 并保护 `data/` 和 `.env`。管理者能改变审核网关地址，应把管理令牌视为高权限凭据。默认不限制上游只能访问公网地址，以便支持自建内网模型。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
pytest -q
node --check src/sub2api_auditer/static/app.js
node --test tests/test_frontend.cjs
sub2api-auditer --host 127.0.0.1 --port 8080
```

需要 Python 3.11+；Node 22 用于前端回归测试，运行服务和页面不需要 Node、CDN 或前端构建步骤。GitHub Actions 测试 Python 3.11/3.12/3.13 和前端，全部成功后才构建镜像。PR 仅构建验证；当前 main 提交发布 latest 和 sha 标签。

## 许可证与来源

MIT License。仓库最初 fork 自 Petsitter，现为独立的 sub2api Prompt Audit 适配器，保留原项目许可证声明。
