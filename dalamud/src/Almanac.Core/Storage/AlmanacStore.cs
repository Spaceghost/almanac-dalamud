using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using Almanac.Core.Llm;
using Microsoft.Data.Sqlite;

namespace Almanac.Core.Storage;

public sealed record ThreadInfo(string Id, string Title, string? ParentId, DateTimeOffset CreatedAt, DateTimeOffset UpdatedAt, int MessageCount);

public sealed record StoredMessage(long Id, string ThreadId, int Seq, ChatMessage Message, DateTimeOffset CreatedAt);

public sealed record BenchRow(long Id, DateTimeOffset CreatedAt, string Model, string Mode, string SuiteVersion, double Score, bool Submitted, string ResultJson);

/// <summary>
/// Everything the plugin keeps on disk, in one SQLite file: settings, conversation threads and messages, benchmark
/// runs, model capability probes and cached downloads. One connection, serialised by a lock (the UI thread reads, the
/// agent task writes); every call is short.
/// </summary>
public sealed class AlmanacStore : IDisposable
{
    public const int SchemaVersion = 1;

    private readonly SqliteConnection db;
    private readonly Lock gate = new();

    public AlmanacStore(string path)
    {
        var builder = new SqliteConnectionStringBuilder { DataSource = path, Mode = path == ":memory:" ? SqliteOpenMode.Memory : SqliteOpenMode.ReadWriteCreate, Pooling = false };
        db = new SqliteConnection(builder.ToString());
        db.Open();
        Exec("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=2000;");
        Migrate();
    }

    public static AlmanacStore InMemory() => new(":memory:");

    private void Migrate()
    {
        var version = Scalar<long>("PRAGMA user_version");
        if (version < 1)
        {
            Exec("""
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, parent_id TEXT REFERENCES threads(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT, tool_calls TEXT, tool_call_id TEXT, created_at TEXT NOT NULL,
                    UNIQUE(thread_id, seq));
                CREATE TABLE IF NOT EXISTS bench_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, model TEXT NOT NULL, mode TEXT NOT NULL,
                    suite_version TEXT NOT NULL, score REAL NOT NULL, submitted INTEGER NOT NULL DEFAULT 0, result_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_caps (model_key TEXT PRIMARY KEY, tool_calling TEXT NOT NULL, probed_at TEXT NOT NULL);
                PRAGMA user_version = 1;
                """);
        }
    }

    // ---- key/value (settings and caches) --------------------------------------------------------------

    public string? Get(string key)
    {
        lock (gate)
        {
            using var cmd = Command("SELECT value FROM kv WHERE key = $k", ("$k", key));
            return cmd.ExecuteScalar() as string;
        }
    }

    public void Set(string key, string value)
    {
        lock (gate)
        {
            using var cmd = Command(
                "INSERT INTO kv(key, value, updated_at) VALUES($k, $v, $t) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                ("$k", key), ("$v", value), ("$t", Now()));
            cmd.ExecuteNonQuery();
        }
    }

    public void Delete(string key)
    {
        lock (gate)
        {
            using var cmd = Command("DELETE FROM kv WHERE key = $k", ("$k", key));
            cmd.ExecuteNonQuery();
        }
    }

    public IReadOnlyDictionary<string, string> GetPrefix(string prefix)
    {
        lock (gate)
        {
            using var cmd = Command("SELECT key, value FROM kv WHERE substr(key, 1, length($p)) = $p", ("$p", prefix));
            using var r = cmd.ExecuteReader();
            var d = new Dictionary<string, string>(StringComparer.Ordinal);
            while (r.Read())
                d[r.GetString(0)] = r.GetString(1);
            return d;
        }
    }

    // ---- threads ------------------------------------------------------------------------------------

    public ThreadInfo CreateThread(string title, string? parentId = null)
    {
        var id = Guid.NewGuid().ToString("N")[..12];
        var now = Now();
        lock (gate)
        {
            using var cmd = Command("INSERT INTO threads(id, title, parent_id, created_at, updated_at) VALUES($id, $t, $p, $n, $n)", ("$id", id), ("$t", title), ("$p", parentId), ("$n", now));
            cmd.ExecuteNonQuery();
        }

        return new ThreadInfo(id, title, parentId, Parse(now), Parse(now), 0);
    }

    public IReadOnlyList<ThreadInfo> ListThreads(int limit = 200)
    {
        lock (gate)
        {
            using var cmd = Command(
                "SELECT t.id, t.title, t.parent_id, t.created_at, t.updated_at, (SELECT count(*) FROM messages m WHERE m.thread_id = t.id) FROM threads t ORDER BY t.updated_at DESC LIMIT $l",
                ("$l", limit));
            using var r = cmd.ExecuteReader();
            var list = new List<ThreadInfo>();
            while (r.Read())
                list.Add(new ThreadInfo(r.GetString(0), r.GetString(1), r.IsDBNull(2) ? null : r.GetString(2), Parse(r.GetString(3)), Parse(r.GetString(4)), r.GetInt32(5)));
            return list;
        }
    }

    public void RenameThread(string id, string title)
    {
        lock (gate)
        {
            using var cmd = Command("UPDATE threads SET title = $t WHERE id = $id", ("$t", title), ("$id", id));
            cmd.ExecuteNonQuery();
        }
    }

    public void DeleteThread(string id)
    {
        lock (gate)
        {
            using var cmd = Command("DELETE FROM threads WHERE id = $id", ("$id", id));
            cmd.ExecuteNonQuery();
        }
    }

    public StoredMessage AppendMessage(string threadId, ChatMessage message)
    {
        var now = Now();
        lock (gate)
        {
            using var tx = db.BeginTransaction();
            using var seqCmd = Command("SELECT coalesce(max(seq), -1) + 1 FROM messages WHERE thread_id = $t", ("$t", threadId));
            seqCmd.Transaction = tx;
            var seq = Convert.ToInt32(seqCmd.ExecuteScalar(), CultureInfo.InvariantCulture);
            using var cmd = Command(
                "INSERT INTO messages(thread_id, seq, role, content, tool_calls, tool_call_id, created_at) VALUES($t, $s, $r, $c, $tc, $tid, $n) RETURNING id",
                ("$t", threadId), ("$s", seq), ("$r", message.Role), ("$c", message.Content), ("$tc", ToolCallsJson(message.ToolCalls)), ("$tid", message.ToolCallId), ("$n", now));
            cmd.Transaction = tx;
            var id = Convert.ToInt64(cmd.ExecuteScalar(), CultureInfo.InvariantCulture);
            using var touch = Command("UPDATE threads SET updated_at = $n WHERE id = $t", ("$n", now), ("$t", threadId));
            touch.Transaction = tx;
            touch.ExecuteNonQuery();
            tx.Commit();
            return new StoredMessage(id, threadId, seq, message, Parse(now));
        }
    }

    public IReadOnlyList<StoredMessage> Messages(string threadId)
    {
        lock (gate)
        {
            using var cmd = Command("SELECT id, seq, role, content, tool_calls, tool_call_id, created_at FROM messages WHERE thread_id = $t ORDER BY seq", ("$t", threadId));
            using var r = cmd.ExecuteReader();
            var list = new List<StoredMessage>();
            while (r.Read())
            {
                var msg = new ChatMessage(r.GetString(2), r.IsDBNull(3) ? null : r.GetString(3), r.IsDBNull(4) ? null : ParseToolCalls(r.GetString(4)), r.IsDBNull(5) ? null : r.GetString(5));
                list.Add(new StoredMessage(r.GetInt64(0), threadId, r.GetInt32(1), msg, Parse(r.GetString(6))));
            }

            return list;
        }
    }

    /// <summary>Copies a thread up to and including message <paramref name="uptoSeq"/> into a new thread (a branch to explore a different follow-up).</summary>
    public ThreadInfo ForkThread(string threadId, int uptoSeq, string title)
    {
        var fork = CreateThread(title, threadId);
        foreach (var m in Messages(threadId).Where(m => m.Seq <= uptoSeq))
            AppendMessage(fork.Id, m.Message);
        return fork;
    }

    // ---- benchmark runs -----------------------------------------------------------------------------

    public long SaveBenchRun(JsonObject result)
    {
        lock (gate)
        {
            using var cmd = Command(
                "INSERT INTO bench_runs(created_at, model, mode, suite_version, score, result_json) VALUES($n, $m, $mode, $v, $s, $j) RETURNING id",
                ("$n", Now()),
                ("$m", result["model"]?["name"]?.GetValue<string>() ?? ""),
                ("$mode", result["mode"]?.GetValue<string>() ?? ""),
                ("$v", result["suite"]?["version"]?.GetValue<string>() ?? ""),
                ("$s", result["metrics"]?["score"]?.GetValue<double>() ?? 0),
                ("$j", result.ToJsonString()));
            return Convert.ToInt64(cmd.ExecuteScalar(), CultureInfo.InvariantCulture);
        }
    }

    public IReadOnlyList<BenchRow> BenchRuns(int limit = 100)
    {
        lock (gate)
        {
            using var cmd = Command("SELECT id, created_at, model, mode, suite_version, score, submitted, result_json FROM bench_runs ORDER BY id DESC LIMIT $l", ("$l", limit));
            using var r = cmd.ExecuteReader();
            var list = new List<BenchRow>();
            while (r.Read())
                list.Add(new BenchRow(r.GetInt64(0), Parse(r.GetString(1)), r.GetString(2), r.GetString(3), r.GetString(4), r.GetDouble(5), r.GetInt64(6) != 0, r.GetString(7)));
            return list;
        }
    }

    public void MarkSubmitted(long id)
    {
        lock (gate)
        {
            using var cmd = Command("UPDATE bench_runs SET submitted = 1 WHERE id = $id", ("$id", id));
            cmd.ExecuteNonQuery();
        }
    }

    // ---- model capabilities ------------------------------------------------------------------------

    public string? GetCapability(string modelKey)
    {
        lock (gate)
        {
            using var cmd = Command("SELECT tool_calling FROM model_caps WHERE model_key = $k", ("$k", modelKey));
            return cmd.ExecuteScalar() as string;
        }
    }

    public void SetCapability(string modelKey, string toolCalling)
    {
        lock (gate)
        {
            using var cmd = Command(
                "INSERT INTO model_caps(model_key, tool_calling, probed_at) VALUES($k, $v, $n) ON CONFLICT(model_key) DO UPDATE SET tool_calling = excluded.tool_calling, probed_at = excluded.probed_at",
                ("$k", modelKey), ("$v", toolCalling), ("$n", Now()));
            cmd.ExecuteNonQuery();
        }
    }

    public void Dispose()
    {
        lock (gate)
            db.Dispose();
    }

    // ---- helpers ------------------------------------------------------------------------------------

    internal static string? ToolCallsJson(IReadOnlyList<ToolCall>? calls) =>
        calls is not { Count: > 0 } ? null : JsonSerializer.Serialize(calls.Select(c => new { id = c.Id, name = c.Name, arguments = c.Arguments }));

    internal static IReadOnlyList<ToolCall>? ParseToolCalls(string json)
    {
        if (JsonNode.Parse(json) is not JsonArray array)
            return null;
        return array.Select(n => new ToolCall(n!["id"]!.GetValue<string>(), n["name"]!.GetValue<string>(), n["arguments"]!.GetValue<string>())).ToList();
    }

    private static string Now() => DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture);

    private static DateTimeOffset Parse(string s) => DateTimeOffset.Parse(s, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind);

    private void Exec(string sql)
    {
        using var cmd = db.CreateCommand();
        cmd.CommandText = sql;
        cmd.ExecuteNonQuery();
    }

    private T Scalar<T>(string sql)
    {
        using var cmd = db.CreateCommand();
        cmd.CommandText = sql;
        return (T)Convert.ChangeType(cmd.ExecuteScalar()!, typeof(T), CultureInfo.InvariantCulture);
    }

    private SqliteCommand Command(string sql, params (string Name, object? Value)[] parameters)
    {
        var cmd = db.CreateCommand();
        cmd.CommandText = sql;
        foreach (var (name, value) in parameters)
            cmd.Parameters.AddWithValue(name, value ?? DBNull.Value);
        return cmd;
    }
}
