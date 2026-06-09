# launch_qpm.ps1 — Launch Squid for the QPM / SciMicroscopy dome setup.
#
# Why this exists: the machine config is chosen by file presence, not a CLI flag
# (see ConfigRepository.get_machine_config). This script makes the dome config the
# *explicit* active config (machine_configs/machine_config.yaml, resolver rule #1)
# every launch, so the backend is deterministic regardless of what else is in the
# folder or what the (unused) cache says.
#
# Reversible: delete machine_configs/machine_config.yaml to return to the implicit
# single-file behavior; delete this script to remove the wrapper entirely.

$ErrorActionPreference = "Stop"

$repo    = "C:\Code\CephlaSquidSandbox\software"
$cfgDir  = Join-Path $repo "machine_configs"
$domeCfg = Join-Path $cfgDir "machine_config_SciMicroscopyLED_CoolLED_Tucsen_test.yaml"
$active  = Join-Path $cfgDir "machine_config.yaml"
$python  = "C:\Users\jialab\.conda\envs\squid\python.exe"
$profile = "Heeseok"   # QPM project profile; does NOT affect machine config

if (-not (Test-Path $domeCfg)) { throw "Dome config not found: $domeCfg" }

# Assert the dome config as the explicit active config (resolver rule #1).
Copy-Item -Force $domeCfg $active
Write-Host "[launch_qpm] Active machine config -> SciMicroscopy dome:" (Split-Path $domeCfg -Leaf) -ForegroundColor Green

# Launch in the squid env with a pinned profile (skips the profile dialog).
Set-Location $repo
& $python main_hcs.py --profile $profile @args
