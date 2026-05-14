import json
import os
import logging
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from config import (
    BASE_DIR, CONFIG, SIGN_METHOD, signer, cookie_pool,
    load_accounts, save_accounts, load_conversation_state,
    LOG_DIR, ACCOUNTS_PATH, rate_limiter, concurrency_limiter
)
from models import ChatCompletionRequest, AnthropicMessageRequest, MODEL_CONFIG
from openai_api import stream_chat_completion, non_stream_chat_completion, generate_images, delete_conversation
from anthropic_api import stream_anthropic_messages, non_stream_anthropic_messages
from podcast import start_podcast_generation, get_podcast_status, get_podcast_audio, get_podcast_script, list_podcasts
from music import start_music_generation, get_music_status, get_music_audio, get_music_lyric, list_music, get_music_styles

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("doubao-api")

_cleanup_task = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global signer, SIGN_METHOD, _cleanup_task
    if SIGN_METHOD == 'b2' and signer:
        logger.info("Initializing B2 Playwright signer (this may take 30-60s)...")
        success = await signer.initialize()
        if success:
            logger.info("B2 Playwright signer initialized successfully")
        else:
            logger.error("B2 Playwright signer initialization failed, falling back to B3")
            SIGN_METHOD = 'b3'
    logger.info(f"Active sign method: {SIGN_METHOD}")
    _cleanup_task = asyncio.create_task(_auto_cleanup_task())
    yield
    if _cleanup_task:
        _cleanup_task.cancel()
    if signer:
        await signer.close()

app = FastAPI(title="Doubao Free API", version="3.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    path = request.url.path

    if path.startswith("/v1/") and not rate_limiter.is_allowed(client_ip):
        logger.warning(f"Rate limit exceeded for {client_ip} on {path}")
        return JSONResponse(
            status_code=429,
            content={
                "error": {"message": "Rate limit exceeded. Please slow down.", "type": "rate_limit_error", "code": 429}
            },
            headers={"Retry-After": str(rate_limiter.get_status(client_ip).get("reset_at", 60))}
        )

    if path in ("/v1/chat/completions", "/v1/messages", "/v1/images/generations", "/v1/podcast/generate"):
        await concurrency_limiter.acquire()
        try:
            response = await call_next(request)
            return response
        finally:
            concurrency_limiter.release()

    return await call_next(request)

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

@app.post("/v1/messages")
async def anthropic_messages(request: AnthropicMessageRequest):
    if request.stream:
        return StreamingResponse(
            stream_anthropic_messages(request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )
    else:
        result = await non_stream_anthropic_messages(request)
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


@app.post("/v1/images/generations")
async def generate_images_endpoint(request: Request):
    """
    OpenAI 兼容的图片生成 API
    请求体示例: { "prompt": "一只可爱的小猫", "n": 1, "size": "1024x1024" }
    """
    try:
        body = await request.json()
        prompt = body.get("prompt", "")
        n = body.get("n", 1)
        size = body.get("size", "1024x1024")
        
        if not prompt:
            raise HTTPException(status_code=400, detail="Missing 'prompt' is required")
        
        result = await generate_images(prompt, n=n, size=size)
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Image generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    pool_status = cookie_pool.status()
    active_count = sum(1 for a in pool_status if a["enabled"])
    result = {
        "status": "ok" if active_count > 0 else "degraded",
        "version": "3.3.0",
        "cookie_set": bool(CONFIG.get('cookie')),
        "sign_method": SIGN_METHOD,
        "accounts_total": len(pool_status),
        "accounts_active": active_count,
        "accounts": pool_status,
        "concurrency": {
            "active": concurrency_limiter.active,
            "max": concurrency_limiter.max_concurrent,
            "total_served": concurrency_limiter.total
        },
        "rate_limit": {
            "max_requests": rate_limiter.max_requests,
            "window_seconds": rate_limiter.window_seconds
        },
        "models": list(MODEL_CONFIG.keys()),
        "features": {
            "vision": True,
            "image_upload": True,
            "image_generation": True,
            "deep_think": True,
            "expert_mode": True,
            "coding_mode": True,
            "writing_mode": True,
            "translation": True,
            "tutor_mode": True,
            "data_analyst_mode": True,
            "anthropic_api": True,
            "podcast": True
        }
    }
    if SIGN_METHOD == 'b2' and signer:
        result["signer_initialized"] = signer._initialized
        result["ms_token_available"] = bool(signer.ms_token)
    return result

@app.post("/v1/podcast/generate")
async def podcast_generate(request: Request):
    try:
        body = await request.json()
        topic = body.get("topic", "")
        conversation_id = body.get("conversation_id", "0")
        file_info = body.get("file_info")
        if not topic and not file_info:
            raise HTTPException(status_code=400, detail="'topic' or 'file_info' is required")
        result = await start_podcast_generation(topic, conversation_id, file_info)
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Podcast generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/podcast/upload")
async def podcast_upload_pdf(file: UploadFile = File(...)):
    from uploader import upload_image
    account = cookie_pool.get_next()
    file_data = await file.read()
    file_name = file.filename or "upload.pdf"

    try:
        attachment = await upload_image(
            file_data=file_data,
            file_name=file_name,
            cookie=account.get('cookie', CONFIG.get('cookie', '')),
            device_id=account.get('device_id', ''),
            tea_uuid=account.get('tea_uuid', ''),
            web_id=account.get('web_id', '')
        )
        return {"status": "ok", "file_info": attachment}
    except Exception as e:
        logger.error(f"Podcast file upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/podcast/status/{task_id}")
async def podcast_status(task_id: str):
    result = await get_podcast_status(task_id)
    if "error" in result and result.get("error") == "Task not found":
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(content=result)

@app.get("/v1/podcast/audio/{task_id}")
async def podcast_audio(task_id: str):
    result = await get_podcast_audio(task_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return JSONResponse(content=result)

@app.get("/v1/podcast/list")
async def podcast_list():
    result = await list_podcasts()
    return JSONResponse(content=result)

@app.get("/v1/podcast/script/{task_id}")
async def podcast_script(task_id: str):
    result = await get_podcast_script(task_id)
    if "error" in result and result.get("error") == "Task not found":
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(content=result)

@app.post("/v1/music/generate")
async def music_generate(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    conversation_id = body.get("conversation_id", "0")
    style = body.get("style", "")
    mood = body.get("mood", "")
    voice = body.get("voice", "")
    lyric = body.get("lyric", "")
    if not prompt and not lyric:
        raise HTTPException(status_code=400, detail="prompt or lyric is required")
    result = await start_music_generation(prompt, conversation_id, style=style, mood=mood, voice=voice, lyric=lyric)
    return JSONResponse(content=result)

@app.get("/v1/music/status/{task_id}")
async def music_status(task_id: str):
    result = await get_music_status(task_id)
    if "error" in result and result.get("error") == "Task not found":
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(content=result)

@app.get("/v1/music/audio/{task_id}")
async def music_audio(task_id: str):
    result = await get_music_audio(task_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return JSONResponse(content=result)

@app.get("/v1/music/lyric/{task_id}")
async def music_lyric(task_id: str):
    result = await get_music_lyric(task_id)
    if "error" in result and result.get("error") == "Task not found":
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(content=result)

@app.get("/v1/music/list")
async def music_list():
    result = await list_music()
    return JSONResponse(content=result)

@app.get("/v1/music/styles")
async def music_styles():
    result = await get_music_styles()
    return JSONResponse(content=result)

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

@app.delete("/v1/conversations/{conversation_id}")
async def delete_conversation_endpoint(conversation_id: str):
    success, error = await delete_conversation(conversation_id)
    if success:
        return {"status": "ok", "message": f"Conversation {conversation_id} deleted"}
    else:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {error}")

@app.post("/v1/conversations/cleanup")
async def cleanup_conversations():
    import glob
    deleted = 0
    errors = 0
    now = datetime.now()

    pattern = os.path.join(BASE_DIR, "conversations", "*.json")
    for filepath in glob.glob(pattern):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                state = json.load(f)
            conv_id = state.get("doubao_conversation_id", "")
            updated_at = state.get("updated_at", "")
            if conv_id and conv_id != "0":
                if updated_at:
                    try:
                        updated_time = datetime.fromisoformat(updated_at)
                        age_hours = (now - updated_time).total_seconds() / 3600
                    except:
                        age_hours = 999
                else:
                    age_hours = 999

                if age_hours > CONFIG.get('conversation_cleanup_hours', 24):
                    success, _ = await delete_conversation(conv_id)
                    if success:
                        os.remove(filepath)
                        deleted += 1
                    else:
                        errors += 1
        except Exception as e:
            logger.error(f"Cleanup error for {filepath}: {e}")
            errors += 1

    return {"status": "ok", "deleted": deleted, "errors": errors}

@app.get("/")
async def index():
    html_path = os.path.join(BASE_DIR, 'index.html')
    if os.path.exists(html_path):
        with open(html_path, 'r', encoding='utf-8') as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Doubao API</h1><p>index.html not found</p>")

async def _auto_cleanup_task():
    interval = CONFIG.get('cleanup_interval_seconds', 3600)
    cleanup_hours = CONFIG.get('conversation_cleanup_hours', 24)
    logger.info(f"Auto cleanup task started: interval={interval}s, cleanup_age={cleanup_hours}h")
    while True:
        await asyncio.sleep(interval)
        try:
            import glob
            now = datetime.now()
            deleted = 0
            pattern = os.path.join(BASE_DIR, "conversations", "*.json")
            for filepath in glob.glob(pattern):
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        state = json.load(f)
                    conv_id = state.get("doubao_conversation_id", "")
                    updated_at = state.get("updated_at", "")
                    if conv_id and conv_id != "0":
                        if updated_at:
                            try:
                                updated_time = datetime.fromisoformat(updated_at)
                                age_hours = (now - updated_time).total_seconds() / 3600
                            except:
                                age_hours = 999
                        else:
                            age_hours = 999
                        if age_hours > cleanup_hours:
                            success, _ = await delete_conversation(conv_id)
                            if success:
                                os.remove(filepath)
                                deleted += 1
                except Exception as e:
                    logger.error(f"Auto cleanup error: {e}")
            if deleted > 0:
                logger.info(f"Auto cleanup: deleted {deleted} old conversations")
        except Exception as e:
            logger.error(f"Auto cleanup task error: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CONFIG.get('server_host', '0.0.0.0'), port=CONFIG.get('server_port', 8765))
