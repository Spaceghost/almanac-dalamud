using System.Net.Http.Headers;
using System.Text.Json.Nodes;

namespace Almanac.Core.Setup;

/// <summary>
/// The inference GPU as the almanac engine sees it (<c>GET /v1/almanac/gpu</c>): the machine running the models,
/// which is often not the one running the game, so the setup asks it instead of measuring locally.
/// </summary>
public sealed record ServerGpu(
    string Name,
    int TotalMb,
    int FreeMb,
    int? BudgetMb,
    IReadOnlyList<(string Model, int VramMb)> Loaded,
    IReadOnlyDictionary<string, int> Reservations,
    string? AutoChoice,
    string? AutoReason)
{
    /// <summary>Reads it from an almanac gateway. Null when it has no GPU report (an older engine, or no GPU).</summary>
    public static async Task<ServerGpu?> ReadAsync(HttpClient http, string baseUrl, string? token, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, ServerDetector.RootOf(baseUrl) + "/v1/almanac/gpu");
        if (!string.IsNullOrEmpty(token))
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        using var response = await http.SendAsync(request, ct).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
            return null;
        return Parse(JsonNode.Parse(await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false)));
    }

    internal static ServerGpu? Parse(JsonNode? body)
    {
        if (body?["inference"] is not JsonObject card)
            return null;
        var loaded = (body["loaded"] as JsonArray ?? [])
            .Select(m => (m?["model"]?.GetValue<string>() ?? "?", m?["vram_mb"]?.GetValue<int>() ?? 0))
            .ToList();
        var reservations = (body["reservations"] as JsonObject ?? [])
            .ToDictionary(kv => kv.Key, kv => kv.Value?.GetValue<int>() ?? 0);
        return new ServerGpu(
            card["name"]?.GetValue<string>() ?? "GPU",
            card["total_mb"]?.GetValue<int>() ?? 0,
            card["free_mb"]?.GetValue<int>() ?? 0,
            body["budget_mb"]?.GetValue<int>(),
            loaded,
            reservations,
            body["auto"]?["choice"]?.GetValue<string>(),
            body["auto"]?["reason"]?.GetValue<string>());
    }
}
