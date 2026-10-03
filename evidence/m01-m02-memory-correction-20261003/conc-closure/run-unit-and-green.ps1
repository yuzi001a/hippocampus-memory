$ErrorActionPreference = 'Continue'
$py      = 'C:/hp-testbed/m01-m02-memory-correction-20261003/venv/Scripts/python.exe'
$srcroot = 'C:/hp-testbed/m01-m02-memory-correction-20261003/src-root'
$secrets = 'C:/hp-testbed/f2-overnight-20261002/secrets/pgpw.tmp'
$parent  = 'C:/hp-testbed/m01-m02-integration-20261003'
$stage   = $parent + '/conc-closure-staging'
$tests   = $srcroot + '/src/v3-core/tests/test_m01_memory_correction.py'
$prod    = $srcroot + '/src/v3-core/src/v3core/active_memory_store.py'
$probe   = $stage + '/m01m02_conc_probe.py'
$driver  = $stage + '/m01_m02_integration.py'
$fixedP  = $stage + '/active_memory_store_fixed.py'
$fixedT  = $stage + '/test_m01_memory_correction_fixed.py'
$prefixP = $stage + '/active_memory_store_prefix.py'
$rootG   = $parent + '/conc-closure-green'

Write-Output ('HOST_CHECK=' + $env:COMPUTERNAME)
if ($env:COMPUTERNAME -ne 'DESKTOP-EQP3OBU') { Write-Output 'WRONG_HOST'; exit 91 }

Write-Output ('FIXED_PRODUCT_SHA256=' + (Get-FileHash -Algorithm SHA256 $fixedP).Hash.ToLower())
Write-Output ('FIXED_TEST_SHA256=' + (Get-FileHash -Algorithm SHA256 $fixedT).Hash.ToLower())
Write-Output ('PROBE_SHA256=' + (Get-FileHash -Algorithm SHA256 $probe).Hash.ToLower())

# build the pre-fix package OUTSIDE the run root (for the unit RED)
$prefixPkg = $stage + '/prefix_pkg'
if (Test-Path $prefixPkg) { Remove-Item -Recurse -Force $prefixPkg }
New-Item -ItemType Directory -Path $prefixPkg -Force | Out-Null
Copy-Item -Recurse -Force ($srcroot + '/src/v3-core/src/v3core') ($prefixPkg + '/v3core')
Get-ChildItem -Recurse -Directory -Filter '__pycache__' $prefixPkg | Remove-Item -Recurse -Force
Copy-Item -Force $prefixP -Force ($prefixPkg + '/v3core/active_memory_store.py')

# the NEW tests must be present for both runs
Copy-Item -Force $fixedT $tests

Set-Location ($srcroot + '/src/v3-core')
$env:PYTHONPATH = $prefixPkg
Write-Output '=== UNIT RED (pre-fix product + new tests) ==='
& $py -m pytest $tests -k "pool_less_store" -q *>&1 | Tee-Object -FilePath ($stage + '/unit-red.log')
Write-Output ('RED_RC=' + $LASTEXITCODE)
$env:PYTHONPATH = ''

# apply the fix to the artifact under test
Copy-Item -Force $fixedP $prod
Write-Output ('APPLIED_PRODUCT_SHA256=' + (Get-FileHash -Algorithm SHA256 $prod).Hash.ToLower())

Write-Output '=== UNIT GREEN (fixed product + new tests) ==='
& $py -m pytest $tests -k "pool_less_store" -q *>&1 | Tee-Object -FilePath ($stage + '/unit-green.log')
Write-Output ('GREEN_RC=' + $LASTEXITCODE)

Write-Output '=== EXISTING CONCURRENCY UNIT TESTS (regression) ==='
& $py -m pytest $tests -k "concurrent or idempotent or retry" -q *>&1 | Tee-Object -FilePath ($stage + '/unit-concurrency.log')
Write-Output ('CONC_RC=' + $LASTEXITCODE)

Write-Output '=== REAL PG GREEN PROBE ==='
if (Test-Path $rootG) { Write-Output ('ROOT_EXISTS_REFUSED=' + $rootG); exit 90 }
New-Item -ItemType Directory -Path $rootG | Out-Null
& $py $probe --root $rootG --source-root $srcroot --env-python $py --secrets $secrets `
    --db-name m01m02e2e_concclosure_g --out ($rootG + '/report.json') `
    --driver $driver --label green *>&1 | Tee-Object -FilePath ($rootG + '/console.log')
Write-Output ('PROBE_RC=' + $LASTEXITCODE)
Write-Output ('GREEN_REPORT=' + (Test-Path ($rootG + '/report.json')))
