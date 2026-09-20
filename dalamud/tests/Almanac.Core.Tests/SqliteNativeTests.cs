using Microsoft.Data.Sqlite;

namespace Almanac.Core.Tests;

/// <summary>
/// The native SQLite that actually loads, not the version of the NuGet wrapper around it.
/// CVE-2025-6965 (GHSA-2m69-gcr7-jv3q) is fixed in SQLite 3.50.2; a dependency change that
/// quietly brings an older native library back fails here.
/// </summary>
public class SqliteNativeTests
{
    static readonly Version Fixed = new(3, 50, 2);

    [Fact]
    public void LoadedSqliteIsNotOlderThanTheFixedRelease()
    {
        using var db = new SqliteConnection("Data Source=:memory:");
        db.Open();
        using var cmd = db.CreateCommand();
        cmd.CommandText = "select sqlite_version()";
        var reported = (string)cmd.ExecuteScalar()!;
        Assert.True(Version.Parse(reported) >= Fixed, $"SQLite {reported} is older than {Fixed}");
    }
}
