using System.Net.Http.Json;
using System.Text.Json.Nodes;

namespace Almanac.Core.Setup;

/// <summary>Where a pull is: Ollama's status line and, while a layer downloads, its bytes.</summary>
public sealed record PullProgress(string Status, long Completed, long Total)
{
    /// <summary>0..1 while bytes are known, else null.</summary>
    public double? Fraction => Total > 0 ? Math.Clamp((double)Completed / Total, 0, 1) : null;
}

/// <summary>
/// Downloads a model into Ollama (<c>POST /api/pull</c>), reporting its progress, so the setup can install a
/// recommended model with one click instead of a command typed in a terminal.
/// </summary>
public static class ModelPull
{
    /// <summary>Pulls <paramref name="model"/> into the Ollama at <paramref name="rootUrl"/> (no /v1).</summary>
    /// <exception cref="InvalidOperationException">Ollama reported an error (unknown model, disk full, ...).</exception>
    public static async Task PullAsync(HttpClient http, string rootUrl, string model, IProgress<PullProgress>? progress, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, rootUrl.TrimEnd('/') + "/api/pull")
        {
            Content = JsonContent.Create(new { model, stream = true }),
        };
        using var response = await http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
            throw new InvalidOperationException($"{model}: {(int)response.StatusCode} {await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false)}".Trim());
        var stream = await response.Content.ReadAsStreamAsync(ct).ConfigureAwait(false);
        await using var scope = stream.ConfigureAwait(false);
        using var reader = new StreamReader(stream);
        var last = "";
        while (await reader.ReadLineAsync(ct).ConfigureAwait(false) is { } line)
        {
            if (line.Length == 0)
                continue;
            var node = JsonNode.Parse(line);
            if (node?["error"]?.GetValue<string>() is { } error)
                throw new InvalidOperationException($"{model}: {error}");
            last = node?["status"]?.GetValue<string>() ?? last;
            progress?.Report(new PullProgress(last, Long(node?["completed"]), Long(node?["total"])));
        }

        if (last != "success")
            throw new InvalidOperationException($"{model}: the download ended at \"{last}\"");
    }

    private static long Long(JsonNode? n) => n is JsonValue v && v.TryGetValue<long>(out var l) ? l : 0;
}
