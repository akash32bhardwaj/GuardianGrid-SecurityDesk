# ============================================================================
# cleanup_octa_folders.ps1  -  tidy the two Octa working folders
# ----------------------------------------------------------------------------
# Nothing is deleted. Every file listed below is MOVED to
#     C:\GuardianGrid\_to_delete_2026-09-21\
# keeping its folder structure, so anything can be put back by moving it
# back. After a week of everything working, delete that folder yourself.
#
# Only files that are clearly not used by the running product are listed:
# backup copies (.bak), "-1" download duplicates, empty stray files, old
# one-off test scripts, OCR debug images, a saved login token, and the
# "Claude outputs" download folder (every file in it has already been
# copied to where it belongs).
#
# NOT touched: .env, config.py, site_config.json, whatsapp_config.py,
# databases, the .bat launchers, cloudflared.exe, yolov8n.pt, anything
# the Docker image or the build uses.
#
# Run in PowerShell:
#   powershell -ExecutionPolicy Bypass -File C:\GuardianGrid\GuardianGrid-SecurityDesk\cleanup_octa_folders.ps1
# ============================================================================

$Dest = "C:\GuardianGrid\_to_delete_2026-09-21"

$Backend = "C:\GuardianGrid\GuardianGrid-SecurityDesk"
$BackendItems = @(
    # backup copies - every one of these is also in git history
    "ack_routes.py.bak", "api_server.py.bak", "api_server.py.pre-hls-overlay.bak",
    "api_server.py.pre-hls.bak", "deploy_v2.sh.bak", "Dockerfile.bak", "Dockerfile.bak2",
    "escalation_metrics.py.bak", "flat_directory.py.bak", "guardian_wiring.py.bak",
    "new_site.sh.bak", "octa_search.py.bak", "reset_demo.sh.bak", "resident_import.py.bak",
    "resident_routes.py.bak", "rtmp_proxy.py.bak", "rtmp_proxy.py.pre-hls-overlay.bak",
    "seed_incidents.py.bak", "site_config.py.bak", "weekly_audit.py.bak",
    # download duplicates and superseded copies
    "panic_routes-1.py", "site_profile-1.py", "octa_search-1.py", "auth_models.py.superseded",
    # empty files created by accident (0 bytes each)
    "Code.txt", "print(result)", "python", "result", "ssh",
    # leftovers from a crashed file sync
    ".fuse_hidden000001ad00000001", ".fuse_hidden000001ad00000002",
    # one-off test scripts from June/July
    "test_whatsapp.py", "test_blacklist.py", "test_face.py",
    # number-plate OCR debug images from June
    "bottom_half.jpg", "car.jpg", "plate_crop.jpg", "plate_final.jpg",
    "plate_thresh.jpg", "plate_tight_thresh.jpg", "plate_tight.jpg", "plate_zone.jpg",
    # a saved login token from a July test - should not sit in a folder
    "tok.json",
    # 1.7 KB test report from July
    "GuardianGrid_Report.pdf",
    # Python cache - rebuilt automatically
    "__pycache__"
)

$Frontend = "C:\Users\akash\Desktop\GuardianGrid\guardiangrid-command-center"
$FrontendItems = @(
    "Claude outputs",
    "lock_old.txt",
    "src\auth\AuthContext.jsx.bak",
    "src\context\SiteConfigContext.jsx.bak",
    "src\v2\api.js.bak",
    "src\v2\GuardianGridV2.jsx.bak",
    "src\v2\IncidentCanvas.jsx.bak",
    "src\v2\IncidentsPage.jsx.bak",
    "src\v2\OctaSearch.jsx.bak",
    "src\v2\ResidentsOnboarding.jsx.bak",
    # stray Python copies inside the frontend source (the real ones live
    # in GuardianGrid-SecurityDesk; these are old and unused)
    "src\resident_app.py",
    "src\v2\morning_report.py",
    "src\v2\apply_hls_overlay_backend.py"
)

function Collect($root, $items, $tag) {
    $found = @()
    foreach ($i in $items) {
        $p = Join-Path $root $i
        if (Test-Path -LiteralPath $p) {
            $found += [pscustomobject]@{ Root = $root; Rel = $i; Tag = $tag; Full = $p }
        }
    }
    return $found
}

$all = @()
$all += Collect $Backend  $BackendItems  "SecurityDesk"
$all += Collect $Frontend $FrontendItems "command-center"

if ($all.Count -eq 0) {
    Write-Host "Nothing to move - the folders are already clean." -ForegroundColor Green
    exit 0
}

Write-Host ""
Write-Host "These $($all.Count) items will be MOVED (not deleted) to $Dest :" -ForegroundColor Cyan
foreach ($x in $all) { Write-Host ("   [{0}] {1}" -f $x.Tag, $x.Rel) }
Write-Host ""
$answer = Read-Host "Type YES to move them"
if ($answer -ne "YES") { Write-Host "Cancelled. Nothing was moved."; exit 0 }

$moved = 0; $failed = 0
foreach ($x in $all) {
    $target = Join-Path (Join-Path $Dest $x.Tag) $x.Rel
    $parent = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    try {
        Move-Item -LiteralPath $x.Full -Destination $target -Force -ErrorAction Stop
        $moved++
    } catch {
        Write-Host "   could not move $($x.Rel): $($_.Exception.Message)" -ForegroundColor Yellow
        $failed++
    }
}

Write-Host ""
Write-Host "Moved $moved item(s). Failed: $failed." -ForegroundColor Green
Write-Host "Everything is in $Dest - delete that folder yourself once you're happy."
Write-Host ""
Write-Host "Next, record the tidy-up in git (in this same PowerShell window):" -ForegroundColor Cyan
Write-Host "   cd C:\GuardianGrid\GuardianGrid-SecurityDesk"
Write-Host "   git add -u"
Write-Host "   git commit -m `"Tidy: remove backups, duplicates, test scripts and debug images`""
Write-Host "   git push origin mediamtx-hls-integration"
