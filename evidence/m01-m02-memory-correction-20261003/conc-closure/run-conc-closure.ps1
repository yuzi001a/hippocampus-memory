$ErrorActionPreference = 'Continue'
$py      = 'C:/hp-testbed/m01-m02-memory-correction-20261003/venv/Scripts/python.exe'
$srcroot = 'C:/hp-testbed/m01-m02-memory-correction-20261003/src-root'
$secrets = 'C:/hp-testbed/f2-overnight-20261002/secrets/pgpw.tmp'
$parent  = 'C:/hp-testbed/m01-m02-integration-20261003'
$stage   = $parent + '/conc-closure-staging'
$rootC   = $parent + '/conc-closure-canon'
$rootI   = $parent + '/conc-closure-instr'
$probe   = $stage + '/m01m02_conc_probe.py'
$driver  = $stage + '/m01_m02_integration.py'
$instr   = $stage + '/active_memory_store_instr.py'

Write-Output ('HOST_CHECK=' + $env:COMPUTERNAME)
if ($env:COMPUTERNAME -ne 'DESKTOP-EQP3OBU') { Write-Output 'WRONG_HOST'; exit 91 }

# snapshot the canonical driver (read-only for this probe; never modified here)
Copy-Item ($env:USERPROFILE + '/m01_m02_integration.py') $driver -Force
Write-Output ('DRIVER_SHA256=' + (Get-FileHash -Algorithm SHA256 $driver).Hash.ToLower())
Write-Output ('PROBE_SHA256=' + (Get-FileHash -Algorithm SHA256 $probe).Hash.ToLower())
Write-Output ('INSTR_SHA256=' + (Get-FileHash -Algorithm SHA256 $instr).Hash.ToLower())

foreach ($r in @($rootC, $rootI)) {
  if (Test-Path $r) { Write-Output ('ROOT_EXISTS_REFUSED=' + $r); exit 90 }
  New-Item -ItemType Directory -Path $r | Out-Null
}

# staged instrumented v3core package (full copy + instrumented store file)
$instrPkg = $rootI + '/instr/v3core'
New-Item -ItemType Directory -Path ($rootI + '/instr') -Force | Out-Null
Copy-Item -Recurse -Force ($srcroot + '/src/v3-core/src/v3core') $instrPkg
Copy-Item -Force $instr ($instrPkg + '/active_memory_store.py')
Write-Output ('INSTR_PKG_STORE_SHA256=' + (Get-FileHash -Algorithm SHA256 ($instrPkg + '/active_memory_store.py')).Hash.ToLower())

Write-Output '=== CANONICAL RUN ==='
& $py $probe --root $rootC --source-root $srcroot --env-python $py --secrets $secrets `
    --db-name m01m02e2e_concclosure_c --out ($rootC + '/report.json') `
    --driver $driver --label canonical *>&1 | Tee-Object -FilePath ($rootC + '/console.log')
Write-Output ('CANON_RC=' + $LASTEXITCODE)

Write-Output '=== INSTRUMENTED RUN ==='
& $py $probe --root $rootI --source-root $srcroot --env-python $py --secrets $secrets `
    --db-name m01m02e2e_concclosure_i --out ($rootI + '/report.json') `
    --driver $driver --instrument-parent ($rootI + '/instr') --label instrumented *>&1 | Tee-Object -FilePath ($rootI + '/console.log')
Write-Output ('INSTR_RC=' + $LASTEXITCODE)

Write-Output ('CANON_REPORT=' + (Test-Path ($rootC + '/report.json')))
Write-Output ('INSTR_REPORT=' + (Test-Path ($rootI + '/report.json')))
