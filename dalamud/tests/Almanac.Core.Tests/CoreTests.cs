using System.Net;
using System.Text.Json.Nodes;
using Almanac.Core.Agent;
using Almanac.Core.Bench;
using Almanac.Core.Llm;
using Almanac.Core.Mcp;
using Almanac.Core.Setup;
using Almanac.Core.Storage;
using Almanac.Core.Tools;

namespace Almanac.Core.Tests;

public sealed class SetupTests
{
    private static CancellationToken Ct => TestContext.Current.CancellationToken;

    [Fact]
    public async Task DetectsOllamaWithModelDetails()
    {
        var handler = new FakeHandler((req, _) => req.RequestUri!.AbsolutePath switch
        {
            "/v1/models" => FakeHandler.Json("""{"data":[{"id":"qwen3:8b"}]}"""),
            "/api/version" => FakeHandler.Json("""{"version":"0.12.3"}"""),
            "/api/tags" => FakeHandler.Json("""{"models":[{"name":"qwen3:8b","size":5200000000,"details":{"quantization_level":"Q4_K_M","parameter_size":"8.2B","family":"qwen3"}}]}"""),
            _ => new HttpResponseMessage(HttpStatusCode.NotFound),
        });
        var server = await new ServerDetector(new HttpClient(handler)).ProbeAsync("127.0.0.1:11434", null, Ct);
        Assert.NotNull(server);
        Assert.Equal(BackendKinds.Ollama, server!.Kind);
        Assert.Equal("http://127.0.0.1:11434/v1", server.BaseUrl);
        Assert.Equal("0.12.3", server.Version);
        var model = Assert.Single(server.Models);
        Assert.Equal("Q4_K_M", model.Quant);
    }

    [Fact]
    public async Task DetectsLmStudioAndLlamaCppAndGenericServers()
    {
        HttpClient Client(Func<string, HttpResponseMessage?> map) =>
            new(new FakeHandler((req, _) => map(req.RequestUri!.AbsolutePath) ?? new HttpResponseMessage(HttpStatusCode.NotFound)));
        var models = FakeHandler.Json("""{"data":[{"id":"m"}]}""");

        var lm = await new ServerDetector(Client(p => p switch
        {
            "/v1/models" => FakeHandler.Json("""{"data":[{"id":"qwen/qwen3-8b"}]}"""),
            "/api/v0/models" => FakeHandler.Json("""{"data":[{"id":"qwen/qwen3-8b","type":"llm","quantization":"Q4_K_M"}]}"""),
            _ => null,
        })).ProbeAsync("http://127.0.0.1:1234/v1", null, Ct);
        Assert.Equal(BackendKinds.LmStudio, lm!.Kind);

        var llama = await new ServerDetector(Client(p => p switch
        {
            "/v1/models" => FakeHandler.Json("""{"data":[{"id":"x.gguf"}]}"""),
            "/props" => FakeHandler.Json("""{"default_generation_settings":{},"build_info":"b6000"}"""),
            _ => null,
        })).ProbeAsync("http://127.0.0.1:8080", null, Ct);
        Assert.Equal(BackendKinds.LlamaCpp, llama!.Kind);

        var generic = await new ServerDetector(Client(p => p == "/v1/models" ? FakeHandler.Json("""{"data":[]}""") : null)).ProbeAsync("http://127.0.0.1:8000/v1", null, Ct);
        Assert.Equal(BackendKinds.OpenAiCompatible, generic!.Kind);

        Assert.Null(await new ServerDetector(Client(_ => null)).ProbeAsync("http://127.0.0.1:9/v1", null, Ct));
    }

    [Theory]
    [InlineData(4096, true, 3072, 1024, "tiny")]
    [InlineData(8192, true, 3072, 5120, "small")]
    [InlineData(12288, true, 3072, 9216, "mid")]
    [InlineData(12288, false, 3072, 12288, "upper")]
    [InlineData(24576, true, 3072, 21504, "large")]
    [InlineData(49152, false, 0, 49152, "xl")]
    public void BudgetPicksTheTier(int vram, bool sameGpu, int reserve, int budget, string tier)
    {
        var recs = Recommendations.Bundled();
        Assert.Equal(budget, Recommendations.Budget(vram, sameGpu, reserve));
        Assert.Equal(tier, recs.TierFor(budget)!.Id);
        Assert.All(recs.For(budget), m => Assert.True(m.VramMb == null || m.VramMb <= budget));
    }

    [Fact]
    public async Task RecommendationsFallBackToCacheThenBundled()
    {
        string? cache = null;
        var offline = new HttpClient(new FakeHandler((_, _) => throw new HttpRequestException("offline")));
        var source = new RecommendationSource(offline, "https://x/recommendations.json", () => cache, v => cache = v);
        Assert.Equal("bundled", (await source.LoadAsync(Ct)).Source);

        var online = new HttpClient(new FakeHandler((_, _) => FakeHandler.Json("""{"schema_version":1,"generated_at":"2026-09-20T00:00:00Z","suite_version":"1.0.0","source":"leaderboard","tiers":[{"id":"all","label":"All","min_vram_mb":0,"max_vram_mb":null,"models":[{"name":"X","ollama":"x:1b","tool_calling":"native","samples":5}]}]}""")));
        var fetched = await new RecommendationSource(online, "https://x/recommendations.json", () => cache, v => cache = v).LoadAsync(Ct);
        Assert.Equal("leaderboard", fetched.Source);
        Assert.NotNull(cache);
        Assert.Equal("leaderboard", (await source.LoadAsync(Ct)).Source); // offline again: the cached copy
    }

    [Theory]
    [InlineData("qwen3:8b", "native")]
    [InlineData("qwen/qwen3-14b", "native")]
    [InlineData("llama3.2:3b", "native")]
    [InlineData("gemma3:12b", "prompted")]
    [InlineData("gpt-oss:20b", "native")]
    [InlineData("some-random-model", "unknown")]
    public void ToolCallingFromName(string model, string expected) => Assert.Equal(expected, ModelCapabilities.FromName(model));

    [Fact]
    public void OllamaShowFacts()
    {
        var show = JsonNode.Parse("""{"capabilities":["completion","tools"],"details":{"quantization_level":"Q4_K_M","parameter_size":"8.2B","family":"qwen3"},"model_info":{"qwen3.context_length":40960},"parameters":"num_ctx 8192\nstop x"}""");
        Assert.Equal("native", ModelCapabilities.FromOllamaShow(show));
        var (quant, context, family, paramsB) = ModelCapabilities.OllamaFacts(show);
        Assert.Equal(("Q4_K_M", 8192, "qwen3", 8.2), (quant, context, family, paramsB));
    }

    [Fact]
    public async Task ProbeDetectsNativeAndPromptedModels()
    {
        var native = new ChatClient(new HttpClient(new FakeHandler((_, _) => FakeHandler.Sse(
            ["""{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"a","function":{"name":"get_time","arguments":"{}"}}]}}]}""", "[DONE]"]))), "http://m/v1");
        Assert.Equal("native", await ModelCapabilities.ProbeAsync(native, "m", Ct));
        var rejecting = new ChatClient(new HttpClient(new FakeHandler((_, _) => FakeHandler.Json("""{"error":"model does not support tools"}""", HttpStatusCode.BadRequest))), "http://m/v1");
        Assert.Equal("prompted", await ModelCapabilities.ProbeAsync(rejecting, "m", Ct));
    }
}

public sealed class TransportTests
{
    private static CancellationToken Ct => TestContext.Current.CancellationToken;

    [Fact]
    public async Task ChatClientAccumulatesSplitToolCallsAndUsage()
    {
        var handler = new FakeHandler((_, _) => FakeHandler.Sse(
        [
            """{"choices":[{"index":0,"delta":{"reasoning_content":"hmm"}}]}""",
            """{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"c1","type":"function","function":{"name":"set_map_flag","arguments":"{\"x\":11"}}]}}]}""",
            """{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":".2,\"y\":14.5}"}}]}}]}""",
            """{"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":10,"completion_tokens":7}}""",
            "[DONE]",
        ]));
        var result = await new ChatClient(new HttpClient(handler), "http://m/v1/").CompleteAsync(new ChatRequest { Model = "m", Messages = [ChatMessage.User("hi")] }, null, Ct);
        var call = Assert.Single(result.ToolCalls);
        Assert.Equal(("c1", "set_map_flag", """{"x":11.2,"y":14.5}"""), (call.Id, call.Name, call.Arguments));
        Assert.Equal("hmm", result.Reasoning);
        Assert.Equal(7, result.OutputTokens);
        Assert.NotNull(result.TtftMs);
        Assert.Equal("http://m/v1/chat/completions", handler.Requests[0].Url);
        Assert.True(JsonNode.Parse(handler.Requests[0].Body)!["stream"]!.GetValue<bool>());
    }

    [Fact]
    public async Task ChatClientHandlesNonStreamingServers()
    {
        var handler = new FakeHandler((_, _) => FakeHandler.Json("""{"choices":[{"message":{"role":"assistant","content":"hello"},"finish_reason":"stop"}],"usage":{"completion_tokens":1}}"""));
        var result = await new ChatClient(new HttpClient(handler), "http://m/v1").CompleteAsync(new ChatRequest { Model = "m", Messages = [ChatMessage.User("hi")] }, null, Ct);
        Assert.Equal("hello", result.Content);
    }

    [Fact]
    public async Task McpClientInitialisesKeepsTheSessionAndReadsSseReplies()
    {
        var handler = new FakeHandler((req, body) =>
        {
            var msg = JsonNode.Parse(body)!;
            var method = msg["method"]!.GetValue<string>();
            Assert.Equal("Bearer", req.Headers.Authorization!.Scheme);
            if (method != "initialize")
                Assert.Equal("s-1", req.Headers.GetValues("Mcp-Session-Id").Single());
            var id = msg["id"]?.ToJsonString() ?? "0";
            string Reply(string result) => "{\"jsonrpc\":\"2.0\",\"id\":" + id + ",\"result\":" + result + "}";
            HttpResponseMessage r = method switch
            {
                "initialize" => FakeHandler.Json(Reply("""{"protocolVersion":"2025-06-18","serverInfo":{"name":"xiv-mcp"}}""")),
                "notifications/initialized" => new HttpResponseMessage(HttpStatusCode.Accepted),
                "tools/list" => FakeHandler.Json(Reply("""{"tools":[{"name":"get_location","description":"Where","inputSchema":{"type":"object"}}]}""")),
                "tools/call" => FakeHandler.Sse(["""{"jsonrpc":"2.0","method":"notifications/progress","params":{}}""", Reply("""{"content":[{"type":"text","text":"{\"territory\":{\"name\":\"Limsa\"}}"}]}""")]),
                _ => new HttpResponseMessage(HttpStatusCode.BadRequest),
            };
            if (method == "initialize")
                r.Headers.Add("Mcp-Session-Id", "s-1");
            return r;
        });
        var client = new McpHttpClient(new HttpClient(handler), new Uri("http://127.0.0.1:41800/mcp"), "t", "almanac-dalamud", "0.1.0");
        var source = new McpToolSource(client);
        var tools = await source.ListAsync(Ct);
        Assert.Equal("get_location", Assert.Single(tools).Name);
        var outcome = await source.CallAsync("get_location", new JsonObject(), Ct);
        Assert.False(outcome.IsError);
        Assert.Equal("Limsa", outcome.Json!["territory"]!["name"]!.GetValue<string>());
        Assert.Equal("xiv-mcp", client.ServerName);
    }

    [Fact]
    public async Task ToolHubFiltersByProfileAndSurvivesADeadSource()
    {
        var suite = Suite.Bundled();
        var mock = new FixtureToolSource(suite, suite.Tools.Values.ToList());
        var dead = new DeadSource();
        var hub = new ToolHub([dead, mock], ToolProfiles.Filter(ToolProfiles.Small));
        var names = (await hub.ListAsync(Ct)).Select(t => t.Name).ToList();
        Assert.Contains("get_location", names);
        Assert.DoesNotContain("teleport", names);
        var r = await hub.CallAsync("nope", new JsonObject(), Ct);
        Assert.True(r.IsError);
    }

    private sealed class DeadSource : IToolSource
    {
        public string Id => "dead";

        public Task<IReadOnlyList<ToolDef>> ListAsync(CancellationToken ct) => throw new HttpRequestException("game closed");

        public Task<ToolOutcome> CallAsync(string name, JsonObject arguments, CancellationToken ct) => throw new NotSupportedException();
    }
}

public sealed class StoreAndChatTests
{
    [Fact]
    public void SettingsRoundTripThroughSqlite()
    {
        using var store = AlmanacStore.InMemory();
        var s = AlmanacSettings.Load(store);
        Assert.Equal("http://127.0.0.1:11434/v1", s.BaseUrl);
        s.Model = "qwen3:8b";
        s.Temperature = 0.7;
        s.MaxSteps = 99;
        s.GameOnSameGpu = false;
        s.Save(store);
        var t = AlmanacSettings.Load(store);
        Assert.Equal(("qwen3:8b", 0.7, 30, false), (t.Model, t.Temperature, t.MaxSteps, t.GameOnSameGpu));
        store.Set("settings.MaxTokens", "not a number");
        Assert.Equal(1024, AlmanacSettings.Load(store).MaxTokens);
    }

    [Fact]
    public void SettingsSaveWritesOnlyChangedKeysInOneTransaction()
    {
        using var store = AlmanacStore.InMemory();
        var s = AlmanacSettings.Load(store);
        s.Save(store);
        Assert.Equal(0, store.SetMany(store.GetPrefix("settings.")));  // saved values read back unchanged
        s.Model = "auto";
        Assert.Equal(1, store.SetMany([KeyValuePair.Create("settings.Model", "auto"), KeyValuePair.Create("settings.Temperature", store.Get("settings.Temperature")!)]));
        Assert.Equal("auto", AlmanacSettings.Load(store).Model);
    }

    [Fact]
    public void ReasoningEffortIsSentOnlyWhenSet()
    {
        ChatRequest Request(string? effort) => new() { Model = "auto", Messages = [ChatMessage.User("hi")], ReasoningEffort = effort };
        Assert.Equal("none", ChatClient.BuildBody(Request("none"))["reasoning_effort"]!.GetValue<string>());
        Assert.False(ChatClient.BuildBody(Request(null)).ContainsKey("reasoning_effort"));
        Assert.False(new AlmanacSettings().Thinking);  // off unless asked for
    }

    [Fact]
    public async Task ModelPullReportsProgressAndFailures()
    {
        var lines = string.Join("\n",
            """{"status":"pulling manifest"}""",
            """{"status":"pulling 2a654d98","digest":"sha256:2a65","total":1000,"completed":250}""",
            """{"status":"pulling 2a654d98","digest":"sha256:2a65","total":1000,"completed":1000}""",
            """{"status":"verifying sha256 digest"}""",
            """{"status":"success"}""");
        var handler = new FakeHandler((req, body) => FakeHandler.Json(lines));
        var seen = new List<PullProgress>();
        await ModelPull.PullAsync(new HttpClient(handler), "http://127.0.0.1:11434", "qwen3.5:4b", new SyncProgress<PullProgress>(seen.Add), CancellationToken.None);
        Assert.Equal("http://127.0.0.1:11434/api/pull", handler.Requests[0].Url);
        Assert.Contains("\"model\":\"qwen3.5:4b\"", handler.Requests[0].Body);
        Assert.Equal(0.25, seen[1].Fraction);
        Assert.Null(seen[0].Fraction);
        Assert.Equal("success", seen[^1].Status);

        var missing = new FakeHandler((req, body) => FakeHandler.Json("""{"error":"pull model manifest: file does not exist"}"""));
        var ex = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            ModelPull.PullAsync(new HttpClient(missing), "http://x", "nope:1b", null, CancellationToken.None));
        Assert.Contains("file does not exist", ex.Message);

        var cut = new FakeHandler((req, body) => FakeHandler.Json("""{"status":"pulling manifest"}"""));
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            ModelPull.PullAsync(new HttpClient(cut), "http://x", "m", null, CancellationToken.None));
    }

    [Fact]
    public async Task ServerGpuReadsTheEngineReport()
    {
        var report = """
            {"gpus":[],"inference":{"name":"Quadro P4000","vendor":"nvidia","total_mb":8192,"used_mb":6691,"free_mb":1501},
             "loaded":[{"model":"qwen3.5:4b","vram_mb":3541}],"installed":["qwen3.5:4b","qwen3.5:9b"],
             "reservations":{"ffxiv":2993},"reclaimable_mb":3541,"headroom_mb":819,"budget_mb":4380,
             "auto":{"models":["qwen3.5:9b","qwen3.5:4b"],"choice":"qwen3.5:4b","reason":"qwen3.5:4b needs about 3541 MB of 4380 MB available"}}
            """;
        var handler = new FakeHandler((req, body) =>
            req.Headers.Authorization?.Parameter == "tok" ? FakeHandler.Json(report) : FakeHandler.Json("{}", System.Net.HttpStatusCode.Unauthorized));
        var gpu = await ServerGpu.ReadAsync(new HttpClient(handler), "http://127.0.0.1:41881/v1", "tok", CancellationToken.None);
        Assert.Equal("http://127.0.0.1:41881/v1/almanac/gpu", handler.Requests[0].Url);
        Assert.NotNull(gpu);
        Assert.Equal(("Quadro P4000", 8192, 1501, 4380), (gpu.Name, gpu.TotalMb, gpu.FreeMb, gpu.BudgetMb));
        Assert.Equal(("qwen3.5:4b", 3541), gpu.Loaded[0]);
        Assert.Equal(2993, gpu.Reservations["ffxiv"]);
        Assert.Equal("qwen3.5:4b", gpu.AutoChoice);
        Assert.Null(await ServerGpu.ReadAsync(new HttpClient(handler), "http://127.0.0.1:41881/v1", "wrong", CancellationToken.None));
        Assert.Null(ServerGpu.Parse(System.Text.Json.Nodes.JsonNode.Parse("""{"gpus":[],"inference":null}""")));
    }

    [Fact]
    public void ThreadsMessagesAndForks()
    {
        using var store = AlmanacStore.InMemory();
        var t = store.CreateThread("Where am I?");
        store.AppendMessage(t.Id, ChatMessage.User("Where am I?"));
        store.AppendMessage(t.Id, ChatMessage.Assistant(null, [new ToolCall("c1", "get_location", "{}")]));
        store.AppendMessage(t.Id, ChatMessage.Tool("c1", """{"territory":{"name":"Limsa"}}"""));
        store.AppendMessage(t.Id, ChatMessage.Assistant("Limsa."));
        var messages = store.Messages(t.Id);
        Assert.Equal(4, messages.Count);
        Assert.Equal("get_location", messages[1].Message.ToolCalls![0].Name);
        var fork = store.ForkThread(t.Id, 0, "branch");
        Assert.Single(store.Messages(fork.Id));
        Assert.Equal(t.Id, fork.ParentId);
        Assert.Equal(2, store.ListThreads().Count);
        store.DeleteThread(t.Id);
        Assert.Single(store.ListThreads());
        Assert.Null(store.ListThreads()[0].ParentId);
    }

    [Fact]
    public async Task ChatSessionRunsTheAgentStreamsAndPersistsFollowUps()
    {
        using var store = AlmanacStore.InMemory();
        var suite = Suite.Bundled();
        var model = BenchRunTests.Perfect();
        model.Script["And the nearest aetheryte?"] = [new ScriptedModel.Turn(null, ("get_location", "{}")), new ScriptedModel.Turn("Limsa Lominsa.")];
        var chat = new ChatClient(new HttpClient(new FakeHandler(model.Respond)), "http://m/v1");
        var tools = new FixtureToolSource(suite, suite.Tools.Values.ToList());
        var session = new ChatSession(store, () => new AgentLoop(chat, tools, new AgentOptions { Model = "m" }), () => ChatSession.DefaultSystemPrompt);

        await session.SendAsync(suite.Tasks[0].Prompt);
        await session.SendAsync("And the nearest aetheryte?");

        Assert.False(session.Busy);
        var thread = Assert.Single(store.ListThreads());
        var roles = store.Messages(thread.Id).Select(m => m.Message.Role).ToList();
        Assert.Equal(["user", "assistant", "tool", "assistant", "user", "assistant", "tool", "assistant"], roles);
        var lines = session.Snapshot();
        Assert.Contains(lines, l => l.Kind == LineKind.Tool && l.Text.Contains("get_location"));
        Assert.Equal("Limsa Lominsa.", lines.Last(l => l.Kind == LineKind.Assistant).Text);

        // Reopening shows the same transcript from SQLite.
        var reopened = new ChatSession(store, () => throw new InvalidOperationException(), () => "");
        reopened.Open(thread.Id);
        Assert.Equal(lines.Count(l => l.Kind != LineKind.Notice), reopened.Snapshot().Count);
    }

    [Fact]
    public void HistoryWindowStartsAtAUserMessage()
    {
        var messages = new List<ChatMessage>();
        for (var i = 0; i < 30; i++)
        {
            messages.Add(ChatMessage.User($"q{i}"));
            messages.Add(ChatMessage.Assistant(null, [new ToolCall("c", "get_time", "{}")]));
            messages.Add(ChatMessage.Tool("c", "{}"));
        }

        var window = ChatSession.Window(messages);
        Assert.True(window.Count <= ChatSession.HistoryWindow);
        Assert.Equal("user", window[0].Role);
    }
}

/// <summary>An IProgress that reports on the calling thread (Progress&lt;T&gt; posts to the thread pool).</summary>
internal sealed class SyncProgress<T>(Action<T> report) : IProgress<T>
{
    public void Report(T value) => report(value);
}
