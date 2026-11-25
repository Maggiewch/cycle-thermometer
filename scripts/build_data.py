"""Bundle data/*.json into assets/data.js so the page also works when opened from disk.

Run after editing any file in data/:
    python3 scripts/build_data.py
"""
import json
import pathlib

root = pathlib.Path(__file__).resolve().parent.parent
data = {}
for f in sorted((root / "data").glob("*.json")):
    data[f.stem] = json.loads(f.read_text(encoding="utf-8"))

out = root / "assets" / "data.js"
out.write_text(
    "// Generated from data/*.json. Regenerate after updating the data files.\n"
    "window.CYC_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n",
    encoding="utf-8",
)
print(f"wrote {out} with {len(data)} datasets")
