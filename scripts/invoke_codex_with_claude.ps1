#Requires -Version 5.1
<#
.SYNOPSIS
  Compatibility shim around invoke_codex_with_claude.py.

.DESCRIPTION
  Resolves a Python interpreter (via $env:SKILLS_PYTHON,
  $env:CLAUDE_AUTOMATION_PYTHON, or PATH) and forwards every argument to
  invoke_codex_with_claude.py. Emits the wrapper's JSON verbatim on stdout.

  The Python wrapper is the single supported entrypoint. This shim only
  exists for backwards compatibility with older callers that hardcoded the
  .ps1 path. New code should call the .py directly.
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ForwardedArgs
)

$ErrorActionPreference = "Stop"

function Resolve-PythonExe {
    foreach ($candidate in @($env:SKILLS_PYTHON, $env:CLAUDE_AUTOMATION_PYTHON)) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command -Name $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    throw "No Python interpreter found. Set SKILLS_PYTHON or CLAUDE_AUTOMATION_PYTHON, or expose python on PATH."
}

$python = Resolve-PythonExe
$script = Join-Path $PSScriptRoot "invoke_codex_with_claude.py"

if (-not $ForwardedArgs) { $ForwardedArgs = @() }
& $python $script @ForwardedArgs
exit $LASTEXITCODE
