using Almanac.Core.Diagnostics;
using System.Text.Json.Nodes;
using Almanac.Core.Llm;
using Almanac.Core.Mcp;

namespace Almanac.Core.Tools;

/// <summary>What a tool call returned, as the model will see it.</summary>
public sealed record ToolOutcome(string Text, bool IsError, JsonNode? Json = null)
{
    public static ToolOutcome Error(string message) => new(new JsonObject { ["error"] = message }.ToJsonString(), true);
}

/// <summary>
/// A provider of tools for the agent loop. This is the extension point for new kinds of tools:
/// <list type="bullet">
/// <item><see cref="McpToolSource"/>: XivMcp (or any MCP server) over HTTP.</item>
/// <item><see cref="FixtureToolSource"/>: the benchmark's deterministic mock tools.</item>
/// <item>Planned: a <c>WasmToolSource</c> that loads sandboxed WebAssembly modules (one module per tool, WASI with no
/// filesystem or network unless granted), each exporting its <see cref="ToolDef"/> JSON and a <c>call(args-json) -> result-json</c>
/// entry point. It only has to implement this interface; <see cref="ToolHub"/> merges it with the others.</item>
/// </list>
/// Implementations must be safe to call from a background thread and must not throw for tool-level failures (return
/// <see cref="ToolOutcome.Error"/> instead); exceptions are reserved for transport failures.
/// </summary>
public interface IToolSource
{
    /// <summary>Stable id shown in the UI, e.g. "xivmcp", "mock", "wasm".</summary>
    string Id { get; }

    Task<IReadOnlyList<ToolDef>> ListAsync(CancellationToken ct);

    Task<ToolOutcome> CallAsync(string name, JsonObject arguments, CancellationToken ct);
}

/// <summary>Tools of an MCP server (XivMcp). XivMcp enforces its permission tiers and in-game approvals server side.</summary>
public sealed class McpToolSource(McpHttpClient client, string id = "xivmcp") : IToolSource, IDisposable
{
    private IReadOnlyList<ToolDef>? cached;
    private int disposed;

    public string Id { get; } = Track(id);

    private static string Track(string id)
    {
        LiveObjects.Acquired(LiveObjects.Kinds.McpToolSource);
        return id;
    }

    public McpHttpClient Client { get; } = client;

    public async Task<IReadOnlyList<ToolDef>> ListAsync(CancellationToken ct)
    {
        if (cached != null)
            return cached;
        var tools = await Client.ListToolsAsync(ct).ConfigureAwait(false);
        cached = tools.Select(t => new ToolDef(t.Name, t.Description, t.InputSchema)).ToList();
        return cached;
    }

    public void Invalidate() => cached = null;

    public void Dispose()
    {
        if (Interlocked.Exchange(ref disposed, 1) != 0)
            return;
        Client.Dispose();
        LiveObjects.Released(LiveObjects.Kinds.McpToolSource);
    }

    public async Task<ToolOutcome> CallAsync(string name, JsonObject arguments, CancellationToken ct)
    {
        try
        {
            var result = await Client.CallToolAsync(name, arguments, ct).ConfigureAwait(false);
            return new ToolOutcome(result.Text, result.IsError, result.Structured ?? TryParse(result.Text));
        }
        catch (McpException ex)
        {
            return ToolOutcome.Error(ex.Message);
        }
    }

    internal static JsonNode? TryParse(string text)
    {
        try
        {
            return JsonNode.Parse(text);
        }
        catch (System.Text.Json.JsonException)
        {
            return null;
        }
    }
}

/// <summary>Several sources behind one list; the first source that lists a name owns it. An optional filter narrows the list.</summary>
public sealed class ToolHub(IReadOnlyList<IToolSource> sources, Func<ToolDef, bool>? filter = null) : IToolSource
{
    private readonly Dictionary<string, IToolSource> owners = new(StringComparer.Ordinal);

    public string Id => "hub";

    public async Task<IReadOnlyList<ToolDef>> ListAsync(CancellationToken ct)
    {
        var all = new List<ToolDef>();
        owners.Clear();
        foreach (var source in sources)
        {
            IReadOnlyList<ToolDef> tools;
            try
            {
                tools = await source.ListAsync(ct).ConfigureAwait(false);
            }
            catch (Exception) when (!ct.IsCancellationRequested)
            {
                continue; // A source that is down (game closed) just contributes nothing.
            }

            foreach (var tool in tools)
            {
                if (owners.ContainsKey(tool.Name) || filter != null && !filter(tool))
                    continue;
                owners[tool.Name] = source;
                all.Add(tool);
            }
        }

        return all;
    }

    public Task<ToolOutcome> CallAsync(string name, JsonObject arguments, CancellationToken ct) =>
        owners.TryGetValue(name, out var source)
            ? source.CallAsync(name, arguments, ct)
            : Task.FromResult(ToolOutcome.Error($"unknown tool {name}"));
}

/// <summary>Named tool sets. Small models do better with fewer, shorter tool descriptions.</summary>
public static class ToolProfiles
{
    public const string Small = "small";
    public const string Standard = "standard";
    public const string All = "all";

    public static readonly string[] Names = [Small, Standard, All];

    public static readonly IReadOnlySet<string> SmallSet = new HashSet<string>(StringComparer.Ordinal)
    {
        "get_location", "get_time", "get_weather_forecast", "list_aetherytes", "set_map_flag", "search_quests", "get_quest",
        "get_player", "search_items", "post_status",
    };

    public static readonly IReadOnlySet<string> StandardSet = new HashSet<string>(SmallSet, StringComparer.Ordinal)
    {
        "get_target", "get_quest_status", "get_item", "find_owned_items", "get_recipe", "search_recipes", "search_duties", "get_duty",
        "list_fates", "get_party", "get_equipment", "get_job_levels", "get_currencies", "get_dialogue", "list_objectives", "post_objective",
        "show_toast", "print_echo", "execute_command", "teleport", "request_action", "get_ticket",
    };

    public static Func<ToolDef, bool>? Filter(string profile) => profile switch
    {
        Small => t => SmallSet.Contains(t.Name),
        Standard => t => StandardSet.Contains(t.Name),
        _ => null,
    };
}
