import json
import os
import uuid
import time
import logging
import asyncio
import base64
import re
from datetime import datetime
from typing import AsyncGenerator, Optional, Union
from urllib.parse import urlencode

import aiohttp
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("doubao-api")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
ACCOUNTS_PATH = os.path.join(BASE_DIR, 'accounts.json')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
CONVERSATION_DIR = os.path.join(BASE_DIR, 'conversations')

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(CONVERSATION_DIR, exist_ok=True)

def load_config():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)

def load_accounts():
    if os.path.exists(ACCOUNTS_PATH):
        with open(ACCOUNTS_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_accounts(accounts):
    with open(ACCOUNTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)

CONFIG = load_config()
ACCOUNTS = load_accounts()

SIGN_METHOD = CONFIG.get('sign_method', 'b3')

signer = None
if SIGN_METHOD == 'b2':
    try:
        from signer import PlaywrightSigner
        signer = PlaywrightSigner(
            cookie=CONFIG.get('cookie', ''),
            device_id=CONFIG.get('device_id', ''),
            web_id=CONFIG.get('web_id', ''),
            tea_uuid=CONFIG.get('tea_uuid', ''),
            fp=CONFIG.get('fp', '')
        )
        logger.info("B2 Playwright signer module loaded (will initialize on startup)")
    except ImportError as e:
        logger.error(f"Failed to import signer module: {e}")
        SIGN_METHOD = 'b3'
        signer = None

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"

MODEL_CONFIG = {
    "doubao-pro-chat": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "快速模式 (Doubao-Seed-2.0-Mini)"},
    "doubao-lite-chat": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "轻量模式"},
    "doubao-thinking": {"bot_id": "7338286299411103781", "use_deep_think": True, "use_auto_cot": False, "desc": "思考模式 (Doubao-Seed-2.0-lite)"},
    "doubao-expert": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": True, "desc": "专家/超能模式"},
    "doubao-pro-32k": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "Pro 32K"},
    "doubao-pro-128k": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "Pro 128K"},
    "doubao-coding": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": True, "desc": "编程模式 (Doubao-Seed-Code)"},
    "doubao-writing": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "写作助手"},
    "doubao-translator": {"bot_id": "7338286299411103781", "use_deep_think": False, "use_auto_cot": False, "desc": "翻译"},
    "doubao-tutor": {"bot_id": "7338286299411103781", "use_deep_think": True, "use_auto_cot": False, "desc": "解题答疑"},
}

SYSTEM_PROMPT_MAP = {
    "doubao-coding": "你是一个专业的编程助手，擅长多种编程语言，能够编写、调试、优化代码，并解释技术概念。请用代码块格式输出代码。",
    "doubao-writing": "你是一个专业的写作助手，擅长各类文体写作，包括公文、邮件、文案、小说、论文等。请根据用户需求生成高质量的结构化文本。",
    "doubao-translator": "你是一个专业的翻译助手，支持多语言互译，自动检测源语言，保持原文语义和语气。请直接输出翻译结果，不要添加额外解释。",
    "doubao-tutor": "你是一个专业的解题答疑老师，擅长数学、物理、化学等学科。请逐步分析问题，给出详细的解题过程和答案，标注关键步骤和易错点。",
}

app = FastAPI(title="Doubao Free API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    global signer, SIGN_METHOD
    if SIGN_METHOD == 'b2' and signer:
        logger.info("Initializing B2 Playwright signer (this may take 30-60s)...")
        success = await signer.initialize()
        if success:
            logger.info("B2 Playwright signer initialized successfully")
        else:
            logger.error("B2 Playwright signer initialization failed, falling back to B3")
            SIGN_METHOD = 'b3'
    logger.info(f"Active sign method: {SIGN_METHOD}")

@app.on_event("shutdown")
async def shutdown_event():
    if signer:
        await signer.close()

class ChatMessage(BaseModel):
    role: str = "user"
    content: Union[str, list] = ""

class ChatCompletionRequest(BaseModel):
    model: str = "doubao-pro-chat"
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=1, le=32768)

class CookiePool:
    def __init__(self):
        self.accounts = []
        self.current_index = 0
        self._init_pool()

    def _init_pool(self):
        primary = {
            "name": "primary",
            "cookie": CONFIG.get('cookie', ''),
            "device_id": CONFIG.get('device_id', ''),
            "web_id": CONFIG.get('web_id', ''),
            "tea_uuid": CONFIG.get('tea_uuid', ''),
            "room_id": CONFIG.get('room_id', ''),
            "fail_count": 0,
            "last_fail": None,
            "enabled": True
        }
        self.accounts = [primary]
        for acc in ACCOUNTS:
            self.accounts.append({
                "name": acc.get("name", "unknown"),
                "cookie": acc.get("cookie", ""),
                "device_id": acc.get("device_id", CONFIG.get('device_id', '')),
                "web_id": acc.get("web_id", CONFIG.get('web_id', '')),
                "tea_uuid": acc.get("tea_uuid", CONFIG.get('tea_uuid', '')),
                "room_id": acc.get("room_id", CONFIG.get('room_id', '')),
                "fail_count": 0,
                "last_fail": None,
                "enabled": True
            })
        logger.info(f"Cookie pool initialized: {len(self.accounts)} accounts")

    def get_next(self) -> dict:
        available = [a for a in self.accounts if a["enabled"] and a["cookie"]]
        if not available:
            logger.warning("No available accounts, re-enabling all")
            for a in self.accounts:
                a["enabled"] = True
                a["fail_count"] = 0
            available = [a for a in self.accounts if a["cookie"]]
        if not available:
            return self.accounts[0] if self.accounts else {}

        account = available[self.current_index % len(available)]
        self.current_index = (self.current_index + 1) % len(available)
        return account

    def report_fail(self, account: dict):
        account["fail_count"] += 1
        account["last_fail"] = datetime.now().isoformat()
        if account["fail_count"] >= 3:
            account["enabled"] = False
            logger.warning(f"Account '{account['name']}' disabled after 3 failures")
        else:
            logger.warning(f"Account '{account['name']}' failure #{account['fail_count']}")

    def report_success(self, account: dict):
        account["fail_count"] = 0

    def status(self) -> list:
        return [{
            "name": a["name"],
            "enabled": a["enabled"],
            "fail_count": a["fail_count"],
            "last_fail": a["last_fail"],
            "cookie_length": len(a.get("cookie", ""))
        } for a in self.accounts]

cookie_pool = CookiePool()

def save_conversation_log(user_input: str, ai_output: str, model: str, conversation_id: str = "", chat_id: str = "", image_urls: list = None):
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    log_file = os.path.join(LOG_DIR, f"chat_{date_str}.jsonl")

    record = {
        "timestamp": now.isoformat(),
        "date": date_str,
        "time": time_str,
        "chat_id": chat_id,
        "conversation_id": conversation_id,
        "model": model,
        "user_input": user_input,
        "ai_output": ai_output,
        "output_length": len(ai_output),
        "image_urls": image_urls or []
    }

    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')

    logger.info(f"Conversation logged: {date_str} {time_str} | user={len(user_input)}chars | ai={len(ai_output)}chars | images={len(image_urls or [])}")

def save_conversation_state(chat_id: str, messages: list, doubao_conv_id: str = "", model: str = ""):
    state_file = os.path.join(CONVERSATION_DIR, f"{chat_id}.json")
    state = {
        "chat_id": chat_id,
        "doubao_conversation_id": doubao_conv_id,
        "model": model,
        "updated_at": datetime.now().isoformat(),
        "messages": [{"role": m.role, "content": m.content} if hasattr(m, 'role') else m for m in messages]
    }
    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_conversation_state(chat_id: str) -> dict:
    state_file = os.path.join(CONVERSATION_DIR, f"{chat_id}.json")
    if os.path.exists(state_file):
        with open(state_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def generate_x_flow_trace():
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex
    return json.dumps({"trace_id": trace_id, "span_id": span_id})

def build_url_params(account: dict):
    return "&".join([
        "aid=497858",
        f"device_id={account.get('device_id', CONFIG.get('device_id', ''))}",
        "device_platform=web",
        "language=zh",
        "pc_version=3.17.3",
        "pkg_type=release_version",
        "real_aid=497858",
        "region=CN",
        "samantha_web=1",
        "sys_region=CN",
        f"tea_uuid={account.get('tea_uuid', CONFIG.get('tea_uuid', ''))}",
        "use-olympus-account=1",
        "version_code=20800",
        f"web_id={account.get('web_id', CONFIG.get('web_id', ''))}"
    ])

def build_headers(account: dict):
    return {
        'content-type': 'application/json',
        'accept': 'text/event-stream',
        'agw-js-conv': 'str',
        'cookie': account.get('cookie', CONFIG.get('cookie', '')),
        'origin': 'https://www.doubao.com',
        'referer': f"https://www.doubao.com/chat/{account.get('room_id', CONFIG.get('room_id', ''))}",
        'user-agent': USER_AGENT,
        'x-flow-trace': generate_x_flow_trace()
    }

def extract_text_from_content(content: Union[str, list]) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    texts.append(item.get("text", ""))
        return "\n".join(texts)
    return str(content)

def extract_image_urls_from_content(content: Union[str, list]) -> list[str]:
    if isinstance(content, str):
        return []
    if isinstance(content, list):
        urls = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "image_url":
                url = item.get("image_url", {}).get("url", "")
                if url:
                    urls.append(url)
        return urls
    return []

async def upload_images_for_message(image_urls: list[str], account: dict) -> list[dict]:
    if not image_urls:
        return []

    from uploader import process_image_url
    attachments = []
    for url in image_urls:
        try:
            att = await process_image_url(
                image_url=url,
                cookie=account.get('cookie', CONFIG.get('cookie', '')),
                device_id=account.get('device_id', ''),
                tea_uuid=account.get('tea_uuid', ''),
                web_id=account.get('web_id', '')
            )
            attachments.append(att)
            logger.info(f"Image uploaded for chat: {att.get('key', '')}")
        except Exception as e:
            logger.error(f"Failed to upload image: {e}")
    return attachments

def build_request_body(messages: list[ChatMessage], conversation_id: str = "0",
                       model: str = "doubao-pro-chat", attachments: list[dict] = None):
    last_msg = messages[-1] if messages else None
    last_text = extract_text_from_content(last_msg.content) if last_msg else ""
    need_create = conversation_id == "0"

    model_cfg = MODEL_CONFIG.get(model, MODEL_CONFIG["doubao-pro-chat"])
    bot_id = model_cfg.get("bot_id", "7338286299411103781")
    use_deep_think = model_cfg.get("use_deep_think", False)
    use_auto_cot = model_cfg.get("use_auto_cot", False)

    system_prompt = SYSTEM_PROMPT_MAP.get(model, "")

    body_messages = []
    for msg in messages:
        text = extract_text_from_content(msg.content)
        msg_attachments = []

        if isinstance(msg.content, list):
            img_urls = extract_image_urls_from_content(msg.content)
            if img_urls and msg == last_msg and attachments:
                msg_attachments = attachments

        body_messages.append({
            "content": json.dumps({"text": text}, ensure_ascii=False),
            "content_type": 2001,
            "attachments": msg_attachments,
            "references": []
        })

    if system_prompt and need_create:
        body_messages.insert(0, {
            "content": json.dumps({"text": system_prompt}, ensure_ascii=False),
            "content_type": 2001,
            "attachments": [],
            "references": []
        })

    return {
        "bot_id": bot_id,
        "completion_option": {
            "is_regen": False,
            "with_suggest": False,
            "need_create_conversation": need_create,
            "launch_stage": 1,
            "use_auto_cot": use_auto_cot,
            "use_deep_think": use_deep_think
        },
        "conversation_id": conversation_id,
        "local_conversation_id": f"local_{uuid.uuid4().int % 10000000000000000}",
        "local_message_id": str(uuid.uuid4()),
        "messages": body_messages[-1:] if need_create else body_messages,
        "ext": {
            "fp": CONFIG.get('fp', '')
        }
    }

def parse_sse_line(line: str):
    if line.startswith('data:'):
        data_str = line[5:].strip()
        if data_str == '[DONE]':
            return None
        try:
            outer = json.loads(data_str)
            event_type = outer.get("event_type")
            raw_event_data = outer.get("event_data", "")
            if isinstance(raw_event_data, str) and raw_event_data:
                try:
                    inner = json.loads(raw_event_data)
                except json.JSONDecodeError:
                    inner = {}
            else:
                inner = raw_event_data if isinstance(raw_event_data, dict) else {}
            return {"event_type": event_type, "data": inner, "event_id": outer.get("event_id")}
        except json.JSONDecodeError:
            return None
    return None

def extract_text_from_event(parsed: dict) -> str:
    if not parsed:
        return ""
    event_type = parsed.get("event_type")
    data = parsed.get("data", {})

    if event_type == 2001:
        message = data.get("message", {})
        content_type = message.get("content_type")
        if content_type in (10000, 2001, 2008, 2071):
            raw_content = message.get("content", "")
            if isinstance(raw_content, str) and raw_content:
                try:
                    content_parsed = json.loads(raw_content)
                    return content_parsed.get("text", "")
                except json.JSONDecodeError:
                    return raw_content
            elif isinstance(raw_content, dict):
                return raw_content.get("text", "")
    return ""

def extract_image_urls_from_event(parsed: dict) -> list[str]:
    if not parsed:
        return []
    event_type = parsed.get("event_type")
    data = parsed.get("data", {})

    if event_type == 2001:
        message = data.get("message", {})
        content_type = message.get("content_type")
        if content_type == 2074:
            raw_content = message.get("content", "")
            if isinstance(raw_content, str):
                try:
                    content_parsed = json.loads(raw_content)
                except json.JSONDecodeError:
                    return []
            elif isinstance(raw_content, dict):
                content_parsed = raw_content
            else:
                return []

            image_urls = []
            creations = content_parsed.get("creations", [])
            for creation in creations:
                image_info = creation.get("image", {})
                if image_info.get("status") == 2:
                    url = (image_info.get("image_raw", {}).get("url") or
                           image_info.get("image_thumb", {}).get("url") or
                           image_info.get("image_ori", {}).get("url"))
                    if url and url not in image_urls:
                        image_urls.append(url)
            return image_urls
    return []

def extract_conversation_id(parsed: dict) -> str:
    data = parsed.get("data", {})
    return data.get("conversation_id", "")

def format_openai_chunk(content: str, model: str, chat_id: str) -> str:
    chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": content},
            "finish_reason": None
        }]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

def format_openai_done() -> str:
    return "data: [DONE]\n\n"

def is_cookie_expired(status_code: int, body: str) -> bool:
    if status_code == 401 or status_code == 403:
        return True
    expired_keywords = ["login", "session expired", "unauthorized", "need_login", "csrf"]
    body_lower = body.lower()
    return any(kw in body_lower for kw in expired_keywords)

async def call_doubao_api(messages: list[ChatMessage], conversation_id: str = "0",
                          model: str = "doubao-pro-chat", max_retries: int = 2,
                          attachments: list[dict] = None) -> AsyncGenerator[bytes, None]:
    account = cookie_pool.get_next()
    body = build_request_body(messages, conversation_id, model, attachments)

    if SIGN_METHOD == 'b2' and signer and signer._initialized:
        base_params = {
            "aid": "497858",
            "device_platform": "web",
            "language": "zh",
            "pc_version": "3.17.3",
            "pkg_type": "release_version",
            "real_aid": "497858",
            "region": "CN",
            "samantha_web": "1",
            "sys_region": "CN",
            "use-olympus-account": "1",
            "version_code": "20800",
        }
        signed_url = await signer.get_signed_url(
            f"{CONFIG['api_base']}/samantha/chat/completion",
            base_params
        )
        if signed_url:
            url = signed_url
            logger.info(f"Using B2 signed URL with a_bogus")
        else:
            logger.warning("B2 signing failed, falling back to B3")
            params = build_url_params(account)
            url = f"{CONFIG['api_base']}/samantha/chat/completion?{params}"
    else:
        params = build_url_params(account)
        url = f"{CONFIG['api_base']}/samantha/chat/completion?{params}"

    headers = build_headers(account)

    logger.info(f"Calling Doubao API: conv_id={conversation_id}, account={account['name']}, method={SIGN_METHOD}, model={model}")

    for attempt in range(max_retries + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url=url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    logger.info(f"Doubao API response status: {resp.status}")

                    if SIGN_METHOD == 'b2' and signer:
                        new_ms_token = resp.headers.get('x-ms-token', '')
                        if new_ms_token:
                            signer.update_ms_token(new_ms_token)

                    if resp.status != 200:
                        error_body = await resp.text()
                        logger.error(f"Doubao API error: {resp.status} - {error_body[:500]}")

                        if is_cookie_expired(resp.status, error_body):
                            cookie_pool.report_fail(account)
                            if attempt < max_retries:
                                account = cookie_pool.get_next()
                                headers = build_headers(account)
                                logger.info(f"Retrying with account: {account['name']}")
                                continue
                        yield json.dumps({"error": True, "status": resp.status, "body": error_body[:500]}).encode()
                        return

                    cookie_pool.report_success(account)
                    async for chunk in resp.content.iter_chunked(1024):
                        yield chunk
                    return
        except asyncio.TimeoutError:
            logger.error(f"Doubao API timeout (attempt {attempt + 1})")
            if attempt < max_retries:
                account = cookie_pool.get_next()
                headers = build_headers(account)
                continue
            yield json.dumps({"error": True, "status": 0, "body": "Request timeout after retries"}).encode()
            return
        except Exception as e:
            logger.error(f"Doubao API exception: {e}")
            if attempt < max_retries:
                account = cookie_pool.get_next()
                headers = build_headers(account)
                continue
            yield json.dumps({"error": True, "status": 0, "body": str(e)}).encode()
            return

async def stream_chat_completion(request: ChatCompletionRequest):
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    full_text = ""
    all_image_urls = []
    conversation_id = "0"
    user_input = extract_text_from_content(request.messages[-1].content) if request.messages else ""
    buffer = ""

    last_msg = request.messages[-1] if request.messages else None
    image_urls = extract_image_urls_from_content(last_msg.content) if last_msg and isinstance(last_msg.content, list) else []
    attachments = None

    if image_urls:
        try:
            account = cookie_pool.get_next()
            attachments = await upload_images_for_message(image_urls, account)
            if attachments:
                logger.info(f"Uploaded {len(attachments)} images for vision request")
        except Exception as e:
            logger.error(f"Image upload failed, continuing without images: {e}")

    try:
        async for raw_chunk in call_doubao_api(request.messages, conversation_id, request.model, attachments=attachments):
            try:
                buffer += raw_chunk.decode('utf-8', errors='replace')
            except:
                continue

            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                line = line.strip()
                if not line:
                    continue

                event_data = parse_sse_line(line)
                if event_data is None:
                    save_conversation_log(user_input, full_text, request.model, conversation_id, chat_id, all_image_urls)
                    save_conversation_state(chat_id, request.messages, conversation_id, request.model)
                    yield format_openai_done()
                    return

                extracted = extract_text_from_event(event_data)
                if extracted:
                    full_text += extracted
                    yield format_openai_chunk(extracted, request.model, chat_id)

                img_urls = extract_image_urls_from_event(event_data)
                if img_urls:
                    all_image_urls.extend(img_urls)
                    for img_url in img_urls:
                        img_markdown = f"\n![image]({img_url})\n"
                        full_text += img_markdown
                        yield format_openai_chunk(img_markdown, request.model, chat_id)

                conv_id = extract_conversation_id(event_data)
                if conv_id:
                    conversation_id = conv_id

                if event_data.get("event_type") == 2003:
                    save_conversation_log(user_input, full_text, request.model, conversation_id, chat_id, all_image_urls)
                    save_conversation_state(chat_id, request.messages, conversation_id, request.model)
                    yield format_openai_chunk("", request.model, chat_id).replace('"finish_reason": null', '"finish_reason": "stop"')
                    yield format_openai_done()
                    return
    except Exception as e:
        logger.error(f"Stream error: {e}")
        if not full_text:
            yield format_openai_chunk(f"[Error: {str(e)}]", request.model, chat_id)

    if buffer.strip():
        line = buffer.strip()
        event_data = parse_sse_line(line)
        if event_data is not None:
            extracted = extract_text_from_event(event_data)
            if extracted:
                full_text += extracted
                yield format_openai_chunk(extracted, request.model, chat_id)

            img_urls = extract_image_urls_from_event(event_data)
            if img_urls:
                all_image_urls.extend(img_urls)

    save_conversation_log(user_input, full_text, request.model, conversation_id, chat_id, all_image_urls)
    save_conversation_state(chat_id, request.messages, conversation_id, request.model)
    yield format_openai_done()

async def non_stream_chat_completion(request: ChatCompletionRequest):
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    full_text = ""
    all_image_urls = []
    conversation_id = "0"
    user_input = extract_text_from_content(request.messages[-1].content) if request.messages else ""
    buffer = ""

    last_msg = request.messages[-1] if request.messages else None
    image_urls = extract_image_urls_from_content(last_msg.content) if last_msg and isinstance(last_msg.content, list) else []
    attachments = None

    if image_urls:
        account = cookie_pool.get_next()
        attachments = await upload_images_for_message(image_urls, account)
        if attachments:
            logger.info(f"Uploaded {len(attachments)} images for vision request")

    async for raw_chunk in call_doubao_api(request.messages, conversation_id, request.model, attachments=attachments):
        try:
            buffer += raw_chunk.decode('utf-8', errors='replace')
        except:
            continue

        while '\n' in buffer:
            line, buffer = buffer.split('\n', 1)
            line = line.strip()
            if not line:
                continue

            event_data = parse_sse_line(line)
            if event_data is None:
                break

            extracted = extract_text_from_event(event_data)
            if extracted:
                full_text += extracted

            img_urls = extract_image_urls_from_event(event_data)
            if img_urls:
                all_image_urls.extend(img_urls)
                for img_url in img_urls:
                    full_text += f"\n![image]({img_url})\n"

            conv_id = extract_conversation_id(event_data)
            if conv_id:
                conversation_id = conv_id

            if event_data.get("event_type") == 2003:
                break

    save_conversation_log(user_input, full_text, request.model, conversation_id, chat_id, all_image_urls)
    save_conversation_state(chat_id, request.messages, conversation_id, request.model)

    result = {
        "id": chat_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": full_text},
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0
        }
    }

    if all_image_urls:
        result["images"] = all_image_urls

    return result

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    if request.stream:
        return StreamingResponse(
            stream_chat_completion(request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )
    else:
        result = await non_stream_chat_completion(request)
        return JSONResponse(content=result)

@app.get("/v1/models")
async def list_models():
    models = []
    for model_id, cfg in MODEL_CONFIG.items():
        models.append({
            "id": model_id,
            "object": "model",
            "owned_by": "doubao",
            "description": cfg.get("desc", ""),
            "capabilities": {
                "vision": True,
                "deep_think": cfg.get("use_deep_think", False),
                "auto_cot": cfg.get("use_auto_cot", False)
            }
        })
    return {"object": "list", "data": models}

@app.post("/v1/images/upload")
async def upload_image_endpoint(file: UploadFile = File(...)):
    from uploader import upload_image

    account = cookie_pool.get_next()
    file_data = await file.read()
    file_name = file.filename or "upload.png"

    try:
        attachment = await upload_image(
            file_data=file_data,
            file_name=file_name,
            cookie=account.get('cookie', CONFIG.get('cookie', '')),
            device_id=account.get('device_id', ''),
            tea_uuid=account.get('tea_uuid', ''),
            web_id=account.get('web_id', '')
        )
        return {"status": "ok", "attachment": attachment}
    except Exception as e:
        logger.error(f"Image upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    pool_status = cookie_pool.status()
    active_count = sum(1 for a in pool_status if a["enabled"])
    result = {
        "status": "ok" if active_count > 0 else "degraded",
        "version": "3.0.0",
        "cookie_set": bool(CONFIG.get('cookie')),
        "sign_method": SIGN_METHOD,
        "accounts_total": len(pool_status),
        "accounts_active": active_count,
        "accounts": pool_status,
        "models": list(MODEL_CONFIG.keys()),
        "features": {
            "vision": True,
            "image_upload": True,
            "deep_think": True,
            "expert_mode": True,
            "coding_mode": True,
            "writing_mode": True,
            "translation": True,
            "tutor_mode": True
        }
    }
    if SIGN_METHOD == 'b2' and signer:
        result["signer_initialized"] = signer._initialized
        result["ms_token_available"] = bool(signer.ms_token)
    return result

@app.get("/logs/today")
async def get_today_logs():
    date_str = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(LOG_DIR, f"chat_{date_str}.jsonl")
    if not os.path.exists(log_file):
        return {"date": date_str, "count": 0, "logs": []}
    records = []
    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except:
                    pass
    return {"date": date_str, "count": len(records), "logs": records}

@app.get("/logs/{date_str}")
async def get_date_logs(date_str: str):
    log_file = os.path.join(LOG_DIR, f"chat_{date_str}.jsonl")
    if not os.path.exists(log_file):
        return {"date": date_str, "count": 0, "logs": []}
    records = []
    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except:
                    pass
    return {"date": date_str, "count": len(records), "logs": records}

@app.get("/accounts")
async def list_accounts():
    return {"accounts": cookie_pool.status()}

@app.post("/accounts")
async def add_account(request: Request):
    body = await request.json()
    required = ["name", "cookie"]
    for field in required:
        if field not in body:
            raise HTTPException(status_code=400, detail=f"Missing field: {field}")

    new_account = {
        "name": body["name"],
        "cookie": body["cookie"],
        "device_id": body.get("device_id", CONFIG.get('device_id', '')),
        "web_id": body.get("web_id", CONFIG.get('web_id', '')),
        "tea_uuid": body.get("tea_uuid", CONFIG.get('tea_uuid', '')),
        "room_id": body.get("room_id", CONFIG.get('room_id', '')),
        "fail_count": 0,
        "last_fail": None,
        "enabled": True
    }
    cookie_pool.accounts.append(new_account)

    accounts_data = []
    for a in cookie_pool.accounts[1:]:
        accounts_data.append({
            "name": a["name"],
            "cookie": a["cookie"],
            "device_id": a.get("device_id", ""),
            "web_id": a.get("web_id", ""),
            "tea_uuid": a.get("tea_uuid", ""),
            "room_id": a.get("room_id", "")
        })
    save_accounts(accounts_data)

    return {"status": "ok", "message": f"Account '{body['name']}' added", "total": len(cookie_pool.accounts)}

@app.delete("/accounts/{name}")
async def remove_account(name: str):
    cookie_pool.accounts = [a for a in cookie_pool.accounts if a["name"] != name]
    accounts_data = []
    for a in cookie_pool.accounts[1:]:
        accounts_data.append({
            "name": a["name"],
            "cookie": a["cookie"],
            "device_id": a.get("device_id", ""),
            "web_id": a.get("web_id", ""),
            "tea_uuid": a.get("tea_uuid", ""),
            "room_id": a.get("room_id", "")
        })
    save_accounts(accounts_data)
    return {"status": "ok", "message": f"Account '{name}' removed", "total": len(cookie_pool.accounts)}

@app.get("/conversations/{chat_id}")
async def get_conversation(chat_id: str):
    state = load_conversation_state(chat_id)
    if not state:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return state

@app.get("/")
async def index():
    html_path = os.path.join(BASE_DIR, 'index.html')
    if os.path.exists(html_path):
        with open(html_path, 'r', encoding='utf-8') as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Doubao API</h1><p>index.html not found</p>")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CONFIG.get('server_host', '0.0.0.0'), port=CONFIG.get('server_port', 8765))
