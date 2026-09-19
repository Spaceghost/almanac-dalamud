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
/// GPU facts from DXGI (works on Windows and under Wine/Proton with DXVK, which reports the real adapter), plus system
/// RAM and whether we run under Wine. Everything fails soft: no DXGI means an empty list.
/// </summary>
public static unsafe class GpuInfo
{
    private static readonly Guid IidFactory1 = new("770aae78-f26f-4dba-a829-253c83d1b387");

    public static IReadOnlyList<GpuAdapter> Adapters()
    {
        try
        {
            return EnumerateAdapters();
        }
        catch (Exception ex) when (ex is DllNotFoundException or EntryPointNotFoundException or SEHException or COMException)
        {
            return [];
        }
    }

    /// <summary>The adapter with the most dedicated VRAM (where a model would run).</summary>
    public static GpuAdapter? Best() => Adapters().OrderByDescending(a => a.DedicatedVideoMemory).FirstOrDefault();

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
    public static string OsFamily()
    {
        try
        {
            var ntdll = NativeLibrary.Load("ntdll.dll");
            return NativeLibrary.TryGetExport(ntdll, "wine_get_version", out _) ? "linux" : "windows";
        }
        catch (DllNotFoundException)
        {
            return OperatingSystem.IsLinux() ? "linux" : OperatingSystem.IsMacOS() ? "macos" : "other";
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

    private static void Release(IntPtr unknown)
    {
        var vtbl = *(IntPtr**)unknown;
        ((delegate* unmanaged[Stdcall]<IntPtr, uint>)vtbl[2])(unknown);
    }

    [DllImport("dxgi.dll", ExactSpelling = true)]
    private static extern int CreateDXGIFactory1(Guid* riid, out IntPtr factory);

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
