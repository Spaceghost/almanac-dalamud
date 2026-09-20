using System.Globalization;
using System.Reflection;
using Almanac.Core.Bench;

namespace Almanac.Core.Storage;

/// <summary>
/// Plugin settings. Stored one row per property in the SQLite <c>kv</c> table (<c>settings.&lt;Name&gt;</c>), so adding a
/// property needs no migration and an unknown or unreadable row just keeps the default.
/// </summary>
public sealed class AlmanacSettings
{
    public const string EngineDirect = "direct";
    public const string EngineAlmanac = "almanac";

    public bool SetupComplete { get; set; }

    /// <summary><see cref="EngineDirect"/>: talk to the model server; <see cref="EngineAlmanac"/>: go through an almanac gateway.</summary>
    public string Engine { get; set; } = EngineDirect;

    public string BaseUrl { get; set; } = "http://127.0.0.1:11434/v1";

    /// <summary>Optional; most local servers need none.</summary>
    public string ApiKey { get; set; } = "";

    public string Model { get; set; } = "";

    public string BackendKind { get; set; } = "";

    /// <summary>auto (probe/known), native, prompted or none.</summary>
    public string ToolCalling { get; set; } = "auto";

    public string ToolProfile { get; set; } = "standard";

    public int MaxSteps { get; set; } = 8;

    public double Temperature { get; set; } = 0.3;

    public int MaxTokens { get; set; } = 1024;

    public string SystemPrompt { get; set; } = "";

    public string AlmanacUrl { get; set; } = "http://127.0.0.1:41881/v1";

    public string AlmanacToken { get; set; } = "";

    /// <summary>Use the model configured in XivMcp's "Local model" section when this plugin has none.</summary>
    public bool FollowXivMcpModel { get; set; } = true;

    /// <summary>Connect to XivMcp through Dalamud IPC (no token to copy).</summary>
    public bool XivMcpViaIpc { get; set; } = true;

    public string XivMcpEndpoint { get; set; } = "http://127.0.0.1:41800/mcp";

    /// <summary>Only for a manual connection (IPC off).</summary>
    public string XivMcpToken { get; set; } = "";

    public bool GameOnSameGpu { get; set; } = true;

    /// <summary>VRAM left for the game when it shares the GPU with the model.</summary>
    public int GameReserveMb { get; set; } = 3072;

    public string LeaderboardUrl { get; set; } = Results.DefaultLeaderboard;

    /// <summary>Where the device link for leaderboard submissions is made.</summary>
    public string SignInUrl { get; set; } = DeviceLink.DefaultApi;

    /// <summary>The player's leaderboard token from the device link. A secret: never logged, shown or put in a URL.</summary>
    public string LeaderboardToken { get; set; } = "";

    public string EffectiveBaseUrl => Engine == EngineAlmanac ? AlmanacUrl : BaseUrl;

    public string EffectiveApiKey => Engine == EngineAlmanac ? AlmanacToken : ApiKey;

    private const string Prefix = "settings.";

    private static readonly PropertyInfo[] Properties = typeof(AlmanacSettings)
        .GetProperties(BindingFlags.Public | BindingFlags.Instance)
        .Where(p => p.CanRead && p.CanWrite)
        .ToArray();

    public static AlmanacSettings Load(AlmanacStore store)
    {
        var settings = new AlmanacSettings();
        var rows = store.GetPrefix(Prefix);
        foreach (var p in Properties)
        {
            if (!rows.TryGetValue(Prefix + p.Name, out var text))
                continue;
            try
            {
                p.SetValue(settings, Convert.ChangeType(text, p.PropertyType, CultureInfo.InvariantCulture));
            }
            catch (Exception ex) when (ex is FormatException or InvalidCastException or OverflowException)
            {
                // Keep the default.
            }
        }

        settings.Normalize();
        return settings;
    }

    public void Save(AlmanacStore store)
    {
        Normalize();
        foreach (var p in Properties)
            store.Set(Prefix + p.Name, Convert.ToString(p.GetValue(this), CultureInfo.InvariantCulture) ?? "");
    }

    public void Normalize()
    {
        MaxSteps = Math.Clamp(MaxSteps, 1, 30);
        MaxTokens = Math.Clamp(MaxTokens, 64, 32768);
        Temperature = Math.Clamp(Temperature, 0, 2);
        GameReserveMb = Math.Clamp(GameReserveMb, 0, 32768);
        if (Engine != EngineAlmanac)
            Engine = EngineDirect;
        if (ToolCalling is not ("auto" or "native" or "prompted" or "none"))
            ToolCalling = "auto";
        if (!Tools.ToolProfiles.Names.Contains(ToolProfile))
            ToolProfile = Tools.ToolProfiles.Standard;
        BaseUrl = BaseUrl.Trim();
        AlmanacUrl = AlmanacUrl.Trim();
        XivMcpEndpoint = XivMcpEndpoint.Trim();
        Model = Model.Trim();
    }
}
