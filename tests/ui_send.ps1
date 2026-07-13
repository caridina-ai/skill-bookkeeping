param(
    [Parameter(Mandatory = $true)]
    [long]$WindowHandle,

    [Parameter(Mandatory = $true)]
    [string]$Text
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName UIAutomationClient
Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class BookkeepingWindow {
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
}
"@

$handle = [IntPtr]$WindowHandle
$processId = [uint32]0
[void][BookkeepingWindow]::GetWindowThreadProcessId($handle, [ref]$processId)
$process = Get-Process -Id $processId -ErrorAction Stop
if ($process.ProcessName -ne "WindowsTerminal") {
    throw "Target handle belongs to $($process.ProcessName), not WindowsTerminal"
}

$root = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
if ($null -eq $root -or [string]::IsNullOrWhiteSpace($root.Current.Name)) {
    throw "Unexpected target window: $($root.Current.Name)"
}
$terminal = $root.FindFirst(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ClassNameProperty,
        "TermControl"
    )
)
if ($null -eq $terminal -or -not $terminal.Current.IsKeyboardFocusable) {
    throw "Could not find a focusable WindowsTerminal control"
}

[void][BookkeepingWindow]::ShowWindow($handle, 9)
$focused = $false
for ($attempt = 0; $attempt -lt 6; $attempt++) {
    [void][BookkeepingWindow]::SetForegroundWindow($handle)
    $terminal.SetFocus()
    Start-Sleep -Milliseconds 350
    if ([BookkeepingWindow]::GetForegroundWindow() -eq $handle) {
        $focused = $true
        break
    }
}
if (-not $focused) {
    throw "WindowsTerminal did not become the foreground window"
}

[System.Windows.Forms.Clipboard]::SetText($Text)
[System.Windows.Forms.SendKeys]::SendWait("^+v")
Start-Sleep -Milliseconds 150
if ([BookkeepingWindow]::GetForegroundWindow() -ne $handle) {
    throw "Foreground window changed before Enter"
}
[System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
