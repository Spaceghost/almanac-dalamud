using System.Text.Json;
using System.Text.Json.Nodes;

namespace Almanac.Core.Setup;

public static class BackendKinds
{
    public const string Ollama = "ollama";
    public const string LmStudio = "lmstudio";
    public const string LlamaCpp = "llamacpp";
    public const string OpenAiCompatible = "openai-compatible";
    public const string Almanac = "almanac";
}

/// <summary>A model a server offers, with whatever the server says about it.</summary>
public sealed record ServerModel(string Id, long? SizeBytes = null, string? Quant = null, string? ParameterSize = null, string? Family = null);

/// <summary>A local model server that answered.</summary>
public sealed record DetectedServer(string Kind, string BaseUrl, string? Version, IReadOnlyList<ServerModel> Models)
{
    /// <summary>The server root (BaseUrl without the trailing /v1).</summary>
    public string RootUrl => ServerDetector.RootOf(BaseUrl);

    public string Label => Kind switch
    {
        BackendKinds.Ollama => "Ollama",
        BackendKinds.LmStudio => "LM Studio",
        BackendKinds.LlamaCpp => "llama.cpp server",
        BackendKinds.Almanac => "almanac engine",
        _ => "OpenAI-compatible server",
    };
}

/// <summary>Finds local model servers on their default ports and identifies them.</summary>
public sealed class ServerDetector(HttpClient http)
{
    /// <summary>Default candidates, most common first. Loopback only: nothing is scanned on the network.</summary>
    public static readonly IReadOnlyList<string> DefaultCandidates =
    [
        "http://127.0.0.1:11434/v1", // Ollama
        "http://127.0.0.1:1234/v1", // LM Studio
        "http://127.0.0.1:8080/v1", // llama.cpp server
        "http://127.0.0.1:5001/v1", // KoboldCpp
        "http://127.0.0.1:8000/v1", // vLLM
    ];

    public TimeSpan Timeout { get; init; } = TimeSpan.FromSeconds(1.5);

    public async Task<IReadOnlyList<DetectedServer>> DetectAllAsync(IEnumerable<string>? candidates, CancellationToken ct)
    {
        var probes = (candidates ?? DefaultCandidates).Distinct(StringComparer.OrdinalIgnoreCase).Select(c => ProbeAsync(c, null, ct)).ToList();
        var results = await Task.WhenAll(probes).ConfigureAwait(false);
        return results.OfType<DetectedServer>().ToList();
    }

    /// <summary>Probes one base URL (…/v1). Returns null when nothing OpenAI-compatible answers.</summary>
    public async Task<DetectedServer?> ProbeAsync(string baseUrl, string? apiKey, CancellationToken ct)
    {
        baseUrl = NormalizeBase(baseUrl);
        var root = RootOf(baseUrl);
        var models = await GetJsonAsync($"{baseUrl}/models", apiKey, ct).ConfigureAwait(false);
        if (models == null)
            return null;
        var list = ParseOpenAiModels(models);

        // Identify: Ollama answers /api/version; LM Studio /api/v0/models; llama.cpp /props.
        if (await GetJsonAsync($"{root}/api/version", apiKey, ct).ConfigureAwait(false) is { } ov && ov["version"] is JsonValue ver)
        {
            var tags = await GetJsonAsync($"{root}/api/tags", apiKey, ct).ConfigureAwait(false);
            var detailed = tags == null ? list : ParseOllamaTags(tags);
            return new DetectedServer(BackendKinds.Ollama, baseUrl, ver.ToString(), detailed.Count > 0 ? detailed : list);
        }

        if (await GetJsonAsync($"{root}/api/v0/models", apiKey, ct).ConfigureAwait(false) is { } lm && lm["data"] is JsonArray)
            return new DetectedServer(BackendKinds.LmStudio, baseUrl, null, ParseLmStudio(lm) is { Count: > 0 } l ? l : list);

        if (await GetJsonAsync($"{root}/props", apiKey, ct).ConfigureAwait(false) is { } props && (props["default_generation_settings"] != null || props["build_info"] != null))
            return new DetectedServer(BackendKinds.LlamaCpp, baseUrl, props["build_info"]?.ToString(), list);

        return new DetectedServer(BackendKinds.OpenAiCompatible, baseUrl, null, list);
    }

    public static string NormalizeBase(string url)
    {
        url = url.Trim().TrimEnd('/');
        if (!url.Contains("://", StringComparison.Ordinal))
            url = "http://" + url;
        if (url.EndsWith("/chat/completions", StringComparison.OrdinalIgnoreCase))
            url = url[..^"/chat/completions".Length];
        if (Uri.TryCreate(url, UriKind.Absolute, out var uri) && (uri.AbsolutePath is "" or "/"))
            url += "/v1";
        return url;
    }

    public static string RootOf(string baseUrl)
    {
        var b = baseUrl.TrimEnd('/');
        return b.EndsWith("/v1", StringComparison.OrdinalIgnoreCase) ? b[..^3] : b;
    }

    internal static List<ServerModel> ParseOpenAiModels(JsonNode node) =>
        (node["data"] as JsonArray ?? [])
        .Select(m => m?["id"]?.GetValue<string>())
        .OfType<string>()
        .Select(id => new ServerModel(id))
        .ToList();

    internal static List<ServerModel> ParseOllamaTags(JsonNode node) =>
        (node["models"] as JsonArray ?? [])
        .Where(m => m?["name"] != null)
        .Select(m => new ServerModel(
            m!["name"]!.GetValue<string>(),
            m["size"]?.GetValue<long>(),
            m["details"]?["quantization_level"]?.GetValue<string>(),
            m["details"]?["parameter_size"]?.GetValue<string>(),
            m["details"]?["family"]?.GetValue<string>()))
        .ToList();

    internal static List<ServerModel> ParseLmStudio(JsonNode node) =>
        (node["data"] as JsonArray ?? [])
        .Where(m => m?["id"] != null && m["type"]?.GetValue<string>() is null or "llm" or "vlm")
        .Select(m => new ServerModel(m!["id"]!.GetValue<string>(), null, m["quantization"]?.GetValue<string>(), null, m["arch"]?.GetValue<string>()))
        .ToList();

    private async Task<JsonNode?> GetJsonAsync(string url, string? apiKey, CancellationToken ct)
    {
        try
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(Timeout);
            using var request = new HttpRequestMessage(HttpMethod.Get, url);
            if (!string.IsNullOrEmpty(apiKey))
                request.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", apiKey);
            using var response = await http.SendAsync(request, cts.Token).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
                return null;
            var text = await response.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false);
            return JsonNode.Parse(text);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException or InvalidOperationException or UriFormatException)
        {
            return null;
        }
    }
}
