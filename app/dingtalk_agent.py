"""钉钉群里的养号员工：接图、给 5 分钟重生成窗口、自动入库与队列分发。"""
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import requests
from PIL import Image, ImageOps

from .main import MEDIA, PUBLIC_URL, PUBLISH_URL, action_signature, as_dict, conn, fallback_content, get_settings, image_prompt, init_db, low_quality_content, next_account_slot, now, recognize_image_facts, schedule_material_automatically, schedule_material_for_account, select_generation_account, submit_material

DOWNLOAD_URL = "https://api.dingtalk.com/v1.0/robot/messageFiles/download"
CARD_URL = "https://api.dingtalk.com/v1.0/im/v1.0/robot/interactiveCards/send"
CARD_UPDATE_URL = "https://api.dingtalk.com/v1.0/im/robots/interactiveCards"
MEDIA_UPLOAD_URL = "https://oapi.dingtalk.com/media/upload"


def config():
    return {
        "app_key": os.getenv("DINGTALK_NURTURE_APP_KEY", "").strip(),
        "app_secret": os.getenv("DINGTALK_NURTURE_APP_SECRET", "").strip(),
        "group_id": os.getenv("NURTURE_DINGTALK_GROUP_ID", "").strip(),
        "confirm_seconds": max(60, int(os.getenv("NURTURE_CONFIRM_SECONDS", "300"))),
        "report_hour": min(23, max(0, int(os.getenv("NURTURE_DAILY_REPORT_HOUR", "9")))),
    }


def ensure_bot_binding(cfg):
    """A Stream credential change means this is a different DingTalk robot.

    The automatic group binding belongs to the robot identity, not to the
    material library.  Clear a stale binding on startup so the first group
    that adds the replacement robot becomes its new intake group.
    """
    if cfg["group_id"]:
        return
    previous_key = state("bot_app_key")
    # Older releases did not persist the robot key.  A saved conversation
    # without one is therefore also a legacy binding and must not block a
    # replacement robot's first group message.
    if state("conversation_id") and (not previous_key or previous_key != cfg["app_key"]):
        set_state("conversation_id", "")
        logging.info("DingTalk robot changed; cleared previous group binding")
    set_state("bot_app_key", cfg["app_key"])


def set_state(key, value):
    c = conn()
    c.execute("INSERT INTO dingtalk_agent_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    c.commit(); c.close()


def state(key):
    c = conn(); row = c.execute("SELECT value FROM dingtalk_agent_state WHERE key=?", (key,)).fetchone(); c.close()
    return row[0] if row else ""


def normal_platform(value):
    value = (value or "").strip().lower()
    return {"小红书": "xiaohongshu", "抖音": "douyin", "视频号": "weixin_channels"}.get(value, value)


def text_of(message):
    if getattr(message, "message_type", "") == "text":
        return (getattr(getattr(message, "text", None), "content", "") or "").strip()
    if getattr(message, "message_type", "") == "richText":
        return "".join(getattr(message, "get_text_list", lambda: [])() or []).strip()
    return ""


def reply_text(message, content):
    webhook = getattr(message, "session_webhook", "") or ""
    if not webhook:
        return
    try:
        requests.post(webhook, json={"msgtype": "text", "text": {"content": content}}, timeout=12).raise_for_status()
    except requests.RequestException:
        logging.exception("DingTalk plain reply failed")


def reply_action_card(webhook, title, markdown, buttons):
    if not webhook:
        return False
    # 钉钉 actionCard 使用 text 字段承载 Markdown；传 markdown 虽然返回 200，客户端不会渲染卡片。
    payload={"msgtype":"actionCard","actionCard":{"title":title,"text":markdown,"btnOrientation":"1","btns":[{"title":label,"actionURL":url} for label,url in buttons]}}
    try:
        response=requests.post(webhook, json=payload, timeout=15)
        response.raise_for_status()
        data=response.json()
        if data.get("errcode", 0):
            logging.error("DingTalk action card rejected: %s", data)
            return False
        return True
    except requests.RequestException:
        logging.exception("DingTalk action card send failed")
        return False


def card_payload(title, lines):
    return {"config": {"autoLayout": True, "enableForward": False},
            "header": {"title": {"type": "text", "text": title}},
            "contents": [{"type": "markdown", "text": "\n\n".join(lines)}]}


PLATFORM_LABELS = {"douyin": "抖音版", "xiaohongshu": "小红书版"}


def platform_copy(row, platform):
    """Read the paired platform version, while keeping existing preview jobs readable."""
    prefix = platform
    title = row[f"{prefix}_title"] or row["title"]
    body = row[f"{prefix}_body"] or row["body"]
    topics = json.loads(row[f"{prefix}_topics_json"] or row["topics_json"] or "[]")
    return title, body, topics


def platform_state(row, platform):
    return row[f"{platform}_state"] or "pending"


def account_label(account_key):
    if not account_key:
        return ""
    c = conn()
    try:
        row = c.execute("SELECT nickname FROM nurture_accounts WHERE account_key=?", (account_key,)).fetchone()
        return (row["nickname"] or account_key) if row else account_key
    finally:
        c.close()


def scheduled_label(value):
    if not value:
        return "尚未匹配可用时段"
    try:
        local = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8)))
        return local.strftime("%m月%d日 %H:%M")
    except (TypeError, ValueError):
        return value


def preview_card_data(row, image_refs):
    seconds = max(0, int(row["confirm_deadline"]) - now())
    minutes, remain = divmod(seconds, 60)
    expires = row["confirm_deadline"]
    is_adjustable = row["status"] == "pending_confirmation" and seconds > 0
    preview_url = f"{PUBLIC_URL}/dingtalk/preview/{row['id']}?token={action_signature(row['id'],'preview',expires)}"
    images = [{"type": "image", "image": ref, "ratio": "3:2", "id": f"image-{index}"} for index, ref in enumerate(image_refs, 1)]
    if is_adjustable:
        countdown = f"**剩余 {minutes:02d}:{remain:02d}** · 不操作会自动入库\n\n[打开实时倒计时预览]({preview_url})"
    else:
        if row["status"] == "discarded":
            countdown = "**该素材已放弃入库**\n\n不会进入发布队列，不能再放弃或换一版。"
        else:
            countdown = "**调整已结束 · 已自动入库**\n\n该素材已进入账号队列，不能再放弃或换一版。"
    contents = [*images]
    for platform in ("douyin", "xiaohongshu"):
        title, body, topics = platform_copy(row, platform)
        state = platform_state(row, platform)
        count = int(row[f"{platform}_regenerate_count"] or 0)
        chosen_key = row[f"{platform}_account_key"] or ""
        chosen_account = f"{account_label(chosen_key)}（已按账号策略生成）" if chosen_key else "未匹配：请先完善账号策略"
        chosen_time = scheduled_label(row[f"{platform}_scheduled_at"])
        contents.extend([
            {"type": "divider", "id": f"{platform}-divider"},
            {"type": "markdown", "text": f"## {PLATFORM_LABELS[platform]}\n\n**标题**\n{title}\n\n**正文**\n{body}\n\n**话题**\n{' '.join('#' + x for x in topics) or '#日常记录'}\n\n**发布账号**：{chosen_account}\n\n**计划发布**：{chosen_time}", "id": f"{platform}-copy"},
        ])
        if state == "confirmed":
            contents.append({"type": "action", "id": f"{platform}-actions", "actions": [{"type": "button", "label": {"type": "text", "text": "该平台已确认排期"}, "actionType": "openLink", "url": {"all": preview_url}, "status": "normal", "disabled": True, "id": f"{platform}-confirmed"}]})
        elif is_adjustable:
            op = "restore" if state == "discarded" else "discard"
            toggle_label = "恢复入库" if state == "discarded" else "放弃入库"
            toggle_url = f"{PUBLIC_URL}/api/dingtalk/jobs/{row['id']}/action?op={op}&platform={platform}&token={action_signature(row['id'],f'{op}:{platform}',expires)}"
            regenerate_url = f"{PUBLIC_URL}/api/dingtalk/jobs/{row['id']}/action?op=regenerate&platform={platform}&token={action_signature(row['id'],f'regenerate:{platform}',expires)}"
            confirm_url = f"{PUBLIC_URL}/api/dingtalk/jobs/{row['id']}/action?op=confirm&platform={platform}&token={action_signature(row['id'],f'confirm:{platform}',expires)}"
            editor_base = f"{PUBLIC_URL}/dingtalk/task/{row['id']}?token={action_signature(row['id'],'preview',expires)}&platform={platform}"
            contents.append({"type": "action", "id": f"{platform}-actions", "actions": [
                {"type": "button", "label": {"type": "text", "text": "改文案"}, "actionType": "openLink", "url": {"all": regenerate_url}, "status": "normal", "id": f"{platform}-regenerate"},
                {"type": "button", "label": {"type": "text", "text": "换账号"}, "actionType": "openLink", "url": {"all": editor_base + "&mode=account"}, "status": "normal", "id": f"{platform}-account"},
                {"type": "button", "label": {"type": "text", "text": "改时间"}, "actionType": "openLink", "url": {"all": editor_base + "&mode=time"}, "status": "normal", "id": f"{platform}-time"},
                {"type": "button", "label": {"type": "text", "text": "换素材"}, "actionType": "openLink", "url": {"all": editor_base + "&mode=material"}, "status": "normal", "id": f"{platform}-material"},
                {"type": "button", "label": {"type": "text", "text": "取消发布"}, "actionType": "openLink", "url": {"all": toggle_url}, "status": "danger", "id": f"{platform}-{op}"},
                {"type": "button", "label": {"type": "text", "text": "确认发布"}, "actionType": "openLink", "url": {"all": confirm_url}, "status": "primary", "id": f"{platform}-confirm"},
            ]})
        else:
            contents.append({"type": "action", "id": f"{platform}-actions", "actions": [{"type": "button", "label": {"type": "text", "text": "已结束"}, "actionType": "openLink", "url": {"all": preview_url}, "status": "normal", "disabled": True, "id": f"{platform}-done"}]})
    contents.extend([{"type": "divider", "id": "countdown-divider"}, {"type": "markdown", "text": countdown, "id": "countdown"}])
    return {
        "config": {"autoLayout": True, "enableForward": False},
        "header": {"title": {"type": "text", "text": "养号素材预览"}},
        "contents": contents,
    }


def upload_card_image(client, image_path):
    """Upload the normalized image to DingTalk and return its internal media id.

    StandardCard uses DingTalk media ids.  They remain available to the card
    client and avoid the intermittent blank rendering caused by remote URLs.
    """
    display_path = dingtalk_display_image(image_path)
    source = MEDIA / display_path
    try:
        with source.open("rb") as file:
            response = requests.post(
                MEDIA_UPLOAD_URL,
                params={"access_token": client.get_access_token(), "type": "image"},
                files={"media": (source.name, file, "image/jpeg")},
                timeout=45,
            )
        response.raise_for_status()
        data = response.json()
        media_id = data.get("media_id") or data.get("mediaId")
        if data.get("errcode") or not media_id:
            raise RuntimeError(str(data))
        return media_id
    except Exception:
        logging.exception("DingTalk card image upload failed for %s", image_path)
        # The H5 preview remains a reliable fallback if an upstream upload is
        # temporarily unavailable; do not send a second standalone image message.
        return ""


def card_image_refs(client, row, image_limit=None):
    """Return cached card media ids, uploading only what this update needs.

    The first image is intentionally enough for the initial card.  Remaining
    images are added by a background update so copy generation is not held up
    by several sequential media uploads.
    """
    originals = json.loads(row["images_json"] or "[]")[:4]
    if image_limit is not None:
        originals = originals[:image_limit]
    try:
        cached = json.loads(row["card_images_json"] or "[]")
    except json.JSONDecodeError:
        cached = []
    refs = [ref for ref in cached if ref]
    if len(refs) >= len(originals):
        return refs[:len(originals)]
    for image_path in originals[len(refs):]:
        ref = upload_card_image(client, image_path)
        if ref:
            refs.append(ref)
    c = conn()
    c.execute("UPDATE dingtalk_material_jobs SET card_images_json=?,updated_at=? WHERE id=?", (json.dumps(refs), now(), row["id"]))
    c.commit(); c.close()
    return refs


def send_or_update_preview_card(client, conversation_id, row, image_limit=None, allow_create=True):
    if not client or not conversation_id:
        return False
    if not row["card_biz_id"] and not allow_create:
        logging.error("refusing to create a replacement preview card for job %s", row["id"])
        return False
    card_biz_id = row["card_biz_id"] or f"nurture-{row['id']}"
    headers = {"Content-Type": "application/json", "x-acs-dingtalk-access-token": client.get_access_token()}
    card_data = json.dumps(preview_card_data(row, card_image_refs(client, row, image_limit)), ensure_ascii=False)
    try:
        if row["card_biz_id"]:
            response = requests.put(CARD_UPDATE_URL, headers=headers, json={"cardBizId": card_biz_id, "cardData": card_data}, timeout=15)
        else:
            response = requests.post(CARD_URL, headers=headers, json={
                "cardTemplateId": "StandardCard", "robotCode": client.credential.client_id,
                "cardData": card_data, "openConversationId": conversation_id,
                "cardBizId": card_biz_id, "sendOptions": {"atAll": False},
            }, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get("errcode", 0):
            logging.error("DingTalk preview card rejected: %s", data)
            return False
        c = conn(); c.execute("UPDATE dingtalk_material_jobs SET card_biz_id=?,card_updated_at=? WHERE id=?", (card_biz_id, now(), row["id"])); c.commit(); c.close()
        return True
    except requests.RequestException:
        logging.exception("DingTalk preview card send/update failed")
        return False


def send_group_card(client, conversation_id, title, lines):
    token = client.get_access_token()
    body = {"cardTemplateId": "StandardCard", "robotCode": client.credential.client_id,
            "cardData": json.dumps(card_payload(title, lines), ensure_ascii=False),
            "openConversationId": conversation_id, "cardBizId": "nurture-" + uuid.uuid4().hex,
            "sendOptions": {"atAll": False}}
    try:
        response = requests.post(CARD_URL, headers={"Content-Type": "application/json", "x-acs-dingtalk-access-token": token}, json=body, timeout=15)
        response.raise_for_status()
    except requests.RequestException:
        logging.exception("DingTalk card send failed")


def dingtalk_display_image(image_path):
    """生成 3:2 预览图：钉钉原生图片消息会裁切任意比例的图，先留白适配来完整保留原图。"""
    source = MEDIA / image_path
    target = source.with_name(f"dingtalk-{source.stem}.jpg")
    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return str(target.relative_to(MEDIA))
    with Image.open(source) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
        canvas = Image.new("RGB", (1200, 800), "#f7f8fa")
        image.thumbnail((1200, 800), Image.Resampling.LANCZOS)
        x = (1200 - image.width) // 2
        y = (800 - image.height) // 2
        canvas.paste(image, (x, y))
        canvas.save(target, "JPEG", quality=92, optimize=True)
    return str(target.relative_to(MEDIA))


def vision_image(image_path):
    """Make a compact, uncropped visual input for the copy model.

    DingTalk originals can be many megabytes.  A 1280px image carries enough
    scene information for a short daily caption while materially reducing the
    base64 upload and model first-token latency.
    """
    source = MEDIA / image_path
    target = source.with_name(f"vision-{source.stem}.jpg")
    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return str(target.relative_to(MEDIA))
    with Image.open(source) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
        image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        image.save(target, "JPEG", quality=82, optimize=True)
    return str(target.relative_to(MEDIA))


def download_images(client, message, job_id):
    codes = getattr(message, "get_image_list", lambda: [])() or []
    if not codes:
        return []
    folder = MEDIA / "pending" / job_id
    folder.mkdir(parents=True, exist_ok=True)
    token = client.get_access_token()
    saved = []
    def fetch_image(index, code):
        response = requests.post(DOWNLOAD_URL, headers={"Content-Type": "application/json", "x-acs-dingtalk-access-token": token}, json={"robotCode": client.credential.client_id, "downloadCode": code}, timeout=20)
        response.raise_for_status()
        binary = requests.get(response.json()["downloadUrl"], timeout=45)
        binary.raise_for_status()
        return index, binary.content
    try:
        with ThreadPoolExecutor(max_workers=min(4, len(codes))) as executor:
            futures = [executor.submit(fetch_image, index, code) for index, code in enumerate(codes, 1)]
            downloads = [future.result() for future in as_completed(futures)]
        for index, content in sorted(downloads):
            name = f"{index:02d}.jpg"
            (folder / name).write_bytes(content)
            saved.append(f"pending/{job_id}/{name}")
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return saved


def content_for(images, note, platform="douyin", account=None, scheduled_at=None, facts=None):
    vision_images = [vision_image(image) for image in images] if not facts else images
    content = None
    last_error = None
    for attempt in range(2):
        try:
            candidate = image_prompt(vision_images, note, retry=attempt > 0, platform=platform, account=account, scheduled_at=scheduled_at, facts=facts)
            if not candidate:
                raise ValueError("empty copy response")
            title = str(candidate.get("title", "")).strip()
            body = str(candidate.get("body", "")).strip()
            if low_quality_content(candidate):
                raise ValueError("placeholder copy response")
            content = candidate
            break
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                logging.warning("copy generation returned fallback-like content; retrying once", exc_info=True)
            else:
                logging.exception("copy generation retry failed; using safe fallback")
    if content is None:
        # Some OpenAI-compatible providers expose the selected text model but
        # reject image_url payloads.  Keep the material and present it for
        # review instead of discarding a successfully received group image.
        logging.error("visual copy generation unavailable; creating a reviewable fallback")
        if isinstance(last_error, httpx.HTTPStatusError):
            try:
                error_payload = last_error.response.json()
                detail = error_payload.get("error", error_payload)
                if isinstance(detail, dict):
                    detail = detail.get("message") or detail.get("detail") or detail.get("type") or ""
            except (ValueError, TypeError):
                detail = last_error.response.text
            detail = re.sub(r"\s+", " ", str(detail)).strip()[:160]
            status = f"http_{last_error.response.status_code}" + (f": {detail}" if detail else "")
        else:
            status = type(last_error).__name__
        set_state("vision_status", status)
        return "图片素材待确认", "已收到图片。图片识别暂不可用，请在预览中补充说明或稍后重生成文案。", []
    set_state("vision_status", "ok")
    title = str(content.get("title", "")).strip()[:80] or "素材待补充说明"
    body = str(content.get("body", "")).strip() or fallback_content(note)["body"]
    topics = [str(x).strip().lstrip("#") for x in content.get("topics", []) if str(x).strip()][:6]
    return title, body, topics


def preview(client, conversation_id, row, image_limit=None, allow_create=True):
    # Image, copy and actions are one interactive card, never split into
    # a native image message plus a text-only card.
    if send_or_update_preview_card(client, conversation_id, row, image_limit=image_limit, allow_create=allow_create):
        return
    if allow_create:
        reply_text(type("Message", (), {"session_webhook": row["reply_webhook"]})(), f"{row['title']}\n{row['body']}\n回复 重生成 {row['id'][:8]} 可换一版。")


def finish_card_images(client, conversation_id, job_id):
    """Fill the rest of a multi-image card without blocking first preview."""
    try:
        c = conn()
        row = c.execute("SELECT * FROM dingtalk_material_jobs WHERE id=? AND status='pending_confirmation'", (job_id,)).fetchone()
        c.close()
        if row:
            send_or_update_preview_card(client, conversation_id, row)
    except Exception:
        logging.exception("DingTalk background card image update failed for %s", job_id)


def bind_group(message, cfg):
    conversation_id = (getattr(message, "conversation_id", "") or "").strip()
    fixed = cfg["group_id"]
    if fixed and fixed != conversation_id:
        return False
    if not fixed and not state("conversation_id"):
        set_state("conversation_id", conversation_id)
    return bool(conversation_id and (fixed or state("conversation_id") == conversation_id))


def make_pending_job(client, message, cfg):
    source_message_id = getattr(message, "message_id", "") or ""
    if source_message_id:
        c = conn()
        duplicate = c.execute("SELECT id FROM dingtalk_material_jobs WHERE conversation_id=? AND source_message_id=?", (getattr(message, "conversation_id", "") or "", source_message_id)).fetchone()
        c.close()
        if duplicate:
            logging.info("ignored duplicate DingTalk message %s", source_message_id)
            return
    job_id = uuid.uuid4().hex
    images = download_images(client, message, job_id)
    if not images:
        reply_text(message, "我这次没有识别到图片。请直接发图片，或把多张图一次性发成一条消息。")
        return
    # Start visual understanding and first-card image preparation together.
    # The card is sent as soon as both are ready; later images update it in
    # place rather than delaying the user's first feedback window.
    # The account is selected before the copy is written.  Each platform has
    # its own strategy and can therefore produce a genuinely different voice.
    c = conn()
    douyin_account = select_generation_account(c, images, "", "douyin")
    xiaohongshu_account = select_generation_account(c, images, "", "xiaohongshu")
    douyin_slot = next_account_slot(c, douyin_account) if douyin_account else None
    xiaohongshu_slot = next_account_slot(c, xiaohongshu_account) if xiaohongshu_account else None
    c.close()
    vision_images = [vision_image(image) for image in images]
    try:
        shared_facts = recognize_image_facts(vision_images)
    except Exception as exc:
        logging.exception("shared image recognition failed for job %s", job_id)
        set_state("vision_status", type(exc).__name__)
        shared_facts = '{"facts":[]}'
    with ThreadPoolExecutor(max_workers=2) as executor:
        douyin_future = executor.submit(content_for, images, "", "douyin", douyin_account, douyin_slot.isoformat() if douyin_slot else "", shared_facts)
        xiaohongshu_future = executor.submit(content_for, images, "", "xiaohongshu", xiaohongshu_account, xiaohongshu_slot.isoformat() if xiaohongshu_slot else "", shared_facts)
        first_image_future = executor.submit(upload_card_image, client, images[0])
        title, body, topics = douyin_future.result()
        xhs_title, xhs_body, xhs_topics = xiaohongshu_future.result()
        first_image_ref = first_image_future.result()
    stamp = now(); conversation_id = getattr(message, "conversation_id", "") or ""
    c = conn(); c.execute("INSERT INTO dingtalk_material_jobs(id,conversation_id,sender_id,sender_nick,source_message_id,images_json,title,body,topics_json,status,confirm_deadline,created_at,updated_at,reply_webhook,card_images_json,douyin_title,douyin_body,douyin_topics_json,xiaohongshu_title,xiaohongshu_body,xiaohongshu_topics_json,douyin_account_id,douyin_account_key,douyin_scheduled_at,xiaohongshu_account_id,xiaohongshu_account_key,xiaohongshu_scheduled_at) VALUES(?,?,?,?,?,?,?,?,?,'pending_confirmation',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, conversation_id, getattr(message, "sender_id", "") or "", getattr(message, "sender_nick", "") or "", source_message_id, json.dumps(images), title, body, json.dumps(topics, ensure_ascii=False), stamp + cfg["confirm_seconds"], stamp, stamp, getattr(message,"session_webhook","") or "", json.dumps([first_image_ref] if first_image_ref else []), title, body, json.dumps(topics, ensure_ascii=False), xhs_title, xhs_body, json.dumps(xhs_topics, ensure_ascii=False), douyin_account["publish_account_id"] if douyin_account else "", douyin_account["account_key"] if douyin_account else "", douyin_slot.isoformat() if douyin_slot else "", xiaohongshu_account["publish_account_id"] if xiaohongshu_account else "", xiaohongshu_account["account_key"] if xiaohongshu_account else "", xiaohongshu_slot.isoformat() if xiaohongshu_slot else "")); c.execute("UPDATE dingtalk_material_jobs SET vision_facts=? WHERE id=?", (shared_facts, job_id)); row=c.execute("SELECT * FROM dingtalk_material_jobs WHERE id=?", (job_id,)).fetchone(); c.commit(); c.close()
    preview(client, conversation_id, row, image_limit=1)
    if len(images) > 1:
        threading.Thread(target=finish_card_images, args=(client, conversation_id, job_id), daemon=True).start()


def regenerate(client, message, token, cfg):
    c = conn(); row = c.execute("SELECT * FROM dingtalk_material_jobs WHERE id LIKE ? AND status='pending_confirmation' ORDER BY created_at DESC LIMIT 1", (token + "%",)).fetchone()
    if not row:
        c.close(); reply_text(message, "没有找到还在确认期内的素材任务。") ; return
    if row["sender_id"] != (getattr(message, "sender_id", "") or ""):
        c.close(); reply_text(message, "只有发图的人可以重生成这条素材。") ; return
    if row["douyin_regenerate_count"] >= 3:
        c.close(); reply_text(message, "这篇素材已经换过 3 版啦，建议直接入库；想要新的角度可以重新发一组图片。") ; return
    c.execute("UPDATE dingtalk_material_jobs SET action_request='regenerate:douyin',updated_at=? WHERE id=?", (now(), row["id"]))
    c.commit(); c.close()


def select_account(c):
    rows = c.execute("SELECT * FROM nurture_accounts WHERE enabled=1 ORDER BY priority,id").fetchall()
    if not rows:
        defaults = get_settings()
        if not defaults.get("platform") or not defaults.get("account_key"):
            return None
        # 兼容素材库已设置的单账号节奏；添加多账号后由账号表接管分发。
        return {"id": None, "platform": defaults["platform"], "account_key": defaults["account_key"],
                "nickname": defaults["account_key"], "priority": 100,
                "interval_days": int(defaults.get("interval_days") or 2),
                "next_publish_at": defaults.get("next_publish_at") or "", "default": True}
    candidates=[]
    for row in rows:
        count=c.execute("SELECT COUNT(*) FROM materials WHERE assigned_account_key=? AND status IN ('queued','pushed')", (row["account_key"],)).fetchone()[0]
        raw=row["next_publish_at"] or ""
        try: available=datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() if raw else 0
        except ValueError: available=0
        candidates.append((count, available, row["priority"], row))
    return min(candidates, key=lambda item:item[:3])[3]


def finalize_job_if_done(c, row):
    states=[platform_state(row, platform) for platform in ("douyin", "xiaohongshu")]
    if not all(state in {"confirmed", "discarded"} for state in states):
        return False
    status="discarded" if all(state == "discarded" for state in states) else "confirmed"
    c.execute("UPDATE dingtalk_material_jobs SET status=?,updated_at=? WHERE id=?", (status, now(), row["id"]))
    shutil.rmtree(MEDIA/"pending"/row["id"], ignore_errors=True)
    return True

def confirm_platform(job_id, platform):
    """Confirm exactly one platform child task; the sibling remains editable."""
    if platform not in PLATFORM_LABELS: return None
    c=conn(); row=c.execute("SELECT * FROM dingtalk_material_jobs WHERE id=? AND status='pending_confirmation'", (job_id,)).fetchone()
    if not row or platform_state(row, platform) != "pending": c.close(); return None
    source_images=json.loads(row["images_json"]); material_id=uuid.uuid4().hex; target=MEDIA/material_id; target.mkdir(parents=True, exist_ok=True); images=[]
    try:
        for index, old in enumerate(source_images,1):
            source=MEDIA/old; suffix=source.suffix or ".jpg"; destination=target/f"{index:02d}{suffix}"
            shutil.copy2(source, destination); images.append(f"{material_id}/{destination.name}")
        title, body, topics = platform_copy(row, platform); stamp=now()
        c.execute("INSERT INTO materials(id,images_json,title,caption,topics_json,note,status,scheduled_at,assigned_account_key,assigned_platform,source_platform,created_at,updated_at) VALUES(?,?,?,?,?,?, 'queued',?,?,?,?,?,?)", (material_id,json.dumps(images),title,body,json.dumps(topics,ensure_ascii=False),row["note"],None,"","",platform,stamp,stamp))
        selected_key=row[f"{platform}_account_key"] or ""; selected_time=row[f"{platform}_scheduled_at"] or ""
        assignment=schedule_material_for_account(c, material_id, selected_key, selected_time) if selected_key else None
        if not assignment: schedule_material_automatically(c, material_id, platform)
        try: stored=json.loads(row["assigned_material_id"] or "{}")
        except json.JSONDecodeError: stored={}
        if not isinstance(stored, dict): stored={}
        stored[platform]=material_id
        c.execute(f"UPDATE dingtalk_material_jobs SET {platform}_state='confirmed',assigned_material_id=?,updated_at=? WHERE id=?", (json.dumps(stored), stamp, job_id))
        updated=c.execute("SELECT * FROM dingtalk_material_jobs WHERE id=?",(job_id,)).fetchone(); finalize_job_if_done(c, updated)
        c.commit()
        # “确认发布” is the approval for a real platform scheduling task,
        # not merely permission to place an item in this material library.
        # submit_material creates the cloud draft, uploads assets and submits
        # the scheduled job, which immediately changes the library state to
        # scheduled (定时发布中).
        try:
            submit_material(material_id)
        except Exception:
            # submit_material records a failed state itself.  Keep the DingTalk
            # approval completed so a transient publishing failure does not
            # reopen or silently discard the reviewed material.
            logging.exception("submit confirmed material %s failed", material_id)
        return material_id
    except Exception:
        shutil.rmtree(target, ignore_errors=True); c.rollback(); raise
    finally: c.close()


def submit_queued_materials(client):
    """Submit approved, account-bound legacy items without manual library clicks."""
    c=conn(); rows=c.execute("SELECT * FROM materials WHERE status='queued' AND publish_job_id IS NULL AND assigned_account_id IS NOT NULL AND assigned_account_id!='' AND assigned_platform IS NOT NULL AND assigned_platform!='' AND scheduled_at IS NOT NULL AND scheduled_at!=''").fetchall(); c.close()
    for row in rows:
        try:
            submit_material(row["id"])
        except Exception:
            logging.exception("submit queued material %s failed", row["id"])


def daily_report(client, cfg):
    conversation_id=cfg["group_id"] or state("conversation_id")
    if not conversation_id: return
    stamp=datetime.now(timezone.utc).astimezone(); report_key=stamp.strftime("%Y-%m-%d")
    if stamp.hour != cfg["report_hour"] or state("daily_report_date") == report_key: return
    c=conn(); accounts=c.execute("SELECT * FROM nurture_accounts WHERE enabled=1 ORDER BY priority,id").fetchall(); lines=[]
    for account in accounts:
        count=c.execute("SELECT COUNT(*) FROM materials WHERE assigned_account_key=? AND status='queued'",(account["account_key"],)).fetchone()[0]
        lines.append(f"- {account['nickname'] or account['account_key']}：待发 {count} 篇；下次 {account['next_publish_at'] or '未排期'}")
    if not accounts:
        defaults=get_settings()
        if defaults.get("account_key"):
            count=c.execute("SELECT COUNT(*) FROM materials WHERE assigned_account_key=? AND status='queued'",(defaults["account_key"],)).fetchone()[0]
            lines.append(f"- {defaults['account_key']}：待发 {count} 篇；下次 {defaults.get('next_publish_at') or '未排期'}")
    c.close(); send_group_card(client, conversation_id, "养号素材日报", lines or ["还没有配置发布账号，素材会先安全入库。"]); set_state("daily_report_date",report_key)


def scheduler(client, cfg):
    while True:
        try:
            c=conn(); requested=c.execute("SELECT * FROM dingtalk_material_jobs WHERE status='pending_confirmation' AND action_request!=''").fetchall(); jobs=c.execute("SELECT id FROM dingtalk_material_jobs WHERE status='pending_confirmation' AND confirm_deadline<=?",(now(),)).fetchall(); c.close()
            for row in requested:
                if row["confirm_deadline"] <= now():
                    c=conn(); c.execute("UPDATE dingtalk_material_jobs SET action_request='',updated_at=? WHERE id=?", (now(), row["id"])); c.commit(); c.close()
                    continue
                action, _, platform = row["action_request"].partition(":")
                if action == "regenerate" and platform in PLATFORM_LABELS:
                    try:
                        c = conn(); account = c.execute("SELECT * FROM nurture_accounts WHERE account_key=? AND platform=? AND enabled=1", (row[f"{platform}_account_key"], platform)).fetchone(); c.close()
                        title, body, topics = content_for(json.loads(row["images_json"]), row["note"], platform, account, row[f"{platform}_scheduled_at"] or "", row["vision_facts"] or None)
                    except Exception as error:
                        logging.exception("regeneration failed for job %s", row["id"])
                        c=conn(); c.execute("UPDATE dingtalk_material_jobs SET action_request='',error=?,updated_at=? WHERE id=?", (str(error)[:300], now(), row["id"])); c.commit(); c.close()
                        continue
                    c=conn(); c.execute(f"UPDATE dingtalk_material_jobs SET {platform}_title=?,{platform}_body=?,{platform}_topics_json=?,{platform}_regenerate_count={platform}_regenerate_count+1,confirm_deadline=?,action_request='',updated_at=? WHERE id=?",(title,body,json.dumps(topics,ensure_ascii=False),now()+cfg["confirm_seconds"],now(),row["id"])); updated=c.execute("SELECT * FROM dingtalk_material_jobs WHERE id=?",(row["id"],)).fetchone(); c.commit(); c.close()
                    # Regeneration is a state change: refresh this card once
                    # with the new copy and reset review deadline.  The H5
                    # preview owns the per-second countdown afterwards.
                    preview(client, updated["conversation_id"], updated, allow_create=False)
                elif action == "confirm" and platform in PLATFORM_LABELS:
                    c=conn(); c.execute("UPDATE dingtalk_material_jobs SET action_request='',updated_at=? WHERE id=?",(now(),row["id"])); c.commit(); c.close()
                    material_id=confirm_platform(row["id"], platform)
                    if material_id:
                        logging.info("confirmed %s platform of job %s to material %s",platform,row["id"],material_id)
                        # This job was excluded from the periodic refresh while
                        # its button action was running.  Refresh it now so the
                        # old countdown never remains frozen after one platform
                        # has been confirmed and the sibling is still editable.
                        c=conn(); updated=c.execute("SELECT * FROM dingtalk_material_jobs WHERE id=?",(row["id"],)).fetchone(); c.close()
                        if updated:
                            send_or_update_preview_card(client, updated["conversation_id"], updated)
                elif action in {"discard", "restore"} and platform in PLATFORM_LABELS:
                    next_state = "discarded" if action == "discard" else "pending"
                    c=conn(); c.execute(f"UPDATE dingtalk_material_jobs SET {platform}_state=?,action_request='',updated_at=? WHERE id=?",(next_state,now(),row["id"])); c.commit(); changed=c.execute("SELECT * FROM dingtalk_material_jobs WHERE id=?", (row["id"],)).fetchone(); c.close()
                    if changed:
                        send_or_update_preview_card(client, changed["conversation_id"], changed)
            for job in jobs:
                c=conn(); pending=c.execute("SELECT * FROM dingtalk_material_jobs WHERE id=? AND status='pending_confirmation'",(job["id"],)).fetchone(); c.close()
                material_ids=[]
                if pending:
                    for platform in ("douyin", "xiaohongshu"):
                        if platform_state(pending, platform) == "pending":
                            material_id=confirm_platform(job["id"], platform)
                            if material_id: material_ids.append(material_id)
                if material_ids:
                    logging.info("auto-confirmed job %s to materials %s",job["id"],material_ids)
                    # Update the original card in place so both operations
                    # immediately become greyed out after automatic storage.
                    c=conn(); completed=c.execute("SELECT * FROM dingtalk_material_jobs WHERE id=?",(job["id"],)).fetchone(); c.close()
                    if completed:
                        send_or_update_preview_card(client, completed["conversation_id"], completed)
            submit_queued_materials(client); daily_report(client,cfg); set_state("last_heartbeat",datetime.now(timezone.utc).isoformat())
        except Exception:
            logging.exception("nurture scheduler failed")
        time.sleep(5)


def run():
    init_db(); cfg=config(); logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not cfg["app_key"] or not cfg["app_secret"]:
        logging.warning("DingTalk nurture worker is waiting for DINGTALK_NURTURE_APP_KEY and DINGTALK_NURTURE_APP_SECRET")
        while True: set_state("last_heartbeat", "waiting_for_credentials"); time.sleep(60)
    ensure_bot_binding(cfg)
    import dingtalk_stream
    class Handler(dingtalk_stream.ChatbotHandler):
        async def process(self, callback):
            message=dingtalk_stream.ChatbotMessage.from_dict(callback.data)
            try:
                if not bind_group(message,cfg): return dingtalk_stream.AckMessage.STATUS_OK,"ignored"
                text=text_of(message); match=re.search(r"(?:重生成|重新生成|重做)\s*([0-9a-fA-F]{6,32})",text)
                if match: regenerate(self.dingtalk_client,message,match.group(1),cfg)
                elif getattr(message,"message_type","") in {"picture","richText"}: make_pending_job(self.dingtalk_client,message,cfg)
                elif "养号" in text and "帮助" in text: reply_text(message,"直接发图片即可生成素材；多张图同一条消息会归为一篇。5 分钟内回复“重生成 任务号”可重做。")
            except Exception:
                logging.exception("nurture message failed"); reply_text(message,"这张图处理失败了，麻烦重新发一次。")
            return dingtalk_stream.AckMessage.STATUS_OK,"OK"
    class ObservedStreamClient(dingtalk_stream.DingTalkStreamClient):
        def open_connection(self):
            try:
                topics = [{"type":"CALLBACK", "topic":topic} for topic in self.callback_handler_map]
                if self._is_event_required:
                    topics.insert(0,{"type":"EVENT","topic":"*"})
                payload={"clientId":self.credential.client_id,"clientSecret":self.credential.client_secret,"subscriptions":topics,"ua":"xinjie-nurture","localIp":self.get_host_ip()}
                response=requests.post(self.OPEN_CONNECTION_API,headers={"Content-Type":"application/json","Accept":"application/json"},data=json.dumps(payload).encode("utf-8"),timeout=15)
                if not response.ok:
                    set_state("stream_gateway", f"http_{response.status_code}")
                    return None
                connection=response.json()
            except Exception:
                set_state("stream_gateway", "error")
                raise
            set_state("stream_gateway", "connected" if connection else "unavailable")
            return connection

    client=ObservedStreamClient(dingtalk_stream.Credential(cfg["app_key"],cfg["app_secret"]))
    client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC,Handler())
    threading.Thread(target=scheduler,args=(client,cfg),daemon=True).start(); client.start_forever()


if __name__ == "__main__":
    run()
