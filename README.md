# Doubao Free API

将豆包桌面客户端的对话能力包装为 OpenAI 兼容的标准 API，供 Claude Code、OpenCode 等 AI Agent 软件直接调用。

## ✨ 功能特性

- 🔄 **OpenAI 兼容** — 完全兼容 `/v1/chat/completions` 接口，支持流式/非流式
- 🖼️ **Vision 图片识别** — 支持 OpenAI Vision 格式，自动上传图片到豆包 ImageX
- 🧠 **思考模式** — 深度推理，边想边搜
- 💻 **编程模式** — 基于 Doubao-Seed-Code 的代码生成
- ✍️ **写作/翻译/解题** — 6种特殊模式，参数切换
- 📊 **管理面板** — Web UI 状态监控、在线对话、日志查看
- 👥 **多账号池** — Cookie 轮询 + 自动故障转移
- 📝 **对话日志** — 自动记录每次输入输出

## 快速开始

### 1. 安装依赖

```bash
pip install aiohttp fastapi uvicorn httpx pycryptodome requests-aws4auth python-multipart
```

### 2. 提取 Cookie

确保豆包桌面客户端已登录，然后运行提取脚本：

```bash
python temp/extract_session.py
```

脚本会自动从豆包客户端提取 Cookie 和设备参数，并写入 `config.json`。

### 3. 启动服务

```bash
python main.py
```

服务启动后监听 `http://localhost:8765`，浏览器打开即可使用管理面板。

### 4. 验证服务

```bash
# 健康检查
curl http://localhost:8765/health

# 查看模型列表
curl http://localhost:8765/v1/models

# 非流式对话
curl -X POST http://localhost:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-pro-chat","messages":[{"role":"user","content":"你好"}]}'

# 流式对话
curl -X POST http://localhost:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"doubao-pro-chat","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

## 对接 AI Agent

### Claude Code

```bash
OPENAI_API_BASE=http://localhost:8765/v1 OPENAI_API_KEY=sk-doubao claude
```

### OpenCode

```json
{
  "provider": "openai",
  "api_base": "http://localhost:8765/v1",
  "api_key": "any-string",
  "model": "doubao-pro-chat"
}
```

### Python SDK

```python
from openai import OpenAI

client = OpenAI(api_key="any-string", base_url="http://localhost:8765/v1")

# 非流式
response = client.chat.completions.create(
    model="doubao-pro-chat",
    messages=[{"role": "user", "content": "你好"}]
)
print(response.choices[0].message.content)

# 流式
stream = client.chat.completions.create(
    model="doubao-pro-chat",
    messages=[{"role": "user", "content": "你好"}],
    stream=True
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

## 可用模型

| 模型 ID | 模式 | 说明 |
|---|---|---|
| `doubao-pro-chat` | 快速模式 | 默认，Doubao-Seed-2.0-Mini |
| `doubao-thinking` | 思考模式 | 深度推理，边想边搜 |
| `doubao-expert` | 超能模式 | 自动搜索+深度分析 |
| `doubao-coding` | 编程模式 | Doubao-Seed-Code 代码生成 |
| `doubao-writing` | 写作助手 | 公文/邮件/文案/小说 |
| `doubao-translator` | 翻译 | 多语言互译 |
| `doubao-tutor` | 解题答疑 | 数学/物理/化学逐步解题 |
| `doubao-lite-chat` | 轻量模式 | 轻量快速 |
| `doubao-pro-32k` | Pro 32K | 长上下文 |
| `doubao-pro-128k` | Pro 128K | 超长上下文 |

## Vision 图片识别

支持 OpenAI Vision API 格式，自动上传图片到豆包 ImageX 服务：

```bash
# URL 图片
curl -X POST http://localhost:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "doubao-pro-chat",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "描述这张图片"},
        {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
      ]
    }]
  }'
```

```python
# Python SDK - base64 图片
import base64
from openai import OpenAI

client = OpenAI(api_key="any-string", base_url="http://localhost:8765/v1")

with open("photo.jpg", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

response = client.chat.completions.create(
    model="doubao-pro-chat",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "描述这张图片"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        ]
    }]
)
```

支持两种图片输入：
- **HTTP URL**：`https://...` — 自动下载并上传
- **Base64 Data URL**：`data:image/png;base64,...` — 自动解码并上传

## API 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/v1/chat/completions` | POST | 对话补全（流式/非流式） |
| `/v1/models` | GET | 模型列表（含能力描述） |
| `/v1/images/upload` | POST | 上传图片文件 |
| `/health` | GET | 健康检查 + 功能状态 |
| `/logs/today` | GET | 查看今日对话日志 |
| `/logs/{date}` | GET | 查看指定日期日志 |
| `/accounts` | GET/POST | 账号池管理 |
| `/accounts/{name}` | DELETE | 删除账号 |
| `/conversations/{id}` | GET | 查看会话状态 |

## 管理面板

浏览器打开 `http://localhost:8765` 即可使用管理面板：

- 📊 **状态页** — 服务状态、账号健康度、模型列表
- 💬 **对话页** — 在线对话测试，支持图片上传
- 📋 **日志页** — 按日期/关键词/模型筛选日志
- 👤 **账号页** — Cookie 账号池管理
- ⚙️ **配置页** — API 使用说明

## 项目结构

```
doubao-api/
├── main.py              # FastAPI 主服务 (V3.0)
├── uploader.py          # 图片上传模块 (ImageX 4步流程)
├── signer.py            # Playwright 签名模块 (B2, 实验性)
├── config.json          # 会话配置 (自动生成)
├── config.example.json  # 配置示例
├── index.html           # 管理面板
├── logs/                # 对话日志 (自动生成)
├── conversations/       # 会话状态 (自动生成)
└── temp/                # 临时文件
    └── extract_session.py  # Cookie 提取脚本
```

## 技术原理

使用 **x-flow-trace 绕过方案（B3）**，无需伪造签名：

1. 从豆包客户端提取 Cookie 和设备参数
2. 构造包含 `x-flow-trace`、`agw-js-conv: str` 等请求头的请求
3. 直接调用豆包 `/samantha/chat/completion` API
4. 将三层嵌套 SSE 响应转换为 OpenAI 标准格式

### 签名方案对比

| 方案 | 方法 | 结果 | 原因 |
|---|---|---|---|
| B3 | x-flow-trace 绕过 | ✅ 成功 | 内部调试头绕过签名验证 |
| B2 | Playwright 生成 X-Bogus | ❌ 失败 | 触发服务端浏览器指纹验证 |
| B1 | 纯 Python a_bogus | ❌ 不可行 | 缺乏浏览器环境 |

### 图片上传流程

```
1. prepare_upload → 获取 AWS 临时凭证
2. ApplyImageUpload → 获取 TOS 存储地址
3. Upload Binary → 上传图片二进制 (CRC32 校验)
4. CommitImageUpload → 确认上传，获取 ImageUri
```

## Cookie 过期处理

Cookie 有效期约 30 天。过期后重新提取即可：

```bash
python temp/extract_session.py
```

脚本会自动更新 `config.json`，无需手动编辑。

## 注意事项

- 仅供学习研究，请勿用于商业用途
- Cookie 属于敏感信息，请勿泄露（`config.json` 已加入 .gitignore）
- 豆包 API 可能随时变更，导致服务失效
- 建议不要高频调用，以免触发风控

## License

MIT
