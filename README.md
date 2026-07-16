# Power Grid Dataset Utilities

Tools for converting power grid datasets (Matpower, Excel, XIIDM) into clean, ML-ready formats with standardized graph representations.

**What's included:** CLI conversion tool • Python dataloaders • Validation scripts • Jupyter notebooks (see [notebooks/](notebooks/))

## Installation

Create a Python environment and install dependencies:

**Using venv:**
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**Using conda:**
```bash
conda create -n datasets-utils python=3.10
conda activate datasets-utils
pip install -r requirements.txt
```

## Setup: Configure Dataset Location

Before using the tools, specify where your raw datasets are stored. Choose one method:

**Option 1 (Recommended): Environment variable**
```bash
export DATASETS_ROOT=/path/to/your/datasets
```

**Option 2: Local config file**
Create `notebooks/.datasets_root` with a single line containing the path:
```
/path/to/your/datasets
```

**Option 3: Use default**
If neither is set, the tools will look for datasets at `/mnt/datadisk/data/datasets`

## Usage: Convert Datasets

Use the CLI tool to convert power grid datasets into standardized formats:

```bash
python scripts/convert.py \
  --from matpower \
  --to parquet \
  --input matpower/case.m \
  --output_dir data/processed/matpower_case/v1 \
  --include_only_in_service \
  --force
```

**Preview conversion without writing files:**
```bash
python scripts/convert.py \
  --from matpower \
  --to parquet \
  --input matpower/case.m \
  --dry_run
```

### Available Options

| Option | Description |
|--------|-------------|
| `--from` | Source format: `matpower`, `rte7000_opensynth` (`xiidm` alias) |
| `--to` | Target format: `parquet`, `json`, `csv`, `npz`, `pt`, `pickle`, `matpower` |
| `--input` | Path to input file (absolute or relative to `DATASETS_ROOT`) |
| `--dataset` | Optional dataset folder name under `DATASETS_ROOT` |
| `--output_dir` | Output directory (default: `data/processed`) |
| `--include_only_in_service` | Filter to in-service elements only |
| `--force` | Overwrite existing output |
| `--dry_run` | Show conversion plan without executing |

**Current limitations:**
- Supported `--to` values are processor-specific (for `rte7000_opensynth`, only `matpower` is valid)

## Output Structure

Successful conversion creates:

```
output_dir/
├── nodes.{parquet,json,csv}           # Node/bus data
├── edges.{parquet,json,csv}           # Branch/line tables/  
tables/                                # Source-specific tables
├── bus.{parquet,json,csv}
├── gen.{parquet,json,csv}
├── branch.{parquet,json,csv}
└── gencost.{parquet,json,csv}
├── metadata.json                      # Dataset metadata
└── manifest.json                      # Conversion manifest
```

**manifest.json** includes: source info, artifact paths, record counts, schema hash, metadata snapshot

## Validation

After conversion, validate power-flow feasibility of a MATPOWER `.m` case:

```bash
python validation/matpower/matpower_pf_soundness.py --input data/processed/matpower_case/case.m
```

**Checks:** parseable MATPOWER structure, finite/valid bus-gen-branch parameters, bus-numbering consistency, connected components with slack (REF) buses per island, and Ybus conditioning.

Dataloader-specific integrity checks (unique IDs, referential integrity, duplicate/empty detection) run automatically as part of `dataloaders/matpower_case.py` and `dataloaders/rte7000_opensynth.py` (see the notebooks under [notebooks/](notebooks/) for usage examples).

## Generation

Under `./src/generation/` there are Jupyter notebooks that facilitate synthetic dataset generation for different tasks:
- `andes_dse_generator.ipynb` is a notebook based on the ANDES python library, for synthetic dataset generation for the Dynamic State Estimation (DSE) task.

## Developer Notes

- Run all scripts from the repository root directory
- Dataloaders include deterministic fingerprinting for reproducibility tracking
- Manifest schema hashes are based on column names and types, not data values
