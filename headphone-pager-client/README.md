# HeadphoneAgent (Windows client)

A lightweight .NET agent that long-polls the Headphone Pager backend and plays **audio messages** through the **Communications** audio role (to trigger Windows communications ducking).

## Pair a device
Get a pairing code from `http://<server>:8080/ui`, then on the kid PC:

```powershell
dotnet run -- --pair 123456 --server http://home.lan:8080 --name "Kid-PC"
```

This writes config to:
`%AppData%\HeadphoneAgent\config.json`

## Run
```powershell
dotnet run
```

## Publish a single-file EXE
```powershell
dotnet publish -c Release
```

Output:
`bin\Release\net8.0-windows\win-x64\publish\HeadphoneAgent.exe`

## Auto-start at logon (Task Scheduler)
Create a task:
- Trigger: **At log on**
- Action: Start program → `HeadphoneAgent.exe`
- Set: “Run only when user is logged on”

## Audio format note
The backend converts uploads/recordings to **WAV**, so the client can play them directly without ffmpeg.


### Publishing note
This client uses System.Text.Json reflection serialization. Trimming is disabled (`PublishTrimmed=false`) to avoid runtime errors.
