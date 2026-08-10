# run_daily_pipeline.ps1
#
# Orchestrates tramos A + B of the DQX ES translation pipeline:
#   A. ETP-runner exports the game's DAT files to raw JSON, and merges in
#      the latest EN from Clarity's repo.
#   B. That JSON gets turned into a local SQLite snapshot, synced up to
#      Supabase (new entries + routine EN refresh, JA changes flagged for
#      review), and the current Supabase state is re-exported to
#      ETP-ready JSON.
#
# Tramo C (JSON -> ETP -> .clpk via clarity.exe) stays manual for now.
#
# This script is meant to be run the same way whether you trigger it by
# hand or via a scheduled GitHub Actions self-hosted runner -- all the
# logic lives here, not duplicated in the workflow YAML.
#
# Requires: DATABASE_URL environment variable set to your Supabase
# connection string (pooler, not direct -- see earlier notes on IPv6).

$ErrorActionPreference = "Stop"

# --- Cargar variables desde .env si existe (no sobrescribe variables ya definidas en la sesion) ---
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -match '^\s*$') { return }  # comentarios y lineas vacias
        $key, $value = $_ -split '=', 2
        $key = $key.Trim()
        if (-not (Test-Path "env:$key")) {
            Set-Item -Path "env:$key" -Value $value.Trim()
        }
    }
    Write-Host "Variables cargadas desde .env" -ForegroundColor DarkGray
}

# --- Paths -- adjust to match your actual folder layout ---
$RawJsonFolder      = ".\etp_output\raw_json"       # where ETP-runner drops the DAT->JSON export
$LocalDb            = ".\translations.db"
$ExportFolder       = ".\etp_output\es_ready_json"  # ETP-ready JSON, feeds into tramo C manually
$ChangedReviewCsv   = ".\logs\ja_changed_$(Get-Date -Format yyyy-MM-dd).csv"
$BackupCsv          = ".\backups\backup_$(Get-Date -Format yyyy-MM-dd).csv"

if (-not $env:DATABASE_URL) {
    Write-Error "DATABASE_URL no esta definida. Configurala antes de correr este script."
    exit 1
}

Write-Host "=== Tramo A: ETP-runner (extraccion DAT + merge EN) ===" -ForegroundColor Cyan
# TODO: reemplazar con el/los comando(s) reales de ETP-runner una vez que
# me pases la sintaxis exacta. Ejemplo de la forma que probablemente tenga:
#
#   etp-runner.exe extract --dat ".\dqx_dat" --output $RawJsonFolder
#   etp-runner.exe merge-en --input $RawJsonFolder --clarity-repo <url o ruta local>
#
# Por ahora este bloque solo verifica que la carpeta ya tenga contenido,
# asumiendo que corriste ETP-runner manualmente antes de este script.
if (-not (Test-Path $RawJsonFolder) -or (Get-ChildItem $RawJsonFolder -Filter *.json -Recurse).Count -eq 0) {
    Write-Error "No se encontraron JSON en $RawJsonFolder. Corre ETP-runner primero (o agrega su comando arriba)."
    exit 1
}
Write-Host "OK: JSON de origen encontrados en $RawJsonFolder"

Write-Host "`n=== Tramo B.1: construir snapshot local (build_translation_db.py) ===" -ForegroundColor Cyan
python scripts\build_translation_db.py $RawJsonFolder --output $LocalDb --overwrite
if ($LASTEXITCODE -ne 0) { Write-Error "build_translation_db.py fallo"; exit 1 }

Write-Host "`n=== Tramo B.2: respaldo antes de tocar Supabase (backup_entries.py) ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path (Split-Path $BackupCsv) | Out-Null
python scripts\backup_entries.py --output $BackupCsv
if ($LASTEXITCODE -ne 0) { Write-Error "backup_entries.py fallo"; exit 1 }

Write-Host "`n=== Tramo B.3: sincronizar con Supabase (sync_json_updates.py) ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path (Split-Path $ChangedReviewCsv) | Out-Null
python scripts\sync_json_updates.py $LocalDb --review-output $ChangedReviewCsv
if ($LASTEXITCODE -ne 0) { Write-Error "sync_json_updates.py fallo"; exit 1 }

if (Test-Path $ChangedReviewCsv) {
    Write-Host "`n[!] Hay entradas con JA modificado que requieren revision manual: $ChangedReviewCsv" -ForegroundColor Yellow
}

Write-Host "`n=== Tramo B.4: re-exportar estado actual de Supabase a JSON (export_translations.py) ===" -ForegroundColor Cyan
python scripts\export_translations.py --lang es --all --output $ExportFolder
if ($LASTEXITCODE -ne 0) { Write-Error "export_translations.py fallo"; exit 1 }

Write-Host "`n=== Listo ===" -ForegroundColor Green
Write-Host "JSON listos para tramo C (conversion manual a ETP/.clpk) en: $ExportFolder"
