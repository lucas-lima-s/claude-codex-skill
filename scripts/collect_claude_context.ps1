#Requires -Version 5.1
<#
.SYNOPSIS
  Thin PowerShell shim around collect_claude_context.py.

.DESCRIPTION
  Resolves a Python interpreter (via $env:CLAUDE_AUTOMATION_PYTHON or PATH)
  and delegates to collect_claude_context.py. Emits the collector's JSON or
  text output verbatim on stdout. Never logs to stdout itself.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$Cwd,
    [string]$TargetPath,
    [string]$RepoRoot,
    [string]$GlobalClaudeMd,
    [ValidateSet("json", "text")] [string]$Format = "json"
)

$ErrorActionPreference = "Stop"

function Resolve-PythonExe {
    if ($env:CLAUDE_AUTOMATION_PYTHON) {
        if (Test-Path -LiteralPath $env:CLAUDE_AUTOMATION_PYTHON -PathType Leaf) {
            return $env:CLAUDE_AUTOMATION_PYTHON
        }
    }
    $cmd = Get-Command -Name python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $cmd = Get-Command -Name python3 -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "No Python interpreter found on PATH and CLAUDE_AUTOMATION_PYTHON is not set."
}

$python = Resolve-PythonExe
$script = Join-Path $PSScriptRoot "collect_claude_context.py"

$argsList = @("--cwd", $Cwd, "--format", $Format)
if ($TargetPath)     { $argsList += @("--target-path", $TargetPath) }
if ($RepoRoot)       { $argsList += @("--repo-root", $RepoRoot) }
if ($GlobalClaudeMd) { $argsList += @("--global-claude-md", $GlobalClaudeMd) }

& $python $script @argsList
exit $LASTEXITCODE
