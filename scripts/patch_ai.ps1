$aiClient='\\192.168.0.244\docker\magiclists-data\backend\ai_client.py'
$lines = Get-Content -LiteralPath $aiClient
$boundary = $lines.Count
for ($i=0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match 'async def curate_rediscover_weekly') { $boundary=$i; break } }
$idxs = @(357,363,382,388,393)
foreach ($ln in $idxs) {
    $i = $ln - 1
    if ($i -ge 0 -and $i -lt $lines.Count) {
        $lines[$i] = $lines[$i] -replace 'candidate_tracks','shuffled_tracks'
    }
}
Set-Content -LiteralPath $aiClient -Value $lines -Encoding UTF8

$aiProv='\\192.168.0.244\docker\magiclists-data\backend\services\ai_providers.py'
$provLines = Get-Content -LiteralPath $aiProv
for ($k=0; $k -lt $provLines.Count; $k++) {
    if ($provLines[$k] -match 'os\.makedirs\("payloads"') {
        $provLines[$k] = $provLines[$k] -replace 'os\.makedirs\("payloads", exist_ok=True\)', 'payload_dir = os.path.join(os.getcwd(), "payloads"); os.makedirs(payload_dir, exist_ok=True)'
    }
    if ($provLines[$k] -match 'payload_file = f"payloads\/google_ai_payload_') {
        $provLines[$k] = $provLines[$k] -replace 'payload_file = f"payloads\/google_ai_payload_\{timestamp\}.json"', 'payload_file = os.path.join(payload_dir, f"google_ai_payload_{timestamp}.json")'
    }
}
Set-Content -LiteralPath $aiProv -Value $provLines -Encoding UTF8

Write-Output 'PATCH_SCRIPT_DONE'