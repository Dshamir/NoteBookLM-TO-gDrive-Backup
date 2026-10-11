# Vendors shields.io badges referenced in README.md into docs/assets/badges/ and rewrites the README to use them.
$ErrorActionPreference = "Stop"
$readme = Join-Path $PSScriptRoot "..\README.md" | Resolve-Path
$outDir = Join-Path (Split-Path $readme) "docs\assets\badges"
New-Item -ItemType Directory -Force $outDir | Out-Null
$text = [IO.File]::ReadAllText($readme)
$pattern = '<img src="(https://img\.shields\.io/[^"]+)" alt="([^"]+)">'
$matches = [regex]::Matches($text, $pattern)
foreach ($m in $matches) {
    $url = $m.Groups[1].Value.Replace('&amp;', '&')
    $alt = $m.Groups[2].Value
    $slug = ($alt.ToLower() -replace '[^a-z0-9]+', '-').Trim('-')
    $file = Join-Path $outDir "$slug.svg"
    curl.exe -s -f -o $file $url
    if ($LASTEXITCODE -ne 0 -or -not (Select-String -Path $file -Pattern '<svg' -Quiet)) { throw "download failed: $url" }
    $text = $text.Replace($m.Groups[1].Value, "docs/assets/badges/$slug.svg")
    Write-Host "ok  $slug.svg"
}
[IO.File]::WriteAllText($readme, $text, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "README updated: $($matches.Count) badges vendored"
