using System.Numerics;
using Almanac.Core.Agent;
using Almanac.Core.Storage;
using Dalamud.Bindings.ImGui;
using Dalamud.Interface.Colors;
using Dalamud.Interface.Utility;
using Dalamud.Interface.Windowing;

namespace Almanac.Plugin.Windows;

/// <summary>In-game chat: threads on the left, the transcript with streaming on the right, follow-ups below.</summary>
public sealed class ChatWindow : Window
{
    private readonly Plugin plugin;
    private readonly Engine engine;
    private readonly AlmanacStore store;
    private string input = "";
    private int lastLineCount;
    private IReadOnlyList<ThreadInfo> threads = [];
    private DateTime threadsLoaded = DateTime.MinValue;
    private string? renaming;
    private string renameText = "";

    public ChatWindow(Plugin plugin, Engine engine, AlmanacStore store)
        : base("Almanac###AlmanacChat")
    {
        this.plugin = plugin;
        this.engine = engine;
        this.store = store;
        Size = new Vector2(720, 480);
        SizeCondition = ImGuiCond.FirstUseEver;
        SizeConstraints = new WindowSizeConstraints { MinimumSize = new Vector2(420, 260), MaximumSize = new Vector2(float.MaxValue, float.MaxValue) };
    }

    /// <summary>Sends text as the next message of the open thread. False when a reply is still running.</summary>
    public bool Send(string text)
    {
        var session = engine.GetChatSession();
        if (session.Busy || string.IsNullOrWhiteSpace(text))
            return false;
        _ = Task.Run(() => session.SendAsync(text));
        threadsLoaded = DateTime.MinValue;
        return true;
    }

    public override void Draw()
    {
        var session = engine.GetChatSession();
        if (DateTime.UtcNow - threadsLoaded > TimeSpan.FromSeconds(session.Busy ? 1 : 5))
        {
            threads = store.ListThreads();
            threadsLoaded = DateTime.UtcNow;
        }

        DrawHeader(session);
        var sidebar = 190 * ImGuiHelpers.GlobalScale;
        if (ImGui.BeginChild("##threads", new Vector2(sidebar, 0), true))
            DrawThreads(session);
        ImGui.EndChild();
        ImGui.SameLine();
        ImGui.BeginGroup();
        DrawTranscript(session);
        DrawInput(session);
        ImGui.EndGroup();
    }

    private void DrawHeader(ChatSession session)
    {
        ImGui.TextColored(ImGuiColors.DalamudViolet, engine.ModelLabel);
        ImGui.SameLine();
        ImGui.TextDisabled($"· tools: {engine.ToolMode(engine.ModelTarget().Model).ToString().ToLowerInvariant()} · {plugin.Settings.ToolProfile}");
        if (engine.XivMcpStatus is { } status)
        {
            ImGui.SameLine();
            ImGui.TextDisabled($"· {status}");
        }

        ImGui.SameLine(ImGui.GetWindowWidth() - 180 * ImGuiHelpers.GlobalScale);
        if (ImGui.SmallButton("Benchmark"))
            plugin.OpenBenchmark();
        ImGui.SameLine();
        if (ImGui.SmallButton("Settings"))
            plugin.OpenSettings();
        ImGui.Separator();
    }

    private void DrawThreads(ChatSession session)
    {
        if (ImGui.Button("New thread", new Vector2(-1, 0)))
            session.New();
        ImGui.Separator();
        foreach (var t in threads)
        {
            var selected = session.Thread?.Id == t.Id;
            if (renaming == t.Id)
            {
                ImGui.SetNextItemWidth(-1);
                if (ImGui.InputText($"##rename-{t.Id}", ref renameText, 80, ImGuiInputTextFlags.EnterReturnsTrue) || ImGui.IsItemDeactivated())
                {
                    if (renameText.Trim().Length > 0)
                        store.RenameThread(t.Id, renameText.Trim());
                    renaming = null;
                    threadsLoaded = DateTime.MinValue;
                }

                continue;
            }

            var label = (t.ParentId != null ? "↳ " : "") + t.Title;
            if (ImGui.Selectable($"{label}##{t.Id}", selected) && !session.Busy)
                session.Open(t.Id);
            if (ImGui.BeginPopupContextItem($"##ctx-{t.Id}"))
            {
                if (ImGui.MenuItem("Rename"))
                {
                    renaming = t.Id;
                    renameText = t.Title;
                }

                if (ImGui.MenuItem("Delete") && !session.Busy)
                {
                    store.DeleteThread(t.Id);
                    if (selected)
                        session.New();
                    threadsLoaded = DateTime.MinValue;
                }

                ImGui.EndPopup();
            }
        }
    }

    private void DrawTranscript(ChatSession session)
    {
        var inputHeight = ImGui.GetFrameHeightWithSpacing() * 3.2f;
        if (ImGui.BeginChild("##transcript", new Vector2(0, -inputHeight), true))
        {
            var lines = session.Snapshot();
            if (lines.Count == 0)
            {
                ImGui.TextDisabled("Ask anything about your game: \"where am I?\", \"when does it rain in Lower La Noscea?\",");
                ImGui.TextDisabled("\"flag X 11.2 Y 14.5\", \"what level is It's Probably Pirates?\". Actions ask you first in game.");
            }

            ImGui.PushTextWrapPos(0);
            for (var i = 0; i < lines.Count; i++)
            {
                var line = lines[i];
                switch (line.Kind)
                {
                    case LineKind.User:
                        ImGui.TextColored(ImGuiColors.TankBlue, "You");
                        ImGui.TextUnformatted(line.Text);
                        break;
                    case LineKind.Assistant:
                        ImGui.TextColored(ImGuiColors.HealerGreen, "Almanac");
                        ImGui.TextUnformatted(line.Text);
                        break;
                    case LineKind.Tool:
                        ImGui.TextColored(ImGuiColors.DalamudGrey, line.Text);
                        break;
                    case LineKind.Notice:
                        ImGui.TextColored(ImGuiColors.DalamudYellow, line.Text);
                        break;
                    case LineKind.Error:
                        ImGui.TextColored(ImGuiColors.DalamudRed, line.Text);
                        break;
                }

                if (line.Seq is { } seq && line.Kind is LineKind.User or LineKind.Assistant && ImGui.BeginPopupContextItem($"##line-{i}"))
                {
                    if (ImGui.MenuItem("Copy"))
                        ImGui.SetClipboardText(line.Text);
                    if (ImGui.MenuItem("Branch a new thread from here") && !session.Busy)
                    {
                        session.Fork(seq);
                        threadsLoaded = DateTime.MinValue;
                    }

                    ImGui.EndPopup();
                }

                if (line.Kind is LineKind.User or LineKind.Assistant)
                    ImGui.Spacing();
            }

            ImGui.PopTextWrapPos();
            if (session.Busy && session.Status is { } status)
                ImGui.TextDisabled(status);

            // Follow the newest text unless the player scrolled up.
            var count = lines.Count + (lines.Count > 0 ? lines[^1].Text.Length : 0);
            if (count != lastLineCount && ImGui.GetScrollY() >= ImGui.GetScrollMaxY() - 40)
                ImGui.SetScrollHereY(1);
            lastLineCount = count;
        }

        ImGui.EndChild();
    }

    private void DrawInput(ChatSession session)
    {
        var sendWidth = 70 * ImGuiHelpers.GlobalScale;
        var submitted = ImGui.InputTextMultiline("##input", ref input, 4000, new Vector2(-sendWidth - ImGui.GetStyle().ItemSpacing.X, ImGui.GetFrameHeightWithSpacing() * 2.6f),
            ImGuiInputTextFlags.EnterReturnsTrue | ImGuiInputTextFlags.CtrlEnterForNewLine);
        ImGui.SameLine();
        ImGui.BeginGroup();
        if (session.Busy)
        {
            if (ImGui.Button("Stop", new Vector2(sendWidth, 0)))
                session.Cancel();
        }
        else if ((ImGui.Button("Send", new Vector2(sendWidth, 0)) || submitted) && input.Trim().Length > 0)
        {
            if (Send(input))
                input = "";
            ImGui.SetKeyboardFocusHere(-1);
        }

        if (!plugin.Settings.SetupComplete && ImGui.Button("Setup", new Vector2(sendWidth, 0)))
            plugin.OpenSetup();
        ImGui.EndGroup();
    }
}
