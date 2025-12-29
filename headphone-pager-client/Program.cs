using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using NAudio.CoreAudioApi;
using NAudio.Wave;

namespace HeadphoneAgent;

public sealed record AgentConfig(
    string ServerBaseUrl,
    string DeviceId,
    string DeviceToken,
    double UrgentVolumeCap // 0.0 - 1.0
);

public sealed record PairCompleteResponse(string deviceId, string deviceToken);

public sealed record NextMessageResponse(
    string messageId,
    string type,
    string? audioUrl,
    string priority,
    string createdAt,
    string expiresAt
);

public sealed record AckRequest(string status, string? details);

public static class Program
{
    private static readonly JsonSerializerOptions JsonOpts = new(JsonSerializerDefaults.Web);

    public static async Task<int> Main(string[] args)
    {
        var cli = CliArgs.Parse(args);

        if (cli.ShowHelp)
        {
            PrintHelp();
            return 0;
        }

        var configPath = ConfigPaths.ConfigFilePath();
        Directory.CreateDirectory(Path.GetDirectoryName(configPath)!);

        if (!string.IsNullOrWhiteSpace(cli.PairCode))
        {
            if (string.IsNullOrWhiteSpace(cli.ServerBaseUrl) || string.IsNullOrWhiteSpace(cli.DeviceName))
            {
                Console.Error.WriteLine("Pairing requires --server and --name.");
                return 2;
            }

            var paired = await PairDevice(cli.ServerBaseUrl!, cli.PairCode!, cli.DeviceName!);
            var cfg = new AgentConfig(
                ServerBaseUrl: NormalizeBase(cli.ServerBaseUrl!),
                DeviceId: paired.deviceId,
                DeviceToken: paired.deviceToken,
                UrgentVolumeCap: 0.85
            );

            await File.WriteAllTextAsync(configPath, JsonSerializer.Serialize(cfg, JsonOpts));
            Console.WriteLine("Paired OK.");
            Console.WriteLine($"DeviceId: {cfg.DeviceId}");
            Console.WriteLine($"Config written to: {configPath}");
            return 0;
        }

        if (!File.Exists(configPath))
        {
            Console.Error.WriteLine($"Config not found at: {configPath}");
            Console.Error.WriteLine("Run pairing first:");
            Console.Error.WriteLine("  HeadphoneAgent.exe --pair 123456 --server http://home.lan:8585 --name \"Kid-PC\"");
            return 2;
        }

        var cfgText = await File.ReadAllTextAsync(configPath);
        var config = JsonSerializer.Deserialize<AgentConfig>(cfgText, JsonOpts)
                     ?? throw new Exception("Failed to parse config.json");

        if (!string.IsNullOrWhiteSpace(cli.ServerBaseUrl))
            config = config with { ServerBaseUrl = NormalizeBase(cli.ServerBaseUrl!) };

        Console.WriteLine($"HeadphoneAgent running for device {config.DeviceId} against {config.ServerBaseUrl}");
        Console.WriteLine("Press Ctrl+C to stop.");

        using var cts = new CancellationTokenSource();
        Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };

        await RunLoop(config, cts.Token);
        return 0;
    }

    private static void PrintHelp()
    {
        Console.WriteLine("""
HeadphoneAgent (Windows)
Long-polls the Headphone Pager backend and plays AUDIO messages through the Communications audio role
(to trigger Windows communications ducking).

Usage:
  Pair:
    HeadphoneAgent.exe --pair 123456 --server http://home.lan:8585 --name "Noam-PC"

  Run (uses saved config):
    HeadphoneAgent.exe

Options:
  --server <url>    Override server base URL at runtime
  --pair <code>     Pair using a pairing code from /ui
  --name <name>     Device name to register during pairing
  --help            Show help

Notes:
  - Backend converts uploads/recordings to WAV, so the client can play them directly.
""");
    }

    private static string NormalizeBase(string baseUrl) => baseUrl.Trim().TrimEnd('/');

    private static async Task<PairCompleteResponse> PairDevice(string serverBaseUrl, string code, string deviceName)
    {
        var baseUrl = NormalizeBase(serverBaseUrl);
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(20) };

        var payload = JsonSerializer.Serialize(new { code, deviceName }, JsonOpts);
        using var content = new StringContent(payload, Encoding.UTF8, "application/json");

        var resp = await http.PostAsync($"{baseUrl}/api/pairing/complete", content);
        var body = await resp.Content.ReadAsStringAsync();

        if (!resp.IsSuccessStatusCode)
            throw new Exception($"Pairing failed: {(int)resp.StatusCode} {body}");

        return JsonSerializer.Deserialize<PairCompleteResponse>(body, JsonOpts)
               ?? throw new Exception("Pairing response parse error");
    }

    private static async Task RunLoop(AgentConfig config, CancellationToken ct)
    {
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(130) };
        http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", config.DeviceToken);

        var backoff = new Backoff(minSeconds: 1, maxSeconds: 15);

        while (!ct.IsCancellationRequested)
        {
            try
            {
                var url = $"{config.ServerBaseUrl}/api/devices/{config.DeviceId}/messages/next?timeout=45";
                using var resp = await http.GetAsync(url, ct);

                if (resp.StatusCode == System.Net.HttpStatusCode.NoContent)
                {
                    backoff.Reset();
                    continue;
                }

                var json = await resp.Content.ReadAsStringAsync(ct);

                if (!resp.IsSuccessStatusCode)
                {
                    Console.Error.WriteLine($"Poll error {(int)resp.StatusCode}: {json}");
                    await Task.Delay(TimeSpan.FromSeconds(backoff.NextDelaySeconds()), ct);
                    continue;
                }

                var msg = JsonSerializer.Deserialize<NextMessageResponse>(json, JsonOpts);
                if (msg is null)
                {
                    Console.Error.WriteLine("Poll returned invalid JSON.");
                    await Task.Delay(TimeSpan.FromSeconds(backoff.NextDelaySeconds()), ct);
                    continue;
                }

                backoff.Reset();

                if (!msg.type.Equals("audio", StringComparison.OrdinalIgnoreCase))
                {
                    await Ack(http, config, msg.messageId, "failed", "Only audio messages are supported by this client.", ct);
                    continue;
                }

                if (string.IsNullOrWhiteSpace(msg.audioUrl))
                {
                    await Ack(http, config, msg.messageId, "failed", "audioUrl missing", ct);
                    continue;
                }

                var urgent = msg.priority.Equals("urgent", StringComparison.OrdinalIgnoreCase);

                var full = msg.audioUrl.StartsWith("http", StringComparison.OrdinalIgnoreCase)
                    ? msg.audioUrl
                    : $"{config.ServerBaseUrl}{msg.audioUrl}";

                Console.WriteLine($"Message {msg.messageId} priority={msg.priority} url={full}");

                bool playedOk;
                string? details = null;

                try
                {
                    playedOk = await PlayAudioFromUrl(http, full, urgent, config, ct);
                }
                catch (Exception ex)
                {
                    playedOk = false;
                    details = ex.Message;
                }

                await Ack(http, config, msg.messageId, playedOk ? "played" : "failed", details, ct);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"Loop error: {ex.Message}");
                try { await Task.Delay(TimeSpan.FromSeconds(backoff.NextDelaySeconds()), ct); } catch { }
            }
        }
    }

    private static async Task Ack(HttpClient http, AgentConfig config, string messageId, string status, string? details, CancellationToken ct)
    {
        var payload = JsonSerializer.Serialize(new AckRequest(status, details), JsonOpts);
        using var content = new StringContent(payload, Encoding.UTF8, "application/json");

        using var req = new HttpRequestMessage(HttpMethod.Post, $"{config.ServerBaseUrl}/api/messages/{messageId}/ack")
        {
            Content = content
        };

        using var resp = await http.SendAsync(req, ct);
        var body = await resp.Content.ReadAsStringAsync(ct);
        if (!resp.IsSuccessStatusCode)
            Console.Error.WriteLine($"ACK error {(int)resp.StatusCode}: {body}");
    }

    private static async Task<bool> PlayAudioFromUrl(HttpClient http, string url, bool urgent, AgentConfig config, CancellationToken ct)
    {
        var tmpDir = Path.Combine(Path.GetTempPath(), "HeadphoneAgent");
        Directory.CreateDirectory(tmpDir);

        var filePath = Path.Combine(tmpDir, $"msg_{Guid.NewGuid():N}.wav");

        var bytes = await http.GetByteArrayAsync(url, ct);
        await File.WriteAllBytesAsync(filePath, bytes, ct);

        try
        {
            PlayViaCommunicationsRole(filePath, urgent, config);
            return true;
        }
        finally
        {
            try { if (File.Exists(filePath)) File.Delete(filePath); } catch { }
        }
    }

    private static void PlayViaCommunicationsRole(string filePath, bool urgent, AgentConfig config)
    {
        using var enumerator = new MMDeviceEnumerator();
        var device = enumerator.GetDefaultAudioEndpoint(DataFlow.Render, Role.Communications);

        float? originalVol = null;
        if (urgent)
        {
            try
            {
                originalVol = device.AudioEndpointVolume.MasterVolumeLevelScalar;
                var cap = (float)Math.Clamp(config.UrgentVolumeCap, 0.0, 1.0);
                if (device.AudioEndpointVolume.MasterVolumeLevelScalar < cap)
                    device.AudioEndpointVolume.MasterVolumeLevelScalar = cap;
            }
            catch { }
        }

        try
        {
            using var reader = new AudioFileReader(filePath);
            using var output = new WasapiOut(device, AudioClientShareMode.Shared, true, 50);
            output.Init(reader);
            output.Play();
            while (output.PlaybackState == PlaybackState.Playing)
                Thread.Sleep(50);
        }
        finally
        {
            if (urgent && originalVol is not null)
            {
                try { device.AudioEndpointVolume.MasterVolumeLevelScalar = originalVol.Value; } catch { }
            }
        }
    }
}

public sealed class Backoff
{
    private readonly int _min;
    private readonly int _max;
    private int _cur;

    public Backoff(int minSeconds, int maxSeconds)
    {
        _min = minSeconds;
        _max = maxSeconds;
        _cur = _min;
    }

    public void Reset() => _cur = _min;

    public int NextDelaySeconds()
    {
        var jitterMs = Random.Shared.Next(0, 500);
        var delay = _cur;
        _cur = Math.Min(_max, _cur * 2);
        return delay + (jitterMs / 1000);
    }
}

public sealed class CliArgs
{
    public bool ShowHelp { get; init; }
    public string? ServerBaseUrl { get; init; }
    public string? PairCode { get; init; }
    public string? DeviceName { get; init; }

    public static CliArgs Parse(string[] args)
    {
        var dict = new Dictionary<string, string?>(StringComparer.OrdinalIgnoreCase);
        for (int i = 0; i < args.Length; i++)
        {
            var a = args[i];
            if (a is "--help" or "-h" or "/?")
                return new CliArgs { ShowHelp = true };

            if (a.StartsWith("--"))
            {
                var key = a;
                string? val = null;
                if (i + 1 < args.Length && !args[i + 1].StartsWith("--"))
                {
                    val = args[i + 1];
                    i++;
                }
                dict[key] = val;
            }
        }

        dict.TryGetValue("--server", out var server);
        dict.TryGetValue("--pair", out var pair);
        dict.TryGetValue("--name", out var name);

        return new CliArgs
        {
            ShowHelp = false,
            ServerBaseUrl = server,
            PairCode = pair,
            DeviceName = name
        };
    }
}

public static class ConfigPaths
{
    public static string ConfigFilePath()
    {
        var appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        return Path.Combine(appData, "HeadphoneAgent", "config.json");
    }
}
