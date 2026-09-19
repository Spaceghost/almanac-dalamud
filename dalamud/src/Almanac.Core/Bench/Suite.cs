using System.Security.Cryptography;
using System.Text.Json.Nodes;
using Almanac.Core.Llm;
using Almanac.Core.Tools;

namespace Almanac.Core.Bench;

public sealed record SuiteTask(
    string Id,
    string Category,
    string Prompt,
    IReadOnlyList<string> Tools,
    int MaxSteps,
    JsonObject Expect,
    bool LiveSkipAnswer);

/// <summary>A parsed benchmark suite. <see cref="Sha256"/> is over the file's exact bytes.</summary>
public sealed class Suite
{
    public required string Id { get; init; }

    public required string Version { get; init; }

    public required string Sha256 { get; init; }

    public required string SystemPrompt { get; init; }

    public required double Temperature { get; init; }

    public required int MaxTokens { get; init; }

    public required double ToolWeight { get; init; }

    public required double AnswerWeight { get; init; }

    public required IReadOnlyDictionary<string, ToolDef> Tools { get; init; }

    public required JsonObject Fixtures { get; init; }

    public required IReadOnlyList<SuiteTask> Tasks { get; init; }

    public static Suite Parse(byte[] bytes)
    {
        var root = JsonNode.Parse(bytes)!.AsObject();
        var defaults = root["defaults"]!.AsObject();
        var weights = defaults["weights"]!.AsObject();
        var maxSteps = defaults["max_steps"]!.GetValue<int>();
        var tools = new Dictionary<string, ToolDef>(StringComparer.Ordinal);
        foreach (var t in root["tools"]!.AsArray())
        {
            var name = t!["name"]!.GetValue<string>();
            tools[name] = new ToolDef(name, t["description"]!.GetValue<string>(), t["inputSchema"]!.AsObject());
        }

        var tasks = new List<SuiteTask>();
        foreach (var t in root["tasks"]!.AsArray())
        {
            tasks.Add(new SuiteTask(
                t!["id"]!.GetValue<string>(),
                t["category"]?.GetValue<string>() ?? "",
                t["prompt"]!.GetValue<string>(),
                t["tools"]!.AsArray().Select(n => n!.GetValue<string>()).ToList(),
                t["max_steps"]?.GetValue<int>() ?? maxSteps,
                t["expect"]!.AsObject(),
                t["live"]?["answer"]?.GetValue<string>() == "skip"));
        }

        return new Suite
        {
            Id = root["id"]!.GetValue<string>(),
            Version = root["version"]!.GetValue<string>(),
            Sha256 = Convert.ToHexStringLower(SHA256.HashData(bytes)),
            SystemPrompt = root["system_prompt"]!.GetValue<string>(),
            Temperature = defaults["temperature"]!.GetValue<double>(),
            MaxTokens = defaults["max_tokens"]!.GetValue<int>(),
            ToolWeight = weights["tools"]!.GetValue<double>(),
            AnswerWeight = weights["answer"]!.GetValue<double>(),
            Tools = tools,
            Fixtures = root["fixtures"]!.AsObject(),
            Tasks = tasks,
        };
    }

    /// <summary>The suite compiled into this assembly (the same bytes as benchmark/suites/ffxiv-core.json).</summary>
    public static Suite Bundled()
    {
        using var stream = typeof(Suite).Assembly.GetManifestResourceStream("Almanac.Core.suites.ffxiv-core.json")!;
        using var ms = new MemoryStream();
        stream.CopyTo(ms);
        return Parse(ms.ToArray());
    }

    public IReadOnlyList<ToolDef> ToolsFor(SuiteTask task) => task.Tools.Where(Tools.ContainsKey).Select(n => Tools[n]).ToList();
}

/// <summary>Mock tools: answers calls from the suite's fixtures (first matching <c>when</c> wins).</summary>
public sealed class FixtureToolSource(Suite suite, IReadOnlyList<ToolDef> offered) : IToolSource
{
    public string Id => "mock";

    public Task<IReadOnlyList<ToolDef>> ListAsync(CancellationToken ct) => Task.FromResult(offered);

    public Task<ToolOutcome> CallAsync(string name, JsonObject arguments, CancellationToken ct)
    {
        var result = Lookup(suite.Fixtures, name, arguments);
        return Task.FromResult(result == null
            ? new ToolOutcome(new JsonObject { ["error"] = "no data" }.ToJsonString(), true)
            : new ToolOutcome(result.ToJsonString(), false, result.DeepClone()));
    }

    public static JsonNode? Lookup(JsonObject fixtures, string name, JsonObject arguments)
    {
        if (!fixtures.TryGetPropertyValue(name, out var fixture) || fixture == null)
            return null;
        if (fixture is not JsonArray cases)
            return fixture;
        foreach (var c in cases)
        {
            if (Matchers.MatchAll(c?["when"] as JsonObject, arguments))
                return c?["result"];
        }

        return null;
    }
}
