using System.Net;
using System.Text.Json.Nodes;
using Almanac.Core.Bench;
using Almanac.Core.Storage;

namespace Almanac.Core.Tests;

public sealed class DeviceLinkTests
{
    private const string Api = "https://link.test/api";
    private const string Token = "gvt_ttttttttttttttttttttttttttttttttttttttttttt";
    private const string DeviceCode = "dc_dddddddddddddddddddddddddddddddd";

    private static HttpResponseMessage Fail(HttpStatusCode status, string error, string? message = null) =>
        FakeHandler.Json(new JsonObject { ["ok"] = false, ["error"] = error, ["message"] = message ?? $"server says {error}" }.ToJsonString(), status);

    /// <summary>The sign-in server: a code, then the scripted poll answers, then the token.</summary>
    private sealed class Site(params HttpResponseMessage[] polls)
    {
        private readonly Queue<HttpResponseMessage> polls = new(polls);

        public int ExpiresIn { get; init; } = 900;

        public List<string?> Authorization { get; } = [];

        public HttpResponseMessage Respond(HttpRequestMessage request, string body)
        {
            var url = request.RequestUri!.ToString();
            Assert.DoesNotContain(Token, url, StringComparison.Ordinal);
            Assert.DoesNotContain(DeviceCode, url, StringComparison.Ordinal);
            Assert.False(request.Headers.Contains("Origin"));
            Authorization.Add(request.Headers.Authorization?.ToString());
            if (url == $"{Api}/device/code")
            {
                Assert.True(JsonNode.DeepEquals(JsonNode.Parse(body), new JsonObject { ["client_id"] = "almanac", ["scope"] = "almanac:submit" }));
                return FakeHandler.Json(new JsonObject
                {
                    ["device_code"] = DeviceCode,
                    ["user_code"] = "BCDF-GHJK",
                    ["verification_uri"] = "https://link.test/apps",
                    ["verification_uri_complete"] = "https://link.test/apps?code=BCDF-GHJK",
                    ["expires_in"] = ExpiresIn,
                    ["interval"] = 5,
                }.ToJsonString());
            }

            if (url == $"{Api}/device/token")
            {
                Assert.True(JsonNode.DeepEquals(JsonNode.Parse(body), new JsonObject { ["client_id"] = "almanac", ["device_code"] = DeviceCode }));
                return this.polls.Count > 0
                    ? this.polls.Dequeue()
                    : FakeHandler.Json(new JsonObject { ["access_token"] = Token, ["token_type"] = "Bearer", ["expires_in"] = 15552000, ["scope"] = "almanac:submit", ["token_id"] = "1" }.ToJsonString());
            }

            return url == $"{Api}/token/revoke" ? FakeHandler.Json("""{"ok":true}""") : new HttpResponseMessage(HttpStatusCode.NotFound);
        }
    }

    private static (DeviceLink Link, FakeHandler Handler, List<double> Waits) Make(Site site)
    {
        var handler = new FakeHandler(site.Respond);
        var waits = new List<double>();
        var link = new DeviceLink(new HttpClient(handler), Api, (t, _) =>
        {
            waits.Add(t.TotalSeconds);
            return Task.CompletedTask;
        });
        return (link, handler, waits);
    }

    [Fact]
    public async Task LinkShowsTheCodeAndReturnsTheToken()
    {
        var (link, handler, waits) = Make(new Site());
        Assert.Equal(LinkState.Idle, link.State);
        LinkPrompt? shown = null;
        var result = await link.RunAsync(p => shown = p, TestContext.Current.CancellationToken);

        Assert.Equal(LinkState.Linked, result.State);
        Assert.Equal(Token, result.AccessToken);
        Assert.Equal(LinkState.Linked, link.State);
        Assert.Equal(new LinkPrompt("BCDF-GHJK", "https://link.test/apps", "https://link.test/apps?code=BCDF-GHJK"), shown);
        Assert.Same(shown, link.Prompt);
        Assert.Equal([5.0], waits);
        Assert.Equal([$"{Api}/device/code", $"{Api}/device/token"], handler.Requests.Select(r => r.Url));
        // Neither secret reaches anything a log or a window would print.
        foreach (var text in new[] { result.ToString(), result.Message, link.Message, shown!.ToString() })
        {
            Assert.DoesNotContain(Token, text, StringComparison.Ordinal);
            Assert.DoesNotContain(DeviceCode, text, StringComparison.Ordinal);
        }
    }

    [Fact]
    public async Task PendingThenSlowDownThenSuccess()
    {
        var (link, _, waits) = Make(new Site(
            Fail(HttpStatusCode.BadRequest, "authorization_pending"),
            Fail(HttpStatusCode.BadRequest, "slow_down"),
            Fail(HttpStatusCode.BadRequest, "authorization_pending")));
        var result = await link.RunAsync(null, TestContext.Current.CancellationToken);
        Assert.Equal(LinkState.Linked, result.State);
        Assert.Equal([5.0, 5.0, 10.0, 10.0], waits);
    }

    [Theory]
    [InlineData("access_denied", LinkState.Denied)]
    [InlineData("expired_token", LinkState.Expired)]
    [InlineData("invalid_grant", LinkState.Failed)]
    public async Task AFinalAnswerStopsThePollWithTheServersMessage(string error, LinkState expected)
    {
        var (link, handler, _) = Make(new Site(Fail(HttpStatusCode.BadRequest, "authorization_pending"), Fail(HttpStatusCode.BadRequest, error, $"no: {error}")));
        var result = await link.RunAsync(null, TestContext.Current.CancellationToken);
        Assert.Equal(expected, result.State);
        Assert.Equal($"no: {error}", result.Message);
        Assert.Equal($"no: {error}", link.Message);
        Assert.Null(result.AccessToken);
        Assert.Equal(3, handler.Requests.Count);
    }

    [Fact]
    public async Task GivesUpWhenTheCodeExpires()
    {
        var pending = Enumerable.Range(0, 50).Select(_ => Fail(HttpStatusCode.BadRequest, "authorization_pending")).ToArray();
        var (link, _, waits) = Make(new Site(pending) { ExpiresIn = 20 });
        var result = await link.RunAsync(null, TestContext.Current.CancellationToken);
        Assert.Equal(LinkState.Expired, result.State);
        Assert.True(waits.Sum() < 20);
    }

    [Fact]
    public async Task ARefusedCodeRequestShowsTheServersMessage()
    {
        var handler = new FakeHandler((_, _) => Fail(HttpStatusCode.TooManyRequests, "busy", "try again in an hour"));
        var link = new DeviceLink(new HttpClient(handler), Api, (_, _) => Task.CompletedTask);
        var result = await link.RunAsync(_ => Assert.Fail("no code to show"), TestContext.Current.CancellationToken);
        Assert.Equal(new LinkResult(LinkState.Failed, "try again in an hour"), result);
        Assert.Null(link.Prompt);
    }

    [Fact]
    public async Task OnlyWebAddressesAreOfferedToTheBrowser()
    {
        var handler = new FakeHandler((_, _) => FakeHandler.Json("""{"device_code":"d","user_code":"BCDF-GHJK","verification_uri":"file:///etc/passwd","interval":5,"expires_in":900}"""));
        var link = new DeviceLink(new HttpClient(handler), Api, (_, _) => Task.CompletedTask);
        var result = await link.RunAsync(_ => Assert.Fail("nothing safe to open"), TestContext.Current.CancellationToken);
        Assert.Equal(LinkState.Failed, result.State);
    }

    [Fact]
    public async Task CancellingStopsTheWait()
    {
        var site = new Site(Fail(HttpStatusCode.BadRequest, "authorization_pending"));
        using var cts = new CancellationTokenSource();
        var link = new DeviceLink(new HttpClient(new FakeHandler(site.Respond)), Api, (_, ct) =>
        {
            cts.Cancel();
            return Task.FromCanceled(ct);
        });
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => link.RunAsync(null, cts.Token));
    }

    [Fact]
    public async Task RevokeSendsTheTokenInTheHeaderOnly()
    {
        var site = new Site();
        var (link, handler, _) = Make(site);
        Assert.True(await link.RevokeAsync(Token, TestContext.Current.CancellationToken));
        Assert.Equal(($"{Api}/token/revoke", ""), handler.Requests.Single());
        Assert.Equal([$"Bearer {Token}"], site.Authorization);
    }

    [Fact]
    public async Task SubmitSendsTheBearerToken()
    {
        string? authorization = null;
        var handler = new FakeHandler((request, _) =>
        {
            authorization = request.Headers.Authorization?.ToString();
            return FakeHandler.Json("""{"ok":true}""", HttpStatusCode.Created);
        });
        var sent = await Results.SubmitAsync(new HttpClient(handler), "https://board.test/almanac/", new JsonObject { ["schema_version"] = 1 }, Token, TestContext.Current.CancellationToken);
        Assert.Equal(SubmitOutcome.Ok, sent.Outcome);
        Assert.Equal($"Bearer {Token}", authorization);
        Assert.Equal("https://board.test/almanac/api/results", handler.Requests.Single().Url);
    }

    [Theory]
    [InlineData(401, "sign_in_required", SubmitOutcome.SignInRequired)]
    [InlineData(401, "invalid_token", SubmitOutcome.SignInRequired)]
    [InlineData(401, "token_revoked", SubmitOutcome.SignInRequired)]
    [InlineData(401, "token_expired", SubmitOutcome.SignInRequired)]
    [InlineData(403, "account_banned", SubmitOutcome.Forbidden)]
    [InlineData(403, "insufficient_scope", SubmitOutcome.Forbidden)]
    [InlineData(429, "slow_down", SubmitOutcome.Rejected)]
    public async Task ARefusalCarriesTheServersMessageAndIsNotRetried(int status, string error, SubmitOutcome expected)
    {
        var handler = new FakeHandler((_, _) => Fail((HttpStatusCode)status, error, $"the server said {error}"));
        var sent = await Results.SubmitAsync(new HttpClient(handler), "https://board.test", new JsonObject(), Token, TestContext.Current.CancellationToken);
        Assert.Equal(new SubmitResult(expected, $"the server said {error}"), sent);
        Assert.False(sent.Ok);
        Assert.Single(handler.Requests);
    }

    [Fact]
    public async Task ARefusalWithoutAMessageStillSaysSomethingAndNeverEchoesTheBody()
    {
        var handler = new FakeHandler((_, _) => FakeHandler.Json($$"""{"access_token":"{{Token}}"}""", HttpStatusCode.BadGateway));
        var sent = await Results.SubmitAsync(new HttpClient(handler), "https://board.test", new JsonObject(), Token, TestContext.Current.CancellationToken);
        Assert.Equal(new SubmitResult(SubmitOutcome.Rejected, "HTTP 502"), sent);
    }

    [Fact]
    public void TheTokenIsKeptInSettingsAndCanBeForgotten()
    {
        using var store = AlmanacStore.InMemory();
        var settings = AlmanacSettings.Load(store);
        Assert.Equal("", settings.LeaderboardToken);
        Assert.Equal(DeviceLink.DefaultApi, settings.SignInUrl);
        settings.LeaderboardToken = Token;
        settings.Save(store);
        Assert.Equal(Token, AlmanacSettings.Load(store).LeaderboardToken);
        settings.LeaderboardToken = "";
        settings.Save(store);
        Assert.Equal("", AlmanacSettings.Load(store).LeaderboardToken);
    }
}
