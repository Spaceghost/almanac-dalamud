using System.Net.Http.Headers;
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

/// <summary>How a submission ended. 401 is <see cref="SignInRequired"/> (forget the token, link again); 403 is <see cref="Forbidden"/> (do not retry).</summary>
public enum SubmitOutcome
{
    Ok,
    SignInRequired,
    Forbidden,
    Rejected,
    Unreachable,
}

/// <summary>The outcome and what to show the player.</summary>
public sealed record SubmitResult(SubmitOutcome Outcome, string Message)
{
    public bool Ok => Outcome == SubmitOutcome.Ok;
}

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

    /// <summary>
    /// Posts a result with the player's leaderboard token. The caller shows the JSON and asks first. The message is the
    /// server's own on a refusal; <see cref="SubmitOutcome.SignInRequired"/> means the token is no good and must be forgotten.
    /// </summary>
    public static async Task<SubmitResult> SubmitAsync(HttpClient http, string leaderboardBase, JsonObject result, string? token, CancellationToken ct)
    {
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Post, $"{leaderboardBase.TrimEnd('/')}/api/results");
            request.Content = new StringContent(result.ToJsonString(), Encoding.UTF8, "application/json");
            if (!string.IsNullOrEmpty(token))
                request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
            using var response = await http.SendAsync(request, ct).ConfigureAwait(false);
            if (response.IsSuccessStatusCode)
                return new SubmitResult(SubmitOutcome.Ok, "Submitted. Thank you!");
            var status = (int)response.StatusCode;
            var (_, message) = DeviceLink.ServerMessage(await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false), status);
            return new SubmitResult(status == 401 ? SubmitOutcome.SignInRequired : status == 403 ? SubmitOutcome.Forbidden : SubmitOutcome.Rejected, message);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            return new SubmitResult(SubmitOutcome.Unreachable, ex.Message);
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
