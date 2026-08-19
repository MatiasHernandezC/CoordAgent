param(
    [Parameter(Mandatory = $true)]
    [string]$SourceEnv,
    [string]$TargetEnv = (Join-Path $PSScriptRoot "..\.env.prod")
)

$ErrorActionPreference = "Stop"

function Read-EnvFile([string]$Path) {
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') {
            $values[$matches[1]] = $matches[2]
        }
    }
    return $values
}

function New-RandomSecret([int]$Bytes = 32) {
    $buffer = New-Object byte[] $Bytes
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($buffer) } finally { $generator.Dispose() }
    return [Convert]::ToBase64String($buffer)
}

$sourcePath = (Resolve-Path -LiteralPath $SourceEnv).Path
$targetPath = [IO.Path]::GetFullPath($TargetEnv)
$examplePath = Join-Path $PSScriptRoot "..\.env.prod.example"
$source = Read-EnvFile $sourcePath

foreach ($required in @("GEMINI_API_KEY", "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET")) {
    if (-not $source.ContainsKey($required) -or [string]::IsNullOrWhiteSpace($source[$required])) {
        throw "Falta $required en el archivo local de origen."
    }
}

$values = @{
    PUBLIC_DOMAIN = "coordina.xshift007.com"
    PUBLIC_URL = "https://coordina.xshift007.com"
    PANEL_ADMIN_USERNAME = "admin"
    PANEL_ADMIN_DISPLAY_NAME = "Administrador Coordina"
    PANEL_ADMIN_PASSWORD = "Adm-$(New-RandomSecret 18)"
    PANEL_SELF_REGISTRATION_ENABLED = "true"
    GATEWAY_API_TOKEN = New-RandomSecret 32
    POSTGRES_USER = "postgres"
    POSTGRES_PASSWORD = New-RandomSecret 32
    POSTGRES_DB = "meetingdb"
    LLM_PROVIDER = "gemini"
    GEMINI_API_KEY = $source["GEMINI_API_KEY"]
    LLM_KEYS_MASTER_KEY = New-RandomSecret 32
    ADMIN_PROXY_HEADER_REQUIRED = "true"
    GEMINI_MODEL = $(if ($source["GEMINI_MODEL"]) { $source["GEMINI_MODEL"] } else { "gemini-2.5-flash-lite" })
    LLM_FALLBACK_ENABLED = "true"
    LLM_CACHE_ENABLED = "true"
    SLACK_BOT_TOKEN = $source["SLACK_BOT_TOKEN"]
    SLACK_SIGNING_SECRET = $source["SLACK_SIGNING_SECRET"]
    SLACK_TRIGGER_WORD = "@coordina"
    GOOGLE_OAUTH_REDIRECT_URI = "https://coordina.xshift007.com/api/admin/google-calendar/callback"
}

$rendered = foreach ($line in Get-Content -LiteralPath $examplePath) {
    if ($line -match '^([A-Z][A-Z0-9_]*)=') {
        $name = $matches[1]
        if ($values.ContainsKey($name)) { "$name=$($values[$name])" } else { $line }
    } else {
        $line
    }
}

[IO.File]::WriteAllLines($targetPath, $rendered, [Text.UTF8Encoding]::new($false))
Write-Output "Configuracion privada preparada en: $targetPath"
Write-Output "Claves existentes copiadas sin mostrarlas; secretos internos nuevos generados."

