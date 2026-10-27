using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

internal sealed class LocalService : IDisposable {
    internal Process Child;
    internal string Url;
    internal string Root=AppDomain.CurrentDomain.BaseDirectory;
    internal string Instance=Guid.NewGuid().ToString("N");
    internal Dictionary<string,object> State;
    internal async Task Start() {
        string python=Path.Combine(Root,"futures-runtime","pythonw.exe");
        if(!File.Exists(python))throw new FileNotFoundException("FUTURES_RUNTIME_MISSING");
        string script=Path.Combine(Root,"taishin_futures_web.py");
        var info=new ProcessStartInfo(python,"-u \""+script+"\" --port 0 --instance-id "+Instance+" --parent-pid "+Process.GetCurrentProcess().Id);
        info.WorkingDirectory=Root;info.UseShellExecute=false;info.CreateNoWindow=true;info.WindowStyle=ProcessWindowStyle.Hidden;
        info.RedirectStandardError=true;info.RedirectStandardOutput=true;
        Child=new Process();Child.StartInfo=info;Child.ErrorDataReceived+=(s,e)=>{};Child.OutputDataReceived+=(s,e)=>{};
        Child.Start();Child.BeginErrorReadLine();Child.BeginOutputReadLine();
        string path=Path.Combine(Root,"private","futures-web-"+Instance+".json");
        var serializer=new JavaScriptSerializer();
        for(int i=0;i<120;i++) {
            if(Child.HasExited)throw new InvalidOperationException("LOCAL_SERVICE_EXITED");
            if(File.Exists(path)) {
                try {
                    var state=serializer.Deserialize<Dictionary<string,object>>(File.ReadAllText(path));
                    if(Convert.ToInt32(state["pid"])==Child.Id && Convert.ToString(state["phase"])=="serving") {
                        Uri url;
                        if(!Uri.TryCreate(Convert.ToString(state["url"]),UriKind.Absolute,out url) || url.Scheme!="http" || url.Host!="127.0.0.1")throw new InvalidOperationException("LOCAL_URL_INVALID");
                        Url=url.AbsoluteUri;State=state;return;
                    }
                }catch(IOException){}catch(ArgumentException){}
            }
            await Task.Delay(250);
        }
        throw new TimeoutException("LOCAL_SERVICE_START_TIMEOUT");
    }
    public void Dispose() {
        if(Child!=null) {try{if(!Child.HasExited){Child.Kill();Child.WaitForExit(4000);}}catch{}Child.Dispose();Child=null;}
    }
}

internal sealed class MainWindow : Form {
    LocalService service;
    WebView2 browser;
    Label message;
    Button retry;
    bool starting;
    internal MainWindow() {
        Text="Momentum · 台新期貨";Icon=Icon.ExtractAssociatedIcon(Application.ExecutablePath);StartPosition=FormStartPosition.CenterScreen;
        var bounds=Screen.PrimaryScreen.WorkingArea;Size=new Size(Math.Min(1320,bounds.Width-60),Math.Min(930,bounds.Height-60));MinimumSize=new Size(820,600);
        Font=new Font("Microsoft JhengHei UI",10);BackColor=Color.FromArgb(245,247,249);
        var intro=new TableLayoutPanel();intro.Dock=DockStyle.Fill;intro.ColumnCount=1;intro.RowCount=3;
        intro.RowStyles.Add(new RowStyle(SizeType.Percent,45));intro.RowStyles.Add(new RowStyle(SizeType.AutoSize));intro.RowStyles.Add(new RowStyle(SizeType.Percent,55));
        message=new Label();message.AutoSize=true;message.Anchor=AnchorStyles.None;message.Text="正在啟動台新期貨工作台……";message.Font=new Font(Font.FontFamily,15);message.ForeColor=Color.FromArgb(24,45,56);
        var actions=new FlowLayoutPanel();actions.AutoSize=true;actions.Anchor=AnchorStyles.None;
        retry=new Button();retry.Text="重新啟動";retry.AutoSize=true;retry.Visible=false;retry.Click+=async(s,e)=>await StartApp();actions.Controls.Add(retry);
        intro.Controls.Add(message,0,0);intro.Controls.Add(actions,0,1);Controls.Add(intro);
        Shown+=async(s,e)=>await StartApp();FormClosed+=(s,e)=>{if(browser!=null)browser.Dispose();if(service!=null)service.Dispose();};
    }
    async Task StartApp() {
        if(starting)return;starting=true;retry.Visible=false;
        try {
            if(service!=null)service.Dispose();service=new LocalService();await service.Start();
            message.Text="服務已就緒，正在開啟工作台……";
            if(browser!=null){Controls.Remove(browser);browser.Dispose();}
            browser=new WebView2();browser.Dock=DockStyle.Fill;Controls.Add(browser);browser.BringToFront();
            string profile=Path.Combine(service.Root,"private","desktop-profile");
            var env=await CoreWebView2Environment.CreateAsync(null,profile,null);
            await browser.EnsureCoreWebView2Async(env);
            browser.CoreWebView2.Settings.IsPasswordAutosaveEnabled=false;
            browser.CoreWebView2.Settings.IsGeneralAutofillEnabled=false;
            browser.CoreWebView2.Settings.IsStatusBarEnabled=false;
            browser.CoreWebView2.Settings.AreDevToolsEnabled=false;
            browser.CoreWebView2.Settings.AreDefaultContextMenusEnabled=false;
            browser.CoreWebView2.NavigationStarting+=(s,e)=>{Uri target;Uri origin=new Uri(service.Url);if(!Uri.TryCreate(e.Uri,UriKind.Absolute,out target) || target.Scheme!="http" || target.Host!="127.0.0.1" || target.Port!=origin.Port)e.Cancel=true;};
            browser.CoreWebView2.NewWindowRequested+=(s,e)=>e.Handled=true;
            browser.CoreWebView2.PermissionRequested+=(s,e)=>e.State=CoreWebView2PermissionState.Deny;
            browser.CoreWebView2.Navigate(service.Url);
            WriteStatus("window_open",null);
        }catch(Exception ex){if(browser!=null)browser.Visible=false;message.Text="工作台啟動失敗（"+ex.GetType().Name+"）。\n請按「重新啟動」；不需要開啟 CMD。";retry.Visible=true;WriteStatus("start_failed",ex.GetType().Name);if(service!=null)service.Dispose();}
        finally{starting=false;}
    }
    void WriteStatus(string stage,string error) {
        try{string root=AppDomain.CurrentDomain.BaseDirectory;Directory.CreateDirectory(Path.Combine(root,"private"));File.WriteAllText(Path.Combine(root,"private","desktop-status.json"),new JavaScriptSerializer().Serialize(new{stage=stage,error=error,checked_at=DateTimeOffset.UtcNow.ToString("o"),pid=Process.GetCurrentProcess().Id,live_enabled=false,order_enabled=false}));}catch{}
    }
}

internal static class Program {
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hWnd,int command);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hWnd);
    static bool ShowExisting() {
        var current=Process.GetCurrentProcess();
        foreach(var process in Process.GetProcesses()) {
            try {
                if(process.Id==current.Id || process.SessionId!=current.SessionId)continue;
                if(process.ProcessName!="Momentum" && process.ProcessName!="MomentumTaishin")continue;
                if(process.MainWindowHandle!=IntPtr.Zero) {ShowWindow(process.MainWindowHandle,9);SetForegroundWindow(process.MainWindowHandle);return true;}
            }catch{}finally{process.Dispose();}
        }
        return false;
    }
    [STAThread] static int Main(string[] args) {
        if(args.Length>0 && args[0]=="--self-test")return SelfTest().GetAwaiter().GetResult();
        bool created;using(var mutex=new Mutex(true,"Local\\MomentumTaishinFuturesDesktop",out created)) {
            if(!created){if(ShowExisting())return 0;MessageBox.Show("APP 已開啟，請切換至現有的 Momentum 視窗。","Momentum");return 0;}
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);Application.Run(new MainWindow());
        }
        return 0;
    }
    static async Task<int> SelfTest() {
        bool ok=false;string error=null;int pid=0;bool http=false;
        using(var service=new LocalService()) {
            try{await service.Start();pid=service.Child.Id;var request=(HttpWebRequest)WebRequest.Create(service.Url);request.Proxy=null;request.Timeout=3000;using(var response=(HttpWebResponse)await request.GetResponseAsync()){http=response.StatusCode==HttpStatusCode.OK;}ok=http;}catch(Exception ex){error=ex.GetType().Name;}
        }
        bool stopped=true;try{stopped=Process.GetProcessById(pid).HasExited;}catch{}
        string root=AppDomain.CurrentDomain.BaseDirectory;Directory.CreateDirectory(Path.Combine(root,"private"));
        File.WriteAllText(Path.Combine(root,"private","desktop-self-test.json"),new JavaScriptSerializer().Serialize(new{ok=ok,http_ok=http,server_stopped=stopped,error=error,checked_at=DateTimeOffset.UtcNow.ToString("o"),live_enabled=false,order_enabled=false}));
        return ok&&stopped?0:1;
    }
}
