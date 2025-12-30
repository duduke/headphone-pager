using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using NAudio.CoreAudioApi;
using NAudio.Wave;
using System.Drawing;
using System.Threading;
using System.Windows.Forms;

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

    
[STAThread]
public static int Main(string[] args)
{
    var cli = CliArgs.Parse(args);

    if (cli.ShowHelp)
    {
        MessageBox.Show(GetHelpText(), "Headphone Pager Agent", MessageBoxButtons.OK, MessageBoxIcon.Information);
        return 0;
    }

    var configPath = ConfigPaths.ConfigFilePath();
    Directory.CreateDirectory(Path.GetDirectoryName(configPath)!);

    // Pairing mode
    if (!string.IsNullOrWhiteSpace(cli.PairCode))
    {
        if (string.IsNullOrWhiteSpace(cli.ServerBaseUrl) || string.IsNullOrWhiteSpace(cli.DeviceName))
        {
            MessageBox.Show("Pairing requires --server and --name.", "Pairing error", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }

        try
        {
            var paired = PairDevice(cli.ServerBaseUrl!, cli.PairCode!, cli.DeviceName!).GetAwaiter().GetResult();
            var cfg = new AgentConfig(
                ServerBaseUrl: NormalizeBase(cli.ServerBaseUrl!),
                DeviceId: paired.deviceId,
                DeviceToken: paired.deviceToken,
                UrgentVolumeCap: 0.85
            );

            File.WriteAllText(configPath, JsonSerializer.Serialize(cfg, JsonOpts));
            MessageBox.Show($"Paired OK.\n\nDeviceId: {cfg.DeviceId}\nConfig written to:\n{configPath}",
                "Headphone Pager Agent", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return 0;
        }
        catch (Exception ex)
        {
            MessageBox.Show($"Pairing failed: {ex.Message}", "Pairing error", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    if (!File.Exists(configPath))
    {
        MessageBox.Show(
            "Config not found.\n\nRun pairing first:\nHeadphoneAgent.exe --pair 123456 --server http://home.lan:8585 --name \"Kid-PC\"",
            "Headphone Pager Agent",
            MessageBoxButtons.OK,
            MessageBoxIcon.Warning
        );
        return 2;
    }

    var cfgText = File.ReadAllText(configPath);
    var config = JsonSerializer.Deserialize<AgentConfig>(cfgText, JsonOpts)
                 ?? throw new Exception("Failed to parse config.json");

    if (!string.IsNullOrWhiteSpace(cli.ServerBaseUrl))
        config = config with { ServerBaseUrl = NormalizeBase(cli.ServerBaseUrl!) };

    // Single-instance lock (per-user)
    using var mutex = new Mutex(initiallyOwned: true, name: "Local\\HeadphonePagerAgent", createdNew: out var createdNew);
    if (!createdNew)
        return 0;

    using var cts = new CancellationTokenSource();
    var agentTask = RunAgentAsync(config, cts.Token);

    Application.EnableVisualStyles();
    Application.SetCompatibleTextRenderingDefault(false);

    var iconPath = Path.Combine(AppContext.BaseDirectory, "tray.ico");
    var tooltip = "Headphone Pager";
    Application.Run(new TrayAppContext(cts, tooltip, iconPath));

    cts.Cancel();
    try { agentTask.GetAwaiter().GetResult(); } catch { }
    return 0;
}

private static async Task<int> RunAgentAsync(AgentConfig config, CancellationToken token)
{
    try
    {
        await RunLoop(config, token);
        return 0;
    }
    catch (OperationCanceledException)
    {
        return 0;
    }
    catch (Exception ex)
    {
        try
        {
            var logPath = Path.Combine(AppContext.BaseDirectory, "agent.log");
            await File.AppendAllTextAsync(logPath, $"[{DateTime.Now:O}] Fatal: {ex}\n");
        }
        catch { }
        return 1;
    }
}

private sealed class TrayAppContext : ApplicationContext
{
    private readonly NotifyIcon _icon;
    private readonly CancellationTokenSource _cts;

    public TrayAppContext(CancellationTokenSource cts, string tooltip, string iconPath)
    {
        _cts = cts;

        Icon ico;
        try
        {
            ico = (!string.IsNullOrWhiteSpace(iconPath) && File.Exists(iconPath))
                ? new Icon(iconPath)
                : SystemIcons.Application;
        }
        catch { ico = SystemIcons.Application; }

        var menu = new ContextMenuStrip();
        var quit = new ToolStripMenuItem("Quit");
        quit.Click += (_, _) => ExitRequested();
        menu.Items.Add(quit);

        _icon = new NotifyIcon
        {
            Icon = ico,
            Text = tooltip.Length > 63 ? tooltip[..63] : tooltip,
            Visible = true,
            ContextMenuStrip = menu
        };

        try { _icon.ShowBalloonTip(1200, "Headphone Pager", "Agent running (right-click to quit)", ToolTipIcon.Info); } catch { }
    }

    private void ExitRequested()
    {
        _cts.Cancel();
        try { _icon.Visible = false; _icon.Dispose(); } catch { }
        ExitThread();
    }
}

private static string GetHelpText()
{
    var sb = new StringBuilder();
    sb.AppendLine("Headphone Pager Agent");
    sb.AppendLine();
    sb.AppendLine("Pair a device:");
    sb.AppendLine("  HeadphoneAgent.exe --pair 123456 --server http://home.lan:8585 --name \"Kid-PC\"");
    sb.AppendLine();
    sb.AppendLine("Run (uses saved config):");
    sb.AppendLine("  HeadphoneAgent.exe --server http://home.lan:8585");
    sb.AppendLine();
    sb.AppendLine("Options:");
    sb.AppendLine("  --pair <code>     Pairing code from the UI");
    sb.AppendLine("  --server <url>    Backend base URL");
    sb.AppendLine("  --name <name>     Device display name (pairing)");
    return sb.ToString();
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
