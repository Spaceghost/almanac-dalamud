using System.Text;
using Almanac.Core.Llm;
using Almanac.Core.Storage;

namespace Almanac.Core.Agent;

public enum LineKind
{
    User,
    Assistant,
    Tool,
    Notice,
    Error,
}

/// <summary>One line of the chat transcript as the UI shows it.</summary>
public sealed record ChatLine(LineKind Kind, string Text, int? Seq = null);

/// <summary>
/// The in-game chat's state, independent of ImGui: the open thread, its transcript, the streaming reply and the
/// running agent. The UI thread reads <see cref="Snapshot"/>; <see cref="SendAsync"/> runs on the thread pool and
/// persists every message to SQLite as it happens, so a crash or reload loses at most the reply being streamed.
/// </summary>
public sealed class ChatSession(AlmanacStore store, Func<AgentLoop> newLoop, Func<string> systemPrompt)
{
    public const int HistoryWindow = 40;

    public const string DefaultSystemPrompt =
        "You are Almanac, a helpful assistant inside FINAL FANTASY XIV. The player talks to you from an in-game window. " +
        "You can read the game through XivMcp tools: use them whenever the answer depends on the player's current state, location, " +
        "inventory or game data, and do not guess such facts. Tools that change the game (teleport, slash commands, chat others can see) " +
        "ask the player to approve them in game first; if one is denied or times out, say so and do not retry it. " +
        "Keep answers short and concrete: names, numbers, coordinates.";

    private readonly Lock gate = new();
    private readonly List<ChatLine> lines = [];
    private readonly StringBuilder streaming = new();
    private CancellationTokenSource? running;

    public ThreadInfo? Thread { get; private set; }

    public bool Busy { get; private set; }

    public string? Status { get; private set; }

    /// <summary>Transcript lines plus the reply being streamed (as a final Assistant line).</summary>
    public IReadOnlyList<ChatLine> Snapshot()
    {
        lock (gate)
        {
            var copy = new List<ChatLine>(lines);
            if (streaming.Length > 0)
                copy.Add(new ChatLine(LineKind.Assistant, streaming.ToString()));
            return copy;
        }
    }

    public void Open(string threadId)
    {
        var info = store.ListThreads().FirstOrDefault(t => t.Id == threadId);
        if (info == null || Busy)
            return;
        Thread = info;
        Reload();
    }

    public void New()
    {
        if (Busy)
            return;
        Thread = null;
        lock (gate)
        {
            lines.Clear();
            streaming.Clear();
        }
    }

    /// <summary>Branches the open thread after message <paramref name="seq"/> and opens the branch.</summary>
    public void Fork(int seq)
    {
        if (Thread == null || Busy)
            return;
        Thread = store.ForkThread(Thread.Id, seq, $"{Thread.Title} (branch)");
        Reload();
    }

    public void Cancel() => running?.Cancel();

    public async Task SendAsync(string text)
    {
        text = text.Trim();
        if (text.Length == 0 || Busy)
            return;
        Busy = true;
        Status = "Thinking…";
        running = new CancellationTokenSource();
        try
        {
            Thread ??= store.CreateThread(Title(text));
            var threadId = Thread.Id;
            store.AppendMessage(threadId, ChatMessage.User(text));
            Reload();

            var history = new List<ChatMessage> { ChatMessage.System(systemPrompt()) };
            history.AddRange(Window(store.Messages(threadId).Select(m => m.Message).ToList()));
            var persisted = new Cursor(history.Count);

            var loop = newLoop();
            var result = await loop.RunAsync(history, e => OnEvent(e, history, persisted, threadId), running.Token).ConfigureAwait(false);
            Persist(history, persisted, threadId);
            if (result.FinalAnswer == null)
                AddLine(LineKind.Notice, $"Stopped after {result.Turns.Count} steps without a final answer.");
            Status = null;
        }
        catch (OperationCanceledException)
        {
            Status = null;
            AddLine(LineKind.Notice, "Stopped.");
        }
        catch (Exception ex)
        {
            Status = null;
            AddLine(LineKind.Error, ex is ChatHttpException or HttpRequestException ? $"Model server: {ex.Message}" : ex.Message);
        }
        finally
        {
            lock (gate)
                streaming.Clear();
            Busy = false;
            running?.Dispose();
            running = null;
            if (Thread != null)
                Reload(keepExtra: true);
        }
    }

    private void OnEvent(AgentEvent e, List<ChatMessage> history, Cursor persisted, string threadId)
    {
        switch (e)
        {
            case TextDeltaEvent t:
                lock (gate)
                    streaming.Append(t.Text);
                Status = null;
                break;
            case ReasoningDeltaEvent:
                Status = "Thinking…";
                break;
            case ToolStartedEvent s:
                // The assistant message with the calls is in history now: save it before the (possibly slow) call.
                Persist(history, persisted, threadId);
                lock (gate)
                    streaming.Clear();
                Status = $"Using {s.Name}…";
                AddLine(LineKind.Tool, $"→ {s.Name} {Shorten(s.Arguments, 120)}");
                break;
            case ToolFinishedEvent f:
                Persist(history, persisted, threadId);
                AddLine(LineKind.Tool, $"{(f.Ok ? "←" : "✗")} {f.Name} ({f.Elapsed.TotalSeconds:0.0}s) {Shorten(f.Preview, 120)}");
                Status = "Thinking…";
                break;
            case ModeChangedEvent m:
                AddLine(LineKind.Notice, $"This model does not take native tool calls; switched to prompted tool calling. ({Shorten(m.Reason, 80)})");
                break;
        }
    }

    /// <summary>How far into the history the store has been written.</summary>
    private sealed class Cursor(int start)
    {
        public int Next = start;
    }

    private void Persist(List<ChatMessage> history, Cursor persisted, string threadId)
    {
        for (; persisted.Next < history.Count; persisted.Next++)
            store.AppendMessage(threadId, history[persisted.Next]);
    }

    private void AddLine(LineKind kind, string text)
    {
        lock (gate)
            lines.Add(new ChatLine(kind, text));
    }

    private void Reload(bool keepExtra = false)
    {
        if (Thread == null)
            return;
        var rebuilt = Render(store.Messages(Thread.Id));
        lock (gate)
        {
            var extras = keepExtra ? lines.Where(l => l.Kind is LineKind.Notice or LineKind.Error && l.Seq == null).ToList() : [];
            lines.Clear();
            lines.AddRange(rebuilt);
            lines.AddRange(extras);
        }
    }

    internal static List<ChatLine> Render(IReadOnlyList<StoredMessage> messages)
    {
        var result = new List<ChatLine>();
        foreach (var m in messages)
        {
            var msg = m.Message;
            switch (msg.Role)
            {
                case "user" when msg.Content?.StartsWith(PromptedTools.ResultPrefix, StringComparison.Ordinal) == true:
                    result.Add(new ChatLine(LineKind.Tool, $"← {Shorten(msg.Content[PromptedTools.ResultPrefix.Length..], 120)}", m.Seq));
                    break;
                case "user":
                    result.Add(new ChatLine(LineKind.User, msg.Content ?? "", m.Seq));
                    break;
                case "assistant":
                    if (msg.ToolCalls is { Count: > 0 } calls)
                    {
                        if (!string.IsNullOrWhiteSpace(msg.Content))
                            result.Add(new ChatLine(LineKind.Assistant, msg.Content!, m.Seq));
                        foreach (var c in calls)
                            result.Add(new ChatLine(LineKind.Tool, $"→ {c.Name} {Shorten(c.Arguments, 120)}", m.Seq));
                    }
                    else if (PromptedTools.TryParse(msg.Content ?? "", "x") is { } prompted)
                    {
                        result.Add(new ChatLine(LineKind.Tool, $"→ {prompted.Name} {Shorten(prompted.Arguments, 120)}", m.Seq));
                    }
                    else
                    {
                        result.Add(new ChatLine(LineKind.Assistant, msg.Content ?? "", m.Seq));
                    }

                    break;
                case "tool":
                    result.Add(new ChatLine(LineKind.Tool, $"← {Shorten(msg.Content ?? "", 120)}", m.Seq));
                    break;
            }
        }

        return result;
    }

    /// <summary>The newest messages that fit the window, never starting with a tool result.</summary>
    internal static List<ChatMessage> Window(List<ChatMessage> messages)
    {
        var start = Math.Max(0, messages.Count - HistoryWindow);
        while (start < messages.Count && messages[start].Role != "user")
            start++;
        return messages.Skip(start).ToList();
    }

    private static string Title(string text)
    {
        var line = text.Split('\n')[0].Trim();
        return line.Length <= 48 ? line : line[..48] + "…";
    }

    private static string Shorten(string text, int max)
    {
        text = text.Replace('\n', ' ');
        return text.Length <= max ? text : text[..max] + "…";
    }
}
