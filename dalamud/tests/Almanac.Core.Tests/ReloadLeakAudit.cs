using System.Reflection;
using System.Text.RegularExpressions;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace Almanac.Core.Tests;

/// <summary>
/// Finds the plugin's C# sources on disk and parses them. The audit is source-level on purpose:
/// Roslyn's own analyzers see one compilation unit at a time and have no rule for "you subscribed
/// to a Dalamud event and never unsubscribed", which is the single defect that leaks the entire
/// game process on a plugin reload.
/// </summary>
internal static class ReloadLeakAudit
{
    /// <summary>
    /// A line may opt out with a marker comment, but only with a reason:
    /// <c>// leak-audit: allow &lt;rule&gt; -- &lt;why this one cannot leak&gt;</c>
    /// </summary>
    public const string MarkerPrefix = "leak-audit: allow";

    private static readonly Regex Marker = new(
        @"//\s*leak-audit:\s*allow\s+(?<rule>[A-Za-z0-9+=.-]+)\s*(?:--|—)\s*(?<reason>.+)$",
        RegexOptions.Compiled | RegexOptions.CultureInvariant);

    /// <summary>The repository root, from the assembly metadata the csproj bakes in, else by walking up.</summary>
    public static string RepoRoot()
    {
        var baked = typeof(ReloadLeakAudit).Assembly
            .GetCustomAttributes<AssemblyMetadataAttribute>()
            .FirstOrDefault(a => a.Key == "AlmanacRepoRoot")?.Value;
        if (baked != null && LooksLikeRepo(baked))
            return Path.GetFullPath(baked);

        for (var dir = AppContext.BaseDirectory; dir != null; dir = Path.GetDirectoryName(dir.TrimEnd(Path.DirectorySeparatorChar)))
        {
            if (LooksLikeRepo(dir))
                return Path.GetFullPath(dir);
        }

        throw new InvalidOperationException(
            $"The reload-leak audit could not find the repository. AssemblyMetadata AlmanacRepoRoot was '{baked ?? "(absent)"}' " +
            $"and no ancestor of '{AppContext.BaseDirectory}' contains dalamud/src/Almanac.Plugin. " +
            "Fix the source discovery rather than skipping the audit: a silent zero-file audit is worse than no audit.");
    }

    private static bool LooksLikeRepo(string dir) =>
        Directory.Exists(Path.Combine(dir, "dalamud", "src", "Almanac.Plugin"));

    /// <summary>Every .cs file of the plugin assembly, excluding build output.</summary>
    public static IReadOnlyList<string> PluginSourceFiles()
    {
        var root = Path.Combine(RepoRoot(), "dalamud", "src", "Almanac.Plugin");
        var files = Directory.EnumerateFiles(root, "*.cs", SearchOption.AllDirectories)
            .Where(f => !f.Contains($"{Path.DirectorySeparatorChar}obj{Path.DirectorySeparatorChar}", StringComparison.Ordinal))
            .Where(f => !f.Contains($"{Path.DirectorySeparatorChar}bin{Path.DirectorySeparatorChar}", StringComparison.Ordinal))
            .OrderBy(f => f, StringComparer.Ordinal)
            .ToList();

        if (files.Count == 0)
        {
            throw new InvalidOperationException(
                $"The reload-leak audit found no .cs files under '{root}'. It must never pass by finding nothing.");
        }

        return files;
    }

    public sealed record TypeSource(string Name, string File, TypeDeclarationSyntax Syntax, SourceText Text);

    public static IReadOnlyList<TypeSource> PluginTypes()
    {
        var types = new List<TypeSource>();
        foreach (var file in PluginSourceFiles())
        {
            var text = File.ReadAllText(file);
            var tree = CSharpSyntaxTree.ParseText(text, path: file);
            var source = new SourceText(file, text);
            foreach (var decl in tree.GetRoot().DescendantNodes().OfType<TypeDeclarationSyntax>())
                types.Add(new TypeSource(decl.Identifier.ValueText, file, decl, source));
        }

        return types;
    }

    /// <summary>A file plus enough to turn a syntax node into "path:line" and to read its trailing comment.</summary>
    public sealed class SourceText(string file, string text)
    {
        private readonly string[] lines = text.Replace("\r\n", "\n", StringComparison.Ordinal).Split('\n');

        public string File { get; } = file;

        public static int LineOf(SyntaxNode node) => node.GetLocation().GetLineSpan().StartLinePosition.Line + 1;

        public string Where(SyntaxNode node) => $"{Path.GetFileName(File)}:{LineOf(node)}";

        public string LineText(int line) => line >= 1 && line <= lines.Length ? lines[line - 1] : "";

        /// <summary>The opt-out reason for <paramref name="rule"/> on this line or the one above it, or null.</summary>
        public string? Allowed(SyntaxNode node, string rule)
        {
            var line = LineOf(node);
            foreach (var candidate in new[] { LineText(line), LineText(line - 1) })
            {
                var m = Marker.Match(candidate);
                if (!m.Success)
                    continue;
                if (!string.Equals(m.Groups["rule"].Value, rule, StringComparison.OrdinalIgnoreCase))
                    continue;
                var reason = m.Groups["reason"].Value.Trim();
                if (reason.Length < 15)
                {
                    throw new InvalidOperationException(
                        $"{Where(node)}: '{MarkerPrefix} {rule}' needs a real reason (at least 15 characters), got '{reason}'.");
                }

                return reason;
            }

            return null;
        }
    }

    /// <summary>Method names declared anywhere in the plugin, used to tell an event handler from arithmetic.</summary>
    public static HashSet<string> DeclaredMethodNames(IReadOnlyList<TypeSource> types)
    {
        var names = new HashSet<string>(StringComparer.Ordinal);
        foreach (var t in types)
        {
            foreach (var m in t.Syntax.Members.OfType<MethodDeclarationSyntax>())
                names.Add(m.Identifier.ValueText);
            foreach (var p in t.Syntax.Members.OfType<PropertyDeclarationSyntax>())
                names.Add(p.Identifier.ValueText);
        }

        // Handlers that live on framework types the plugin subscribes with directly.
        names.UnionWith(["Draw", "Update", "Invalidate", "Dispose", "Toggle"]);
        return names;
    }

    /// <summary>The member name on the left of a <c>+=</c> / <c>-=</c>: <c>pi.UiBuilder.Draw</c> -&gt; <c>UiBuilder.Draw</c>.</summary>
    public static string? EventName(ExpressionSyntax lhs) => lhs switch
    {
        MemberAccessExpressionSyntax m when m.Expression is MemberAccessExpressionSyntax inner =>
            $"{inner.Name.Identifier.ValueText}.{m.Name.Identifier.ValueText}",
        MemberAccessExpressionSyntax m => $"{Describe(m.Expression)}.{m.Name.Identifier.ValueText}",
        IdentifierNameSyntax id => id.Identifier.ValueText,
        _ => null,
    };

    private static string Describe(ExpressionSyntax e) => e switch
    {
        IdentifierNameSyntax id => id.Identifier.ValueText,
        ThisExpressionSyntax => "this",
        _ => e.ToString(),
    };

    /// <summary>True when the right-hand side of a <c>+=</c> looks like a delegate rather than a number or a string.</summary>
    public static bool IsHandler(ExpressionSyntax rhs, HashSet<string> methodNames) => rhs switch
    {
        LambdaExpressionSyntax or AnonymousMethodExpressionSyntax => true,
        ObjectCreationExpressionSyntax o => o.Type.ToString().EndsWith("Handler", StringComparison.Ordinal)
            || o.Type.ToString().StartsWith("Action", StringComparison.Ordinal)
            || o.Type.ToString().StartsWith("EventHandler", StringComparison.Ordinal),
        IdentifierNameSyntax id => methodNames.Contains(id.Identifier.ValueText),
        MemberAccessExpressionSyntax m => methodNames.Contains(m.Name.Identifier.ValueText),
        _ => false,
    };

    /// <summary>True when the handler cannot be unsubscribed at all, because nothing holds a reference to it.</summary>
    public static bool IsUnremovableHandler(ExpressionSyntax rhs) =>
        rhs is LambdaExpressionSyntax or AnonymousMethodExpressionSyntax;

    /// <summary>Acquire/release pairs: a call named Key must be answered by one of Value in the same type.</summary>
    public static readonly (string Acquire, string[] Release, string What)[] Pairs =
    [
        ("AddHandler", ["RemoveHandler"], "a slash command registered with ICommandManager"),
        ("AddWindow", ["RemoveWindow", "RemoveAllWindows"], "a window added to a WindowSystem"),
        ("RegisterFunc", ["UnregisterFunc"], "an IPC provider function"),
        ("RegisterAction", ["UnregisterAction"], "an IPC provider action"),
        ("Subscribe", ["Unsubscribe"], "an IPC subscriber"),
        ("HookFromAddress", ["Dispose"], "a Dalamud hook"),
        ("CreateHook", ["Dispose"], "a Dalamud hook"),
        ("NewDelegateFontHandle", ["Dispose"], "a font handle"),
        ("NewGameFontHandle", ["Dispose"], "a font handle"),
        ("CreateFontAtlas", ["Dispose"], "a font atlas"),
        ("CreateFromImageAsync", ["Dispose"], "a texture wrap"),
        ("CreateFromRaw", ["Dispose"], "a texture wrap"),
        ("CreateFromExistingTextureAsync", ["Dispose"], "a texture wrap"),
        ("CreateEmpty", ["Dispose"], "a texture wrap"),
    ];

    /// <summary>
    /// DtrBar entries are acquired by <c>DtrBar.Get(...)</c>, which is too common a method name to
    /// match on its own; the receiver has to mention the bar.
    /// </summary>
    public static bool IsDtrAcquire(InvocationExpressionSyntax call) =>
        call.Expression is MemberAccessExpressionSyntax { Name.Identifier.ValueText: "Get" } m
        && m.Expression.ToString().Contains("DtrBar", StringComparison.OrdinalIgnoreCase);

    public static IEnumerable<InvocationExpressionSyntax> Calls(TypeDeclarationSyntax type) =>
        type.DescendantNodes().OfType<InvocationExpressionSyntax>();

    public static string? CallName(InvocationExpressionSyntax call) => call.Expression switch
    {
        MemberAccessExpressionSyntax m => m.Name.Identifier.ValueText,
        IdentifierNameSyntax id => id.Identifier.ValueText,
        GenericNameSyntax g => g.Identifier.ValueText,
        MemberBindingExpressionSyntax b => b.Name.Identifier.ValueText,
        _ => null,
    };
}
