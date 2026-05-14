import json
import os
import logging
from datetime import datetime

logger = logging.getLogger("doubao-api")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
ACCOUNTS_PATH = os.path.join(BASE_DIR, 'accounts.json')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
CONVERSATION_DIR = os.path.join(BASE_DIR, 'conversations')

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(CONVERSATION_DIR, exist_ok=True)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"

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
