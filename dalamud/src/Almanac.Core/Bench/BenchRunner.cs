using System.Diagnostics;
using System.Text.Json.Nodes;
using Almanac.Core.Agent;
using Almanac.Core.Llm;
using Almanac.Core.Tools;

namespace Almanac.Core.Bench;

public enum BenchMode
{
    Mock,
    Live,
}

public sealed record TaskRun(SuiteTask Task, TaskScore Score, AgentRunResult? Agent, double? TtftMs, double? TokensPerSecond, int OutputTokens, double DurationMs, string? Answer);

public sealed record BenchRun(
    Suite Suite,
    string Model,
    BenchMode Mode,
    string ToolCalling,
    IReadOnlyList<TaskRun> Tasks,
    double Score,
    double SuccessRate,
    double ToolCallValidity,
    double? Quality,
    double TokensPerSecond,
    double TtftMs,
    int? PeakVramMb,
    double TotalSeconds);

public sealed record BenchProgress(int Done, int Total, string? CurrentTask, TaskRun? Finished);

/// <summary>Runs a suite against one model (benchmark/README.md, "Running a task" and "Scoring").</summary>
public sealed class BenchRunner(IChatBackend chat, Suite suite)
{
    public TimeSpan TaskTimeout { get; init; } = TimeSpan.FromMinutes(3);

    /// <summary>Live mode: XivMcp (calls go there, but the suite's own tool definitions are what the model sees).</summary>
    public IToolSource? LiveTools { get; init; }

    public IVramProbe? Vram { get; init; }

    public ToolCallingMode ToolCalling { get; init; } = ToolCallingMode.Native;

    public async Task<BenchRun> RunAsync(string model, BenchMode mode, IReadOnlyCollection<string>? onlyTasks, IProgress<BenchProgress>? progress, CancellationToken ct)
    {
        if (mode == BenchMode.Live && LiveTools == null)
            throw new InvalidOperationException("Live mode needs XivMcp.");
        var tasks = suite.Tasks.Where(t => onlyTasks == null || onlyTasks.Count == 0 || onlyTasks.Contains(t.Id)).ToList();

        await WarmUpAsync(model, ct).ConfigureAwait(false);

        var sampler = Vram == null ? null : new VramSampler(Vram, TimeSpan.FromSeconds(1));
        try
        {
            sampler?.Start();
            var wall = Stopwatch.StartNew();
            var runs = new List<TaskRun>();
            var mode2 = ToolCalling;
            foreach (var task in tasks)
            {
                progress?.Report(new BenchProgress(runs.Count, tasks.Count, task.Id, null));
                var run = await RunTaskAsync(task, model, mode, mode2, ct).ConfigureAwait(false);
                if (run.Agent?.ModeUsed == ToolCallingMode.Prompted)
                    mode2 = ToolCallingMode.Prompted; // the backend rejected native tools once; don't retry every task
                runs.Add(run);
                progress?.Report(new BenchProgress(runs.Count, tasks.Count, null, run));
            }

            wall.Stop();
            int? peak = sampler == null ? null : await sampler.StopAsync().ConfigureAwait(false);
            return Summarize(suite, model, mode, runs, peak, wall.Elapsed.TotalSeconds, mode2);
        }
        finally
        {
            // The sampler owns a CancellationTokenSource and a polling task; a cancelled or failed
            // run must still let go of both.
            if (sampler != null)
                await sampler.DisposeAsync().ConfigureAwait(false);
        }
    }

    internal static BenchRun Summarize(Suite suite, string model, BenchMode mode, IReadOnlyList<TaskRun> runs, int? peak, double totalSeconds, ToolCallingMode modeUsed)
    {
        var allCalls = runs.Sum(r => r.Score.TotalCalls);
        var validCalls = runs.Sum(r => r.Score.ValidCalls);
        var answers = runs.Where(r => r.Score.AnswerScore != null).Select(r => r.Score.AnswerScore!.Value).ToList();
        var expectingCalls = runs.Where(r => r.Task.Expect["calls"] is JsonArray { Count: > 0 }).ToList();
        var toolCalling = modeUsed == ToolCallingMode.Prompted ? "prompted"
            : expectingCalls.Count > 0 && expectingCalls.All(r => r.Score.TotalCalls == 0) ? "none"
            : "native";
        return new BenchRun(
            suite,
            model,
            mode,
            toolCalling,
            runs,
            Math.Round(100 * (runs.Count == 0 ? 0 : runs.Average(r => r.Score.Score)), 1),
            runs.Count == 0 ? 0 : (double)runs.Count(r => r.Score.Success) / runs.Count,
            allCalls == 0 ? 1 : (double)validCalls / allCalls,
            answers.Count == 0 ? null : answers.Average(),
            Median(runs.Select(r => r.TokensPerSecond).OfType<double>()),
            Median(runs.Select(r => r.TtftMs).OfType<double>()),
            peak,
            totalSeconds);
    }

    private async Task<TaskRun> RunTaskAsync(SuiteTask task, string model, BenchMode mode, ToolCallingMode toolMode, CancellationToken ct)
    {
        var offered = suite.ToolsFor(task);
        IToolSource source = mode == BenchMode.Mock ? new FixtureToolSource(suite, offered) : new OfferedToolSource(LiveTools!, offered);
        var loop = new AgentLoop(chat, source, new AgentOptions
        {
            Model = model,
            Mode = toolMode,
            MaxSteps = task.MaxSteps,
            Temperature = suite.Temperature,
            MaxTokens = suite.MaxTokens,
        });
        var history = new List<ChatMessage> { ChatMessage.System(suite.SystemPrompt), ChatMessage.User(task.Prompt) };
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(TaskTimeout);
        var sw = Stopwatch.StartNew();
        AgentRunResult? result = null;
        string? transportError = null;
        try
        {
            result = await loop.RunAsync(history, null, timeout.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            transportError = "timeout";
        }
        catch (HttpRequestException)
        {
            transportError = "http";
        }
        catch (ChatHttpException)
        {
            transportError = "http";
        }

        sw.Stop();
        IReadOnlyList<CallRecord> calls = result?.Calls ?? [];
        var score = Scorer.Score(task, suite.ToolWeight, suite.AnswerWeight, mode == BenchMode.Live, calls, result?.FinalAnswer, transportError);
        IReadOnlyList<ChatResult> turns = result?.Turns ?? [];
        var outputTokens = turns.Sum(t => t.OutputTokens);
        var genMs = turns.Sum(t => t.GenerationMs);
        double? tps = genMs > 0 ? outputTokens / (genMs / 1000.0) : null;
        return new TaskRun(task, score, result, turns.FirstOrDefault()?.TtftMs, tps, outputTokens, sw.Elapsed.TotalMilliseconds, result?.FinalAnswer);
    }

    private async Task WarmUpAsync(string model, CancellationToken ct)
    {
        try
        {
            await chat.CompleteAsync(new ChatRequest { Model = model, Messages = [ChatMessage.User("Reply with OK")], MaxTokens = 8, Temperature = 0 }, null, ct).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is ChatHttpException or HttpRequestException)
        {
            // The first task reports the error properly.
        }
    }

    internal static double Median(IEnumerable<double> values)
    {
        var sorted = values.OrderBy(v => v).ToList();
        if (sorted.Count == 0)
            return 0;
        var mid = sorted.Count / 2;
        return sorted.Count % 2 == 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
    }

    /// <summary>Offers the suite's tool definitions but forwards calls to the live source.</summary>
    private sealed class OfferedToolSource(IToolSource inner, IReadOnlyList<ToolDef> offered) : IToolSource
    {
        public string Id => inner.Id;

        public Task<IReadOnlyList<ToolDef>> ListAsync(CancellationToken ct) => Task.FromResult(offered);

        public Task<ToolOutcome> CallAsync(string name, JsonObject arguments, CancellationToken ct) => inner.CallAsync(name, arguments, ct);
    }
}
