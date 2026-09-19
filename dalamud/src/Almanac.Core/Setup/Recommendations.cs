using System.Text.Json;
using System.Text.Json.Nodes;

namespace Almanac.Core.Setup;

public sealed record RecommendedModel(
    string Name,
    string? Ollama,
    string? LmStudio,
    string Quant,
    int Context,
    int? VramMb,
    string ToolCalling,
    double? Score,
    double? TokensPerSecond,
    int Samples,
    string Notes);

public sealed record RecommendationTier(string Id, string Label, int MinVramMb, int? MaxVramMb, IReadOnlyList<RecommendedModel> Models)
{
    public bool Contains(int vramMb) => vramMb >= MinVramMb && (MaxVramMb == null || vramMb < MaxVramMb);
}

/// <summary>Model recommendations per VRAM tier (benchmark/schema/recommendations.schema.json).</summary>
public sealed class Recommendations
{
    public const string BundledResource = "Almanac.Core.recommendations.json";

    public required string Source { get; init; }

    public required string GeneratedAt { get; init; }

    public required IReadOnlyList<RecommendationTier> Tiers { get; init; }

    public static Recommendations Parse(string json)
    {
        var root = JsonNode.Parse(json) ?? throw new JsonException("empty");
        if (root["schema_version"]?.GetValue<int>() != 1)
            throw new JsonException("unsupported schema_version");
        var tiers = new List<RecommendationTier>();
        foreach (var t in root["tiers"]!.AsArray())
        {
            var models = new List<RecommendedModel>();
            foreach (var m in t!["models"]!.AsArray())
            {
                models.Add(new RecommendedModel(
                    m!["name"]!.GetValue<string>(),
                    m["ollama"]?.GetValue<string>(),
                    m["lmstudio"]?.GetValue<string>(),
                    m["quant"]?.GetValue<string>() ?? "",
                    m["context"]?.GetValue<int>() ?? 0,
                    m["vram_mb"]?.GetValue<int>(),
                    m["tool_calling"]?.GetValue<string>() ?? "unknown",
                    m["score"]?.GetValue<double>(),
                    m["tokens_per_s"]?.GetValue<double>(),
                    m["samples"]?.GetValue<int>() ?? 0,
                    m["notes"]?.GetValue<string>() ?? ""));
            }

            tiers.Add(new RecommendationTier(t["id"]!.GetValue<string>(), t["label"]!.GetValue<string>(), t["min_vram_mb"]!.GetValue<int>(), t["max_vram_mb"]?.GetValue<int>(), models));
        }

        return new Recommendations
        {
            Source = root["source"]?.GetValue<string>() ?? "leaderboard",
            GeneratedAt = root["generated_at"]?.GetValue<string>() ?? "",
            Tiers = tiers.OrderBy(t => t.MinVramMb).ToList(),
        };
    }

    public static Recommendations Bundled()
    {
        using var stream = typeof(Recommendations).Assembly.GetManifestResourceStream(BundledResource)!;
        using var reader = new StreamReader(stream);
        return Parse(reader.ReadToEnd());
    }

    /// <summary>
    /// VRAM the model may use: the card's dedicated memory, minus what the game needs when both share one GPU.
    /// </summary>
    public static int Budget(int dedicatedVramMb, bool gameOnSameGpu, int gameReserveMb) =>
        Math.Max(0, dedicatedVramMb - (gameOnSameGpu ? gameReserveMb : 0));

    public RecommendationTier? TierFor(int budgetMb) => Tiers.FirstOrDefault(t => t.Contains(budgetMb)) ?? Tiers.LastOrDefault(t => budgetMb >= t.MinVramMb);

    /// <summary>The tier's models that fit the budget (unknown size counts as fitting), best first.</summary>
    public IReadOnlyList<RecommendedModel> For(int budgetMb) =>
        TierFor(budgetMb)?.Models.Where(m => m.VramMb == null || m.VramMb <= budgetMb).ToList() ?? [];
}

/// <summary>
/// Fetches recommendations from the leaderboard, falling back to the last good copy and then to the bundled list.
/// </summary>
public sealed class RecommendationSource(HttpClient http, string url, Func<string?> readCache, Action<string> writeCache)
{
    public async Task<Recommendations> LoadAsync(CancellationToken ct)
    {
        try
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(TimeSpan.FromSeconds(5));
            var text = await http.GetStringAsync(url, cts.Token).ConfigureAwait(false);
            var parsed = Recommendations.Parse(text);
            writeCache(text);
            return parsed;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException or InvalidOperationException or KeyNotFoundException or NullReferenceException)
        {
            // Offline or not deployed yet.
        }

        if (readCache() is { } cached)
        {
            try
            {
                return Recommendations.Parse(cached);
            }
            catch (Exception ex) when (ex is JsonException or InvalidOperationException or NullReferenceException)
            {
            }
        }

        return Recommendations.Bundled();
    }
}
