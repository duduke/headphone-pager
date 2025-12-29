# Headphone Pager 🎧📣

**Headphone Pager** is a lightweight home system that lets a parent send short voice messages directly into their kids’ headphones — even while they’re gaming.

It solves a very real problem:  
> *“My kids are wearing headphones and can’t hear me calling them.”*

Instead of yelling or barging into rooms, you record a message in a browser UI and it plays immediately on their Windows PCs.

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
+-------------------+
| FastAPI Backend   |
|                   |
| - Device registry |
| - Message queue   |
| - Audio storage   |
| - WAV conversion  |
+-------------------+
        |
        | HTTP (long-poll)
        v
+-------------------+
| Windows Agent     |
| (HeadphoneAgent)  |
|                   |
| - Polls messages  |
| - Downloads WAV   |
| - Plays audio     |
+-------------------+
```

---

## Repository Layout

```
headphone-pager/
├── app/
│   ├── server.py
│   ├── Dockerfile
│   ├── requirements.txt
│   └── static/
├── docker-compose.yml
├── docker-compose.caddy.yml
├── Caddyfile
├── README.md
```

---

## Running the Backend (with HTTPS UI)

### Requirements
- Docker + Docker Compose
- A local hostname (e.g. `home.lan`) resolving to the machine

### 1. Create `.env`
```env
ADMIN_TOKEN=replace_with_a_long_random_value
```

### 2. Start with Caddy (recommended)
```bash
docker compose -f docker-compose.caddy.yml up -d --build
```

### Endpoints
- **UI (HTTPS, mic works):**
  ```
  https://home.lan/ui
  ```
- **Backend API (HTTP):**
  ```
  http://home.lan:8585
  ```

---

## Windows Client (HeadphoneAgent)

### Pairing a device
```powershell
HeadphoneAgent.exe --pair 123456 --server http://home.lan:8585 --name "Gaming-PC"
```

### Normal run
```powershell
HeadphoneAgent.exe --server http://home.lan:8585
```

---

## Audio Handling

Browsers usually record audio as `webm/opus` or `ogg`.  
For maximum compatibility, the backend converts **all audio uploads to WAV** using `ffmpeg`.

This guarantees that the Windows client can always play received messages reliably.

---

## Security Model

- **Admin token** required to send messages
- **Device token** required to receive messages
- Devices can only access their own messages

Designed for a **trusted home network**, not hostile environments.

---

## License

This project is intended for **personal / home use**.
