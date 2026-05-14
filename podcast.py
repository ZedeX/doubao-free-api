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

PODCAST_TASKS = {}


def build_podcast_request_body(topic: str, conversation_id: str = "0", file_info: dict = None):
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
            "content": json.dumps({"text": f"请帮我生成一期关于「{topic}」的AI播客"}, ensure_ascii=False),
            "content_type": 2001,
            "attachments": [],
            "references": []
        }],
        "ext": {
            "fp": CONFIG.get('fp', '')
        }
    }
    if file_info:
        body["messages"][0]["attachments"] = [file_info]
    return body


async def call_fpa_api(api_name: str, params: dict, account: dict):
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
        "scene": "FPA_Podcast",
        "payload": json.dumps({
            "api_name": api_name,
            "params": json.dumps(params)
        })
    }

    headers = {
        'content-type': 'application/json',
        'cookie': account.get('cookie', CONFIG.get('cookie', '')),
        'origin': 'https://www.doubao.com',
        'referer': 'https://www.doubao.com/',
        'user-agent': USER_AGENT,
        'x-flow-trace': generate_x_flow_trace()
    }

    query_params = {}
    if uid:
        query_params["uid"] = uid

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers, params=query_params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            data = await resp.json()
            if data.get("code") != 0 or not data.get("data", {}).get("resp") or not data.get("data", {}).get("success"):
                raise Exception(f"FPA API failed: {json.dumps(data, ensure_ascii=False)[:300]}")
            return json.loads(data["data"]["resp"])


async def start_podcast_generation(topic: str, conversation_id: str = "0", file_info: dict = None):
    account = cookie_pool.get_next()
    task_id = f"podcast-{uuid.uuid4().hex[:12]}"

    PODCAST_TASKS[task_id] = {
        "task_id": task_id,
        "topic": topic,
        "status": "generating",
        "created_at": time.time(),
        "conversation_id": conversation_id,
        "episode_id": None,
        "audio_url": None,
        "cover_url": None,
        "title": None,
        "duration": None,
        "script": None,
        "error": None,
        "account": account,
        "file_info": file_info
    }

    asyncio.create_task(_run_podcast_generation(task_id, topic, conversation_id, account, file_info))

    return {
        "task_id": task_id,
        "status": "generating",
        "topic": topic
    }


async def _run_podcast_generation(task_id: str, topic: str, conversation_id: str, account: dict, file_info: dict = None):
    try:
        params = build_url_params(account)
        url = f"{CONFIG['api_base']}/samantha/chat/completion?{params}"
        headers = build_headers(account)
        body = build_podcast_request_body(topic, conversation_id, file_info)

        logger.info(f"[Podcast] Starting generation: topic={topic}, task_id={task_id}")

        full_text = ""
        episode_id = None
        audio_url = None
        cover_url = None
        title = None
        duration = None
        real_conv_id = conversation_id

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=180)) as resp:
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

                                conv_id = event_data.get("conversation_id")
                                if conv_id:
                                    real_conv_id = conv_id

                            if event_type == 2003:
                                content_obj = event_data.get("content_obj", {})

                                podcast_data = content_obj.get("podcast")
                                if podcast_data:
                                    episode_id = podcast_data.get("id")
                                    title = podcast_data.get("podcast_title") or podcast_data.get("title")
                                    meta = podcast_data.get("meta", {})
                                    cover_url = meta.get("podcast_cover_thumbnail")
                                    playback = meta.get("playback_model", {})
                                    audio_url = playback.get("audio_link")
                                    dur = playback.get("duration")
                                    if dur:
                                        try:
                                            duration = int(dur)
                                        except:
                                            duration = None
                                    logger.info(f"[Podcast] Found podcast data: episode_id={episode_id}")

                                widget_data_str = content_obj.get("widget_data")
                                if widget_data_str and not episode_id:
                                    try:
                                        widget_data = json.loads(widget_data_str) if isinstance(widget_data_str, str) else widget_data_str
                                        data_section = widget_data.get("data", {})
                                        if isinstance(data_section, str):
                                            data_section = json.loads(data_section)
                                        episode_list = data_section.get("episodeList", {})
                                        episodes = episode_list.get("episodes", [])
                                        if episodes:
                                            ep = episodes[0]
                                            episode_id = ep.get("id")
                                            title = ep.get("podcast_title") or ep.get("title")
                                            meta = ep.get("meta", {})
                                            cover_url = meta.get("podcast_cover_thumbnail")
                                            playback = meta.get("playback_model", {})
                                            audio_url = playback.get("audio_link")
                                            dur = playback.get("duration")
                                            if dur:
                                                try:
                                                    duration = int(dur)
                                                except:
                                                    duration = None
                                            logger.info(f"[Podcast] Found episode from widget_data: episode_id={episode_id}")
                                    except (json.JSONDecodeError, KeyError, TypeError) as e:
                                        logger.warning(f"[Podcast] Failed to parse widget_data: {e}")

                        except json.JSONDecodeError:
                            pass

        PODCAST_TASKS[task_id]["script"] = full_text
        PODCAST_TASKS[task_id]["conversation_id"] = real_conv_id

        if episode_id:
            PODCAST_TASKS[task_id]["episode_id"] = episode_id
            PODCAST_TASKS[task_id]["title"] = title
            PODCAST_TASKS[task_id]["cover_url"] = cover_url

            if not audio_url:
                logger.info(f"[Podcast] No audio_url in SSE, polling for detail...")
                await _poll_podcast_detail(task_id, episode_id, account)
            else:
                PODCAST_TASKS[task_id]["audio_url"] = audio_url
                PODCAST_TASKS[task_id]["duration"] = duration
                PODCAST_TASKS[task_id]["status"] = "completed"
                logger.info(f"[Podcast] Generation completed: task_id={task_id}, episode_id={episode_id}")
        else:
            if full_text:
                PODCAST_TASKS[task_id]["status"] = "script_ready"
                PODCAST_TASKS[task_id]["title"] = topic
                logger.info(f"[Podcast] Script generated but no audio episode_id. Script length: {len(full_text)}")
            else:
                PODCAST_TASKS[task_id]["status"] = "failed"
                PODCAST_TASKS[task_id]["error"] = "No content returned from podcast generation"

    except Exception as e:
        logger.error(f"[Podcast] Generation failed: {e}")
        PODCAST_TASKS[task_id]["status"] = "failed"
        PODCAST_TASKS[task_id]["error"] = str(e)


async def _poll_podcast_detail(task_id: str, episode_id: str, account: dict, max_retries: int = 30):
    for i in range(max_retries):
        try:
            result = await call_fpa_api("GetGenPodcastDetail", {"id": episode_id}, account)

            episode = result.get("episode", {})
            meta = episode.get("meta", {})
            playback = meta.get("playback_model", {})

            audio_link = playback.get("audio_link")
            if audio_link:
                PODCAST_TASKS[task_id]["audio_url"] = audio_link
                PODCAST_TASKS[task_id]["duration"] = int(playback.get("duration", 0)) if playback.get("duration") else None
                PODCAST_TASKS[task_id]["cover_url"] = meta.get("podcast_cover_thumbnail")
                PODCAST_TASKS[task_id]["title"] = episode.get("podcast_title") or episode.get("title")
                PODCAST_TASKS[task_id]["status"] = "completed"
                logger.info(f"[Podcast] Polling completed: task_id={task_id}")
                return

            await asyncio.sleep(3)
        except Exception as e:
            logger.warning(f"[Podcast] Poll attempt {i+1} failed: {e}")
            await asyncio.sleep(3)

    PODCAST_TASKS[task_id]["status"] = "script_ready"
    PODCAST_TASKS[task_id]["error"] = "Audio polling timed out, but script is available"


async def get_podcast_status(task_id: str):
    task = PODCAST_TASKS.get(task_id)
    if not task:
        return {"error": "Task not found", "task_id": task_id}

    return {
        "task_id": task["task_id"],
        "topic": task["topic"],
        "status": task["status"],
        "episode_id": task.get("episode_id"),
        "title": task.get("title"),
        "cover_url": task.get("cover_url"),
        "audio_url": task.get("audio_url"),
        "duration": task.get("duration"),
        "script_length": len(task.get("script", "")) if task.get("script") else 0,
        "error": task.get("error"),
        "created_at": task.get("created_at")
    }


async def get_podcast_script(task_id: str):
    task = PODCAST_TASKS.get(task_id)
    if not task:
        return {"error": "Task not found"}
    return {
        "task_id": task_id,
        "topic": task["topic"],
        "status": task["status"],
        "script": task.get("script", ""),
        "title": task.get("title")
    }


async def get_podcast_audio(task_id: str):
    task = PODCAST_TASKS.get(task_id)
    if not task:
        return {"error": "Task not found"}

    if task["status"] not in ("completed", "script_ready"):
        return {"error": f"Podcast not ready, current status: {task['status']}"}

    episode_id = task.get("episode_id")
    audio_url = task.get("audio_url")

    if audio_url:
        return {
            "task_id": task_id,
            "episode_id": episode_id,
            "audio_url": audio_url,
            "title": task.get("title"),
            "duration": task.get("duration"),
            "cover_url": task.get("cover_url")
        }

    if episode_id:
        try:
            account = task.get("account", cookie_pool.get_next())
            result = await call_fpa_api("GetGenPodcastVideoUrl", {"episode_id": episode_id}, account)
            video_url = result.get("video_url")
            if video_url:
                task["audio_url"] = video_url
                return {
                    "task_id": task_id,
                    "episode_id": episode_id,
                    "audio_url": video_url,
                    "title": task.get("title"),
                    "duration": task.get("duration"),
                    "cover_url": task.get("cover_url")
                }
        except Exception as e:
            pass

    return {"error": "Audio not available. Podcast script is ready but audio generation requires the Doubao client."}


async def list_podcasts():
    tasks = []
    for task_id, task in PODCAST_TASKS.items():
        tasks.append({
            "task_id": task_id,
            "topic": task["topic"],
            "status": task["status"],
            "title": task.get("title"),
            "duration": task.get("duration"),
            "script_length": len(task.get("script", "")) if task.get("script") else 0,
            "created_at": task.get("created_at")
        })
    tasks.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"tasks": tasks}
