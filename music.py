import json
import uuid
import time
import asyncio
import logging
from typing import Optional

import aiohttp

from config import CONFIG, USER_AGENT, cookie_pool
from sse import (
    build_url_params, build_headers, generate_x_flow_trace,
    parse_sse_line, extract_conversation_id
)

logger = logging.getLogger("doubao-api")

MUSIC_TASKS = {}

MUSIC_STYLES = ["流行", "嘻哈", "国风", "DJ", "摇滚", "民谣", "R&B", "雷鬼", "朋克", "电音", "爵士"]
MUSIC_MOODS = ["快乐", "放松", "活力", "兴奋", "忧郁", "鼓舞", "伤感", "怀旧", "浪漫"]
MUSIC_VOICES = ["女声", "男声"]


def build_music_request_body(prompt: str, conversation_id: str = "0",
                              style: str = "", mood: str = "", voice: str = "",
                              lyric: str = ""):
    parts = []
    if lyric:
        parts.append(f"歌词如下：\n{lyric}")
    else:
        parts.append(f"请帮我生成一首关于「{prompt}」的歌曲")

    if style:
        parts.append(f"风格：{style}")
    if mood:
        parts.append(f"情绪：{mood}")
    if voice:
        parts.append(f"音色：{voice}")

    text = "，".join(parts) if not lyric else parts[0]

    body = {
        "bot_id": "7338286299411103781",
        "completion_option": {
            "is_regen": False,
            "with_suggest": False,
            "need_create_conversation": conversation_id == "0",
            "launch_stage": 1,
            "use_auto_cot": False,
            "use_deep_think": False
        },
        "conversation_id": conversation_id,
        "local_conversation_id": f"local_{uuid.uuid4().int % 10000000000000000}",
        "local_message_id": str(uuid.uuid4()),
        "messages": [{
            "content": json.dumps({
                "text": text,
                "intention": "MusicAssistant"
            }, ensure_ascii=False),
            "content_type": 2001,
            "attachments": [],
            "references": []
        }],
        "ext": {
            "fp": CONFIG.get('fp', ''),
            "input_skill": json.dumps({
                "skill_id": "MusicAssistant",
                "skill_type": "MusicAssistant"
            })
        }
    }
    return body


async def call_fpa_music_api(api_name: str, params: dict, account: dict):
    url = f"{CONFIG['api_base']}/api/doubao/do_action_v2"
    uid = ""
    try:
        cookie = account.get('cookie', CONFIG.get('cookie', ''))
        for part in cookie.split(';'):
            part = part.strip()
            if part.startswith('uid_tt='):
                uid = part.split('=', 1)[1]
                break
    except Exception:
        pass

    payload = {
        "scene": "FPA_Music",
        "payload": json.dumps({
            "api_name": api_name,
            "params": json.dumps(params)
        })
    }

    headers = {
        'content-type': 'application/json',
        'cookie': account.get('cookie', CONFIG.get('cookie', '')),
        'origin': 'https://www.doubao.com',
        'referer': 'https://www.doubao.com/chat/music',
        'user-agent': USER_AGENT,
        'x-flow-trace': generate_x_flow_trace()
    }

    query_params = {}
    if uid:
        query_params["uid"] = uid

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers, params=query_params,
                                timeout=aiohttp.ClientTimeout(total=30)) as resp:
            data = await resp.json()
            if data.get("code") != 0 or not data.get("data", {}).get("resp"):
                raise Exception(f"FPA Music API failed: {json.dumps(data, ensure_ascii=False)[:300]}")
            return json.loads(data["data"]["resp"])


async def start_music_generation(prompt: str, conversation_id: str = "0",
                                  style: str = "", mood: str = "", voice: str = "",
                                  lyric: str = ""):
    account = cookie_pool.get_next()
    task_id = f"music-{uuid.uuid4().hex[:12]}"

    MUSIC_TASKS[task_id] = {
        "task_id": task_id,
        "prompt": prompt,
        "style": style,
        "mood": mood,
        "voice": voice,
        "lyric": lyric,
        "status": "generating",
        "created_at": time.time(),
        "conversation_id": conversation_id,
        "music_id": None,
        "audio_url": None,
        "cover_url": None,
        "title": None,
        "duration": None,
        "generated_lyric": None,
        "error": None,
        "account": account
    }

    asyncio.create_task(_run_music_generation(
        task_id, prompt, conversation_id, account,
        style=style, mood=mood, voice=voice, lyric=lyric
    ))

    return {
        "task_id": task_id,
        "status": "generating",
        "prompt": prompt,
        "style": style,
        "mood": mood,
        "voice": voice
    }


async def _run_music_generation(task_id: str, prompt: str, conversation_id: str,
                                 account: dict, style: str = "", mood: str = "",
                                 voice: str = "", lyric: str = ""):
    try:
        params = build_url_params(account)
        url = f"{CONFIG['api_base']}/samantha/chat/completion?{params}"
        headers = build_headers(account)
        body = build_music_request_body(prompt, conversation_id, style, mood, voice, lyric)

        logger.info(f"[Music] Starting generation: prompt={prompt}, task_id={task_id}")

        full_text = ""
        music_id = None
        audio_url = None
        cover_url = None
        title = None
        duration = None
        real_conv_id = conversation_id

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=180)) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    raise Exception(f"Samantha API returned {resp.status}: {err_text[:300]}")

                async for raw_line in resp.content:
                    try:
                        line = raw_line.decode('utf-8', errors='replace').strip()
                    except:
                        continue

                    if not line:
                        continue

                    if line.startswith('data:'):
                        data_str = line[5:].strip()
                        if data_str == '[DONE]':
                            break
                        try:
                            outer = json.loads(data_str)
                            event_type = outer.get("event_type")
                            event_data_raw = outer.get("event_data", "")

                            if isinstance(event_data_raw, str) and event_data_raw:
                                try:
                                    event_data = json.loads(event_data_raw)
                                except json.JSONDecodeError:
                                    event_data = {}
                            else:
                                event_data = event_data_raw if isinstance(event_data_raw, dict) else {}

                            if event_type == 2001:
                                msg = event_data.get("message", {})
                                ct = msg.get("content_type")
                                raw_content = msg.get("content", "")

                                if ct in (10000, 2001) and isinstance(raw_content, str):
                                    try:
                                        parsed = json.loads(raw_content)
                                        text = parsed.get("text", "")
                                        if text:
                                            full_text += text
                                    except json.JSONDecodeError:
                                        if raw_content:
                                            full_text += raw_content

                                elif ct not in (10000, 2001):
                                    logger.info(f"[Music] Found content_type={ct}: {str(raw_content)[:200]}")
                                    try:
                                        ct_content = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
                                        music_data = ct_content.get("music", ct_content.get("music_card", {}))
                                        if music_data:
                                            music_id = music_data.get("id")
                                            title = music_data.get("title") or music_data.get("music_title")
                                            meta = music_data.get("meta", {})
                                            cover_url = meta.get("music_cover_thumbnail") or meta.get("cover_url")
                                            playback = meta.get("playback_model", {})
                                            audio_url = playback.get("audio_link")
                                            dur = playback.get("duration")
                                            if dur:
                                                try:
                                                    duration = int(dur)
                                                except:
                                                    duration = None
                                            logger.info(f"[Music] Found music data: music_id={music_id}")
                                    except (json.JSONDecodeError, KeyError, TypeError) as e:
                                        logger.warning(f"[Music] Failed to parse music content: {e}")

                                conv_id = event_data.get("conversation_id")
                                if conv_id:
                                    real_conv_id = conv_id

                            if event_type == 2003:
                                content_obj = event_data.get("content_obj", {})
                                music_data = content_obj.get("music", content_obj.get("music_card"))
                                if music_data:
                                    music_id = music_data.get("id")
                                    title = music_data.get("title") or music_data.get("music_title")
                                    meta = music_data.get("meta", {})
                                    cover_url = meta.get("music_cover_thumbnail") or meta.get("cover_url")
                                    playback = meta.get("playback_model", {})
                                    audio_url = playback.get("audio_link")
                                    dur = playback.get("duration")
                                    if dur:
                                        try:
                                            duration = int(dur)
                                        except:
                                            duration = None
                                    logger.info(f"[Music] Found music in content_obj: music_id={music_id}")

                        except json.JSONDecodeError:
                            pass

        MUSIC_TASKS[task_id]["generated_lyric"] = full_text
        MUSIC_TASKS[task_id]["conversation_id"] = real_conv_id

        if music_id:
            MUSIC_TASKS[task_id]["music_id"] = music_id
            MUSIC_TASKS[task_id]["title"] = title
            MUSIC_TASKS[task_id]["cover_url"] = cover_url

            if not audio_url:
                logger.info(f"[Music] No audio_url in SSE, polling for detail...")
                await _poll_music_detail(task_id, music_id, account)
            else:
                MUSIC_TASKS[task_id]["audio_url"] = audio_url
                MUSIC_TASKS[task_id]["duration"] = duration
                MUSIC_TASKS[task_id]["status"] = "completed"
                logger.info(f"[Music] Generation completed: task_id={task_id}")
        else:
            if full_text:
                MUSIC_TASKS[task_id]["status"] = "lyrics_ready"
                MUSIC_TASKS[task_id]["title"] = prompt
                logger.info(f"[Music] Lyrics generated but no music card. Lyrics length: {len(full_text)}")
            else:
                MUSIC_TASKS[task_id]["status"] = "failed"
                MUSIC_TASKS[task_id]["error"] = "No content returned from music generation"

    except Exception as e:
        logger.error(f"[Music] Generation failed: {e}")
        MUSIC_TASKS[task_id]["status"] = "failed"
        MUSIC_TASKS[task_id]["error"] = str(e)


async def _poll_music_detail(task_id: str, music_id: str, account: dict, max_retries: int = 30):
    for i in range(max_retries):
        try:
            result = await call_fpa_music_api("GetGenMusicDetail", {"id": music_id}, account)

            music = result.get("music", result.get("episode", {}))
            meta = music.get("meta", {})
            playback = meta.get("playback_model", {})

            audio_link = playback.get("audio_link")
            if audio_link:
                MUSIC_TASKS[task_id]["audio_url"] = audio_link
                MUSIC_TASKS[task_id]["duration"] = int(playback.get("duration", 0)) if playback.get("duration") else None
                MUSIC_TASKS[task_id]["cover_url"] = meta.get("music_cover_thumbnail") or meta.get("cover_url")
                MUSIC_TASKS[task_id]["title"] = music.get("title") or music.get("music_title")
                MUSIC_TASKS[task_id]["status"] = "completed"
                logger.info(f"[Music] Polling completed: task_id={task_id}")
                return

            await asyncio.sleep(3)
        except Exception as e:
            logger.warning(f"[Music] Poll attempt {i+1} failed: {e}")
            await asyncio.sleep(3)

    MUSIC_TASKS[task_id]["status"] = "lyrics_ready"
    MUSIC_TASKS[task_id]["error"] = "Audio polling timed out, but lyrics are available"


async def get_music_status(task_id: str):
    task = MUSIC_TASKS.get(task_id)
    if not task:
        return {"error": "Task not found", "task_id": task_id}

    return {
        "task_id": task["task_id"],
        "prompt": task["prompt"],
        "status": task["status"],
        "style": task.get("style"),
        "mood": task.get("mood"),
        "voice": task.get("voice"),
        "music_id": task.get("music_id"),
        "title": task.get("title"),
        "cover_url": task.get("cover_url"),
        "audio_url": task.get("audio_url"),
        "duration": task.get("duration"),
        "lyrics_length": len(task.get("generated_lyric", "")) if task.get("generated_lyric") else 0,
        "error": task.get("error"),
        "created_at": task.get("created_at")
    }


async def get_music_lyric(task_id: str):
    task = MUSIC_TASKS.get(task_id)
    if not task:
        return {"error": "Task not found"}
    return {
        "task_id": task_id,
        "prompt": task["prompt"],
        "status": task["status"],
        "lyric": task.get("generated_lyric", ""),
        "title": task.get("title")
    }


async def get_music_audio(task_id: str):
    task = MUSIC_TASKS.get(task_id)
    if not task:
        return {"error": "Task not found"}

    if task["status"] not in ("completed", "lyrics_ready"):
        return {"error": f"Music not ready, current status: {task['status']}"}

    music_id = task.get("music_id")
    audio_url = task.get("audio_url")

    if audio_url:
        return {
            "task_id": task_id,
            "music_id": music_id,
            "audio_url": audio_url,
            "title": task.get("title"),
            "duration": task.get("duration"),
            "cover_url": task.get("cover_url")
        }

    if music_id:
        try:
            account = task.get("account", cookie_pool.get_next())
            result = await call_fpa_music_api("GetGenMusicUrl", {"music_id": music_id}, account)
            url = result.get("audio_url") or result.get("url")
            if url:
                task["audio_url"] = url
                return {
                    "task_id": task_id,
                    "music_id": music_id,
                    "audio_url": url,
                    "title": task.get("title"),
                    "duration": task.get("duration"),
                    "cover_url": task.get("cover_url")
                }
        except Exception as e:
            pass

    return {"error": "Audio not available. Lyrics are ready but music generation requires the Doubao client."}


async def list_music():
    tasks = []
    for task_id, task in MUSIC_TASKS.items():
        tasks.append({
            "task_id": task_id,
            "prompt": task["prompt"],
            "status": task["status"],
            "style": task.get("style"),
            "mood": task.get("mood"),
            "title": task.get("title"),
            "duration": task.get("duration"),
            "lyrics_length": len(task.get("generated_lyric", "")) if task.get("generated_lyric") else 0,
            "created_at": task.get("created_at")
        })
    tasks.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"tasks": tasks}


async def get_music_styles():
    return {
        "styles": MUSIC_STYLES,
        "moods": MUSIC_MOODS,
        "voices": MUSIC_VOICES
    }
