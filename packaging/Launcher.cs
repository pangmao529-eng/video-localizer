using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;

[assembly: AssemblyTitle("Young Tiger Video Localizer")]
[assembly: AssemblyVersion("0.1.0.0")]

internal static class Launcher
{
    // ProcessStartInfo on .NET Framework takes a single Windows command line.
    // Quote backslashes and quotes according to CommandLineToArgvW conventions.
    private static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in value)
        {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"')
                result.Append('\\', slashes * 2 + 1);
            else
                result.Append('\\', slashes);
            result.Append(ch);
            slashes = 0;
        }
        return result.Append('\\', slashes * 2).Append('"').ToString();
    }

    private static int Main(string[] args)
    {
        try
        {
            string launcher = Assembly.GetExecutingAssembly().Location;
            string home = Path.GetDirectoryName(launcher);
            string revision = File.ReadAllText(Path.Combine(home, "active.txt")).Trim();
            if (!Regex.IsMatch(revision, "^[0-9a-f]{32}$"))
                throw new IOException("Installation metadata is invalid. Run the installer again.");
            string release = Path.Combine(home, "versions", revision);
            string media = Path.Combine(release, "bin");
            var arguments = new List<string> { "-X", "utf8", "-B", "-m", "video_localizer" };
            arguments.AddRange(args.Length == 0 ? new string[] { "setup", "--gui" } : args);
            var start = new ProcessStartInfo(Path.Combine(release, "runtime", "python.exe"));
            start.UseShellExecute = false;
            start.Arguments = String.Join(" ", arguments.ConvertAll(Quote).ToArray());
            start.EnvironmentVariables["VIDEO_LOCALIZER_EXE"] = launcher;
            start.EnvironmentVariables["VIDEO_LOCALIZER_FFMPEG"] = Path.Combine(media, "ffmpeg.exe");
            start.EnvironmentVariables["VIDEO_LOCALIZER_FFPROBE"] = Path.Combine(media, "ffprobe.exe");
            start.EnvironmentVariables["VIDEO_LOCALIZER_FONT"] = Path.Combine(release, "fonts", "NotoSansCJKkr-Regular.otf");
            start.EnvironmentVariables["PATH"] = media + Path.PathSeparator + Environment.GetEnvironmentVariable("PATH");
            using (var child = Process.Start(start))
            {
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("Video Localizer: " + error.Message);
            return 1;
        }
    }
}
