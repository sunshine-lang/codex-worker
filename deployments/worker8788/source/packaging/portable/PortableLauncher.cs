using System;
using System.Diagnostics;
using System.IO;
using System.Text;

internal static class PortableLauncher
{
    static readonly string[] Required = {
        @".runtime\python313\python.exe", "portable.py", "worker-settings.json",
        @"bin\CodexRemoteWorker\CodexRemoteWorker.exe", @"internal\verify-json-runtime.py"
    };

    static int Run(string[] args)
    {
        if (args.Length == 1 && (args[0] == "--help" || args[0] == "-h"))
        {
            Console.WriteLine("Codex Worker portable launcher\nUsage: CodexWorker.exe [start|status|stop|check] [--no-pause]\n       CodexWorker.exe --check-runtime\nDouble-click to start. Keep this EXE inside the complete extracted folder.");
            return 0;
        }
        string command = "start";
        bool selected = false, checkRuntime = false;
        foreach (string arg in args)
        {
            if (arg == "--no-pause") continue;
            if (!selected && (arg == "start" || arg == "stop" || arg == "status" || arg == "check" || arg == "--check-runtime"))
            {
                command = arg;
                checkRuntime = arg == "--check-runtime";
                selected = true;
            }
            else
            {
                Console.Error.WriteLine("INVALID_COMMAND: use start, status, stop or check. See --help.");
                return 2;
            }
        }
        string root = AppDomain.CurrentDomain.BaseDirectory;
        foreach (string relative in Required)
        {
            if (!File.Exists(Path.Combine(root, relative)))
            {
                Console.Error.WriteLine("PACKAGE_INCOMPLETE: 缺少 " + relative + "。请完整解压，保留 EXE 所在目录的全部文件。");
                return 3;
            }
        }
        if (checkRuntime)
        {
            Console.WriteLine("PACKAGE_FILES_PRESENT: 启动所需文件存在。此检查未验证登录、沙箱、运行库或服务就绪。");
            return 0;
        }
        ProcessStartInfo start = new ProcessStartInfo {
            FileName = Path.Combine(root, @".runtime\python313\python.exe"),
            Arguments = "-X utf8 \"" + Path.Combine(root, "portable.py") + "\" " + command,
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = false
        };
        start.EnvironmentVariables["PYTHONUTF8"] = "1";
        start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        using (Process child = Process.Start(start))
        {
            if (child == null) return 4;
            child.WaitForExit();
            return child.ExitCode;
        }
    }

    static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        int result;
        try { result = Run(args); }
        catch (Exception) {
            Console.Error.WriteLine("LAUNCH_FAILED: 无法启动本目录运行环境。请确认完整解压；运行库缺失时查看 使用说明.txt。");
            result = 4;
        }
        // Report success only after the controller confirms readiness.
        if (args.Length == 0)
        {
            Console.WriteLine(result == 0
                ? "\n本次启动已完成。关闭窗口后，已就绪的后台 Worker 会继续运行；停止请双击 停止服务.cmd。"
                : "\n本次启动未完成，请先处理上方错误后重试。此窗口没有确认后台服务已就绪。");
            if (!Console.IsInputRedirected)
            {
                Console.WriteLine("按任意键关闭此窗口。");
                Console.ReadKey(true);
            }
        }
        return result;
    }
}
