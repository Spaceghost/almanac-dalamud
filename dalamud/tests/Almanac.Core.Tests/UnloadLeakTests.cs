using System.Runtime.CompilerServices;
using Almanac.Core.Agent;
using Almanac.Core.Bench;
using Almanac.Core.Diagnostics;
using Almanac.Core.Llm;
using Almanac.Core.Mcp;
using Almanac.Core.Storage;
using Almanac.Core.Tools;

namespace Almanac.Core.Tests;

/// <summary>
/// Builds the part of the plugin's service graph that does not need Dalamud, uses it, tears it
/// down, and asserts nothing is left over.
///
/// WHAT THIS COVERS: Almanac.Core -- the SQLite store, the chat session and its cancellation
/// token source, the MCP client and tool source, the agent loop, the benchmark runner and the
/// VRAM sampler. These are the objects that own a handle, a thread or a token source.
///
/// WHAT THIS DOES NOT COVER, and cannot: Dalamud cannot be loaded outside the game, so the
/// Almanac.Plugin layer -- Plugin, Engine, XivMcpLink and the four ImGui windows -- is never
/// constructed here. Their registrations (the /almanac command, the WindowSystem, the UiBuilder
/// events, the IPC provider and subscriber) are checked by the source audit in
/// ReloadLeakAuditTests and by the CA2213/CA1001 build errors, and observed at runtime by the
/// opt-in leak watch. Nothing in this file should be read as evidence that a real in-game unload
/// is clean; docs/QUALITY.md says what does provide that evidence.
///
/// These tests take the collection lock because LiveObjects is process-wide.
/// </summary>
[Collection("live-objects")]
public class UnloadLeakTests
{
    [Fact]
    public async Task The_core_service_graph_leaves_no_live_objects_behind()
    {
        LiveObjects.Reset();

        var store = AlmanacStore.InMemory();
        var model = new ScriptedModel();
        model.Script["hello"] = [new ScriptedModel.Turn("hi")];
        using var handler = new FakeHandler(model.Respond);
        using var http = new HttpClient(handler);

        var mcp = new McpHttpClient(http, new Uri("http://127.0.0.1:1/mcp"), null, "almanac-dalamud", "0.0.0");
        var tools = new McpToolSource(mcp);
        var session = new ChatSession(
            store,
            () => new AgentLoop(new ChatClient(http, "http://m/v1", null), new ToolHub([tools]), new AgentOptions { Model = "m" }),
            () => ChatSession.DefaultSystemPrompt);

        await session.SendAsync("hello");

        Assert.Equal(1, LiveObjects.Count(LiveObjects.Kinds.Store));
        Assert.Equal(1, LiveObjects.Count(LiveObjects.Kinds.ChatSession));
        Assert.Equal(1, LiveObjects.Count(LiveObjects.Kinds.McpClient));
        Assert.Equal(1, LiveObjects.Count(LiveObjects.Kinds.McpToolSource));

        session.Dispose();
        tools.Dispose();
        store.Dispose();

        Assert.True(
            LiveObjects.Outstanding().Count == 0,
            "After disposing the Core graph these counters are still non-zero: " + LiveObjects.Describe());
    }

    [Fact]
    public async Task A_cancelled_benchmark_run_still_releases_its_vram_sampler()
    {
        LiveObjects.Reset();
        using var handler = new FakeHandler((_, _) => FakeHandler.Json("""{"choices":[{"message":{"content":"x"},"finish_reason":"stop"}]}"""));
        using var http = new HttpClient(handler);
        using var cts = new CancellationTokenSource();
        await cts.CancelAsync();

        var runner = new BenchRunner(new ChatClient(http, "http://m/v1", null), Suite.Bundled()) { Vram = new NeverProbe() };
        await Assert.ThrowsAnyAsync<OperationCanceledException>(
            () => runner.RunAsync("m", BenchMode.Mock, null, null, cts.Token));

        Assert.Equal(0, LiveObjects.Count(LiveObjects.Kinds.VramSampler));
    }

    /// <summary>
    /// Counters only prove the bookkeeping. This proves the graph is actually unreachable: nothing
    /// static holds a reference to the store or the session once they are disposed and dropped.
    /// </summary>
    [Fact]
    public void The_disposed_graph_becomes_unreachable()
    {
        LiveObjects.Reset();
        var (store, session) = BuildAndDrop();

        for (var i = 0; i < 5 && (store.IsAlive || session.IsAlive); i++)
        {
            GC.Collect(2, GCCollectionMode.Forced, blocking: true);
            GC.WaitForPendingFinalizers();
        }

        Assert.False(store.IsAlive, "The AlmanacStore survived disposal: something still holds a reference to it.");
        Assert.False(session.IsAlive, "The ChatSession survived disposal: something still holds a reference to it.");
        Assert.True(LiveObjects.Outstanding().Count == 0, LiveObjects.Describe());
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static (WeakReference Store, WeakReference Session) BuildAndDrop()
    {
        var store = AlmanacStore.InMemory();
        var session = new ChatSession(store, () => throw new InvalidOperationException(), () => "");
        var refs = (new WeakReference(store), new WeakReference(session));
        session.Dispose();
        store.Dispose();
        return refs;
    }

    private sealed class NeverProbe : IVramProbe
    {
        public Task<int?> ReadUsedMbAsync(CancellationToken ct) => Task.FromResult<int?>(null);
    }
}

/// <summary>LiveObjects is process-wide, so the tests that reset it must not run in parallel.</summary>
[CollectionDefinition("live-objects", DisableParallelization = true)]
public class LiveObjectsTestGroup;
