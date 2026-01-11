
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
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Headphone Pager</title>
  <style>
    :root {
      --primary: #3b82f6;
      --primary-dark: #2563eb;
      --success: #10b981;
      --danger: #ef4444;
      --warning: #f59e0b;
      --gray-50: #f9fafb;
      --gray-100: #f3f4f6;
      --gray-200: #e5e7eb;
      --gray-300: #d1d5db;
      --gray-600: #4b5563;
      --gray-700: #374151;
      --gray-800: #1f2937;
      --gray-900: #111827;
      --border-radius: 12px;
      --shadow-sm: 0 1px 2px 0 rgb(0 0 0 / 0.05);
      --shadow: 0 1px 3px 0 rgb(0 0 0 / 0.1), 0 1px 2px -1px rgb(0 0 0 / 0.1);
      --shadow-lg: 0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 / 0.1);
    }
    
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }
    
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      min-height: 100vh;
      padding: 16px;
      color: var(--gray-900);
    }
    
    .container {
      max-width: 800px;
      margin: 0 auto;
    }
    
    .header {
      background: white;
      border-radius: var(--border-radius);
      padding: 24px;
      margin-bottom: 20px;
      box-shadow: var(--shadow-lg);
      text-align: center;
    }
    
    .header h1 {
      font-size: 28px;
      color: var(--gray-900);
      margin-bottom: 8px;
      font-weight: 700;
    }
    
    .header p {
      color: var(--gray-600);
      font-size: 14px;
    }
    
    .card {
      background: white;
      border-radius: var(--border-radius);
      padding: 24px;
      margin-bottom: 20px;
      box-shadow: var(--shadow-lg);
    }
    
    .card-header {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 20px;
      padding-bottom: 16px;
      border-bottom: 2px solid var(--gray-100);
    }
    
    .card-number {
      background: var(--primary);
      color: white;
      width: 32px;
      height: 32px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 16px;
      flex-shrink: 0;
    }
    
    .card-header h2 {
      font-size: 20px;
      color: var(--gray-800);
      font-weight: 600;
      flex: 1;
    }
    
    .form-group {
      margin-bottom: 16px;
    }
    
    .form-group:last-child {
      margin-bottom: 0;
    }
    
    label {
      display: block;
      font-size: 14px;
      font-weight: 500;
      color: var(--gray-700);
      margin-bottom: 6px;
    }
    
    input, select, textarea {
      width: 100%;
      padding: 12px 16px;
      font-size: 15px;
      border: 2px solid var(--gray-200);
      border-radius: 8px;
      background: white;
      color: var(--gray-900);
      transition: all 0.2s;
      font-family: inherit;
    }
    
    input:focus, select:focus, textarea:focus {
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
    }
    
    input[type="file"] {
      padding: 10px;
      font-size: 14px;
    }
    
    textarea {
      min-height: 100px;
      resize: vertical;
    }
    
    button {
      background: var(--primary);
      color: white;
      border: none;
      padding: 12px 24px;
      font-size: 15px;
      font-weight: 500;
      border-radius: 8px;
      cursor: pointer;
      transition: all 0.2s;
      font-family: inherit;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      white-space: nowrap;
    }
    
    button:hover:not(:disabled) {
      background: var(--primary-dark);
      transform: translateY(-1px);
      box-shadow: var(--shadow);
    }
    
    button:active:not(:disabled) {
      transform: translateY(0);
    }
    
    button:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    
    button.secondary {
      background: var(--gray-100);
      color: var(--gray-700);
    }
    
    button.secondary:hover:not(:disabled) {
      background: var(--gray-200);
    }
    
    button.success {
      background: var(--success);
    }
    
    button.success:hover:not(:disabled) {
      background: #059669;
    }
    
    button.danger {
      background: var(--danger);
    }
    
    button.danger:hover:not(:disabled) {
      background: #dc2626;
    }
    
    .button-group {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }
    
    .button-group button {
      flex: 1;
      min-width: 120px;
    }
    
    .row {
      display: grid;
      gap: 12px;
      grid-template-columns: 1fr;
    }
    
    @media (min-width: 640px) {
      .row.cols-2 {
        grid-template-columns: 1fr 1fr;
      }
      
      .row.cols-3 {
        grid-template-columns: 1fr 1fr 1fr;
      }
      
      .row.auto-fit {
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      }
    }
    
    .info-box {
      background: var(--gray-50);
      border: 1px solid var(--gray-200);
      border-radius: 8px;
      padding: 12px 16px;
      font-size: 14px;
      color: var(--gray-700);
      line-height: 1.6;
    }
    
    .info-box code {
      background: white;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 13px;
      color: var(--primary);
      font-family: 'Monaco', 'Courier New', monospace;
    }
    
    .status-message {
      padding: 12px 16px;
      border-radius: 8px;
      font-size: 14px;
      margin-top: 12px;
      display: none;
    }
    
    .status-message.show {
      display: block;
    }
    
    .status-message.success {
      background: #d1fae5;
      color: #065f46;
      border: 1px solid #6ee7b7;
    }
    
    .status-message.error {
      background: #fee2e2;
      color: #991b1b;
      border: 1px solid #fca5a5;
    }
    
    .status-message.info {
      background: #dbeafe;
      color: #1e40af;
      border: 1px solid #93c5fd;
    }
    
    .pill {
      display: inline-block;
      padding: 6px 14px;
      border-radius: 999px;
      background: var(--gray-100);
      color: var(--gray-700);
      font-size: 13px;
      font-weight: 500;
      border: 1px solid var(--gray-200);
    }
    
    .pill.recording {
      background: #fee2e2;
      color: #991b1b;
      border-color: #fca5a5;
      animation: pulse 2s ease-in-out infinite;
    }
    
    .pill.ready {
      background: #d1fae5;
      color: #065f46;
      border-color: #6ee7b7;
    }
    
    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.7; }
    }
    
    audio {
      width: 100%;
      margin-top: 12px;
      border-radius: 8px;
    }
    
    .device-info {
      background: var(--gray-50);
      border: 1px solid var(--gray-200);
      border-radius: 8px;
      padding: 12px 16px;
      font-size: 14px;
      color: var(--gray-700);
      margin-top: 12px;
    }
    
    .device-info strong {
      color: var(--gray-900);
      font-weight: 600;
    }
    
    .pairing-code {
      background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
      color: white;
      padding: 20px;
      border-radius: 8px;
      text-align: center;
      margin-top: 16px;
      box-shadow: var(--shadow);
    }
    
    .pairing-code-number {
      font-size: 36px;
      font-weight: 700;
      letter-spacing: 4px;
      margin: 8px 0;
      font-family: 'Monaco', 'Courier New', monospace;
    }
    
    .pairing-code-label {
      font-size: 12px;
      opacity: 0.9;
      text-transform: uppercase;
      letter-spacing: 1px;
    }
    
    .pairing-code-expires {
      font-size: 13px;
      opacity: 0.8;
      margin-top: 8px;
    }
    
    .section-divider {
      border: none;
      border-top: 2px solid var(--gray-100);
      margin: 24px 0;
    }
    
    .section-title {
      font-size: 16px;
      font-weight: 600;
      color: var(--gray-800);
      margin-bottom: 16px;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    
    .section-title::before {
      content: '';
      width: 4px;
      height: 20px;
      background: var(--primary);
      border-radius: 2px;
    }
    
    .hidden {
      display: none !important;
    }
    
    /* Loading spinner */
    .spinner {
      border: 3px solid var(--gray-200);
      border-top: 3px solid var(--primary);
      border-radius: 50%;
      width: 20px;
      height: 20px;
      animation: spin 1s linear infinite;
      display: inline-block;
    }
    
    @keyframes spin {
      0% { transform: rotate(0deg); }
      100% { transform: rotate(360deg); }
    }
    
    /* Mobile optimizations */
    @media (max-width: 639px) {
      body {
        padding: 12px;
      }
      
      .header {
        padding: 20px 16px;
      }
      
      .header h1 {
        font-size: 24px;
      }
      
      .card {
        padding: 20px 16px;
      }
      
      .card-header h2 {
        font-size: 18px;
      }
      
      .pairing-code-number {
        font-size: 28px;
        letter-spacing: 2px;
      }
      
      button {
        padding: 12px 16px;
        font-size: 14px;
      }
    }
    
    /* Accessibility improvements */
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
      }
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>🎧 Headphone Pager</h1>
      <p>Send audio messages to your devices instantly</p>
    </div>

    <!-- Admin Token Section -->
    <div class="card">
      <div class="card-header">
        <div class="card-number">1</div>
        <h2>Authentication</h2>
      </div>
      
      <div class="form-group">
        <label for="adminToken">Admin Token</label>
        <input id="adminToken" type="password" placeholder="Enter your admin token" autocomplete="off"/>
      </div>
      
      <button onclick="saveToken()" class="success">
        <span>💾</span> Save Token
      </button>
      
      <div class="info-box" style="margin-top: 16px;">
        Your token is stored locally in your browser using <code>localStorage</code>. It's never sent to any external servers.
      </div>
      
      <div id="tokenStatus" class="status-message"></div>
    </div>

    <!-- Device Pairing Section -->
    <div class="card">
      <div class="card-header">
        <div class="card-number">2</div>
        <h2>Pair a Device</h2>
      </div>
      
      <button onclick="pairStart()">
        <span>🔗</span> Generate Pairing Code
      </button>
      
      <div id="pairOut"></div>
      <div id="pairStatus" class="status-message"></div>
    </div>

    <!-- Messaging Section -->
    <div class="card">
      <div class="card-header">
        <div class="card-number">3</div>
        <h2>Send Message</h2>
      </div>
      
      <!-- Device Selection -->
      <div class="form-group">
        <label for="deviceSelect">Select Device</label>
        <select id="deviceSelect" onchange="onDeviceSelected()">
          <option value="">Choose a device...</option>
        </select>
      </div>
      
      <div class="button-group">
        <button onclick="loadDevices(true)" class="secondary">
          <span>🔄</span> Refresh Devices
        </button>
      </div>
      
      <div id="deviceMeta" class="device-info hidden"></div>
      <div id="devicesStatus" class="status-message"></div>
      
      <!-- Hidden field for device ID -->
      <input id="deviceId" type="hidden"/>
      
      <hr class="section-divider"/>
      
      <!-- Message Settings -->
      <div class="section-title">Message Settings</div>
      
      <div class="row cols-2">
        <div class="form-group">
          <label for="priority">Priority</label>
          <select id="priority">
            <option value="normal">🔔 Normal</option>
            <option value="urgent">🚨 Urgent</option>
          </select>
        </div>
        
        <div class="form-group">
          <label for="ttl">Expiration</label>
          <select id="ttl">
            <option value="60">1 minute</option>
            <option value="300">5 minutes</option>
            <option value="600" selected>10 minutes</option>
            <option value="1800">30 minutes</option>
            <option value="3600">1 hour</option>
          </select>
        </div>
      </div>
      
      <hr class="section-divider"/>
      
      <!-- Audio Upload -->
      <div class="section-title">📎 Upload Audio File</div>
      
      <div class="form-group">
        <input id="audioFile" type="file" accept="audio/*"/>
      </div>
      
      <button onclick="sendAudioFile()" class="success">
        <span>📤</span> Upload & Send
      </button>
      
      <div class="info-box" style="margin-top: 12px;">
        Upload any audio file. The server will automatically convert it to WAV format.
      </div>
      
      <hr class="section-divider"/>
      
      <!-- Voice Recording -->
      <div class="section-title">🎤 Record Voice Message</div>
      
      <div class="button-group">
        <button id="recStart" onclick="startRecording()" class="danger">
          <span>⏺️</span> Start Recording
        </button>
        <button id="recStop" onclick="stopRecording()" disabled class="secondary">
          <span>⏹️</span> Stop
        </button>
      </div>
      
      <div style="margin-top: 12px;">
        <span id="recStatus" class="pill">Ready to record</span>
      </div>
      
      <audio id="recPreview" controls class="hidden"></audio>
      
      <button id="sendRecordedBtn" onclick="sendRecorded()" disabled class="success" style="margin-top: 12px; width: 100%;">
        <span>📤</span> Send Recorded Message
      </button>
      
      <div id="sendStatus" class="status-message"></div>
    </div>
  </div>

  <script>
    // Token Management
    function getToken() {
      return localStorage.getItem("adminToken") || "";
    }
    
    function saveToken() {
      const t = document.getElementById("adminToken").value.trim();
      const status = document.getElementById("tokenStatus");
      
      if (!t) {
        showStatus(status, "Please enter a token", "error");
        return;
      }
      
      localStorage.setItem("adminToken", t);
      showStatus(status, "✓ Token saved successfully!", "success");
    }
    
    // Load saved token on page load
    document.getElementById("adminToken").value = getToken();

    // Pairing
    async function pairStart() {
      const pairOut = document.getElementById("pairOut");
      const status = document.getElementById("pairStatus");
      
      pairOut.innerHTML = "";
      status.className = "status-message";
      
      try {
        const res = await fetch("/api/pairing/start", {
          method: "POST",
          headers: { "Authorization": "Bearer " + getToken() }
        });
        
        if (!res.ok) {
          showStatus(status, "Error generating pairing code. Check your admin token.", "error");
          return;
        }
        
        const j = await res.json();
        
        pairOut.innerHTML = `
          <div class="pairing-code">
            <div class="pairing-code-label">Pairing Code</div>
            <div class="pairing-code-number">${j.code}</div>
            <div class="pairing-code-expires">Expires: ${new Date(j.expiresAt).toLocaleString()}</div>
          </div>
        `;
        
        showStatus(status, "✓ Pairing code generated! Enter this code on your device.", "success");
      } catch (err) {
        showStatus(status, "Network error: " + err.message, "error");
      }
    }

    // Device Management
    async function loadDevices(selectFirst = false) {
      const status = document.getElementById("devicesStatus");
      const meta = document.getElementById("deviceMeta");
      
      showStatus(status, "Loading devices...", "info");
      meta.classList.add("hidden");
      
      try {
        const res = await fetch("/api/devices", {
          headers: { "Authorization": "Bearer " + getToken() }
        });
        
        if (!res.ok) {
          showStatus(status, "Failed to load devices. Check your admin token.", "error");
          return;
        }
        
        const list = await res.json();
        const sel = document.getElementById("deviceSelect");
        
        sel.innerHTML = '<option value="">Choose a device...</option>';
        
        window._devicesById = {};
        for (const d of list) {
          window._devicesById[d.deviceId] = d;
          const opt = document.createElement("option");
          opt.value = d.deviceId;
          opt.textContent = d.name || d.deviceId;
          sel.appendChild(opt);
        }
        
        if (list.length === 0) {
          showStatus(status, "No devices paired yet. Generate a pairing code first.", "info");
          return;
        }
        
        showStatus(status, `✓ Loaded ${list.length} device(s)`, "success");
        
        if (selectFirst && !sel.value && list.length > 0) {
          sel.value = list[0].deviceId;
          onDeviceSelected();
        }
      } catch (err) {
        showStatus(status, "Network error: " + err.message, "error");
      }
    }

    function onDeviceSelected() {
      const sel = document.getElementById("deviceSelect");
      const deviceId = sel.value || "";
      const meta = document.getElementById("deviceMeta");
      
      if (!deviceId) {
        meta.classList.add("hidden");
        document.getElementById("deviceId").value = "";
        return;
      }
      
      document.getElementById("deviceId").value = deviceId;
      
      const d = (window._devicesById || {})[deviceId];
      if (!d) {
        meta.classList.add("hidden");
        return;
      }
      
      meta.innerHTML = `
        <strong>Device:</strong> ${d.name || deviceId}<br>
        <strong>ID:</strong> ${deviceId}<br>
        <strong>Last seen:</strong> ${formatLastSeen(d.lastSeenAt)}
      `;
      meta.classList.remove("hidden");
    }

    function formatLastSeen(lastSeenAt) {
      if (!lastSeenAt) return "never";
      try {
        const date = new Date(lastSeenAt);
        const now = new Date();
        const diffMs = now - date;
        const diffMins = Math.floor(diffMs / 60000);
        
        if (diffMins < 1) return "just now";
        if (diffMins < 60) return `${diffMins} minute${diffMins !== 1 ? 's' : ''} ago`;
        
        const diffHours = Math.floor(diffMins / 60);
        if (diffHours < 24) return `${diffHours} hour${diffHours !== 1 ? 's' : ''} ago`;
        
        return date.toLocaleString();
      } catch {
        return lastSeenAt;
      }
    }

    // Message Helpers
    function baseMessage() {
      return {
        priority: document.getElementById("priority").value,
        ttlSeconds: parseInt(document.getElementById("ttl").value, 10)
      };
    }

    function getDeviceIdOrWarn(statusEl) {
      const deviceId = document.getElementById("deviceId").value.trim();
      if (!deviceId) {
        showStatus(statusEl, "Please select a device first", "error");
        return "";
      }
      return deviceId;
    }

    // Audio Upload
    async function uploadBlobAsAudioAndSend(deviceId, blob, filenameHint) {
      const status = document.getElementById("sendStatus");
      
      const fd = new FormData();
      const file = new File([blob], filenameHint || "recording.webm", { 
        type: blob.type || "audio/webm" 
      });
      fd.append("file", file);
      
      showStatus(status, "Uploading audio...", "info");
      
      const up = await fetch("/api/uploads/audio", {
        method: "POST",
        headers: { "Authorization": "Bearer " + getToken() },
        body: fd
      });
      
      if (!up.ok) {
        showStatus(status, "Upload failed: " + up.status, "error");
        return null;
      }
      
      const uj = await up.json();
      
      showStatus(status, "Sending message...", "info");
      
      const body = Object.assign(baseMessage(), { 
        type: "audio", 
        audioBlobKey: uj.audioBlobKey 
      });
      
      const res = await fetch("/api/devices/" + encodeURIComponent(deviceId) + "/messages", {
        method: "POST",
        headers: { 
          "Authorization": "Bearer " + getToken(), 
          "Content-Type": "application/json" 
        },
        body: JSON.stringify(body)
      });
      
      if (!res.ok) {
        showStatus(status, "Send failed: " + res.status, "error");
        return null;
      }
      
      const j = await res.json();
      return { upload: uj, send: j };
    }

    async function sendAudioFile() {
      const status = document.getElementById("sendStatus");
      const deviceId = getDeviceIdOrWarn(status);
      if (!deviceId) return;
      
      const fileInput = document.getElementById("audioFile");
      if (!fileInput.files.length) {
        showStatus(status, "Please select an audio file", "error");
        return;
      }
      
      const f = fileInput.files[0];
      
      try {
        const result = await uploadBlobAsAudioAndSend(deviceId, f, f.name);
        if (!result) return;
        
        showStatus(status, `✓ Audio message sent successfully! (ID: ${result.send.messageId.substring(0, 8)}...)`, "success");
        fileInput.value = "";
      } catch (err) {
        showStatus(status, "Error: " + err.message, "error");
      }
    }

    // Recording
    let mediaRecorder = null;
    let recordedChunks = [];
    let recordedBlob = null;

    function setRecStatus(text, type = "default") {
      const el = document.getElementById("recStatus");
      el.textContent = text;
      el.className = "pill";
      if (type === "recording") el.classList.add("recording");
      if (type === "ready") el.classList.add("ready");
    }

    function pickMimeType() {
      const candidates = [
        "audio/webm;codecs=opus",
        "audio/webm",
        "audio/ogg;codecs=opus",
        "audio/ogg"
      ];
      for (const t of candidates) {
        if (window.MediaRecorder && MediaRecorder.isTypeSupported && 
            MediaRecorder.isTypeSupported(t)) return t;
      }
      return "";
    }

    async function startRecording() {
      const status = document.getElementById("sendStatus");
      status.className = "status-message";
      
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
        showStatus(status, "Recording not supported in this browser", "error");
        return;
      }
      
      recordedChunks = [];
      recordedBlob = null;
      
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const mimeType = pickMimeType();
        mediaRecorder = mimeType ? 
          new MediaRecorder(stream, { mimeType }) : 
          new MediaRecorder(stream);
        
        mediaRecorder.ondataavailable = (e) => {
          if (e.data && e.data.size > 0) recordedChunks.push(e.data);
        };
        
        mediaRecorder.onstop = () => {
          stream.getTracks().forEach(t => t.stop());
          
          recordedBlob = new Blob(recordedChunks, { 
            type: mediaRecorder.mimeType || "audio/webm" 
          });
          const url = URL.createObjectURL(recordedBlob);
          
          const audio = document.getElementById("recPreview");
          audio.src = url;
          audio.classList.remove("hidden");
          
          document.getElementById("sendRecordedBtn").disabled = false;
          setRecStatus(`Ready (${Math.round(recordedBlob.size / 1024)} KB)`, "ready");
        };
        
        mediaRecorder.start();
        document.getElementById("recStart").disabled = true;
        document.getElementById("recStop").disabled = false;
        document.getElementById("sendRecordedBtn").disabled = true;
        document.getElementById("recPreview").classList.add("hidden");
        setRecStatus("Recording...", "recording");
      } catch (err) {
        showStatus(status, "Microphone access denied or error: " + err.message, "error");
      }
    }

    function stopRecording() {
      if (!mediaRecorder) return;
      if (mediaRecorder.state === "recording") mediaRecorder.stop();
      document.getElementById("recStart").disabled = false;
      document.getElementById("recStop").disabled = true;
      setRecStatus("Processing...");
    }

    async function sendRecorded() {
      const status = document.getElementById("sendStatus");
      const deviceId = getDeviceIdOrWarn(status);
      if (!deviceId) return;
      
      if (!recordedBlob) {
        showStatus(status, "No recording available", "error");
        return;
      }
      
      document.getElementById("sendRecordedBtn").disabled = true;
      
      try {
        const ext = (recordedBlob.type && recordedBlob.type.includes("ogg")) ? ".ogg" : ".webm";
        const result = await uploadBlobAsAudioAndSend(deviceId, recordedBlob, "recording" + ext);
        
        if (!result) {
          document.getElementById("sendRecordedBtn").disabled = false;
          return;
        }
        
        showStatus(status, `✓ Voice message sent successfully! (ID: ${result.send.messageId.substring(0, 8)}...)`, "success");
        
        // Reset recording UI
        recordedBlob = null;
        document.getElementById("recPreview").classList.add("hidden");
        setRecStatus("Ready to record");
      } catch (err) {
        showStatus(status, "Error: " + err.message, "error");
        document.getElementById("sendRecordedBtn").disabled = false;
      }
    }

    // Utility Functions
    function showStatus(element, message, type = "info") {
      element.textContent = message;
      element.className = "status-message show " + type;
    }

    // Auto-load devices on page load if token is available
    window.addEventListener("DOMContentLoaded", () => {
      if (getToken()) {
        loadDevices();
      }
    });
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
