using System.Text.Json.Nodes;
using Almanac.Core.Bench;
using Almanac.Core.Llm;

namespace Almanac.Core.Tests;

/// <summary>The C# scorer against the language-neutral vectors the Python engine is tested with.</summary>
public sealed class ScoringTests
{
    private static readonly Suite Suite = Suite.Bundled();

    private static JsonObject Vectors() =>
        JsonNode.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "testdata", "scoring-vectors.json")))!.AsObject();

    public static TheoryData<string> VectorNames()
    {
        var data = new TheoryData<string>();
        foreach (var v in Vectors()["vectors"]!.AsArray())
            data.Add(v!["name"]!.GetValue<string>());
        return data;
    }

    [Fact]
    public void BundledSuiteIsTheOneTheVectorsPin()
    {
        var pinned = Vectors()["suite"]!;
        Assert.Equal(pinned["sha256"]!.GetValue<string>(), Suite.Sha256);
        Assert.Equal(pinned["version"]!.GetValue<string>(), Suite.Version);
    }

    [Theory]
    [MemberData(nameof(VectorNames))]
    public void Vector(string name)
    {
        var v = Vectors()["vectors"]!.AsArray().First(n => n!["name"]!.GetValue<string>() == name)!;
        var task = Suite.Tasks.Single(t => t.Id == v["task"]!.GetValue<string>());
        var offered = Suite.ToolsFor(task);
        var live = v["mode"]!.GetValue<string>() == "live";
        var calls = new List<CallRecord>();
        foreach (var c in v["calls"]!.AsArray())
        {
            var raw = c!["arguments"]!.GetValue<string>();
            var (valid, reason, _) = Scorer.Validate(offered, c["name"]!.GetValue<string>(), raw);
            calls.Add(new CallRecord(c["name"]!.GetValue<string>(), raw, valid, reason, valid ? c["result"]?.DeepClone() : null));
        }

        var answer = v["final_answer"]?.GetValue<string>();
        var score = Scorer.Score(task, Suite.ToolWeight, Suite.AnswerWeight, live, calls, answer);
        var e = v["expected"]!;
        Assert.Equal(e["tool_score"]!.GetValue<double>(), score.ToolScore, 6);
        if (e["answer_score"] is null)
            Assert.Null(score.AnswerScore);
        else
            Assert.Equal(e["answer_score"]!.GetValue<double>(), score.AnswerScore!.Value, 6);
        Assert.Equal(e["score"]!.GetValue<double>(), score.Score, 6);
        Assert.Equal(e["success"]!.GetValue<bool>(), score.Success);
        Assert.Equal(e["error"]?.GetValue<string>(), score.Error);
        Assert.Equal(e["valid_calls"]!.GetValue<int>(), score.ValidCalls);
        Assert.Equal(e["total_calls"]!.GetValue<int>(), score.TotalCalls);
    }

    [Theory]
    [InlineData("```\n/gearset change 3\n```", "/gearset change 3")]
    [InlineData("`/GEARSET  change 3`", "/gearset change 3")]
    [InlineData("/hudlayout 2.", "/hudlayout 2")]
    public void ExactNormalisation(string answer, string expected) =>
        Assert.Equal(Scorer.NormalizeExact(expected), Scorer.NormalizeExact(answer), ignoreCase: true);

    [Fact]
    public void NumbersIgnoreThousandsSeparators() => Assert.Contains(1234.0, Scorer.Numbers("costs 1,234 gil at 9.5"));

    [Fact]
    public void PathSelectsByKeySubstring()
    {
        var node = JsonNode.Parse("""{"a":[{"name":"Foo","v":1},{"name":"Moraby Drydocks","v":243}]}""");
        Assert.Equal(243, JsonPathLite.Resolve(node, "a[name~moraby].v")!.GetValue<int>());
        Assert.Equal("Foo", JsonPathLite.Resolve(node, "a[0].name")!.GetValue<string>());
        Assert.Null(JsonPathLite.Resolve(node, "a[5].name"));
    }

    [Fact]
    public void SchemaRejectsUnknownArgumentsAndWrongTypes()
    {
        var tool = Suite.Tools["get_weather_forecast"];
        Assert.Null(SchemaLite.Validate(tool.InputSchema, JsonNode.Parse("""{"territoryId":135}""")));
        Assert.Null(SchemaLite.Validate(tool.InputSchema, JsonNode.Parse("""{"territoryId":135.0}""")));
        Assert.NotNull(SchemaLite.Validate(tool.InputSchema, JsonNode.Parse("""{"territoryId":"135"}""")));
        Assert.NotNull(SchemaLite.Validate(tool.InputSchema, JsonNode.Parse("""{"zone":"x"}""")));
        Assert.NotNull(SchemaLite.Validate(tool.InputSchema, JsonNode.Parse("""{"count":99}""")));
    }

    [Fact]
    public void FixturesPickFirstMatchingCase()
    {
        var r = FixtureToolSource.Lookup(Suite.Fixtures, "list_aetherytes", JsonNode.Parse("""{"nameContains":"LIMSA"}""")!.AsObject());
        Assert.Equal(1, r!["total"]!.GetValue<int>());
        var any = FixtureToolSource.Lookup(Suite.Fixtures, "list_aetherytes", []);
        Assert.Equal(5, any!["total"]!.GetValue<int>());
        Assert.Null(FixtureToolSource.Lookup(Suite.Fixtures, "teleport", []));
    }

    [Fact]
    public void PromptedCallsParseFromFencedJson()
    {
        var call = PromptedTools.TryParse("```json\n{\"tool\": \"get_location\", \"arguments\": {}}\n```", "c1");
        Assert.Equal("get_location", call!.Name);
        Assert.Null(PromptedTools.TryParse("You are in Limsa.", "c2"));
    }
}
