# Headphone Pager – Windows Client 🎧🖥️

This folder contains the **Windows client (agent)** for **Headphone Pager**.

The agent runs quietly in the background on a Windows PC and plays voice messages through the user’s headphones when they are sent from the backend UI.

---

## Key Features

### ✅ Background operation
- Runs as a **Windows GUI app** (`WinExe`)
- **No console window**
- Designed to run only when a user is logged in

### ✅ System tray integration
- Shows a **tray icon** while running
- Tooltip: **“Headphone Pager”**
- Right‑click menu:
  - **Quit** – cleanly stops the agent

### ✅ Single‑instance locking
- Only **one instance per user session** is allowed
- Launching the agent again while it’s already running will exit silently
- Prevents duplicate tray icons or double audio playback

### ✅ Reliable audio playback
- Polls the backend using **long polling** (HTTP)
- Downloads audio as **WAV (PCM)** for maximum compatibility
- Plays messages over the active Windows audio device (headphones, speakers, etc.)

---

## Release

File **HeadphoneAgent-v0.1.3.zip** contains compiled release for Windows x64

## Build Requirements

- Windows 10 / 11
- .NET SDK **8.0+**
- Backend server running and reachable

---

## Building the Client

From this folder:

```powershell
dotnet publish -c Release -r win-x64 --self-contained false
```

The output binary will be located at:

```
bin\Release\net8.0-windows\win-x64\publish\HeadphoneAgent.exe
```

---

## Pairing a Device (First Run)

Before the agent can receive messages, it must be paired with the backend.

1. Open the backend UI
2. Generate a **pairing code**
3. Run:

```powershell
HeadphoneAgent.exe --pair 123456 --server http://home.lan:8585 --name "Gaming-PC"
```

On success:
- A configuration file is written to the user profile
- A confirmation dialog is shown

---

## Normal Usage

After pairing, simply run:

```powershell
HeadphoneAgent.exe
```

What happens:
- The agent starts silently
- A tray icon appears
- The agent waits for incoming messages

To stop the agent:
- Right‑click the tray icon
- Select **Quit**

---

## Tray Icon Notes

- Windows may hide new tray icons by default
- Click the **^** arrow near the clock to find it
- You can drag it out to make it always visible

The tray icon file is:
```
tray.ico
```
It is copied automatically to the publish output.

---

## Configuration Location

The agent stores its configuration per user, typically under:

```
%APPDATA%\HeadphonePager\config.json
```

This includes:
- Server URL
- Device ID
- Device authentication token

---

## Troubleshooting

### No tray icon appears
- Check the hidden tray overflow (`^`)
- Ensure only one instance is running (Task Manager)
- Make sure you are running the **published EXE**, not `dotnet run`

### No audio playback
- Verify the backend is reachable
- Confirm the device is listed as “last seen” in the UI
- Test with a known WAV file from the backend UI

---

## Security Model

- Each agent has a **device‑specific token**
- The agent can only:
  - Poll for its own messages
  - Download its own audio
- No admin privileges are required

---

## Intended Use

This client is designed for **home / family environments**, where trust exists between the backend and client machines.

It is intentionally lightweight and avoids:
- Windows services
- Kernel hooks
- Global audio interception

---

Happy paging 😊
