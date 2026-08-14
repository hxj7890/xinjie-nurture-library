import base64, hashlib, hmac, html, json, os, re, shutil, sqlite3, time, uuid
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
    c.commit(); c.close(); MEDIA.mkdir(parents=True,exist_ok=True)
def get_settings():
    c=conn(); values={r["key"]:r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return values
def next_slot(settings):
    raw=settings.get("next_publish_at", "")
    if not raw: return None
    return datetime.fromisoformat(raw.replace("Z","+00:00"))
def image_prompt(files, note, retry=False):
    if not OPENAI_KEY: return ""
    retry_rule="这是自动重试，必须结合图片里的具体物品、场景或动作写完整内容；绝不能使用“今天的小日常”“今天留下一点日常”等泛化占位语。" if retry else ""
    content=[{"type":"text","text":"根据这些照片生成一条真实日常分享。只返回 JSON：{\"title\":\"不超过20字的自然标题\",\"body\":\"30到100字的正文，像真人随手记录，可有轻微吐槽或语气词，不虚构地点、人物关系或经历\",\"topics\":[\"2到4个不带#的话题\"]}。" + retry_rule + ("用户补充："+note if note else "")}]
    for path in files:
        data=base64.b64encode((MEDIA/path).read_bytes()).decode(); content.append({"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{data}"}})
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

def generate_content(files, note):
    """Generate usable copy twice at most; never silently save filler copy."""
    last_error = None
    for attempt in range(2):
        try:
            candidate = image_prompt(files, note, retry=attempt > 0)
            if low_quality_content(candidate):
                raise ValueError("low-quality copy response")
            return candidate
        except Exception as exc:
            last_error = exc
    raise RuntimeError("文案生成结果不符合质量要求") from last_error
def fallback_content(note): return {"title":"今天的小日常", "body":note.strip() or "今天留下一点日常。", "topics":[]}
def serialize_all():
    c=conn(); rows=[as_dict(r) for r in c.execute("SELECT * FROM materials ORDER BY CASE status WHEN 'queued' THEN 0 ELSE 1 END, scheduled_at, created_at")]; c.close(); return rows

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

def save_material_publish_config(material_id, account_id, scheduled_at, music_enabled=True, music=None):
    row = material_row(material_id)
    if row["publish_job_id"]:
        raise HTTPException(409, "该素材已提交发布任务，不能再修改；请先刷新状态或到蚁小二处理")
    account = account_by_id(account_id)
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

app=FastAPI(title="养号素材库",version="0.1.0")
@app.on_event("startup")
def startup(): init_db()
@app.get("/api/health")
def health(): return {"status":"ok","release":os.getenv("XINJIE_RELEASE_VERSION","local"),"released_at":os.getenv("XINJIE_RELEASED_AT","")}
@app.get("/api/materials")
def materials(): return {"items":serialize_all(),"settings":get_settings()}
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
@app.get("/api/nurture/accounts")
def nurture_accounts():
    c=conn(); rows=[dict(row) for row in c.execute("SELECT * FROM nurture_accounts ORDER BY priority, id")]; c.close(); return {"items":rows}
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
    return {"configured":bool(os.getenv("DINGTALK_NURTURE_APP_KEY") and os.getenv("DINGTALK_NURTURE_APP_SECRET")),"bound_group":bool(rows.get("conversation_id")),"pending_confirmation":pending,"last_heartbeat":rows.get("last_heartbeat","")}

def action_signature(job_id:str, action:str, deadline:int):
    secret=os.getenv("DINGTALK_NURTURE_APP_SECRET", "").encode()
    raw=f"{job_id}:{action}:{deadline}".encode()
    return hmac.new(secret, raw, hashlib.sha256).hexdigest()[:24]

def valid_dingtalk_job_token(row, job_id:str, op:str, token:str):
    return (row["status"] == "pending_confirmation" and now() <= row["confirm_deadline"]
            and hmac.compare_digest(token, action_signature(job_id, op, row["confirm_deadline"])))

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
    topics=" ".join("#"+html.escape(x) for x in json.loads(row["topics_json"] or "[]")) or "#日常记录"
    deadline=int(row["confirm_deadline"])
    discard=f"{PUBLIC_URL}/api/dingtalk/jobs/{job_id}/action?op=discard&token={action_signature(job_id,'discard',deadline)}"
    regenerate=f"{PUBLIC_URL}/api/dingtalk/jobs/{job_id}/action?op=regenerate&token={action_signature(job_id,'regenerate',deadline)}"
    adjustable=row["status"] == "pending_confirmation" and now() < deadline
    discard_button=(f"<a id='discard' class='button' href='{html.escape(discard, quote=True)}'>放弃入库</a>" if adjustable else "<span id='discard' class='button disabled' aria-disabled='true'>放弃入库</span>")
    regenerate_button=(f"<a id='regenerate' class='button regenerate' href='{html.escape(regenerate, quote=True)}'>换一版</a>" if adjustable else "<span id='regenerate' class='button disabled' aria-disabled='true'>换一版</span>")
    discarded = row["status"] == "discarded"
    final_hint=("倒计时结束后会自动入库，并按账号队列安排。" if adjustable else ("该素材已放弃入库，不会进入发布队列。" if discarded else "已自动入库，并已按账号队列安排。"))
    return HTMLResponse(f"""<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>养号素材预览</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f5f7fb;color:#172033;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif}}
    main{{max-width:680px;margin:auto;background:white;min-height:100vh}}header{{padding:22px 20px;background:#eaf3ff;color:#1677ff;font-size:22px;font-weight:700}}
    .images{{display:grid;gap:10px;padding:12px;background:#f5f7fb}}img{{display:block;width:100%;max-height:560px;object-fit:contain;background:#f4f5f7;border-radius:10px}}
    section{{padding:0 20px}}h3{{font-size:16px;margin:22px 0 8px}}p{{font-size:17px;line-height:1.7;margin:0;white-space:pre-wrap}}.topics{{font-weight:600}}
    .timer{{margin:22px 0 10px;padding-top:18px;border-top:1px solid #e9edf3;font-size:16px}}.timer strong{{font-size:22px}}.hint{{color:#667085;font-size:14px}}
    .actions{{display:grid;gap:12px;margin:20px 0 30px}}.button{{display:block;padding:15px;text-align:center;text-decoration:none;border-radius:12px;font-size:17px;font-weight:600;background:#f1f3f5;color:#26324a}}.button.regenerate{{background:#1677ff;color:white}}.button.disabled{{background:#eaecf0;color:#98a2b3;cursor:not-allowed}}
    </style><main><header>养号素材预览</header><div class='images'>{images}</div><section>
    <h3>标题</h3><p>{html.escape(row['title'])}</p><h3>正文</h3><p>{html.escape(row['body'])}</p><h3>话题</h3><p class='topics'>{topics}</p>
    <div class='timer'>还可调整 <strong id='countdown'>--:--</strong> · 已换 {row['regenerate_count']} / 3 版</div><p id='hint' class='hint'>{final_hint}</p>
    <div class='actions'>{discard_button}{regenerate_button}</div>
    </section></main><script>const deadline={deadline}*1000,el=document.getElementById('countdown'),hint=document.getElementById('hint'),discarded={str(discarded).lower()};function finish(){{el.textContent='00:00';hint.textContent=discarded?'该素材已放弃入库，不会进入发布队列。':'已自动入库，并已按账号队列安排。';for(const id of ['discard','regenerate']){{const button=document.getElementById(id);if(button){{button.removeAttribute('href');button.className='button disabled';button.setAttribute('aria-disabled','true');}}}}}}function tick(){{const s=Math.max(0,Math.ceil((deadline-Date.now())/1000));el.textContent=String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');if(!s){{finish();clearInterval(timer);}}}}tick();const timer=setInterval(tick,1000);</script>""")

@app.get("/api/dingtalk/jobs/{job_id}/action")
def dingtalk_job_action(job_id:str, op:str, token:str):
    if op not in {"regenerate", "confirm", "discard"}:
        raise HTTPException(404,"操作不存在")
    c=conn(); row=c.execute("SELECT * FROM dingtalk_material_jobs WHERE id=?",(job_id,)).fetchone()
    if not row:
        c.close(); raise HTTPException(404,"素材任务不存在")
    valid = valid_dingtalk_job_token(row, job_id, op, token)
    if not valid:
        c.close(); return HTMLResponse("<meta charset='utf-8'><h2>这个预览已失效</h2><p>请回到钉钉重新发送图片。</p>",status_code=410)
    if op == "regenerate" and row["regenerate_count"] >= 3:
        c.close(); return HTMLResponse("<meta charset='utf-8'><h2>已达到 3 次换一版上限</h2><p>可以直接入库，或重新发图生成新的素材。</p>")
    if row["action_request"]:
        c.close(); return HTMLResponse("<meta charset='utf-8'><h2>正在处理</h2><p>请回到钉钉，机器人会在几秒内更新预览。</p>")
    c.execute("UPDATE dingtalk_material_jobs SET action_request=?,updated_at=? WHERE id=?",(op,now(),job_id)); c.commit(); c.close()
    label={"regenerate":"换一版", "confirm":"立即入库", "discard":"放弃入库"}[op]
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
    settings=get_settings(); slot=next_slot(settings); scheduled=slot.isoformat() if slot else None
    if slot:
      following=slot+timedelta(days=int(settings["interval_days"])); c=conn(); c.execute("UPDATE settings SET value=? WHERE key='next_publish_at'",(following.isoformat(),)); c.commit(); c.close()
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
@app.put("/api/materials/{material_id}/publish-config")
def update_publish_config(material_id:str, payload:PublishConfig):
    return {"item":save_material_publish_config(material_id, payload.account_id, payload.scheduled_at, payload.music_enabled, payload.music)}
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
    job=gateway_request("POST",f"/api/publish/jobs/{row['publish_job_id']}/refresh")
    status_map={"published":"published","failed":"failed","scheduled":"scheduled","submitted":"submitted"}
    c=conn(); c.execute("UPDATE materials SET status=?,error=?,updated_at=? WHERE id=?",(status_map.get(job.get("status"),"submitted"),job.get("error_message","")[:240],now(),material_id)); c.commit(); c.close()
    return {"item":as_dict(material_row(material_id)),"job":job}
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
