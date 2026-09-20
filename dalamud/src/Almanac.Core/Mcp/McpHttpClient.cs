using Almanac.Core.Diagnostics;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Almanac.Core.Llm;

namespace Almanac.Core.Mcp;

/// <summary>An MCP tool as <c>tools/list</c> describes it.</summary>
public sealed record McpTool(string Name, string Description, JsonObject InputSchema, JsonObject? Annotations);

/// <summary>A <c>tools/call</c> result: the text content joined, plus structured content when the server sent it.</summary>
public sealed record McpCallResult(string Text, bool IsError, JsonNode? Structured);

public sealed class McpException(string message) : Exception(message);

/// <summary>
/// MCP client over Streamable HTTP (JSON-RPC POSTs; replies as JSON or as an SSE stream). Enough for XivMcp and the
/// almanac MCP server: initialize, tools/list, tools/call. Thread-safe for concurrent calls after initialisation.
/// </summary>
public sealed class McpHttpClient(HttpClient http, Uri endpoint, string? bearerToken, string clientName, string clientVersion) : IDisposable
{
    public const string ProtocolVersion = "2025-06-18";

    private readonly SemaphoreSlim initLock = Track();

    private int disposed;
    private string? sessionId;
    private string? negotiatedVersion;
    private int nextId;

    public Uri Endpoint { get; } = endpoint;

    public bool Initialized => negotiatedVersion != null;

    public string? ServerName { get; private set; }

    public async Task InitializeAsync(CancellationToken ct)
    {
        await initLock.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            if (negotiatedVersion != null)
                return;
            var result = await RequestAsync("initialize", new JsonObject
            {
                ["protocolVersion"] = ProtocolVersion,
                ["capabilities"] = new JsonObject(),
                ["clientInfo"] = new JsonObject { ["name"] = clientName, ["version"] = clientVersion },
            }, ct).ConfigureAwait(false);
            negotiatedVersion = result?["protocolVersion"]?.GetValue<string>() ?? ProtocolVersion;
            ServerName = result?["serverInfo"]?["name"]?.GetValue<string>();
            await NotifyAsync("notifications/initialized", ct).ConfigureAwait(false);
        }
        finally
        {
            initLock.Release();
        }
    }

    public async Task<IReadOnlyList<McpTool>> ListToolsAsync(CancellationToken ct)
    {
        await InitializeAsync(ct).ConfigureAwait(false);
        var tools = new List<McpTool>();
        string? cursor = null;
        for (var page = 0; page < 20; page++)
        {
            var p = new JsonObject();
            if (cursor != null)
                p["cursor"] = cursor;
            var result = await RequestAsync("tools/list", p, ct).ConfigureAwait(false);
            if (result?["tools"] is JsonArray array)
            {
                foreach (var t in array)
                {
                    var name = t?["name"]?.GetValue<string>();
                    if (string.IsNullOrEmpty(name))
                        continue;
                    var schema = t!["inputSchema"] as JsonObject ?? new JsonObject { ["type"] = "object" };
                    tools.Add(new McpTool(name, t["description"]?.GetValue<string>() ?? "", (JsonObject)schema.DeepClone(), t["annotations"]?.DeepClone() as JsonObject));
                }
            }

            cursor = result?["nextCursor"]?.GetValue<string>();
            if (string.IsNullOrEmpty(cursor))
                break;
        }

        return tools;
    }

    public async Task<McpCallResult> CallToolAsync(string name, JsonObject arguments, CancellationToken ct)
    {
        await InitializeAsync(ct).ConfigureAwait(false);
        var result = await RequestAsync("tools/call", new JsonObject { ["name"] = name, ["arguments"] = arguments.DeepClone() }, ct).ConfigureAwait(false);
        var isError = result?["isError"] is JsonValue v && v.TryGetValue<bool>(out var e) && e;
        var sb = new StringBuilder();
        if (result?["content"] is JsonArray content)
        {
            foreach (var item in content)
            {
                if (item?["type"]?.GetValue<string>() == "text" && item["text"]?.GetValue<string>() is { } text)
                {
                    if (sb.Length > 0)
                        sb.Append('\n');
                    sb.Append(text);
                }
            }
        }

        var structured = result?["structuredContent"]?.DeepClone();
        if (sb.Length == 0 && structured != null)
            sb.Append(structured.ToJsonString());
        return new McpCallResult(sb.ToString(), isError, structured);
    }

    private async Task NotifyAsync(string method, CancellationToken ct)
    {
        using var message = NewRequest(new JsonObject { ["jsonrpc"] = "2.0", ["method"] = method });
        using var response = await http.SendAsync(message, HttpCompletionOption.ResponseHeadersRead, ct).ConfigureAwait(false);
        // 202 Accepted is the norm; anything else is ignored for notifications.
    }

    private async Task<JsonNode?> RequestAsync(string method, JsonObject parameters, CancellationToken ct)
    {
        var id = Interlocked.Increment(ref nextId);
        using var message = NewRequest(new JsonObject { ["jsonrpc"] = "2.0", ["id"] = id, ["method"] = method, ["params"] = parameters });
        using var response = await http.SendAsync(message, HttpCompletionOption.ResponseHeadersRead, ct).ConfigureAwait(false);
        if (response.Headers.TryGetValues("Mcp-Session-Id", out var ids))
            sessionId = ids.FirstOrDefault() ?? sessionId;
        if (!response.IsSuccessStatusCode)
        {
            var body = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            if ((int)response.StatusCode == 404 && sessionId != null)
            {
                // Session expired (server restarted): start over next time.
                sessionId = null;
                negotiatedVersion = null;
            }

            throw new McpException($"{method}: HTTP {(int)response.StatusCode} {(body.Length > 200 ? body[..200] : body)}".Trim());
        }

        var mediaType = response.Content.Headers.ContentType?.MediaType ?? "";
        var stream = await response.Content.ReadAsStreamAsync(ct).ConfigureAwait(false);
        await using var streamScope = stream.ConfigureAwait(false);
        JsonNode? reply = null;
        if (mediaType.Contains("event-stream", StringComparison.OrdinalIgnoreCase))
        {
            await foreach (var data in Sse.ReadDataAsync(stream, ct).ConfigureAwait(false))
            {
                JsonNode? node;
                try
                {
                    node = JsonNode.Parse(data);
                }
                catch (JsonException)
                {
                    continue;
                }

                if (node?["id"] is JsonValue idv && idv.TryGetValue<int>(out var rid) && rid == id)
                {
                    reply = node;
                    break;
                }
            }
        }
        else
        {
            reply = await JsonNode.ParseAsync(stream, cancellationToken: ct).ConfigureAwait(false);
        }

        if (reply == null)
            throw new McpException($"{method}: no response");
        if (reply["error"] is { } error)
            throw new McpException($"{method}: {error["message"]?.GetValue<string>() ?? error.ToJsonString()}");
        return reply["result"];
    }

    private HttpRequestMessage NewRequest(JsonObject body)
    {
        var message = new HttpRequestMessage(HttpMethod.Post, Endpoint)
        {
            Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json"),
        };
        message.Headers.Accept.ParseAdd("application/json");
        message.Headers.Accept.ParseAdd("text/event-stream");
        if (!string.IsNullOrEmpty(bearerToken))
            message.Headers.Authorization = new AuthenticationHeaderValue("Bearer", bearerToken);
        if (sessionId != null)
            message.Headers.TryAddWithoutValidation("Mcp-Session-Id", sessionId);
        if (negotiatedVersion != null)
            message.Headers.TryAddWithoutValidation("MCP-Protocol-Version", negotiatedVersion);
        return message;
    }

    /// <summary>Releases the initialisation lock. The <see cref="HttpClient"/> is the caller's and is left alone.</summary>
    public void Dispose()
    {
        if (Interlocked.Exchange(ref disposed, 1) != 0)
            return;
        initLock.Dispose();
        LiveObjects.Released(LiveObjects.Kinds.McpClient);
    }

    private static SemaphoreSlim Track()
    {
        LiveObjects.Acquired(LiveObjects.Kinds.McpClient);
        return new SemaphoreSlim(1, 1);
    }
}
