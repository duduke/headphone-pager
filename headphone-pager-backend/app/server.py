
import os
import subprocess
import time
import tempfile
import json
import uuid
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Literal, Dict, Any
# -----------------------------
# Audio conversion (server-side)
# -----------------------------
def _ffmpeg_path() -> str:
    return os.environ.get("FFMPEG_PATH", "ffmpeg")

def convert_to_wav_bytes(input_bytes: bytes, input_ext: str = ".bin") -> bytes:
    """Convert arbitrary audio bytes to WAV (PCM s16le, 48kHz, stereo) using ffmpeg."""
    with tempfile.TemporaryDirectory() as td:
        in_path = Path(td) / f"in{input_ext or '.bin'}"
        out_path = Path(td) / "out.wav"
        in_path.write_bytes(input_bytes)

        cmd = [
            _ffmpeg_path(),
            "-y",
            "-i", str(in_path),
            "-vn",
            "-ac", "2",
            "-ar", "48000",
            "-c:a", "pcm_s16le",
            "-f", "wav",
            str(out_path),
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail=f"ffmpeg conversion failed: {proc.stderr.decode('utf-8', errors='ignore')[:1200]}")
        wav = out_path.read_bytes()

    if len(wav) < 12 or wav[0:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise HTTPException(status_code=500, detail="Converted audio is not a valid WAV (RIFF/WAVE header missing)")
    return wav



def _ffmpeg_path() -> str:
    return os.environ.get("FFMPEG_PATH", "ffmpeg")

def _is_wav_filename(name: str) -> bool:
    return name.lower().endswith(".wav")

def convert_to_wav(input_path: str, output_path: str) -> None:
    """Convert arbitrary audio file to WAV (PCM s16le, 48kHz, stereo) using ffmpeg."""
    cmd = [
        _ffmpeg_path(),
        "-y",
        "-i", input_path,
        "-ac", "2",
        "-ar", "48000",
        "-c:a", "pcm_s16le",
        output_path,
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode('utf-8', errors='ignore')[:4000]}")


from fastapi import FastAPI, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# -----------------------------
# Configuration (env)
# -----------------------------
DB_PATH = os.getenv("DB_PATH", "/data/app.db")
BLOB_DIR = os.getenv("BLOB_DIR", "/data/blobs")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
DEFAULT_MESSAGE_TTL_SECONDS = int(os.getenv("DEFAULT_MESSAGE_TTL_SECONDS", "600"))  # 10 min
PAIRING_CODE_TTL_SECONDS = int(os.getenv("PAIRING_CODE_TTL_SECONDS", "300"))        # 5 min
LONGPOLL_TIMEOUT_SECONDS = int(os.getenv("LONGPOLL_TIMEOUT_SECONDS", "45"))
BASE_URL = os.getenv("BASE_URL", "")

if not ADMIN_TOKEN:
    # Allow running locally without env, but strongly recommend setting it.
    # In Docker, provide ADMIN_TOKEN.
    ADMIN_TOKEN = "dev-admin-token-change-me"

Path(BLOB_DIR).mkdir(parents=True, exist_ok=True)
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

# -----------------------------
# Utilities
# -----------------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def dt_to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def iso_to_dt(s: str) -> datetime:
    # Accept Z or offset
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)

def new_token(nbytes: int = 32) -> str:
    # URL-safe token
    return uuid.uuid4().hex + uuid.uuid4().hex

def new_pairing_code() -> str:
    # 6-digit code; avoid leading zeros? keep simple.
    import secrets
    return f"{secrets.randbelow(1_000_000):06d}"

def safe_ext(content_type: str, filename: str) -> str:
    # best-effort extension
    ext = ""
    if filename:
        ext = Path(filename).suffix.lower()
        if len(ext) > 8:
            ext = ""
    if not ext:
        if content_type == "audio/webm":
            ext = ".webm"
        elif content_type in ("audio/wav", "audio/x-wav"):
            ext = ".wav"
        elif content_type == "audio/mpeg":
            ext = ".mp3"
        elif content_type == "audio/ogg":
            ext = ".ogg"
    return ext or ".bin"

# -----------------------------
# SQLite (simple, reliable)
# -----------------------------
_db_lock = asyncio.Lock()

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS devices (
        device_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        device_token TEXT NOT NULL,
        paired_at TEXT NOT NULL,
        last_seen_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pairing_codes (
        code TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT,
        claimed_device_id TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        message_id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL,
        type TEXT NOT NULL,
        text TEXT,
        audio_blob_key TEXT,
        priority TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        state TEXT NOT NULL,
        details TEXT,
        FOREIGN KEY(device_id) REFERENCES devices(device_id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS audio_blobs (
        blob_key TEXT PRIMARY KEY,
        content_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        file_path TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()

async def db_exec(sql: str, params: tuple = ()) -> None:
    async with _db_lock:
        def _run():
            conn = _connect()
            conn.execute(sql, params)
            conn.commit()
            conn.close()
        await asyncio.to_thread(_run)

async def db_fetchone(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    async with _db_lock:
        def _run():
            conn = _connect()
            cur = conn.execute(sql, params)
            row = cur.fetchone()
            conn.close()
            return row
        return await asyncio.to_thread(_run)

async def db_fetchall(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    async with _db_lock:
        def _run():
            conn = _connect()
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
            conn.close()
            return rows
        return await asyncio.to_thread(_run)

# -----------------------------
# In-memory notifiers for long-poll
# -----------------------------
_device_conditions: Dict[str, asyncio.Condition] = {}
_conditions_lock = asyncio.Lock()

async def _get_condition(device_id: str) -> asyncio.Condition:
    async with _conditions_lock:
        cond = _device_conditions.get(device_id)
        if cond is None:
            cond = asyncio.Condition()
            _device_conditions[device_id] = cond
        return cond

async def notify_device(device_id: str) -> None:
    cond = await _get_condition(device_id)
    async with cond:
        cond.notify_all()

# -----------------------------
# Auth dependencies
# -----------------------------
def _bearer_token(req: Request) -> str:
    auth = req.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return ""
    return auth.split(" ", 1)[1].strip()

async def require_admin(req: Request) -> None:
    token = _bearer_token(req)
    if not token or token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized (admin)")

class DeviceContext(BaseModel):
    device_id: str
    name: str

async def require_device(req: Request, device_id: str) -> DeviceContext:
    token = _bearer_token(req)
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized (device)")
    row = await db_fetchone("SELECT device_id, name, device_token FROM devices WHERE device_id = ?", (device_id,))
    if not row or row["device_token"] != token:
        raise HTTPException(status_code=401, detail="Unauthorized (device)")
    # Update last seen (best-effort)
    await db_exec("UPDATE devices SET last_seen_at = ? WHERE device_id = ?", (dt_to_iso(utcnow()), device_id))
    return DeviceContext(device_id=row["device_id"], name=row["name"])

async def require_device_or_admin(req: Request, device_id: str) -> None:
    """Allow either:
    - admin bearer token (ADMIN_TOKEN), or
    - the device's bearer token for the given device_id.
    """
    token = _bearer_token(req)
    if token and token == ADMIN_TOKEN:
        return
    # Will raise 401 if invalid
    await require_device(req, device_id=device_id)

# -----------------------------
# Pydantic models
# -----------------------------
MessageType = Literal["tts", "audio"]
PriorityType = Literal["normal", "urgent"]
AckStatus = Literal["played", "failed", "expired"]

class PairStartResponse(BaseModel):
    code: str
    expiresAt: str

class PairCompleteRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=12)
    deviceName: str = Field(..., min_length=1, max_length=100)

class PairCompleteResponse(BaseModel):
    deviceId: str
    deviceToken: str

class EnqueueMessageRequest(BaseModel):
    type: MessageType
    text: Optional[str] = None
    audioBlobKey: Optional[str] = None
    priority: PriorityType = "normal"
    ttlSeconds: Optional[int] = None
    expiresAt: Optional[str] = None  # ISO8601, optional alternative to ttlSeconds

class EnqueueMessageResponse(BaseModel):
    messageId: str
    expiresAt: str

class NextMessageResponse(BaseModel):
    messageId: str
    type: MessageType
    text: Optional[str] = None
    audioUrl: Optional[str] = None
    audioBlobKey: Optional[str] = None
    priority: PriorityType
    createdAt: str
    expiresAt: str

class AckRequest(BaseModel):
    status: AckStatus
    details: Optional[str] = None
    playedAt: Optional[str] = None

class UploadAudioResponse(BaseModel):
    audioBlobKey: str
    contentType: str
    sizeBytes: int

# -----------------------------
# App
# -----------------------------
app = FastAPI(title="Headphone Pager", version="1.0")

@app.on_event("startup")
async def _startup():
    init_db()

# -----------------------------
# UI (simple single page)
# -----------------------------
_UI_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Headphone Pager</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 900px; margin: 24px auto; padding: 0 16px; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
    input, select, textarea, button { font-size: 16px; padding: 10px; }
    textarea { width: 100%; min-height: 80px; }
    .card { border: 1px solid #ddd; border-radius: 12px; padding: 16px; margin: 16px 0; }
    .muted { color: #666; }
    code { background:#f6f6f6; padding:2px 6px; border-radius:6px; }
    .pill { display:inline-block; padding:4px 10px; border-radius:999px; background:#f3f3f3; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    h3 { margin-bottom: 8px; }
  </style>
</head>
<body>
<h1>Headphone Pager</h1>
<p class="muted">Single-container FastAPI backend. Enter your admin token once per session.</p>

<div class="card">
  <h2>1) Admin token</h2>
  <div class="row">
    <input id="adminToken" type="password" placeholder="ADMIN_TOKEN" style="flex:1; min-width: 260px;"/>
    <button onclick="saveToken()">Save</button>
  </div>
  <p class="muted">Token is stored in <code>localStorage</code> in this browser only.</p>
</div>

<div class="card">
  <h2>2) Pair a device</h2>
  <button onclick="pairStart()">Generate pairing code</button>
  <p id="pairOut"></p>
</div>

<div class="card">
  <h2>3) Send a message</h2>

  <div class="row">
    <select id="deviceSelect" style="flex:1; min-width: 260px;" onchange="onDeviceSelected()">
      <option value="">Select device…</option>
    </select>
    <button onclick="loadDevices(true)" type="button">Refresh</button>

    <select id="priority">
      <option value="normal">normal</option>
      <option value="urgent">urgent</option>
    </select>
    <select id="ttl">
      <option value="60">expires in 1m</option>
      <option value="300">expires in 5m</option>
      <option value="600" selected>expires in 10m</option>
      <option value="1800">expires in 30m</option>
      <option value="3600">expires in 1h</option>
    </select>

    <!-- hidden field used by send functions -->
    <input id="deviceId" placeholder="Device ID" style="flex:1; min-width: 260px; display:none;"/>
  </div>

  <p class="muted" id="devicesOut"></p>
  <p class="muted" id="deviceMeta"></p>

  <h3>Voice message</h3>
  <p class="muted">Upload an audio file or record directly in the browser. The server converts audio to <code>.wav</code> for client simplicity.</p>

  <div class="row">
    <input id="audioFile" type="file" accept="audio/*"/>
    <button onclick="sendAudioFile()">Upload + Send</button>
  </div>

  <div class="row" style="margin-top: 8px;">
    <button id="recStart" onclick="startRecording()">Start recording</button>
    <button id="recStop" onclick="stopRecording()" disabled>Stop</button>
    <span id="recStatus" class="pill">idle</span>
  </div>

  <div class="row" style="margin-top: 8px;">
    <audio id="recPreview" controls style="width: 100%; display:none;"></audio>
  </div>

  <div class="row" style="margin-top: 8px;">
    <button id="sendRecordedBtn" onclick="sendRecorded()" disabled>Send recorded message</button>
  </div>

  <p id="sendOut"></p>
</div>

<script>
function getToken() {
  return localStorage.getItem("adminToken") || "";
}
function saveToken() {
  const t = document.getElementById("adminToken").value.trim();
  localStorage.setItem("adminToken", t);
  alert("Saved.");
}
document.getElementById("adminToken").value = getToken();

async function pairStart() {
  const res = await fetch("/api/pairing/start", {
    method: "POST",
    headers: { "Authorization": "Bearer " + getToken() }
  });
  const out = document.getElementById("pairOut");
  if (!res.ok) { out.textContent = "Error: " + res.status; return; }
  const j = await res.json();
  out.innerHTML = "Pairing code: <code>" + j.code + "</code> (expires " + j.expiresAt + ")";
}

async function loadDevices(selectFirst=false) {
  const out = document.getElementById("devicesOut");
  const meta = document.getElementById("deviceMeta");
  out.textContent = "Loading devices…";
  meta.textContent = "";
  const res = await fetch("/api/devices", {
    headers: { "Authorization": "Bearer " + getToken() }
  });
  if (!res.ok) { out.textContent = "Failed to load devices: " + res.status; return; }
  const list = await res.json();

  const sel = document.getElementById("deviceSelect");
  sel.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Select device…";
  sel.appendChild(placeholder);

  window._devicesById = {};
  for (const d of list) {
    window._devicesById[d.deviceId] = d;
    const opt = document.createElement("option");
    opt.value = d.deviceId;
    opt.textContent = d.name ? d.name : d.deviceId;
    sel.appendChild(opt);
  }

  if (list.length === 0) {
    out.textContent = "No devices registered yet. Pair a device first.";
    return;
  }
  out.textContent = "Loaded " + list.length + " device(s).";

  if (selectFirst && !sel.value) sel.value = list[0].deviceId;
  onDeviceSelected();
}

function formatLastSeen(lastSeenAt) {
  if (!lastSeenAt) return "never";
  try { return new Date(lastSeenAt).toLocaleString(); }
  catch { return lastSeenAt; }
}

function onDeviceSelected() {
  const sel = document.getElementById("deviceSelect");
  const deviceId = sel.value || "";
  const meta = document.getElementById("deviceMeta");
  if (!deviceId) { meta.textContent = ""; return; }

  document.getElementById("deviceId").value = deviceId;

  const d = (window._devicesById || {})[deviceId];
  if (!d) { meta.textContent = ""; return; }

  meta.textContent = "Selected: " + (d.name || deviceId) + " • Device ID: " + deviceId + " • Last seen: " + formatLastSeen(d.lastSeenAt);
}

function baseMessage() {
  return {
    priority: document.getElementById("priority").value,
    ttlSeconds: parseInt(document.getElementById("ttl").value, 10)
  };
}

function getDeviceIdOrWarn(outEl) {
  const deviceId = document.getElementById("deviceId").value.trim();
  if (!deviceId) { outEl.textContent = "Device required. Select one from the dropdown and click Refresh if needed."; return ""; }
  return deviceId;
}

async function uploadBlobAsAudioAndSend(deviceId, blob, filenameHint) {
  const out = document.getElementById("sendOut");

  const fd = new FormData();
  const file = new File([blob], filenameHint || "recording.webm", { type: blob.type || "audio/webm" });
  fd.append("file", file);

  const up = await fetch("/api/uploads/audio", {
    method: "POST",
    headers: { "Authorization": "Bearer " + getToken() },
    body: fd
  });
  if (!up.ok) { out.textContent = "Upload error: " + up.status; return null; }
  const uj = await up.json();

  const body = Object.assign(baseMessage(), { type: "audio", audioBlobKey: uj.audioBlobKey });
  const res = await fetch("/api/devices/" + encodeURIComponent(deviceId) + "/messages", {
    method: "POST",
    headers: { "Authorization": "Bearer " + getToken(), "Content-Type":"application/json" },
    body: JSON.stringify(body)
  });
  if (!res.ok) { out.textContent = "Send error: " + res.status; return null; }
  const j = await res.json();
  return { upload: uj, send: j };
}

async function sendAudioFile() {
  const out = document.getElementById("sendOut");
  const deviceId = getDeviceIdOrWarn(out);
  if (!deviceId) return;

  const fileInput = document.getElementById("audioFile");
  if (!fileInput.files.length) { out.textContent = "Audio file required."; return; }
  const f = fileInput.files[0];

  const result = await uploadBlobAsAudioAndSend(deviceId, f, f.name);
  if (!result) return;
  out.textContent = "Uploaded + Sent audio. messageId=" + result.send.messageId + " expiresAt=" + result.send.expiresAt;
}

/* -------- Browser recording -------- */
let mediaRecorder = null;
let recordedChunks = [];
let recordedBlob = null;

function setRecStatus(text) {
  document.getElementById("recStatus").textContent = text;
}

function pickMimeType() {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/ogg"
  ];
  for (const t of candidates) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) return t;
  }
  return "";
}

async function startRecording() {
  const out = document.getElementById("sendOut");
  out.textContent = "";

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
    out.textContent = "Recording not supported in this browser.";
    return;
  }

  recordedChunks = [];
  recordedBlob = null;

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mimeType = pickMimeType();
    mediaRecorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);

    mediaRecorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) recordedChunks.push(e.data);
    };

    mediaRecorder.onstop = () => {
      stream.getTracks().forEach(t => t.stop());

      recordedBlob = new Blob(recordedChunks, { type: mediaRecorder.mimeType || "audio/webm" });
      const url = URL.createObjectURL(recordedBlob);

      const audio = document.getElementById("recPreview");
      audio.src = url;
      audio.style.display = "block";

      document.getElementById("sendRecordedBtn").disabled = false;
      setRecStatus("recorded (" + Math.round(recordedBlob.size/1024) + " KB)");
    };

    mediaRecorder.start();
    document.getElementById("recStart").disabled = true;
    document.getElementById("recStop").disabled = false;
    document.getElementById("sendRecordedBtn").disabled = true;
    document.getElementById("recPreview").style.display = "none";
    setRecStatus("recording…");
  } catch (err) {
    out.textContent = "Mic permission/recording error: " + err;
  }
}

function stopRecording() {
  if (!mediaRecorder) return;
  if (mediaRecorder.state === "recording") mediaRecorder.stop();
  document.getElementById("recStart").disabled = false;
  document.getElementById("recStop").disabled = true;
  setRecStatus("processing…");
}

async function sendRecorded() {
  const out = document.getElementById("sendOut");
  const deviceId = getDeviceIdOrWarn(out);
  if (!deviceId) return;
  if (!recordedBlob) { out.textContent = "No recording available."; return; }

  document.getElementById("sendRecordedBtn").disabled = true;
  out.textContent = "Uploading…";

  const ext = (recordedBlob.type && recordedBlob.type.includes("ogg")) ? ".ogg" : ".webm";
  const result = await uploadBlobAsAudioAndSend(deviceId, recordedBlob, "recording" + ext);
  if (!result) {
    document.getElementById("sendRecordedBtn").disabled = false;
    return;
  }
  out.textContent = "Recorded + Sent audio. messageId=" + result.send.messageId + " expiresAt=" + result.send.expiresAt;
}
</script>
</body>
</html>
"""

@app.get("/ui", response_class=HTMLResponse)
async def ui():
    return HTMLResponse(_UI_HTML)

# -----------------------------
# Pairing endpoints
# -----------------------------
@app.post("/api/pairing/start", response_model=PairStartResponse)
async def pairing_start(_: None = Depends(require_admin)):
    # Generate unique code (best-effort uniqueness)
    for _ in range(5):
        code = new_pairing_code()
        existing = await db_fetchone("SELECT code FROM pairing_codes WHERE code = ? AND used_at IS NULL AND expires_at > ?", (code, dt_to_iso(utcnow())))
        if not existing:
            break
    created = utcnow()
    expires = created + timedelta(seconds=PAIRING_CODE_TTL_SECONDS)
    await db_exec(
        "INSERT OR REPLACE INTO pairing_codes(code, created_at, expires_at, used_at, claimed_device_id) VALUES(?,?,?,?,?)",
        (code, dt_to_iso(created), dt_to_iso(expires), None, None)
    )
    return PairStartResponse(code=code, expiresAt=dt_to_iso(expires))

@app.post("/api/pairing/complete", response_model=PairCompleteResponse)
async def pairing_complete(req: PairCompleteRequest):
    row = await db_fetchone("SELECT code, expires_at, used_at FROM pairing_codes WHERE code = ?", (req.code,))
    if not row:
        raise HTTPException(status_code=400, detail="Invalid pairing code")
    if row["used_at"]:
        raise HTTPException(status_code=400, detail="Pairing code already used")
    if iso_to_dt(row["expires_at"]) <= utcnow():
        raise HTTPException(status_code=400, detail="Pairing code expired")

    device_id = str(uuid.uuid4())
    device_token = new_token()
    now = utcnow()

    await db_exec(
        "INSERT INTO devices(device_id, name, device_token, paired_at, last_seen_at) VALUES(?,?,?,?,?)",
        (device_id, req.deviceName, device_token, dt_to_iso(now), None)
    )
    await db_exec(
        "UPDATE pairing_codes SET used_at = ?, claimed_device_id = ? WHERE code = ?",
        (dt_to_iso(now), device_id, req.code)
    )
    return PairCompleteResponse(deviceId=device_id, deviceToken=device_token)

# -----------------------------
# Device listing (admin helper)
# -----------------------------
@app.get("/api/devices", dependencies=[Depends(require_admin)])
async def list_devices():
    rows = await db_fetchall("SELECT device_id, name, paired_at, last_seen_at FROM devices ORDER BY paired_at DESC")
    return [
        {
            "deviceId": r["device_id"],
            "name": r["name"],
            "pairedAt": r["paired_at"],
            "lastSeenAt": r["last_seen_at"],
        }
        for r in rows
    ]

# -----------------------------
# Upload audio (admin)
# -----------------------------
@app.post("/api/uploads/audio", response_model=UploadAudioResponse)
async def upload_audio(file: UploadFile = File(...), _: None = Depends(require_admin)):
    # Accept uploads even if content_type is missing or generic (some browsers send octet-stream)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    # Pick extension for ffmpeg probing
    ext = Path(file.filename or "").suffix.lower()
    if not ext:
        # fall back to content-type mapping if you have safe_ext; otherwise default
        try:
            ext = safe_ext(getattr(file, "content_type", "") or "", file.filename or "") or ".bin"
        except Exception:
            ext = ".bin"

    wav_bytes = convert_to_wav_bytes(data, input_ext=ext)

    # Store as .wav (client expects wav)
    blob_key = "b_" + uuid.uuid4().hex
    path = Path(BLOB_DIR) / f"{blob_key}.wav"
    path.write_bytes(wav_bytes)

    await db_exec(
        "INSERT INTO audio_blobs(blob_key, content_type, size_bytes, file_path, created_at) VALUES(?,?,?,?,?)",
        (blob_key, "audio/wav", len(wav_bytes), str(path), dt_to_iso(utcnow()))
    )

    return UploadAudioResponse(audioBlobKey=blob_key, contentType="audio/wav", sizeBytes=len(wav_bytes))


@app.get("/api/devices/{device_id}/audio/{blob_key}")
async def get_audio(device_id: str, blob_key: str, req: Request):
    await require_device_or_admin(req, device_id=device_id)
    row = await db_fetchone("SELECT file_path, content_type FROM audio_blobs WHERE blob_key = ?", (blob_key,))
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    fp = row["file_path"]
    if not os.path.exists(fp):
        raise HTTPException(status_code=404, detail="Missing blob file")
    return FileResponse(fp, media_type=row["content_type"], filename=os.path.basename(fp))

# -----------------------------
# Messaging helpers
# -----------------------------
async def expire_queued_messages(device_id: Optional[str] = None) -> None:
    now_iso = dt_to_iso(utcnow())
    if device_id:
        await db_exec(
            "UPDATE messages SET state = 'expired' WHERE state = 'queued' AND device_id = ? AND expires_at <= ?",
            (device_id, now_iso)
        )
    else:
        await db_exec(
            "UPDATE messages SET state = 'expired' WHERE state = 'queued' AND expires_at <= ?",
            (now_iso,)
        )

async def fetch_next_message(device_id: str) -> Optional[sqlite3.Row]:
    await expire_queued_messages(device_id=device_id)
    return await db_fetchone(
        "SELECT * FROM messages WHERE device_id = ? AND state = 'queued' AND expires_at > ? ORDER BY created_at ASC LIMIT 1",
        (device_id, dt_to_iso(utcnow()))
    )

def build_audio_url(device_id: str, blob_key: str) -> str:
    # Keep it simple: authenticated URL. Client passes bearer token.
    return f"/api/devices/{device_id}/audio/{blob_key}"

# -----------------------------
# Enqueue message (admin)
# -----------------------------
@app.post("/api/devices/{device_id}/messages", response_model=EnqueueMessageResponse)
async def enqueue_message(device_id: str, req: EnqueueMessageRequest, _: None = Depends(require_admin)):
    # validate device exists
    dev = await db_fetchone("SELECT device_id FROM devices WHERE device_id = ?", (device_id,))
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")

    if req.type == "tts":
        if not req.text or not req.text.strip():
            raise HTTPException(status_code=400, detail="text is required for type=tts")
    elif req.type == "audio":
        if not req.audioBlobKey:
            raise HTTPException(status_code=400, detail="audioBlobKey is required for type=audio")
        blob = await db_fetchone("SELECT blob_key FROM audio_blobs WHERE blob_key = ?", (req.audioBlobKey,))
        if not blob:
            raise HTTPException(status_code=400, detail="audioBlobKey not found")

    created = utcnow()
    if req.expiresAt:
        expires = iso_to_dt(req.expiresAt)
    else:
        ttl = req.ttlSeconds if req.ttlSeconds is not None else DEFAULT_MESSAGE_TTL_SECONDS
        if ttl <= 0 or ttl > 24 * 3600:
            raise HTTPException(status_code=400, detail="ttlSeconds must be between 1 and 86400")
        expires = created + timedelta(seconds=ttl)

    if expires <= created:
        raise HTTPException(status_code=400, detail="expiresAt must be in the future")

    message_id = str(uuid.uuid4())
    await db_exec(
        """INSERT INTO messages(message_id, device_id, type, text, audio_blob_key, priority, created_at, expires_at, state, details)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            message_id, device_id, req.type,
            (req.text.strip() if req.text else None),
            req.audioBlobKey,
            req.priority,
            dt_to_iso(created),
            dt_to_iso(expires),
            "queued",
            None
        )
    )
    # Wake any long-poll for that device
    await notify_device(device_id)
    return EnqueueMessageResponse(messageId=message_id, expiresAt=dt_to_iso(expires))

# -----------------------------
# Long poll next message (device)
# -----------------------------
@app.get("/api/devices/{device_id}/messages/next", response_model=NextMessageResponse, responses={204: {"description": "No Content"}})
async def messages_next(device_id: str, request: Request, timeout: int = LONGPOLL_TIMEOUT_SECONDS, ctx: DeviceContext = Depends(require_device)):
    # timeout clamp
    if timeout < 1:
        timeout = 1
    if timeout > 120:
        timeout = 120

    # Fast path: check immediately
    row = await fetch_next_message(device_id)
    if row:
        # Mark delivered (optional)
        await db_exec("UPDATE messages SET state = 'delivered' WHERE message_id = ? AND state = 'queued'", (row["message_id"],))
        return NextMessageResponse(
            messageId=row["message_id"],
            type=row["type"],
            text=row["text"],
            audioBlobKey=row["audio_blob_key"],
            audioUrl=(build_audio_url(device_id, row["audio_blob_key"]) if row["type"] == "audio" and row["audio_blob_key"] else None),
            priority=row["priority"],
            createdAt=row["created_at"],
            expiresAt=row["expires_at"],
        )

    # Wait on condition
    cond = await _get_condition(device_id)
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return Response(status_code=204)

        # If client disconnected, stop early
        if await request.is_disconnected():
            return Response(status_code=204)

        async with cond:
            try:
                await asyncio.wait_for(cond.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return Response(status_code=204)

        # after notify, check again
        row = await fetch_next_message(device_id)
        if row:
            await db_exec("UPDATE messages SET state = 'delivered' WHERE message_id = ? AND state = 'queued'", (row["message_id"],))
            return NextMessageResponse(
                messageId=row["message_id"],
                type=row["type"],
                text=row["text"],
                audioBlobKey=row["audio_blob_key"],
                audioUrl=(build_audio_url(device_id, row["audio_blob_key"]) if row["type"] == "audio" and row["audio_blob_key"] else None),
                priority=row["priority"],
                createdAt=row["created_at"],
                expiresAt=row["expires_at"],
            )

# -----------------------------
# ACK (device)
# -----------------------------
@app.post("/api/messages/{message_id}/ack")
async def ack_message(message_id: str, req: AckRequest, request: Request):
    # We need to verify the device token belongs to the message's device.
    token = _bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized (device)")

    msg = await db_fetchone("SELECT message_id, device_id, state, expires_at FROM messages WHERE message_id = ?", (message_id,))
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    dev = await db_fetchone("SELECT device_token FROM devices WHERE device_id = ?", (msg["device_id"],))
    if not dev or dev["device_token"] != token:
        raise HTTPException(status_code=401, detail="Unauthorized (device)")

    now = utcnow()
    # If message already expired, force expired state unless it was played
    expired = iso_to_dt(msg["expires_at"]) <= now

    new_state = None
    if req.status == "played":
        new_state = "played"
    elif req.status == "failed":
        new_state = "failed"
    elif req.status == "expired":
        new_state = "expired"

    if expired and new_state != "played":
        new_state = "expired"

    await db_exec("UPDATE messages SET state = ?, details = ? WHERE message_id = ?", (new_state, req.details, message_id))
    return {"ok": True, "state": new_state}

# -----------------------------
# Health
# -----------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True, "time": dt_to_iso(utcnow())}
