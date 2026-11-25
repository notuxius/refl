$ErrorActionPreference = 'Stop'
$exe = $env:RELF_UPDATER_EXE
$stage = $env:RELF_UPDATER_STAGE
$staged = [IO.Path]::Combine($stage, 'Relf.exe')
$backup = [IO.Path]::Combine($stage, 'previous.exe')
$started = [IO.Path]::Combine($stage, 'started')
$armed = $false
$closed = $false
$installed = $false
$locked = $false

function Start-Relf($readyFile, $failure) {
    $info = [Diagnostics.ProcessStartInfo]::new($exe)
    $info.Arguments = $env:RELF_UPDATER_ARGS
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.WorkingDirectory = [Environment]::CurrentDirectory
    foreach ($key in @($info.EnvironmentVariables.Keys)) {
        if ($key.StartsWith('RELF_UPDATER_')) {
            $info.EnvironmentVariables.Remove($key)
        }
    }
    $info.EnvironmentVariables['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    if ($readyFile) { $info.EnvironmentVariables['RELF_UPDATE_READY'] = $readyFile }
    if ($failure) { $info.EnvironmentVariables['RELF_UPDATE_FAILURE'] = $failure }
    return [Diagnostics.Process]::Start($info)
}

function Show-UpdateError($detail) {
    Add-Type -AssemblyName System.Windows.Forms
    $null = [Windows.Forms.MessageBox]::Show($detail, 'Relf update failed')
}

try {
    $mutex = [Threading.Mutex]::new($false, $env:RELF_UPDATER_LOCK)
    try {
        $locked = $mutex.WaitOne(0)
    } catch [Threading.AbandonedMutexException] {
        $locked = $true
    }
    if (!$locked) { throw 'Another Relf update is already running.' }
    $running = Get-Process -Id ($env:RELF_UPDATER_PIDS -split ',' | ForEach-Object { [int]$_ })
    foreach ($process in $running) { $null = $process.Handle }
    [IO.File]::WriteAllText([IO.Path]::Combine($stage, 'armed'), '')
    $armed = $true
    foreach ($process in $running) {
        if (!$process.WaitForExit(30000)) { throw 'Relf is still running. Close it and try again.' }
    }
    $name = [WildcardPattern]::Escape([IO.Path]::GetFileNameWithoutExtension($exe))
    if (Get-Process -Name $name -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe }) {
        throw 'Another Relf window is still open. Close it and try again.'
    }
    $closed = $true
    [IO.File]::Replace($staged, $exe, $backup)
    $next = Start-Relf $started $null
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while (![IO.File]::Exists($started)) {
        if ($next.WaitForExit(100) -and ![IO.File]::Exists($started)) {
            throw 'The updated Relf window could not open.'
        }
        if ($timer.Elapsed.TotalSeconds -gt 45 -and ![IO.File]::Exists($started)) {
            Start-Process -FilePath "$env:SystemRoot\System32\taskkill.exe" -ArgumentList "/PID $($next.Id) /T /F" -WindowStyle Hidden -Wait
            $null = $next.WaitForExit(5000)
            throw 'The updated Relf window did not open in time.'
        }
    }
    $installed = $true
} catch {
    $detail = $_.Exception.Message
    if (!$armed) {
        [IO.File]::WriteAllText([IO.Path]::Combine($stage, 'error'), $detail)
        exit 1
    }
    try {
        if ([IO.File]::Exists($backup)) {
            if ([IO.File]::Exists($exe)) {
                [IO.File]::Replace($backup, $exe, [NullString]::Value)
            } else {
                [IO.File]::Move($backup, $exe)
            }
        }
        if ($closed) {
            $null = Start-Relf $null $detail
        } else {
            Show-UpdateError $detail
        }
    } catch {
        $restoreError = $_.Exception.Message
        if ([IO.File]::Exists($backup)) {
            $restoreError += " The previous executable is saved at $backup"
        }
        Show-UpdateError "$detail $restoreError"
    }
    exit 1
} finally {
    if ($armed) {
        try {
            if ($installed) { [IO.File]::Delete($backup) }
            [IO.File]::Delete($staged)
            [IO.File]::Delete($started)
            [IO.File]::Delete([IO.Path]::Combine($stage, 'armed'))
            if (![IO.File]::Exists($backup)) { [IO.Directory]::Delete($stage) }
        } catch {
            # A cleanup failure must not interrupt the relaunched app.
        }
    }
    if ($locked) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
