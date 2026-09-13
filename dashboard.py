import asyncio, base64, hashlib, logging, os, secrets, shutil, sqlite3, tarfile, tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import aiohttp
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DASHBOARD_DB", "dashboard.db"))
SYNC_ROOT = Path(os.getenv("GITHUB_SYNC_ROOT", "projects"))
CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_urlsafe(48)
FERNET = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SESSION_SECRET.encode()).digest()))
CHECK_EVERY = 30


def now(): return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS github_accounts(
          github_id INTEGER PRIMARY KEY, login TEXT NOT NULL, avatar TEXT,
          token TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS project_settings(
          github_id INTEGER PRIMARY KEY, repo TEXT, branch TEXT, auto_update INTEGER NOT NULL DEFAULT 0,
          last_sha TEXT, last_checked TEXT, last_synced TEXT, last_error TEXT);
        """)
        self.db.commit()
        try: os.chmod(DB_PATH, 0o600)
        except OSError: pass

    def account(self, gid):
        row = self.db.execute("SELECT * FROM github_accounts WHERE github_id=?", (gid,)).fetchone()
        if not row: return None
        item = dict(row)
        try: item["token"] = FERNET.decrypt(item["token"].encode()).decode()
        except Exception: return None
        return item

    def save_account(self, user, token):
        enc = FERNET.encrypt(token.encode()).decode()
        self.db.execute("""INSERT INTO github_accounts(github_id,login,avatar,token,updated_at)
          VALUES(?,?,?,?,?) ON CONFLICT(github_id) DO UPDATE SET
          login=excluded.login,avatar=excluded.avatar,token=excluded.token,updated_at=excluded.updated_at""",
          (user["id"], user["login"], user.get("avatar_url", ""), enc, now()))
        self.db.execute("INSERT OR IGNORE INTO project_settings(github_id) VALUES(?)", (user["id"],))
        self.db.commit()

    def disconnect(self, gid):
        self.db.execute("DELETE FROM project_settings WHERE github_id=?", (gid,))
        self.db.execute("DELETE FROM github_accounts WHERE github_id=?", (gid,))
        self.db.commit()

    def settings(self, gid):
        row = self.db.execute("SELECT * FROM project_settings WHERE github_id=?", (gid,)).fetchone()
        if not row:
            self.db.execute("INSERT INTO project_settings(github_id) VALUES(?)", (gid,)); self.db.commit()
            row = self.db.execute("SELECT * FROM project_settings WHERE github_id=?", (gid,)).fetchone()
        item = dict(row); item["auto_update"] = bool(item["auto_update"]); return item

    def save_settings(self, gid, repo, branch, enabled):
        old = self.settings(gid); reset = old.get("repo") != repo or old.get("branch") != branch
        self.db.execute("""UPDATE project_settings SET repo=?,branch=?,auto_update=?,last_sha=?,last_error=NULL
          WHERE github_id=?""", (repo, branch, int(enabled), None if reset else old.get("last_sha"), gid))
        self.db.commit()

    def status(self, gid, sha=None, synced=False, error=None):
        stamp = now()
        if synced:
            self.db.execute("UPDATE project_settings SET last_sha=?,last_checked=?,last_synced=?,last_error=NULL WHERE github_id=?",
                            (sha, stamp, stamp, gid))
        else:
            self.db.execute("UPDATE project_settings SET last_checked=?,last_error=? WHERE github_id=?", (stamp, error, gid))
        self.db.commit()

    def jobs(self):
        rows = self.db.execute("""SELECT s.*,a.token FROM project_settings s JOIN github_accounts a USING(github_id)
          WHERE s.auto_update=1 AND s.repo IS NOT NULL AND s.branch IS NOT NULL""").fetchall()
        out=[]
        for row in rows:
            item=dict(row)
            try: item["token"] = FERNET.decrypt(item["token"].encode()).decode(); out.append(item)
            except Exception: pass
        return out

store = Store()


async def gh(token, path, params=None):
    headers={"Authorization":f"Bearer {token}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"anonka-control"}
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.get("https://api.github.com"+path, params=params) as r:
            data=await r.json(content_type=None)
            if r.status >= 400: raise HTTPException(502, data.get("message","GitHub API error") if isinstance(data,dict) else "GitHub API error")
            return data


async def repos(token):
    out=[]
    for page in range(1,11):
        batch=await gh(token,"/user/repos",{"visibility":"all","affiliation":"owner,collaborator,organization_member","sort":"updated","per_page":100,"page":page})
        out += batch
        if len(batch)<100: break
    return out


def account(request):
    gid=request.session.get("github_id")
    acc=store.account(int(gid)) if gid else None
    if not acc: raise HTTPException(401,"GitHub is not connected")
    return acc


def callback(request): return f"{BASE_URL}/auth/github/callback" if BASE_URL else str(request.url_for("github_callback"))


def extract_archive(archive, dest):
    parent=dest.parent; parent.mkdir(parents=True, exist_ok=True)
    tmp=Path(tempfile.mkdtemp(prefix="sync-",dir=parent)); staged=parent/("."+dest.name+".next"); backup=parent/("."+dest.name+".old")
    try:
        with tarfile.open(archive,"r:*") as tf:
            for m in tf.getmembers():
                p=Path(m.name)
                if p.is_absolute() or ".." in p.parts or m.issym() or m.islnk(): continue
                tf.extract(m,tmp)
        roots=list(tmp.iterdir()); src=roots[0] if len(roots)==1 and roots[0].is_dir() else tmp
        shutil.rmtree(staged,ignore_errors=True); shutil.rmtree(backup,ignore_errors=True); shutil.copytree(src,staged)
        if dest.exists(): dest.rename(backup)
        staged.rename(dest); shutil.rmtree(backup,ignore_errors=True)
    finally:
        shutil.rmtree(tmp,ignore_errors=True); Path(archive).unlink(missing_ok=True)


async def download(token, repo, branch, dest):
    headers={"Authorization":f"Bearer {token}","Accept":"application/vnd.github+json","User-Agent":"anonka-control"}
    fd,name=tempfile.mkstemp(suffix=".tar.gz"); os.close(fd); archive=Path(name)
    try:
        async with aiohttp.ClientSession(headers=headers,timeout=aiohttp.ClientTimeout(total=120)) as s:
            async with s.get(f"https://api.github.com/repos/{repo}/tarball/{quote(branch,safe='')}") as r:
                if r.status>=400: raise RuntimeError(f"archive HTTP {r.status}")
                with archive.open("wb") as f:
                    async for chunk in r.content.iter_chunked(1024*1024): f.write(chunk)
        await asyncio.to_thread(extract_archive,archive,dest)
    except Exception:
        archive.unlink(missing_ok=True); raise


async def sync(job):
    gid=int(job["github_id"])
    try:
        b=await gh(job["token"],f"/repos/{job['repo']}/branches/{quote(job['branch'],safe='')}"); sha=b["commit"]["sha"]
        if sha==job.get("last_sha"): store.status(gid); return
        name=job["repo"].split("/",1)[-1].replace("..","_")
        await download(job["token"],job["repo"],job["branch"],SYNC_ROOT/str(gid)/name)
        store.status(gid,sha,True); log.info("Synced %s@%s",job["repo"],job["branch"])
    except Exception as e:
        log.exception("GitHub auto-update failed"); store.status(gid,error=str(e)[:500])


async def worker():
    while True:
        for job in store.jobs(): await sync(job)
        await asyncio.sleep(CHECK_EVERY)


@asynccontextmanager
async def lifespan(app):
    task=asyncio.create_task(worker())
    try: yield
    finally:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass

app=FastAPI(title="Anonka Control",lifespan=lifespan)
app.add_middleware(SessionMiddleware,secret_key=SESSION_SECRET,same_site="lax",https_only=BASE_URL.startswith("https://"),max_age=2592000)


class Settings(BaseModel):
    repo: str
    branch: str
    auto_update: bool=False


@app.get("/")
@app.get("/profile")
async def index(): return FileResponse(ROOT/"static"/"index.html")

@app.get("/health")
async def health(): return {"status":"ok"}

@app.get("/auth/github")
async def login(request:Request):
    if not CLIENT_ID or not CLIENT_SECRET: raise HTTPException(503,"GitHub OAuth is not configured")
    state=secrets.token_urlsafe(32); request.session["oauth_state"]=state
    q=urlencode({"client_id":CLIENT_ID,"redirect_uri":callback(request),"scope":"repo read:user","state":state})
    return RedirectResponse("https://github.com/login/oauth/authorize?"+q)

@app.get("/auth/github/callback",name="github_callback")
async def github_callback(request:Request,code:str="",state:str=""):
    expected=request.session.pop("oauth_state",None)
    if not expected or not state or not secrets.compare_digest(expected,state) or not code: raise HTTPException(400,"Invalid OAuth state")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.post("https://github.com/login/oauth/access_token",headers={"Accept":"application/json"},data={"client_id":CLIENT_ID,"client_secret":CLIENT_SECRET,"code":code,"redirect_uri":callback(request)}) as r:
            data=await r.json(content_type=None)
    token=data.get("access_token")
    if not token: raise HTTPException(502,"GitHub did not return an access token")
    user=await gh(token,"/user"); store.save_account(user,token); request.session["github_id"]=user["id"]
    return RedirectResponse("/?github=connected",303)

@app.get("/api/profile")
async def profile(request:Request):
    gid=request.session.get("github_id"); acc=store.account(int(gid)) if gid else None
    return {"oauth_configured":bool(CLIENT_ID and CLIENT_SECRET),"connected":bool(acc),"account":None if not acc else {"id":acc["github_id"],"login":acc["login"],"avatar":acc["avatar"]}}

@app.post("/api/github/disconnect")
async def disconnect(request:Request):
    gid=request.session.get("github_id")
    if gid: store.disconnect(int(gid))
    request.session.clear(); return {"ok":True}

@app.get("/api/github/repos")
async def list_repos(request:Request):
    acc=account(request); data=await repos(acc["token"])
    return [{"full_name":r["full_name"],"private":bool(r.get("private")),"default_branch":r.get("default_branch") or "main"} for r in data]

@app.get("/api/github/branches")
async def branches(request:Request,repo:str):
    acc=account(request); allowed={r["full_name"] for r in await repos(acc["token"])}
    if repo not in allowed: raise HTTPException(403,"Repository is not accessible")
    data=await gh(acc["token"],f"/repos/{repo}/branches",{"per_page":100})
    return [{"name":b["name"],"sha":b["commit"]["sha"]} for b in data]

@app.get("/api/project/settings")
async def get_settings(request:Request):
    acc=account(request); data=store.settings(acc["github_id"]); data["check_interval_seconds"]=CHECK_EVERY; return data

@app.post("/api/project/settings")
async def save_settings(request:Request,payload:Settings):
    acc=account(request); allowed={r["full_name"] for r in await repos(acc["token"])}
    if payload.repo not in allowed: raise HTTPException(403,"Repository is not accessible")
    try: await gh(acc["token"],f"/repos/{payload.repo}/branches/{quote(payload.branch,safe='')}")
    except HTTPException: raise HTTPException(400,"Branch does not exist")
    store.save_settings(acc["github_id"],payload.repo,payload.branch,payload.auto_update)
    return {"ok":True,"settings":store.settings(acc["github_id"])}

@app.post("/api/project/sync-now")
async def sync_now(request:Request):
    acc=account(request); s=store.settings(acc["github_id"])
    if not s.get("repo") or not s.get("branch"): raise HTTPException(400,"Select repository and branch first")
    s["token"]=acc["token"]; await sync(s); return {"ok":True}
