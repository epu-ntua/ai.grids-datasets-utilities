# GridFM-Datakit Repository 
Instructions for version 1.0.4.

## Installation Instructions

### 1. Clone the repository:
```bash
git clone https://github.com/gridfm/gridfm-datakit
cd gridfm-datakit
```

### 2. Create Python venv
Make sure you have Python 3.10, 3.11, or 3.12 installed. ⚠️ Windows users: Python 3.12 is not supported. Use Python 3.10.11 or 3.11.9.

To check your installed python versions, run:
```bash
py -0p
```

It should print the available interpreters, e.g.:
```bash
 -V:3.14 *        C:\Python314\python.exe
 -V:3.13          C:\Users\<you>\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\python.exe
 -V:3.11          C:\Users\<you>\AppData\Local\Programs\Python\Python311\python.exe
```

Then, create the environment and activate it.
- On Windows:
```bash
py -3.11 -m venv my_venv_name
my_venv_name/Scripts/activate
```

- On Linux/macOS (conda):
```bash
conda create -n my_venv_name python=3.10
conda activate my_venv_name
```

### 3. Install the repository on developer mode
```bash
pip3 install -e '.[test,dev]'
```

### 4. Set up julia
```bash
gridfm_datakit setup_pm
```

If it gets stuck in:
```bash
Precompiling packages finished.
  5 dependencies successfully precompiled in 27 seconds. 47 already precompiled.
     Project No packages added to or removed from `<conda_envs>/my_venv_name/julia_env/Project.toml`
    Manifest No packages added to or removed from `<conda_envs>/my_venv_name/julia_env/Manifest.toml`
   Resolving package versions...
  Installing artifacts ╺━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 0/10
```

It's probably stuck on installing the `PowerModels` and `Ipopt` packages. Open a new terminal and run (change the paths/`my_venv_name` to match your environment):
```bash
<conda_envs>/my_venv_name/julia_env/pyjuliapkg/install/bin/julia \
  --project=<conda_envs>/my_venv_name/julia_env \
  -e 'using Pkg; Pkg.add(["PowerModels","Ipopt"]); Pkg.precompile()'
```

Then rerun:
```bash
gridfm_datakit setup_pm
```

## Execution Instructions
To create synthetic datasets (once you're set up and with the venv activated), inspect the file:
```bash
./scripts/config/default.yaml
```

Make however changes you deem necessary. The `.yaml` itself has instructions. Execute from the repository's root:
```bash
gridfm_datakit generate ./scripts/config/default.yaml
```

Time to complete depends on the grid's size, the number of scenarios generated and the number of topology/admittance/generation perturbations generated. For reference, generating 10k scenarios with **no** perturbations whatsoever takes around 30-90 minutes on a multi-core server, depending on the grid's size.