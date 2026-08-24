import base64, hashlib, hmac, html, json, os, re, shutil, sqlite3, threading, time, uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.getenv("APP_DB_PATH", ROOT / "data/nurture_library.sqlite3"))
MEDIA = Path(os.getenv("APP_MEDIA_DIR", ROOT / "media"))
PUBLISH_URL = os.getenv("PUBLISH_GATEWAY_URL", "http://publish:8010").rstrip("/")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
PUBLIC_URL = os.getenv("NURTURE_PUBLIC_URL", "https://nurture.xinjieai.com").rstrip("/")
CHINA_TZ = timezone(timedelta(hours=8))

def conn():
    DB.parent.mkdir(parents=True, exist_ok=True); c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
def now(): return int(time.time())
def as_dict(row):
    item=dict(row)
    item["images"] = json.loads(item.pop("images_json") or "[]")
    item["topics"] = json.loads(item.pop("topics_json", "[]") or "[]")
    item["body"] = item.get("caption", "")
    return item
def init_db():
    c=conn(); c.executescript("""
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS materials (
      id TEXT PRIMARY KEY, images_json TEXT NOT NULL, caption TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'queued', scheduled_at TEXT, publish_draft_id INTEGER,
      publish_job_id INTEGER, error TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
    """)
    defaults={"interval_days":"2","next_publish_at":"","platform":"","account_key":""}
    for k,v in defaults.items(): c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))
    columns = {row[1] for row in c.execute("PRAGMA table_info(materials)")}
    if "title" not in columns:
        c.execute("ALTER TABLE materials ADD COLUMN title TEXT NOT NULL DEFAULT ''")
    if "topics_json" not in columns:
        c.execute("ALTER TABLE materials ADD COLUMN topics_json TEXT NOT NULL DEFAULT '[]'")
    if "assigned_account_key" not in columns:
        c.execute("ALTER TABLE materials ADD COLUMN assigned_account_key TEXT NOT NULL DEFAULT ''")
    if "assigned_platform" not in columns:
        c.execute("ALTER TABLE materials ADD COLUMN assigned_platform TEXT NOT NULL DEFAULT ''")
    if "assigned_account_id" not in columns:
        c.execute("ALTER TABLE materials ADD COLUMN assigned_account_id TEXT NOT NULL DEFAULT ''")
    if "music_enabled" not in columns:
        c.execute("ALTER TABLE materials ADD COLUMN music_enabled INTEGER NOT NULL DEFAULT 1")
    if "music_json" not in columns:
        c.execute("ALTER TABLE materials ADD COLUMN music_json TEXT NOT NULL DEFAULT '{}'")
    if "source_platform" not in columns:
        c.execute("ALTER TABLE materials ADD COLUMN source_platform TEXT NOT NULL DEFAULT ''")
    # 历史素材没有来源平台。旧版已发布记录可能仍是 submitted；唯一绑定抖音账号的记录归入抖音。
    c.execute("UPDATE materials SET source_platform='douyin',updated_at=? WHERE COALESCE(source_platform,'')='' AND status IN ('published','submitted') AND LOWER(COALESCE(assigned_platform,''))='douyin'", (now(),))
    c.execute("UPDATE materials SET source_platform='xiaohongshu',updated_at=? WHERE COALESCE(source_platform,'')=''", (now(),))
    c.execute("UPDATE materials SET source_platform='douyin',updated_at=? WHERE source_platform='xiaohongshu' AND status IN ('published','submitted') AND LOWER(COALESCE(assigned_platform,''))='douyin'", (now(),))
    c.executescript("""
    CREATE TABLE IF NOT EXISTS nurture_accounts (
      id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, account_key TEXT NOT NULL UNIQUE,
      nickname TEXT NOT NULL DEFAULT '', priority INTEGER NOT NULL DEFAULT 100,
      interval_days INTEGER NOT NULL DEFAULT 2, next_publish_at TEXT NOT NULL DEFAULT '',
      enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS dingtalk_material_jobs (
      id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, sender_id TEXT NOT NULL,
      sender_nick TEXT NOT NULL DEFAULT '', source_message_id TEXT NOT NULL DEFAULT '',
      images_json TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', title TEXT NOT NULL,
      body TEXT NOT NULL, topics_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
      confirm_deadline INTEGER NOT NULL, regenerate_count INTEGER NOT NULL DEFAULT 0,
      assigned_material_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_dingtalk_material_jobs_deadline ON dingtalk_material_jobs(status, confirm_deadline);
    CREATE TABLE IF NOT EXISTS dingtalk_agent_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    account_columns = {row[1] for row in c.execute("PRAGMA table_info(nurture_accounts)")}
    for column, definition in {
        "publish_account_id": "TEXT NOT NULL DEFAULT ''",
        "position": "TEXT NOT NULL DEFAULT ''", "persona": "TEXT NOT NULL DEFAULT ''",
        "audience": "TEXT NOT NULL DEFAULT ''", "content_topics_json": "TEXT NOT NULL DEFAULT '[]'",
        "tone": "TEXT NOT NULL DEFAULT ''", "blocked_topics_json": "TEXT NOT NULL DEFAULT '[]'",
        "weekly_quota": "INTEGER NOT NULL DEFAULT 3", "publish_days_json": "TEXT NOT NULL DEFAULT '[1,3,6]'",
        "publish_times_json": "TEXT NOT NULL DEFAULT '[\"20:00\"]'", "min_interval_days": "INTEGER NOT NULL DEFAULT 1",
        "auto_publish": "INTEGER NOT NULL DEFAULT 1",
    }.items():
        if column not in account_columns:
            c.execute(f"ALTER TABLE nurture_accounts ADD COLUMN {column} {definition}")
    job_columns = {row[1] for row in c.execute("PRAGMA table_info(dingtalk_material_jobs)")}
    if "reply_webhook" not in job_columns:
        c.execute("ALTER TABLE dingtalk_material_jobs ADD COLUMN reply_webhook TEXT NOT NULL DEFAULT ''")
    if "action_request" not in job_columns:
        c.execute("ALTER TABLE dingtalk_material_jobs ADD COLUMN action_request TEXT NOT NULL DEFAULT ''")
    if "card_biz_id" not in job_columns:
        c.execute("ALTER TABLE dingtalk_material_jobs ADD COLUMN card_biz_id TEXT NOT NULL DEFAULT ''")
    if "card_updated_at" not in job_columns:
        c.execute("ALTER TABLE dingtalk_material_jobs ADD COLUMN card_updated_at INTEGER NOT NULL DEFAULT 0")
    if "card_images_json" not in job_columns:
        c.execute("ALTER TABLE dingtalk_material_jobs ADD COLUMN card_images_json TEXT NOT NULL DEFAULT '[]'")
    for column, definition in {
        "selected_platform": "TEXT NOT NULL DEFAULT ''", "selected_account_id": "TEXT NOT NULL DEFAULT ''",
        "selected_account_key": "TEXT NOT NULL DEFAULT ''", "selected_scheduled_at": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if column not in job_columns:
            c.execute(f"ALTER TABLE dingtalk_material_jobs ADD COLUMN {column} {definition}")
    for column, definition in {
        "douyin_title": "TEXT NOT NULL DEFAULT ''", "douyin_body": "TEXT NOT NULL DEFAULT ''",
        "douyin_topics_json": "TEXT NOT NULL DEFAULT '[]'", "douyin_state": "TEXT NOT NULL DEFAULT 'pending'",
        "douyin_regenerate_count": "INTEGER NOT NULL DEFAULT 0", "xiaohongshu_title": "TEXT NOT NULL DEFAULT ''",
        "xiaohongshu_body": "TEXT NOT NULL DEFAULT ''", "xiaohongshu_topics_json": "TEXT NOT NULL DEFAULT '[]'",
        "xiaohongshu_state": "TEXT NOT NULL DEFAULT 'pending'", "xiaohongshu_regenerate_count": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if column not in job_columns:
            c.execute(f"ALTER TABLE dingtalk_material_jobs ADD COLUMN {column} {definition}")
    job_columns = {row[1] for row in c.execute("PRAGMA table_info(dingtalk_material_jobs)")}
    for platform in ("douyin", "xiaohongshu"):
        for column, definition in {
            f"{platform}_account_id": "TEXT NOT NULL DEFAULT ''", f"{platform}_account_key": "TEXT NOT NULL DEFAULT ''",
            f"{platform}_scheduled_at": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in job_columns:
                c.execute(f"ALTER TABLE dingtalk_material_jobs ADD COLUMN {column} {definition}")
    # 迁移历史上由钉钉入库流程自动带入的排期：未在发布队列选择账号的素材一律恢复为无定时。
    c.execute("UPDATE materials SET scheduled_at=NULL,assigned_account_key='',assigned_platform='',updated_at=? WHERE status='queued' AND COALESCE(assigned_account_id,'')='' AND publish_job_id IS NULL AND scheduled_at IS NOT NULL AND scheduled_at!=''",(now(),))
    c.commit(); c.close(); MEDIA.mkdir(parents=True,exist_ok=True)
def get_settings():
    c=conn(); values={r["key"]:r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return values
def next_slot(settings):
    raw=settings.get("next_publish_at", "")
    if not raw: return None
    return datetime.fromisoformat(raw.replace("Z","+00:00"))

def json_list(value, fallback=None):
    try:
        result = json.loads(value or "[]")
        return result if isinstance(result, list) else (fallback or [])
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback or []

def week_start(value):
    local = value.astimezone(CHINA_TZ)
    return (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

def parse_slot_time(value):
    try:
        hour, minute = (int(part) for part in str(value).split(":", 1))
        if 0 <= hour <= 23 and 0 <= minute <= 59: return hour, minute
    except (ValueError, TypeError):
        pass
    return 20, 0

def next_account_slot(c, account, after=None, exclude_job_id=""):
    """Find the next unused slot, including still-reviewable DingTalk reservations."""
    start = (after or datetime.now(timezone.utc)).astimezone(CHINA_TZ)
    days = [int(day) for day in json_list(account["publish_days_json"], [1, 3, 6]) if str(day).isdigit() and 0 <= int(day) <= 6] or [1, 3, 6]
    times = [parse_slot_time(value) for value in json_list(account["publish_times_json"], ["20:00"])] or [(20, 0)]
    quota = max(1, min(int(account["weekly_quota"] or 3), 14))
    min_gap = max(0, min(int(account["min_interval_days"] or 1), 30))
    rows = c.execute("SELECT scheduled_at FROM materials WHERE assigned_account_key=? AND scheduled_at IS NOT NULL AND scheduled_at!='' AND status!='failed'", (account["account_key"],)).fetchall()
    scheduled=[]
    for row in rows:
        try: scheduled.append(datetime.fromisoformat(row["scheduled_at"].replace("Z", "+00:00")).astimezone(CHINA_TZ))
        except ValueError: pass
    # A task card has already promised this account and time to the reviewer.
    # Count that promise before it becomes a material, otherwise several cards
    # created during the review window can be assigned to the same slot.
    platform = account["platform"]
    pending = c.execute(
        f"SELECT id,{platform}_scheduled_at AS scheduled_at FROM dingtalk_material_jobs "
        f"WHERE status='pending_confirmation' AND {platform}_state='pending' "
        f"AND {platform}_account_key=? AND {platform}_scheduled_at IS NOT NULL AND {platform}_scheduled_at!=''",
        (account["account_key"],),
    ).fetchall()
    for row in pending:
        if exclude_job_id and row["id"] == exclude_job_id:
            continue
        try: scheduled.append(datetime.fromisoformat(row["scheduled_at"].replace("Z", "+00:00")).astimezone(CHINA_TZ))
        except ValueError: pass
    for offset in range(84):
        date = (start + timedelta(days=offset)).date()
        if date.weekday() not in days: continue
        for hour, minute in times:
            candidate = datetime(date.year, date.month, date.day, hour, minute, tzinfo=CHINA_TZ)
            if candidate <= start + timedelta(minutes=1): continue
            if sum(1 for item in scheduled if week_start(item) == week_start(candidate)) >= quota: continue
            if any(abs((candidate - item).total_seconds()) < min_gap * 86400 for item in scheduled): continue
            return candidate.astimezone(timezone.utc)
    return None

def schedule_material_automatically(c, material_id, platform):
    # Automatic publishing must never bypass the strategy that shaped the copy.
    # Accounts without a prepared persona remain available for a human to choose,
    # but are excluded from unattended matching and scheduling.
    rows = strategy_accounts(c, platform)
    candidates=[]
    for account in rows:
        slot=next_account_slot(c, account)
        if slot:
            load=c.execute("SELECT COUNT(*) FROM materials WHERE assigned_account_key=? AND status IN ('queued','scheduled','pushed','submitted')", (account["account_key"],)).fetchone()[0]
            candidates.append((slot, load, int(account["priority"]), account))
    if not candidates: return None
    slot, _, _, account=min(candidates, key=lambda item:(item[0], item[1], item[2]))
    c.execute("UPDATE materials SET assigned_account_id=?,assigned_account_key=?,assigned_platform=?,scheduled_at=?,status='queued',updated_at=? WHERE id=?", (account["publish_account_id"], account["account_key"], account["platform"], slot.isoformat(), now(), material_id))
    c.execute("UPDATE nurture_accounts SET next_publish_at=?,updated_at=? WHERE id=?", (slot.isoformat(), now(), account["id"]))
    return {"account":dict(account), "scheduled_at":slot.isoformat()}

def schedule_material_for_account(c, material_id, account_key, scheduled_at=""):
    account=c.execute("SELECT * FROM nurture_accounts WHERE account_key=? AND enabled=1", (account_key,)).fetchone()
    if not account: return None
    if scheduled_at:
        try:
            slot=datetime.fromisoformat(scheduled_at.replace("Z", "+00:00")).astimezone(timezone.utc)
            if slot <= datetime.now(timezone.utc): return None
        except ValueError: return None
    else:
        slot=next_account_slot(c, account)
    if not slot: return None
    c.execute("UPDATE materials SET assigned_account_id=?,assigned_account_key=?,assigned_platform=?,scheduled_at=?,status='queued',updated_at=? WHERE id=?", (account["publish_account_id"], account["account_key"], account["platform"], slot.isoformat(), now(), material_id))
    c.execute("UPDATE nurture_accounts SET next_publish_at=?,updated_at=? WHERE id=?", (slot.isoformat(), now(), account["id"]))
    return {"account":dict(account), "scheduled_at":slot.isoformat()}
def generation_strategy(account):
    """Return the copy-relevant portion of an account strategy.

    Scheduling fields deliberately stay out of the prompt.  They decide when a
    note is sent; position, persona and content boundaries decide how it reads.
    """
    if not account:
        return None
    topics = json_list(account["content_topics_json"] if isinstance(account, sqlite3.Row) else account.get("content_topics_json", "[]"))
    blocked = json_list(account["blocked_topics_json"] if isinstance(account, sqlite3.Row) else account.get("blocked_topics_json", "[]"))
    get = (lambda key, default="": account[key] if isinstance(account, sqlite3.Row) else account.get(key, default))
    if not (str(get("position")).strip() and str(get("persona")).strip() and topics):
        return None
    return {
        "account_key": str(get("account_key")).strip(),
        "nickname": str(get("nickname") or get("account_key")).strip(),
        "position": str(get("position")).strip(),
        "persona": str(get("persona")).strip(),
        "audience": str(get("audience")).strip(),
        "topics": topics,
        "tone": str(get("tone")).strip(),
        "blocked": blocked,
    }

def strategy_accounts(c, platform):
    rows = c.execute("SELECT * FROM nurture_accounts WHERE enabled=1 AND auto_publish=1 AND platform=? ORDER BY priority,id", (platform,)).fetchall()
    return [row for row in rows if generation_strategy(row)]


def assignment_metrics(c, account):
    """Measure an account's committed workload for balanced content matching."""
    key = account["account_key"]
    platform = account["platform"]
    current_week = week_start(datetime.now(timezone.utc))
    recent_cutoff = now() - 14 * 86400
    material_rows = c.execute(
        "SELECT scheduled_at,created_at,updated_at FROM materials "
        "WHERE assigned_account_key=? AND status!='failed'", (key,)
    ).fetchall()
    pending_rows = c.execute(
        f"SELECT {platform}_scheduled_at AS scheduled_at,created_at,updated_at "
        f"FROM dingtalk_material_jobs WHERE status='pending_confirmation' "
        f"AND {platform}_state='pending' AND {platform}_account_key=?", (key,)
    ).fetchall()
    all_rows = [*material_rows, *pending_rows]
    weekly = 0
    recent = 0
    activity = []
    for row in all_rows:
        created = int(row["created_at"] or 0)
        updated = int(row["updated_at"] or 0)
        activity.append(max(created, updated))
        if max(created, updated) >= recent_cutoff:
            recent += 1
        slot = row["scheduled_at"] or ""
        try:
            if week_start(datetime.fromisoformat(slot.replace("Z", "+00:00"))) == current_week:
                weekly += 1
        except (TypeError, ValueError):
            # A reviewed item without a persisted slot still consumes this
            # week's matching capacity, rather than being assigned repeatedly.
            if max(created, updated) >= int(current_week.timestamp()):
                weekly += 1
    recent_sorted = sorted(activity, reverse=True)
    consecutive = sum(1 for value in recent_sorted[:2] if value >= now() - 3 * 86400)
    return {"weekly": weekly, "recent": recent, "consecutive": consecutive, "last_activity": recent_sorted[0] if recent_sorted else 0}


def choose_balanced_account(rows, ranked_keys, metrics):
    """Keep the semantic shortlist, then favour accounts needing coverage."""
    if not rows:
        return None
    rank = {key: index for index, key in enumerate(ranked_keys)}
    # Do not keep adding to an account whose current-week quota is already
    # committed while another strategy-ready account still has room.
    available = [row for row in rows if metrics[row["account_key"]]["weekly"] < max(1, int(row["weekly_quota"] or 2))]
    candidates = available or rows
    ordered = sorted(candidates, key=lambda row: (rank.get(row["account_key"], len(rows)), int(row["priority"]), row["id"]))
    # Preserve relevance by choosing within the model's best four strategies;
    # if it did not return a usable ranking, the ordered candidate list is the
    # safe deterministic fallback.
    shortlist = ordered[:min(4, len(ordered))]
    return min(shortlist, key=lambda row: (
        metrics[row["account_key"]]["weekly"] * 60
        + metrics[row["account_key"]]["recent"] * 12
        + metrics[row["account_key"]]["consecutive"] * 24
        + rank.get(row["account_key"], len(rows)) * 8,
        int(row["priority"]), row["id"],
    ))


def select_generation_account(c, files, note, platform):
    """Choose a strategy-ready account with semantic relevance and coverage balance."""
    rows = strategy_accounts(c, platform)
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    metrics = {row["account_key"]: assignment_metrics(c, row) for row in rows}
    # Vision provides a relevance order, not the final decision.  The final
    # choice also protects weekly quotas and avoids repeated recent matches.
    options = []
    for row in rows:
        strategy = generation_strategy(row)
        options.append({"account_key": strategy["account_key"], "定位": strategy["position"], "人设": strategy["persona"], "主题": strategy["topics"], "禁发": strategy["blocked"]})
    try:
        content = [{"type":"text", "text":"根据图片素材，返回最适合的前 4 个账号，按适合度从高到低排序。只返回 JSON：{\"ranked_account_keys\":[\"候选账号标识1\",\"候选账号标识2\"]}。优先选择内容主题、受众和人设最贴合者；如果都不贴合，选择最通用且禁发主题不冲突的账号。候选账号：" + json.dumps(options, ensure_ascii=False) + ("。用户补充：" + note if note else "")}]
        for path in files:
            data = base64.b64encode((MEDIA / path).read_bytes()).decode()
            content.append({"type":"image_url", "image_url":{"url":f"data:image/jpeg;base64,{data}"}})
        response = httpx.post(f"{OPENAI_BASE_URL}/chat/completions", headers={"Authorization":f"Bearer {OPENAI_KEY}"}, json={"model":OPENAI_MODEL,"messages":[{"role":"user","content":content}],"max_tokens":120}, timeout=60)
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"].strip()
        result = json.loads(re.sub(r"^```(?:json)?|```$", "", raw).strip())
        ranked = result.get("ranked_account_keys", [])
        if not isinstance(ranked, list): ranked = []
        ranked = [str(key) for key in ranked if any(row["account_key"] == str(key) for row in rows)]
        # Backward-compatible handling for a model response in the old format.
        if not ranked and result.get("account_key"):
            ranked = [str(result["account_key"])]
        return choose_balanced_account(rows, ranked, metrics)
    except Exception:
        # A matching API outage must not collapse every task onto the first
        # priority account; the same balancing rule remains available locally.
        return choose_balanced_account(rows, [], metrics)

def publishing_time_context(scheduled_at):
    """Use the planned slot as writing context, never as an assumed capture time."""
    if not scheduled_at:
        return ""
    try:
        local = datetime.fromisoformat(str(scheduled_at).replace("Z", "+00:00")).astimezone(CHINA_TZ)
    except (TypeError, ValueError):
        return ""
    scene = "清晨" if local.hour < 10 else "午间" if local.hour < 15 else "傍晚或晚饭时间" if local.hour < 21 else "夜晚"
    return (
        f"这条内容计划在 {local.strftime('%m月%d日 %H:%M')}（{scene}）发布。发布时间是文案场景依据，不代表照片拍摄时间。"
        "除非图片或用户说明有清晰时间证据（例如钟表、明确天色、画面文字），不要擅自写“今天中午”“早上刚刚”等拍摄时刻。"
        "没有明确时间证据时，食物可自然按本次发布时间写成午间、晚饭或夜宵场景；其他素材也应使用与该发布时间协调的表达。"
    )


def image_prompt(files, note, retry=False, platform="douyin", account=None, scheduled_at=None):
    if not OPENAI_KEY: return ""
    retry_rule="这是自动重试，必须结合图片里的具体物品、场景或动作写完整内容；绝不能使用“今天的小日常”“今天留下一点日常”等泛化占位语。" if retry else ""
    human_voice_rule=(
        "长期文案规则：必须像真实用户随手发的生活分享，减少 AI 味。"
        "从图片中挑 1 至 2 个具体细节写起，可自然加入“啊、还好、结果、没想到、笑死、确实”等口语或轻微吐槽，"
        "但不要硬塞网络词。避免工整抒情、空泛感悟、鸡汤、总结式收尾，以及“治愈、烟火气、记录美好、忙碌的一天”等高频 AI 套话。"
        "不虚构地点、人物关系、事件或感受；看不清的信息宁可不写。"
    )
    platform_rule = (
        "这是抖音版：标题要有短视频开场感，正文适合作为口播或字幕，第一句从具体画面或轻微钩子开始；表达短、节奏快、口语化。"
        if platform == "douyin" else
        "这是小红书版：标题要有图文封面感，正文是更完整、易读的真实分享；表达有细节和可收藏感，但不夸张、不硬凑攻略。"
    )
    strategy = generation_strategy(account)
    account_rule = ""
    if strategy:
        account_rule = (
            f"这是账号「{strategy['nickname']}」的专属文案，必须严格使用以下账号策略："
            f"账号定位：{strategy['position']}。账号人设：{strategy['persona']}。"
            f"目标受众：{strategy['audience'] or '按人设自然表达'}。"
            f"可写主题：{'、'.join(strategy['topics'])}。"
            f"文案语气：{strategy['tone'] or '自然口语化'}。"
            f"禁止涉及：{'、'.join(strategy['blocked']) or '无'}。"
            "不要解释策略，也不要套用其他账号的表达；若图片与可写主题不完全贴合，宁可写成真实的轻量日常，也不要硬编。"
        )
    content=[{"type":"text","text":"根据这些照片生成一条真实日常分享。只返回 JSON：{\"title\":\"不超过20字的自然标题\",\"body\":\"30到100字的正文，像真人随手记录，可有轻微吐槽或语气词，不虚构地点、人物关系或经历\",\"topics\":[\"2到4个不带#的话题\"]}。" + human_voice_rule + platform_rule + account_rule + publishing_time_context(scheduled_at) + retry_rule + ("用户补充："+note if note else "")}]
    for path in files:
        # AI 星火's OpenAI-compatible gateway accepts vision inputs by HTTPS
        # URL, but rejects inline data URLs for gpt-5.5.  Media is already
        # served by this application at a public, immutable path.
        content.append({"type":"image_url","image_url":{"url":f"{PUBLIC_URL}/media/{path}"}})
    payload={"model":OPENAI_MODEL,"messages":[{"role":"user","content":content}],"max_tokens":180}
    response=httpx.post(f"{OPENAI_BASE_URL}/chat/completions",headers={"Authorization":f"Bearer {OPENAI_KEY}"},json=payload,timeout=60); response.raise_for_status()
    raw = response.json()["choices"][0]["message"]["content"].strip()
    return json.loads(re.sub(r"^```(?:json)?|```$", "", raw).strip())

def low_quality_content(content):
    """Reject template filler instead of quietly storing it as a material."""
    if not isinstance(content, dict):
        return True
    title = re.sub(r"\s+", "", str(content.get("title", "")).strip())
    body = re.sub(r"\s+", "", str(content.get("body", "")).strip())
    topics = [str(topic).strip().lstrip("#") for topic in content.get("topics", []) if str(topic).strip()]
    generic_titles = {"今天的小日常", "日常", "生活分享", "随手记录"}
    generic_bodies = {"今天留下一点日常。", "记录一下今天。", "分享一下日常。"}
    return title in generic_titles or body in generic_bodies or len(title) < 4 or len(body) < 30 or len(topics) < 2

def generate_content(files, note, platform="douyin", account=None, scheduled_at=None):
    """Generate usable copy twice at most; never silently save filler copy."""
    last_error = None
    for attempt in range(2):
        try:
            candidate = image_prompt(files, note, retry=attempt > 0, platform=platform, account=account, scheduled_at=scheduled_at)
            if low_quality_content(candidate):
                raise ValueError("low-quality copy response")
            return candidate
        except Exception as exc:
            last_error = exc
    raise RuntimeError("文案生成结果不符合质量要求") from last_error
def fallback_content(note): return {"title":"今天的小日常", "body":note.strip() or "今天留下一点日常。", "topics":[]}
def serialize_all():
    c=conn(); rows=[as_dict(r) for r in c.execute("SELECT * FROM materials ORDER BY CASE WHEN status='queued' THEN 0 WHEN status='scheduled' THEN 1 WHEN status IN ('published','submitted') THEN 2 ELSE 3 END, CASE WHEN status IN ('published','submitted') THEN updated_at END DESC, CASE WHEN status='scheduled' THEN scheduled_at END ASC, created_at DESC")]; c.close(); return rows

def gateway_error(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            return exc.response.json().get("detail") or "发布服务返回错误"
        except (ValueError, AttributeError):
            return "发布服务返回错误"
    return "发布服务暂不可用"

def gateway_request(method, path, **kwargs):
    try:
        response = httpx.request(method, f"{PUBLISH_URL}{path}", timeout=90, **kwargs)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, gateway_error(exc)) from exc

def account_by_id(account_id):
    accounts = gateway_request("GET", "/api/publish/accounts")
    account = next((item for item in accounts if str(item.get("id")) == str(account_id) and int(item.get("enabled", 0)) == 1), None)
    if not account:
        raise HTTPException(422, "请选择一个已授权且启用的发布账号")
    return account

def material_row(material_id):
    c=conn(); row=c.execute("SELECT * FROM materials WHERE id=?",(material_id,)).fetchone(); c.close()
    if not row: raise HTTPException(404,"素材不存在")
    return row

def delete_material_record(material_id):
    row = material_row(material_id)
    if row["publish_job_id"]:
        raise HTTPException(409, "该素材已有发布任务，不能删除；请先取消定时或等待发布完成")
    c = conn(); c.execute("DELETE FROM materials WHERE id=?", (material_id,)); c.commit(); c.close()
    folder = (MEDIA / material_id).resolve()
    media_root = MEDIA.resolve()
    if str(folder).startswith(str(media_root) + os.sep):
        shutil.rmtree(folder, ignore_errors=True)
    return {"id": material_id, "ok": True}

def save_material_publish_config(material_id, account_id, scheduled_at, music_enabled=True, music=None):
    row = material_row(material_id)
    if row["publish_job_id"]:
        raise HTTPException(409, "该素材已提交发布任务，不能再修改；请先刷新状态或到蚁小二处理")
    account = account_by_id(account_id)
    source_platform = str(row["source_platform"] or "").strip().lower()
    if source_platform and account.get("platform") != source_platform:
        labels = {"douyin": "抖音", "xiaohongshu": "小红书"}
        raise HTTPException(422, f"{labels.get(source_platform, source_platform)}素材只能选择对应平台账号")
    if scheduled_at:
        try: datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
        except ValueError as exc: raise HTTPException(422, "发布时间格式不正确") from exc
    c=conn(); c.execute("UPDATE materials SET assigned_account_id=?,assigned_account_key=?,assigned_platform=?,scheduled_at=?,music_enabled=?,music_json=?,status='queued',error='',updated_at=? WHERE id=?",(
        str(account["id"]), account.get("account_key", ""), account.get("platform", ""), scheduled_at or None,
        int(bool(music_enabled)), json.dumps(music or {}, ensure_ascii=False), now(), material_id)); c.commit(); c.close()
    return as_dict(material_row(material_id))

def submit_material(material_id):
    row = material_row(material_id)
    if row["publish_job_id"]:
        return {"item": as_dict(row), "already_submitted": True}
    if not row["assigned_account_id"]:
        raise HTTPException(422, "请先选择发布账号")
    item = as_dict(row)
    try:
        account = account_by_id(row["assigned_account_id"])
        draft_id = row["publish_draft_id"]
        if not draft_id:
            draft = gateway_request("POST", "/api/publish/drafts", json={
                "title": item.get("title") or "今天的小日常", "content": item["caption"], "topics": item.get("topics", []),
                "content_type": "image", "platforms": [], "selected_accounts": {}, "scheduled_at": None,
            })
            draft_id = draft["id"]
            for image in item["images"]:
                with (MEDIA/image).open("rb") as file:
                    gateway_request("POST", f"/api/publish/drafts/{draft_id}/assets", data={"asset_type":"image"}, files={"file":(Path(image).name,file,"image/jpeg")})
            c=conn(); c.execute("UPDATE materials SET publish_draft_id=?,updated_at=? WHERE id=?",(draft_id,now(),material_id)); c.commit(); c.close()
        gateway_request("PATCH", f"/api/publish/drafts/{draft_id}/targets", json={
            "platforms":[account["platform"]], "selected_accounts":{account["platform"]:str(account["id"])}, "scheduled_at":row["scheduled_at"],
        })
        music = json.loads(row["music_json"] or "{}")
        gateway_request("PATCH", f"/api/publish/drafts/{draft_id}/music", json={"music_enabled":bool(row["music_enabled"]), "selected_music":music})
        scheduled = gateway_request("POST", f"/api/publish/drafts/{draft_id}/schedule", json={"scheduled_at":row["scheduled_at"]})
        job_id = scheduled["job_ids"][0]
        result = gateway_request("POST", f"/api/publish/jobs/{job_id}/submit")
        status = "scheduled" if row["scheduled_at"] else "submitted"
        c=conn(); c.execute("UPDATE materials SET publish_job_id=?,status=?,error='',updated_at=? WHERE id=?",(job_id,status,now(),material_id)); c.commit(); c.close()
        return {"item":as_dict(material_row(material_id)),"job":result}
    except HTTPException as exc:
        c=conn(); c.execute("UPDATE materials SET status='failed',error=?,updated_at=? WHERE id=?",(str(exc.detail)[:240],now(),material_id)); c.commit(); c.close(); raise

def sync_publish_status(row, raise_on_error=True):
    """Copy the authoritative cloud-task state back to the material library."""
    try:
        job=gateway_request("POST",f"/api/publish/jobs/{row['publish_job_id']}/refresh")
    except HTTPException:
        if raise_on_error:
            raise
        return None
    status_map={"published":"published","failed":"failed","scheduled":"scheduled","submitted":"submitted"}
    c=conn(); c.execute("UPDATE materials SET status=?,error=?,updated_at=? WHERE id=?",(
        status_map.get(job.get("status"),"submitted"),job.get("error_message","")[:240],now(),row["id"])); c.commit(); c.close()
    return as_dict(material_row(row["id"]))

def refresh_due_scheduled_materials():
    """Move elapsed scheduled jobs out of the scheduled bucket without user action."""
    cutoff=datetime.now(timezone.utc).isoformat()
    c=conn(); rows=c.execute("SELECT * FROM materials WHERE status='scheduled' AND publish_job_id IS NOT NULL AND scheduled_at IS NOT NULL AND scheduled_at != '' AND scheduled_at <= ?",(cutoff,)).fetchall(); c.close()
    for row in rows:
        sync_publish_status(row, raise_on_error=False)

def scheduled_status_watcher():
    while True:
        try:
            refresh_due_scheduled_materials()
        except Exception:
            pass
        time.sleep(60)

app=FastAPI(title="养号素材库",version="0.1.0")
@app.on_event("startup")
def startup():
    init_db()
    threading.Thread(target=scheduled_status_watcher, daemon=True).start()
@app.get("/api/health")
def health(): return {"status":"ok","release":os.getenv("XINJIE_RELEASE_VERSION","local"),"released_at":os.getenv("XINJIE_RELEASED_AT","")}
@app.get("/api/materials")
def materials():
    refresh_due_scheduled_materials()
    return {"items":serialize_all(),"settings":get_settings()}
class Settings(BaseModel):
    interval_days:int=2; next_publish_at:Optional[str]=None; platform:str=""; account_key:str=""
@app.put("/api/settings")
def save_settings(payload:Settings):
    if not 1 <= payload.interval_days <= 30: raise HTTPException(422,"发布间隔须为 1-30 天")
    values={"interval_days":str(payload.interval_days),"next_publish_at":payload.next_publish_at or "","platform":payload.platform.strip().lower(),"account_key":payload.account_key.strip()}
    c=conn(); [c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,v)) for k,v in values.items()]; c.commit(); c.close(); return get_settings()
class NurtureAccount(BaseModel):
    platform: str
    account_key: str
    nickname: str = ""
    priority: int = 100
    interval_days: int = 2
    next_publish_at: Optional[str] = None
    enabled: bool = True
class AccountStrategy(BaseModel):
    account_id: str
    platform: str
    account_key: str
    nickname: str = ""
    enabled: bool = True
    position: str = ""
    persona: str = ""
    audience: str = ""
    content_topics: list[str] = []
    tone: str = ""
    blocked_topics: list[str] = []
    weekly_quota: int = 3
    publish_days: list[int] = [1, 3, 6]
    publish_times: list[str] = ["20:00"]
    min_interval_days: int = 1
    auto_publish: bool = True

def serialize_strategy(row):
    item=dict(row)
    item["content_topics"]=json_list(item.pop("content_topics_json", "[]"))
    item["blocked_topics"]=json_list(item.pop("blocked_topics_json", "[]"))
    item["publish_days"]=json_list(item.pop("publish_days_json", "[1,3,6]"), [1,3,6])
    item["publish_times"]=json_list(item.pop("publish_times_json", '["20:00"]'), ["20:00"])
    item["auto_publish"]=bool(item.get("auto_publish", 1))
    item["enabled"]=bool(item.get("enabled", 1))
    return item
@app.get("/api/nurture/accounts")
def nurture_accounts():
    c=conn(); rows=[serialize_strategy(row) for row in c.execute("SELECT * FROM nurture_accounts ORDER BY priority, id")]; c.close(); return {"items":rows}
@app.put("/api/nurture/accounts/strategy")
def save_account_strategy(payload:AccountStrategy):
    if not payload.account_id.strip() or not payload.account_key.strip() or not payload.platform.strip():
        raise HTTPException(422,"账号、平台和账号标识不能为空")
    if not 1 <= payload.weekly_quota <= 14: raise HTTPException(422,"每周篇数须为 1-14")
    if not 0 <= payload.min_interval_days <= 30: raise HTTPException(422,"最小间隔须为 0-30 天")
    days=sorted({int(day) for day in payload.publish_days if isinstance(day, int) and 0 <= day <= 6})
    if not days: raise HTTPException(422,"请至少选择一个发布日")
    times=[]
    for value in payload.publish_times:
        hour, minute=parse_slot_time(value)
        if f"{hour:02d}:{minute:02d}" not in times: times.append(f"{hour:02d}:{minute:02d}")
    if not times: raise HTTPException(422,"请至少设置一个发布时间")
    stamp=now(); c=conn()
    existing=c.execute("SELECT id FROM nurture_accounts WHERE account_key=?",(payload.account_key.strip(),)).fetchone()
    values=(payload.account_id.strip(), payload.platform.strip().lower(), payload.account_key.strip(), payload.nickname.strip(), payload.position.strip()[:80], payload.persona.strip()[:600], payload.audience.strip()[:200], json.dumps([item.strip() for item in payload.content_topics if item.strip()][:8],ensure_ascii=False), payload.tone.strip()[:120], json.dumps([item.strip() for item in payload.blocked_topics if item.strip()][:8],ensure_ascii=False), payload.weekly_quota, json.dumps(days), json.dumps(times), payload.min_interval_days, int(payload.auto_publish), int(payload.enabled), stamp)
    if existing:
        c.execute("UPDATE nurture_accounts SET publish_account_id=?,platform=?,account_key=?,nickname=?,position=?,persona=?,audience=?,content_topics_json=?,tone=?,blocked_topics_json=?,weekly_quota=?,publish_days_json=?,publish_times_json=?,min_interval_days=?,auto_publish=?,enabled=?,updated_at=? WHERE id=?", (*values, existing["id"]))
        row=c.execute("SELECT * FROM nurture_accounts WHERE id=?",(existing["id"],)).fetchone()
    else:
        cursor=c.execute("INSERT INTO nurture_accounts(publish_account_id,platform,account_key,nickname,position,persona,audience,content_topics_json,tone,blocked_topics_json,weekly_quota,publish_days_json,publish_times_json,min_interval_days,auto_publish,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*values, stamp))
        row=c.execute("SELECT * FROM nurture_accounts WHERE id=?",(cursor.lastrowid,)).fetchone()
    c.commit(); c.close(); return serialize_strategy(row)
@app.put("/api/nurture/accounts")
def save_nurture_accounts(payload:list[NurtureAccount]):
    if len(payload)>100: raise HTTPException(422,"账号数量不能超过 100")
    c=conn(); c.execute("DELETE FROM nurture_accounts")
    stamp=now()
    for item in payload:
        if not item.platform.strip() or not item.account_key.strip(): c.close(); raise HTTPException(422,"平台和账号标识不能为空")
        if not 1 <= item.interval_days <= 30: c.close(); raise HTTPException(422,"发布间隔须为 1-30 天")
        c.execute("INSERT INTO nurture_accounts(platform,account_key,nickname,priority,interval_days,next_publish_at,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(item.platform.strip().lower(),item.account_key.strip(),item.nickname.strip(),item.priority,item.interval_days,item.next_publish_at or "",int(item.enabled),stamp,stamp))
    c.commit(); c.close(); return nurture_accounts()
@app.get("/api/dingtalk/status")
def dingtalk_status():
    c=conn(); rows={r["key"]:r["value"] for r in c.execute("SELECT * FROM dingtalk_agent_state")}; pending=c.execute("SELECT COUNT(*) FROM dingtalk_material_jobs WHERE status='pending_confirmation'").fetchone()[0]; c.close()
    return {"configured":bool(os.getenv("DINGTALK_NURTURE_APP_KEY") and os.getenv("DINGTALK_NURTURE_APP_SECRET")),"bound_group":bool(rows.get("conversation_id")),"pending_confirmation":pending,"last_heartbeat":rows.get("last_heartbeat",""),"stream_gateway":rows.get("stream_gateway","")}

@app.get("/dingtalk/status", response_class=HTMLResponse)
def dingtalk_status_page():
    status=dingtalk_status()
    rows=[("凭证已配置", "是" if status["configured"] else "否"),("已绑定群聊", "是" if status["bound_group"] else "否"),("Stream 网关", status["stream_gateway"] or "等待连接"),("最近心跳", status["last_heartbeat"] or "暂无"),("待确认素材",str(status["pending_confirmation"]))]
    items="".join(f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>" for label,value in rows)
    return HTMLResponse(f"<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>钉钉机器人状态</title><style>body{{margin:0;padding:32px;background:#f5f7fb;color:#172033;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif}}main{{max-width:560px;margin:auto;background:#fff;border-radius:14px;padding:24px;box-shadow:0 5px 24px #17203314}}h1{{font-size:22px;margin:0 0 8px}}p{{color:#667085}}table{{width:100%;border-collapse:collapse;margin-top:20px}}th,td{{padding:13px 0;border-top:1px solid #edf0f5;text-align:left}}th{{color:#667085;font-weight:500;width:42%}}td{{font-weight:650}}</style><main><h1>养号专员01 · 钉钉连接状态</h1><p>仅展示运行状态，不包含任何凭证。</p><table>{items}</table></main>")

def action_signature(job_id:str, action:str, deadline:int):
    secret=os.getenv("DINGTALK_NURTURE_APP_SECRET", "").encode()
    raw=f"{job_id}:{action}:{deadline}".encode()
    return hmac.new(secret, raw, hashlib.sha256).hexdigest()[:24]

def valid_dingtalk_job_token(row, job_id:str, op:str, token:str, platform:str=""):
    return (row["status"] == "pending_confirmation" and now() <= row["confirm_deadline"]
            and hmac.compare_digest(token, action_signature(job_id, f"{op}:{platform}" if platform else op, row["confirm_deadline"])))

def valid_dingtalk_preview_token(row, job_id:str, token:str):
    """Keep the detail view available after auto-storage so it can show its final state."""
    return (row["status"] in {"pending_confirmation", "confirmed", "discarded"}
            and hmac.compare_digest(token, action_signature(job_id, "preview", row["confirm_deadline"])))

@app.get("/dingtalk/preview/{job_id}", response_class=HTMLResponse)
def dingtalk_preview(job_id:str, token:str):
    """A DingTalk H5 detail page: the browser owns the per-second countdown."""
    c=conn(); row=c.execute("SELECT * FROM dingtalk_material_jobs WHERE id=?",(job_id,)).fetchone(); c.close()
    if not row or not valid_dingtalk_preview_token(row, job_id, token):
        return HTMLResponse("<meta charset='utf-8'><h2>这个预览已失效</h2><p>请回到钉钉重新发送图片。</p>", status_code=410)
    image_paths=json.loads(row["images_json"] or "[]")
    images="".join(f"<img src='{html.escape(PUBLIC_URL + '/media/' + path, quote=True)}' alt='素材图片'>" for path in image_paths)
    platform_labels={"douyin":"抖音版", "xiaohongshu":"小红书版"}
    platform_sections=[]
    for platform in ("douyin", "xiaohongshu"):
        title=row[f"{platform}_title"] or row["title"]
        body=row[f"{platform}_body"] or row["body"]
        try: platform_topics=json.loads(row[f"{platform}_topics_json"] or row["topics_json"] or "[]")
        except json.JSONDecodeError: platform_topics=[]
        topics=" ".join("#"+html.escape(str(item)) for item in platform_topics) or "#日常记录"
        state=row[f"{platform}_state"] or "pending"
        state_label={"pending":"待确认", "confirmed":"已确认排期", "discarded":"已取消发布"}.get(state, state)
        platform_sections.append(
            f"<article class='platform-copy'><div class='platform-head'><h2>{platform_labels[platform]}</h2><span class='state {html.escape(state)}'>{html.escape(state_label)}</span></div>"
            f"<h3>标题</h3><p>{html.escape(title)}</p><h3>正文</h3><p>{html.escape(body)}</p><h3>话题</h3><p class='topics'>{topics}</p></article>"
        )
    deadline=int(row["confirm_deadline"])
    adjustable=row["status"] == "pending_confirmation" and now() < deadline
    discarded = row["status"] == "discarded"
    final_hint=("倒计时结束后，仍待确认的平台会自动进入定时发布队列；请在钉钉任务卡操作各平台。" if adjustable else ("该素材已放弃入库，不会进入发布队列。" if discarded else "已进入定时发布队列。"))
    return HTMLResponse(f"""<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>养号素材预览</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f5f7fb;color:#172033;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif}}
    main{{max-width:680px;margin:auto;background:white;min-height:100vh}}header{{padding:22px 20px;background:#eaf3ff;color:#1677ff;font-size:22px;font-weight:700}}
    .images{{display:grid;gap:10px;padding:12px;background:#f5f7fb}}img{{display:block;width:100%;max-height:560px;object-fit:contain;background:#f4f5f7;border-radius:10px}}
    section{{padding:0 20px}}.platform-copy{{padding:18px 0;border-bottom:1px solid #e9edf3}}.platform-head{{display:flex;align-items:center;justify-content:space-between;gap:12px}}h2{{font-size:19px;margin:0}}h3{{font-size:16px;margin:18px 0 8px}}p{{font-size:17px;line-height:1.7;margin:0;white-space:pre-wrap}}.topics{{font-weight:600}}.state{{font-size:13px;padding:5px 9px;border-radius:99px;background:#edf4ff;color:#1677ff}}.state.confirmed{{background:#eaf8ef;color:#1e8e52}}.state.discarded{{background:#fdf0f0;color:#b54747}}
    .timer{{margin:22px 0 10px;padding-top:18px;font-size:16px}}.timer strong{{font-size:22px}}.hint{{color:#667085;font-size:14px;margin-bottom:30px}}
    </style><main><header>养号素材预览</header><div class='images'>{images}</div><section>
    {''.join(platform_sections)}
    <div class='timer'>还可调整 <strong id='countdown'>--:--</strong> · 已换 {row['regenerate_count']} / 3 版</div><p id='hint' class='hint'>{final_hint}</p>
    </section></main><script>const deadline={deadline}*1000,el=document.getElementById('countdown'),hint=document.getElementById('hint'),discarded={str(discarded).lower()};function finish(){{el.textContent='00:00';hint.textContent=discarded?'该素材已放弃入库，不会进入发布队列。':'已进入定时发布队列。';}}function tick(){{const s=Math.max(0,Math.ceil((deadline-Date.now())/1000));el.textContent=String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');if(!s){{finish();clearInterval(timer);}}}}tick();const timer=setInterval(tick,1000);</script>""")

@app.get("/dingtalk/task/{job_id}", response_class=HTMLResponse)
def dingtalk_task_editor(job_id:str, token:str, platform:str, mode:str="account"):
    if platform not in {"douyin","xiaohongshu"}: raise HTTPException(422,"平台不存在")
    c=conn(); row=c.execute("SELECT * FROM dingtalk_material_jobs WHERE id=?",(job_id,)).fetchone()
    if not row or not valid_dingtalk_preview_token(row,job_id,token): c.close(); raise HTTPException(410,"任务已失效")
    accounts=c.execute("SELECT * FROM nurture_accounts WHERE enabled=1 AND platform=? ORDER BY priority,id",(platform,)).fetchall(); c.close()
    title=row[f"{platform}_title"] or row["title"]; body=row[f"{platform}_body"] or row["body"]
    selected=row[f"{platform}_account_key"] or ""; scheduled=row[f"{platform}_scheduled_at"] or ""
    account_options="".join(f"<option value='{html.escape(account['account_key'],quote=True)}' {'selected' if account['account_key']==selected else ''}>{html.escape(account['nickname'] or account['account_key'])} · {html.escape(account['position'] or '未设置定位')}</option>" for account in accounts)
    action=f"{PUBLIC_URL}/api/dingtalk/jobs/{job_id}/platform/{platform}/edit"
    hint={"account":"选择后会按新账号的人设、语气与内容边界，重新生成当前平台文案；另一平台不会变化。","time":"修改后只调整本平台的发布时间。","material":"上传替换图片后，系统会按两个平台各自已匹配账号的策略重新生成文案。"}.get(mode,"可在这里修改当前平台任务。")
    fields=(f"<label>发布账号<select name='account_key'><option value=''>按账号策略重新推荐并生成文案</option>{account_options}</select></label>" if mode=="account" else f"<input type='hidden' name='account_key' value='{html.escape(selected,quote=True)}'>")
    fields+=(f"<label>计划发布时间<input name='scheduled_at' type='datetime-local' value='{html.escape(scheduled[:16],quote=True)}'></label>" if mode=="time" else f"<input type='hidden' name='scheduled_at' value='{html.escape(scheduled,quote=True)}'>")
    fields+=("<label>替换图片<input name='files' type='file' accept='image/*' multiple required></label>" if mode=="material" else "")
    return HTMLResponse(f"""<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>编辑{platform}</title><style>body{{margin:0;background:#f5f7fb;color:#172033;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif}}main{{max-width:640px;margin:auto;padding:22px}}article{{background:#fff;border-radius:14px;padding:20px;box-shadow:0 4px 16px #17203312}}h1{{font-size:20px}}p{{line-height:1.65}}label{{display:grid;gap:6px;margin:16px 0;font-size:14px;font-weight:650}}input,select{{padding:11px;border:1px solid #d5dce8;border-radius:8px;font:inherit}}button{{width:100%;padding:13px;border:0;border-radius:9px;background:#1677ff;color:white;font:inherit;font-weight:700}}.hint{{color:#61708a;background:#eef5ff;padding:10px;border-radius:8px;font-size:13px}}small{{color:#7b8798}}</style><main><article><h1>{html.escape({'douyin':'抖音','xiaohongshu':'小红书'}[platform])}任务 · {html.escape({'account':'换账号','time':'改时间','material':'换素材'}.get(mode,'编辑'))}</h1><p><b>{html.escape(title)}</b><br>{html.escape(body)}</p><p class='hint'>{hint}</p><form method='post' enctype='multipart/form-data' action='{html.escape(action,quote=True)}'><input type='hidden' name='token' value='{html.escape(token,quote=True)}'><input type='hidden' name='mode' value='{html.escape(mode,quote=True)}'>{fields}<button type='submit'>保存本平台修改</button></form><p><small>本次修改只影响当前平台子任务。</small></p></article></main>""")

@app.post("/api/dingtalk/jobs/{job_id}/platform/{platform}/edit", response_class=HTMLResponse)
async def edit_dingtalk_platform_task(job_id:str, platform:str, token:str=Form(...), mode:str=Form(""), account_key:str=Form(""), scheduled_at:str=Form(""), files:list[UploadFile]=File([])):
    if platform not in {"douyin","xiaohongshu"}: raise HTTPException(422,"平台不存在")
    c=conn(); row=c.execute("SELECT * FROM dingtalk_material_jobs WHERE id=?",(job_id,)).fetchone()
    if not row or not valid_dingtalk_preview_token(row,job_id,token) or row["status"]!="pending_confirmation": c.close(); raise HTTPException(410,"任务已失效")
    if mode=="account":
        if account_key:
            account=c.execute("SELECT * FROM nurture_accounts WHERE account_key=? AND platform=? AND enabled=1",(account_key,platform)).fetchone()
            if not account: c.close(); raise HTTPException(422,"请选择已启用的本平台账号")
        else:
            account=select_generation_account(c, json.loads(row["images_json"]), row["note"], platform)
            if not account: c.close(); raise HTTPException(422,"该平台还没有完善人设且已启用自动发布的账号")
        slot=next_account_slot(c, account, exclude_job_id=job_id)
        content=generate_content(json.loads(row["images_json"]), row["note"], platform, account, scheduled_at=slot.isoformat() if slot else "")
        c.execute(f"UPDATE dingtalk_material_jobs SET {platform}_account_id=?,{platform}_account_key=?,{platform}_scheduled_at=?,{platform}_title=?,{platform}_body=?,{platform}_topics_json=?,updated_at=? WHERE id=?",(account["publish_account_id"],account["account_key"],slot.isoformat() if slot else "",content["title"],content["body"],json.dumps(content["topics"],ensure_ascii=False),now(),job_id))
    elif mode=="time":
        value=scheduled_at.strip()
        if value:
            try:
                parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
                if parsed <= datetime.now(parsed.tzinfo or timezone.utc): raise ValueError
            except ValueError: c.close(); raise HTTPException(422,"请输入未来的发布时间")
        account=c.execute("SELECT * FROM nurture_accounts WHERE account_key=? AND platform=? AND enabled=1",(row[f"{platform}_account_key"],platform)).fetchone()
        if account and value:
            content=generate_content(json.loads(row["images_json"]), row["note"], platform, account, scheduled_at=value)
            c.execute(f"UPDATE dingtalk_material_jobs SET {platform}_scheduled_at=?,{platform}_title=?,{platform}_body=?,{platform}_topics_json=?,updated_at=? WHERE id=?",(value,content["title"],content["body"],json.dumps(content["topics"],ensure_ascii=False),now(),job_id))
        else:
            c.execute(f"UPDATE dingtalk_material_jobs SET {platform}_scheduled_at=?,updated_at=? WHERE id=?",(value,now(),job_id))
    elif mode=="material":
        valid=[file for file in files if file.filename and (file.content_type or "").startswith("image/")]
        if not valid: c.close(); raise HTTPException(422,"请至少上传一张图片")
        folder=MEDIA/"pending"/job_id; shutil.rmtree(folder,ignore_errors=True); folder.mkdir(parents=True,exist_ok=True); images=[]
        for index,file in enumerate(valid[:18],1):
            suffix=Path(file.filename).suffix.lower() or '.jpg'; target=folder/f"{index:02d}{suffix}"; target.write_bytes(await file.read()); images.append(f"pending/{job_id}/{target.name}")
        other="xiaohongshu" if platform=="douyin" else "douyin"
        current=c.execute("SELECT * FROM nurture_accounts WHERE account_key=? AND platform=? AND enabled=1",(row[f"{platform}_account_key"],platform)).fetchone()
        other_current=c.execute("SELECT * FROM nurture_accounts WHERE account_key=? AND platform=? AND enabled=1",(row[f"{other}_account_key"],other)).fetchone()
        account=current or select_generation_account(c, images, "", platform)
        other_account=other_current or select_generation_account(c, images, "", other)
        new_current_slot=next_account_slot(c, account, exclude_job_id=job_id) if account else None
        new_other_slot=next_account_slot(c, other_account, exclude_job_id=job_id) if other_account else None
        current_slot=row[f"{platform}_scheduled_at"] or (new_current_slot.isoformat() if new_current_slot else "")
        other_slot=row[f"{other}_scheduled_at"] or (new_other_slot.isoformat() if new_other_slot else "")
        content=generate_content(images,"",platform,account,scheduled_at=current_slot); other_content=generate_content(images,"",other,other_account,scheduled_at=other_slot)
        c.execute(f"UPDATE dingtalk_material_jobs SET images_json=?,{platform}_account_id=?,{platform}_account_key=?,{platform}_scheduled_at=?,{platform}_title=?,{platform}_body=?,{platform}_topics_json=?,{other}_account_id=?,{other}_account_key=?,{other}_scheduled_at=?,{other}_title=?,{other}_body=?,{other}_topics_json=?,updated_at=? WHERE id=?",(json.dumps(images),account["publish_account_id"] if account else "",account["account_key"] if account else "",current_slot,content["title"],content["body"],json.dumps(content["topics"],ensure_ascii=False),other_account["publish_account_id"] if other_account else "",other_account["account_key"] if other_account else "",other_slot,other_content["title"],other_content["body"],json.dumps(other_content["topics"],ensure_ascii=False),now(),job_id))
    else: c.close(); raise HTTPException(422,"操作不存在")
    c.commit(); c.close(); return HTMLResponse("<meta charset='utf-8'><script>location.replace(document.referrer||'/')</script><p>已保存，请回到钉钉查看更新后的任务卡。</p>")

@app.get("/api/dingtalk/jobs/{job_id}/action")
def dingtalk_job_action(job_id:str, op:str, token:str, platform:str=""):
    if op not in {"regenerate", "confirm", "discard", "restore"}:
        raise HTTPException(404,"操作不存在")
    if platform not in {"douyin", "xiaohongshu"}:
        raise HTTPException(422,"请选择抖音或小红书版本")
    c=conn(); row=c.execute("SELECT * FROM dingtalk_material_jobs WHERE id=?",(job_id,)).fetchone()
    if not row:
        c.close(); raise HTTPException(404,"素材任务不存在")
    valid = valid_dingtalk_job_token(row, job_id, op, token, platform)
    if not valid:
        c.close(); return HTMLResponse("<meta charset='utf-8'><h2>这个预览已失效</h2><p>请回到钉钉重新发送图片。</p>",status_code=410)
    if op == "regenerate" and row[f"{platform}_regenerate_count"] >= 3:
        c.close(); return HTMLResponse("<meta charset='utf-8'><h2>已达到 3 次换一版上限</h2><p>可以直接入库，或重新发图生成新的素材。</p>")
    if row["action_request"]:
        c.close(); return HTMLResponse("<meta charset='utf-8'><h2>正在处理</h2><p>请回到钉钉，机器人会在几秒内更新预览。</p>")
    c.execute("UPDATE dingtalk_material_jobs SET action_request=?,updated_at=? WHERE id=?",(f"{op}:{platform}" if platform else op,now(),job_id)); c.commit(); c.close()
    labels={"regenerate":"换一版", "confirm":"立即入库", "discard":"放弃入库", "restore":"恢复入库"}
    prefix={"douyin":"抖音版·", "xiaohongshu":"小红书版·"}.get(platform, "")
    label=prefix + labels[op]
    return HTMLResponse(f"<meta charset='utf-8'><style>body{{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;padding:48px;color:#1f2937}}h2{{color:#1677ff}}</style><h2>{label}已提交</h2><p>请回到钉钉，机器人会在几秒内发送最新状态。</p>")
@app.post("/api/materials/import")
async def import_group(files:list[UploadFile]=File(...), note:str=Form("")):
    if not files: raise HTTPException(422,"请至少上传一张图片")
    if len(files)>18: raise HTTPException(422,"一组最多 18 张图片")
    material_id=uuid.uuid4().hex; folder=MEDIA/material_id; folder.mkdir(parents=True)
    images=[]
    try:
      for index,file in enumerate(files,1):
        if not (file.content_type or "").startswith("image/"): raise HTTPException(422,"只支持图片素材")
        suffix=Path(file.filename or "image.jpg").suffix.lower() or ".jpg"; name=f"{index:02d}{suffix}"; target=folder/name
        target.write_bytes(await file.read()); images.append(f"{material_id}/{name}")
      content=generate_content(images,note)
    except Exception as exc:
      shutil.rmtree(folder,ignore_errors=True)
      if isinstance(exc,HTTPException): raise
      raise HTTPException(502,"文案生成失败，请稍后重试") from exc
    # 入库只保存素材内容；发布时间必须由用户在发布队列中单独或批量设置。
    scheduled = None
    title=str(content.get("title", "")).strip()[:80] or "今天的小日常"
    body=str(content.get("body", "")).strip() or fallback_content(note)["body"]
    topics=[str(topic).strip().lstrip("#") for topic in content.get("topics", []) if str(topic).strip()][:6]
    timestamp=now(); c=conn(); c.execute("INSERT INTO materials(id,images_json,title,caption,topics_json,note,scheduled_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(material_id,json.dumps(images),title,body,json.dumps(topics,ensure_ascii=False),note,scheduled,timestamp,timestamp)); c.commit(); row=c.execute("SELECT * FROM materials WHERE id=?",(material_id,)).fetchone(); c.close(); return {"item":as_dict(row)}
class Caption(BaseModel): caption:str
class MaterialContent(BaseModel):
    title: str
    body: str
    topics: list[str] = []
class PublishConfig(BaseModel):
    account_id: str
    scheduled_at: Optional[str] = None
    music_enabled: bool = True
    music: dict = {}
class BatchPublishConfig(BaseModel):
    material_ids: list[str]
    account_id: str
    start_at: Optional[str] = None
    interval_minutes: int = 0
    music_enabled: bool = True
    music: dict = {}
class BatchDelete(BaseModel):
    material_ids: list[str]
@app.patch("/api/materials/{material_id}/caption")
def update_caption(material_id:str,payload:Caption):
    caption=payload.caption.strip()
    if not caption: raise HTTPException(422,"文案不能为空")
    c=conn(); changed=c.execute("UPDATE materials SET caption=?,updated_at=? WHERE id=?",(caption,now(),material_id)).rowcount; c.commit(); c.close()
    if not changed: raise HTTPException(404,"素材不存在")
    return {"ok":True}
@app.patch("/api/materials/{material_id}/content")
def update_content(material_id:str,payload:MaterialContent):
    title=payload.title.strip()
    body=payload.body.strip()
    topics=[topic.strip().lstrip("#") for topic in payload.topics if topic.strip()][:6]
    if not title: raise HTTPException(422,"标题不能为空")
    if not body: raise HTTPException(422,"正文不能为空")
    c=conn(); row=c.execute("SELECT * FROM materials WHERE id=?",(material_id,)).fetchone()
    if not row: c.close(); raise HTTPException(404,"素材不存在")
    c.execute("UPDATE materials SET title=?,caption=?,topics_json=?,updated_at=? WHERE id=?",(title[:80],body,json.dumps(topics,ensure_ascii=False),now(),material_id)); c.commit(); c.close()
    if row["publish_draft_id"]:
        try:
            response=httpx.patch(f"{PUBLISH_URL}/api/publish/drafts/{row['publish_draft_id']}/content",json={"title":title[:80],"content":body,"topics":topics},timeout=20)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(502,"素材库已更新，但同步发布草稿失败") from exc
    return {"ok":True}
@app.get("/api/publish/options")
def publish_options():
    accounts = gateway_request("GET", "/api/publish/accounts")
    music = gateway_request("GET", "/api/music/hot")
    return {"accounts":[item for item in accounts if int(item.get("enabled", 0)) == 1], "music":music}
@app.get("/api/account-sync/accounts")
def account_sync_accounts():
    return {"items": gateway_request("GET", "/api/publish/accounts")}
@app.post("/api/account-sync")
def sync_accounts_from_gateway():
    return gateway_request("POST", "/api/publish/accounts/sync")
@app.patch("/api/account-sync/accounts/{account_id}/note")
def save_account_type(account_id:int, payload:dict):
    return gateway_request("PATCH", f"/api/publish/accounts/{account_id}/note", json={"account_note":str(payload.get("account_note", ""))[:80]})
@app.put("/api/materials/{material_id}/publish-config")
def update_publish_config(material_id:str, payload:PublishConfig):
    return {"item":save_material_publish_config(material_id, payload.account_id, payload.scheduled_at, payload.music_enabled, payload.music)}
@app.delete("/api/materials/{material_id}")
def delete_material(material_id:str):
    return delete_material_record(material_id)
@app.post("/api/materials/batch-delete")
def batch_delete_materials(payload:BatchDelete):
    if not payload.material_ids or len(payload.material_ids) > 100:
        raise HTTPException(422, "请选择 1-100 条素材")
    results=[]
    for material_id in dict.fromkeys(payload.material_ids):
        try: results.append(delete_material_record(material_id))
        except HTTPException as exc: results.append({"id":material_id,"ok":False,"error":exc.detail})
    return {"items":results}
@app.post("/api/materials/batch/publish-config")
def batch_publish_config(payload:BatchPublishConfig):
    if not payload.material_ids or len(payload.material_ids) > 100:
        raise HTTPException(422,"请选择 1-100 条素材")
    if not 0 <= payload.interval_minutes <= 24 * 60:
        raise HTTPException(422,"发布间隔须为 0-1440 分钟")
    start = None
    if payload.start_at:
        try: start = datetime.fromisoformat(payload.start_at.replace("Z", "+00:00"))
        except ValueError as exc: raise HTTPException(422,"起始时间格式不正确") from exc
    items=[]
    for index, material_id in enumerate(payload.material_ids):
        scheduled = (start + timedelta(minutes=index * payload.interval_minutes)).isoformat() if start else None
        items.append(save_material_publish_config(material_id, payload.account_id, scheduled, payload.music_enabled, payload.music))
    return {"items":items}
@app.post("/api/materials/{material_id}/publish")
def publish_material(material_id:str):
    return submit_material(material_id)
@app.post("/api/materials/batch/publish")
def batch_publish_materials(material_ids:list[str]):
    if not material_ids or len(material_ids) > 100: raise HTTPException(422,"请选择 1-100 条素材")
    results=[]
    for material_id in material_ids:
        try: results.append({"id":material_id,"ok":True,**submit_material(material_id)})
        except HTTPException as exc: results.append({"id":material_id,"ok":False,"error":exc.detail})
    return {"items":results}
@app.post("/api/materials/{material_id}/refresh")
def refresh_material_publish(material_id:str):
    row=material_row(material_id)
    if not row["publish_job_id"]: raise HTTPException(422,"该素材还没有发布任务")
    item=sync_publish_status(row)
    return {"item":item}
@app.post("/api/materials/{material_id}/cancel")
def cancel_material_publish(material_id:str):
    row=material_row(material_id)
    if not row["publish_draft_id"]: raise HTTPException(422,"该素材没有可取消的定时任务")
    gateway_request("POST",f"/api/publish/drafts/{row['publish_draft_id']}/cancel")
    c=conn(); c.execute("UPDATE materials SET publish_job_id=NULL,status='queued',error='',updated_at=? WHERE id=?",(now(),material_id)); c.commit(); c.close()
    return {"item":as_dict(material_row(material_id))}
@app.post("/api/materials/{material_id}/push")
def push(material_id:str):
    settings=get_settings()
    if not settings["platform"] or not settings["account_key"]: raise HTTPException(422,"请先设置发布平台和账号")
    c=conn(); row=c.execute("SELECT * FROM materials WHERE id=?",(material_id,)).fetchone(); c.close()
    if not row: raise HTTPException(404,"素材不存在")
    item=as_dict(row)
    if item["publish_draft_id"]: return {"draft_id":item["publish_draft_id"],"already_pushed":True}
    try:
      draft=httpx.post(f"{PUBLISH_URL}/api/publish/drafts",json={"title":item.get("title") or "今天的小日常","content":item["caption"],"topics":item.get("topics", []),"content_type":"image","platforms":[settings["platform"]],"selected_accounts":{settings["platform"]:settings["account_key"]},"scheduled_at":item["scheduled_at"]},timeout=20).json(); draft_id=draft["id"]
      for image in item["images"]:
        with (MEDIA/image).open("rb") as f: response=httpx.post(f"{PUBLISH_URL}/api/publish/drafts/{draft_id}/assets",data={"asset_type":"image"},files={"file":(Path(image).name,f,"image/jpeg")},timeout=60); response.raise_for_status()
      c=conn(); c.execute("UPDATE materials SET status='pushed',publish_draft_id=?,updated_at=? WHERE id=?",(draft_id,now(),material_id)); c.commit(); c.close(); return {"draft_id":draft_id}
    except Exception as exc:
      c=conn(); c.execute("UPDATE materials SET error=?,updated_at=? WHERE id=?",(str(exc)[:200],now(),material_id)); c.commit(); c.close(); raise HTTPException(502,"推送蚁小二发布入口失败") from exc
@app.get("/media/{path:path}")
def media(path:str):
    file=(MEDIA/path).resolve()
    if not str(file).startswith(str(MEDIA.resolve())) or not file.is_file(): raise HTTPException(404,"图片不存在")
    return FileResponse(file)
app.mount("/",StaticFiles(directory=ROOT/"static",html=True),name="static")
