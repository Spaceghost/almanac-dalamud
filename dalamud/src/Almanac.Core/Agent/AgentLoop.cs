using System.Text.Json.Nodes;
using Almanac.Core.Bench;
using Almanac.Core.Llm;
using Almanac.Core.Tools;

namespace Almanac.Core.Agent;

public sealed record AgentOptions
{
    public required string Model { get; init; }

    public ToolCallingMode Mode { get; init; } = ToolCallingMode.Native;

    /// <summary>Model turns per user message.</summary>
    public int MaxSteps { get; init; } = 8;

    public double? Temperature { get; init; }

    public int? MaxTokens { get; init; }

    /// <summary>Passed through as <see cref="ChatRequest.ReasoningEffort"/>.</summary>
    public string? ReasoningEffort { get; init; }

    /// <summary>Switch to <see cref="ToolCallingMode.Prompted"/> when the backend rejects native tools.</summary>
    public bool FallBackToPrompted { get; init; } = true;
}

public abstract record AgentEvent;

public sealed record TextDeltaEvent(string Text) : AgentEvent;

public sealed record ReasoningDeltaEvent(string Text) : AgentEvent;

public sealed record ToolStartedEvent(string Name, string Arguments) : AgentEvent;

public sealed record ToolFinishedEvent(string Name, bool Ok, string Preview, TimeSpan Elapsed) : AgentEvent;

public sealed record TurnEvent(ChatResult Result) : AgentEvent;

public sealed record ModeChangedEvent(ToolCallingMode Mode, string Reason) : AgentEvent;

public sealed record AgentRunResult(
    string? FinalAnswer,
    IReadOnlyList<ChatResult> Turns,
    IReadOnlyList<CallRecord> Calls,
    ToolCallingMode ModeUsed,
    bool HitStepLimit);

/// <summary>
/// The agent loop shared by the in-game chat and the benchmark: stream a reply, run the tool calls it asks for
/// (validated against each tool's schema first), feed the results back, repeat until the model answers without a
/// tool call or the step limit is reached. <paramref name="history"/> is appended to in place.
/// </summary>
public sealed class AgentLoop(IChatBackend chat, IToolSource tools, AgentOptions options)
{
    public ToolCallingMode Mode { get; private set; } = options.Mode;

    public async Task<AgentRunResult> RunAsync(List<ChatMessage> history, Action<AgentEvent>? onEvent, CancellationToken ct)
    {
        IReadOnlyList<ToolDef> offered = [];
        if (Mode != ToolCallingMode.None)
            offered = await tools.ListAsync(ct).ConfigureAwait(false);
        var turns = new List<ChatResult>();
        var calls = new List<CallRecord>();
        var callSeq = 0;

        for (var step = 0; step < options.MaxSteps; step++)
        {
            ct.ThrowIfCancellationRequested();
            ChatResult result;
            try
            {
                result = await chat.CompleteAsync(BuildRequest(history, offered), d => Forward(d, onEvent), ct).ConfigureAwait(false);
            }
            catch (ToolsUnsupportedException ex) when (Mode == ToolCallingMode.Native && options.FallBackToPrompted)
            {
                Mode = ToolCallingMode.Prompted;
                onEvent?.Invoke(new ModeChangedEvent(Mode, ex.Message));
                step--;
                continue;
            }

            turns.Add(result);
            onEvent?.Invoke(new TurnEvent(result));

            var requested = result.ToolCalls.ToList();
            if (Mode == ToolCallingMode.Prompted && requested.Count == 0 && offered.Count > 0
                && PromptedTools.TryParse(result.Content, $"call_{callSeq}") is { } prompted)
                requested.Add(prompted);
            if (requested.Count == 0)
            {
                history.Add(ChatMessage.Assistant(result.Content));
                return new AgentRunResult(result.Content, turns, calls, Mode, false);
            }

            if (Mode == ToolCallingMode.Prompted)
                history.Add(ChatMessage.Assistant(result.Content));
            else
                history.Add(ChatMessage.Assistant(result.Content, requested.Select(c => c with { Id = string.IsNullOrEmpty(c.Id) ? $"call_{callSeq}" : c.Id }).ToList()));

            foreach (var call in requested)
            {
                callSeq++;
                var (valid, reason, args) = Scorer.Validate(offered, call.Name, call.Arguments);
                string text;
                JsonNode? json = null;
                var started = DateTime.UtcNow;
                onEvent?.Invoke(new ToolStartedEvent(call.Name, call.Arguments));
                if (!valid)
                {
                    text = ToolOutcome.Error($"invalid call: {reason}").Text;
                    onEvent?.Invoke(new ToolFinishedEvent(call.Name, false, reason ?? "invalid", DateTime.UtcNow - started));
                }
                else
                {
                    ToolOutcome outcome;
                    try
                    {
                        outcome = await tools.CallAsync(call.Name, args!, ct).ConfigureAwait(false);
                    }
                    catch (Exception ex) when (ex is not OperationCanceledException)
                    {
                        outcome = ToolOutcome.Error(ex.Message);
                    }

                    text = outcome.Text;
                    json = outcome.Json ?? McpToolSource.TryParse(outcome.Text);
                    onEvent?.Invoke(new ToolFinishedEvent(call.Name, !outcome.IsError, Preview(outcome.Text), DateTime.UtcNow - started));
                }

                calls.Add(new CallRecord(call.Name, call.Arguments, valid, reason, json));
                history.Add(Mode == ToolCallingMode.Prompted
                    ? ChatMessage.User(PromptedTools.ResultPrefix + Compact(text))
                    : ChatMessage.Tool(string.IsNullOrEmpty(call.Id) ? $"call_{callSeq - 1}" : call.Id, text));
            }
        }

        return new AgentRunResult(null, turns, calls, Mode, true);
    }

    private ChatRequest BuildRequest(List<ChatMessage> history, IReadOnlyList<ToolDef> offered)
    {
        IReadOnlyList<ChatMessage> messages = history;
        if (Mode == ToolCallingMode.Prompted && offered.Count > 0)
        {
            var suffix = PromptedTools.SystemSuffix(offered);
            var list = history.ToList();
            if (list.Count > 0 && list[0].Role == "system")
                list[0] = list[0] with { Content = $"{list[0].Content}\n\n{suffix}" };
            else
                list.Insert(0, ChatMessage.System(suffix));
            messages = list;
        }

        return new ChatRequest
        {
            Model = options.Model,
            Messages = messages,
            Tools = Mode == ToolCallingMode.Native && offered.Count > 0 ? offered : null,
            Temperature = options.Temperature,
            MaxTokens = options.MaxTokens,
            ReasoningEffort = options.ReasoningEffort,
        };
    }

    private static void Forward(StreamDelta delta, Action<AgentEvent>? onEvent)
    {
        if (onEvent == null)
            return;
        if (delta.Kind == DeltaKind.Text)
            onEvent(new TextDeltaEvent(delta.Text));
        else if (delta.Kind == DeltaKind.Reasoning)
            onEvent(new ReasoningDeltaEvent(delta.Text));
    }

    private static string Compact(string text)
    {
        var node = McpToolSource.TryParse(text);
        return node?.ToJsonString() ?? text;
    }

    private static string Preview(string text) => text.Length <= 160 ? text : text[..160] + "…";
}
