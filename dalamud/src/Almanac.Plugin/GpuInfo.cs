using System.Runtime.InteropServices;

namespace Almanac.Plugin;

public sealed record GpuAdapter(string Name, uint VendorId, long DedicatedVideoMemory)
{
    public int VramMb => (int)(DedicatedVideoMemory / (1024 * 1024));

    public string Vendor => VendorId switch
    {
        0x10DE => "nvidia",
        0x1002 or 0x1022 => "amd",
        0x8086 => "intel",
        0x106B => "apple",
        _ => "other",
    };
}

/// <summary>
/// GPU facts from Vulkan, falling back to DXGI, plus system RAM and whether we run under Wine. Vulkan comes first
/// because DXGI under DXVK lists only what the game may render on: DXVK_FILTER_DEVICE_NAME (the usual way to pin the
/// game to one card) hides every other GPU, including the one a model server runs on. Everything fails soft: neither
/// API means an empty list.
/// </summary>
public static unsafe class GpuInfo
{
    private static readonly Guid IidFactory1 = new("770aae78-f26f-4dba-a829-253c83d1b387");

    public static IReadOnlyList<GpuAdapter> Adapters()
    {
        var vulkan = Soft(EnumerateVulkan);
        return vulkan.Count > 0 ? vulkan : Soft(EnumerateAdapters);
    }

    private static List<GpuAdapter> Soft(Func<List<GpuAdapter>> enumerate)
    {
        try
        {
            return enumerate();
        }
        catch (Exception ex) when (ex is DllNotFoundException or EntryPointNotFoundException or SEHException or COMException)
        {
            return [];
        }
    }

    /// <summary>The adapter the game renders on: the first one DXGI lists. Null without DXGI.</summary>
    public static string? GameAdapterName() => Soft(EnumerateAdapters).FirstOrDefault()?.Name;

    /// <summary>
    /// Index of the adapter a model would run on: the most dedicated VRAM, and between equals the one the game does
    /// not render on. -1 for an empty list.
    /// </summary>
    public static int BestIndex(IReadOnlyList<GpuAdapter> adapters, string? gameAdapterName) =>
        adapters.Count == 0
            ? -1
            : adapters.Select((a, i) => (a, i))
                .OrderByDescending(x => x.a.DedicatedVideoMemory)
                .ThenBy(x => string.Equals(x.a.Name, gameAdapterName, StringComparison.OrdinalIgnoreCase))
                .First().i;

    /// <summary>The adapter a model would run on (see <see cref="BestIndex"/>).</summary>
    public static GpuAdapter? Best()
    {
        var adapters = Adapters();
        return adapters.Count == 0 ? null : adapters[BestIndex(adapters, GameAdapterName())];
    }

    public static int? SystemRamGb()
    {
        try
        {
            var status = new MemoryStatusEx { Length = (uint)sizeof(MemoryStatusEx) };
            return GlobalMemoryStatusEx(ref status) ? (int)Math.Round(status.TotalPhys / (1024.0 * 1024 * 1024)) : null;
        }
        catch (Exception ex) when (ex is DllNotFoundException or EntryPointNotFoundException)
        {
            return null;
        }
    }

    /// <summary>"linux" under Wine/Proton (it is a Linux machine), else "windows".</summary>
    /// <remarks>
    /// Some Wine builds (wine-xiv-staging, as XIVLauncher ships) hide <c>wine_get_version</c> from
    /// GetProcAddress, so it is only the first of three signs: Wine's <c>\\?\unix\</c> path namespace, which
    /// maps the host's root, and its <c>HKLM\Software\Wine</c> key are there in every build.
    /// </remarks>
    public static string OsFamily()
    {
        if (!OperatingSystem.IsWindows())
            return OperatingSystem.IsLinux() ? "linux" : OperatingSystem.IsMacOS() ? "macos" : "other";
        return IsWine() ? "linux" : "windows";
    }

    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    private static bool IsWine()
    {
        try
        {
            if (NativeLibrary.TryLoad("ntdll.dll", out var ntdll) && NativeLibrary.TryGetExport(ntdll, "wine_get_version", out _))
                return true;
        }
        catch (Exception ex) when (ex is BadImageFormatException or DllNotFoundException)
        {
        }

        try
        {
            if (Directory.Exists(@"\\?\unix\"))
                return true;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ArgumentException)
        {
        }

        try
        {
            using var key = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(@"Software\Wine");
            return key != null;
        }
        catch (Exception ex) when (ex is System.Security.SecurityException or UnauthorizedAccessException or IOException)
        {
            return false;
        }
    }

    private static List<GpuAdapter> EnumerateAdapters()
    {
        var list = new List<GpuAdapter>();
        var iid = IidFactory1;
        if (CreateDXGIFactory1(&iid, out var factory) < 0 || factory == IntPtr.Zero)
            return list;
        try
        {
            var vtbl = *(IntPtr**)factory;
            // IDXGIFactory1::EnumAdapters1 is vtable slot 12.
            var enumAdapters1 = (delegate* unmanaged[Stdcall]<IntPtr, uint, IntPtr*, int>)vtbl[12];
            for (uint i = 0; i < 16; i++)
            {
                IntPtr adapter;
                if (enumAdapters1(factory, i, &adapter) < 0 || adapter == IntPtr.Zero)
                    break;
                try
                {
                    // IDXGIAdapter1::GetDesc1 is vtable slot 10.
                    var avtbl = *(IntPtr**)adapter;
                    var getDesc1 = (delegate* unmanaged[Stdcall]<IntPtr, AdapterDesc1*, int>)avtbl[10];
                    AdapterDesc1 desc;
                    if (getDesc1(adapter, &desc) >= 0 && (desc.Flags & 2) == 0) // skip DXGI_ADAPTER_FLAG_SOFTWARE
                    {
                        var name = new string(desc.Description, 0, 128).TrimEnd('\0').Trim();
                        list.Add(new GpuAdapter(name, desc.VendorId, (long)desc.DedicatedVideoMemory));
                    }
                }
                finally
                {
                    Release(adapter);
                }
            }
        }
        finally
        {
            Release(factory);
        }

        return list;
    }

    private static List<GpuAdapter> EnumerateVulkan()
    {
        var list = new List<GpuAdapter>();
        // Vulkan 1.1 for vkGetPhysicalDeviceProperties2, which carries the device UUID.
        var app = new VkApplicationInfo { SType = 0, ApiVersion = (1u << 22) | (1u << 12) };
        var info = new VkInstanceCreateInfo { SType = 1, ApplicationInfo = (IntPtr)(&app) };
        if (vkCreateInstance(&info, null, out var instance) != 0 || instance == IntPtr.Zero)
            return list;
        try
        {
            uint count = 0;
            if (vkEnumeratePhysicalDevices(instance, &count, null) < 0 || count == 0)
                return list;
            count = Math.Min(count, 16);
            var devices = stackalloc IntPtr[(int)count];
            if (vkEnumeratePhysicalDevices(instance, &count, devices) < 0)
                return list;

            // VkPhysicalDeviceProperties is 824 bytes and VkPhysicalDeviceMemoryProperties 520 on 64-bit; only the
            // fixed-offset fields below are read.
            // VkPhysicalDeviceProperties2 is a 16-byte header then the 824 bytes above; VkPhysicalDeviceIDProperties
            // is 64 bytes with deviceUUID at 16.
            var props2 = stackalloc byte[1024];
            var props = props2 + 16;
            var id = stackalloc byte[64];
            var memory = stackalloc byte[1024];
            var seen = new HashSet<Guid>();
            for (var i = 0; i < count; i++)
            {
                new Span<byte>(props2, 1024).Clear();
                new Span<byte>(id, 64).Clear();
                *(uint*)props2 = 1000059001; // VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2
                *(IntPtr*)(props2 + 8) = (IntPtr)id;
                *(uint*)id = 1000071004; // VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES
                vkGetPhysicalDeviceProperties2(devices[i], props2);

                // One card can be listed once per installed driver manifest; the device UUID tells those apart
                // from two genuinely identical cards.
                if (!seen.Add(new Guid(new ReadOnlySpan<byte>(id + 16, 16))))
                    continue;
                if (*(uint*)(props + 16) == 4) // VK_PHYSICAL_DEVICE_TYPE_CPU: llvmpipe and friends
                    continue;
                var vendorId = *(uint*)(props + 8);
                var name = Marshal.PtrToStringUTF8((IntPtr)(props + 20), 256).Split('\0')[0].Trim();

                // The largest DEVICE_LOCAL heap is the VRAM; the small host-visible BAR heap is device-local too.
                vkGetPhysicalDeviceMemoryProperties(devices[i], memory);
                var heaps = Math.Min(*(uint*)(memory + 260), 16);
                ulong vram = 0;
                for (var h = 0; h < heaps; h++)
                {
                    var heap = memory + 264 + (h * 16);
                    if ((*(uint*)(heap + 8) & 1) != 0) // VK_MEMORY_HEAP_DEVICE_LOCAL_BIT
                        vram = Math.Max(vram, *(ulong*)heap);
                }

                list.Add(new GpuAdapter(name, vendorId, (long)vram));
            }
        }
        finally
        {
            vkDestroyInstance(instance, null);
        }

        return list;
    }

    private static void Release(IntPtr unknown)
    {
        var vtbl = *(IntPtr**)unknown;
        ((delegate* unmanaged[Stdcall]<IntPtr, uint>)vtbl[2])(unknown);
    }

    [DllImport("dxgi.dll", ExactSpelling = true)]
    private static extern int CreateDXGIFactory1(Guid* riid, out IntPtr factory);

    [DllImport("vulkan-1.dll", ExactSpelling = true)]
    private static extern int vkCreateInstance(VkInstanceCreateInfo* createInfo, void* allocator, out IntPtr instance);

    [DllImport("vulkan-1.dll", ExactSpelling = true)]
    private static extern void vkDestroyInstance(IntPtr instance, void* allocator);

    [DllImport("vulkan-1.dll", ExactSpelling = true)]
    private static extern int vkEnumeratePhysicalDevices(IntPtr instance, uint* count, IntPtr* devices);

    [DllImport("vulkan-1.dll", ExactSpelling = true)]
    private static extern void vkGetPhysicalDeviceProperties2(IntPtr device, byte* properties);

    [DllImport("vulkan-1.dll", ExactSpelling = true)]
    private static extern void vkGetPhysicalDeviceMemoryProperties(IntPtr device, byte* properties);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GlobalMemoryStatusEx(ref MemoryStatusEx buffer);

    [StructLayout(LayoutKind.Sequential)]
    private struct AdapterDesc1
    {
        public fixed char Description[128];
        public uint VendorId;
        public uint DeviceId;
        public uint SubSysId;
        public uint Revision;
        public nuint DedicatedVideoMemory;
        public nuint DedicatedSystemMemory;
        public nuint SharedSystemMemory;
        public uint LuidLow;
        public int LuidHigh;
        public uint Flags;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct VkApplicationInfo
    {
        public uint SType;
        public IntPtr Next;
        public IntPtr ApplicationName;
        public uint ApplicationVersion;
        public IntPtr EngineName;
        public uint EngineVersion;
        public uint ApiVersion;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct VkInstanceCreateInfo
    {
        public uint SType;
        public IntPtr Next;
        public uint Flags;
        public IntPtr ApplicationInfo;
        public uint EnabledLayerCount;
        public IntPtr EnabledLayerNames;
        public uint EnabledExtensionCount;
        public IntPtr EnabledExtensionNames;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MemoryStatusEx
    {
        public uint Length;
        public uint MemoryLoad;
        public ulong TotalPhys;
        public ulong AvailPhys;
        public ulong TotalPageFile;
        public ulong AvailPageFile;
        public ulong TotalVirtual;
        public ulong AvailVirtual;
        public ulong AvailExtendedVirtual;
    }
}
