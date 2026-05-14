import json
import uuid
import asyncio
import logging
from typing import AsyncGenerator, Union

import aiohttp
from urllib.parse import urlencode

from config import CONFIG, SIGN_METHOD, signer, cookie_pool
from models import ChatMessage, MODEL_CONFIG
from sse import (
    build_url_params, build_headers, build_request_body,
    extract_text_from_content, extract_image_urls_from_content,
    parse_sse_line, extract_text_from_event, extract_image_urls_from_event,
    extract_conversation_id, is_cookie_expired,
    format_openai_chunk, format_openai_done
)
from config import save_conversation_log, save_conversation_state

logger = logging.getLogger("doubao-api")


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
                async with session.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    if resp.status != 200:
                        body_text = await resp.text()
                        logger.error(f"Doubao API returned {resp.status}: {body_text[:200]}")

                        if is_cookie_expired(resp.status, body_text):
                            cookie_pool.report_fail(account)
                            if attempt < max_retries:
                                account = cookie_pool.get_next()
                                headers = build_headers(account)
                                continue

                        if attempt < max_retries:
                            account = cookie_pool.get_next()
                            headers = build_headers(account)
                            continue
                        yield json.dumps({"error": True, "status": resp.status, "body": body_text[:500]}).encode()
                        return

                    cookie_pool.report_success(account)

                    async for chunk in resp.content.iter_any():
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


async def stream_chat_completion(request):
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


async def non_stream_chat_completion(request):
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

    import time
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
