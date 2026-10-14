# Scope Trace Digitizer

Converts Keysight InfiniiVision oscilloscope screenshots into CSV data, with one file per channel in real time and voltage units.

**Live demo:** https://adsd1911.github.io/scope-trace-digitizer/

## Features

- Separates traces by colour (CH1–CH4 and Math) and extracts them column by column
- Reads the on-screen scales, delay, trigger level and measurements, and checks them against the extracted trace
- Processes images in batches and writes a summary table
- Lets you review each trace in the browser before exporting, and plot CSV files back into graphs

## Usage

**Browser:** open the demo link and drop in screenshots. Processing runs entirely in the browser (Pyodide + Tesseract.js), so no files are uploaded.

**Local:**

```bash
pip install -r requirements.txt
python3 scope_web.py                          # web interface at http://127.0.0.1:8737
python3 scope_digitizer.py path/to/images     # CSVs, check images and graphs
python3 scope_digitizer.py plot path/to/csvs  # CSV -> graphs
```

The local version reads on-screen text with macOS Vision. On other systems, pass the scales directly, e.g. `--ch1 500mV --time 2ms`.

## Output

Each trace is saved as `NAME_ch1_yellow.csv`:

```
x (s),y (V)
-0.00859375,0.015
-0.0085625,0.015
```

## License

MIT
