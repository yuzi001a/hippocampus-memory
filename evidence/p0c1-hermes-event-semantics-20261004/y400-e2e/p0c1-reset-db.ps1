$ErrorActionPreference='Stop'
if ($env:COMPUTERNAME -ne 'DESKTOP-EQP3OBU') { exit 91 }
$py='C:/hp-testbed/m03-derived-propagation-20261003/venv/Scripts/python.exe'
$env:PGPASSWORD=(Get-Content '<TESTBED_SECRETS>/pgpw.tmp' -Raw).Trim()
& $py -B C:/hp-testbed/p0c1-hermes-event-semantics-20261004/p0c1_reset_db.py
exit $LASTEXITCODE
