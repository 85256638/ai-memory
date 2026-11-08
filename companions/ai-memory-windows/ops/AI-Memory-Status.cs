using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

[assembly: AssemblyTitle("AI Memory 状态")]
[assembly: AssemblyProduct("AI Memory 本机状态托盘")]
[assembly: AssemblyVersion("1.0.0.0")]

internal static class Program
{
    internal const string MutexName = @"Local\AiMemoryStatusTray";
    internal static readonly string Ops = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
    internal static readonly string Disabled = Path.Combine(Ops, "tray.disabled");

    internal static string Setting(string key, string fallback)
    {
        string path = Path.Combine(Ops, "windows-settings.json");
        if (!File.Exists(path)) return fallback;
        Dictionary<string, object> settings = new JavaScriptSerializer()
            .Deserialize<Dictionary<string, object>>(File.ReadAllText(path, Encoding.UTF8));
        object value;
        if (!settings.TryGetValue(key, out value)) return fallback;
        string text = value as string;
        if (String.IsNullOrEmpty(text) || !Path.IsPathRooted(text))
            throw new InvalidDataException("Invalid configured path.");
        return text;
    }

    [STAThread]
    private static void Main(string[] args)
    {
        try
        {
            if (Array.IndexOf(args, "--login") >= 0 || Array.IndexOf(args, "--enable") >= 0)
                if (File.Exists(Disabled)) File.Delete(Disabled);
            if (File.Exists(Disabled)) return;
            bool created;
            using (Mutex mutex = new Mutex(true, MutexName, out created))
            {
                if (!created) return;
                try
                {
                    Application.EnableVisualStyles();
                    Application.SetCompatibleTextRenderingDefault(false);
                    Application.Run(new StatusTray());
                }
                finally { mutex.ReleaseMutex(); }
            }
        }
        catch (Exception error)
        {
            // Diagnostic class only: never write HTTP response bodies or secrets.
            File.WriteAllText(Path.Combine(Ops, "tray-error.txt"),
                DateTimeOffset.UtcNow.ToString("o") + " " + error.GetType().Name, Encoding.UTF8);
        }
    }
}

internal sealed class StatusTray : ApplicationContext
{
    [DllImport("user32.dll")]
    private static extern bool DestroyIcon(IntPtr handle);
    private readonly NotifyIcon tray;
    private readonly Dictionary<string, Icon> icons = new Dictionary<string, Icon>();
    private readonly System.Windows.Forms.Timer timer;
    private readonly Control dispatcher = new Control();
    private readonly ToolStripMenuItem headline;
    private int checking;
    private bool exiting;
    private readonly JavaScriptSerializer json = new JavaScriptSerializer();

    internal StatusTray()
    {
        dispatcher.CreateControl();
        icons.Add("healthy", DrawIcon(Color.FromArgb(29, 170, 91)));
        icons.Add("warning", DrawIcon(Color.FromArgb(225, 159, 24)));
        icons.Add("failed", DrawIcon(Color.FromArgb(218, 54, 61)));
        icons.Add("maintenance", DrawIcon(Color.FromArgb(112, 124, 139)));
        ContextMenuStrip menu = new ContextMenuStrip();
        headline = new ToolStripMenuItem("AI Memory：正在检查") { Enabled = false };
        menu.Items.Add(headline);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("查看运行状态", null, delegate { ShowStatus(); });
        menu.Items.Add("立即检查并尝试恢复", null, delegate { CheckNow(); });
        menu.Items.Add("打开 AI Memory 网页", null,
            delegate { Process.Start("http://127.0.0.1:49374/web"); });
        menu.Items.Add("打开备份文件夹", null,
            delegate { Process.Start("explorer.exe", Quote(Program.Setting("backups", Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "ai-memory-backups")))); });
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("退出状态图标（记忆服务继续运行）", null, delegate {
            File.WriteAllText(Program.Disabled, DateTimeOffset.UtcNow.ToString("o"));
            ExitThread();
        });
        tray = new NotifyIcon {
            Icon = icons["warning"], Text = "AI Memory：正在检查",
            ContextMenuStrip = menu, Visible = true
        };
        tray.DoubleClick += delegate { ShowStatus(); };
        timer = new System.Windows.Forms.Timer { Interval = 15000 };
        timer.Tick += delegate { Probe(); };
        timer.Start();
        Probe();
    }

    private static string Quote(string value) { return "\"" + value + "\""; }

    private void ShowStatus()
    {
        Process.Start("notepad.exe", Quote(Path.Combine(Program.Ops, "status.txt")));
    }

    private void CheckNow()
    {
        string python = Program.Setting("pythonw", Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            @"Programs\Python\Python311\pythonw.exe"));
        Process.Start(new ProcessStartInfo(python,
            Quote(Path.Combine(Program.Ops, "watch_ai_memory.py")) + " --show") {
            UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = Program.Ops
        });
        // The next short HTTP poll shows recovery without waiting for a new login.
        Probe();
    }

    private static Icon DrawIcon(Color color)
    {
        using (Bitmap bitmap = new Bitmap(32, 32))
        {
            using (Graphics graphics = Graphics.FromImage(bitmap))
            using (Brush brush = new SolidBrush(color))
            using (Font font = new Font("Segoe UI", 17, FontStyle.Bold, GraphicsUnit.Pixel))
            using (StringFormat format = new StringFormat {
                Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center })
            {
                graphics.Clear(Color.Transparent);
                graphics.SmoothingMode = SmoothingMode.AntiAlias;
                graphics.FillEllipse(brush, 1, 1, 30, 30);
                graphics.DrawString("M", font, Brushes.White, new RectangleF(0, 0, 32, 31), format);
            }
            IntPtr handle = bitmap.GetHicon();
            try { return (Icon)Icon.FromHandle(handle).Clone(); }
            finally { DestroyIcon(handle); }
        }
    }

    private void Probe()
    {
        if (Interlocked.Exchange(ref checking, 1) != 0 || exiting) return;
        ThreadPool.QueueUserWorkItem(delegate {
            string state = "failed";
            string label = "服务不可达";
            string version = "";
            try
            {
                bool online = false;
                try
                {
                    HttpWebRequest request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:49374/healthz");
                    request.Proxy = null;
                    request.Timeout = 2500;
                    request.ReadWriteTimeout = 2500;
                    using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                        online = response.StatusCode == HttpStatusCode.OK;
                }
                catch (WebException) { }
                Dictionary<string, object> status = null;
                string statusPath = Path.Combine(Program.Ops, "status.json");
                if (File.Exists(statusPath))
                    status = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(statusPath, Encoding.UTF8));
                if (File.Exists(Path.Combine(Program.Ops, "maintenance.flag")))
                {
                    state = "maintenance";
                    label = online ? "维护中，服务在线" : "维护中，服务离线";
                }
                else if (!online) { state = "failed"; label = "服务不可达"; }
                else if (status == null) { state = "warning"; label = "服务在线，等待完整检查"; }
                else
                {
                    DateTimeOffset checkTime;
                    object value;
                    bool fresh = status.TryGetValue("last_check", out value)
                        && DateTimeOffset.TryParse(Convert.ToString(value), out checkTime)
                        && (DateTimeOffset.UtcNow - checkTime).TotalMinutes < 10;
                    string health = status.ContainsKey("health") ? Convert.ToString(status["health"]) : "";
                    version = status.ContainsKey("server_version") ? Convert.ToString(status["server_version"]) : "";
                    if (!fresh) { state = "warning"; label = "完整检查超过 10 分钟未刷新"; }
                    else if (health == "healthy") { state = "healthy"; label = "运行正常"; }
                    else if (health == "failed") { state = "failed"; label = "认证或监测检查失败"; }
                    else { state = "warning"; label = "需要检查，双击查看"; }
                }
                Dictionary<string, object> receipt = new Dictionary<string, object> {
                    { "time_utc", DateTimeOffset.UtcNow.ToString("o") },
                    { "pid", Process.GetCurrentProcess().Id },
                    { "state", state }, { "label", label }, { "health_endpoint_ok", online },
                    { "shell_icon_requested_visible", true }, { "poll_interval_seconds", 15 }
                };
                string pending = Path.Combine(Program.Ops, "tray-status.json.tmp");
                File.WriteAllText(pending, json.Serialize(receipt), Encoding.UTF8);
                string final = Path.Combine(Program.Ops, "tray-status.json");
                if (File.Exists(final)) File.Replace(pending, final, null);
                else File.Move(pending, final);
            }
            catch (Exception)
            {
                state = "warning";
                label = "无法读取完整状态";
            }
            if (!exiting && dispatcher.IsHandleCreated)
            {
                try
                {
                    dispatcher.BeginInvoke((MethodInvoker)delegate {
                        if (exiting) return;
                        tray.Icon = icons[state];
                        string text = "AI Memory：" + label + (version.Length == 0 ? "" : "  v" + version);
                        tray.Text = text.Length > 63 ? text.Substring(0, 63) : text;
                        headline.Text = text;
                    });
                }
                catch (InvalidOperationException) { }
            }
            Interlocked.Exchange(ref checking, 0);
        });
    }

    protected override void ExitThreadCore()
    {
        exiting = true;
        timer.Stop();
        timer.Dispose();
        tray.Visible = false;
        tray.Dispose();
        dispatcher.Dispose();
        foreach (Icon icon in icons.Values) icon.Dispose();
        base.ExitThreadCore();
    }
}
