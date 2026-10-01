param([ValidateRange(1, 65535)][int]$Port = 8917)
$ErrorActionPreference = 'Stop'
$PipelineDirectory = Join-Path $PSScriptRoot '..\pipeline'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'Install uv first: winget install --id astral-sh.uv -e, then reopen PowerShell.'
}
Write-Host "Starting PublikClip MCP at http://127.0.0.1:$Port/mcp"
Write-Host 'Keep this window open. Press Ctrl+C to stop the server and its active job.'
& uv --directory $PipelineDirectory run --frozen --group mcp publikclip-mcp --transport streamable-http --port $Port
exit $LASTEXITCODE
