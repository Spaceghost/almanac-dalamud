using System.Collections.Concurrent;
using System.Globalization;

namespace Almanac.Core.Diagnostics;

/// <summary>
/// A count of the things Almanac is holding that a plugin reload has to give back: registrations
/// with Dalamud, and objects that own an OS handle or a thread. Every acquisition increments and
/// every release decrements, so after <c>Dispose()</c> every counter must be zero.
///
/// This is deliberately a process-wide static: it has to survive the disposal of everything else
/// so that the unload test, and the in-game leak watch, can read the counters afterwards. It costs
/// one interlocked increment per registration and is never read from the render thread.
/// </summary>
public static class LiveObjects
{
    /// <summary>Counter names. Strings, not an enum, so the plugin layer can add its own.</summary>
    public static class Kinds
    {
        public const string Store = "sqlite-store";
        public const string ChatSession = "chat-session";
        public const string McpClient = "mcp-client";
        public const string McpToolSource = "mcp-tool-source";
        public const string VramSampler = "vram-sampler";
        public const string Command = "command-handler";
        public const string Window = "window";
        public const string IpcProvider = "ipc-provider";
        public const string IpcSubscription = "ipc-subscription";
        public const string UiEvent = "ui-event-subscription";
        public const string Hook = "hook";
        public const string FontHandle = "font-handle";
        public const string TextureWrap = "texture-wrap";
    }

    private static readonly ConcurrentDictionary<string, StrongBox<int>> Counters = new(StringComparer.Ordinal);

    public static void Acquired(string kind, int n = 1) => Add(kind, n);

    public static void Released(string kind, int n = 1) => Add(kind, -n);

    public static int Count(string kind) =>
        Counters.TryGetValue(kind, out var box) ? Volatile.Read(ref box.Value) : 0;

    /// <summary>Every counter that has ever been touched, in a stable order.</summary>
    public static IReadOnlyList<KeyValuePair<string, int>> Snapshot() =>
        Counters.Select(kv => new KeyValuePair<string, int>(kv.Key, Volatile.Read(ref kv.Value.Value)))
            .OrderBy(kv => kv.Key, StringComparer.Ordinal)
            .ToList();

    /// <summary>Counters that are not zero. After a clean unload this is empty.</summary>
    public static IReadOnlyList<KeyValuePair<string, int>> Outstanding() =>
        Snapshot().Where(kv => kv.Value != 0).ToList();

    public static string Describe()
    {
        var snapshot = Snapshot();
        return snapshot.Count == 0
            ? "(nothing registered)"
            : string.Join(", ", snapshot.Select(kv => $"{kv.Key}={kv.Value.ToString(CultureInfo.InvariantCulture)}"));
    }

    /// <summary>Clears every counter. For tests that need a known starting point.</summary>
    public static void Reset() => Counters.Clear();

    private static void Add(string kind, int delta)
    {
        var box = Counters.GetOrAdd(kind, _ => new StrongBox<int>(0));
        Interlocked.Add(ref box.Value, delta);
    }

    private sealed class StrongBox<T>(T value)
    {
        public T Value = value;
    }
}
