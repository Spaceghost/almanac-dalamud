using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Almanac.Core.Llm;

/// <summary>
/// Tool calling for models without native tool calls: tools are described in the system prompt and a reply that is
/// only a JSON object <c>{"tool": ..., "arguments": {...}}</c> is a call. The wording is part of the benchmark
/// contract (benchmark/README.md), so do not change it without a new suite version.
/// </summary>
public static class PromptedTools
{
    public const string ResultPrefix = "Tool result: ";

    public static string SystemSuffix(IEnumerable<ToolDef> tools)
    {
        var sb = new StringBuilder();
        sb.Append("You can call these tools. To call one, reply with only a JSON object on one line:\n");
        sb.Append("{\"tool\": \"<name>\", \"arguments\": {...}}\n");
        sb.Append("After a call you will get its result as the next user message, starting \"Tool result:\". When you have the answer, reply normally without JSON.\n");
        foreach (var t in tools)
            sb.Append(t.Name).Append(": ").Append(t.Description).Append(" Arguments (JSON Schema): ").Append(t.InputSchema.ToJsonString()).Append('\n');
        return sb.ToString().TrimEnd('\n');
    }

    /// <summary>Parses a prompted tool call from a whole reply, or returns null when the reply is an answer.</summary>
    public static ToolCall? TryParse(string reply, string id)
    {
        var text = StripFence(reply.Trim());
        if (!text.StartsWith('{'))
            return null;
        try
        {
            if (JsonNode.Parse(text) is not JsonObject o || o["tool"] is not JsonValue tv || !tv.TryGetValue<string>(out var name))
                return null;
            var args = o["arguments"] is JsonObject a ? a.ToJsonString() : o["arguments"] is JsonValue av && av.TryGetValue<string>(out var s) ? s : "{}";
            return new ToolCall(id, name, args);
        }
        catch (JsonException)
        {
            return null;
        }
    }

    /// <summary>Removes one surrounding ``` or ```json fence.</summary>
    public static string StripFence(string text)
    {
        if (!text.StartsWith("```", StringComparison.Ordinal) || !text.EndsWith("```", StringComparison.Ordinal) || text.Length < 6)
            return text;
        var inner = text[3..^3];
        var newline = inner.IndexOf('\n');
        if (newline >= 0 && inner[..newline].Trim().All(char.IsLetter))
            inner = inner[(newline + 1)..];
        else if (inner.StartsWith("json", StringComparison.OrdinalIgnoreCase))
            inner = inner[4..];
        return inner.Trim();
    }
}
