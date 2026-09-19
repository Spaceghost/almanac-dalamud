using System.Text.Json.Nodes;
using Almanac.Core.Llm;

namespace Almanac.Core.Setup;

/// <summary>Whether a model can call tools: what we know by name, what Ollama reports, and a live probe.</summary>
public static class ModelCapabilities
{
    public const string Native = "native";
    public const string Prompted = "prompted";
    public const string Unknown = "unknown";

    // Families whose common local builds (Ollama templates, LM Studio/llama.cpp chat templates) emit native tool calls.
    private static readonly string[] NativeFamilies =
    [
        "qwen2.5", "qwen3", "qwq", "llama3.1", "llama3.2", "llama3.3", "llama-3.1", "llama-3.2", "llama-3.3", "llama4", "mistral-small",
        "mistral-nemo", "mistral-large", "ministral", "devstral", "magistral", "command-r", "granite3", "granite-3", "granite4", "hermes3",
        "gpt-oss", "firefunction", "smollm2", "phi4-mini", "nemotron", "glm4", "glm-4",
    ];

    // Families usually shipped without a tool-call template: they work through the prompted fallback.
    private static readonly string[] PromptedFamilies = ["gemma", "phi3", "phi-3", "phi4", "deepseek-r1", "llama2", "llama3:", "llama3-", "tinyllama", "mistral:7b"];

    /// <summary>A guess from the model id alone.</summary>
    public static string FromName(string modelId)
    {
        var id = modelId.ToLowerInvariant();
        var slash = id.LastIndexOf('/');
        var bare = slash >= 0 ? id[(slash + 1)..] : id;
        foreach (var f in NativeFamilies)
        {
            if (bare.StartsWith(f, StringComparison.Ordinal) || bare.Contains(f, StringComparison.Ordinal))
                return Native;
        }

        foreach (var f in PromptedFamilies)
        {
            if (bare.StartsWith(f, StringComparison.Ordinal))
                return Prompted;
        }

        return Unknown;
    }

    /// <summary>Reads Ollama's POST /api/show <c>capabilities</c> (present in current Ollama versions).</summary>
    public static string? FromOllamaShow(JsonNode? show)
    {
        if (show?["capabilities"] is not JsonArray caps)
            return null;
        return caps.Any(c => c?.GetValue<string>() == "tools") ? Native : Prompted;
    }

    /// <summary>
    /// Asks the model to call a trivial tool. Native when it answers with a tool call; prompted when the backend rejects
    /// tools or the model answers in text.
    /// </summary>
    public static async Task<string> ProbeAsync(IChatBackend chat, string model, CancellationToken ct)
    {
        var tool = new ToolDef("get_time", "Returns the current Eorzea time.", new JsonObject { ["type"] = "object", ["properties"] = new JsonObject() });
        try
        {
            var result = await chat.CompleteAsync(new ChatRequest
            {
                Model = model,
                Messages = [ChatMessage.System("Use tools when they help."), ChatMessage.User("What time is it in Eorzea? Use the tool.")],
                Tools = [tool],
                Temperature = 0,
                MaxTokens = 256,
            }, null, ct).ConfigureAwait(false);
            return result.ToolCalls.Any(c => c.Name == "get_time") ? Native : Prompted;
        }
        catch (ToolsUnsupportedException)
        {
            return Prompted;
        }
    }

    public static ToolCallingMode ToMode(string capability) => capability == Prompted ? ToolCallingMode.Prompted : ToolCallingMode.Native;

    /// <summary>Parses Ollama /api/show into facts for a benchmark result.</summary>
    public static (string? Quant, int? Context, string? Family, double? ParamsB) OllamaFacts(JsonNode? show)
    {
        var details = show?["details"];
        var quant = details?["quantization_level"]?.GetValue<string>();
        var family = details?["family"]?.GetValue<string>();
        double? paramsB = null;
        if (details?["parameter_size"]?.GetValue<string>() is { } ps)
        {
            var s = ps.Trim().ToUpperInvariant();
            if (s.EndsWith('B') && double.TryParse(s[..^1], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var b))
                paramsB = b;
            else if (s.EndsWith('M') && double.TryParse(s[..^1], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var m))
                paramsB = m / 1000;
        }

        int? context = null;
        if (show?["model_info"] is JsonObject info)
        {
            foreach (var (key, value) in info)
            {
                if (key.EndsWith(".context_length", StringComparison.Ordinal) && value is JsonValue v && v.TryGetValue<int>(out var n))
                    context = n;
            }
        }

        if (show?["parameters"]?.GetValue<string>() is { } parameters)
        {
            foreach (var line in parameters.Split('\n'))
            {
                var parts = line.Split(' ', StringSplitOptions.RemoveEmptyEntries);
                if (parts.Length == 2 && parts[0] == "num_ctx" && int.TryParse(parts[1], out var numCtx))
                    context = numCtx;
            }
        }

        return (quant, context, family, paramsB);
    }
}
