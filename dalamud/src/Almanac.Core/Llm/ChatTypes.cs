using System.Text.Json.Nodes;

namespace Almanac.Core.Llm;

/// <summary>One chat message in OpenAI Chat Completions terms.</summary>
public sealed record ChatMessage(string Role, string? Content, IReadOnlyList<ToolCall>? ToolCalls = null, string? ToolCallId = null)
{
    public static ChatMessage System(string text) => new("system", text);

    public static ChatMessage User(string text) => new("user", text);

    public static ChatMessage Assistant(string? text, IReadOnlyList<ToolCall>? calls = null) => new("assistant", text, calls is { Count: > 0 } ? calls : null);

    public static ChatMessage Tool(string callId, string text) => new("tool", text, null, callId);
}

/// <summary>A tool call as the model emitted it. <see cref="Arguments"/> is the raw JSON text.</summary>
public sealed record ToolCall(string Id, string Name, string Arguments);

/// <summary>A tool offered to the model (MCP shape: name, description, JSON Schema input).</summary>
public sealed record ToolDef(string Name, string Description, JsonObject InputSchema);

/// <summary>How the model is asked to call tools.</summary>
public enum ToolCallingMode
{
    /// <summary>OpenAI <c>tools</c> / <c>tool_calls</c>.</summary>
    Native,

    /// <summary>Tools described in the system prompt; the model answers with a JSON object (see <see cref="PromptedTools"/>).</summary>
    Prompted,

    /// <summary>No tools at all.</summary>
    None,
}

public sealed class ChatRequest
{
    public required string Model { get; init; }

    public required IReadOnlyList<ChatMessage> Messages { get; init; }

    public IReadOnlyList<ToolDef>? Tools { get; init; }

    public double? Temperature { get; init; }

    public int? MaxTokens { get; init; }
}

public enum DeltaKind
{
    Text,
    Reasoning,
    ToolCall,
}

public readonly record struct StreamDelta(DeltaKind Kind, string Text);

/// <summary>Result of one streamed completion, with the timings the benchmark needs.</summary>
public sealed record ChatResult(
    string Content,
    string Reasoning,
    IReadOnlyList<ToolCall> ToolCalls,
    string? FinishReason,
    int? PromptTokens,
    int? CompletionTokens,
    int Deltas,
    double? TtftMs,
    double GenerationMs)
{
    /// <summary>Output tokens: the backend's usage when it reports one, else one per streamed delta.</summary>
    public int OutputTokens => CompletionTokens ?? Deltas;
}

/// <summary>The backend answered with an HTTP error.</summary>
public class ChatHttpException(int status, string message) : Exception(message)
{
    public int Status { get; } = status;
}

/// <summary>The backend or model rejected the <c>tools</c> field; retry with <see cref="ToolCallingMode.Prompted"/>.</summary>
public sealed class ToolsUnsupportedException(int status, string message) : ChatHttpException(status, message);

public interface IChatBackend
{
    Task<ChatResult> CompleteAsync(ChatRequest request, Action<StreamDelta>? onDelta, CancellationToken ct);
}
