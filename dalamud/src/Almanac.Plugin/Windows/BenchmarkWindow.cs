using System.Numerics;
using System.Text.Json;
using System.Text.Json.Nodes;
using Almanac.Core.Bench;
using Almanac.Core.Setup;
using Almanac.Core.Storage;
using Dalamud.Bindings.ImGui;
using Dalamud.Interface.Colors;
using Dalamud.Interface.Utility;
using Dalamud.Interface.Windowing;
using Dalamud.Utility;

namespace Almanac.Plugin.Windows;

/// <summary>
/// Runs the ffxiv-core suite on the current model (mock tools, or live through XivMcp), stores every run in SQLite and,
/// only when the player asks and after showing the exact JSON, submits it to the community leaderboard.
/// </summary>
public sealed class BenchmarkWindow : Window, IDisposable
{
    private readonly Plugin plugin;
    private readonly Engine engine;
    private readonly AlmanacStore store;
    private readonly Suite suite = Suite.Bundled();

    private bool live;
    private CancellationTokenSource? cts;
    private Task? running;
    private BenchProgress? progress;
    private readonly List<TaskRun> finished = [];
    private string? error;
    private (long Id, JsonObject Json)? lastResult;
    private (long Id, string Text)? submitPreview;
    private string? submitMessage;
    private DeviceLink? link;
    private CancellationTokenSource? linkCts;
    private IReadOnlyList<BenchRow> history = [];
    private bool historyDirty = true;

    public BenchmarkWindow(Plugin plugin, Engine engine, AlmanacStore store)
        : base("Almanac benchmark###AlmanacBench")
    {
        this.plugin = plugin;
        this.engine = engine;
        this.store = store;
        Size = new Vector2(680, 520);
        SizeCondition = ImGuiCond.FirstUseEver;
    }

    public override void Draw()
    {
        if (historyDirty)
        {
            history = store.BenchRuns();
            historyDirty = false;
        }

        var model = engine.ModelTarget().Model;
        ImGui.TextUnformatted($"Suite {suite.Id} {suite.Version}: {suite.Tasks.Count} tasks. Model: {(model.Length > 0 ? model : "(none, run setup)")}");
        ImGui.TextDisabled("Measures tool use, answer quality, speed and VRAM. Read-only: at most it places a map flag and posts on XivMcp's agent board.");
        ImGui.BeginDisabled(running != null);
        if (ImGui.RadioButton("Mock game data (works anywhere, comparable)", !live))
            live = false;
        ImGui.SameLine();
        if (ImGui.RadioButton("Live through XivMcp", live))
            live = true;
        ImGui.EndDisabled();

        if (running == null)
        {
            ImGui.BeginDisabled(model.Length == 0);
            if (ImGui.Button("Run benchmark"))
                Start(model);
            ImGui.EndDisabled();
        }
        else if (ImGui.Button("Stop"))
        {
            cts?.Cancel();
        }

        if (progress is { } p)
        {
            ImGui.SameLine();
            ImGui.ProgressBar(p.Total == 0 ? 0 : (float)p.Done / p.Total, new Vector2(-1, 0), p.CurrentTask is { } t ? $"{p.Done}/{p.Total} {t}" : $"{p.Done}/{p.Total}");
        }

        if (error != null)
            ImGui.TextColored(ImGuiColors.DalamudRed, error);

        DrawTasks();
        DrawLastResult();
        DrawHistory();
    }

    private void Start(string model)
    {
        finished.Clear();
        error = null;
        lastResult = null;
        submitPreview = null;
        submitMessage = null;
        cts = new CancellationTokenSource();
        var token = cts.Token;
        var mode = live ? BenchMode.Live : BenchMode.Mock;
        var settings = plugin.Settings;
        running = Task.Run(async () =>
        {
            try
            {
                var runner = await engine.BenchRunnerAsync(mode).ConfigureAwait(false);
                if (mode == BenchMode.Live && runner.LiveTools == null)
                {
                    error = engine.XivMcpStatus ?? "XivMcp is not connected.";
                    return;
                }

                var reporter = new Progress<BenchProgress>(pr =>
                {
                    progress = pr;
                    if (pr.Finished is { } f)
                    {
                        lock (finished)
                            finished.Add(f);
                    }
                });
                var run = await runner.RunAsync(model, mode, null, reporter, token).ConfigureAwait(false);
                var facts = await ModelFactsAsync(settings, model, token).ConfigureAwait(false);
                var gpu = GpuInfo.Best();
                var hardware = new HardwareFacts(gpu?.Name ?? "unknown", gpu?.Vendor ?? "unknown", gpu?.VramMb ?? 0, GpuInfo.SystemRamGb(), GpuInfo.OsFamily());
                var json = Results.Build(run, hardware, facts, "almanac-dalamud", Engine.Version);
                var id = store.SaveBenchRun(json);
                store.SetCapability(engine.CapabilityKey(model), run.ToolCalling == "prompted" ? ModelCapabilities.Prompted : ModelCapabilities.Native);
                lastResult = (id, json);
                historyDirty = true;
            }
            catch (OperationCanceledException)
            {
                error = "Stopped.";
            }
            catch (Exception ex)
            {
                error = ex.Message;
            }
            finally
            {
                running = null;
                progress = null;
            }
        });
    }

    private async Task<ModelFacts> ModelFactsAsync(AlmanacSettings s, string model, CancellationToken ct)
    {
        var kind = s.Engine == AlmanacSettings.EngineAlmanac ? BackendKinds.Almanac : string.IsNullOrEmpty(s.BackendKind) ? BackendKinds.OpenAiCompatible : s.BackendKind;
        string? version = null;
        string? quant = null;
        int? context = null;
        string? family = null;
        double? paramsB = null;
        if (kind == BackendKinds.Ollama)
        {
            var root = ServerDetector.RootOf(engine.ModelTarget().BaseUrl);
            try
            {
                version = JsonNode.Parse(await engine.Quick.GetStringAsync($"{root}/api/version", ct).ConfigureAwait(false))?["version"]?.GetValue<string>();
                using var content = new StringContent(new JsonObject { ["model"] = model }.ToJsonString());
                using var r = await engine.Quick.PostAsync($"{root}/api/show", content, ct).ConfigureAwait(false);
                if (r.IsSuccessStatusCode)
                    (quant, context, family, paramsB) = ModelCapabilities.OllamaFacts(JsonNode.Parse(await r.Content.ReadAsStringAsync(ct).ConfigureAwait(false)));
            }
            catch (Exception ex) when (ex is HttpRequestException or JsonException or TaskCanceledException)
            {
            }
        }

        return new ModelFacts(kind, version, model, family, paramsB, quant ?? "unknown", context ?? 0);
    }

    private void DrawTasks()
    {
        List<TaskRun> rows;
        lock (finished)
            rows = [.. finished];
        if (rows.Count == 0)
            return;
        if (ImGui.BeginTable("##tasks", 6, ImGuiTableFlags.RowBg | ImGuiTableFlags.ScrollY | ImGuiTableFlags.SizingStretchProp, new Vector2(0, 180 * ImGuiHelpers.GlobalScale)))
        {
            ImGui.TableSetupColumn("Task");
            ImGui.TableSetupColumn("Score");
            ImGui.TableSetupColumn("Calls");
            ImGui.TableSetupColumn("TTFT");
            ImGui.TableSetupColumn("tok/s");
            ImGui.TableSetupColumn("Error");
            ImGui.TableHeadersRow();
            foreach (var r in rows)
            {
                ImGui.TableNextRow();
                ImGui.TableNextColumn();
                ImGui.TextUnformatted(r.Task.Id);
                if (r.Answer is { } answer && ImGui.IsItemHovered())
                    ImGui.SetTooltip(answer.Length > 400 ? answer[..400] + "…" : answer);
                ImGui.TableNextColumn();
                ImGui.TextColored(r.Score.Success ? ImGuiColors.HealerGreen : r.Score.Score > 0 ? ImGuiColors.DalamudYellow : ImGuiColors.DalamudRed, $"{r.Score.Score:0.00}");
                ImGui.TableNextColumn();
                ImGui.TextUnformatted($"{r.Score.ValidCalls}/{r.Score.TotalCalls}");
                ImGui.TableNextColumn();
                ImGui.TextUnformatted(r.TtftMs is { } t ? $"{t:0} ms" : "—");
                ImGui.TableNextColumn();
                ImGui.TextUnformatted(r.TokensPerSecond is { } tps ? $"{tps:0.0}" : "—");
                ImGui.TableNextColumn();
                ImGui.TextUnformatted(r.Score.Error ?? "");
            }

            ImGui.EndTable();
        }
    }

    private void DrawLastResult()
    {
        if (lastResult is not { } result)
            return;
        var m = result.Json["metrics"]!;
        ImGui.Separator();
        ImGui.TextColored(ImGuiColors.DalamudViolet, $"Score {m["score"]} / 100");
        ImGui.SameLine();
        ImGui.TextUnformatted($"· success {m["success_rate"]!.GetValue<double>():P0} · valid calls {m["tool_call_validity"]!.GetValue<double>():P0} · {m["tokens_per_s"]} tok/s · TTFT {m["ttft_ms"]} ms · peak VRAM {(m["peak_vram_mb"] is { } v ? $"{v} MB" : "n/a")} · tools {result.Json["model"]!["tool_calling"]}");

        if (submitPreview == null && ImGui.Button("Share with the community leaderboard…"))
            submitPreview = (result.Id, result.Json.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
        DrawSubmit();
    }

    private void DrawSubmit()
    {
        if (submitPreview is not { } preview)
        {
            if (submitMessage != null)
                ImGui.TextUnformatted(submitMessage);
            return;
        }

        ImGui.TextWrapped($"This exact JSON will be sent to {plugin.Settings.LeaderboardUrl.TrimEnd('/')}/api/results. It has no names, paths or addresses: GPU model and VRAM, rounded RAM, OS family, backend, model and scores.");
        var text = preview.Text;
        ImGui.InputTextMultiline("##preview", ref text, text.Length + 1, new Vector2(-1, 160 * ImGuiHelpers.GlobalScale), ImGuiInputTextFlags.ReadOnly);
        if (link != null)
        {
            DrawLink();
            return;
        }

        var signedIn = plugin.Settings.LeaderboardToken.Length > 0;
        if (!signedIn)
            ImGui.TextWrapped("The leaderboard takes results from signed-in players. Send opens spacegho.st in your browser, where you sign in with GitHub or XIVAuth and approve Almanac; the result is sent once you have.");
        if (ImGui.Button(signedIn ? "Send it" : "Sign in and send"))
        {
            var row = store.BenchRuns().FirstOrDefault(r => r.Id == preview.Id);
            if (row != null)
                Send(row);
            if (link == null)
                submitPreview = null;
        }

        ImGui.SameLine();
        if (ImGui.Button("Cancel"))
            submitPreview = null;
        if (signedIn)
        {
            ImGui.SameLine();
            if (ImGui.SmallButton("Sign out"))
                SignOut();
        }
    }

    /// <summary>The wait for the player to approve in the browser. The poll runs on the thread pool; this only reads its state.</summary>
    private void DrawLink()
    {
        if (link?.Prompt is { } prompt)
        {
            ImGui.TextUnformatted("Your code:");
            ImGui.SameLine();
            ImGui.TextColored(ImGuiColors.DalamudViolet, prompt.UserCode);
            ImGui.TextWrapped($"Approve Almanac at {prompt.VerificationUri} — it should have opened in your browser.");
            if (ImGui.Button("Open the page again"))
                Util.OpenLink(prompt.OpenUri);
            ImGui.SameLine();
        }

        if (ImGui.Button("Cancel##link"))
            linkCts?.Cancel();
        ImGui.TextDisabled(link?.Message ?? "");
    }

    private void Send(BenchRow row)
    {
        var settings = plugin.Settings;
        var url = settings.LeaderboardUrl;
        var result = JsonNode.Parse(row.ResultJson)!.AsObject();
        var token = settings.LeaderboardToken;
        DeviceLink? linking = null;
        CancellationTokenSource? linkingCts = null;
        if (token.Length == 0)
        {
            linkCts?.Dispose();
            linkCts = linkingCts = new CancellationTokenSource();
            link = linking = new DeviceLink(engine.Quick, settings.SignInUrl);
            submitMessage = null;
        }
        else
        {
            submitMessage = "Sending…";
        }

        _ = Task.Run(async () =>
        {
            try
            {
                if (linking != null)
                {
                    var linked = await linking.RunAsync(prompt => Util.OpenLink(prompt.OpenUri), linkingCts!.Token).ConfigureAwait(false);
                    if (linked.AccessToken is not { } fresh)
                    {
                        submitMessage = linked.Message;
                        return;
                    }

                    token = settings.LeaderboardToken = fresh;
                    settings.Save(store);
                    submitMessage = "Signed in. Sending…";
                }

                var sent = await Results.SubmitAsync(engine.Quick, url, result, token, CancellationToken.None).ConfigureAwait(false);
                if (sent.Ok)
                    store.MarkSubmitted(row.Id);
                if (sent.Outcome == SubmitOutcome.SignInRequired)
                {
                    // The token is no good: forget it, so the next Send links again. Never retried here by itself.
                    settings.LeaderboardToken = "";
                    settings.Save(store);
                    submitMessage = $"{sent.Message} Press Share to sign in again.";
                }
                else
                {
                    submitMessage = sent.Message;
                }

                historyDirty = true;
            }
            catch (OperationCanceledException)
            {
                submitMessage = "Sign-in cancelled. Nothing was sent.";
            }
            catch (Exception ex)
            {
                submitMessage = ex.Message;
            }
            finally
            {
                if (linking != null && ReferenceEquals(link, linking))
                {
                    link = null;
                    submitPreview = null;
                }
            }
        });
    }

    private void SignOut()
    {
        var settings = plugin.Settings;
        var token = settings.LeaderboardToken;
        settings.LeaderboardToken = "";
        settings.Save(store);
        submitMessage = "Signed out.";
        var revoke = new DeviceLink(engine.Quick, settings.SignInUrl);
        _ = Task.Run(() => revoke.RevokeAsync(token, CancellationToken.None));
    }

    private void DrawHistory()
    {
        if (history.Count == 0 || !ImGui.CollapsingHeader($"Past runs ({history.Count})"))
            return;
        foreach (var row in history)
        {
            ImGui.TextUnformatted($"{row.CreatedAt.LocalDateTime:g}  {row.Model}  {row.Mode}  suite {row.SuiteVersion}  score {row.Score:0.0}{(row.Submitted ? "  (shared)" : "")}");
            if (!row.Submitted && running == null)
            {
                ImGui.SameLine();
                if (ImGui.SmallButton($"Share…##{row.Id}"))
                    submitPreview = (row.Id, JsonNode.Parse(row.ResultJson)!.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
            }
        }

        DrawSubmitIfNoLast();
    }

    private void DrawSubmitIfNoLast()
    {
        if (lastResult == null)
            DrawSubmit();
    }

    public void Dispose()
    {
        cts?.Cancel();
        cts?.Dispose();
        linkCts?.Cancel();
        linkCts?.Dispose();
    }
}
