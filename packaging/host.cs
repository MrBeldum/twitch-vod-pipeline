// C# 5 / .NET Framework 4 host. Compiled by packaging/build.cmd (csc).
// This is the process Windows recognises: icon, VERSIONINFO, AppUserModelID,
// Start Menu shortcut, Apps & Features uninstall entry. It launches
// pythonw -m vodpipe app in the project folder and keeps a tray icon.

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("VOD Pipeline")]
[assembly: AssemblyProduct("VOD Pipeline")]
[assembly: AssemblyDescription("Twitch VOD to Premiere pipeline")]
[assembly: AssemblyCompany("MrBeldum")]
[assembly: AssemblyCopyright("Copyright (c) 2026 MrBeldum. MIT licensed.")]
// Version attributes and BuildInfo live in the generated version.g.cs, written
// from vodpipe.__version__ by packaging/prebuild.py. They were literals here
// and were still reporting 1.0.0 two releases later.

internal static class Native
{
    public const string Aumid = "MrBeldum.VODPipeline";
    public const string AppName = "VOD Pipeline";
    public const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\VODPipeline";
    public const string AppPathsKey = @"Software\Microsoft\Windows\CurrentVersion\App Paths\VODPipeline.exe";

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    public static extern int SetCurrentProcessExplicitAppUserModelID(string appID);

    public const int SHCNE_ASSOCCHANGED = 0x08000000;
    public const uint SHCNF_IDLIST = 0x0000;

    [DllImport("shell32.dll")]
    public static extern void SHChangeNotify(int eventId, uint flags, IntPtr item1, IntPtr item2);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetInformationJobObject(
        IntPtr hJob, int infoClass, IntPtr lpJobObjectInfo, uint cbJobObjectInfoLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr hObject);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr FindWindow(string lpClassName, string lpWindowName);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    public const int JobObjectExtendedLimitInformation = 9;
    public const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;
    public const int SW_RESTORE = 9;

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit;
        public long PerUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinWorkingSetSize;
        public UIntPtr MaxWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct IO_COUNTERS
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    public static extern int SHGetPropertyStoreFromParsingName(
        string pszPath, IntPtr pbc, int flags, ref Guid riid, out IPropertyStore ppv);

    public const int GPS_READWRITE = 2;

    public static readonly Guid IID_IPropertyStore =
        new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
    public static readonly Guid PKEY_AppUserModel_ID_Fmtid =
        new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
}

[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IPropertyStore
{
    uint GetCount(out uint cProps);
    uint GetAt(uint iProp, out PROPERTYKEY pkey);
    uint GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
    uint SetValue(ref PROPERTYKEY key, ref PROPVARIANT pv);
    uint Commit();
}

[StructLayout(LayoutKind.Sequential)]
internal struct PROPERTYKEY
{
    public Guid fmtid;
    public uint pid;
}

[StructLayout(LayoutKind.Sequential)]
internal struct PROPVARIANT
{
    public ushort vt;
    public ushort wReserved1;
    public ushort wReserved2;
    public ushort wReserved3;
    public IntPtr pszVal;
}

internal sealed class HostContext : ApplicationContext
{
    readonly Process _child;
    readonly NotifyIcon _tray;
    readonly IntPtr _job;

    public HostContext(Process child, NotifyIcon tray, IntPtr job)
    {
        _child = child;
        _tray = tray;
        _job = job;
        _child.EnableRaisingEvents = true;
        _child.Exited += delegate
        {
            MethodInvoker invoker = new MethodInvoker(ExitThreadSafe);
            if (_tray != null && _tray.ContextMenuStrip != null
                && _tray.ContextMenuStrip.IsHandleCreated)
            {
                _tray.ContextMenuStrip.BeginInvoke(invoker);
            }
            else
            {
                ExitThreadSafe();
            }
        };
    }

    void ExitThreadSafe()
    {
        try
        {
            if (_tray != null) _tray.Visible = false;
        }
        catch { }
        ExitThread();
    }

    public void Quit()
    {
        try
        {
            if (!_child.HasExited)
            {
                _child.Kill();
                _child.WaitForExit(8000);
            }
        }
        catch { }
        ExitThreadSafe();
    }

    public void FocusWindow()
    {
        IntPtr found = IntPtr.Zero;
        Native.EnumWindows(delegate(IntPtr hWnd, IntPtr lParam)
        {
            if (!Native.IsWindowVisible(hWnd)) return true;
            StringBuilder sb = new StringBuilder(512);
            Native.GetWindowText(hWnd, sb, sb.Capacity);
            string title = sb.ToString();
            if (title.IndexOf("VOD Pipeline", StringComparison.OrdinalIgnoreCase) >= 0
                && title.IndexOf("Visual Studio", StringComparison.OrdinalIgnoreCase) < 0)
            {
                found = hWnd;
                return false;
            }
            return true;
        }, IntPtr.Zero);
        if (found != IntPtr.Zero)
        {
            Native.ShowWindow(found, Native.SW_RESTORE);
            Native.SetForegroundWindow(found);
        }
    }
}

internal static class Program
{
    [STAThread]
    static int Main(string[] args)
    {
        Native.SetCurrentProcessExplicitAppUserModelID(Native.Aumid);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        string mode = args.Length > 0 ? args[0] : "";
        if (string.Equals(mode, "--install", StringComparison.OrdinalIgnoreCase))
            return Install();
        if (string.Equals(mode, "--uninstall", StringComparison.OrdinalIgnoreCase))
            return Uninstall();

        bool created;
        using (Mutex mutex = new Mutex(true, @"Local\MrBeldum.VODPipeline", out created))
        {
            if (!created)
            {
                FocusExisting();
                return 0;
            }
            return Run();
        }
    }

    static int Run()
    {
        string exe = Application.ExecutablePath;
        string exeDir = Path.GetDirectoryName(exe);
        string root;
        try
        {
            root = FindRoot(exeDir);
        }
        catch (Exception ex)
        {
            MessageBox.Show(ex.Message, Native.AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }
        string python;
        try
        {
            python = FindPython(root);
        }
        catch (Exception ex)
        {
            MessageBox.Show(ex.Message, Native.AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }

        ProcessStartInfo psi = new ProcessStartInfo();
        psi.FileName = python;
        psi.Arguments = "-m vodpipe app";
        psi.WorkingDirectory = root;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.WindowStyle = ProcessWindowStyle.Hidden;

        Process child;
        try
        {
            child = Process.Start(psi);
        }
        catch (Exception ex)
        {
            MessageBox.Show("Could not start the pipeline:\n" + ex.Message,
                Native.AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        if (child == null)
        {
            MessageBox.Show("Could not start the pipeline.", Native.AppName,
                MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }

        IntPtr job = CreateKillOnCloseJob();
        if (job != IntPtr.Zero)
        {
            try { Native.AssignProcessToJobObject(job, child.Handle); }
            catch { }
        }

        NotifyIcon tray = new NotifyIcon();
        try
        {
            Icon icon = Icon.ExtractAssociatedIcon(exe);
            tray.Icon = icon != null ? icon : SystemIcons.Application;
        }
        catch
        {
            tray.Icon = SystemIcons.Application;
        }
        tray.Text = Native.AppName;
        tray.Visible = true;

        HostContext ctx = new HostContext(child, tray, job);
        ContextMenuStrip menu = new ContextMenuStrip();
        menu.Items.Add("Open VOD Pipeline", null, delegate { ctx.FocusWindow(); });
        menu.Items.Add("Quit", null, delegate { ctx.Quit(); });
        tray.ContextMenuStrip = menu;
        tray.DoubleClick += delegate { ctx.FocusWindow(); };

        Application.Run(ctx);

        try { if (!child.HasExited) child.Kill(); }
        catch { }
        try { if (job != IntPtr.Zero) Native.CloseHandle(job); }
        catch { }
        try { tray.Visible = false; tray.Dispose(); }
        catch { }
        return child.HasExited ? child.ExitCode : 0;
    }

    static IntPtr CreateKillOnCloseJob()
    {
        IntPtr job = Native.CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) return IntPtr.Zero;
        Native.JOBOBJECT_EXTENDED_LIMIT_INFORMATION info =
            new Native.JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        info.BasicLimitInformation.LimitFlags = Native.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        int length = Marshal.SizeOf(typeof(Native.JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        IntPtr ptr = Marshal.AllocHGlobal(length);
        try
        {
            Marshal.StructureToPtr(info, ptr, false);
            if (!Native.SetInformationJobObject(job, Native.JobObjectExtendedLimitInformation,
                ptr, (uint)length))
            {
                Native.CloseHandle(job);
                return IntPtr.Zero;
            }
        }
        finally
        {
            Marshal.FreeHGlobal(ptr);
        }
        return job;
    }

    static void FocusExisting()
    {
        Native.EnumWindows(delegate(IntPtr hWnd, IntPtr lParam)
        {
            if (!Native.IsWindowVisible(hWnd)) return true;
            StringBuilder sb = new StringBuilder(512);
            Native.GetWindowText(hWnd, sb, sb.Capacity);
            if (sb.ToString().IndexOf("VOD Pipeline", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                Native.ShowWindow(hWnd, Native.SW_RESTORE);
                Native.SetForegroundWindow(hWnd);
                return false;
            }
            return true;
        }, IntPtr.Zero);
    }

    static string FindRoot(string exeDir)
    {
        string[] candidates = new string[] { exeDir, Directory.GetParent(exeDir) != null
            ? Directory.GetParent(exeDir).FullName : exeDir };
        for (int i = 0; i < candidates.Length; i++)
        {
            string probe = Path.Combine(candidates[i], "vodpipe", "__init__.py");
            if (File.Exists(probe)) return candidates[i];
        }
        throw new InvalidOperationException(
            "VODPipeline.exe must sit in the twitch-vod-pipeline folder "
            + "(next to the vodpipe package).");
    }

    static string FindPython(string root)
    {
        string[] hints = new string[]
        {
            @"C:\Python314\pythonw.exe",
            @"C:\Python314\python.exe",
            Path.Combine(root, ".venv", "Scripts", "pythonw.exe"),
            Path.Combine(root, ".venv", "Scripts", "python.exe"),
        };
        for (int i = 0; i < hints.Length; i++)
        {
            if (File.Exists(hints[i])) return hints[i];
        }
        string onPath = FindOnPath("pythonw.exe");
        if (onPath != null) return onPath;
        onPath = FindOnPath("python.exe");
        if (onPath != null) return onPath;
        throw new InvalidOperationException(
            "Python was not found. Install Python 3.14 or add pythonw.exe to PATH.");
    }

    static string FindOnPath(string name)
    {
        string path = Environment.GetEnvironmentVariable("PATH");
        if (string.IsNullOrEmpty(path)) return null;
        string[] parts = path.Split(new char[] { ';' }, StringSplitOptions.RemoveEmptyEntries);
        for (int i = 0; i < parts.Length; i++)
        {
            try
            {
                string candidate = Path.Combine(parts[i].Trim().Trim('"'), name);
                if (File.Exists(candidate)) return candidate;
            }
            catch { }
        }
        return null;
    }

    static int Install()
    {
        try
        {
            string exe = Application.ExecutablePath;
            string exeDir = Path.GetDirectoryName(exe);
            string root = FindRoot(exeDir);
            string programs = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.StartMenu),
                "Programs");
            Directory.CreateDirectory(programs);
            string shortcut = Path.Combine(programs, Native.AppName + ".lnk");
            CreateShortcut(shortcut, exe, "", root, exe);

            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(Native.UninstallKey))
            {
                key.SetValue("DisplayName", Native.AppName);
                key.SetValue("DisplayIcon", exe);
                key.SetValue("Publisher", "MrBeldum");
                key.SetValue("DisplayVersion", BuildInfo.Version);
                key.SetValue("InstallLocation", root);
                key.SetValue("UninstallString", "\"" + exe + "\" --uninstall");
                key.SetValue("QuietUninstallString", "\"" + exe + "\" --uninstall");
                key.SetValue("NoModify", 1, RegistryValueKind.DWord);
                key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
                key.SetValue("HelpLink", "https://github.com/MrBeldum/twitch-vod-pipeline");
                key.SetValue("URLInfoAbout", "https://github.com/MrBeldum/twitch-vod-pipeline");
            }
            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(Native.AppPathsKey))
            {
                key.SetValue("", exe);
                key.SetValue("Path", root);
            }
            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(
                @"Software\Classes\Applications\VODPipeline.exe"))
            {
                key.SetValue("FriendlyAppName", Native.AppName);
            }
            try
            {
                string tile = Path.Combine(root, "VODPipeline.VisualElementsManifest.xml");
                string src = Path.Combine(root, "packaging", "VODPipeline.VisualElementsManifest.xml");
                if (File.Exists(src)) File.Copy(src, tile, true);
            }
            catch { }
            // The icon is compiled into this exe, so a rebuilt host is a new
            // icon for a path the shell has already cached. Nothing repaints
            // until the shell is told, which is why a corrected icon could
            // look like it had not been corrected at all.
            try { Native.SHChangeNotify(Native.SHCNE_ASSOCCHANGED, Native.SHCNF_IDLIST, IntPtr.Zero, IntPtr.Zero); }
            catch { }
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("Install failed: " + ex.Message);
            return 1;
        }
    }

    static int Uninstall()
    {
        try
        {
            string programs = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.StartMenu),
                "Programs", Native.AppName + ".lnk");
            if (File.Exists(programs)) File.Delete(programs);
        }
        catch { }
        try { Registry.CurrentUser.DeleteSubKeyTree(Native.UninstallKey, false); }
        catch { }
        try { Registry.CurrentUser.DeleteSubKeyTree(Native.AppPathsKey, false); }
        catch { }
        try
        {
            Registry.CurrentUser.DeleteSubKeyTree(
                @"Software\Classes\Applications\VODPipeline.exe", false);
        }
        catch { }
        return 0;
    }

    static void CreateShortcut(string path, string target, string args, string workDir, string icon)
    {
        Type t = Type.GetTypeFromProgID("WScript.Shell");
        object shell = Activator.CreateInstance(t);
        object sc = t.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod,
            null, shell, new object[] { path });
        Type scType = sc.GetType();
        scType.InvokeMember("TargetPath", BindingFlags.SetProperty, null, sc, new object[] { target });
        scType.InvokeMember("Arguments", BindingFlags.SetProperty, null, sc, new object[] { args });
        scType.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, sc, new object[] { workDir });
        scType.InvokeMember("WindowStyle", BindingFlags.SetProperty, null, sc, new object[] { 1 });
        scType.InvokeMember("Description", BindingFlags.SetProperty, null, sc,
            new object[] { "Record Twitch, transcribe, editor report" });
        scType.InvokeMember("IconLocation", BindingFlags.SetProperty, null, sc,
            new object[] { icon + ",0" });
        scType.InvokeMember("Save", BindingFlags.InvokeMethod, null, sc, null);
        TrySetShortcutAumid(path, Native.Aumid);
    }

    static void TrySetShortcutAumid(string shortcutPath, string aumid)
    {
        IPropertyStore store = null;
        IntPtr mem = IntPtr.Zero;
        try
        {
            Guid iid = Native.IID_IPropertyStore;
            int hr = Native.SHGetPropertyStoreFromParsingName(
                shortcutPath, IntPtr.Zero, Native.GPS_READWRITE, ref iid, out store);
            if (hr != 0 || store == null) return;
            PROPERTYKEY key = new PROPERTYKEY();
            key.fmtid = Native.PKEY_AppUserModel_ID_Fmtid;
            key.pid = 5;
            PROPVARIANT pv = new PROPVARIANT();
            pv.vt = 31; // VT_LPWSTR
            mem = Marshal.StringToCoTaskMemUni(aumid);
            pv.pszVal = mem;
            store.SetValue(ref key, ref pv);
            store.Commit();
        }
        catch { }
        finally
        {
            if (mem != IntPtr.Zero) Marshal.FreeCoTaskMem(mem);
            if (store != null) Marshal.ReleaseComObject(store);
        }
    }
}
