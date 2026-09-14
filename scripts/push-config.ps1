<#
.SYNOPSIS
  Pushes every secret and variable the pipeline needs from your local .env to the GitHub repo.

  Run it yourself after filling .env (never commit .env). It never prints secret values.
  Giscus repo and category IDs are looked up automatically once Discussions are enabled.

.USAGE
  .\scripts\push-config.ps1            # push everything found in .env
  .\scripts\push-config.ps1 -Check     # only report what is set and what is missing
#>
param([switch]$Check)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) { Write-Host "No .env at $envFile. Copy .env.example to .env and fill it in." -ForegroundColor Yellow; exit 1 }

$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) { $gh = Get-Command "C:\Program Files\GitHub CLI\gh.exe" -ErrorAction SilentlyContinue }
if (-not $gh) { Write-Host "GitHub CLI not found. Install: winget install GitHub.cli" -ForegroundColor Yellow; exit 1 }
& $gh.Source auth status 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "Not logged in. Run:  gh auth login" -ForegroundColor Yellow; exit 1 }

$repo = (& $gh.Source repo view --json nameWithOwner -q .nameWithOwner 2>$null)
if (-not $repo) { Write-Host "This folder is not linked to a GitHub repo yet. Run the repo steps in SETUP.md first." -ForegroundColor Yellow; exit 1 }
Write-Host "Repo: $repo"

# Parse .env into a hashtable.
$vals = @{}
foreach ($line in Get-Content $envFile) {
  $t = $line.Trim()
  if ($t -eq "" -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
  $k, $v = $t.Split("=", 2)
  $vals[$k.Trim()] = $v.Trim().Trim('"').Trim("'")
}

# Search Console: if the downloaded service-account key file is saved in the repo root as
# gsc-service-account.json (gitignored), use it instead of pasting the JSON into .env.
$gscFile = Join-Path $root "gsc-service-account.json"
$envHasGsc = [bool]$vals["GSC_SERVICE_ACCOUNT_JSON"]
if (-not $vals["GSC_SERVICE_ACCOUNT_JSON"] -and (Test-Path $gscFile)) {
  try {
    $vals["GSC_SERVICE_ACCOUNT_JSON"] = (Get-Content $gscFile -Raw | ConvertFrom-Json | ConvertTo-Json -Compress -Depth 10)
    Write-Host "Using gsc-service-account.json for GSC_SERVICE_ACCOUNT_JSON"
  } catch { Write-Host "gsc-service-account.json is not valid JSON" -ForegroundColor Yellow }
}

# Secrets stay secret; PUBLIC_ values are embedded in the site so they are stored as secrets too
# (harmless) except the ones that are convenient to edit in the GitHub UI, which go to variables.
$secrets   = @("DATABASE_URL", "GEMINI_API_KEY", "GROQ_API_KEY", "KIT_API_KEY",
               "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID",
               "PUBLIC_SUPABASE_URL", "PUBLIC_SUPABASE_ANON_KEY", "GSC_SERVICE_ACCOUNT_JSON",
               "BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL", "VAPID_PRIVATE_KEY",
               "ADMIN_REVIEW_KEY")
$variables = @("PUBLIC_KIT_FORM_URL", "NEWSLETTER_HOUR_UTC", "PUBLIC_VAPID_KEY",
               "PUBLIC_GISCUS_REPO", "PUBLIC_GISCUS_REPO_ID", "PUBLIC_GISCUS_CATEGORY", "PUBLIC_GISCUS_CATEGORY_ID")

# Giscus: fill the IDs from GitHub itself when Discussions are enabled.
if (-not $vals["PUBLIC_GISCUS_REPO"]) { $vals["PUBLIC_GISCUS_REPO"] = $repo }
if (-not $vals["PUBLIC_GISCUS_CATEGORY"]) { $vals["PUBLIC_GISCUS_CATEGORY"] = "Announcements" }
$owner, $name = $repo.Split("/")
$query = 'query($o:String!,$n:String!){ repository(owner:$o,name:$n){ id hasDiscussionsEnabled discussionCategories(first:25){ nodes{ id name } } } }'
$json = & $gh.Source api graphql -f query=$query -f o=$owner -f n=$name 2>$null | ConvertFrom-Json
if ($json -and $json.data.repository) {
  $r = $json.data.repository
  if ($r.hasDiscussionsEnabled) {
    $vals["PUBLIC_GISCUS_REPO_ID"] = $r.id
    $cat = $r.discussionCategories.nodes | Where-Object { $_.name -eq $vals["PUBLIC_GISCUS_CATEGORY"] } | Select-Object -First 1
    if ($cat) { $vals["PUBLIC_GISCUS_CATEGORY_ID"] = $cat.id }
    else { Write-Host "Discussions are on but there is no category named '$($vals["PUBLIC_GISCUS_CATEGORY"])'. Create it (Announcements type) at https://github.com/$repo/discussions/categories" -ForegroundColor Yellow }
  } else {
    Write-Host "Discussions are not enabled: Settings > General > Features > Discussions. Giscus IDs skipped." -ForegroundColor Yellow
  }
}

function Report($label, $keys) {
  foreach ($k in $keys) {
    $state = if ($vals[$k]) { "set" } else { "missing" }
    $color = if ($vals[$k]) { "Green" } else { "DarkGray" }
    Write-Host ("  {0,-28} {1}" -f $k, $state) -ForegroundColor $color
  }
}
Write-Host "Secrets:";   Report "secret" $secrets
Write-Host "Variables:"; Report "variable" $variables
if ($Check) { exit 0 }

foreach ($k in $secrets) {
  # --body rather than a pipe: Windows PowerShell 5.1 writes a UTF-8 byte-order mark in front of
  # piped text, which silently corrupts the secret (DATABASE_URL then fails to parse in CI).
  if ($k -eq "GSC_SERVICE_ACCOUNT_JSON" -and -not $envHasGsc -and (Test-Path $gscFile)) {
    # JSON contains quotes that PowerShell strips from native arguments; feed the file through cmd
    # redirection instead (raw bytes, no byte-order mark).
    & cmd /c "`"$($gh.Source)`" secret set $k --repo $repo < `"$gscFile`"" | Out-Null; Write-Host "  pushed secret   $k (from gsc-service-account.json)"
  } elseif ($vals[$k]) { & $gh.Source secret set $k --repo $repo --body $vals[$k] | Out-Null; Write-Host "  pushed secret   $k" }
}
foreach ($k in $variables) {
  if ($vals[$k]) { & $gh.Source variable set $k --repo $repo --body $vals[$k] | Out-Null; Write-Host "  pushed variable $k" }
}
Write-Host "Done. Trigger a run:  gh workflow run 'Ingest and publish'" -ForegroundColor Green
