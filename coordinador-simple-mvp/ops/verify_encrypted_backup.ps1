[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BackupFile,

    [string]$PrivateKey = "$HOME\.tavi-coordina-backup\private-key.pem",

    [string]$Certificate = "$HOME\.tavi-coordina-backup\recipient-cert.pem"
)

$ErrorActionPreference = "Stop"

$backupPath = (Resolve-Path -LiteralPath $BackupFile).Path
$keyPath = (Resolve-Path -LiteralPath $PrivateKey).Path
$certificatePath = (Resolve-Path -LiteralPath $Certificate).Path
$checksumPath = "$backupPath.sha256"

if (-not (Test-Path -LiteralPath $checksumPath)) {
    throw "Falta el checksum: $checksumPath"
}

$expectedHash = ((Get-Content -LiteralPath $checksumPath -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $backupPath).Hash.ToLowerInvariant()
if ($actualHash -ne $expectedHash) {
    throw "El SHA256 no coincide para $backupPath"
}

$openssl = Get-Command openssl -ErrorAction Stop
$tar = Get-Command tar -ErrorAction Stop
$temporaryArchive = Join-Path ([IO.Path]::GetDirectoryName($backupPath)) (".verify-{0}.tar.gz" -f [guid]::NewGuid())

try {
    & $openssl.Source cms -decrypt -binary -inform DER `
        -in $backupPath -recip $certificatePath -inkey $keyPath `
        -out $temporaryArchive
    if ($LASTEXITCODE -ne 0) {
        throw "OpenSSL no pudo descifrar el respaldo."
    }

    $entries = @(& $tar.Source -tzf $temporaryArchive)
    if ($LASTEXITCODE -ne 0) {
        throw "El contenido tar.gz descifrado no es valido."
    }

    $requiredEntries = @(
        "postgres.dump",
        "pg_restore.list",
        "waauth.tar.gz",
        "payload.sha256",
        "metadata.txt"
    )
    foreach ($entry in $requiredEntries) {
        if ($entries -notcontains $entry) {
            throw "El respaldo no contiene el archivo requerido: $entry"
        }
    }
}
finally {
    if (Test-Path -LiteralPath $temporaryArchive) {
        Remove-Item -LiteralPath $temporaryArchive -Force
    }
}

[pscustomobject]@{
    Backup = $backupPath
    Sha256 = $actualHash
    Encrypted = $true
    StructureVerified = $true
}
