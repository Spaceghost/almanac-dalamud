using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Almanac.Core.Bench;

/// <summary>Hardware facts that may leave the machine (all coarse, nothing identifying).</summary>
public sealed record HardwareFacts(string GpuModel, string GpuVendor, int VramMb, int? SystemRamGb, string Os)
{
    public static string VendorOf(string gpuName)
    {
        var n = gpuName.ToLowerInvariant();
        if (n.Contains("nvidia") || n.Contains("geforce") || n.Contains("rtx") || n.Contains("quadro"))
            return "nvidia";
        if (n.Contains("amd") || n.Contains("radeon"))
            return "amd";
        if (n.Contains("intel") || n.Contains("arc"))
            return "intel";
        if (n.Contains("apple"))
            return "apple";
        return n.Length == 0 || n == "unknown" ? "unknown" : "other";
    }
}

/// <summary>Model and backend facts for a result.</summary>
public sealed record ModelFacts(string BackendKind, string? BackendVersion, string Name, string? Family, double? ParamsB, string Quant, int Context);

/// <summary>Builds and submits results in the v1 format (benchmark/schema/results.schema.json).</summary>
public static partial class Results
{
    public const string DefaultLeaderboard = "https://spacegho.st/mods/ffxiv/almanac";

    public static JsonObject Build(BenchRun run, HardwareFacts hardware, ModelFacts model, string clientName, string clientVersion)
    {
        var tasks = new JsonArray();
        foreach (var t in run.Tasks)
        {
            tasks.Add(new JsonObject
            {
                ["id"] = t.Task.Id,
                ["success"] = t.Score.Success,
                ["score"] = Round(t.Score.Score, 4),
                ["tool_calls"] = t.Score.TotalCalls,
                ["tool_calls_valid"] = t.Score.ValidCalls,
                ["ttft_ms"] = t.TtftMs is { } ttft ? Round(ttft, 1) : (double?)null,
                ["tokens_per_s"] = t.TokensPerSecond is { } tps ? Round(tps, 2) : (double?)null,
                ["output_tokens"] = t.OutputTokens,
                ["duration_ms"] = Round(t.DurationMs, 1),
                ["error"] = t.Score.Error,
            });
        }

        var hw = new JsonObject
        {
            ["gpu_model"] = Clean(hardware.GpuModel, 80, "unknown"),
            ["gpu_vendor"] = hardware.GpuVendor,
            ["vram_mb"] = Math.Max(0, hardware.VramMb / 256 * 256),
            ["os"] = hardware.Os,
        };
        if (hardware.SystemRamGb is { } ram)
            hw["system_ram_gb"] = ram;

        var backend = new JsonObject { ["kind"] = model.BackendKind };
        if (!string.IsNullOrEmpty(model.BackendVersion))
            backend["version"] = Clean(model.BackendVersion, 32, "");

        var modelNode = new JsonObject
        {
            ["name"] = SafeModelName(model.Name),
            ["quant"] = Clean(model.Quant, 24, "unknown"),
            ["context"] = Math.Max(0, model.Context),
            ["tool_calling"] = run.ToolCalling,
        };
        if (!string.IsNullOrEmpty(model.Family))
            modelNode["family"] = Clean(model.Family, 40, "");
        modelNode["params_b"] = model.ParamsB;

        var metrics = new JsonObject
        {
            ["score"] = run.Score,
            ["success_rate"] = Round(run.SuccessRate, 4),
            ["tool_call_validity"] = Round(run.ToolCallValidity, 4),
            ["tokens_per_s"] = Round(run.TokensPerSecond, 2),
            ["ttft_ms"] = Round(run.TtftMs, 1),
            ["peak_vram_mb"] = run.PeakVramMb,
            ["total_s"] = Round(run.TotalSeconds, 2),
        };
        if (run.Quality is { } q)
            metrics["quality"] = Round(q, 4);

        return new JsonObject
        {
            ["schema_version"] = 1,
            ["suite"] = new JsonObject { ["id"] = run.Suite.Id, ["version"] = run.Suite.Version, ["sha256"] = run.Suite.Sha256 },
            ["client"] = new JsonObject { ["name"] = clientName, ["version"] = Clean(clientVersion, 32, "0") },
            ["mode"] = run.Mode == BenchMode.Live ? "live" : "mock",
            ["hardware"] = hw,
            ["backend"] = backend,
            ["model"] = modelNode,
            ["metrics"] = metrics,
            ["tasks"] = tasks,
        };
    }

    /// <summary>Posts a result. Returns (ok, message). The caller shows the JSON and asks first.</summary>
    public static async Task<(bool Ok, string Message)> SubmitAsync(HttpClient http, string leaderboardBase, JsonObject result, CancellationToken ct)
    {
        try
        {
            using var content = new StringContent(result.ToJsonString(), Encoding.UTF8, "application/json");
            using var response = await http.PostAsync($"{leaderboardBase.TrimEnd('/')}/api/results", content, ct).ConfigureAwait(false);
            var body = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            if (body.Length > 300)
                body = body[..300];
            return (response.IsSuccessStatusCode, response.IsSuccessStatusCode ? "Submitted. Thank you!" : $"HTTP {(int)response.StatusCode}: {body}");
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            return (false, ex.Message);
        }
    }

    /// <summary>
    /// Model ids can be file paths (llama.cpp reports the GGUF path, which may contain a user name): keep only the file
    /// name then, and replace anything outside the schema's character set.
    /// </summary>
    public static string SafeModelName(string name)
    {
        var n = name.Trim();
        if (n.Contains('\\') || n.StartsWith('/') || n.StartsWith('~') || n.Length > 2 && n[1] == ':' && char.IsLetter(n[0]))
            n = n[(n.LastIndexOfAny(['/', '\\']) + 1)..];
        return Clean(ModelName().Replace(n, "_"), 120, "unknown");
    }

    private static double Round(double v, int digits) => Math.Round(v, digits);

    private static string Clean(string? s, int max, string fallback)
    {
        s = (s ?? "").Trim();
        s = new string(s.Where(c => !char.IsControl(c)).ToArray());
        if (s.Length == 0)
            return fallback;
        return s.Length > max ? s[..max] : s;
    }

    [GeneratedRegex("[^A-Za-z0-9._:/@+-]")]
    private static partial Regex ModelName();
}
