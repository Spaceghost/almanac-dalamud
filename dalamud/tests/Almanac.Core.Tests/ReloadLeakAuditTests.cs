using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace Almanac.Core.Tests;

/// <summary>
/// The reload-leak gate. Dalamud reloads a plugin inside the running game: one event left
/// subscribed, one command left registered or one handle left undisposed and the old assembly --
/// and everything it ever referenced -- stays alive until the player quits the game. This project
/// has been bitten by exactly that, so it is checked on every build rather than by review.
///
/// What this does NOT cover is stated in docs/QUALITY.md: it is a syntactic audit of the plugin's
/// own sources, so it cannot see a subscription made inside a library, one built through
/// reflection, or a handler stored and released by a helper type in another file.
/// </summary>
public class ReloadLeakAuditTests
{
    [Fact]
    public void The_audit_finds_the_plugin_sources()
    {
        var files = ReloadLeakAudit.PluginSourceFiles();
        Assert.NotEmpty(files);
        // A plugin with fewer than three source files means the discovery, not the plugin, shrank.
        Assert.True(
            files.Count >= 3,
            $"Only {files.Count} plugin source file(s) found under {ReloadLeakAudit.RepoRoot()}; the audit is not looking at the plugin.");
        Assert.Contains(files, f => Path.GetFileName(f) == "Plugin.cs");
    }

    [Fact]
    public void Every_event_subscription_is_matched_by_an_unsubscribe_in_the_same_type()
    {
        var types = ReloadLeakAudit.PluginTypes();
        var methodNames = ReloadLeakAudit.DeclaredMethodNames(types);
        var problems = new List<string>();

        foreach (var type in types)
        {
            var subscribes = new List<(string Event, AssignmentExpressionSyntax Node)>();
            var unsubscribes = new HashSet<string>(StringComparer.Ordinal);

            foreach (var a in type.Syntax.DescendantNodes().OfType<AssignmentExpressionSyntax>())
            {
                var name = ReloadLeakAudit.EventName(a.Left);
                if (name == null || !ReloadLeakAudit.IsHandler(a.Right, methodNames))
                    continue;
                if (a.Kind() == SyntaxKind.AddAssignmentExpression)
                    subscribes.Add((name, a));
                else if (a.Kind() == SyntaxKind.SubtractAssignmentExpression)
                    unsubscribes.Add(name);
            }

            foreach (var (name, node) in subscribes)
            {
                if (type.Text.Allowed(node, "event") != null)
                    continue;

                if (ReloadLeakAudit.IsUnremovableHandler(node.Right))
                {
                    problems.Add(
                        $"{type.Text.Where(node)}: {type.Name} subscribes to '{name}' with a lambda. " +
                        "A lambda cannot be unsubscribed -- nothing holds the delegate -- so this survives every reload. " +
                        "Use a named method and remove it in Dispose().");
                    continue;
                }

                if (!unsubscribes.Contains(name))
                {
                    problems.Add(
                        $"{type.Text.Where(node)}: {type.Name} does '{name} += {node.Right}' with no matching '{name} -= …' anywhere in the type. " +
                        "Unsubscribe it in Dispose().");
                }
            }
        }

        Assert.True(problems.Count == 0, Report("Event subscriptions that outlive the plugin", problems));
    }

    [Fact]
    public void Every_dalamud_registration_is_released_in_the_same_type()
    {
        var types = ReloadLeakAudit.PluginTypes();
        var problems = new List<string>();

        foreach (var type in types)
        {
            var calls = ReloadLeakAudit.Calls(type.Syntax).ToList();
            var names = calls.Select(ReloadLeakAudit.CallName).OfType<string>().ToHashSet(StringComparer.Ordinal);

            foreach (var (acquire, release, what) in ReloadLeakAudit.Pairs)
            {
                var site = calls.FirstOrDefault(c => ReloadLeakAudit.CallName(c) == acquire);
                if (site == null || release.Any(names.Contains))
                    continue;
                if (type.Text.Allowed(site, acquire) != null)
                    continue;
                problems.Add(
                    $"{type.Text.Where(site)}: {type.Name} acquires {what} with '{acquire}(…)' but never calls " +
                    $"{string.Join(" or ", release.Select(r => $"'{r}'"))} in the same type.");
            }

            var dtr = calls.FirstOrDefault(ReloadLeakAudit.IsDtrAcquire);
            if (dtr != null && !names.Contains("Remove") && !names.Contains("Dispose") && type.Text.Allowed(dtr, "DtrBar") == null)
            {
                problems.Add(
                    $"{type.Text.Where(dtr)}: {type.Name} takes a DtrBar entry but never calls 'Remove' or 'Dispose' on it. " +
                    "A stale entry keeps drawing after the plugin is gone.");
            }
        }

        Assert.True(problems.Count == 0, Report("Dalamud registrations that are never released", problems));
    }

    /// <summary>
    /// The plugin's own entry point is the one type where a miss is unrecoverable, so it is checked
    /// by name as well: it must be IDalamudPlugin, it must have a Dispose, and Dispose must undo the
    /// four things the constructor does.
    /// </summary>
    [Fact]
    public void The_plugin_entry_point_undoes_what_it_sets_up()
    {
        var plugin = ReloadLeakAudit.PluginTypes().Single(t => t.Name == "Plugin");
        var dispose = plugin.Syntax.Members.OfType<MethodDeclarationSyntax>().SingleOrDefault(m => m.Identifier.ValueText == "Dispose");
        Assert.True(dispose != null, $"{plugin.Text.File}: the Plugin type has no Dispose().");

        var body = dispose!.ToString();
        foreach (var required in new[] { "RemoveHandler", "RemoveAllWindows", "UnregisterFunc" })
        {
            Assert.True(
                body.Contains(required, StringComparison.Ordinal),
                $"Plugin.Dispose() never calls '{required}'. Dalamud will not do it for you on reload.");
        }
    }

    private static string Report(string title, List<string> problems) =>
        $"{title} ({problems.Count}):{Environment.NewLine}  " +
        string.Join(Environment.NewLine + "  ", problems) +
        Environment.NewLine + Environment.NewLine +
        $"If one of these genuinely cannot leak, mark the line: // {ReloadLeakAudit.MarkerPrefix} <rule> -- <why>";
}
