[CmdletBinding()]
param(
    [string]$SshHost = "root@161.35.17.179",
    [string]$RemoteBackupCommand = "/usr/local/sbin/tavi-coordina-backup",
    [string]$LocalBackupDirectory = "$HOME\Backups\TAVI-Coordina",
    [string]$PrivateKey = "$HOME\.tavi-coordina-backup\private-key.pem",
    [string]$Certificate = "$HOME\.tavi-coordina-backup\recipient-cert.pem"
)

$ErrorActionPreference = "Stop"

$null = New-Item -ItemType Directory -Force -Path $LocalBackupDirectory
$localDirectory = (Resolve-Path -LiteralPath $LocalBackupDirectory).Path

$output = @(& ssh -o BatchMode=yes -o ConnectTimeout=15 $SshHost $RemoteBackupCommand)
if ($LASTEXITCODE -ne 0) {
    throw "El respaldo remoto fallo."
}

$remoteBackup = ($output | Where-Object { $_ -like "BACKUP_FILE=*" } | Select-Object -Last 1) -replace '^BACKUP_FILE=', ''
$remoteChecksum = ($output | Where-Object { $_ -like "SHA256_FILE=*" } | Select-Object -Last 1) -replace '^SHA256_FILE=', ''
if (-not $remoteBackup -or -not $remoteChecksum) {
    throw "El comando remoto no devolvio las rutas del respaldo."
}

$fileName = [IO.Path]::GetFileName($remoteBackup)
$checksumName = [IO.Path]::GetFileName($remoteChecksum)
$localBackup = Join-Path $localDirectory $fileName
$localChecksum = Join-Path $localDirectory $checksumName
$backupPart = "$localBackup.part"
$checksumPart = "$localChecksum.part"

try {
    & scp -q -o BatchMode=yes -o ConnectTimeout=15 "${SshHost}:$remoteBackup" $backupPart
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudo copiar el respaldo cifrado."
    }

    & scp -q -o BatchMode=yes -o ConnectTimeout=15 "${SshHost}:$remoteChecksum" $checksumPart
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudo copiar el checksum."
    }

    Move-Item -LiteralPath $backupPart -Destination $localBackup -Force
    Move-Item -LiteralPath $checksumPart -Destination $localChecksum -Force
}
finally {
    foreach ($partialFile in @($backupPart, $checksumPart)) {
        if (Test-Path -LiteralPath $partialFile) {
            Remove-Item -LiteralPath $partialFile -Force
        }
    }
}

$verificationScript = Join-Path $PSScriptRoot "verify_encrypted_backup.ps1"
$verification = & $verificationScript -BackupFile $localBackup -PrivateKey $PrivateKey -Certificate $Certificate
if ($LASTEXITCODE -ne 0) {
    throw "La verificacion local del respaldo fallo."
}

Get-ChildItem -LiteralPath $localDirectory -Filter "tavi-coordina-*.cms*" -File |
    Where-Object LastWriteTimeUtc -lt (Get-Date).ToUniversalTime().AddDays(-30) |
    Remove-Item -Force

$status = [ordered]@{
    completed_utc = (Get-Date).ToUniversalTime().ToString("o")
    backup = $localBackup
    sha256 = $verification.Sha256
    encrypted = $true
    structure_verified = $true
}
$status | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $localDirectory "last-success.json") -Encoding UTF8
$verification
