using System.Numerics;
using System.Text.Json.Nodes;
using Almanac.Core.Llm;
using Almanac.Core.Setup;
using Almanac.Core.Storage;
using Dalamud.Bindings.ImGui;
using Dalamud.Interface.Colors;
using Dalamud.Interface.Utility;
using Dalamud.Interface.Windowing;

namespace Almanac.Plugin.Windows;

/// <summary>
/// First-run wizard: 1 find a model server, 2 read the GPU and recommend models for its VRAM, 3 pick a model and
/// check its tool calling, 4 connect to XivMcp. Every network call runs off the framework thread.
/// </summary>
public sealed class SetupWindow : Window, IDisposable
{
    /// <summary>The almanac gateway maps "local*" to its configured default model.</summary>
    private const string AlmanacDefaultModel = "local";

    private static readonly string[] Steps = ["Model server", "Your GPU", "Model", "XivMcp"];

    private readonly Plugin plugin;
    private readonly Engine engine;
    private readonly AlmanacStore store;
    private readonly XivMcpLink xivmcp;
    private readonly CancellationTokenSource cts = new();

    private int step;
    private Task<IReadOnlyList<DetectedServer>>? detecting;
    private IReadOnlyList<DetectedServer> servers = [];
    private DetectedServer? server;
    private string customUrl = "";
    private string customKey = "";
    private string? probeMessage;
    private bool useAlmanac;
    private string almanacUrl = "";
    private string almanacToken = "";

    private IReadOnlyList<GpuAdapter> adapters = [];
    private Task<(IReadOnlyList<GpuAdapter> Adapters, int Best)>? detectingGpu;
    private int adapterIndex;
    private int manualVramGb;
    private Recommendations? recommendations;
    private Task<Recommendations>? loadingRecommendations;

    private int modelIndex;
    private Task<string>? probing;
    private string? probeModel;
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, string> ollamaCaps = new(StringComparer.Ordinal);

    private Task? connecting;

    // The almanac engine's own view of its GPU (it may be another machine than the game's).
    private Task<ServerGpu?>? readingServerGpu;
    private ServerGpu? serverGpu;

    // One-click install of a recommended model into Ollama.
    private Task? pulling;
    private string? pullModel;
    private volatile PullProgress? pullProgress;
    private string? pullMessage;

    public SetupWindow(Plugin plugin, Engine engine, AlmanacStore store, XivMcpLink xivmcp)
        : base("Almanac setup###AlmanacSetup")
    {
        this.plugin = plugin;
        this.engine = engine;
        this.store = store;
        this.xivmcp = xivmcp;
        Size = new Vector2(640, 500);
        SizeCondition = ImGuiCond.FirstUseEver;
    }

    public override void OnOpen()
    {
        step = 0;
        var s = plugin.Settings;
        customUrl = s.BaseUrl;
        customKey = s.ApiKey;
        useAlmanac = s.Engine == AlmanacSettings.EngineAlmanac;
        almanacUrl = s.AlmanacUrl;
        almanacToken = s.AlmanacToken;
        // Creating a Vulkan instance and a DXGI factory takes long enough to hitch a frame: keep it off the draw thread.
        detectingGpu = Task.Run(() =>
        {
            var found = GpuInfo.Adapters();
            return (found, Math.Max(0, GpuInfo.BestIndex(found, GpuInfo.GameAdapterName())));
        });
        loadingRecommendations ??= new RecommendationSource(engine.Quick, $"{s.LeaderboardUrl.TrimEnd('/')}/recommendations.json",
            () => store.Get("cache.recommendations"), v => store.Set("cache.recommendations", v)).LoadAsync(cts.Token);
        Detect();
    }

    public override void Draw()
    {
        // Step header.
        for (var i = 0; i < Steps.Length; i++)
        {
            if (i > 0)
                ImGui.SameLine();
            var label = $"{i + 1}. {Steps[i]}";
            if (i == step)
                ImGui.TextColored(ImGuiColors.DalamudViolet, label);
            else
                ImGui.TextDisabled(label);
        }

        ImGui.Separator();
        var footer = ImGui.GetFrameHeightWithSpacing() * 1.4f;
        if (ImGui.BeginChild("##step", new Vector2(0, -footer)))
        {
            ImGui.PushTextWrapPos(0);
            switch (step)
            {
                case 0: DrawServer(); break;
                case 1: DrawGpu(); break;
                case 2: DrawModel(); break;
                default: DrawXivMcp(); break;
            }

            ImGui.PopTextWrapPos();
        }

        ImGui.EndChild();
        ImGui.Separator();
        ImGui.BeginDisabled(step == 0);
        if (ImGui.Button("Back"))
            step--;
        ImGui.EndDisabled();
        ImGui.SameLine();
        if (step < Steps.Length - 1)
        {
            ImGui.BeginDisabled(!CanAdvance());
            if (ImGui.Button("Next"))
                Advance();
            ImGui.EndDisabled();
        }
        else if (ImGui.Button("Finish and open the chat"))
        {
            plugin.Settings.SetupComplete = true;
            plugin.SaveSettings();
            IsOpen = false;
            plugin.OpenChat();
        }
    }

    // ---- 1: server ------------------------------------------------------------------------------

    private void Detect()
    {
        if (detecting is { IsCompleted: false })
            return;
        detecting = new ServerDetector(engine.Quick).DetectAllAsync(null, cts.Token);
    }

    private void DrawServer()
    {
        ImGui.TextUnformatted("Almanac talks to a model server running on this PC. It works with Ollama, LM Studio, a llama.cpp server or anything with an OpenAI-compatible API.");
        ImGui.Spacing();
        if (detecting is { IsCompleted: true } done)
        {
            servers = done.IsCompletedSuccessfully ? done.Result : [];
            detecting = null;
            server ??= servers.FirstOrDefault(s => s.BaseUrl == plugin.Settings.BaseUrl) ?? servers.FirstOrDefault();
        }

        if (detecting != null)
            ImGui.TextDisabled("Looking on the usual ports…");
        else if (servers.Count == 0)
            ImGui.TextColored(ImGuiColors.DalamudOrange, "No model server found. Install Ollama (ollama.com) or LM Studio (lmstudio.ai), start it, then press Detect again.");

        foreach (var s in servers)
        {
            if (ImGui.RadioButton($"{s.Label} at {s.BaseUrl} — {s.Models.Count} model(s){(s.Version != null ? $", v{s.Version}" : "")}", !useAlmanac && server == s))
            {
                server = s;
                useAlmanac = false;
            }
        }

        if (ImGui.Button("Detect again"))
            Detect();

        ImGui.Spacing();
        ImGui.TextDisabled("Somewhere else? Any OpenAI-compatible base URL (ending in /v1):");
        ImGui.SetNextItemWidth(320 * ImGuiHelpers.GlobalScale);
        ImGui.InputText("URL##custom", ref customUrl, 256);
        ImGui.SetNextItemWidth(320 * ImGuiHelpers.GlobalScale);
        ImGui.InputText("API key (optional)##custom", ref customKey, 256, ImGuiInputTextFlags.Password);
        if (ImGui.Button("Check this URL"))
        {
            probeMessage = "Checking…";
            var url = customUrl;
            var key = customKey;
            _ = Task.Run(async () =>
            {
                var found = await new ServerDetector(engine.Quick).ProbeAsync(url, key, cts.Token).ConfigureAwait(false);
                if (found == null)
                {
                    probeMessage = "Nothing OpenAI-compatible answered there.";
                    return;
                }

                servers = [.. servers.Where(s => s.BaseUrl != found.BaseUrl), found];
                server = found;
                useAlmanac = false;
                probeMessage = $"Found {found.Label} with {found.Models.Count} model(s).";
            });
        }

        if (probeMessage != null)
        {
            ImGui.SameLine();
            ImGui.TextDisabled(probeMessage);
        }

        ImGui.Spacing();
        ImGui.Separator();
        if (ImGui.RadioButton("Use an almanac engine (power users)", useAlmanac))
            useAlmanac = true;
        ImGui.TextDisabled("The almanac engine (Python, same repository) serves an OpenAI-compatible gateway with model residency and a memory guard.");
        if (useAlmanac)
        {
            ImGui.SetNextItemWidth(320 * ImGuiHelpers.GlobalScale);
            ImGui.InputText("Gateway URL", ref almanacUrl, 256);
            ImGui.SetNextItemWidth(320 * ImGuiHelpers.GlobalScale);
            ImGui.InputText("Token", ref almanacToken, 256, ImGuiInputTextFlags.Password);
            ImGui.TextDisabled("The token is in ~/.config/almanac/token on the machine running almanac.");
        }
    }

    // ---- 2: GPU ---------------------------------------------------------------------------------

    private void DrawGpu()
    {
        var s = plugin.Settings;
        if (useAlmanac && DrawServerGpu())
        {
            DrawRecommendations();
            return;
        }

        if (detectingGpu is { IsCompleted: true } done)
        {
            if (done.IsCompletedSuccessfully)
                (adapters, adapterIndex) = done.Result;
            detectingGpu = null;
        }

        if (detectingGpu != null)
        {
            ImGui.TextDisabled("Reading your GPUs...");
            return;
        }

        if (adapters.Count == 0)
        {
            ImGui.TextColored(ImGuiColors.DalamudOrange, "Could not read your GPU. Enter its video memory:");
            ImGui.SetNextItemWidth(120);
            ImGui.InputInt("GB of VRAM", ref manualVramGb);
            manualVramGb = Math.Clamp(manualVramGb, 0, 256);
        }
        else
        {
            for (var i = 0; i < adapters.Count; i++)
            {
                if (ImGui.RadioButton($"{adapters[i].Name} — {adapters[i].VramMb / 1024.0:0.#} GB##gpu{i}", adapterIndex == i))
                    adapterIndex = i;
            }

            ImGui.TextDisabled("Pick the GPU the model server runs on.");
        }

        var same = s.GameOnSameGpu;
        if (ImGui.Checkbox("The game runs on this GPU too", ref same))
            s.GameOnSameGpu = same;
        if (s.GameOnSameGpu)
        {
            var reserveGb = s.GameReserveMb / 1024f;
            ImGui.SetNextItemWidth(200 * ImGuiHelpers.GlobalScale);
            if (ImGui.SliderFloat("GB kept for the game", ref reserveGb, 1, 8, "%.1f"))
                s.GameReserveMb = (int)(reserveGb * 1024);
        }

        var budget = Budget();
        ImGui.Spacing();
        ImGui.TextColored(ImGuiColors.DalamudViolet, $"About {budget / 1024.0:0.#} GB of VRAM for the model.");
        DrawRecommendations();
    }

    /// <summary>
    /// The engine's GPU, as it reports it: free VRAM now, what a game reserved, and which model "auto" would pick.
    /// False while there is nothing to show (still asking, or an engine without the report): the local view is used.
    /// </summary>
    private bool DrawServerGpu()
    {
        if (readingServerGpu == null && serverGpu == null)
            readingServerGpu = ServerGpu.ReadAsync(engine.Quick, ServerDetector.NormalizeBase(almanacUrl), almanacToken, cts.Token);
        if (readingServerGpu is { IsCompleted: true } read)
        {
            serverGpu = read.IsCompletedSuccessfully ? read.Result : null;
            readingServerGpu = null;
            if (serverGpu == null)
                readingServerGpu = Task.FromResult<ServerGpu?>(null); // asked once; fall back to the local view
        }

        if (serverGpu is not { } g)
        {
            if (readingServerGpu is { IsCompleted: false })
                ImGui.TextDisabled("Asking the almanac engine about its GPU…");
            return false;
        }

        ImGui.TextUnformatted($"The almanac engine runs its models on {g.Name}: {g.TotalMb / 1024.0:0.#} GB, {g.FreeMb / 1024.0:0.#} GB free now.");
        foreach (var (model, mb) in g.Loaded)
            ImGui.TextDisabled($"Loaded: {model} ({mb / 1024.0:0.#} GB)");
        foreach (var (owner, mb) in g.Reservations)
            ImGui.TextDisabled($"Reserved by {owner}: {mb / 1024.0:0.#} GB");
        if (g.AutoChoice != null)
        {
            ImGui.TextColored(ImGuiColors.HealerGreen, $"Model \"auto\" picks {g.AutoChoice} right now.");
            ImGui.TextDisabled(g.AutoReason ?? "");
            ImGui.TextDisabled("It follows the free VRAM by itself: smaller while a game holds the card, larger when it is free.");
        }

        if (ImGui.SmallButton("Ask again"))
        {
            serverGpu = null;
            readingServerGpu = null;
        }

        ImGui.Spacing();
        if (g.BudgetMb is { } budget)
            ImGui.TextColored(ImGuiColors.DalamudViolet, $"About {budget / 1024.0:0.#} GB of VRAM for a model there.");
        return true;
    }

    private void DrawRecommendations()
    {
        var budget = Budget();
        if (loadingRecommendations is { IsCompleted: true } load)
        {
            recommendations = load.IsCompletedSuccessfully ? load.Result : Recommendations.Bundled();
            loadingRecommendations = null;
        }

        if (recommendations == null)
        {
            ImGui.TextDisabled("Loading recommendations…");
            return;
        }

        var tier = recommendations.TierFor(budget);
        ImGui.TextUnformatted($"Recommended ({tier?.Label ?? "?"}, {(recommendations.Source == "bundled" ? "built-in list" : "community leaderboard")}):");
        var installed = CurrentServer()?.Models.Select(m => m.Id).ToHashSet(StringComparer.OrdinalIgnoreCase) ?? [];
        if (ImGui.BeginTable("##recs", 5, ImGuiTableFlags.RowBg | ImGuiTableFlags.BordersInnerH | ImGuiTableFlags.SizingStretchProp))
        {
            ImGui.TableSetupColumn("Model");
            ImGui.TableSetupColumn("VRAM");
            ImGui.TableSetupColumn("Tools");
            ImGui.TableSetupColumn("Score");
            ImGui.TableSetupColumn("");
            ImGui.TableHeadersRow();
            foreach (var m in recommendations.For(budget))
            {
                ImGui.TableNextRow();
                ImGui.TableNextColumn();
                ImGui.TextUnformatted(m.Name);
                if (m.Notes.Length > 0 && ImGui.IsItemHovered())
                    ImGui.SetTooltip(m.Notes);
                ImGui.TableNextColumn();
                ImGui.TextUnformatted(m.VramMb is { } v ? $"{v / 1024.0:0.#} GB" : "?");
                ImGui.TableNextColumn();
                ImGui.TextUnformatted(m.ToolCalling);
                ImGui.TableNextColumn();
                ImGui.TextUnformatted(m.Score is { } sc ? $"{sc:0} ({m.Samples})" : "—");
                ImGui.TableNextColumn();
                var id = CurrentServer()?.Kind == BackendKinds.LmStudio ? m.LmStudio : m.Ollama;
                if (id != null && installed.Contains(id))
                    ImGui.TextColored(ImGuiColors.HealerGreen, "installed");
                else if (m.Ollama != null && pullModel == m.Ollama && pulling is { IsCompleted: false })
                    ImGui.ProgressBar((float)(pullProgress?.Fraction ?? 0), new Vector2(-1, 0), pullProgress?.Status ?? "starting");
                else if (m.Ollama != null && CurrentServer()?.Kind == BackendKinds.Ollama)
                {
                    ImGui.BeginDisabled(pulling is { IsCompleted: false });
                    if (ImGui.SmallButton($"Download##{m.Name}"))
                        StartPull(m.Ollama);
                    ImGui.EndDisabled();
                }
                else if (m.Ollama != null && ImGui.SmallButton($"Copy pull command##{m.Name}"))
                    ImGui.SetClipboardText($"ollama pull {m.Ollama}");
            }

            ImGui.EndTable();
        }

        if (pullMessage != null)
            ImGui.TextWrapped(pullMessage);
        ImGui.TextDisabled(CurrentServer()?.Kind == BackendKinds.Ollama
            ? "Download installs a model into Ollama here; then press Next."
            : "Install a model with its pull command in a terminal (or search it in LM Studio), then press Next.");
    }

    /// <summary>Downloads a model into the chosen Ollama; the model list is refreshed when it is in.</summary>
    private void StartPull(string model)
    {
        if (CurrentServer() is not { } srv)
            return;
        pullModel = model;
        pullProgress = null;
        pullMessage = null;
        var progress = new Progress<PullProgress>(p => pullProgress = p);
        pulling = Task.Run(async () =>
        {
            try
            {
                await ModelPull.PullAsync(engine.Http, srv.RootUrl, model, progress, cts.Token).ConfigureAwait(false);
                pullMessage = $"{model} is installed.";
                Detect(); // the new model appears in the server's list
            }
            catch (Exception ex) when (ex is InvalidOperationException or HttpRequestException or IOException or System.Text.Json.JsonException)
            {
                pullMessage = $"Could not download {model}: {ex.Message}";
            }
        });
    }

    private int Budget()
    {
        if (useAlmanac && serverGpu?.BudgetMb is { } serverBudget)
            return serverBudget;
        var s = plugin.Settings;
        var vram = adapters.Count == 0 ? manualVramGb * 1024 : adapters[Math.Clamp(adapterIndex, 0, adapters.Count - 1)].VramMb;
        return Recommendations.Budget(vram, s.GameOnSameGpu, s.GameReserveMb);
    }

    // ---- 3: model -------------------------------------------------------------------------------

    private DetectedServer? CurrentServer() => useAlmanac ? null : server;

    private void DrawModel()
    {
        var models = CurrentServer()?.Models.Select(m => m.Id).ToList() ?? [];
        if (useAlmanac)
            models = serverGpu?.AutoChoice != null ? ["auto", AlmanacDefaultModel, .. models] : [AlmanacDefaultModel, .. models];
        if (models.Count == 0)
        {
            ImGui.TextColored(ImGuiColors.DalamudOrange, "The server lists no models. Install one (see the previous step), then press Refresh.");
            if (ImGui.Button("Refresh"))
            {
                Detect();
                step = 0;
            }

            return;
        }

        modelIndex = Math.Clamp(modelIndex, 0, models.Count - 1);
        var current = plugin.Settings.Model;
        var preset = models.IndexOf(current);
        if (preset >= 0 && probeModel == null)
            modelIndex = preset;

        ImGui.TextUnformatted("Pick the model to chat with:");
        if (ImGui.BeginListBox("##models", new Vector2(-1, 180 * ImGuiHelpers.GlobalScale)))
        {
            for (var i = 0; i < models.Count; i++)
            {
                var id = models[i];
                var cap = Capability(id);
                if (ImGui.Selectable($"{id}   [tools: {cap}]", modelIndex == i))
                {
                    modelIndex = i;
                    probeModel = null;
                }
            }

            ImGui.EndListBox();
        }

        var selected = models[modelIndex];
        ImGui.TextDisabled("\"native\": the model calls XivMcp's tools directly. \"prompted\": it works through a text fallback (slower, less reliable). \"unknown\": press Test.");
        if (probing is { IsCompleted: true } p)
        {
            if (p.IsCompletedSuccessfully)
                store.SetCapability(KeyFor(probeModel!), p.Result);
            probing = null;
        }

        ImGui.BeginDisabled(probing != null || useAlmanac && modelIndex == 0);
        if (ImGui.Button(probing != null ? "Testing…" : "Test tool calling"))
        {
            probeModel = selected;
            var client = new ChatClient(engine.Http, BaseUrl(), Key());
            probing = Task.Run(() => ModelCapabilities.ProbeAsync(client, selected, cts.Token));
        }

        ImGui.EndDisabled();
        if (probeModel == selected && probing == null)
        {
            ImGui.SameLine();
            ImGui.TextUnformatted($"Result: {Capability(selected)}");
        }
    }

    private string Capability(string model)
    {
        if (store.GetCapability(KeyFor(model)) is { } stored)
            return stored;
        if (CurrentServer()?.Kind == BackendKinds.Ollama)
        {
            if (ollamaCaps.TryGetValue(model, out var c))
                return c;
            ollamaCaps[model] = ModelCapabilities.FromName(model);
            var root = CurrentServer()!.RootUrl;
            _ = Task.Run(async () =>
            {
                try
                {
                    using var content = new StringContent(new JsonObject { ["model"] = model }.ToJsonString());
                    using var r = await engine.Quick.PostAsync($"{root}/api/show", content, cts.Token).ConfigureAwait(false);
                    if (r.IsSuccessStatusCode && ModelCapabilities.FromOllamaShow(JsonNode.Parse(await r.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false))) is { } cap)
                        ollamaCaps[model] = cap;
                }
                catch (Exception)
                {
                    // Keep the name-based guess.
                }
            });
            return ollamaCaps[model];
        }

        return ModelCapabilities.FromName(model);
    }

    private string BaseUrl() => useAlmanac ? almanacUrl : server?.BaseUrl ?? customUrl;

    private string? Key() => useAlmanac ? almanacToken : string.IsNullOrWhiteSpace(customKey) ? null : customKey;

    private string KeyFor(string model) => $"{ServerDetector.RootOf(BaseUrl())}|{model}";

    // ---- 4: XivMcp ------------------------------------------------------------------------------

    private void DrawXivMcp()
    {
        var s = plugin.Settings;
        ImGui.TextUnformatted("Almanac uses the XivMcp plugin to read the game and to act in it. Anything that changes your game (teleport, commands, chat) still asks you in XivMcp's approval window first.");
        ImGui.Spacing();
        var version = xivmcp.ApiVersion();
        if (version == null)
        {
            ImGui.TextColored(ImGuiColors.DalamudOrange, "XivMcp is not loaded. Install and enable it, or connect to it manually below. Without it Almanac can chat but cannot see your game.");
        }
        else
        {
            ImGui.TextColored(ImGuiColors.HealerGreen, $"XivMcp is loaded (API {version}{(xivmcp.ApiRevision() is { } rev ? $", revision {rev}" : "")}).");
        }

        var viaIpc = s.XivMcpViaIpc;
        if (ImGui.Checkbox("Connect automatically through XivMcp (recommended)", ref viaIpc))
            s.XivMcpViaIpc = viaIpc;
        ImGui.TextDisabled("XivMcp issues Almanac its own client token; you can revoke it in XivMcp's settings.");
        if (!s.XivMcpViaIpc)
        {
            var endpoint = s.XivMcpEndpoint;
            ImGui.SetNextItemWidth(320 * ImGuiHelpers.GlobalScale);
            if (ImGui.InputText("Endpoint", ref endpoint, 256))
                s.XivMcpEndpoint = endpoint;
            var token = s.XivMcpToken;
            ImGui.SetNextItemWidth(320 * ImGuiHelpers.GlobalScale);
            if (ImGui.InputText("Client token", ref token, 256, ImGuiInputTextFlags.Password))
                s.XivMcpToken = token;
        }

        if (ImGui.Button(connecting is { IsCompleted: false } ? "Connecting…" : "Test the connection") && connecting is not { IsCompleted: false })
        {
            plugin.SaveSettings();
            connecting = engine.XivMcpToolsAsync(forceNew: true);
        }

        if (engine.XivMcpStatus is { } status)
        {
            ImGui.SameLine();
            ImGui.TextUnformatted(status);
        }
    }

    // ---- navigation -----------------------------------------------------------------------------

    private bool CanAdvance() => step switch
    {
        0 => useAlmanac ? almanacUrl.Length > 0 : server != null,
        _ => true,
    };

    private void Advance()
    {
        var s = plugin.Settings;
        switch (step)
        {
            case 0:
                s.Engine = useAlmanac ? AlmanacSettings.EngineAlmanac : AlmanacSettings.EngineDirect;
                if (useAlmanac)
                {
                    s.AlmanacUrl = ServerDetector.NormalizeBase(almanacUrl);
                    s.AlmanacToken = almanacToken;
                }
                else if (server != null)
                {
                    s.BaseUrl = server.BaseUrl;
                    s.BackendKind = server.Kind;
                    s.ApiKey = server.BaseUrl == ServerDetector.NormalizeBase(customUrl) ? customKey : s.ApiKey;
                }

                break;
            case 2:
                var models = CurrentServer()?.Models.Select(m => m.Id).ToList() ?? [];
                if (useAlmanac)
                    models = serverGpu?.AutoChoice != null ? ["auto", AlmanacDefaultModel, .. models] : [AlmanacDefaultModel, .. models];
                if (models.Count > 0)
                    s.Model = models[Math.Clamp(modelIndex, 0, models.Count - 1)];
                break;
        }

        plugin.SaveSettings();
        step++;
    }

    public void Dispose()
    {
        cts.Cancel();
        cts.Dispose();
    }
}
