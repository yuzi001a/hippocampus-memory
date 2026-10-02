$ErrorActionPreference = "Continue"
$expected = "DESKTOP-EQP3OBU"
$machine = (hostname).Trim().ToUpperInvariant()
if ($machine -ne $expected) { "HOST_GATE_FAIL=" + $machine; exit 91 }

$base = "C:\hp-testbed\f2-overnight-20261002"
$stage = "C:\hp-testbed\b04-integration-20261002"
$root = Join-Path $stage "runs\b04-integration-run-1"
$python = Join-Path $base "pyenv\Scripts\python.exe"
$node = Join-Path $base "node\node-v24.21.0-win-x64\node.exe"
$secrets = Join-Path $base "secrets\pgpw.tmp"
$helper = Join-Path $stage "b04_integration.py"
$adapter = Join-Path $stage "adapter"
$stub = Join-Path $stage "adapter\eval\stub-model-server.mjs"
$dshHome = Join-Path $stage "dsh-home"
$out = Join-Path $root "b04-integration.json"

"HOST=" + $machine
"HASH_HELPER=" + (Get-FileHash $helper -Algorithm SHA256).Hash
"HASH_ADAPTER_INDEX=" + (Get-FileHash (Join-Path $adapter "src\index.js") -Algorithm SHA256).Hash
"HASH_STUB=" + (Get-FileHash $stub -Algorithm SHA256).Hash
"CORE=" + (& $python -c "import v3core; print(v3core.__file__)")

# The driver refuses a non-empty run root: a typo must not scatter artifacts.
if (-not (Test-Path $root)) { New-Item -ItemType Directory -Path $root | Out-Null }
"ROOT_EXISTS=" + (Test-Path $root)
"ROOT_EMPTY=" + (-not (Get-ChildItem -Force $root | Select-Object -First 1))

& $python -B $helper --root $root --support-root $base --secrets $secrets `
    --env-root (Join-Path $base "pyenv") --node $node --dsh-home $dshHome `
    --adapter $adapter --stub $stub --db-name b04dsh_20261002 --out $out
$rc = $LASTEXITCODE
"ACCEPTANCE_RC=" + $rc
"OUT_EXISTS=" + (Test-Path $out)
exit $rc
