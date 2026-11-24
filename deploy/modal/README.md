# 在 Modal 上部署 OpenTalking

这里的文件把 OpenTalking 部署到 [Modal](https://modal.com)，按下面的四步顺序推进。
每一步都可以单独停下来看效果，花费从几乎为零逐步增加。

| 步骤 | 内容 | 硬件 | 预计花费 |
|---|---|---|---|
| 1 | mock 模式 + TURN，打通 Modal 上的 WebRTC | CPU | ≈ $0.05/小时 |
| 2 | 换 LLM、加知识库，用问题集评测客服 QA | CPU（同第 1 步） | 只有 LLM API 费用 |
| 3 | QuickTalk 实时口型 | L4 GPU | ≈ $0.9/小时 |
| 4 | FlashTalk-14B 离线生成 20 秒样片（手势 + 口型） | H100 | 每条 ≈ $0.3–0.7（另见文末） |

## 文件

| 文件 | 作用 |
|---|---|
| `realtime_app.py` | Modal app：单容器运行 `opentalking-unified`（API + worker）+ nginx（WebUI 和反向代理） |
| `modal_entrypoint.py` | 容器启动逻辑：生成 TURN 凭据、basic auth、首次启动时复制头像、启动并监控进程 |
| `nginx.conf` | `/` 提供 WebUI，`/api/*` 和 `/streaming/*` 反代到 127.0.0.1:8000，SSE 不缓冲 |
| `download_models.py` | 用 CPU 容器把权重下载到 `opentalking-models` Volume |
| `qa_eval.py` + `qa_cases.example.yaml` | 用真实会话链路批量测客服问答的质量和延迟 |
| `secrets.env.example` | Modal secret 模板（LLM / STT / TTS / TURN / 访问密码） |

## 架构

```
浏览器 ──HTTPS──> https://<workspace>--opentalking-<mode>-web.modal.run
                    nginx :8080 ── /        WebUI（apps/web/dist）
                               └─ /api/*   → opentalking-unified :8000
浏览器 <──WebRTC 媒体──> Cloudflare TURN（turns:443） <──> 容器内的 aiortc
容器挂载：/app/data ← Volume opentalking-data（知识库、记忆、导出、头像）
          /models   ← Volume opentalking-models（QuickTalk / FlashTalk 权重）
```

为什么这样设计：

- **单容器（`max_containers=1`）**：unified 模式把会话、任务队列和 WebRTC peer 都放在一个进程的内存里，请求一旦落到第二个容器就会出现随机 404。需要多路并发时，再换成 API + worker + Redis 的拆分部署。
- **必须配 TURN**：Modal 只能通过 HTTPS 或 TCP tunnel 进入容器，没有入站 UDP。启动脚本会让浏览器只走中继（`iceTransportPolicy=relay`），服务端 aiortc 默认通过 `turns:…:443` 连接 TURN。
- **权重放在 Volume，不打进镜像**：镜像小、构建快，FlashTalk 的几十 GB 权重只需下载一次。

## 0. 准备

```bash
pip install modal
```

```bash
modal setup
```

**TURN**：推荐 Cloudflare Realtime TURN（有免费额度）。在 Cloudflare 控制台打开 Realtime → TURN Server，创建一个 key，记下 Key ID 和 API Token。
容器每次启动时会用它们换取短期 ICE 凭据，默认有效期 48 小时，可用 `OT_TURN_TTL_SEC` 调整。
其他 TURN 服务（metered.ca、Twilio、自建 coturn）也可以，直接在 secret 里写 `OPENTALKING_WEBRTC_ICE_SERVERS` 即可。

**Secret**：

```bash
cp deploy/modal/secrets.env.example deploy/modal/secrets.env
```

编辑 `deploy/modal/secrets.env`，至少填好 LLM key、STT key、TURN 凭据和访问密码。这个文件已加入 `.gitignore`。

注意：默认 STT 是 DashScope，没有 `OPENTALKING_STT_DASHSCOPE_API_KEY` 时**创建会话会直接返回 400**，纯文字对话也一样。不打算用语音输入时，可以先随便填一个值占位。

然后创建 secret：

```bash
modal secret create opentalking-env --from-dotenv deploy/modal/secrets.env --force
```

以后改了配置，重新执行上面这条命令，再重新部署即可。

## 第 1 步：mock 模式，打通 WebRTC

```bash
modal deploy deploy/modal/realtime_app.py
```

第一次构建镜像大约需要 10–15 分钟（Python 依赖和前端构建），之后只要依赖没变就会走缓存。
打开命令输出里的 `https://…--opentalking-mock-web.modal.run`，输入 basic auth 账号密码，在 WebUI 里选择 mock 模型和任意头像，开始对话。

检查：

```bash
curl -u demo:change-me https://<url>/api/models
```

```bash
curl -u demo:change-me https://<url>/api/sessions/webrtc/ice-config
```

`ice-config` 的返回里应该能看到 `turn:` / `turns:` 地址，并且 `iceTransportPolicy` 是 `relay`。

验收标准：页面能看到静态头像画面，能听到 TTS 声音，字幕正常滚动。

如果有声音和字幕、但没有画面，或者一直在连接：

1. 看容器日志（`modal app logs opentalking-mock`），找 `Server-side aiortc TURN` 那一行，确认 TURN 配置已生效。
2. 打开浏览器的 `chrome://webrtc-internals`，确认选中的 candidate pair 是 `relay` 类型。
3. 把 `OT_SERVER_TURN_TRANSPORT` 依次换成 `tcp` 或 `udp` 再试一次。`udp` 延迟最低，但需要 Modal 允许出站 UDP。

不用的时候容器会在 5 分钟后自动缩到 0（可用 `OT_SCALEDOWN_SEC` 调整）。之后第一次访问要冷启动，大约 30–60 秒。

## 第 2 步：调教客服 QA

“聪明”主要取决于下面三件事，跟渲染模型无关，所以继续在 mock 模式下迭代，成本最低：

1. **LLM**：在 secret 里改 `OPENTALKING_LLM_BASE_URL` / `OPENTALKING_LLM_MODEL`。任何 OpenAI 兼容接口都可以，例如 DashScope qwen-plus/max、DeepSeek，或者通过 Anthropic 的 OpenAI 兼容端点接入 Claude。客服场景要同时看准确率和首字延迟，建议拿同一套问题集横向对比两三个模型。
2. **知识库**：在 WebUI 的“知识库”页面上传 FAQ、产品手册、退换货政策等文档（存在 Volume 里，重启不会丢）。会话里勾选这个知识库，或者把它的 id 写进问题集的 `knowledge_base_ids`。
   注意：LightRAG 的 embedding 默认复用 LLM 的 base_url 和 key。如果你用的 LLM 服务没有 embedding 接口，要单独配置 `OPENTALKING_AGENT_LIGHTRAG_EMBEDDING_*`。
3. **System prompt / Persona**：要求回答简短、口语化、适合播报；知识库查不到就转人工，不要编造。模板见 `secrets.env.example` 和 `qa_cases.example.yaml`。

批量评测（在你本机运行，只需要先装 `pip install httpx pyyaml`）：

```bash
python deploy/modal/qa_eval.py --base-url https://<url>/api --auth demo:change-me --cases deploy/modal/qa_cases.example.yaml --out qa_results.jsonl
```

脚本会对每个问题新建会话，通过 `/speak` 提问，再从 SSE 读取 `subtitle.chunk` 和 `speech.ended`（完整回复）。不需要建立 WebRTC 连接。
`first_subtitle_ms` 是从提问到第一句回复的 TTS 音频准备好的时间（LLM 首句 + TTS 首包），最接近用户实际感受到的“开口延迟”。TTS 失败时不会有字幕，这一列会是空的。
关键词检查（`expect_any` / `expect_all` / `forbid`）只是冒烟测试，回复内容还是要人工看一遍。
问题集建议包含：知识库内的问题、需要多轮追问的问题、超出范围应转人工的问题、情绪激动的客户、提示词注入。

## 第 3 步：QuickTalk 实时口型（L4）

先把权重下载到 Volume（只需一次，CPU 容器，几分钟）：

```bash
modal run deploy/modal/download_models.py::quicktalk
```

然后部署 GPU 版本。它是一个独立的 app（`opentalking-quicktalk`），和 mock 版共享知识库 Volume：

```bash
OT_MODE=quicktalk modal deploy deploy/modal/realtime_app.py
```

如果 L4 的帧率达不到 25 fps，可以换成 A10 重新部署：

```bash
OT_MODE=quicktalk OT_GPU=A10 modal deploy deploy/modal/realtime_app.py
```

第一次使用某个头像时，QuickTalk 会先生成模板和人脸缓存，需要等一会儿。缓存写在 `/app/data/avatars`，之后会复用。

测完记得停掉，避免有人访问时又被唤醒（停掉 mock 版同理）：

```bash
modal app stop opentalking-quicktalk
```

延迟和帧率可以用仓库自带的 `scripts/benchmark_opentalking_e2e.py --api-base-url https://<url>/api` 测量。注意：它的 aiortc 客户端需要你本机能连到 TURN，而且目前不支持 basic auth，测之前可以临时去掉访问密码。

## 第 4 步：FlashTalk-14B 20 秒样片（手势 + 口型）

先下载权重（大文件，CPU 容器，可能要 30–60 分钟）：

```bash
modal run deploy/modal/download_models.py::flashtalk
```

后续还会补上 `flashtalk_sample.py`：在 H100 上启动 OmniRT FlashTalk，通过 `/video-creation` 生成 mp4 并写回 Volume。
要点：官方说明不开 offload 需要 64GB 以上显存（开 `--cpu_offload` 约 40GB），所以选 H100 80GB。
OmniRT 的 CUDA 环境（CUDA 12.8 torch + `flash_attn==2.8.0.post2`）建议先用 `modal shell --gpu H100` 手动调通，再固化进镜像。
参考图要用半身、手在画面里的照片，FlashTalk 才有机会生成手部动作。

## 已知限制和注意事项

- **SQLite / LightRAG 放在 Modal Volume 上**：单容器下没问题，但不要同时部署多个副本写同一个 Volume。
- **SSE 长连接**：前端的 EventSource 会自动重连；如果遇到 Modal 网关的超时断开，日志里会看到重连，属于正常现象。
- **安全**：务必设置 `OT_BASIC_AUTH_*`。否则任何拿到 URL 的人都能消耗你的 LLM 和 GPU 额度。
- **并发**：单容器、单 GPU，适合演示和小规模试用，不适合生产流量。
