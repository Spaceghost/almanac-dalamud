using System.Numerics;
using Almanac.Core;
using Almanac.Core.Agent;
using Almanac.Core.Storage;
using Almanac.Core.Tools;
using Dalamud.Bindings.ImGui;
using Dalamud.Interface.Colors;
using Dalamud.Interface.Utility;
using Dalamud.Interface.Windowing;

namespace Almanac.Plugin.Windows;

/// <summary>All settings in one place; the setup wizard covers the common path.</summary>
public sealed class SettingsWindow : Window
{
    private static readonly string[] ToolCallingOptions = ["auto", "native", "prompted", "none"];

    private static Changelog? changelog;
    private static string? changelogError;

    private readonly Plugin plugin;
    private readonly Engine engine;

    public SettingsWindow(Plugin plugin, Engine engine)
        : base("Almanac settings###AlmanacSettings")
    {
        this.plugin = plugin;
        this.engine = engine;
        Size = new Vector2(560, 520);
        SizeCondition = ImGuiCond.FirstUseEver;
    }

    public override void Draw()
    {
        var s = plugin.Settings;
        var changed = false;
        var w = 320 * ImGuiHelpers.GlobalScale;

        if (ImGui.Button("Run the setup wizard"))
            plugin.OpenSetup();

        Section("What's new");
        DrawChangelog();

        Section("Model");
        var almanac = s.Engine == AlmanacSettings.EngineAlmanac;
        if (ImGui.RadioButton("Model server directly", !almanac))
        {
            s.Engine = AlmanacSettings.EngineDirect;
            changed = true;
        }

        ImGui.SameLine();
        if (ImGui.RadioButton("Through an almanac engine", almanac))
        {
            s.Engine = AlmanacSettings.EngineAlmanac;
            changed = true;
        }

        changed |= Text(almanac ? "Gateway URL" : "Base URL", almanac ? s.AlmanacUrl : s.BaseUrl, v => { if (almanac) s.AlmanacUrl = v; else s.BaseUrl = v; }, w);
        changed |= Text(almanac ? "Gateway token" : "API key", almanac ? s.AlmanacToken : s.ApiKey, v => { if (almanac) s.AlmanacToken = v; else s.ApiKey = v; }, w, password: true);
        changed |= Text("Model", s.Model, v => s.Model = v, w);
        var follow = s.FollowXivMcpModel;
        if (ImGui.Checkbox("Use XivMcp's local model when this is empty", ref follow))
        {
            s.FollowXivMcpModel = follow;
            changed = true;
        }

        ImGui.TextDisabled($"In use: {engine.ModelLabel} at {engine.ModelTarget().BaseUrl}");

        Section("Agent");
        var tc = Array.IndexOf(ToolCallingOptions, s.ToolCalling);
        ImGui.SetNextItemWidth(160);
        if (ImGui.Combo("Tool calling", ref tc, ToolCallingOptions, ToolCallingOptions.Length))
        {
            s.ToolCalling = ToolCallingOptions[Math.Max(0, tc)];
            changed = true;
        }

        ImGui.SameLine();
        ImGui.TextDisabled($"(auto: {engine.Capability(engine.ModelTarget().Model)})");
        var profile = Array.IndexOf(ToolProfiles.Names, s.ToolProfile);
        ImGui.SetNextItemWidth(160);
        if (ImGui.Combo("XivMcp tools", ref profile, ToolProfiles.Names, ToolProfiles.Names.Length))
        {
            s.ToolProfile = ToolProfiles.Names[Math.Max(0, profile)];
            changed = true;
        }

        ImGui.TextDisabled("small: 10 tools (best for small models) · standard: about 30 · all: everything XivMcp offers.");
        var steps = s.MaxSteps;
        ImGui.SetNextItemWidth(160);
        if (ImGui.InputInt("Max steps per question", ref steps))
        {
            s.MaxSteps = steps;
            changed = true;
        }

        var temp = (float)s.Temperature;
        ImGui.SetNextItemWidth(160);
        if (ImGui.SliderFloat("Temperature", ref temp, 0, 1.5f, "%.2f"))
        {
            s.Temperature = temp;
            changed = true;
        }

        var maxTokens = s.MaxTokens;
        ImGui.SetNextItemWidth(160);
        if (ImGui.InputInt("Max reply tokens", ref maxTokens, 128))
        {
            s.MaxTokens = maxTokens;
            changed = true;
        }

        ImGui.TextUnformatted("System prompt (empty = default)");
        var prompt = s.SystemPrompt;
        if (ImGui.InputTextMultiline("##system", ref prompt, 4000, new Vector2(-1, 90 * ImGuiHelpers.GlobalScale)))
        {
            s.SystemPrompt = prompt;
            changed = true;
        }

        if (string.IsNullOrWhiteSpace(s.SystemPrompt) && ImGui.IsItemHovered())
            ImGui.SetTooltip(ChatSession.DefaultSystemPrompt);

        Section("XivMcp");
        var ipc = s.XivMcpViaIpc;
        if (ImGui.Checkbox("Connect through XivMcp automatically", ref ipc))
        {
            s.XivMcpViaIpc = ipc;
            changed = true;
        }

        if (!s.XivMcpViaIpc)
        {
            changed |= Text("Endpoint", s.XivMcpEndpoint, v => s.XivMcpEndpoint = v, w);
            changed |= Text("Client token", s.XivMcpToken, v => s.XivMcpToken = v, w, password: true);
        }

        if (engine.XivMcpStatus is { } status)
            ImGui.TextDisabled(status);

        Section("Leaderboard");
        changed |= Text("URL", s.LeaderboardUrl, v => s.LeaderboardUrl = v, w);
        ImGui.TextDisabled("Nothing is sent unless you press Share in the benchmark window.");

        if (changed)
            plugin.SaveSettings();
    }

    /// <summary>
    /// The changelog, straight from changelog.json (embedded in Almanac.Core, and the same file
    /// CHANGELOG.md is rendered from). Read once; a plugin reload is what picks up a new build.
    /// </summary>
    private static void DrawChangelog()
    {
        if (changelog is null && changelogError is null)
        {
            try
            {
                changelog = Changelog.Bundled();
            }
            catch (Exception ex)
            {
                changelogError = ex.Message;
            }
        }

        if (changelog is null)
        {
            ImGui.TextDisabled($"The changelog could not be read: {changelogError}");
            return;
        }

        for (var i = 0; i < changelog.Releases.Count; i++)
        {
            var release = changelog.Releases[i];
            var flags = i == 0 ? ImGuiTreeNodeFlags.DefaultOpen : ImGuiTreeNodeFlags.None;
            if (!ImGui.CollapsingHeader($"{release.Heading}###changelog{i}", flags))
                continue;

            Wrapped(release.Blurb, ImGuiColors.DalamudGrey);
            foreach (var item in release.Items)
                Wrapped($"{Changelog.Label(item.Status)}   {item.Text}", StatusColour(item.Status));

            ImGui.Spacing();
        }
    }

    /// <summary>NEW and FIX are in a release; BETA is merged but unverified in game; SOON is still being built.</summary>
    private static Vector4 StatusColour(string status) => status switch
    {
        "new" => ImGuiColors.HealerGreen,
        "fix" => ImGuiColors.DalamudOrange,
        "beta" => ImGuiColors.TankBlue,
        _ => ImGuiColors.DalamudGrey,
    };

    private static void Wrapped(string text, Vector4 colour)
    {
        if (text.Length == 0)
            return;
        ImGui.PushStyleColor(ImGuiCol.Text, colour);
        ImGui.TextWrapped(text);
        ImGui.PopStyleColor();
    }

    private static void Section(string title)
    {
        ImGui.Spacing();
        ImGui.Separator();
        ImGui.TextColored(ImGuiColors.DalamudViolet, title);
    }

    private static bool Text(string label, string value, Action<string> set, float width, bool password = false)
    {
        ImGui.SetNextItemWidth(width);
        if (!ImGui.InputText(label, ref value, 512, password ? ImGuiInputTextFlags.Password : ImGuiInputTextFlags.None))
            return false;
        set(value);
        return true;
    }
}
