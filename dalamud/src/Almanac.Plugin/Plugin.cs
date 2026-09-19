using Almanac.Core.Storage;
using Almanac.Plugin.Windows;
using Dalamud.Game.Command;
using Dalamud.Interface.Windowing;
using Dalamud.Plugin;
using Dalamud.Plugin.Ipc;
using Dalamud.Plugin.Services;

namespace Almanac.Plugin;

/// <summary>
/// Dalamud entry point: the SQLite store, settings, the XivMcp link, the engine, windows, the /almanac command and a
/// small IPC surface (Almanac.Ask) for other plugins such as a terminal's /ask.
/// </summary>
public sealed class Plugin : IDalamudPlugin
{
    public const string Command = "/almanac";

    private readonly IDalamudPluginInterface pi;
    private readonly ICommandManager commands;
    private readonly IPluginLog log;
    private readonly AlmanacStore store;
    private readonly XivMcpLink xivmcp;
    private readonly Engine engine;
    private readonly WindowSystem windows = new("Almanac");
    private readonly ChatWindow chatWindow;
    private readonly SetupWindow setupWindow;
    private readonly BenchmarkWindow benchWindow;
    private readonly SettingsWindow settingsWindow;
    private readonly ICallGateProvider<int> apiVersion;
    private readonly ICallGateProvider<string, bool> ask;

    public Plugin(IDalamudPluginInterface pi, ICommandManager commands, IFramework framework, IPluginLog log)
    {
        this.pi = pi;
        this.commands = commands;
        this.log = log;

        store = new AlmanacStore(Path.Combine(pi.GetPluginConfigDirectory(), "almanac.sqlite"));
        Settings = AlmanacSettings.Load(store);
        xivmcp = new XivMcpLink(pi, framework, log);
        engine = new Engine(store, xivmcp, () => Settings);
        xivmcp.SubscribeLocalModelChanged(engine.Invalidate);

        chatWindow = new ChatWindow(this, engine, store);
        setupWindow = new SetupWindow(this, engine, store, xivmcp);
        benchWindow = new BenchmarkWindow(this, engine, store);
        settingsWindow = new SettingsWindow(this, engine);
        windows.AddWindow(chatWindow);
        windows.AddWindow(setupWindow);
        windows.AddWindow(benchWindow);
        windows.AddWindow(settingsWindow);

        pi.UiBuilder.Draw += windows.Draw;
        pi.UiBuilder.OpenMainUi += OpenChat;
        pi.UiBuilder.OpenConfigUi += () => settingsWindow.IsOpen = true;

        commands.AddHandler(Command, new CommandInfo(OnCommand)
        {
            HelpMessage = "Open the Almanac chat. \"/almanac <question>\" asks right away; \"/almanac setup|bench|settings|new\".",
        });

        apiVersion = pi.GetIpcProvider<int>("Almanac.ApiVersion");
        apiVersion.RegisterFunc(() => 1);
        // Almanac.Ask: Func<string, bool> — opens the chat and sends the text; false when busy or not set up.
        ask = pi.GetIpcProvider<string, bool>("Almanac.Ask");
        ask.RegisterFunc(Ask);

        if (!Settings.SetupComplete)
            setupWindow.IsOpen = true;
    }

    public AlmanacSettings Settings { get; private set; }

    public void SaveSettings()
    {
        Settings.Save(store);
        engine.Invalidate();
    }

    public void OpenChat() => chatWindow.IsOpen = true;

    public void OpenSetup() => setupWindow.IsOpen = true;

    public void OpenBenchmark() => benchWindow.IsOpen = true;

    public void OpenSettings() => settingsWindow.IsOpen = true;

    private bool Ask(string text)
    {
        if (!Settings.SetupComplete || string.IsNullOrWhiteSpace(text))
            return false;
        chatWindow.IsOpen = true;
        return chatWindow.Send(text);
    }

    private void OnCommand(string command, string args)
    {
        switch (args.Trim().ToLowerInvariant())
        {
            case "":
                chatWindow.Toggle();
                break;
            case "setup":
                OpenSetup();
                break;
            case "bench" or "benchmark":
                OpenBenchmark();
                break;
            case "settings" or "config":
                OpenSettings();
                break;
            case "new":
                engine.GetChatSession().New();
                OpenChat();
                break;
            default:
                if (!Ask(args.Trim()))
                    OpenSetup();
                break;
        }
    }

    public void Dispose()
    {
        commands.RemoveHandler(Command);
        ask.UnregisterFunc();
        apiVersion.UnregisterFunc();
        pi.UiBuilder.Draw -= windows.Draw;
        pi.UiBuilder.OpenMainUi -= OpenChat;
        windows.RemoveAllWindows();
        benchWindow.Dispose();
        setupWindow.Dispose();
        engine.Dispose();
        xivmcp.Dispose();
        try
        {
            store.Dispose();
        }
        catch (Exception ex)
        {
            log.Warning(ex, "Closing the Almanac database failed");
        }
    }
}
