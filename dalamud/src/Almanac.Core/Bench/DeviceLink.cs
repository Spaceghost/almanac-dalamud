using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Almanac.Core.Bench;

/// <summary>Where a device link stands. Everything after <see cref="Waiting"/> is final.</summary>
public enum LinkState
{
    Idle,
    Requesting,
    Waiting,
    Linked,
    Denied,
    Expired,
    Failed,
}

/// <summary>What the player has to see to approve the link. The device code itself stays inside <see cref="DeviceLink"/>.</summary>
public sealed record LinkPrompt(string UserCode, string VerificationUri, string OpenUri);

/// <summary>How a link ended. <see cref="AccessToken"/> is a secret: it is set only for <see cref="LinkState.Linked"/> and never printed.</summary>
public sealed record LinkResult(LinkState State, string Message, string? AccessToken = null)
{
    public override string ToString() => $"{State}: {Message}";
}

/// <summary>
/// Device-link sign-in for leaderboard submissions (RFC 8628 in shape): ask for a code, show it, poll until the player
/// approves in the browser. The device code and the access token travel only in request bodies and the Authorization
/// header, and are never logged or put in a URL.
/// </summary>
public sealed class DeviceLink(HttpClient http, string api = DeviceLink.DefaultApi, Func<TimeSpan, CancellationToken, Task>? delay = null)
{
    public const string DefaultApi = "https://spacegho.st/mods/ffxiv/term/vote/api";
    public const string ClientId = "almanac";
    public const string Scope = "almanac:submit";

    private readonly string api = api.TrimEnd('/');
    private readonly Func<TimeSpan, CancellationToken, Task> delay = delay ?? Task.Delay;
    private volatile LinkPrompt? prompt;
    private volatile string message = "";
    private int state;

    public LinkState State => (LinkState)Volatile.Read(ref state);

    /// <summary>Set once the server has issued a code, for the UI to show.</summary>
    public LinkPrompt? Prompt => prompt;

    /// <summary>The last thing worth showing: the server's message on a failure.</summary>
    public string Message => message;

    /// <summary>Runs the whole flow. <paramref name="onPrompt"/> is called once, when there is a code to show and a page to open.</summary>
    public async Task<LinkResult> RunAsync(Action<LinkPrompt>? onPrompt, CancellationToken ct)
    {
        Set(LinkState.Requesting, "Asking spacegho.st for a code…");
        JsonObject grant;
        try
        {
            using var response = await PostAsync("device/code", new JsonObject { ["client_id"] = ClientId, ["scope"] = Scope }, null, ct).ConfigureAwait(false);
            var body = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
                return End(LinkState.Failed, ServerMessage(body, (int)response.StatusCode).Message);
            grant = Parse(body) ?? [];
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException && !ct.IsCancellationRequested)
        {
            return End(LinkState.Failed, $"Could not reach the sign-in server: {ex.Message}");
        }

        var deviceCode = Text(grant, "device_code");
        var userCode = Text(grant, "user_code");
        var where = Text(grant, "verification_uri");
        if (deviceCode.Length == 0 || userCode.Length == 0 || !IsWebAddress(where))
            return End(LinkState.Failed, "The sign-in server sent an answer this version does not understand.");
        var complete = Text(grant, "verification_uri_complete");
        var interval = TimeSpan.FromSeconds(Math.Clamp(Number(grant, "interval", 5), 1, 60));
        var expires = TimeSpan.FromSeconds(Math.Clamp(Number(grant, "expires_in", 900), 1, 3600));

        prompt = new LinkPrompt(userCode, where, IsWebAddress(complete) ? complete : where);
        Set(LinkState.Waiting, "Waiting for you to approve Almanac in the browser…");
        onPrompt?.Invoke(prompt);

        var waited = TimeSpan.Zero;
        while (true)
        {
            if (waited + interval >= expires)
                return End(LinkState.Expired, "The code expired before it was approved. Press Send again for a new one.");
            await delay(interval, ct).ConfigureAwait(false);
            waited += interval;
            string body;
            int status;
            try
            {
                using var response = await PostAsync("device/token", new JsonObject { ["client_id"] = ClientId, ["device_code"] = deviceCode }, null, ct).ConfigureAwait(false);
                body = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
                status = (int)response.StatusCode;
            }
            catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException && !ct.IsCancellationRequested)
            {
                continue; // a dropped poll is not an answer; the expiry above still ends the wait
            }

            if (status == 200)
            {
                var token = Text(Parse(body) ?? [], "access_token");
                return token.Length > 0
                    ? End(LinkState.Linked, "Signed in.", token)
                    : End(LinkState.Failed, "The sign-in server sent an answer this version does not understand.");
            }

            var (error, text) = ServerMessage(body, status);
            switch (error)
            {
                case "authorization_pending":
                    continue;
                case "slow_down":
                    interval += TimeSpan.FromSeconds(5);
                    continue;
                case "access_denied":
                    return End(LinkState.Denied, text);
                case "expired_token":
                    return End(LinkState.Expired, text);
                default:
                    return End(LinkState.Failed, text);
            }
        }
    }

    /// <summary>Sign out: tells the server to forget the token. The caller deletes its copy whatever this returns.</summary>
    public async Task<bool> RevokeAsync(string token, CancellationToken ct)
    {
        try
        {
            using var response = await PostAsync("token/revoke", null, token, ct).ConfigureAwait(false);
            return response.IsSuccessStatusCode;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            return false;
        }
    }

    /// <summary>(error, message) from a failure body. Never the raw body, which on the token endpoint could hold a token.</summary>
    public static (string Error, string Message) ServerMessage(string body, int status)
    {
        var json = Parse(body) ?? [];
        var error = Text(json, "error");
        var text = Text(json, "message");
        if (text.Length > 300)
            text = text[..300];
        return (error, text.Length > 0 ? text : error.Length > 0 ? $"HTTP {status}: {error}" : $"HTTP {status}");
    }

    private async Task<HttpResponseMessage> PostAsync(string path, JsonObject? body, string? bearer, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, $"{api}/{path}");
        if (body != null)
            request.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
        if (bearer != null)
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", bearer);
        return await http.SendAsync(request, ct).ConfigureAwait(false);
    }

    private LinkResult End(LinkState to, string text, string? token = null)
    {
        Set(to, text);
        return new LinkResult(to, text, token);
    }

    private void Set(LinkState to, string text)
    {
        message = text;
        Volatile.Write(ref state, (int)to);
    }

    private static JsonObject? Parse(string body)
    {
        try
        {
            return JsonNode.Parse(body) as JsonObject;
        }
        catch (JsonException)
        {
            return null;
        }
    }

    private static string Text(JsonObject json, string key) =>
        json[key] is JsonValue v && v.TryGetValue<string>(out var s) ? s.Trim() : "";

    private static double Number(JsonObject json, string key, double fallback) =>
        json[key] is JsonValue v && v.TryGetValue<double>(out var d) && double.IsFinite(d) ? d : fallback;

    /// <summary>Only http(s) addresses are ever handed to the system's browser.</summary>
    private static bool IsWebAddress(string s) =>
        Uri.TryCreate(s, UriKind.Absolute, out var uri) && (uri.Scheme == Uri.UriSchemeHttps || uri.Scheme == Uri.UriSchemeHttp);
}
