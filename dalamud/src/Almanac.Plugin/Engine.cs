using Almanac.Core.Agent;
using Almanac.Core.Bench;
using Almanac.Core.Llm;
using Almanac.Core.Mcp;
using Almanac.Core.Setup;
using Almanac.Core.Storage;
using Almanac.Core.Tools;

namespace Almanac.Plugin;

/// <summary>
/// Wires settings to the Core pieces: the model client, XivMcp's tools, the agent loop and the chat session.
/// Rebuilt lazily whenever settings change (<see cref="Invalidate"/>).
/// </summary>
public sealed class Engine(AlmanacStore store, XivMcpLink xivmcp, Func<AlmanacSettings> settings) : IDisposable
{
    public static readonly string Version = typeof(Engine).Assembly.GetName().Version?.ToString(3) ?? "0.1.0";

    // Long timeout: XivMcp holds Action-tier calls while the player decides in game, and big models are slow.
    private readonly HttpClient http = new() { Timeout = TimeSpan.FromMinutes(10) };
    private readonly HttpClient quick = new() { Timeout = TimeSpan.FromSeconds(20) };
    private McpToolSource? xivTools;
    private string? xivModel;

    public HttpClient Http => http;

    public HttpClient Quick => quick;

    public string? XivMcpStatus { get; private set; }

    public ChatSession? Session { get; private set; }

    /// <summary>The model to use: this plugin's, or the one configured in XivMcp's "Local model" section.</summary>
    public (string BaseUrl, string? ApiKey, string Model) ModelTarget()
    {
        var s = settings();
        if (s.Engine == AlmanacSettings.EngineDirect && string.IsNullOrWhiteSpace(s.Model) && s.FollowXivMcpModel && xivmcp.LocalModel() is { Configured: true } lm
            && !string.IsNullOrEmpty(lm.Endpoint) && !string.IsNullOrEmpty(lm.Model))
        {
            xivModel = lm.Model;
            return (lm.Endpoint!, null, lm.Model!);
        }

        return (s.EffectiveBaseUrl, string.IsNullOrWhiteSpace(s.EffectiveApiKey) ? null : s.EffectiveApiKey, s.Model);
    }

    public string ModelLabel => ModelTarget() is var t && t.Model.Length > 0 ? t.Model + (xivModel == t.Model ? " (from XivMcp)" : "") : "(no model)";

    public ChatClient Chat()
    {
        var (baseUrl, apiKey, _) = ModelTarget();
        return new ChatClient(http, baseUrl, apiKey);
    }

    public ToolCallingMode ToolMode(string model)
    {
        var s = settings();
        return s.ToolCalling switch
        {
            "native" => ToolCallingMode.Native,
            "prompted" => ToolCallingMode.Prompted,
            "none" => ToolCallingMode.None,
            _ => ModelCapabilities.ToMode(Capability(model)),
        };
    }

    /// <summary>What we know about a model's tool calling: a stored probe, else a guess from its name.</summary>
    public string Capability(string model) => store.GetCapability(CapabilityKey(model)) ?? ModelCapabilities.FromName(model);

    public string CapabilityKey(string model) => $"{ServerDetector.RootOf(ModelTarget().BaseUrl)}|{model}";

    [System.Diagnostics.CodeAnalysis.SuppressMessage(
        "Reliability",
        "CA2000:Dispose objects before losing scope",
        Justification = "The McpHttpClient is handed to the McpToolSource stored in the xivTools field; Invalidate() and Dispose() release it, which CA2213 checks.")]
    public async Task<McpToolSource?> XivMcpToolsAsync(bool forceNew = false)
    {
        if (xivTools != null && !forceNew)
            return xivTools;
        var s = settings();
        var connection = await xivmcp.ConnectAsync(s.XivMcpViaIpc, s.XivMcpEndpoint, s.XivMcpToken, forceNew).ConfigureAwait(false);
        if (connection == null)
        {
            XivMcpStatus = xivmcp.LastError ?? "XivMcp not connected.";
            return null;
        }

        xivTools?.Dispose();
        xivTools = new McpToolSource(new McpHttpClient(http, connection.Endpoint, connection.Token, XivMcpLink.ClientName, Version));
        try
        {
            var tools = await xivTools.ListAsync(CancellationToken.None).ConfigureAwait(false);
            XivMcpStatus = $"XivMcp connected ({connection.Via}, {tools.Count} tools).";
        }
        catch (Exception ex) when (ex is McpException or HttpRequestException or TaskCanceledException)
        {
            XivMcpStatus = $"XivMcp: {ex.Message}";
            xivTools.Dispose();
            xivTools = null;
            xivmcp.Forget();
        }

        return xivTools;
    }

    /// <summary>The chat session, created on first use; tools come from XivMcp filtered by the chosen profile.</summary>
    public ChatSession GetChatSession()
    {
        if (Session != null)
            return Session;
        Session = new ChatSession(store, NewLoop, () => string.IsNullOrWhiteSpace(settings().SystemPrompt) ? Almanac.Core.Agent.ChatSession.DefaultSystemPrompt : settings().SystemPrompt);
        return Session;
    }

    private AgentLoop NewLoop()
    {
        var s = settings();
        var (_, _, model) = ModelTarget();
        if (string.IsNullOrWhiteSpace(model))
            throw new InvalidOperationException("No model chosen. Open /almanac setup.");
        var tools = xivTools ?? XivMcpToolsAsync().GetAwaiter().GetResult();
        IReadOnlyList<IToolSource> sources = tools == null ? [] : [tools];
        IToolSource source = new ToolHub(sources, ToolProfiles.Filter(s.ToolProfile));
        return new AgentLoop(Chat(), source, new AgentOptions
        {
            Model = model,
            Mode = ToolMode(model),
            MaxSteps = s.MaxSteps,
            Temperature = s.Temperature,
            MaxTokens = s.MaxTokens,
        });
    }

    /// <summary>A benchmark runner for the current model; live mode needs XivMcp.</summary>
    public async Task<BenchRunner> BenchRunnerAsync(BenchMode mode)
    {
        var (baseUrl, _, model) = ModelTarget();
        var live = mode == BenchMode.Live ? await XivMcpToolsAsync().ConfigureAwait(false) : null;
        var backendKind = settings().Engine == AlmanacSettings.EngineAlmanac ? BackendKinds.Almanac : settings().BackendKind;
        IVramProbe vram = backendKind == BackendKinds.Ollama
            ? new FirstVramProbe(new OllamaVramProbe(quick, ServerDetector.RootOf(baseUrl)), new NvidiaSmiProbe())
            : new NvidiaSmiProbe();
        return new BenchRunner(Chat(), Suite.Bundled())
        {
            LiveTools = live,
            Vram = vram,
            ToolCalling = ToolMode(model) == ToolCallingMode.None ? ToolCallingMode.Native : ToolMode(model),
        };
    }

    /// <summary>Settings changed: rebuild clients on next use.</summary>
    public void Invalidate()
    {
        // The old tool source owns an MCP client with a semaphore in it; dropping the reference would leak it.
        xivTools?.Dispose();
        xivTools = null;
        xivModel = null;
        xivmcp.Forget();
    }

    public void Dispose()
    {
        Session?.Dispose();
        Session = null;
        xivTools?.Dispose();
        xivTools = null;
        http.Dispose();
        quick.Dispose();
    }
}
