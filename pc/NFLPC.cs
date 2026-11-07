// NFLPC.cs -- NFL Hub for Windows: a launcher that behaves like an app.
//
//   NFLHub.exe              starts the hub (if needed) and opens a chromeless
//                           app window; closing that window stops everything
//   NFLHub.exe --local      bind 127.0.0.1 only (default is 0.0.0.0 so the
//                           phone can still connect)
//   NFLHub.exe --port 8796  use another port
//   NFLHub.exe --dir PATH   where hub.py lives (default: next to this exe)
//
// Built with the .NET Framework compiler that ships with Windows:
//   csc /target:winexe /win32icon:app.ico /r:System.Management.dll ...
//
// Why not embed a browser: the page needs hls.js + MSE. Chrome/Edge in
// --app mode gives a real chromium window with no tabs, no address bar and a
// private profile, which is exactly the shell the Android app provides.

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Management;
using System.Net;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

static class Program
{
    const string MUTEX_NAME = "NFLHubPC.SingleInstance.v1";
    const int DEFAULT_PORT = 8795;
    const int WINDOW_W = 1520;
    const int WINDOW_H = 960;

    [STAThread]
    static int Main(string[] args)
    {
        // first line of defence: if the process dies before any window appears,
        // this is the only place that can say so (pythonw-style silence).
        Log("boot pid=" + System.Diagnostics.Process.GetCurrentProcess().Id +
            " args=" + string.Join(" ", args));
        Options opt;
        try { opt = Options.Parse(args); }
        catch (Exception e)
        {
            Log("parse failed: " + e);
            return 9;
        }
        bool mine;
        try
        {
            using (Mutex mutex = new Mutex(true, MUTEX_NAME, out mine))
            {
                if (!mine)
                {
                    // already running: bring THAT window forward instead of stacking
                    // another identical app window every time the icon is clicked
                    Log("another instance is running");
                    if (FocusExistingWindow()) { Log("focused the existing window"); return 0; }
                    int live = ReadPortFile();
                    if (live > 0 && Ping(live, 1500)) OpenWindow(opt, live, false);
                    else MessageBox.Show("nfl hub is already starting. Close this dialog and wait for its window.",
                                         "nfl hub", MessageBoxButtons.OK, MessageBoxIcon.Information);
                    return 0;
                }

                try { return Run(opt); }
                catch (Exception e) { Log("run failed: " + e); Fail(opt, e); return 1; }
            }
        }
        catch (Exception e)
        {
            Log("fatal: " + e);
            return 10;
        }
    }

    static int Run(Options opt)
    {
        Log("start exe=" + Application.ExecutablePath + " hubDir=" + opt.HubDir + " port=" + opt.Port +
            " local=" + opt.LocalOnly);
        // A window is already on screen (an earlier launcher may have exited while
        // the browser kept running): focus it rather than open a second one.
        if (FocusExistingWindow())
        {
            Log("window already open, focused it");
            return 0;
        }
        string hubDir = opt.HubDir;
        string hubPy = Path.Combine(hubDir, "hub.py");
        if (!File.Exists(hubPy))
        {
            Log("hub.py missing in " + hubDir);
            MessageBox.Show("hub.py was not found next to NFLHub.exe.\n\nLooked in:\n" + hubDir +
                            "\n\nPut NFLHub.exe in the nfl\\hub folder (or pass --dir).",
                            "nfl hub", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }
        if (!Directory.Exists(opt.ProfileDir)) Directory.CreateDirectory(opt.ProfileDir);

        int port = opt.Port;
        bool startedByUs = false;
        Process hub = null;

        if (!(Ping(port, 900)))
        {
            Log("no hub on " + port + ", looking for pythonw");
            // A portable bundle ships its own interpreter in <root>\python; use it
            // before anything on PATH so the folder works on a PC without Python.
            string pythonw = BundledPython(hubDir);
            if (pythonw != null) Log("using the bundled interpreter");
            else pythonw = FindPythonW();
            if (pythonw == null)
            {
                Log("pythonw not found");
                MessageBox.Show("Python 3 was not found.\n\nInstall it from python.org (tick \"Add python.exe to PATH\"), " +
                                "then run this again.", "nfl hub", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 3;
            }
            port = FreePort(opt.Port);
            string bind = opt.LocalOnly ? "127.0.0.1" : "0.0.0.0";
            Log("pythonw=" + pythonw + " chosen port=" + port + " bind=" + bind);
            ProcessStartInfo psi = new ProcessStartInfo(pythonw,
                "hub.py --port " + port + " --host " + bind);
            psi.WorkingDirectory = hubDir;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.WindowStyle = ProcessWindowStyle.Hidden;
            hub = Process.Start(psi);
            startedByUs = true;
            Log("spawned hub pid=" + (hub == null ? 0 : hub.Id));
            WritePortFile(port);
        }
        else
        {
            Log("reusing the hub already on " + port);
            WritePortFile(port);
        }

        bool up = WaitForHub(port, 30);
        Log("hub answering=" + up);
        if (!up)
        {
            MessageBox.Show("The hub did not answer on 127.0.0.1:" + port + " within 30 s.\n\n" +
                            "Run this in the folder to see why:\n  python hub.py --port " + port,
                            "nfl hub", MessageBoxButtons.OK, MessageBoxIcon.Error);
            if (startedByUs) KillQuiet(hub);
            return 4;
        }

        string url = "http://127.0.0.1:" + port + "/";
        Process browser = OpenWindow(opt, port, true);
        Log("browser pid=" + (browser == null ? 0 : browser.Id));
        if (browser == null)
        {
            // No Chrome/Chromium/Firefox/Brave. Do NOT silently fall back to the
            // Windows default (that is Edge, and Edge is what we were asked to
            // stop opening): show the URL and let the user decide.
            Log("no preferred browser found; not opening one");
            using (ServerWindow w = new ServerWindow(url)) w.ShowDialog();
        }
        else
        {
            WaitForBrowser(browser, opt.ProfileDir);
        }
        Log("app window closed");

        if (startedByUs)
        {
            Log("shutting the hub down");
            Shutdown(port);
            if (hub != null) { if (!hub.WaitForExit(5000)) KillQuiet(hub); }
            Log("done");
        }
        return 0;
    }

    static readonly object LOG_LOCK = new object();

    static string LogPath
    {
        get
        {
            string dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "NFLHubPC");
            try { Directory.CreateDirectory(dir); } catch { }
            return Path.Combine(dir, "launcher.log");
        }
    }

    static void Log(string msg)
    {
        try
        {
            lock (LOG_LOCK)
            {
                string p = LogPath;
                FileInfo fi = new FileInfo(p);
                if (fi.Exists && fi.Length > 400000) File.Delete(p);
                File.AppendAllText(p, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + msg + Environment.NewLine);
            }
        }
        catch { }
    }

    // ------------------------------------------------------------------ http

    static bool Ping(int port, int timeoutMs)
    {
        try
        {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + "/api/ping");
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            req.Proxy = null;
            req.CachePolicy = new System.Net.Cache.RequestCachePolicy(System.Net.Cache.RequestCacheLevel.NoCacheNoStore);
            using (HttpWebResponse r = (HttpWebResponse)req.GetResponse())
            using (StreamReader sr = new StreamReader(r.GetResponseStream()))
            {
                string body = sr.ReadToEnd();
                return body.Contains("nflhub");
            }
        }
        catch { return false; }
    }

    static bool WaitForHub(int port, int seconds)
    {
        DateTime until = DateTime.UtcNow.AddSeconds(seconds);
        while (DateTime.UtcNow < until)
        {
            if (Ping(port, 800)) return true;
            Thread.Sleep(400);
        }
        return false;
    }

    static void Shutdown(int port)
    {
        try
        {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + "/api/shutdown");
            req.Timeout = 3000; req.Proxy = null;
            using (HttpWebResponse r = (HttpWebResponse)req.GetResponse()) { }
        }
        catch { }
    }

    // --------------------------------------------------------------- browser

    // Chrome first, Firefox second. Edge is deliberately absent: it is the
    // Windows default and it kept being what opened. It can still be used, but
    // only when named: --browser edge, or "edge" in browser.txt.
    static readonly string[][] BROWSERS = new string[][] {
        new string[] { "chrome",   @"%ProgramFiles%\Google\Chrome\Application\chrome.exe" },
        new string[] { "chrome",   @"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" },
        new string[] { "chrome",   @"%LocalAppData%\Google\Chrome\Application\chrome.exe" },
        new string[] { "chromium", @"%ProgramFiles%\Chromium\Application\chrome.exe" },
        new string[] { "firefox",  @"%ProgramFiles%\Mozilla Firefox\firefox.exe" },
        new string[] { "firefox",  @"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe" },
        new string[] { "brave",    @"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe" },
    };

    /// <summary>browser.txt next to the exe: a name (chrome/firefox/edge/brave) or a full path.</summary>
    static string BrowserOverride(Options opt)
    {
        string file = Path.Combine(opt.HubDir, "browser.txt");
        try
        {
            if (!File.Exists(file)) return null;
            foreach (string raw in File.ReadAllLines(file))
            {
                string s = raw.Trim();
                if (s.Length == 0 || s.StartsWith("#")) continue;
                string low = s.ToLowerInvariant();
                if (low == "chrome") s = @"%ProgramFiles%\Google\Chrome\Application\chrome.exe";
                else if (low == "firefox") s = @"%ProgramFiles%\Mozilla Firefox\firefox.exe";
                else if (low == "edge") s = @"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe";
                else if (low == "brave") s = @"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe";
                s = Environment.ExpandEnvironmentVariables(s);
                return File.Exists(s) ? s : null;
            }
        }
        catch { }
        return null;
    }

    /// <summary>(kind, path) of the browser to use, or (null, null).</summary>
    static string[] FindBrowser(Options opt)
    {
        if (!string.IsNullOrEmpty(opt.Browser))
        {
            string forced = Environment.ExpandEnvironmentVariables(opt.Browser);
            if (File.Exists(forced)) return new string[] { KindOf(forced), forced };
            Log("--browser not found: " + forced);
        }
        string over = BrowserOverride(opt);
        if (over != null) return new string[] { KindOf(over), over };
        foreach (string[] cand in BROWSERS)
        {
            string p = Environment.ExpandEnvironmentVariables(cand[1]);
            if (File.Exists(p)) return new string[] { cand[0], p };
        }
        return new string[] { null, null };
    }

    static string KindOf(string exe)
    {
        string n = Path.GetFileName(exe).ToLowerInvariant();
        if (n.Contains("firefox")) return "firefox";
        if (n.Contains("brave")) return "brave";
        if (n.Contains("msedge")) return "edge";
        return "chrome";
    }

    static Process OpenWindow(Options opt, int port, bool track)
    {
        string[] pick = FindBrowser(opt);
        string kind = pick[0], exe = pick[1];
        if (exe == null)
        {
            Log("no chromium/firefox found, falling back to the default browser");
            return null;
        }
        Log("browser=" + kind + " exe=" + exe);
        string url = "http://127.0.0.1:" + port + "/";
        ProcessStartInfo psi = new ProcessStartInfo(exe);
        if (kind == "firefox")
        {
            // Firefox has no --app mode: a dedicated profile + its own instance is
            // the closest thing, and it keeps the user's own Firefox untouched.
            psi.Arguments = "-no-remote -new-instance -profile \"" + opt.ProfileDir + "\" -new-window " + url;
        }
        else
        {
            psi.Arguments = "--app=" + url +
                            " --window-size=" + (opt.Width > 0 ? opt.Width : WINDOW_W) + "," + (opt.Height > 0 ? opt.Height : WINDOW_H) +
                            " --user-data-dir=\"" + opt.ProfileDir + "\"" +
                            " --no-first-run --no-default-browser-check" +
                            " --autoplay-policy=no-user-gesture-required" +
                            " --disable-background-mode" +
                            " --disable-features=Translate,MediaRouter" +
                            " --disable-session-crashed-bubble --hide-crash-restore-bubble";
        }
        psi.UseShellExecute = false;
        try
        {
            Process p = Process.Start(psi);
            if (!track) return null;
            return p;
        }
        catch (Exception e)
        {
            Log("browser start failed: " + e.Message);
            return null;
        }
    }

    // ------------------------------------------------------- existing window

    delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr hWnd, StringBuilder s, int max);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hWnd, int cmd);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hWnd);

    /// <summary>
    /// Find our app window (a browser showing the hub page) and bring it forward.
    /// Without this every click on the icon opened yet another identical window,
    /// because each launch is a new process that never knew about the old one.
    /// </summary>
    static bool FocusExistingWindow()
    {
        IntPtr found = IntPtr.Zero;
        try
        {
            EnumWindows(delegate(IntPtr h, IntPtr l)
            {
                if (!IsWindowVisible(h)) return true;
                var sb = new StringBuilder(512);
                GetWindowText(h, sb, sb.Capacity);
                string title = sb.ToString();
                if (title.IndexOf("nfl hub", StringComparison.OrdinalIgnoreCase) < 0) return true;
                uint pid;
                GetWindowThreadProcessId(h, out pid);
                try
                {
                    string name = Process.GetProcessById((int)pid).ProcessName.ToLowerInvariant();
                    if (name != "chrome" && name != "msedge" && name != "firefox" && name != "brave") return true;
                }
                catch { return true; }
                found = h;
                return false;                 // stop enumerating
            }, IntPtr.Zero);
        }
        catch (Exception e)
        {
            Log("window scan failed: " + e.Message);
            return false;
        }
        if (found == IntPtr.Zero) return false;
        try
        {
            if (IsIconic(found)) ShowWindow(found, 9);   // SW_RESTORE
            else ShowWindow(found, 5);                   // SW_SHOW
            SetForegroundWindow(found);
            return true;
        }
        catch (Exception e)
        {
            Log("focus failed: " + e.Message);
            return false;
        }
    }

    /// <summary>The interpreter shipped inside a portable bundle: &lt;root&gt;\python\pythonw.exe.</summary>
    static string BundledPython(string hubDir)
    {
        string d = hubDir;
        for (int i = 0; i < 4; i++)
        {
            if (string.IsNullOrEmpty(d)) break;
            string cand = Path.Combine(d, "python", "pythonw.exe");
            if (File.Exists(cand)) return cand;
            d = Path.GetDirectoryName(d);
        }
        return null;
    }

    static void WaitForBrowser(Process browser, string profileDir)
    {
        while (!browser.HasExited) { Thread.Sleep(700); }
        // --app hands off to an already running instance, which can make the
        // process we started exit while the window is still up. Ask WMI.
        for (int i = 0; i < 4; i++)
        {
            if (!AnyBrowserOnProfile(profileDir)) break;
            Thread.Sleep(1000);
        }
        while (AnyBrowserOnProfile(profileDir)) Thread.Sleep(1000);
    }

    static bool AnyBrowserOnProfile(string profileDir)
    {
        try
        {
            using (ManagementObjectSearcher s = new ManagementObjectSearcher(
                "SELECT ProcessId,CommandLine FROM Win32_Process WHERE Name='chrome.exe' OR Name='msedge.exe' OR Name='brave.exe'"))
            foreach (ManagementObject mo in s.Get())
            {
                object cl = mo["CommandLine"];
                if (cl != null && cl.ToString().IndexOf(profileDir, StringComparison.OrdinalIgnoreCase) >= 0)
                    return true;
            }
        }
        catch { return false; }
        return false;
    }

    // ---------------------------------------------------------------- python

    static string FindPythonW()
    {
        string local = Environment.GetEnvironmentVariable("LOCALAPPDATA") ?? "";
        string pf = Environment.GetEnvironmentVariable("ProgramFiles") ?? @"C:\Program Files";
        string pf86 = Environment.GetEnvironmentVariable("ProgramFiles(x86)") ?? @"C:\Program Files (x86)";

        // 1) next to python.exe on PATH
        string path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (string dir in path.Split(';'))
        {
            if (dir.Trim().Length == 0) continue;
            string c = Path.Combine(dir.Trim(), "pythonw.exe");
            if (File.Exists(c)) return c;
        }
        // 2) the usual installer locations, newest version first
        string[] roots = new string[] {
            Path.Combine(local, @"Programs\Python"),
            pf, pf86, Path.Combine(local, "Microsoft", "WindowsApps"),
        };
        foreach (string root in roots)
        {
            if (!Directory.Exists(root)) continue;
            string[] dirs = Directory.GetDirectories(root, "Python3*");
            Array.Sort(dirs);
            Array.Reverse(dirs);
            foreach (string d in dirs)
            {
                string c = Path.Combine(d, "pythonw.exe");
                if (File.Exists(c)) return c;
            }
        }
        return null;
    }

    // ------------------------------------------------------------------ misc

    static int FreePort(int start)
    {
        for (int p = start; p < start + 40; p++)
        {
            if (Ping(p, 250)) continue;                       // something already there
            try
            {
                System.Net.Sockets.TcpListener l = new System.Net.Sockets.TcpListener(IPAddress.Loopback, p);
                l.Start(); l.Stop();
                return p;
            }
            catch { }
        }
        return start;
    }

    static string PortFile
    {
        get
        {
            string dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "NFLHubPC");
            Directory.CreateDirectory(dir);
            return Path.Combine(dir, "port.txt");
        }
    }

    static void WritePortFile(int port)
    {
        try { File.WriteAllText(PortFile, port.ToString()); } catch { }
    }

    static int ReadPortFile()
    {
        try
        {
            string t = File.ReadAllText(PortFile).Trim();
            int p;
            return int.TryParse(t, out p) ? p : 0;
        }
        catch { return 0; }
    }

    static void KillQuiet(Process p)
    {
        try { if (p != null && !p.HasExited) p.Kill(); } catch { }
    }

    static void Fail(Options opt, Exception e)
    {
        MessageBox.Show("nfl hub could not start.\n\n" + e.GetType().Name + ": " + e.Message +
                        "\n\nFolder: " + opt.HubDir,
                        "nfl hub", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    // ----------------------------------------------------------------- types

    class Options
    {
        public int Port = DEFAULT_PORT;
        public bool LocalOnly;
        public int Width, Height;
        public string HubDir;
        public string ProfileDir;
        public string Browser;              // --browser <name|path>

        public static Options Parse(string[] a)
        {
            Options o = new Options();
            o.HubDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
            for (int i = 0; i < a.Length; i++)
            {
                string s = a[i].ToLowerInvariant();
                if (s == "--local") o.LocalOnly = true;
                else if (s == "--port" && i + 1 < a.Length) { int.TryParse(a[++i], out o.Port); }
                else if (s == "--dir" && i + 1 < a.Length) o.HubDir = a[++i].TrimEnd('\\');
                else if (s == "--browser" && i + 1 < a.Length)
                {
                    string b = a[++i].Trim();
                    string low = b.ToLowerInvariant();
                    if (low == "chrome") b = @"%ProgramFiles%\Google\Chrome\Application\chrome.exe";
                    else if (low == "firefox") b = @"%ProgramFiles%\Mozilla Firefox\firefox.exe";
                    else if (low == "edge") b = @"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe";
                    else if (low == "brave") b = @"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe";
                    o.Browser = b;
                }
                else if (s == "--size" && i + 1 < a.Length)
                {
                    string[] wh = a[++i].Split('x');
                    if (wh.Length == 2) { int.TryParse(wh[0], out o.Width); int.TryParse(wh[1], out o.Height); }
                }
            }
            if (o.Port <= 0 || o.Port > 65535) o.Port = DEFAULT_PORT;
            o.ProfileDir = Path.Combine(o.HubDir, "appprofile");
            return o;
        }
    }

    /// <summary>Fallback when no chromium is installed: keep the hub alive and visible.</summary>
    class ServerWindow : Form
    {
        public ServerWindow(string url)
        {
            Text = "nfl hub";
            Width = 520; Height = 170;
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false; MinimizeBox = true;
            BackColor = Color.FromArgb(11, 12, 15);

            Label lab = new Label();
            lab.ForeColor = Color.FromArgb(233, 234, 238);
            lab.Font = new Font("Segoe UI", 10f);
            lab.Dock = DockStyle.Fill;
            lab.TextAlign = ContentAlignment.MiddleCenter;
            lab.Text = "nfl hub is running.\n\n" + url + "\n\nClose this window to stop it.";
            Controls.Add(lab);
        }
    }
}
