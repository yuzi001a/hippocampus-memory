$ErrorActionPreference = 'Continue'
$py      = 'C:/hp-testbed/m01-m02-memory-correction-20261003/venv/Scripts/python.exe'
$srcroot = 'C:/hp-testbed/m01-m02-memory-correction-20261003/src-root'
$parent  = 'C:/hp-testbed/m01-m02-integration-20261003'
$stage   = $parent + '/conc-closure-staging'
$tests   = $srcroot + '/src/v3-core/tests/test_m01_memory_correction.py'
$fixedT  = $stage + '/test_m01_memory_correction_fixed.py'
$prefixP = $stage + '/active_memory_store_prefix.py'
$fixedP  = $stage + '/active_memory_store_fixed.py'
$prod    = $srcroot + '/src/v3-core/src/v3core/active_memory_store.py'

Write-Output ('HOST_CHECK=' + $env:COMPUTERNAME)
if ($env:COMPUTERNAME -ne 'DESKTOP-EQP3OBU') { Write-Output 'WRONG_HOST'; exit 91 }

Copy-Item -Force $fixedT $tests

# pre-fix package (src-root is already fixed, so overwrite with the prefix file)
$prefixPkg = $stage + '/prefix_pkg'
if (Test-Path $prefixPkg) { Remove-Item -Recurse -Force $prefixPkg }
New-Item -ItemType Directory -Path $prefixPkg -Force | Out-Null
Copy-Item -Recurse -Force ($srcroot + '/src/v3-core/src/v3core') ($prefixPkg + '/v3core')
Get-ChildItem -Recurse -Directory -Filter '__pycache__' $prefixPkg | Remove-Item -Recurse -Force
Copy-Item -Force $prefixP ($prefixPkg + '/v3core/active_memory_store.py')

Set-Location ($srcroot + '/src/v3-core')
$env:PYTHONPATH = $prefixPkg
Write-Output '=== UNIT RED (pre-fix product + new tests) ==='
& $py -m pytest $tests -k "pool_less_store" -q *>&1 | Tee-Object -FilePath ($stage + '/unit-red2.log')
Write-Output ('RED_RC=' + $LASTEXITCODE)
$env:PYTHONPATH = ''

Copy-Item -Force $fixedP $prod
Write-Output '=== UNIT GREEN (fixed product + new tests) ==='
& $py -m pytest $tests -k "pool_less_store" -q *>&1 | Tee-Object -FilePath ($stage + '/unit-green2.log')
Write-Output ('GREEN_RC=' + $LASTEXITCODE)
