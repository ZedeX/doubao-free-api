import json
import os
import logging
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from config import (
    BASE_DIR, CONFIG, SIGN_METHOD, signer, cookie_pool,
    load_accounts, save_accounts, load_conversation_state,
    LOG_DIR, ACCOUNTS_PATH
)
from models import ChatCompletionRequest, AnthropicMessageRequest, MODEL_CONFIG
from openai_api import stream_chat_completion, non_stream_chat_completion
from anthropic_api import stream_anthropic_messages, non_stream_anthropic_messages

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("doubao-api")

app = FastAPI(title="Doubao Free API", version="3.1.0")

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

@app.get("/health")
async def health():
    pool_status = cookie_pool.status()
    active_count = sum(1 for a in pool_status if a["enabled"])
    result = {
        "status": "ok" if active_count > 0 else "degraded",
        "version": "3.1.0",
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
            "tutor_mode": True,
            "anthropic_api": True
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
