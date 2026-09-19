using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Almanac.Core.Bench;

/// <summary>Argument matchers of the suite (benchmark/README.md, "Argument matchers").</summary>
public static class Matchers
{
    /// <summary>True when every key of <paramref name="spec"/> matches the argument of that name.</summary>
    public static bool MatchAll(JsonObject? spec, JsonObject args)
    {
        if (spec == null)
            return true;
        foreach (var (key, matcher) in spec)
        {
            if (!args.TryGetPropertyValue(key, out var value) || value == null)
                return false;
            if (!Match(matcher, value))
                return false;
        }

        return true;
    }

    public static bool Match(JsonNode? matcher, JsonNode value)
    {
        if (matcher is JsonObject m && m.Count >= 1)
        {
            if (m.TryGetPropertyValue("eq", out var eq))
                return JsonEquals(eq, value);
            if (m.TryGetPropertyValue("approx", out var approx))
            {
                var tolerance = m["tolerance"] is { } t ? Num(t) ?? 0 : 0;
                return Num(value) is { } n && Num(approx) is { } target && Math.Abs(n - target) <= tolerance + 1e-9;
            }

            if (m.TryGetPropertyValue("icontains_any", out var needles))
            {
                if (value is not JsonValue sv || !sv.TryGetValue<string>(out var s))
                    return false;
                return needles is JsonArray array && array.Any(n => n is JsonValue nv && nv.TryGetValue<string>(out var needle) && s.Contains(needle, StringComparison.OrdinalIgnoreCase));
            }
        }

        return JsonEquals(matcher, value);
    }

    public static bool JsonEquals(JsonNode? a, JsonNode? b)
    {
        if (a == null || b == null)
            return a == null && b == null;
        if (Num(a) is { } x && Num(b) is { } y && IsNumber(a) && IsNumber(b))
            return Math.Abs(x - y) < 1e-9;
        return JsonNode.DeepEquals(a, b);
    }

    public static bool IsNumber(JsonNode node) => node is JsonValue v && v.GetValueKind() == JsonValueKind.Number;

    public static double? Num(JsonNode? node)
    {
        if (node is not JsonValue v || v.GetValueKind() != JsonValueKind.Number)
            return null;
        return v.TryGetValue<double>(out var d) ? d : double.Parse(v.ToJsonString(), CultureInfo.InvariantCulture);
    }
}

/// <summary>Paths into tool results: dotted keys, <c>[n]</c> and <c>[key~text]</c>.</summary>
public static class JsonPathLite
{
    public static JsonNode? Resolve(JsonNode? root, string path)
    {
        var node = root;
        var i = 0;
        while (node != null && i < path.Length)
        {
            if (path[i] == '.')
            {
                i++;
                continue;
            }

            if (path[i] == '[')
            {
                var end = path.IndexOf(']', i);
                if (end < 0)
                    return null;
                var inside = path[(i + 1)..end];
                i = end + 1;
                if (node is not JsonArray array)
                    return null;
                var tilde = inside.IndexOf('~');
                if (tilde >= 0)
                {
                    var key = inside[..tilde];
                    var text = inside[(tilde + 1)..];
                    node = array.FirstOrDefault(e => e?[key] is JsonValue v && v.TryGetValue<string>(out var s) && s.Contains(text, StringComparison.OrdinalIgnoreCase));
                }
                else if (int.TryParse(inside, NumberStyles.Integer, CultureInfo.InvariantCulture, out var index))
                {
                    node = index >= 0 && index < array.Count ? array[index] : null;
                }
                else
                {
                    return null;
                }

                continue;
            }

            var next = i;
            while (next < path.Length && path[next] != '.' && path[next] != '[')
                next++;
            var name = path[i..next];
            i = next;
            node = node is JsonObject o && o.TryGetPropertyValue(name, out var child) ? child : null;
        }

        return node;
    }
}

/// <summary>The JSON Schema subset the suite uses to decide whether a tool call is valid.</summary>
public static class SchemaLite
{
    /// <summary>Returns null when valid, otherwise a short reason.</summary>
    public static string? Validate(JsonObject schema, JsonNode? value, string where = "arguments")
    {
        if (schema["type"] is JsonValue tv && tv.TryGetValue<string>(out var type) && !TypeMatches(type, value))
            return $"{where} must be {type}";
        if (schema["enum"] is JsonArray options && !options.Any(o => Matchers.JsonEquals(o, value)))
            return $"{where} must be one of {options.ToJsonString()}";
        if (value is JsonValue && Matchers.Num(value) is { } n)
        {
            if (schema["minimum"] is { } min && Matchers.Num(min) is { } lo && n < lo)
                return $"{where} must be >= {lo.ToString(CultureInfo.InvariantCulture)}";
            if (schema["maximum"] is { } max && Matchers.Num(max) is { } hi && n > hi)
                return $"{where} must be <= {hi.ToString(CultureInfo.InvariantCulture)}";
        }

        if (value is JsonValue sv && sv.TryGetValue<string>(out var s) && schema["maxLength"] is { } ml && Matchers.Num(ml) is { } maxLen && s.Length > maxLen)
            return $"{where} is longer than {maxLen.ToString(CultureInfo.InvariantCulture)}";

        if (value is JsonObject obj)
        {
            var properties = schema["properties"] as JsonObject;
            if (schema["required"] is JsonArray required)
            {
                foreach (var r in required)
                {
                    var name = r?.GetValue<string>();
                    if (name != null && (!obj.TryGetPropertyValue(name, out var v) || v == null))
                        return $"missing required {name}";
                }
            }

            foreach (var (key, child) in obj)
            {
                if (properties != null && properties[key] is JsonObject childSchema)
                {
                    if (child == null)
                        continue; // null for an optional argument is treated as absent
                    if (Validate(childSchema, child, key) is { } reason)
                        return reason;
                }
                else if (schema["additionalProperties"] is JsonValue ap && ap.TryGetValue<bool>(out var allowed) && !allowed)
                {
                    return $"unknown argument {key}";
                }
            }
        }

        return null;
    }

    private static bool TypeMatches(string type, JsonNode? value) => type switch
    {
        "object" => value is JsonObject,
        "array" => value is JsonArray,
        "string" => value is JsonValue v && v.GetValueKind() == JsonValueKind.String,
        "boolean" => value is JsonValue v && v.GetValueKind() is JsonValueKind.True or JsonValueKind.False,
        "number" => value is JsonValue v && v.GetValueKind() == JsonValueKind.Number,
        "integer" => Matchers.Num(value) is { } n && Math.Abs(n - Math.Round(n)) < 1e-9,
        "null" => value == null,
        _ => true,
    };
}
