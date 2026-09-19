using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Almanac.Core.Llm;

namespace Almanac.Core.Bench;

/// <summary>One tool call made during a task, as recorded by the runner.</summary>
public sealed record CallRecord(string Name, string RawArguments, bool Valid, string? InvalidReason, JsonNode? Result);

public sealed record TaskScore(double ToolScore, double? AnswerScore, double Score, bool Success, string? Error, int ValidCalls, int TotalCalls);

/// <summary>Scoring rules of benchmark/README.md ("Scoring"). Pure: the runner feeds it what happened.</summary>
public static partial class Scorer
{
    /// <summary>Checks a raw call against the offered tools: returns (valid, reason, parsed arguments).</summary>
    public static (bool Valid, string? Reason, JsonObject? Args) Validate(IReadOnlyList<ToolDef> offered, string name, string rawArguments)
    {
        var tool = offered.FirstOrDefault(t => t.Name == name);
        if (tool == null)
            return (false, $"unknown tool {name}", null);
        JsonNode? parsed;
        try
        {
            parsed = string.IsNullOrWhiteSpace(rawArguments) ? new JsonObject() : JsonNode.Parse(rawArguments);
        }
        catch (JsonException)
        {
            return (false, "arguments are not valid JSON", null);
        }

        if (parsed is not JsonObject args)
            return (false, "arguments must be a JSON object", null);
        var reason = SchemaLite.Validate(tool.InputSchema, args);
        return (reason == null, reason, args);
    }

    public static TaskScore Score(SuiteTask task, double toolWeight, double answerWeight, bool live, IReadOnlyList<CallRecord> calls, string? finalAnswer, string? transportError = null)
    {
        var expect = task.Expect;
        var total = calls.Count;
        var valid = calls.Count(c => c.Valid);

        // ---- tool score
        var expected = expect["calls"] as JsonArray ?? [];
        double toolScore;
        if (expected.Count == 0)
        {
            var forbid = expect["forbid_any_call"] is JsonValue fv && fv.TryGetValue<bool>(out var f) && f;
            toolScore = forbid && total > 0 ? 0 : 1;
        }
        else
        {
            var used = new bool[calls.Count];
            var matched = 0;
            foreach (var e in expected)
            {
                var name = e!["name"]!.GetValue<string>();
                var argSpec = e["args"] as JsonObject;
                for (var i = 0; i < calls.Count; i++)
                {
                    if (used[i] || !calls[i].Valid || calls[i].Name != name)
                        continue;
                    var args = JsonNode.Parse(string.IsNullOrWhiteSpace(calls[i].RawArguments) ? "{}" : calls[i].RawArguments) as JsonObject ?? [];
                    if (!Matchers.MatchAll(argSpec, args))
                        continue;
                    used[i] = true;
                    matched++;
                    break;
                }
            }

            toolScore = (double)matched / expected.Count;
        }

        if (total > 0)
            toolScore *= (double)valid / total;

        // ---- answer score
        double? answerScore = live && task.LiveSkipAnswer ? null : AnswerScore(expect["answer"] as JsonObject, calls, finalAnswer);

        var score = answerScore is { } a ? toolWeight * toolScore + answerWeight * a : toolScore;
        score = Math.Round(score, 6);
        var success = score >= 1 - 1e-9;

        string? error = transportError;
        if (error == null)
        {
            if (string.IsNullOrWhiteSpace(finalAnswer))
                error = "no_answer";
            else if (valid < total)
                error = "bad_tool_call";
            else if (answerScore is { } aa && aa < 1 - 1e-9)
                error = "wrong_answer";
        }

        return new TaskScore(toolScore, answerScore, score, success, error, valid, total);
    }

    public static double AnswerScore(JsonObject? spec, IReadOnlyList<CallRecord> calls, string? answer)
    {
        if (string.IsNullOrWhiteSpace(answer))
            return 0;
        if (spec == null || spec.Count == 0)
            return 1;
        var checks = 0;
        var passed = 0;

        void Check(bool ok)
        {
            checks++;
            if (ok)
                passed++;
        }

        bool Has(string needle) => answer.Contains(needle, StringComparison.OrdinalIgnoreCase);

        foreach (var s in Strings(spec["contains_all"]))
            Check(Has(s));
        if (spec["contains_any"] is JsonArray any)
            Check(any.Any(n => n is JsonValue v && v.TryGetValue<string>(out var s) && Has(s)));
        foreach (var s in Strings(spec["not_contains"]))
            Check(!Has(s));
        if (spec["contains_from_tool"] is JsonArray fromTool)
        {
            foreach (var entry in fromTool)
            {
                var value = FromTool(calls, entry);
                Check(ValueText(value) is { Length: > 0 } s && Has(s));
            }
        }

        if (spec["numbers_from_tool"] is JsonArray numbersFromTool)
        {
            var numbers = Numbers(answer);
            foreach (var entry in numbersFromTool)
            {
                var target = Matchers.Num(FromTool(calls, entry));
                var tolerance = entry?["tolerance"] is { } t ? Matchers.Num(t) ?? 0 : 0;
                Check(target is { } x && numbers.Any(n => Math.Abs(n - x) <= tolerance + 1e-9));
            }
        }

        if (spec["exact"] is JsonValue exact && exact.TryGetValue<string>(out var exactText))
            Check(string.Equals(NormalizeExact(answer), NormalizeExact(exactText), StringComparison.OrdinalIgnoreCase));
        if (spec["max_chars"] is { } maxChars && Matchers.Num(maxChars) is { } max)
            Check(answer.Length <= max);

        return checks == 0 ? 1 : (double)passed / checks;
    }

    public static string NormalizeExact(string text)
    {
        var s = text.Trim();
        s = PromptedTools.StripFence(s).Trim();
        s = s.Trim('`', '"', '\'').Trim();
        s = Whitespace().Replace(s, " ");
        if (s.EndsWith('.'))
            s = s[..^1];
        return s.Trim();
    }

    public static List<double> Numbers(string text)
    {
        var cleaned = Thousands().Replace(text, "");
        return NumberPattern().Matches(cleaned).Select(m => double.Parse(m.Value, CultureInfo.InvariantCulture)).ToList();
    }

    /// <summary>Text of a scalar for contains_from_tool: strings as is, numbers in shortest form (no ".0"), true/false.</summary>
    internal static string? ValueText(JsonNode? value)
    {
        if (value is not JsonValue v)
            return null;
        return v.GetValueKind() switch
        {
            JsonValueKind.String => v.GetValue<string>(),
            JsonValueKind.Number => Matchers.Num(v)!.Value.ToString("R", CultureInfo.InvariantCulture),
            JsonValueKind.True => "true",
            JsonValueKind.False => "false",
            _ => null,
        };
    }

    private static JsonNode? FromTool(IReadOnlyList<CallRecord> calls, JsonNode? entry)
    {
        var tool = entry?["tool"]?.GetValue<string>();
        var path = entry?["path"]?.GetValue<string>() ?? "";
        var first = calls.FirstOrDefault(c => c.Valid && c.Name == tool && c.Result != null);
        return first == null ? null : JsonPathLite.Resolve(first.Result, path);
    }

    private static IEnumerable<string> Strings(JsonNode? node) =>
        node is JsonArray a ? a.Select(n => n is JsonValue v && v.TryGetValue<string>(out var s) ? s : null).OfType<string>() : [];

    [GeneratedRegex(@"\s+")]
    private static partial Regex Whitespace();

    [GeneratedRegex(@"(?<=\d),(?=\d{3}(?!\d))")]
    private static partial Regex Thousands();

    [GeneratedRegex(@"-?\d+(?:\.\d+)?")]
    private static partial Regex NumberPattern();
}
