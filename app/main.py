import base64, json, os, re, shutil, sqlite3, time, uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.getenv("APP_DB_PATH", ROOT / "data/nurture_library.sqlite3"))
MEDIA = Path(os.getenv("APP_MEDIA_DIR", ROOT / "media"))
PUBLISH_URL = os.getenv("PUBLISH_GATEWAY_URL", "http://publish:8010").rstrip("/")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

def conn():
    DB.parent.mkdir(parents=True, exist_ok=True); c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
def now(): return int(time.time())
def as_dict(row):
    item=dict(row); item["images"]=json.loads(item.pop("images_json") or "[]"); return item
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
    c.commit(); c.close(); MEDIA.mkdir(parents=True,exist_ok=True)
def get_settings():
    c=conn(); values={r["key"]:r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return values
def next_slot(settings):
    raw=settings.get("next_publish_at", "")
    if not raw: return None
    return datetime.fromisoformat(raw.replace("Z","+00:00"))
def image_prompt(files, note):
    if not OPENAI_KEY: return ""
    content=[{"type":"text","text":"你是一个记录真实日常的人。根据这些照片写且只写一条中文生活分享文案，30到80字，自然克制、有画面感，不加标题、标签、营销话术，不虚构地点、人物关系或经历。" + ("用户补充："+note if note else "")}]
    for path in files:
        data=base64.b64encode((MEDIA/path).read_bytes()).decode(); content.append({"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{data}"}})
    payload={"model":OPENAI_MODEL,"messages":[{"role":"user","content":content}],"max_tokens":180}
    response=httpx.post(f"{OPENAI_BASE_URL}/chat/completions",headers={"Authorization":f"Bearer {OPENAI_KEY}"},json=payload,timeout=60); response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()
def fallback_caption(note): return note.strip() or "今天留下一点日常。"
def serialize_all():
    c=conn(); rows=[as_dict(r) for r in c.execute("SELECT * FROM materials ORDER BY CASE status WHEN 'queued' THEN 0 ELSE 1 END, scheduled_at, created_at")]; c.close(); return rows

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
      caption=image_prompt(images,note) or fallback_caption(note)
    except Exception as exc:
      shutil.rmtree(folder,ignore_errors=True)
      if isinstance(exc,HTTPException): raise
      raise HTTPException(502,"文案生成失败，请稍后重试") from exc
    settings=get_settings(); slot=next_slot(settings); scheduled=slot.isoformat() if slot else None
    if slot:
      following=slot+timedelta(days=int(settings["interval_days"])); c=conn(); c.execute("UPDATE settings SET value=? WHERE key='next_publish_at'",(following.isoformat(),)); c.commit(); c.close()
    timestamp=now(); c=conn(); c.execute("INSERT INTO materials(id,images_json,caption,note,scheduled_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(material_id,json.dumps(images),caption,note,scheduled,timestamp,timestamp)); c.commit(); row=c.execute("SELECT * FROM materials WHERE id=?",(material_id,)).fetchone(); c.close(); return {"item":as_dict(row)}
class Caption(BaseModel): caption:str
@app.patch("/api/materials/{material_id}/caption")
def update_caption(material_id:str,payload:Caption):
    caption=payload.caption.strip()
    if not caption: raise HTTPException(422,"文案不能为空")
    c=conn(); changed=c.execute("UPDATE materials SET caption=?,updated_at=? WHERE id=?",(caption,now(),material_id)).rowcount; c.commit(); c.close()
    if not changed: raise HTTPException(404,"素材不存在")
    return {"ok":True}
@app.post("/api/materials/{material_id}/push")
def push(material_id:str):
    settings=get_settings()
    if not settings["platform"] or not settings["account_key"]: raise HTTPException(422,"请先设置发布平台和账号")
    c=conn(); row=c.execute("SELECT * FROM materials WHERE id=?",(material_id,)).fetchone(); c.close()
    if not row: raise HTTPException(404,"素材不存在")
    item=as_dict(row)
    if item["publish_draft_id"]: return {"draft_id":item["publish_draft_id"],"already_pushed":True}
    try:
      draft=httpx.post(f"{PUBLISH_URL}/api/publish/drafts",json={"title":"","content":item["caption"],"topics":[],"content_type":"image","platforms":[settings["platform"]],"selected_accounts":{settings["platform"]:settings["account_key"]},"scheduled_at":item["scheduled_at"]},timeout=20).json(); draft_id=draft["id"]
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
