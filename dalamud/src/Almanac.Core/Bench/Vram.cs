using System.Diagnostics;
using System.Globalization;
using System.Text.Json.Nodes;

namespace Almanac.Core.Bench;

/// <summary>Reads GPU memory in use, in MB, or null when it cannot tell.</summary>
public interface IVramProbe
{
    Task<int?> ReadUsedMbAsync(CancellationToken ct);
}

/// <summary>Ollama's own accounting: the sum of <c>size_vram</c> of the loaded models (GET /api/ps).</summary>
public sealed class OllamaVramProbe(HttpClient http, string rootUrl) : IVramProbe
{
    public async Task<int?> ReadUsedMbAsync(CancellationToken ct)
    {
        try
        {
            var text = await http.GetStringAsync($"{rootUrl.TrimEnd('/')}/api/ps", ct).ConfigureAwait(false);
            return Parse(text);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or System.Text.Json.JsonException)
        {
            return null;
        }
    }

    internal static int? Parse(string json)
    {
        if (JsonNode.Parse(json)?["models"] is not JsonArray models)
            return null;
        long bytes = 0;
        foreach (var m in models)
            bytes += m?["size_vram"]?.GetValue<long>() ?? 0;
        return (int)(bytes / (1024 * 1024));
    }
}

/// <summary><c>nvidia-smi</c> total memory.used over all GPUs (Linux and Windows drivers ship it).</summary>
public sealed class NvidiaSmiProbe : IVramProbe
{
    public async Task<int?> ReadUsedMbAsync(CancellationToken ct)
    {
        try
        {
            using var p = Process.Start(new ProcessStartInfo("nvidia-smi", "--query-gpu=memory.used --format=csv,noheader,nounits")
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            });
            if (p == null)
                return null;
            var output = await p.StandardOutput.ReadToEndAsync(ct).ConfigureAwait(false);
            await p.WaitForExitAsync(ct).ConfigureAwait(false);
            return p.ExitCode == 0 ? ParseCsv(output) : null;
        }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException or IOException)
        {
            return null;
        }
    }

    internal static int? ParseCsv(string output)
    {
        var total = 0;
        var any = false;
        foreach (var line in output.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            if (int.TryParse(line, NumberStyles.Integer, CultureInfo.InvariantCulture, out var mb))
            {
                total += mb;
                any = true;
            }
        }

        return any ? total : null;
    }
}

/// <summary>Tries each probe in order; the first that answers wins for the rest of the run.</summary>
public sealed class FirstVramProbe(params IVramProbe[] probes) : IVramProbe
{
    private IVramProbe? chosen;

    public async Task<int?> ReadUsedMbAsync(CancellationToken ct)
    {
        if (chosen != null)
            return await chosen.ReadUsedMbAsync(ct).ConfigureAwait(false);
        foreach (var probe in probes)
        {
            if (await probe.ReadUsedMbAsync(ct).ConfigureAwait(false) is { } mb)
            {
                chosen = probe;
                return mb;
            }
        }

        return null;
    }
}

/// <summary>Samples a probe every interval in the background and keeps the peak.</summary>
public sealed class VramSampler(IVramProbe probe, TimeSpan interval) : IAsyncDisposable
{
    private readonly CancellationTokenSource cts = new();
    private Task? loop;
    private int? peak;

    public void Start() => loop = Task.Run(async () =>
    {
        while (!cts.IsCancellationRequested)
        {
            await SampleAsync().ConfigureAwait(false);
            try
            {
                await Task.Delay(interval, cts.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }
    });

    public async Task<int?> StopAsync()
    {
        await cts.CancelAsync().ConfigureAwait(false);
        if (loop != null)
            await loop.ConfigureAwait(false);
        loop = null;
        await SampleAsync().ConfigureAwait(false);
        return peak;
    }

    private async Task SampleAsync()
    {
        try
        {
            if (await probe.ReadUsedMbAsync(CancellationToken.None).ConfigureAwait(false) is { } mb)
                peak = Math.Max(peak ?? 0, mb);
        }
        catch
        {
            // Sampling is best effort.
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (loop != null)
            await StopAsync().ConfigureAwait(false);
        cts.Dispose();
    }
}
