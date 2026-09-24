using System.Diagnostics;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Almanac.Core.Llm;

/// <summary>
/// Streaming OpenAI Chat Completions client for any compatible server: Ollama, LM Studio, llama.cpp server,
/// vLLM, the almanac gateway. <see cref="BaseUrl"/> ends in <c>/v1</c> (or whatever precedes <c>/chat/completions</c>).
/// </summary>
public sealed class ChatClient(HttpClient http, string baseUrl, string? apiKey = null) : IChatBackend
{
    public string BaseUrl { get; } = baseUrl.TrimEnd('/');

    public async Task<ChatResult> CompleteAsync(ChatRequest request, Action<StreamDelta>? onDelta, CancellationToken ct)
    {
        using var message = new HttpRequestMessage(HttpMethod.Post, $"{BaseUrl}/chat/completions")
        {
            Content = new StringContent(BuildBody(request).ToJsonString(), Encoding.UTF8, "application/json"),
        };
        if (!string.IsNullOrEmpty(apiKey))
            message.Headers.Authorization = new AuthenticationHeaderValue("Bearer", apiKey);
        message.Headers.Accept.ParseAdd("text/event-stream");

        var sw = Stopwatch.StartNew();
        using var response = await http.SendAsync(message, HttpCompletionOption.ResponseHeadersRead, ct).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
        {
            var body = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            var text = Truncate(ErrorText(body), 300);
            var status = (int)response.StatusCode;
            if (request.Tools is { Count: > 0 } && (status >= 400 && text.Contains("tool", StringComparison.OrdinalIgnoreCase) || LooksLikeToolsUnsupported(text)))
                throw new ToolsUnsupportedException(status, text);
            throw new ChatHttpException(status, $"HTTP {status}: {text}");
        }

        var accumulator = new StreamAccumulator(sw, onDelta);
        var mediaType = response.Content.Headers.ContentType?.MediaType ?? "";
        var stream = await response.Content.ReadAsStreamAsync(ct).ConfigureAwait(false);
        await using var streamScope = stream.ConfigureAwait(false);
        if (mediaType.Contains("event-stream", StringComparison.OrdinalIgnoreCase))
        {
            await foreach (var data in Sse.ReadDataAsync(stream, ct).ConfigureAwait(false))
            {
                if (data == "[DONE]")
                    break;
                JsonNode? chunk;
                try
                {
                    chunk = JsonNode.Parse(data);
                }
                catch (JsonException)
                {
                    continue;
                }

                if (chunk?["error"] is { } error)
                    throw new ChatHttpException(200, ErrorText(error.ToJsonString()));
                accumulator.AddChunk(chunk);
            }
        }
        else
        {
            // Server ignored stream=true: one JSON body.
            var node = await JsonNode.ParseAsync(stream, cancellationToken: ct).ConfigureAwait(false);
            accumulator.AddWhole(node);
        }

        return accumulator.Finish();
    }

    internal static JsonObject BuildBody(ChatRequest request)
    {
        var messages = new JsonArray();
        foreach (var m in request.Messages)
            messages.Add(MessageJson(m));
        var body = new JsonObject
        {
            ["model"] = request.Model,
            ["messages"] = messages,
            ["stream"] = true,
            ["stream_options"] = new JsonObject { ["include_usage"] = true },
        };
        if (request.Temperature is { } t)
            body["temperature"] = t;
        if (request.MaxTokens is { } max)
            body["max_tokens"] = max;
        if (!string.IsNullOrEmpty(request.ReasoningEffort))
            body["reasoning_effort"] = request.ReasoningEffort;
        if (request.Tools is { Count: > 0 } tools)
        {
            var array = new JsonArray();
            foreach (var tool in tools)
            {
                array.Add(new JsonObject
                {
                    ["type"] = "function",
                    ["function"] = new JsonObject
                    {
                        ["name"] = tool.Name,
                        ["description"] = tool.Description,
                        ["parameters"] = tool.InputSchema.DeepClone(),
                    },
                });
            }

            body["tools"] = array;
        }

        return body;
    }

    internal static JsonObject MessageJson(ChatMessage m)
    {
        var o = new JsonObject { ["role"] = m.Role, ["content"] = m.Content ?? "" };
        if (m.ToolCalls is { Count: > 0 } calls)
        {
            var array = new JsonArray();
            foreach (var c in calls)
            {
                array.Add(new JsonObject
                {
                    ["id"] = c.Id,
                    ["type"] = "function",
                    ["function"] = new JsonObject { ["name"] = c.Name, ["arguments"] = c.Arguments },
                });
            }

            o["tool_calls"] = array;
        }

        if (m.ToolCallId != null)
            o["tool_call_id"] = m.ToolCallId;
        return o;
    }

    internal static bool LooksLikeToolsUnsupported(string error)
    {
        var e = error.ToLowerInvariant();
        return e.Contains("does not support tools") || e.Contains("tools are not supported") || e.Contains("tool use is not supported")
               || e.Contains("unsupported") && e.Contains("tool") || e.Contains("unknown field") && e.Contains("tools");
    }

    private static string ErrorText(string body)
    {
        try
        {
            var node = JsonNode.Parse(body);
            var error = node?["error"];
            if (error is JsonValue v)
                return v.ToString();
            if (error?["message"] is { } msg)
                return msg.ToString();
        }
        catch (JsonException)
        {
        }

        return body.Trim();
    }

    private static string Truncate(string s, int max) => s.Length <= max ? s : s[..max] + "…";

    /// <summary>Accumulates streamed deltas (content, reasoning, indexed tool-call fragments) and timings.</summary>
    internal sealed class StreamAccumulator(Stopwatch sw, Action<StreamDelta>? onDelta)
    {
        private readonly StringBuilder content = new();
        private readonly StringBuilder reasoning = new();
        private readonly SortedDictionary<int, (string? Id, StringBuilder Name, StringBuilder Args)> calls = [];
        private double? firstDeltaMs;
        private double lastDeltaMs;
        private int deltas;
        private int? promptTokens;
        private int? completionTokens;
        private string? finishReason;

        public void AddChunk(JsonNode? chunk)
        {
            if (chunk == null)
                return;
            ReadUsage(chunk["usage"]);
            if (chunk["choices"] is not JsonArray choices || choices.Count == 0)
                return;
            var choice = choices[0];
            if (choice?["finish_reason"] is JsonValue fr && fr.TryGetValue<string>(out var reason))
                finishReason = reason;
            var delta = choice?["delta"];
            if (delta == null)
                return;
            var before = deltas;

            if (Str(delta["content"]) is { Length: > 0 } text)
            {
                Mark();
                content.Append(text);
                onDelta?.Invoke(new StreamDelta(DeltaKind.Text, text));
            }

            var think = Str(delta["reasoning_content"]) ?? Str(delta["reasoning"]);
            if (think is { Length: > 0 })
            {
                Mark();
                reasoning.Append(think);
                onDelta?.Invoke(new StreamDelta(DeltaKind.Reasoning, think));
            }

            if (delta["tool_calls"] is JsonArray toolCalls)
            {
                Mark();
                foreach (var tc in toolCalls)
                {
                    if (tc == null)
                        continue;
                    var index = tc["index"] is JsonValue iv && iv.TryGetValue<int>(out var i) ? i : calls.Count;
                    if (!calls.TryGetValue(index, out var entry))
                        entry = (null, new StringBuilder(), new StringBuilder());
                    var id = Str(tc["id"]);
                    if (!string.IsNullOrEmpty(id))
                        entry.Id = id;
                    var fn = tc["function"];
                    if (Str(fn?["name"]) is { Length: > 0 } name)
                    {
                        entry.Name.Append(name);
                        onDelta?.Invoke(new StreamDelta(DeltaKind.ToolCall, name));
                    }

                    // Some servers send arguments as an object rather than a string.
                    var args = fn?["arguments"];
                    if (args is JsonValue av && av.TryGetValue<string>(out var argText))
                        entry.Args.Append(argText);
                    else if (args is JsonObject ao)
                        entry.Args.Append(ao.ToJsonString());
                    calls[index] = entry;
                }
            }

            // One delta per chunk, however many fields it carried.
            if (deltas > before + 1)
                deltas = before + 1;
        }

        public void AddWhole(JsonNode? node)
        {
            ReadUsage(node?["usage"]);
            var message = node?["choices"]?[0]?["message"];
            finishReason = Str(node?["choices"]?[0]?["finish_reason"]);
            if (message == null)
                return;
            Mark();
            if (Str(message["content"]) is { } text)
            {
                content.Append(text);
                onDelta?.Invoke(new StreamDelta(DeltaKind.Text, text));
            }

            if ((Str(message["reasoning_content"]) ?? Str(message["reasoning"])) is { } think)
                reasoning.Append(think);
            if (message["tool_calls"] is JsonArray toolCalls)
            {
                var i = 0;
                foreach (var tc in toolCalls)
                {
                    var args = tc?["function"]?["arguments"];
                    var argText = args is JsonValue v && v.TryGetValue<string>(out var s) ? s : args?.ToJsonString() ?? "";
                    calls[i++] = (Str(tc?["id"]), new StringBuilder(Str(tc?["function"]?["name"]) ?? ""), new StringBuilder(argText));
                }
            }
        }

        public ChatResult Finish()
        {
            var toolCalls = calls.Select((kv, n) => new ToolCall(
                string.IsNullOrEmpty(kv.Value.Id) ? $"call_{n}" : kv.Value.Id!,
                kv.Value.Name.ToString(),
                kv.Value.Args.Length == 0 ? "{}" : kv.Value.Args.ToString())).ToList();
            // Generation time runs from the first delta to the end of the stream (benchmark/README.md, tokens_per_s).
            var end = Math.Max(lastDeltaMs, sw.Elapsed.TotalMilliseconds);
            var genMs = firstDeltaMs is { } first ? Math.Max(0, end - first) : 0;
            return new ChatResult(content.ToString(), reasoning.ToString(), toolCalls, finishReason, promptTokens, completionTokens, deltas, firstDeltaMs, genMs);
        }

        private void Mark()
        {
            var now = sw.Elapsed.TotalMilliseconds;
            firstDeltaMs ??= now;
            lastDeltaMs = now;
            deltas++;
        }

        private void ReadUsage(JsonNode? usage)
        {
            if (usage == null)
                return;
            if (usage["prompt_tokens"] is JsonValue p && p.TryGetValue<int>(out var pt))
                promptTokens = pt;
            if (usage["completion_tokens"] is JsonValue c && c.TryGetValue<int>(out var ctok))
                completionTokens = ctok;
        }

        private static string? Str(JsonNode? node) => node is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;
    }
}

/// <summary>Minimal server-sent events reader: yields the joined <c>data:</c> lines of each event.</summary>
public static class Sse
{
    public static async IAsyncEnumerable<string> ReadDataAsync(Stream stream, [System.Runtime.CompilerServices.EnumeratorCancellation] CancellationToken ct)
    {
        using var reader = new StreamReader(stream, Encoding.UTF8);
        var data = new StringBuilder();
        while (true)
        {
            var line = await reader.ReadLineAsync(ct).ConfigureAwait(false);
            if (line == null)
            {
                if (data.Length > 0)
                    yield return data.ToString();
                yield break;
            }

            if (line.Length == 0)
            {
                if (data.Length > 0)
                {
                    yield return data.ToString();
                    data.Clear();
                }

                continue;
            }

            if (line.StartsWith("data:", StringComparison.Ordinal))
            {
                var value = line.AsSpan(5);
                if (value.Length > 0 && value[0] == ' ')
                    value = value[1..];
                if (data.Length > 0)
                    data.Append('\n');
                data.Append(value);
            }
        }
    }
}
