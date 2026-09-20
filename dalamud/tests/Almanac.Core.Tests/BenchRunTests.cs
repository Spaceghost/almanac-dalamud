using System.Text.Json.Nodes;
using Almanac.Core.Bench;
using Almanac.Core.Llm;
using Almanac.Core.Storage;
using static Almanac.Core.Tests.ScriptedModel;

namespace Almanac.Core.Tests;

public sealed class BenchRunTests
{
    private static readonly Suite Suite = Suite.Bundled();

    /// <summary>A model that does everything right.</summary>
    internal static ScriptedModel Perfect(bool rejectTools = false)
    {
        var m = new ScriptedModel { RejectTools = rejectTools };
        void S(string id, params Turn[] turns) => m.Script[Suite.Tasks.Single(t => t.Id == id).Prompt] = [.. turns];
        S("where_am_i", new Turn(null, ("get_location", "{}")), new Turn("You are in Limsa Lominsa Lower Decks at X 9.4, Y 11.8."));
        S("nearest_aetheryte", new Turn(null, ("get_location", "{}")), new Turn("The closest aetheryte is Limsa Lominsa."));
        S("weather_next_here", new Turn(null, ("get_weather_forecast", "{}")), new Turn("Next up: Clouds."));
        S("weather_rain_zone", new Turn(null, ("get_weather_forecast", """{"territoryId":135}""")), new Turn("Yes: Rain starts at ET 00:00."));
        S("teleport_cost", new Turn(null, ("list_aetherytes", """{"nameContains":"Moraby"}""")), new Turn("A teleport to Moraby Drydocks costs 243 gil."));
        S("aetherytes_filtered", new Turn(null, ("list_aetherytes", """{"nameContains":"Noscea"}""")), new Turn("Summerford Farms, Moraby Drydocks and Costa del Sol."));
        S("flag_coordinates", new Turn(null, ("set_map_flag", """{"x":11.2,"y":14.5}""")), new Turn("Flag placed at 11.2, 14.5."));
        S("quest_level", new Turn(null, ("search_quests", """{"query":"It's Probably Pirates"}""")), new Turn("It is a level 13 quest."));
        S("command_gearset", new Turn("/gearset change 3"));
        S("command_hudlayout", new Turn("`/hudlayout 2`"));
        S("restraint_general", new Turn("It finds you a party and queues you for duties."));
        S("multi_step_status",
            new Turn(null, ("get_location", "{}")),
            new Turn(null, ("get_weather_forecast", "{}")),
            new Turn(null, ("post_status", """{"agent":"almanac-bench","status":"Limsa: Clouds next"}""")),
            new Turn("You are in Limsa Lominsa Lower Decks; Clouds come next. Posted to the board."));
        return m;
    }

    private static async Task<BenchRun> Run(ScriptedModel model, ToolCallingMode mode = ToolCallingMode.Native)
    {
        var handler = new FakeHandler(model.Respond);
        var chat = new ChatClient(new HttpClient(handler), "http://model.test/v1");
        var runner = new BenchRunner(chat, Suite) { ToolCalling = mode, Vram = new FixedVram(4321) };
        return await runner.RunAsync("qwen3:8b", BenchMode.Mock, null, null, TestContext.Current.CancellationToken);
    }

    [Fact]
    public async Task PerfectModelScoresHundredWithNativeTools()
    {
        var run = await Run(Perfect());
        Assert.All(run.Tasks, t => Assert.True(t.Score.Success, $"{t.Task.Id}: {t.Score}"));
        Assert.Equal(100, run.Score);
        Assert.Equal("native", run.ToolCalling);
        Assert.Equal(1, run.ToolCallValidity);
        Assert.Equal(4321, run.PeakVramMb);
        Assert.True(run.TtftMs >= 0);
    }

    [Fact]
    public async Task BackendWithoutToolsFallsBackToPrompted()
    {
        var run = await Run(Perfect(rejectTools: true));
        Assert.Equal("prompted", run.ToolCalling);
        Assert.Equal(100, run.Score);
    }

    [Fact]
    public async Task ModelThatNeverCallsToolsIsMarkedNone()
    {
        var m = new ScriptedModel();
        foreach (var t in Suite.Tasks)
            m.Script[t.Prompt] = [new Turn("I don't know.")];
        var run = await Run(m);
        Assert.Equal("none", run.ToolCalling);
        Assert.True(run.Score < 30);
    }

    [Fact]
    public async Task ResultValidatesAgainstTheSharedSchemaAndCarriesNothingPersonal()
    {
        var run = await Run(Perfect());
        var result = Results.Build(run, new HardwareFacts("NVIDIA GeForce RTX 4060", "nvidia", 8188, 32, "windows"),
            new ModelFacts("ollama", "0.12.0", "qwen3:8b", "qwen3", 8.2, "Q4_K_M", 8192), "almanac-dalamud", "0.1.0");
        var schema = JsonNode.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "schema", "results.schema.json")))!.AsObject();
        var errors = TestSchema.Errors(schema, result);
        Assert.True(errors.Count == 0, string.Join("; ", errors));
        Assert.Equal(7936, result["hardware"]!["vram_mb"]!.GetValue<int>()); // rounded down to 256 MB
        var text = result.ToJsonString();
        Assert.DoesNotContain("model.test", text);
        Assert.DoesNotContain(Environment.UserName, text);
    }

    [Theory]
    [InlineData("/home/someone/models/Qwen3-8B-Q4_K_M.gguf", "Qwen3-8B-Q4_K_M.gguf")]
    [InlineData("C:\\Users\\someone\\models\\x.gguf", "x.gguf")]
    [InlineData("qwen/qwen3-8b", "qwen/qwen3-8b")]
    [InlineData("qwen3:8b", "qwen3:8b")]
    public void ModelIdsThatArePathsKeepOnlyTheFileName(string id, string expected) => Assert.Equal(expected, Results.SafeModelName(id));

    [Fact]
    public async Task RunsAreStoredInSqlite()
    {
        using var store = AlmanacStore.InMemory();
        var run = await Run(Perfect());
        var result = Results.Build(run, new HardwareFacts("unknown", "unknown", 0, null, "linux"), new ModelFacts("ollama", null, "qwen3:8b", null, null, "unknown", 0), "almanac-dalamud", "0.1.0");
        var id = store.SaveBenchRun(result);
        var row = Assert.Single(store.BenchRuns());
        Assert.Equal(id, row.Id);
        Assert.Equal(100, row.Score);
        Assert.False(row.Submitted);
        store.MarkSubmitted(id);
        Assert.True(store.BenchRuns()[0].Submitted);
    }

    [Fact]
    public async Task SubmitPostsToTheLeaderboardApi()
    {
        var handler = new FakeHandler((_, _) => FakeHandler.Json("""{"ok":true}"""));
        var sent = await Results.SubmitAsync(new HttpClient(handler), Results.DefaultLeaderboard, new JsonObject { ["schema_version"] = 1 }, null, TestContext.Current.CancellationToken);
        Assert.True(sent.Ok);
        Assert.Equal("https://spacegho.st/mods/ffxiv/almanac/api/results", handler.Requests.Single().Url);
    }

    private sealed class FixedVram(int mb) : IVramProbe
    {
        public Task<int?> ReadUsedMbAsync(CancellationToken ct) => Task.FromResult<int?>(mb);
    }
}

/// <summary>JSON Schema subset validator for tests (type incl. unions, const, enum, pattern, required, additionalProperties, items, min/max, maxLength, maxItems).</summary>
internal static class TestSchema
{
    public static List<string> Errors(JsonObject schema, JsonNode? value, string at = "$")
    {
        var errors = new List<string>();
        if (schema["const"] is { } c && !JsonNode.DeepEquals(c, value))
            errors.Add($"{at}: const");
        if (schema["enum"] is JsonArray e && !e.Any(o => JsonNode.DeepEquals(o, value)))
            errors.Add($"{at}: enum {value?.ToJsonString()}");
        if (schema["type"] is { } type)
        {
            var types = type is JsonArray ta ? ta.Select(t => t!.GetValue<string>()).ToList() : [type.GetValue<string>()];
            if (!types.Any(t => Is(t, value)))
                errors.Add($"{at}: type {string.Join('|', types)} got {value?.ToJsonString() ?? "null"}");
        }

        if (Num(value) is { } n)
        {
            if (Num(schema["minimum"]) is { } min && n < min)
                errors.Add($"{at}: minimum");
            if (Num(schema["maximum"]) is { } max && n > max)
                errors.Add($"{at}: maximum");
        }

        if (value is JsonValue sv && sv.GetValueKind() == System.Text.Json.JsonValueKind.String)
        {
            var s = sv.GetValue<string>();
            if (Num(schema["maxLength"]) is { } ml && s.Length > ml)
                errors.Add($"{at}: maxLength");
            if (schema["pattern"] is { } p && !System.Text.RegularExpressions.Regex.IsMatch(s, p.GetValue<string>()))
                errors.Add($"{at}: pattern");
        }

        if (value is JsonObject o)
        {
            var props = schema["properties"] as JsonObject;
            foreach (var r in schema["required"] as JsonArray ?? [])
            {
                if (!o.ContainsKey(r!.GetValue<string>()))
                    errors.Add($"{at}: missing {r}");
            }

            foreach (var (k, child) in o)
            {
                if (props?[k] is JsonObject ps)
                    errors.AddRange(Errors(ps, child, $"{at}.{k}"));
                else if (schema["additionalProperties"] is JsonValue ap && !ap.GetValue<bool>())
                    errors.Add($"{at}: unexpected {k}");
            }
        }

        if (value is JsonArray a)
        {
            if (Num(schema["maxItems"]) is { } mi && a.Count > mi)
                errors.Add($"{at}: maxItems");
            if (schema["items"] is JsonObject items)
            {
                for (var i = 0; i < a.Count; i++)
                    errors.AddRange(Errors(items, a[i], $"{at}[{i}]"));
            }
        }

        return errors;
    }

    private static double? Num(JsonNode? node) =>
        node is JsonValue v && v.GetValueKind() == System.Text.Json.JsonValueKind.Number
            ? double.Parse(v.ToJsonString(), System.Globalization.CultureInfo.InvariantCulture)
            : null;

    private static bool Is(string type, JsonNode? v) => type switch
    {
        "null" => v == null,
        "object" => v is JsonObject,
        "array" => v is JsonArray,
        "string" => v is JsonValue s && s.GetValueKind() == System.Text.Json.JsonValueKind.String,
        "boolean" => v is JsonValue b && b.GetValueKind() is System.Text.Json.JsonValueKind.True or System.Text.Json.JsonValueKind.False,
        "number" => Num(v) != null,
        "integer" => Num(v) is { } i && Math.Abs(i % 1) < 1e-9,
        _ => true,
    };
}
