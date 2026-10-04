$ErrorActionPreference='Stop'
if ($env:COMPUTERNAME -ne 'DESKTOP-EQP3OBU') { exit 91 }
$py='C:/hp-testbed/m03-derived-propagation-20261003/venv/Scripts/python.exe'
$base='C:/hp-testbed/p0c1-hermes-event-semantics-20261004'
$ev=$base+'/evidence/p0c1-hermes-event-semantics-20261004'
$env:PGPASSWORD=(Get-Content '<TESTBED_SECRETS>/pgpw.tmp' -Raw).Trim()
& $py -B $base/p0c1_verify.py ($ev+'/p0c1-db-groundtruth.json')
exit $LASTEXITCODE
