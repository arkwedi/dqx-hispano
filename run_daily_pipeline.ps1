# run_daily_pipeline.ps1
#
# End-to-end DQX ES pipeline, from the game's raw DAT files all the way to
# a common.zip ready for Clarity's .clpk packaging tool. Collapses what
# used to be 9 manual steps into one script call -- only the final
# common.zip -> .clpk conversion (Clarity's own tool) stays manual, since
# we don't yet know if that step has a CLI equivalent.
#
#   1. etp.exe all .                -> DAT -> raw JSON (ETPLocalizer, deja
#                                      common/, etp/, json/, rps/ en la
#                                      misma carpeta donde corres etp.exe)
#   2. etp.exe port-translations .  -> merges in Clarity's current EN
#   3. build_translation_db.py      -> JSON -> local SQLite snapshot
#      (lee json/ recursivamente, no le importa la subestructura interna)
#   4. sync_json_updates.py         -> SQLite -> Supabase
#   5. export_translations.py       -> Supabase -> ES JSON, escrito a una
#                                      carpeta de staging plana y luego
#                                      copiado de vuelta a la ubicacion
#                                      ORIGINAL exacta de cada archivo
#                                      (usando un mapa armado en el paso 1,
#                                      sin necesidad de conocer de antemano
#                                      la subestructura de json/) + fresh
#                                      glossary.db / clarity_dialog.db
#   6. etp.exe rebuild . .          -> JSON -> ETP binaries in common/
#   7. Compress common/ -> common.zip
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
        if ($_ -match '^\s*#' -or $_ -match '^\s*$') { return }
        $key, $value = $_ -split '=', 2
        $key = $key.Trim()
        if (-not (Test-Path "env:$key")) {
            Set-Item -Path "env:$key" -Value $value.Trim()
        }
    }
    Write-Host "Variables cargadas desde .env" -ForegroundColor DarkGray
}

# --- Rutas -- ajusta a tu layout real ---
$EtpExe             = $env:ETP_EXE_PATH        # ej: "C:\Tools\ETPLocalizer\etp.exe"
$EtpWorkDir         = $env:ETP_WORK_DIR        # carpeta donde corres etp.exe (aqui aparecen common/, etp/, json/, rps/)
$LocalDb            = ".\translations.db"
$ExportStaging      = $env:ETP_WORK_DIR&"\json\_lang\en"   # export_translations.py escribe aqui, plano (un .json por archivo)
$ChangedReviewCsv   = ".\logs\ja_changed_$(Get-Date -Format yyyy-MM-dd).csv"
$BackupCsv          = ".\backups\backup_$(Get-Date -Format yyyy-MM-dd).csv"
$ClarityGlossaryDb  = $env:CLARITY_GLOSSARY_DB_PATH   # ej: "C:\dqxclarity\misc_files\glossary.db"
$ClarityDialogDb    = $env:CLARITY_DIALOG_DB_PATH     # ej: "C:\dqxclarity\misc_files\clarity_dialog.db"
$CommonZipOutput    = ".\etp_output\common.zip"

if (-not $env:DATABASE_URL) {
    Write-Error "DATABASE_URL no esta definida. Configurala antes de correr este script."
    exit 1
}
if (-not $EtpExe -or -not (Test-Path $EtpExe)) {
    Write-Error "ETP_EXE_PATH no apunta a un etp.exe valido. Definelo en tu .env."
    exit 1
}
if (-not $EtpWorkDir -or -not (Test-Path $EtpWorkDir)) {
    Write-Error "ETP_WORK_DIR no apunta a una carpeta valida. Definelo en tu .env."
    exit 1
}

Push-Location $EtpWorkDir
try {
    Write-Host "=== Paso 1: DAT -> JSON crudo (etp.exe all .) ===" -ForegroundColor Cyan
    & $EtpExe all .
    if ($LASTEXITCODE -ne 0) { throw "etp.exe all fallo" }

    Write-Host "`n=== Paso 2: importar EN de Clarity (etp.exe port-translations .) ===" -ForegroundColor Cyan
    & $EtpExe port-translations .
    if ($LASTEXITCODE -ne 0) { throw "etp.exe port-translations fallo" }
}
finally {
    Pop-Location
}

$RawJsonFolder = Join-Path $EtpWorkDir "json"
if (-not (Test-Path $RawJsonFolder) -or (Get-ChildItem $RawJsonFolder -Filter *.json -Recurse).Count -eq 0) {
    Write-Error "No se encontraron JSON en $RawJsonFolder tras correr ETPLocalizer."
    exit 1
}
$jsonFileCount = (Get-ChildItem $RawJsonFolder -Filter *.json -Recurse).Count
Write-Host "OK: $jsonFileCount JSON de origen en $RawJsonFolder"

# Mapa nombre-de-archivo -> ruta completa real, para poder reescribir cada
# archivo exactamente donde etp.exe espera encontrarlo en el paso 6, sin
# tener que adivinar/hardcodear la subestructura interna de json/.
$jsonFileMap = @{}
Get-ChildItem $RawJsonFolder -Filter *.json -Recurse | ForEach-Object {
    $jsonFileMap[$_.BaseName] = $_.FullName
}

Write-Host "`n=== Paso 3: construir snapshot local (build_translation_db.py) ===" -ForegroundColor Cyan
python scripts\build_translation_db.py $RawJsonFolder --output $LocalDb --overwrite
if ($LASTEXITCODE -ne 0) { Write-Error "build_translation_db.py fallo"; exit 1 }

Write-Host "`n=== Paso 3b: respaldo antes de tocar Supabase (backup_entries.py) ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path (Split-Path $BackupCsv) | Out-Null
python scripts\backup_entries.py --output $BackupCsv
if ($LASTEXITCODE -ne 0) { Write-Error "backup_entries.py fallo"; exit 1 }

Write-Host "`n=== Paso 4: sincronizar con Supabase (sync_json_updates.py) ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path (Split-Path $ChangedReviewCsv) | Out-Null
python scripts\sync_json_updates.py $LocalDb --review-output $ChangedReviewCsv
if ($LASTEXITCODE -ne 0) { Write-Error "sync_json_updates.py fallo"; exit 1 }

if (Test-Path $ChangedReviewCsv) {
    Write-Host "`n[!] Hay entradas con JA modificado que requieren revision manual: $ChangedReviewCsv" -ForegroundColor Yellow
}

Write-Host "`n=== Paso 5: Supabase -> JSON con ES + DBs de Clarity (export_translations.py) ===" -ForegroundColor Cyan
if (Test-Path $ExportStaging) { Remove-Item $ExportStaging -Recurse -Force }
$exportArgs = @("--lang", "es", "--all", "--output", $ExportStaging, "--build-clarity-dbs")
python scripts\export_translations.py @exportArgs
if ($LASTEXITCODE -ne 0) { Write-Error "export_translations.py fallo"; exit 1 }

# export_translations.py escribe un .json plano por archivo en $ExportStaging.
# Los reescribimos en su ubicacion ORIGINAL real (usando el mapa armado mas
# arriba), para que etp.exe rebuild encuentre cada uno donde corresponde,
# sin importar que tan anidada este la carpeta json/ internamente.
# --- Paso 5 (Sección de copiado corregida) ---
$copiedCount = 0
$missingCount = 0

Get-ChildItem $ExportStaging -Filter *.json | ForEach-Object {
    if ($jsonFileMap.ContainsKey($_.BaseName)) {
        $destinationPath = $jsonFileMap[$_.BaseName]
        Copy-Item $_.FullName -Destination $destinationPath -Force
        $copiedCount++
    } else {
        # Si por alguna razón no estaba en el mapa, forzamos la copia directa a la carpeta json de ETPWorkDir
        $fallbackPath = Join-Path $EtpWorkDir "json\$($_.Name)"
        Copy-Item $_.FullName -Destination $fallbackPath -Force
        Write-Warning "No se encontró mapa para $($_.Name); copiado directamente a $fallbackPath"
        $missingCount++
    }
}
Write-Host "$copiedCount archivos actualizados exitosamente en $RawJsonFolder" -ForegroundColor Green

# --build-clarity-dbs escribe glossary.db/clarity_dialog.db en $ExportStaging;
# las copiamos a donde tu Clarity local las lee, si configuraste esas rutas.
$generatedGlossaryDb = Join-Path $ExportStaging "glossary.db"
$generatedDialogDb   = Join-Path $ExportStaging "clarity_dialog.db"
if ($ClarityGlossaryDb -and (Test-Path $generatedGlossaryDb)) {
    Copy-Item $generatedGlossaryDb $ClarityGlossaryDb -Force
    Write-Host "glossary.db copiada a $ClarityGlossaryDb"
}
if ($ClarityDialogDb -and (Test-Path $generatedDialogDb)) {
    Copy-Item $generatedDialogDb $ClarityDialogDb -Force
    Write-Host "clarity_dialog.db copiada a $ClarityDialogDb"
}

Write-Host "`n=== Paso 6: JSON -> ETP binario (etp.exe rebuild . .) ===" -ForegroundColor Cyan
Push-Location $EtpWorkDir
try {
    & $EtpExe rebuild . .
    if ($LASTEXITCODE -ne 0) { throw "etp.exe rebuild fallo" }
}
finally {
    Pop-Location
}

$CommonFolder = Join-Path $EtpWorkDir "common"
if (-not (Test-Path $CommonFolder)) {
    Write-Error "No se encontro la carpeta common/ tras el rebuild en $CommonFolder"
    exit 1
}

Write-Host "`n=== Paso 7: comprimir common/ -> common.zip ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path (Split-Path $CommonZipOutput) | Out-Null
if (Test-Path $CommonZipOutput) { Remove-Item $CommonZipOutput }
Compress-Archive -Path "$CommonFolder\*" -DestinationPath $CommonZipOutput

Write-Host "`n=== Listo ===" -ForegroundColor Green
Write-Host "common.zip generado en: $CommonZipOutput"
Write-Host "Unico paso manual restante: abrir Clarity y convertir ese zip a .clpk."
