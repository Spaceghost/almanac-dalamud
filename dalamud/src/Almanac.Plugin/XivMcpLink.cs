using System.Text.Json.Nodes;
using Dalamud.Plugin;
using Dalamud.Plugin.Services;

namespace Almanac.Plugin;

/// <summary>XivMcp's Dalamud IPC gates this plugin uses (see XivMcp's src/Shared/IpcContract.cs).</summary>
public static class XivMcpGates
{
    /// <summary>Func&lt;int&gt;; absent when XivMcp is not loaded.</summary>
    public const string ApiVersion = "XivMcp.ApiVersion";

    /// <summary>Func&lt;int&gt;: 2 or later has the gates below.</summary>
    public const string ApiRevision = "XivMcp.ApiRevision";

    /// <summary>Func&lt;string, string&gt;: client name → {endpoint, endpoints, token, clientName} or {error}. Issues a fresh per-client token.</summary>
    public const string ConnectClient = "XivMcp.ConnectClient";

    /// <summary>Func&lt;string&gt;: {configured, endpoint, model, hasApiKey}.</summary>
    public const string GetLocalModel = "XivMcp.GetLocalModel";

    /// <summary>Func&lt;string&gt;: {running, endpoint, ...}.</summary>
    public const string GetStatus = "XivMcp.GetStatus";

    /// <summary>Message: raised when XivMcp's local model settings change.</summary>
    public const string LocalModelChanged = "XivMcp.LocalModelChanged";
}

public sealed record XivMcpConnection(Uri Endpoint, string? Token, string Via);

public sealed record XivMcpLocalModel(bool Configured, string? Endpoint, string? Model, bool HasApiKey);

/// <summary>
/// Finds XivMcp: over IPC when it is loaded in this game (it hands out a named client token, so no token is ever
/// copied or stored by Almanac), or from the manual endpoint/token in settings. IPC calls run on the framework thread.
/// </summary>
public sealed class XivMcpLink(IDalamudPluginInterface pi, IFramework framework, IPluginLog log) : IDisposable
{
    public const string ClientName = "almanac-dalamud";

    private XivMcpConnection? cached;
    private Action? onLocalModelChanged;

    public string? LastError { get; private set; }

    /// <summary>XivMcp's API version, or null when it is not loaded.</summary>
    public int? ApiVersion() => Call(() => pi.GetIpcSubscriber<int>(XivMcpGates.ApiVersion).InvokeFunc());

    public int? ApiRevision() => Call(() => pi.GetIpcSubscriber<int>(XivMcpGates.ApiRevision).InvokeFunc());

    public bool Loaded => ApiVersion() != null;

    public XivMcpLocalModel? LocalModel()
    {
        var json = Call(() => pi.GetIpcSubscriber<string>(XivMcpGates.GetLocalModel).InvokeFunc());
        if (json == null)
            return null;
        try
        {
            var n = JsonNode.Parse(json);
            return new XivMcpLocalModel(
                n?["configured"]?.GetValue<bool>() ?? false,
                n?["endpoint"]?.GetValue<string>(),
                n?["model"]?.GetValue<string>(),
                n?["hasApiKey"]?.GetValue<bool>() ?? false);
        }
        catch (Exception ex) when (ex is System.Text.Json.JsonException or InvalidOperationException)
        {
            return null;
        }
    }

    /// <summary>Returns a connection, asking XivMcp for a client token over IPC when allowed.</summary>
    public async Task<XivMcpConnection?> ConnectAsync(bool viaIpc, string manualEndpoint, string manualToken, bool forceNew = false)
    {
        if (!viaIpc)
        {
            LastError = null;
            return Uri.TryCreate(manualEndpoint, UriKind.Absolute, out var uri)
                ? new XivMcpConnection(uri, string.IsNullOrWhiteSpace(manualToken) ? null : manualToken.Trim(), "manual")
                : Fail("The XivMcp endpoint is not a valid URL.");
        }

        if (cached != null && !forceNew)
            return cached;

        var json = await framework.RunOnFrameworkThread(() => Call(() => pi.GetIpcSubscriber<string, string>(XivMcpGates.ConnectClient).InvokeFunc(ClientName))).ConfigureAwait(false);
        if (json == null)
            return Fail(Loaded ? "This XivMcp version cannot connect plugins yet; update XivMcp or enter the endpoint and a client token manually." : "XivMcp is not loaded.");
        try
        {
            var n = JsonNode.Parse(json);
            if (n?["error"]?.GetValue<string>() is { } error)
                return Fail(error == "disabled" ? "XivMcp does not let plugins connect themselves (XivMcp → Settings → Local model)." : $"XivMcp: {error}");
            var endpoint = PreferLoopback(n?["endpoints"] as JsonArray) ?? n?["endpoint"]?.GetValue<string>();
            var token = n?["token"]?.GetValue<string>();
            if (endpoint == null || !Uri.TryCreate(endpoint, UriKind.Absolute, out var uri))
                return Fail("XivMcp returned no endpoint.");
            LastError = null;
            cached = new XivMcpConnection(uri, token, "ipc");
            return cached;
        }
        catch (Exception ex) when (ex is System.Text.Json.JsonException or InvalidOperationException)
        {
            return Fail("XivMcp returned an unreadable reply.");
        }
    }

    public void Forget() => cached = null;

    /// <summary>
    /// XivMcp can bind loopback and a tailnet address at once and prefers the tailnet one for remote clients. Almanac
    /// runs in the same game, so it takes the loopback endpoint whenever one is bound.
    /// </summary>
    internal static string? PreferLoopback(JsonArray? endpoints) =>
        endpoints?.Select(e => e as JsonValue)
            .Select(v => v != null && v.TryGetValue<string>(out var url) ? url : null)
            .FirstOrDefault(url => url != null && Uri.TryCreate(url, UriKind.Absolute, out var uri) && uri.IsLoopback);

    public void SubscribeLocalModelChanged(Action handler)
    {
        onLocalModelChanged = handler;
        try
        {
            pi.GetIpcSubscriber<object>(XivMcpGates.LocalModelChanged).Subscribe(handler);
        }
        catch (Exception ex)
        {
            log.Debug(ex, "XivMcp.LocalModelChanged subscribe failed");
        }
    }

    public void Dispose()
    {
        if (onLocalModelChanged == null)
            return;
        try
        {
            pi.GetIpcSubscriber<object>(XivMcpGates.LocalModelChanged).Unsubscribe(onLocalModelChanged);
        }
        catch (Exception)
        {
            // XivMcp may already be gone.
        }
    }

    private XivMcpConnection? Fail(string message)
    {
        LastError = message;
        return null;
    }

    private static T? Call<T>(Func<T> f)
    {
        try
        {
            return f();
        }
        catch (Exception)
        {
            // IpcNotReadyError / IpcTypeMismatchError: not loaded or an older XivMcp.
            return default;
        }
    }

    private static int? Call(Func<int> f)
    {
        try
        {
            return f();
        }
        catch (Exception)
        {
            return null;
        }
    }
}
