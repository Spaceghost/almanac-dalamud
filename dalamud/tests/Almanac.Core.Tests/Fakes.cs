using System.Net;
using System.Text;
using System.Text.Json.Nodes;

namespace Almanac.Core.Tests;

/// <summary>An HttpMessageHandler answering from a function (no sockets).</summary>
public sealed class FakeHandler(Func<HttpRequestMessage, string, HttpResponseMessage> respond) : HttpMessageHandler
{
    public List<(string Url, string Body)> Requests { get; } = [];

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        var body = request.Content == null ? "" : await request.Content.ReadAsStringAsync(ct);
        lock (Requests)
            Requests.Add((request.RequestUri!.ToString(), body));
        return respond(request, body);
    }

    public static HttpResponseMessage Json(string json, HttpStatusCode status = HttpStatusCode.OK) =>
        new(status) { Content = new StringContent(json, Encoding.UTF8, "application/json") };

    public static HttpResponseMessage Sse(IEnumerable<string> data) =>
        new(HttpStatusCode.OK) { Content = new StringContent(string.Concat(data.Select(d => $"data: {d}\n\n")), Encoding.UTF8, "text/event-stream") };
}

/// <summary>
/// A scripted OpenAI-compatible model. For each user prompt it plays a list of turns; a turn is either tool calls or a
/// final text. The turn index is the number of assistant messages after the last real user message.
/// Streams like Ollama: text in small pieces, tool-call arguments split across chunks, usage in the last chunk.
/// </summary>
public sealed class ScriptedModel
{
    public Dictionary<string, List<Turn>> Script { get; } = new(StringComparer.Ordinal);

    public bool RejectTools { get; init; }

    public sealed record Turn(string? Text, params (string Name, string Args)[] Calls);

    public HttpResponseMessage Respond(HttpRequestMessage request, string body)
    {
        var json = JsonNode.Parse(body)!;
        if (RejectTools && json["tools"] != null)
            return FakeHandler.Json("""{"error":{"message":"registry.ollama.ai/library/x does not support tools"}}""", HttpStatusCode.BadRequest);
        var messages = json["messages"]!.AsArray();
        var lastUser = -1;
        for (var i = 0; i < messages.Count; i++)
        {
            if (messages[i]!["role"]!.GetValue<string>() == "user" && !messages[i]!["content"]!.GetValue<string>().StartsWith("Tool result:", StringComparison.Ordinal))
                lastUser = i;
        }

        var prompt = messages[lastUser]!["content"]!.GetValue<string>();
        var turnIndex = messages.Skip(lastUser + 1).Count(m => m!["role"]!.GetValue<string>() == "assistant");
        if (!Script.TryGetValue(prompt, out var turns))
            return FakeHandler.Sse(Text("OK"));
        var turn = turns[Math.Min(turnIndex, turns.Count - 1)];
        if (turn.Calls.Length > 0 && json["tools"] == null)
        {
            // Prompted mode: answer with the JSON object form (one call per reply).
            var (name, args) = turn.Calls[0];
            return FakeHandler.Sse(Text($"{{\"tool\": \"{name}\", \"arguments\": {args}}}"));
        }

        return FakeHandler.Sse(turn.Calls.Length > 0 ? Calls(turn.Calls) : Text(turn.Text ?? ""));
    }

    private static IEnumerable<string> Text(string text)
    {
        for (var i = 0; i < text.Length; i += 4)
            yield return new JsonObject { ["choices"] = new JsonArray(new JsonObject { ["index"] = 0, ["delta"] = new JsonObject { ["content"] = text.Substring(i, Math.Min(4, text.Length - i)) } }) }.ToJsonString();
        yield return """{"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}""";
        yield return """{"choices":[],"usage":{"prompt_tokens":50,"completion_tokens":12}}""";
        yield return "[DONE]";
    }

    private static IEnumerable<string> Calls((string Name, string Args)[] calls)
    {
        for (var i = 0; i < calls.Length; i++)
        {
            var (name, args) = calls[i];
            var half = args.Length / 2;
            yield return new JsonObject { ["choices"] = new JsonArray(new JsonObject { ["index"] = 0, ["delta"] = new JsonObject { ["tool_calls"] = new JsonArray(new JsonObject { ["index"] = i, ["id"] = $"call_{i}", ["type"] = "function", ["function"] = new JsonObject { ["name"] = name, ["arguments"] = args[..half] } }) } }) }.ToJsonString();
            yield return new JsonObject { ["choices"] = new JsonArray(new JsonObject { ["index"] = 0, ["delta"] = new JsonObject { ["tool_calls"] = new JsonArray(new JsonObject { ["index"] = i, ["function"] = new JsonObject { ["arguments"] = args[half..] } }) } }) }.ToJsonString();
        }

        yield return """{"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}""";
        yield return "[DONE]";
    }
}
