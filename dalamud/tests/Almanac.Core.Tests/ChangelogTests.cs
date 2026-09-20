namespace Almanac.Core.Tests;

/// <summary>
/// The changelog players read in game is changelog.json, embedded in this assembly. CHANGELOG.md is
/// rendered from the same file by tools/changelog.py (CI runs it with --check), so these tests only
/// have to keep the in-game side honest: the file parses, and every entry carries a status word the
/// view knows how to label.
/// </summary>
public sealed class ChangelogTests
{
    [Fact]
    public void BundledChangelogParses()
    {
        var log = Changelog.Bundled();
        Assert.NotEmpty(log.Releases);
        Assert.All(log.Releases, r =>
        {
            Assert.NotEmpty(r.Version);
            Assert.NotEmpty(r.Title);
            Assert.NotEmpty(r.Items);
            Assert.All(r.Items, i => Assert.NotEmpty(i.Text));
        });
    }

    [Fact]
    public void EveryStatusHasALabel()
    {
        string[] known = ["new", "fix", "beta", "next"];
        foreach (var release in Changelog.Bundled().Releases)
            foreach (var item in release.Items)
            {
                Assert.Contains(item.Status, known);
                Assert.NotEmpty(Changelog.Label(item.Status));
            }
    }

    [Fact]
    public void UnreleasedSectionIsHeadedByItsTitleAlone()
    {
        var unreleased = new ChangelogRelease("next", "In the workshop", "", [new ChangelogItem("beta", "x")]);
        var released = new ChangelogRelease("0.1", "First light", "", [new ChangelogItem("new", "x")]);
        Assert.True(unreleased.Unreleased);
        Assert.Equal("In the workshop", unreleased.Heading);
        Assert.False(released.Unreleased);
        Assert.Equal("0.1 · First light", released.Heading);
    }
}
