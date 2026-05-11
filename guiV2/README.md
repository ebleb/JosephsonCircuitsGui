# Circuit Project GUI V2

Native PyQt6 version of the circuit project editor.

Run from the repository root:

```bash
python3 guiV2/main.py
```

This app uses native file dialogs and writes directly to project folders. A
project folder contains `project.json` plus one JSON file per local cell.

Current coverage:

- native new/open/save project folder workflow,
- project explorer for local cells, imported project references, built-ins, and results,
- resizable panels using `QSplitter`,
- schematic canvas with block placement, moving, port wiring, exported pins, and text labels,
- cell and block inspector editing,
- built-in catalog loaded from `logic/built-in`,
- pipeline JSON export compatible with `logic/run_pipeline.sh`,
- direct simulation run through `QProcess` with stdout/stderr in the bottom panel.

Install PyQt6 if needed:

```bash
python3 -m pip install PyQt6
```
