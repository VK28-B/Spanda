# Spanda for Linux

This folder contains the packaging scaffold for Kubuntu/Plasma-like Linux
systems. The `.deb` packages are built on native Linux cloud runners because
PyInstaller cannot cross-compile Windows builds into Linux binaries.

No WSL installation is required, and nothing is downloaded or triggered until
you run the workflow yourself.

## Output files

After a successful workflow run, the artifacts contain:

- `spanda_3.0_amd64.deb`
- `spanda_3.0_arm64.deb`
- `spanda_3.0_amd64_portable.tar.gz`
- `spanda_3.0_arm64_portable.tar.gz`

Copy the `.deb` files into this folder on Windows:

```text
D:\My_Apps\SysAnalytics\linux\
```

## Build steps

1. Upload this project folder to a GitHub repository. If privacy matters, do
   **not** upload `Spanda.exe`, DuckDB database files, screenshots, or exports.
2. Open the repository on GitHub.
3. Go to **Actions** → **Build Linux .deb packages**.
4. Click **Run workflow** and confirm.
5. Wait for both `amd64` and `arm64` jobs to finish.
6. Download both artifacts and copy the `.deb` files into the `linux` folder.

The workflow is `workflow_dispatch` only, so it never runs automatically.

## Install on the target Linux system

For an Intel/AMD 64-bit machine:

```sh
sudo apt install ./spanda_3.0_amd64.deb
```

For an ARM64 machine:

```sh
sudo apt install ./spanda_3.0_arm64.deb
```

Then launch from the application menu or with:

```sh
spanda
```

## Graphical requirements

Spanda uses DearPyGUI/GLFW and needs X11 or XWayland. The launcher checks for
`DISPLAY`/`WAYLAND_DISPLAY`, tries to find an XWayland socket on Wayland-only
sessions, warns if OpenGL cannot be confirmed, and exits with a clear message
when no graphical session is available.

The package declares its runtime dependencies, including Mesa/OpenGL, X11/XCB,
fontconfig, `xdg-utils`, `procps`, `pciutils`, `util-linux`, `dmidecode`, and
`x11-xserver-utils`. `xwayland`, Mesa tools, the hicolor icon theme, and DejaVu
fonts are recommended.

## Portable fallback

If the custom OS cannot install `.deb` files, extract the portable archive and
run the bundled `spanda` binary from a graphical terminal:

```sh
tar -xzf spanda_3.0_amd64_portable.tar.gz
./spanda
```

Use the `arm64` archive on ARM64 hardware.

## Data location

Linux settings/data are stored under:

```text
${XDG_DATA_HOME:-~/.local/share}/Spanda/spanda.duckdb
```

CSV exports go to `~/Documents` unless `XDG_DOCUMENTS_HOME` is set.
