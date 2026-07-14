# Firmware build matrix

Status: canonical table for local and GitHub Actions builds

The machine-readable source is [`ci/firmware_matrix.json`](../ci/firmware_matrix.json). The builder validates
that table before compiling. A push to `main`, including a merged pull request, runs
`.github/workflows/firmware-matrix.yml` and produces every supported binary.

## Parameters

| User option | Compiler definition | Supported values | Meaning |
| --- | --- | --- | --- |
| Load profile | `BMCU_P1S`, `BMCU_SOFT_LOAD` | table below | loading-force behavior |
| Autoload | `BMCU_DM_TWO_MICROSWITCH` | `0`, `1` | disable/enable dual-switch assisted autoload |
| Filament RGB | `BMCU_ONLINE_LED_FILAMENT_RGB` | `0`, `1` | front LED buffer-only or filament-color display |
| AMS address | `BAMBU_BUS_AMS_NUM` | `0..3` | AMS A through D |
| Retraction | `AMS_RETRACT_LEN` | target table below | retraction distance in metres |

## Load profiles

| Profile ID | User-facing folder | `BMCU_P1S` | `BMCU_SOFT_LOAD` | Intended use |
| --- | --- | ---: | ---: | --- |
| `standard` | `standard(A1)` | 0 | 0 | normal A1/A1 mini loading force |
| `high_force` | `high_force_load(P1S)` | 1 | 0 | long/high-friction PTFE path |
| `soft_load` | `soft_load(A1)` | 0 | 1 | reduced loading force |

`BMCU_P1S=1` together with `BMCU_SOFT_LOAD=1` is contradictory and is not built.

## Address and retraction targets

| Target | `BAMBU_BUS_AMS_NUM` | `AMS_RETRACT_LEN` | Count per feature shard |
| --- | ---: | --- | ---: |
| SOLO | 0 | `0.095f` | 1 |
| AMS A | 0 | `0.10f`, `0.20f`, then `0.25f..0.90f` in 0.05 m steps | 16 |
| AMS B | 1 | same 16 distances | 16 |
| AMS C | 2 | same 16 distances | 16 |
| AMS D | 3 | same 16 distances | 16 |
| Total | — | — | 65 |

The exact list is `0.10, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75,
0.80, 0.85, 0.90` metres. There is intentionally no `0.15` variant because the existing release set does not
define one.

## GitHub Actions shards

Each row is one parallel Actions job and produces 65 binaries.

| Profile | Autoload | Filament RGB | Binaries |
| --- | ---: | ---: | ---: |
| standard | 0 | 0 | 65 |
| standard | 0 | 1 | 65 |
| standard | 1 | 0 | 65 |
| standard | 1 | 1 | 65 |
| high_force | 0 | 0 | 65 |
| high_force | 0 | 1 | 65 |
| high_force | 1 | 0 | 65 |
| high_force | 1 | 1 | 65 |
| soft_load | 0 | 0 | 65 |
| soft_load | 0 | 1 | 65 |
| soft_load | 1 | 0 | 65 |
| soft_load | 1 | 1 | 65 |
| **Total** | — | — | **780** |

The workflow limits execution to six simultaneous jobs to reduce repeated toolchain downloads while retaining
parallelism. Each shard is independently downloadable for diagnostics. After all shards pass, the package job
downloads them, verifies every SHA-256/CRC32/size entry, and uploads one `firmware-all-<git-sha>` artifact.

## Artifact layout

```text
firmwares/
  standard|high_force|soft_load/
    autoload_on|autoload_off/
      filament_rgb_on|filament_rgb_off/
        solo/solo_0.095f.bin
        ams_a/ams_a_0.10f.bin ...
        ams_b/...
        ams_c/...
        ams_d/...
manifests/
  manifest-<profile>-autoload<0|1>-rgb<0|1>.json
manifest.json
manifest.csv
manifest.txt
guides/
```

Manifest rows contain every compiler option, relative path, size, SHA-256, CRC32, and source git SHA.

## Local commands

Validate and inspect the generated GitHub matrix without compiling:

```bash
python3 ci/firmware_matrix.py validate
python3 ci/firmware_matrix.py matrix
python3 ci/firmware_matrix.py list --profile standard --autoload 1 --rgb 0
```

Build one 65-binary shard:

```bash
python3 ci/firmware_matrix.py build \
  --profile standard --autoload 1 --rgb 0 --output firmware-build
```

Build all 780 supported variants locally:

```bash
./build_all_firmwares.sh firmware-build
```
