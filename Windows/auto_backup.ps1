#requires -Version 5.1
<#
Windows 文件/文件夹定时备份（Windows PowerShell 5.1 或更新版本）。

使用方法：
  1. 编辑下方配置区的版本号、任务列表、复制方式、目标目录和时间间隔。
  2. 将脚本保存为 UTF-8（带 BOM），在 PowerShell 中执行 .\auto_backup.ps1。
     所有设置都在脚本中，不需要命令行参数。
     默认使用 Vss：普通权限启动时会请求 Windows UAC 管理员授权。
     同意后在新的管理员 PowerShell 窗口运行，原启动进程不再执行备份；
     取消授权则提示并停止，不会降级复制或反复请求。已经提升时不重复请求。
     Cooperative 模式不请求管理员权限；也可以自行从管理员 PowerShell 启动。
     提权后保留窗口以便查看错误和单次执行结果，不会永久修改系统执行策略。
  3. 启动时显示版本、任务列表等信息及 ---；随后立即备份第一轮。
     每个任务只输出一条结果日志；明细和摘要保存在任务目录的 .backup-info 下。
     若源名称也是 .backup-info，元数据目录改名为 .backup-info_，避免覆盖源内容。
     $RunOnce = $true 可只运行一轮；持续运行时按 Ctrl+C 停止。

任务字段（顺序为 id、mode、path）：
  id：唯一任务名，仅限字母、数字、下划线和连字符，不能重名（不区分大小写）。
  mode：folder 备份整个文件夹；file 备份单个文件。
  path：源路径。各任务可以共用一个源路径，但必须使用不同 id。
目标布局：D:\Backups\任务id_Unix秒时间戳\源文件夹名\... 或 \源文件名。
同一 id 在同一秒重复执行时等下一秒创建新目录，不覆盖已有目录。

$BackupMode 决定所有任务使用哪一种复制方式（与任务的 mode 字段不同）：

Cooperative：申请 RH oplock；冲突、映射写入或不支持 oplock 时跳过文件。
             共享写入无需等待备份确认；独占打开可能等待我们关闭源句柄。
             不保证任意打开方式下零错误、零等待，也不保证多文件事务一致。
Vss：每个任务为源所在的本地卷创建快照，再从快照复制，需要管理员权限。
     创建时可能短暂停顿源卷的写入；权限、空间、VSS 服务等仍可能导致失败。
     使用 ClientAccessible 上下文，不协调应用 writer，只有崩溃一致性。
     Windows 自带提供者按卷使用写时复制，创建快照无需先复制完整文件。
     因此，创建时冻结写入的等待时间不按单个文件大小线性增长；实际仍受
     缓存刷新、当前磁盘负载等影响，不能承诺固定时长或完全无感。
     冻结结束后即可恢复写入，不必等待整个文件或目录备份完成。

两种模式都复制普通文件的主数据流、最后修改时间和目录结构（含隐藏文件、
系统文件和空目录）；不复制 ACL、ADS、硬链接关系等特殊元数据。
仅支持本地 Windows 卷，不支持 UNC/网络共享；文件夹源和目标不得互相包含，
文件源也不能放在备份目标中。文件任务不接受符号链接等重解析点。
子目录/文件中的符号链接、junction 等重解析点跳过，并把本轮标为 Partial。
选中的文件夹根目录可以是链接，每次任务执行时解析为实际目录；不递归进入子链接。
每个任务每次都是独立全量备份，跳过的文件下一轮重试；不会删除旧备份。
间隔从上一轮结束后开始计算，循环串行执行。Ctrl+C 停止。
强制结束进程/断电可能留下 VSS 快照；元数据中的 events.jsonl 会记录快照 ID。

建议：对于本地文件/小目录，能接受创建快照时短暂停写并具备管理员权限时，
默认推荐 Vss，尤其适合备份可能被程序长期占用的源文件。
不建议用于备份大文件目录；完整备份的存储体积和读取文件所需时间都会成为
瓶颈。快照创建时的短暂冻结与后续完整复制是两个阶段；复制仍需读取全部
数据，并与其他程序竞争磁盘带宽。Vss 不代表存档逻辑必然完整或备份必定成功。
#>

# ======================== 用户配置 ========================
$ScriptVersion = '2.0.0'
$BackupMode = 'Vss'                  # 默认推荐 'Vss'（需管理员）；也可选 'Cooperative'
$BackupTasks = @(
    # PBZ <Steam库>\steamapps\common\PBZ\ProjectPBZ\Saved\SaveGames\Steam\<SteamID64>\
    # Timberborn %USERPROFILE%\Documents\Timberborn\Saves\
    # @{ id = 'back_game_a';      mode = 'folder'; path = 'C:/xxx/a' }
    # @{ id = 'back_doc_my_work'; mode = 'file';   path = 'C:/xxx/a.docx' }
)
$DestinationRoot = 'D:/Backups'
$IntervalSeconds = 500               # 上一轮结束后等待的秒数
$BufferKiB = 256                     # 每次读取的块大小，范围 4..4096 KiB
$RunOnce = $false                    # $true：只运行一轮；$false：持续定时运行
# ==========================================================

Set-StrictMode -Version 2.0
$script:BackupEntryPath = $PSCommandPath

$script:NativeCode = @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace FolderBackup
{
    public sealed class NativeCopyResult
    {
        public string Status { get; set; }
        public string Message { get; set; }
        public long BytesCopied { get; set; }
        public DateTime SourceLastWriteTimeUtc { get; set; }
    }

    public sealed class NativeDirectoryEntry
    {
        public string Name { get; set; }
        public bool IsDirectory { get; set; }
        public bool IsReparsePoint { get; set; }
        public long Length { get; set; }
    }

    // For Windows 8+ and PowerShell 5.1 / .NET Framework 4.x.
    // RH permits normal writes to proceed and lets us close on a conflicting open.
    // A conflicting exclusive opener can still wait for cancellation/handle cleanup.
    public static class NativeCopy
    {
        private const uint GenericRead = 0x80000000;
        private const uint ShareAll = 7;
        private const uint OpenExisting = 3;
        private const uint AttributeNormal = 0x80;
        private const uint AttributeDirectory = 0x10;
        private const uint AttributeReparsePoint = 0x400;
        private const uint FlagOverlapped = 0x40000000;
        private const uint FlagSequentialScan = 0x08000000;
        private const uint FlagOpenReparsePoint = 0x00200000;
        private const uint FlagOpenRequiringOplock = 0x00040000;
        private const uint RequestOplock = 0x00090240;
        private const int ErrorIoPending = 997;
        private const int ErrorHandleEof = 38;
        private const int ErrorNoMoreFiles = 18;

        [StructLayout(LayoutKind.Sequential)]
        private struct ExtendedParameters
        {
            public uint Size, Attributes, Flags, SecurityQosFlags;
            public IntPtr SecurityAttributes, TemplateFile;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct OplockInput
        {
            public ushort Version, Length;
            public uint Level, Flags;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct NativeOverlappedData
        {
            public UIntPtr Internal, InternalHigh;
            public uint Offset, OffsetHigh;
            public IntPtr Event;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct NativeFileTime
        {
            public uint Low, High;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct FileInformation
        {
            public uint Attributes;
            public NativeFileTime CreationTime, LastAccessTime, LastWriteTime;
            public uint VolumeSerialNumber, SizeHigh, SizeLow, LinkCount;
            public uint FileIndexHigh, FileIndexLow;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct FindData
        {
            public uint Attributes;
            public NativeFileTime CreationTime, LastAccessTime, LastWriteTime;
            public uint SizeHigh, SizeLow, Reserved0, Reserved1;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
            public string Name;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 14)]
            public string AlternateName;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
        private static extern SafeFileHandle CreateFile2(string name, uint access,
            uint share, uint creation, ref ExtendedParameters parameters);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
        private static extern SafeFileHandle CreateFileW(string name, uint access,
            uint share, IntPtr security, uint creation, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool DeviceIoControl(SafeFileHandle file, uint code,
            IntPtr input, uint inputSize, IntPtr output, uint outputSize,
            IntPtr bytesReturned, IntPtr overlapped);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool ReadFile(SafeFileHandle file, IntPtr buffer,
            uint count, out uint countRead, IntPtr overlapped);
        [DllImport("kernel32.dll", EntryPoint = "ReadFile", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool ReadFileAsync(SafeFileHandle file, IntPtr buffer,
            uint count, IntPtr countRead, IntPtr overlapped);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetOverlappedResult(SafeFileHandle file,
            IntPtr overlapped, out uint bytesTransferred,
            [MarshalAs(UnmanagedType.Bool)] bool wait);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CancelIoEx(SafeFileHandle file, IntPtr overlapped);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetFileInformationByHandle(SafeFileHandle file,
            out FileInformation info);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
        private static extern IntPtr FindFirstFileW(string pattern, out FindData data);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool FindNextFileW(IntPtr find, out FindData data);
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool FindClose(IntPtr find);

        private sealed class ConflictException : IOException
        {
            public ConflictException(string message) : base(message) { }
        }

        private sealed class SourceSession : IDisposable
        {
            public SafeFileHandle File;
            public readonly ManualResetEvent OplockEvent = new ManualResetEvent(false);
            public readonly ManualResetEvent ReadEvent = new ManualResetEvent(false);
            private readonly ManualResetEvent StopEvent = new ManualResetEvent(false);
            public readonly IntPtr OplockOverlapped;
            public readonly IntPtr ReadOverlapped;
            public readonly IntPtr OplockInputBuffer;
            public readonly IntPtr OplockOutputBuffer;
            public bool OplockPending;
            public bool ReadPending;
            public volatile bool Broken;
            private volatile bool Stopping;
            private Thread Watcher;
            private bool Closed;

            public SourceSession()
            {
                OplockOverlapped = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(NativeOverlappedData)));
                ReadOverlapped = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(NativeOverlappedData)));
                OplockInputBuffer = Marshal.AllocHGlobal(12);
                OplockOutputBuffer = Marshal.AllocHGlobal(24);
                NativeOverlappedData op = new NativeOverlappedData();
                op.Event = OplockEvent.SafeWaitHandle.DangerousGetHandle();
                Marshal.StructureToPtr(op, OplockOverlapped, false);
                OplockInput input = new OplockInput();
                input.Version = 1;
                input.Length = 12;
                input.Level = 1 | 2; // OPLOCK_LEVEL_CACHE_READ | OPLOCK_LEVEL_CACHE_HANDLE
                input.Flags = 1;     // REQUEST_OPLOCK_INPUT_FLAG_REQUEST
                Marshal.StructureToPtr(input, OplockInputBuffer, false);
                for (int i = 0; i < 24; ++i) Marshal.WriteByte(OplockOutputBuffer, i, 0);
            }

            public void StartWatcher()
            {
                Watcher = new Thread(delegate()
                {
                    int signaled = WaitHandle.WaitAny(new WaitHandle[] { OplockEvent, StopEvent });
                    if (signaled == 0 && !Stopping)
                    {
                        Broken = true;
                        // Do this on the watcher thread, including while target writes are slow.
                        // Cancellation may itself need time; buffer ownership stays with the reader.
                        CancelAndClose();
                    }
                });
                Watcher.IsBackground = true;
                Watcher.Name = "FolderBackup oplock watcher";
                Watcher.Start();
            }

            public void CheckUnbroken()
            {
                if (Broken || (OplockPending && OplockEvent.WaitOne(0)))
                    throw new ConflictException("The source changed or another opener needs access.");
            }

            public void PrepareRead(long offset)
            {
                ReadEvent.Reset();
                NativeOverlappedData op = new NativeOverlappedData();
                op.Offset = unchecked((uint)offset);
                op.OffsetHigh = unchecked((uint)(offset >> 32));
                op.Event = ReadEvent.SafeWaitHandle.DangerousGetHandle();
                Marshal.StructureToPtr(op, ReadOverlapped, false);
            }

            private void CancelAndClose()
            {
                SafeFileHandle file = File;
                if (file == null) return;
                try
                {
                    // Do not cancel the pending oplock before closing: removing H protection
                    // while a read handle remains open could cause a foreground sharing error.
                    if (!file.IsClosed && !file.IsInvalid) CancelIoEx(file, ReadOverlapped);
                }
                catch (ObjectDisposedException) { }
                file.Dispose();
            }

            public void CloseSource()
            {
                if (Closed) return;
                Stopping = true;
                StopEvent.Set();
                CancelAndClose();
                if (Watcher != null) Watcher.Join();
                // Closing/canceling does not entitle us to free an OVERLAPPED or its buffer.
                // Wait until the kernel has actually completed every outstanding operation.
                if (ReadPending)
                {
                    ReadEvent.WaitOne();
                    ReadPending = false;
                }
                if (OplockPending)
                {
                    OplockEvent.WaitOne();
                    OplockPending = false;
                }
                Closed = true;
            }

            public void Dispose()
            {
                CloseSource();
                Marshal.FreeHGlobal(OplockOutputBuffer);
                Marshal.FreeHGlobal(OplockInputBuffer);
                Marshal.FreeHGlobal(ReadOverlapped);
                Marshal.FreeHGlobal(OplockOverlapped);
                StopEvent.Dispose();
                ReadEvent.Dispose();
                OplockEvent.Dispose();
            }
        }

        public static string ExtendedPath(string path)
        {
            if (path.StartsWith(@"\\?\", StringComparison.Ordinal)) return path;
            string full = Path.GetFullPath(path);
            return full.StartsWith(@"\\", StringComparison.Ordinal)
                ? @"\\?\UNC\" + full.Substring(2) : @"\\?\" + full;
        }

        public static NativeDirectoryEntry[] EnumerateDirectory(string path)
        {
            List<NativeDirectoryEntry> entries = new List<NativeDirectoryEntry>();
            FindData data;
            IntPtr find = FindFirstFileW(ExtendedPath(path).TrimEnd('\\') + @"\*", out data);
            if (find == new IntPtr(-1))
            {
                int error = Marshal.GetLastWin32Error();
                if (error == 2) return entries.ToArray();
                throw new Win32Exception(error, "Cannot enumerate directory: " + path);
            }
            try
            {
                do
                {
                    if (data.Name == "." || data.Name == "..") continue;
                    entries.Add(new NativeDirectoryEntry
                    {
                        Name = data.Name,
                        IsDirectory = (data.Attributes & AttributeDirectory) != 0,
                        IsReparsePoint = (data.Attributes & AttributeReparsePoint) != 0,
                        Length = unchecked((long)(((ulong)data.SizeHigh << 32) | data.SizeLow))
                    });
                }
                while (FindNextFileW(find, out data));
                int error = Marshal.GetLastWin32Error();
                if (error != ErrorNoMoreFiles)
                    throw new Win32Exception(error, "Cannot continue enumerating directory: " + path);
            }
            finally { FindClose(find); }
            return entries.ToArray();
        }

        public static NativeCopyResult CopyFile(string source, string destination, int bufferBytes)
        {
            return CopyCore(source, destination, bufferBytes, true);
        }

        public static NativeCopyResult CopySnapshotFile(string source, string destination, int bufferBytes)
        {
            return CopyCore(source, destination, bufferBytes, false);
        }

        private static NativeCopyResult CopyCore(string source, string destination, int bufferBytes, bool guarded)
        {
            NativeCopyResult result = new NativeCopyResult { Status = "Failed", Message = "", BytesCopied = 0 };
            if (bufferBytes < 4096 || bufferBytes > 16 * 1024 * 1024)
            {
                result.Message = "Buffer size must be between 4096 and 16777216 bytes.";
                return result;
            }
            SourceSession session = null;
            IntPtr nativeBuffer = IntPtr.Zero;
            FileStream output = null;
            string temporary = null;
            try
            {
                session = new SourceSession();
                if (guarded)
                {
                    ExtendedParameters parameters = new ExtendedParameters();
                    parameters.Size = (uint)Marshal.SizeOf(typeof(ExtendedParameters));
                    parameters.Attributes = AttributeNormal;
                    parameters.Flags = FlagOverlapped | FlagOpenRequiringOplock | FlagOpenReparsePoint;
                    session.File = CreateFile2(ExtendedPath(source), GenericRead, ShareAll, OpenExisting, ref parameters);
                    if (session.File.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot open source for guarded copying.");
                    // Nothing may access the file between atomic open and the oplock request.
                    bool completed = DeviceIoControl(session.File, RequestOplock, session.OplockInputBuffer,
                        12, session.OplockOutputBuffer, 24, IntPtr.Zero, session.OplockOverlapped);
                    int error = Marshal.GetLastWin32Error();
                    if (completed || error != ErrorIoPending)
                        throw new ConflictException("Cannot obtain a read/handle oplock (Win32 " + error + ").");
                    session.OplockPending = true;
                    session.StartWatcher();
                    session.CheckUnbroken();
                }
                else
                {
                    session.File = CreateFileW(ExtendedPath(source), GenericRead, ShareAll, IntPtr.Zero,
                        OpenExisting, FlagSequentialScan | FlagOpenReparsePoint, IntPtr.Zero);
                    if (session.File.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot open snapshot file.");
                }

                FileInformation initial;
                if (!GetFileInformationByHandle(session.File, out initial))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot read source metadata.");
                if ((initial.Attributes & (AttributeDirectory | AttributeReparsePoint)) != 0)
                    throw new ConflictException("Directories and reparse points are not copied as files.");
                long expected = unchecked((long)(((ulong)initial.SizeHigh << 32) | initial.SizeLow));
                long writeTime = unchecked((long)(((ulong)initial.LastWriteTime.High << 32) | initial.LastWriteTime.Low));
                result.SourceLastWriteTimeUtc = DateTime.FromFileTimeUtc(writeTime);
                string target = Path.GetFullPath(destination);
                temporary = Path.Combine(Path.GetDirectoryName(target), ".backup-" + Guid.NewGuid().ToString("N") + ".tmp");
                output = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None, bufferBytes);
                byte[] managedBuffer = new byte[bufferBytes];
                nativeBuffer = Marshal.AllocHGlobal(bufferBytes);
                while (result.BytesCopied < expected)
                {
                    if (guarded) session.CheckUnbroken();
                    uint wanted = (uint)Math.Min((long)bufferBytes, expected - result.BytesCopied);
                    uint received;
                    if (guarded)
                    {
                        session.PrepareRead(result.BytesCopied);
                        bool immediate;
                        try
                        {
                            immediate = ReadFileAsync(session.File, nativeBuffer, wanted, IntPtr.Zero, session.ReadOverlapped);
                        }
                        catch (ObjectDisposedException)
                        {
                            throw new ConflictException("Source access was relinquished to another operation.");
                        }
                        int readError = Marshal.GetLastWin32Error();
                        if (!immediate && readError == ErrorIoPending)
                        {
                            session.ReadPending = true;
                            WaitHandle.WaitAny(new WaitHandle[] { session.OplockEvent, session.ReadEvent });
                            if (session.Broken || session.OplockEvent.WaitOne(0))
                                throw new ConflictException("Source changed while reading; this copy was discarded.");
                            if (!GetOverlappedResult(session.File, session.ReadOverlapped, out received, false))
                                throw new Win32Exception(Marshal.GetLastWin32Error(), "Source read failed.");
                            session.ReadPending = false;
                        }
                        else if (!immediate)
                        {
                            if (readError == ErrorHandleEof) received = 0;
                            else throw new Win32Exception(readError, "Source read failed.");
                        }
                        else
                        {
                            session.CheckUnbroken();
                            if (!GetOverlappedResult(session.File, session.ReadOverlapped, out received, false))
                                throw new Win32Exception(Marshal.GetLastWin32Error(), "Source read failed.");
                        }
                        session.CheckUnbroken();
                    }
                    else if (!ReadFile(session.File, nativeBuffer, wanted, out received, IntPtr.Zero))
                    {
                        throw new Win32Exception(Marshal.GetLastWin32Error(), "Snapshot read failed.");
                    }
                    if (received == 0) throw new ConflictException("Source ended before its initial file size.");
                    Marshal.Copy(nativeBuffer, managedBuffer, 0, (int)received);
                    output.Write(managedBuffer, 0, (int)received);
                    result.BytesCopied += received;
                }
                if (guarded) session.CheckUnbroken();
                FileInformation final;
                if (!GetFileInformationByHandle(session.File, out final))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot recheck source metadata.");
                if (initial.SizeHigh != final.SizeHigh || initial.SizeLow != final.SizeLow ||
                    initial.LastWriteTime.High != final.LastWriteTime.High || initial.LastWriteTime.Low != final.LastWriteTime.Low)
                    throw new ConflictException("Source metadata changed during copying.");
                if (guarded) session.CheckUnbroken();
                // The source interval ends here. Later source changes do not invalidate bytes already read.
                session.CloseSource();
                if (session.Broken) throw new ConflictException("Source access conflicted during copying.");
                output.Flush();
                output.Dispose();
                output = null;
                File.SetLastWriteTimeUtc(temporary, result.SourceLastWriteTimeUtc);
                File.Move(temporary, target); // Never overwrite any prior completed backup.
                temporary = null;
                result.Status = "Copied";
            }
            catch (ConflictException ex)
            {
                result.Status = "Skipped";
                result.Message = ex.Message;
            }
            catch (ObjectDisposedException ex)
            {
                result.Status = session != null && session.Broken ? "Skipped" : "Failed";
                result.Message = ex.Message;
            }
            catch (Win32Exception ex)
            {
                int code = ex.NativeErrorCode;
                bool conflict = code == 2 || code == 3 || code == 32 || code == 33 ||
                    code == 300 || code == 301 || code == 801 || code == 802 || code == 803 || code == 995;
                result.Status = guarded && (conflict || (session != null && session.Broken)) ? "Skipped" : "Failed";
                result.Message = ex.Message + " (Win32 " + code + ")";
            }
            catch (Exception ex)
            {
                result.Status = guarded && session != null && session.Broken ? "Skipped" : "Failed";
                result.Message = ex.Message;
            }
            finally
            {
                // Keep the native read buffer alive until SourceSession has drained pending I/O.
                if (session != null) session.Dispose();
                if (nativeBuffer != IntPtr.Zero) Marshal.FreeHGlobal(nativeBuffer);
                if (output != null)
                {
                    try { output.Dispose(); }
                    catch (Exception ex) { result.Message += " Target cleanup: " + ex.Message; }
                }
                if (temporary != null)
                {
                    try { File.Delete(temporary); }
                    catch (Exception ex) { result.Message += " Temporary file cleanup: " + ex.Message; }
                }
            }
            return result;
        }
    }
}
'@

$script:PathCode = @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using System.Collections.Generic;
using System.IO;
using Microsoft.Win32.SafeHandles;

namespace FolderBackup
{
    public sealed class ResolvedDirectory
    {
        public string CanonicalPath;
        public string VolumeName;
        public string RelativePath;
        public bool IsDirectory;
        public bool IsReparsePoint;
    }

    public static class NativePaths
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct FileInfo
        {
            public uint Attributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME Creation;
            public System.Runtime.InteropServices.ComTypes.FILETIME Access;
            public System.Runtime.InteropServices.ComTypes.FILETIME Write;
            public uint VolumeSerial;
            public uint SizeHigh;
            public uint SizeLow;
            public uint Links;
            public uint IndexHigh;
            public uint IndexLow;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
        private static extern SafeFileHandle CreateFileW(string path, uint access,
            uint share, IntPtr security, uint disposition, uint flags, IntPtr template);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
        private static extern uint GetFinalPathNameByHandleW(SafeFileHandle file,
            StringBuilder path, uint capacity, uint flags);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(SafeFileHandle file,
            out FileInfo info);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreateDirectoryW(string path, IntPtr security);

        public static bool TryCreateNewDirectory(string path)
        {
            string extended = path;
            if (!extended.StartsWith(@"\\?\", StringComparison.Ordinal))
            {
                string full = Path.GetFullPath(path);
                extended = full.StartsWith(@"\\", StringComparison.Ordinal)
                    ? @"\\?\UNC\" + full.Substring(2) : @"\\?\" + full;
            }
            if (CreateDirectoryW(extended, IntPtr.Zero)) return true;
            int error = Marshal.GetLastWin32Error();
            if (error == 183 || error == 80) return false;
            throw new Win32Exception(error, "Cannot allocate backup directory: " + path);
        }

        public static string PredictDirectoryPath(string path)
        {
            // Resolve the nearest existing ancestor before creating anything.
            // This also catches destinations reached through junctions/symlinks.
            Stack<string> missing = new Stack<string>();
            string candidate = path;
            while (true)
            {
                using (SafeFileHandle file = CreateFileW(candidate, 0, 7,
                    IntPtr.Zero, 3, 0x02000000, IntPtr.Zero))
                {
                    if (!file.IsInvalid)
                    {
                        string result = ResolveDirectory(candidate).CanonicalPath;
                        while (missing.Count > 0)
                            result = result.TrimEnd('\\') + "\\" + missing.Pop();
                        return result;
                    }
                    int error = Marshal.GetLastWin32Error();
                    if (error != 2 && error != 3)
                        throw new Win32Exception(error,
                            "Cannot resolve destination ancestor: " + candidate);
                }
                string trimmed = candidate.TrimEnd('\\');
                string parent = Path.GetDirectoryName(trimmed);
                string leaf = Path.GetFileName(trimmed);
                if (String.IsNullOrEmpty(parent) || String.IsNullOrEmpty(leaf))
                    throw new DirectoryNotFoundException(
                        "No existing destination ancestor: " + path);
                missing.Push(leaf);
                candidate = parent;
            }
        }

        public static bool IsReparseDirectory(string path)
        {
            // Recheck queued directories: their attributes can change after the
            // parent was enumerated. OPEN_REPARSE_POINT inspects the link itself.
            using (SafeFileHandle file = CreateFileW(path, 0, 7, IntPtr.Zero,
                3, 0x02200000, IntPtr.Zero))
            {
                if (file.IsInvalid)
                    throw new Win32Exception(Marshal.GetLastWin32Error(),
                        "Cannot inspect directory: " + path);
                FileInfo info;
                if (!GetFileInformationByHandle(file, out info))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                if ((info.Attributes & 0x10) == 0)
                    throw new IOException("Directory is no longer a directory: " + path);
                return (info.Attributes & 0x400) != 0;
            }
        }

        public static ResolvedDirectory ResolveDirectory(string path)
        {
            return ResolvePath(path, true);
        }

        public static ResolvedDirectory ResolveFile(string path)
        {
            return ResolvePath(path, false);
        }

        private static ResolvedDirectory ResolvePath(string path, bool requireDirectory)
        {
            // Metadata-only access: no data-read/write access or restrictive sharing.
            // Directory roots keep the existing behavior of resolving a selected
            // junction. File roots inspect the final component without following
            // a reparse point; the copier intentionally does not clone such links.
            uint flags = requireDirectory ? 0x02000000u : 0x02200000u;
            using (SafeFileHandle file = CreateFileW(path, 0, 7, IntPtr.Zero,
                3, flags, IntPtr.Zero))
            {
                if (file.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error(),
                    "Cannot resolve source path: " + path);
                FileInfo info;
                if (!GetFileInformationByHandle(file, out info))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                bool isDirectory = (info.Attributes & 0x10) != 0;
                bool isReparsePoint = (info.Attributes & 0x400) != 0;
                if (requireDirectory && !isDirectory)
                    throw new ArgumentException("Source must be a directory: " + path);
                if (!requireDirectory && isDirectory)
                    throw new ArgumentException("Source must be a file: " + path);
                if (!requireDirectory && isReparsePoint)
                    throw new ArgumentException("Reparse-point file sources are not supported: " + path);
                StringBuilder buffer = new StringBuilder(512);
                uint length = GetFinalPathNameByHandleW(file, buffer, (uint)buffer.Capacity, 1);
                if (length == 0) throw new Win32Exception(Marshal.GetLastWin32Error(),
                    "Only local Windows volumes are supported: " + path);
                if (length >= buffer.Capacity)
                {
                    buffer = new StringBuilder(checked((int)length + 1));
                    length = GetFinalPathNameByHandleW(file, buffer, (uint)buffer.Capacity, 1);
                    if (length == 0 || length >= buffer.Capacity)
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                string canonical = buffer.ToString();
                int end = canonical.IndexOf("}\\", StringComparison.Ordinal);
                if (!canonical.StartsWith(@"\\?\Volume{", StringComparison.OrdinalIgnoreCase) || end < 0)
                    throw new ArgumentException("Only local Windows volumes are supported: " + path);
                string volume = canonical.Substring(0, end + 2);
                string relative = canonical.Substring(end + 2).TrimEnd('\\');
                return new ResolvedDirectory {
                    CanonicalPath = volume + relative,
                    VolumeName = volume,
                    RelativePath = relative,
                    IsDirectory = isDirectory,
                    IsReparsePoint = isReparsePoint
                };
            }
        }
    }
}
'@

function Initialize-BackupRuntime {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw '此脚本仅支持 Windows。'
    }
    if ([Environment]::OSVersion.Version -lt [version]'6.2') {
        throw 'Cooperative 模式需要 Windows 8 / Server 2012 或更新版本。'
    }
    if (-not ('FolderBackup.NativeCopy' -as [type])) {
        Add-Type -TypeDefinition $script:NativeCode -Language CSharp -ErrorAction Stop
    }
    if (-not ('FolderBackup.NativePaths' -as [type])) {
        Add-Type -TypeDefinition $script:PathCode -Language CSharp -ErrorAction Stop
    }
    elseif ($null -eq ('FolderBackup.NativePaths' -as [type]).GetMethod('ResolveFile') -or
        $null -eq ('FolderBackup.NativePaths' -as [type]).GetMethod('TryCreateNewDirectory')) {
        throw '当前 PowerShell 已加载旧版备份组件，请重新打开 PowerShell 后运行新脚本。'
    }
}

function ConvertTo-BackupFullPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { throw '目录路径不能为空。' }
    $expanded = [Environment]::ExpandEnvironmentVariables($Path)
    return [IO.Path]::GetFullPath($expanded)
}

function Test-BackupPathOverlap {
    param([string]$First, [string]$Second)
    $firstRoot = $First.TrimEnd('\') + '\'
    $secondRoot = $Second.TrimEnd('\') + '\'
    return $firstRoot.StartsWith($secondRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $secondRoot.StartsWith($firstRoot, [StringComparison]::OrdinalIgnoreCase)
}

function Get-BackupTaskValue {
    param($Task, [string]$Name)
    if ($null -eq $Task) { return $null }
    if ($Task -is [Collections.IDictionary]) { return $Task[$Name] }
    $property = $Task.PSObject.Properties[$Name]
    if ($null -ne $property) { return $property.Value }
    return $null
}

function Assert-BackupConfiguration {
    [CmdletBinding()]
    param(
        [object[]]$Tasks,
        [string]$Destination,
        [string]$CopyMode = 'Cooperative',
        [int]$ReadBufferKiB = 256,
        [int]$WaitSeconds = 300,
        [string]$Version = $ScriptVersion
    )
    if ([string]::IsNullOrWhiteSpace($Version)) { throw '版本号不能为空。' }
    if ([string]::IsNullOrWhiteSpace($Destination)) { throw '目标目录不能为空。' }
    if ($CopyMode -notin @('Cooperative', 'Vss')) { throw 'BackupMode 必须是 Cooperative 或 Vss。' }
    if ($WaitSeconds -lt 1) { throw 'IntervalSeconds 必须大于或等于 1。' }
    if ($ReadBufferKiB -lt 4 -or $ReadBufferKiB -gt 4096) { throw 'BufferKiB 必须在 4..4096 范围内。' }
    if ($null -eq $Tasks -or $Tasks.Count -eq 0) { throw '至少配置一个备份任务。' }
    $ids = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($task in $Tasks) {
        $id = [string](Get-BackupTaskValue $task 'id')
        $kind = [string](Get-BackupTaskValue $task 'mode')
        $path = [string](Get-BackupTaskValue $task 'path')
        if ($id -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$') {
            throw ('任务 id 无效：{0}。请使用 1..80 个字母、数字、下划线或连字符，并以字母或数字开头。' -f $id)
        }
        if (-not $ids.Add($id)) { throw ('任务 id 重复（不区分大小写）：{0}' -f $id) }
        if ($kind -notin @('folder', 'file')) { throw ('任务 {0} 的 mode 必须是 folder 或 file。' -f $id) }
        if ([string]::IsNullOrWhiteSpace($path)) { throw ('任务 {0} 的 path 不能为空。' -f $id) }
    }
}

function Write-BackupStartup {
    param(
        [object[]]$Tasks,
        [string]$Destination,
        [string]$CopyMode,
        [int]$WaitSeconds,
        [string]$Version = $ScriptVersion
    )
    Write-Host ('Backup v{0}' -f $Version)
    Write-Host ('复制方式：{0}；目标：{1}；每轮结束后等待：{2} 秒' -f $CopyMode, $Destination, $WaitSeconds)
    Write-Host ('任务列表（{0}）：' -f $Tasks.Count)
    foreach ($task in $Tasks) {
        Write-Host ('  id = {0}, mode = {1}, path = {2}' -f `
            (Get-BackupTaskValue $task 'id'), (Get-BackupTaskValue $task 'mode'), (Get-BackupTaskValue $task 'path'))
    }
    Write-Host '---'
}

function Test-BackupAdministrator {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw '此脚本仅支持 Windows。'
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    }
    finally { $identity.Dispose() }
}

function Get-BackupPowerShellExecutable {
    # Use the engine belonging to this installation, including when called from
    # ISE. Do not search PATH or relaunch the ISE host executable itself.
    foreach ($name in @('pwsh.exe', 'powershell.exe')) {
        $path = [IO.Path]::Combine($PSHOME, $name)
        if ([IO.File]::Exists($path)) { return $path }
    }
    throw '无法定位当前 PowerShell 安装中的可执行文件。'
}

function New-BackupElevationCommand {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )
    # Encode PowerShell literals, not shell fragments. UTF-16LE is required by
    # both Windows PowerShell and pwsh for -EncodedCommand.
    $scriptLiteral = "'" + $ScriptPath.Replace("'", "''") + "'"
    $directoryLiteral = "'" + $WorkingDirectory.Replace("'", "''") + "'"
    $bootstrap = @(
        '$backupOldMarker = [Environment]::GetEnvironmentVariable(''AUTO_BACKUP_UAC_RELAUNCH'', ''Process'')'
        ('[Environment]::SetEnvironmentVariable(''AUTO_BACKUP_UAC_RELAUNCH'', {0}, ''Process'')' -f $scriptLiteral)
        'try {'
        ('    Set-Location -LiteralPath {0} -ErrorAction Stop' -f $directoryLiteral)
        ('    [Environment]::CurrentDirectory = {0}' -f $directoryLiteral)
        ('    & {0}' -f $scriptLiteral)
        '}'
        'catch { Write-Error -Message $_.Exception.Message -ErrorAction Continue }'
        'finally {'
        '    [Environment]::SetEnvironmentVariable(''AUTO_BACKUP_UAC_RELAUNCH'', $backupOldMarker, ''Process'')'
        '}'
    ) -join [Environment]::NewLine
    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($bootstrap))
}

function Request-BackupElevation {
    <# True: execute here. False: an elevated child was started; this parent stops. #>
    [CmdletBinding()]
    param(
        [ValidateSet('Cooperative', 'Vss')][string]$CopyMode,
        [string]$ScriptPath = $script:BackupEntryPath,
        [string]$WorkingDirectory = ''
    )
    if ($CopyMode -ne 'Vss') { return $true }
    if (Test-BackupAdministrator) { return $true }
    if ([string]::IsNullOrWhiteSpace($ScriptPath)) { throw '自动提权需要脚本文件的完整路径。' }
    $ScriptPath = [IO.Path]::GetFullPath($ScriptPath)
    if (-not [IO.File]::Exists($ScriptPath)) { throw ('无法重新启动脚本：{0}' -f $ScriptPath) }
    if ([Environment]::GetEnvironmentVariable('AUTO_BACKUP_UAC_RELAUNCH', 'Process') -eq $ScriptPath) {
        throw '已请求管理员授权，但新进程仍没有管理员权限，已停止以避免重复请求 UAC。请手动以管理员身份启动脚本。'
    }
    if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        $location = Get-Location
        if ($location.Provider.Name -eq 'FileSystem') { $WorkingDirectory = $location.ProviderPath }
        else { $WorkingDirectory = [IO.Path]::GetDirectoryName($ScriptPath) }
    }
    try {
        $engine = Get-BackupPowerShellExecutable
        $encoded = New-BackupElevationCommand -ScriptPath $ScriptPath -WorkingDirectory $WorkingDirectory
        $arguments = '-NoProfile -NoExit'
        # Preserve only an explicitly selected Process policy. Never persist a
        # policy change or unconditionally bypass the user's execution policy.
        $policy = [string](Get-ExecutionPolicy -Scope Process -ErrorAction Stop)
        if ($policy -ne 'Undefined') {
            if ($policy -notin @('Restricted', 'AllSigned', 'RemoteSigned', 'Unrestricted', 'Bypass', 'Default')) {
                throw ('无法识别当前进程的执行策略：{0}' -f $policy)
            }
            $arguments += ' -ExecutionPolicy ' + $policy
        }
        $arguments += ' -EncodedCommand ' + $encoded
        $null = Start-Process -FilePath $engine -Verb RunAs -ArgumentList $arguments -WorkingDirectory $WorkingDirectory -ErrorAction Stop
    }
    catch {
        $exception = $_.Exception
        while ($null -ne $exception) {
            if ($exception -is [ComponentModel.Win32Exception] -and $exception.NativeErrorCode -eq 1223) {
                throw '已取消管理员授权，备份未启动。'
            }
            $exception = $exception.InnerException
        }
        throw ('请求管理员权限失败，备份未启动：{0}' -f $_.Exception.Message)
    }
    return $false
}

function Assert-BackupVssAccess {
    if (-not (Test-BackupAdministrator)) {
        throw 'Vss 模式需要管理员权限。请直接运行 .\auto_backup.ps1 以请求 UAC，或在管理员 PowerShell 中调用备份函数。'
    }
}

function Get-BackupTaskSource {
    param($Task)
    $path = ConvertTo-BackupFullPath ([string](Get-BackupTaskValue $Task 'path'))
    $kind = ([string](Get-BackupTaskValue $Task 'mode')).ToLowerInvariant()
    if ($kind -eq 'file') { $resolved = [FolderBackup.NativePaths]::ResolveFile($path) }
    else { $resolved = [FolderBackup.NativePaths]::ResolveDirectory($path) }
    $name = [IO.Path]::GetFileName($path.TrimEnd('\'))
    if ([string]::IsNullOrWhiteSpace($name) -or $name.EndsWith(':')) { $name = 'Volume' }
    foreach ($invalid in [IO.Path]::GetInvalidFileNameChars()) {
        if ($name.IndexOf($invalid) -ge 0) { throw '源必须是普通文件或文件夹路径，不支持备用数据流等特殊名称。' }
    }
    return [pscustomobject]@{ Path = $path; Kind = $kind; Name = $name; Resolved = $resolved }
}

function Assert-BackupSourceSeparation {
    param($Source, [string]$DestinationCanonical)
    # A file cannot contain a target directory; the same boundary comparison also
    # rejects a file selected from inside our own backup destination.
    if (Test-BackupPathOverlap $Source.Resolved.CanonicalPath $DestinationCanonical) {
        throw ('源和备份目标路径重叠：{0}' -f $Source.Path)
    }
}

function Assert-BackupDestinationSafety {
    param([object[]]$Tasks, [string]$Destination)
    $destinationPath = ConvertTo-BackupFullPath $Destination
    $predicted = [FolderBackup.NativePaths]::PredictDirectoryPath($destinationPath)
    # Check ALL valid tasks before writing anything. A file task must not create
    # backups inside another task's source tree just because it is listed first.
    foreach ($task in $Tasks) {
        try { $fullPath = ConvertTo-BackupFullPath ([string](Get-BackupTaskValue $task 'path')) }
        catch { continue } # Invalid paths receive per-task error results.
        if (Test-BackupPathOverlap $fullPath $destinationPath) {
            throw ('源和备份目标路径重叠：{0}' -f $fullPath)
        }
        try { $source = Get-BackupTaskSource $task }
        catch {
            # A missing folder can still contain the configured destination.
            # Resolve its closest existing ancestor, including any junction, so
            # an earlier task cannot create files inside this later source.
            try {
                $source = [pscustomobject]@{
                    Path = $fullPath
                    Resolved = [pscustomobject]@{ CanonicalPath = [FolderBackup.NativePaths]::PredictDirectoryPath($fullPath) }
                }
            }
            catch { continue }
        }
        Assert-BackupSourceSeparation $source $predicted
    }
}

function New-BackupTaskDirectory {
    [CmdletBinding()]
    param([string]$Destination, [string]$Id)
    if ($Id -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$') { throw '无效的任务 id。' }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $epoch = New-Object DateTime(1970, 1, 1, 0, 0, 0, [DateTimeKind]::Utc)
    do {
        $timestamp = [long][Math]::Floor(([DateTime]::UtcNow - $epoch).TotalSeconds)
        $path = [IO.Path]::Combine($Destination, ('{0}_{1}' -f $Id, $timestamp))
        if ([FolderBackup.NativePaths]::TryCreateNewDirectory($path)) { return $path }
        # Preserve the requested id_UnixSeconds shape while handling same-second
        # invocations and concurrent instances without overwriting their outputs.
        Start-Sleep -Milliseconds 100
    } while ($watch.Elapsed.TotalSeconds -lt 30)
    throw ('30 秒内无法为任务分配新的时间戳目录：{0}' -f $Id)
}

function Write-BackupEvent {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Kind,
        [string]$Source = '',
        [string]$Target = '',
        [string]$Message = ''
    )
    $event = [ordered]@{
        TimeUtc = [DateTime]::UtcNow.ToString('o')
        Kind = $Kind
        Source = $Source
        Target = $Target
        Message = $Message
    }
    $Context.Writer.WriteLine(($event | ConvertTo-Json -Compress -Depth 5))
    $Context.Writer.Flush()
}

function New-BackupShadow {
    param([Parameter(Mandatory = $true)][string]$VolumeName, $Context)
    $result = Invoke-CimMethod -Namespace root/cimv2 -ClassName Win32_ShadowCopy `
        -MethodName Create -Arguments @{ Volume = $VolumeName; Context = 'ClientAccessible' } `
        -ErrorAction Stop
    if ([uint32]$result.ReturnValue -ne 0) {
        throw ('VSS 创建失败，返回码 {0}，卷 {1}。' -f $result.ReturnValue, $VolumeName)
    }
    $id = [string]$result.ShadowID
    if ([string]::IsNullOrEmpty($id)) { throw 'VSS 未返回快照 ID。' }
    # Register before logging/querying, so a later exception still reaches cleanup.
    $record = [pscustomobject]@{ Id = $id; Volume = $VolumeName; Device = $null; Deleted = $false }
    $Context.CreatedShadows.Add($record)
    Write-BackupEvent $Context 'SnapshotCreated' $VolumeName '' $id
    $shadow = Get-CimInstance -Namespace root/cimv2 -ClassName Win32_ShadowCopy `
        -Filter ("ID='{0}'" -f $id) -ErrorAction Stop
    if ($null -eq $shadow -or [string]::IsNullOrEmpty([string]$shadow.DeviceObject)) {
        throw ('找不到刚创建的 VSS 快照：{0}' -f $id)
    }
    $record.Device = [string]$shadow.DeviceObject
    return $record
}

function Remove-BackupShadows {
    param($Context)
    foreach ($record in $Context.CreatedShadows) {
        try {
            $shadow = Get-CimInstance -Namespace root/cimv2 -ClassName Win32_ShadowCopy `
                -Filter ("ID='{0}'" -f $record.Id) -ErrorAction Stop
            if ($null -ne $shadow) {
                Remove-CimInstance -InputObject $shadow -ErrorAction Stop
            }
            $record.Deleted = $true
            try { Write-BackupEvent $Context 'SnapshotDeleted' $record.Volume '' $record.Id }
            catch {
                $Context.Stats.Failed++
                if ($null -ne $Context.PSObject.Properties['Diagnostics']) {
                    $Context.Diagnostics.Add(('快照已清理但日志写入失败：{0}' -f $_.Exception.Message))
                }
            }
        }
        catch {
            $Context.Stats.Failed++
            $cleanupError = $_.Exception.Message
            if ($null -ne $Context.PSObject.Properties['Diagnostics']) {
                $Context.Diagnostics.Add(('快照清理失败，请保留 ID {0}：{1}' -f $record.Id, $cleanupError))
            }
            try {
                Write-BackupEvent $Context 'SnapshotCleanupFailed' $record.Volume '' `
                    ('{0}: {1}' -f $record.Id, $cleanupError)
            }
            catch {
                if ($null -ne $Context.PSObject.Properties['Diagnostics']) {
                    $Context.Diagnostics.Add(('快照清理错误无法写入日志：{0}' -f $_.Exception.Message))
                }
            }
        }
    }
}

function Copy-BackupFile {
    <# 单文件复制入口，文件任务与目录遍历共用；VSS 的 ReadPath 由任务层提供。 #>
    param(
        [Parameter(Mandatory = $true)][string]$ReadPath,
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [Parameter(Mandatory = $true)][string]$DisplayPath,
        [Parameter(Mandatory = $true)]$Context
    )
    try {
        if ($Context.Mode -eq 'Vss') {
            $copy = [FolderBackup.NativeCopy]::CopySnapshotFile($ReadPath, $TargetPath, $Context.BufferBytes)
        }
        else {
            $copy = [FolderBackup.NativeCopy]::CopyFile($ReadPath, $TargetPath, $Context.BufferBytes)
        }
        switch ($copy.Status) {
            'Copied' {
                $Context.Stats.Copied++
                $Context.Stats.BytesCopied += [long]$copy.BytesCopied
            }
            'Skipped' { $Context.Stats.Skipped++ }
            default { $Context.Stats.Failed++ }
        }
        Write-BackupEvent $Context ([string]$copy.Status) $DisplayPath $TargetPath ([string]$copy.Message)
    }
    catch {
        $Context.Stats.Failed++
        Write-BackupEvent $Context 'Failed' $DisplayPath $TargetPath $_.Exception.Message
    }
}

function Copy-BackupTree {
    param(
        [Parameter(Mandatory = $true)][string]$ReadRoot,
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)][string]$DisplayRoot,
        [Parameter(Mandatory = $true)]$Context
    )
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push('')
    while ($pending.Count -gt 0) {
        $relative = $pending.Pop()
        $readDirectory = $ReadRoot.TrimEnd('\')
        $targetDirectory = $TargetRoot
        $displayDirectory = $DisplayRoot
        if ($relative.Length -gt 0) {
            $readDirectory += '\' + $relative
            $targetDirectory = [IO.Path]::Combine($TargetRoot, $relative)
            $displayDirectory = [IO.Path]::Combine($DisplayRoot, $relative)
        }
        else {
            # A volume root must keep its trailing separator (\\?\Volume{...}\).
            $readDirectory = $ReadRoot
        }
        try {
            if ([FolderBackup.NativePaths]::IsReparseDirectory($readDirectory)) {
                $Context.Stats.Skipped++
                Write-BackupEvent $Context 'Skipped' $displayDirectory $targetDirectory '目录已变为重解析点，未跟随。'
                continue
            }
            [void][IO.Directory]::CreateDirectory($targetDirectory)
            $entries = [FolderBackup.NativeCopy]::EnumerateDirectory($readDirectory)
            $Context.Stats.Directories++
        }
        catch {
            $Context.Stats.Failed++
            Write-BackupEvent $Context 'DirectoryFailed' $displayDirectory $targetDirectory $_.Exception.Message
            continue
        }
        foreach ($entry in $entries) {
            $childRelative = $entry.Name
            if ($relative.Length -gt 0) { $childRelative = $relative + '\' + $entry.Name }
            $readPath = $ReadRoot.TrimEnd('\') + '\' + $childRelative
            $targetPath = [IO.Path]::Combine($TargetRoot, $childRelative)
            $displayPath = [IO.Path]::Combine($DisplayRoot, $childRelative)
            if ($entry.IsReparsePoint) {
                $Context.Stats.Skipped++
                Write-BackupEvent $Context 'Skipped' $displayPath $targetPath '重解析点未跟随。'
                continue
            }
            if ($entry.IsDirectory) {
                $pending.Push($childRelative)
                continue
            }
            Copy-BackupFile $readPath $targetPath $displayPath $Context
        }
    }
}

function Invoke-BackupTask {
    <# 单个任务：分配目录、准备快照、复制文件或目录、清理并返回摘要；不输出控制台日志。 #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)][string]$Destination,
        [ValidateSet('Cooperative', 'Vss')][string]$CopyMode = 'Cooperative',
        [ValidateRange(4, 4096)][int]$ReadBufferKiB = 256
    )
    $summary = [ordered]@{
        Id = [string](Get-BackupTaskValue $Task 'id')
        TaskMode = [string](Get-BackupTaskValue $Task 'mode')
        Source = [string](Get-BackupTaskValue $Task 'path')
        Version = $ScriptVersion
        CopyMode = $CopyMode
        Status = 'Failed'
        StartedUtc = [DateTime]::UtcNow.ToString('o')
        FinishedUtc = $null
        DurationSeconds = 0.0
        BackupDirectory = $null
        MetadataDirectory = $null
        Counts = [ordered]@{ Copied = 0; Skipped = 0; Failed = 0; Directories = 0; BytesCopied = [long]0 }
        Snapshots = @()
        Diagnostics = @()
        Error = $null
        Notes = @(
            'Only primary file data streams, file last-write times, and directory structure are copied.'
            'Reparse points are skipped or rejected; ACLs, alternate streams, and hard-link relationships are not preserved.'
            'Cooperative mode does not guarantee application-level or whole-tree consistency or zero interference.'
            'Complete refers to entries enumerated in this task; new live entries may wait until a later run.'
            'VSS ClientAccessible does not coordinate application writers; each task creates its own volume snapshot.'
        )
    }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $context = $null
    $writer = $null
    try {
        Assert-BackupConfiguration -Tasks @($Task) -Destination $Destination -CopyMode $CopyMode -ReadBufferKiB $ReadBufferKiB
        Initialize-BackupRuntime
        Assert-BackupDestinationSafety -Tasks @($Task) -Destination $Destination
        if ($CopyMode -eq 'Vss') { Assert-BackupVssAccess }

        $destinationPath = ConvertTo-BackupFullPath $Destination
        $source = $null
        $sourceError = $null
        try { $source = Get-BackupTaskSource $Task }
        catch { $sourceError = $_.Exception.Message }
        $predicted = [FolderBackup.NativePaths]::PredictDirectoryPath($destinationPath)
        if ($null -ne $source) { Assert-BackupSourceSeparation $source $predicted }
        [void][IO.Directory]::CreateDirectory($destinationPath)
        $resolvedDestination = [FolderBackup.NativePaths]::ResolveDirectory($destinationPath)
        if ($null -ne $source) {
            Assert-BackupSourceSeparation $source $resolvedDestination.CanonicalPath
        }
        $taskDirectory = New-BackupTaskDirectory -Destination $destinationPath -Id $summary.Id
        $summary.BackupDirectory = $taskDirectory
        $metadataName = '.backup-info'
        if ($null -ne $source -and $source.Name -ieq $metadataName) { $metadataName = '.backup-info_' }
        $metadataDirectory = [IO.Path]::Combine($taskDirectory, $metadataName)
        [void][IO.Directory]::CreateDirectory($metadataDirectory)
        $summary.MetadataDirectory = $metadataDirectory
        $encoding = New-Object Text.UTF8Encoding($false)
        $writer = New-Object IO.StreamWriter([IO.Path]::Combine($metadataDirectory, 'events.jsonl'), $false, $encoding)
        $context = [pscustomobject]@{
            Mode = $CopyMode
            BufferBytes = $ReadBufferKiB * 1024
            Writer = $writer
            CreatedShadows = (New-Object 'System.Collections.Generic.List[object]')
            Diagnostics = (New-Object 'System.Collections.Generic.List[string]')
            Stats = $summary.Counts
        }
        Write-BackupEvent $context 'TaskStarted' $summary.Source $taskDirectory $summary.Id
        if ($null -ne $sourceError) {
            Write-BackupEvent $context 'SourceUnavailable' $summary.Source '' $sourceError
            throw $sourceError
        }
        $readPath = $source.Resolved.CanonicalPath
        if ($CopyMode -eq 'Vss') {
            $snapshot = New-BackupShadow $source.Resolved.VolumeName $context
            $readPath = $snapshot.Device.TrimEnd('\') + '\' + $source.Resolved.RelativePath
        }
        $targetPath = [IO.Path]::Combine($taskDirectory, $source.Name)
        if ($source.Kind -eq 'file') {
            Copy-BackupFile -ReadPath $readPath -TargetPath $targetPath -DisplayPath $source.Path -Context $context
        }
        else {
            Copy-BackupTree -ReadRoot $readPath -TargetRoot $targetPath -DisplayRoot $source.Path -Context $context
        }
    }
    catch {
        $summary.Error = $_.Exception.Message
        $summary.Counts.Failed++
        if ($null -ne $context) {
            try { Write-BackupEvent $context 'TaskFailed' $summary.Source $summary.BackupDirectory $summary.Error }
            catch { $context.Diagnostics.Add(('任务错误无法写入日志：{0}' -f $_.Exception.Message)) }
        }
    }
    finally {
        if ($null -ne $context) {
            try { Remove-BackupShadows $context }
            catch {
                $summary.Counts.Failed++
                $context.Diagnostics.Add(('快照清理异常：{0}' -f $_.Exception.Message))
            }
            $summary.Snapshots = @($context.CreatedShadows.ToArray())
            $summary.Diagnostics = @($context.Diagnostics.ToArray())
        }
        if ($null -ne $writer) {
            try { $writer.Dispose() }
            catch {
                $summary.Counts.Failed++
                $summary.Error = '关闭任务日志失败：' + $_.Exception.Message
            }
        }
        $summary.FinishedUtc = [DateTime]::UtcNow.ToString('o')
        $summary.DurationSeconds = [Math]::Round($watch.Elapsed.TotalSeconds, 3)
        $summary.Status = 'Complete'
        if (($summary.Counts.Skipped + $summary.Counts.Failed) -gt 0) { $summary.Status = 'Partial' }
        if ($null -ne $summary.Error) { $summary.Status = 'Failed' }
        if ($summary.Counts.Directories -eq 0 -and $summary.Counts.Copied -eq 0 -and $summary.Counts.Failed -gt 0) {
            $summary.Status = 'Failed'
        }
        if ($null -ne $summary.MetadataDirectory) {
            try {
                $encoding = New-Object Text.UTF8Encoding($false)
                $json = $summary | ConvertTo-Json -Depth 8
                [IO.File]::WriteAllText([IO.Path]::Combine($summary.MetadataDirectory, 'summary.json'), $json, $encoding)
            }
            catch {
                $summary.Counts.Failed++
                $summary.Status = 'Failed'
                $summary.Error = '保存摘要失败：' + $_.Exception.Message
            }
        }
    }
    return [pscustomobject]$summary
}

function Write-BackupTaskLog {
    param($Result)
    $target = [string]$Result.BackupDirectory
    if ([string]::IsNullOrEmpty($target)) { $target = '-' }
    $line = '[{0}] {1} {2} 成功={3} 跳过={4} 失败={5} 耗时={6:F3}s 目标={7}' -f
        (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Result.Id, $Result.Status,
        $Result.Counts.Copied, $Result.Counts.Skipped, $Result.Counts.Failed,
        $Result.DurationSeconds, $target
    if (-not [string]::IsNullOrEmpty([string]$Result.Error)) {
        $line += ' 原因=' + [string]$Result.Error
    }
    elseif ($Result.Diagnostics.Count -gt 0) {
        $line += ' 提示=' + [string]$Result.Diagnostics[0]
    }
    Write-Host ($line -replace '[\r\n]+', ' ')
}

function Invoke-BackupCycle {
    <# 一轮顺序执行所有任务；每任务一条控制台日志，失败不会中断其他任务。 #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object[]]$Tasks,
        [Parameter(Mandatory = $true)][string]$Destination,
        [ValidateSet('Cooperative', 'Vss')][string]$CopyMode = 'Cooperative',
        [ValidateRange(4, 4096)][int]$ReadBufferKiB = 256
    )
    Assert-BackupConfiguration -Tasks $Tasks -Destination $Destination -CopyMode $CopyMode -ReadBufferKiB $ReadBufferKiB
    Initialize-BackupRuntime
    try { Assert-BackupDestinationSafety -Tasks $Tasks -Destination $Destination }
    catch {
        # A cycle-wide safety failure must not allow earlier tasks to create
        # backups inside a later task's source. Report once per task, then retry
        # the safety preflight on the next scheduled cycle.
        $failure = $_.Exception.Message
        foreach ($task in $Tasks) {
            $result = [pscustomobject]@{
                Id = [string](Get-BackupTaskValue $task 'id')
                TaskMode = [string](Get-BackupTaskValue $task 'mode')
                Source = [string](Get-BackupTaskValue $task 'path')
                Version = $ScriptVersion; CopyMode = $CopyMode; Status = 'Failed'
                BackupDirectory = $null; MetadataDirectory = $null; DurationSeconds = 0.0
                StartedUtc = [DateTime]::UtcNow.ToString('o'); FinishedUtc = [DateTime]::UtcNow.ToString('o')
                Counts = [ordered]@{ Copied = 0; Skipped = 0; Failed = 1; Directories = 0; BytesCopied = [long]0 }
                Snapshots = @(); Diagnostics = @(); Error = $failure
            }
            Write-BackupTaskLog $result
            Write-Output $result
        }
        return
    }
    foreach ($task in $Tasks) {
        $result = Invoke-BackupTask -Task $task -Destination $Destination -CopyMode $CopyMode -ReadBufferKiB $ReadBufferKiB
        Write-BackupTaskLog $result
        Write-Output $result
    }
}

function Start-BackupSchedule {
    [CmdletBinding()]
    param()
    Assert-BackupConfiguration -Tasks $BackupTasks -Destination $DestinationRoot -CopyMode $BackupMode -ReadBufferKiB $BufferKiB -WaitSeconds $IntervalSeconds -Version $ScriptVersion
    if (-not (Request-BackupElevation -CopyMode $BackupMode)) { return }
    Write-BackupStartup -Tasks $BackupTasks -Destination $DestinationRoot -CopyMode $BackupMode -WaitSeconds $IntervalSeconds -Version $ScriptVersion
    Initialize-BackupRuntime
    if ($BackupMode -eq 'Vss') { Assert-BackupVssAccess }
    do {
        $null = Invoke-BackupCycle -Tasks $BackupTasks -Destination $DestinationRoot -CopyMode $BackupMode -ReadBufferKiB $BufferKiB
        if ($RunOnce) { break }
        Start-Sleep -Seconds $IntervalSeconds
    } while ($true)
}

# Dot-sourcing exposes the functions without starting scheduled work.
if ($MyInvocation.InvocationName -ne '.') {
    Start-BackupSchedule
}
