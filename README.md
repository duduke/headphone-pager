# Headphone Pager 🎧📣

**Headphone Pager** is a lightweight home system that lets a parent send short voice messages directly into their kids’ headphones — even while they’re gaming.

It solves a very real problem:  
> *“My kids are wearing headphones and can’t hear me calling them.”*

Instead of yelling or barging into rooms, you record a message in a browser UI and it plays immediately on their Windows PCs.

---

## Repository Structure

This repository contains **two separate components**, kept intentionally independent:

```
headphone-pager/
├── headphone-pager-backend/
│   ├── app/
│   │   ├── server.py
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── static/        # Web UI (record, send, device selection)
│   ├── docker-compose.yml
│   ├── docker-compose.caddy.yml
│   ├── Caddyfile
│   └── README.md
│
├── headphone-pager-client/
│   ├── HeadphoneAgent/    # .NET Windows client
│   ├── HeadphoneAgent.sln
│   └── README.md
│
└── README.md              # (this file)
```

- **Backend**: FastAPI service + web UI + audio processing  
- **Client**: Lightweight Windows agent that receives and plays messages

They can be developed, built, and deployed independently.

---

## Features

### ✅ What works today
- **Browser-based recording UI**
  - Record voice messages directly from the browser
  - Secure context via HTTPS (required for microphone access)
- **Windows background client**
  - Extremely lightweight .NET agent
  - Long-polling (no WebSockets, no heavy frameworks)
  - Plays messages over whatever audio device the kid is using
- **Reliable audio delivery**
  - All audio is converted server-side to **WAV (PCM)** for maximum compatibility
  - Works for both uploaded WAV files and browser-recorded audio
- **Device management**
  - Pair devices using short pairing codes
  - Device list with names + “last seen”
  - Dropdown selection in the UI
- **Message behavior**
  - Messages auto-expire (default: 10 minutes)
  - “Urgent” messages can override normal audio
- **Portable backend**
  - Runs fully in Docker
  - Works in a home lab or cloud
  - Optional Caddy integration for HTTPS

---

## Architecture Overview

```
Browser (UI + Mic)
        |
        | HTTPS
        v
+---------------------------+
| Headphone Pager Backend   |
| (FastAPI + UI)            |
|                           |
| - Device registry         |
| - Message queue           |
| - Audio storage           |
| - WAV conversion (ffmpeg) |
+---------------------------+
        |
        | HTTP (long-poll)
        v
+---------------------------+
| Windows Agent             |
| (HeadphoneAgent)          |
|                           |
| - Polls messages          |
| - Downloads WAV           |
| - Plays audio             |
+---------------------------+
```

---

## Backend: Running the Server

All backend instructions live in:

```
headphone-pager-backend/README.md
```

In short:
- The backend runs in Docker
- The UI is served from the backend
- HTTPS is provided via **Caddy** (recommended for browser recording)
- The backend API is also exposed over plain HTTP for Windows clients

Typical endpoints:
- UI: `https://home.lan/ui`
- API: `http://home.lan:8585`

---

## Client: Windows Agent

All client instructions live in:

```
headphone-pager-client/README.md
```

Highlights:
- Runs only when a user is logged in
- Pairs once using a short pairing code
- Polls the backend using long polling
- Plays received messages immediately

Example pairing command:
```powershell
HeadphoneAgent.exe --pair 123456 --server http://home.lan:8585 --name "Gaming-PC"
```

---

## Audio Handling

Browsers typically record audio as `webm/opus` or `ogg`.

To keep the Windows client extremely simple and reliable, the backend:
- Converts **all audio uploads** to **WAV**
- Uses `ffmpeg` for format normalization
- Always serves `audio/wav` to clients

This guarantees consistent playback behavior.

---

## Security Model

- **Admin token** required to:
  - Upload audio
  - Send messages
- **Device token** required to:
  - Poll messages
  - Download audio
- Devices can only access their own messages

This project is designed for a **trusted home network**, not hostile environments.

---

## License
This project is licensed under the **PolyForm Noncommercial License 1.0.0**. 

Individual and personal use is encouraged, but commercial use is strictly prohibited. 
For the full terms, please see the [LICENSE](LICENSE) file.

[License: PolyForm Noncommercial](https://img.shields.io/badge/License-PolyForm_Noncommercial_1.0.0-lightgrey.svg)
