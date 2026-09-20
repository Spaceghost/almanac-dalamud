using System.Text.Json;
using System.Text.Json.Nodes;

namespace Almanac.Core;

/// <summary>One entry in the changelog: what changed, and how far it has been verified.</summary>
/// <param name="Status">new, fix, beta or next (see <see cref="Changelog.Label"/>).</param>
public sealed record ChangelogItem(string Status, string Text);

/// <summary>A release, or the unreleased "In the workshop" section when <paramref name="Version"/> is "next".</summary>
public sealed record ChangelogRelease(string Version, string Title, string Blurb, IReadOnlyList<ChangelogItem> Items)
{
    public bool Unreleased => Version == "next";

    /// <summary>"0.1 · Title", or just the title for the unreleased section.</summary>
    public string Heading => Unreleased ? Title : $"{Version} · {Title}";
}

/// <summary>
/// The changelog players read in the What's new view. It is generated from nothing: changelog.json at
/// the top of the repository is the source of truth, embedded here at build time and rendered into
/// CHANGELOG.md by tools/changelog.py, so the two can never disagree.
/// </summary>
public sealed class Changelog
{
    public const string BundledResource = "Almanac.Core.changelog.json";

    public required string Intro { get; init; }

    public required IReadOnlyList<ChangelogRelease> Releases { get; init; }

    /// <summary>What a status word means, in the same words everywhere these mods use it.</summary>
    public static string Label(string status) => status switch
    {
        "new" => "NEW",
        "fix" => "FIX",
        "beta" => "BETA",
        "next" => "SOON",
        _ => status.ToUpperInvariant(),
    };

    public static Changelog Parse(string json)
    {
        var root = JsonNode.Parse(json) ?? throw new JsonException("empty");
        var releases = new List<ChangelogRelease>();
        foreach (var r in root["releases"]!.AsArray())
        {
            var items = new List<ChangelogItem>();
            foreach (var i in r!["items"]!.AsArray())
                items.Add(new ChangelogItem(i!["status"]!.GetValue<string>(), i["text"]!.GetValue<string>()));
            releases.Add(new ChangelogRelease(
                r["version"]!.GetValue<string>(),
                r["title"]!.GetValue<string>(),
                r["blurb"]?.GetValue<string>() ?? "",
                items));
        }

        if (releases.Count == 0)
            throw new JsonException("no releases");

        return new Changelog
        {
            Intro = root["intro"]?.GetValue<string>() ?? "",
            Releases = releases,
        };
    }

    public static Changelog Bundled()
    {
        using var stream = typeof(Changelog).Assembly.GetManifestResourceStream(BundledResource)!;
        using var reader = new StreamReader(stream);
        return Parse(reader.ReadToEnd());
    }
}
