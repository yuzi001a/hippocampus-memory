$ErrorActionPreference='Continue'
if ($env:COMPUTERNAME -ne 'DESKTOP-EQP3OBU') { exit 91 }
$py='C:/hp-testbed/m03-derived-propagation-20261003/venv/Scripts/python.exe'
$base='C:/hp-testbed/p0c1-hermes-event-semantics-20261004'
$env:PGPASSWORD=(Get-Content '<TESTBED_SECRETS>/pgpw.tmp' -Raw).Trim()
Set-Location $base
& $py -B evidence/p0c1-hermes-event-semantics-20261004/p0c1_e2e.py --pg-host 127.0.0.1 --pg-port 55432 --pg-db p0c1e2e_20261004 --pg-user f2e2e --bootstrap --keep | Tee-Object -FilePath ($base+'/e2e-console.log')
$rc=$LASTEXITCODE
Write-Output ('DRIVER_RC='+$rc)
exit $rc
