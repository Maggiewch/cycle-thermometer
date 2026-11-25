"""Refresh every indicator that comes from FRED, then rebuild assets/data.js.

Usage:
    python3 scripts/update_fred.py            # fetch, update data/*.json, rebuild
    python3 scripts/update_fred.py --dry-run  # fetch and show what would change, write nothing

What it updates:
  * `value` and `asOf` of each FRED-based row in data/fred.json, data/detail.json
    and data/countries.json (rows listed in SPECS below);
  * the trend-chart series in data/history.json and data/intl_history.json
    (the most recent HISTORY_MONTHS months).

What it does not touch: rows from other sources (ISM, CAPE, margin debt, the
Bank of England, China's statistics bureau and so on), thresholds, `note` text
and data/country_view.json. Rows whose note quotes a number are listed at the
end so you can check them by hand.

Needs only the Python standard library and an internet connection. No API key.
"""

import argparse
import csv
import datetime as dt
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HISTORY_MONTHS = 30
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}"


# ---------------------------------------------------------------- fetching

_cache = {}


def _download(url):
    """FRED's CSV endpoint answers HTTP/2 requests but can stall on HTTP/1.1 ones,
    which is all urllib speaks, so curl (HTTP/2 by default) is tried first."""
    if shutil.which("curl"):
        r = subprocess.run(
            ["curl", "-sS", "--fail", "--compressed", "--connect-timeout", "15", "-m", "45",
             "--retry", "3", "--retry-delay", "2", "--retry-all-errors", url],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout:
            return r.stdout
        err = r.stderr.strip() or f"curl exit {r.returncode}"
    else:
        err = "curl not found"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        raise RuntimeError(f"{err}; urllib: {e}")


def fetch(sid):
    """Return [(date, value)] for a FRED series, oldest first, missing values dropped."""
    if sid in _cache:
        return _cache[sid]
    start = (dt.date.today() - dt.timedelta(days=365 * 5)).isoformat()
    url = FRED_CSV.format(sid=sid, start=start)
    last_err = None
    for attempt in range(2):
        try:
            text = _download(url)
            break
        except Exception as e:  # network hiccup: retry once
            last_err = e
            time.sleep(3)
    else:
        raise RuntimeError(f"could not download {sid}: {last_err}")
    rows = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or "date" not in header[0].lower():
        raise RuntimeError(f"{sid}: unexpected response (is the series id right?)")
    for d, v in reader:
        if v in ("", "."):
            continue
        rows.append((dt.date.fromisoformat(d), float(v)))
    if not rows:
        raise RuntimeError(f"{sid}: no data")
    _cache[sid] = rows
    return rows


def prefetch(sids, workers=8):
    """Download all series in parallel; failures are reported later, when a row needs them."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    sids = sorted(set(sids))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch, s): s for s in sids}
        for f in as_completed(futs):
            done += 1
            try:
                f.result()
                note = "ok"
            except Exception as e:
                note = f"failed ({e})"
            print(f"  [{done}/{len(sids)}] {futs[f]} {note}", flush=True)


# ---------------------------------------------------------------- helpers

def freq(rows):
    """'d' daily/weekly, 'm' monthly, 'q' quarterly, from the typical gap."""
    gaps = sorted((b[0] - a[0]).days for a, b in zip(rows[-13:], rows[-12:]))
    g = gaps[len(gaps) // 2]
    return "d" if g <= 8 else "m" if g <= 45 else "q"


def label(d, f):
    if f == "q":
        return f"{d.year} Q{(d.month - 1) // 3 + 1}"
    if f == "m":
        return d.strftime("%Y-%m")
    return d.isoformat()


def year_ago(rows, i, f):
    """Index of the observation one year before rows[i], or None."""
    d = rows[i][0]
    if f in ("m", "q"):
        target = d.replace(year=d.year - 1)
        for j in range(i - 1, -1, -1):
            if rows[j][0] == target:
                return j
            if rows[j][0] < target:
                return None
        return None
    target = d - dt.timedelta(days=365)
    best = None
    for j in range(i - 1, -1, -1):
        if rows[j][0] <= target:
            best = j
            break
    return best


def latest_with_year_ago(rows, f):
    """Latest observation that has a matching one a year earlier (skips gaps such as a missing month)."""
    for i in range(len(rows) - 1, max(-1, len(rows) - 6), -1):
        j = year_ago(rows, i, f)
        if j is not None:
            return i, j
    raise RuntimeError("no observation with a year-ago match")


# ---------------------------------------------------------------- calculations
# Each returns (value, asOf label).

def c_level(sid, nd=2):
    rows = fetch(sid)
    d, v = rows[-1]
    return round(v, nd), label(d, freq(rows))


def c_yoy(sid, nd=2):
    rows = fetch(sid)
    f = freq(rows)
    i, j = latest_with_year_ago(rows, f)
    return round(100 * (rows[i][1] / rows[j][1] - 1), nd), label(rows[i][0], "m" if f == "d" else f)


def c_chg1y(sid, nd=2):
    rows = fetch(sid)
    f = freq(rows)
    i, j = latest_with_year_ago(rows, f)
    return round(rows[i][1] - rows[j][1], nd), label(rows[i][0], f)


def c_chg_n(sid, n, nd=2):
    rows = fetch(sid)
    return round(rows[-1][1] - rows[-1 - n][1], nd), label(rows[-1][0], freq(rows))


def c_ann(sid, n, nd=2):
    """Growth over the last n periods, annualised (n=3 monthly -> 3-month annualised; n=1 quarterly -> q/q annualised)."""
    rows = fetch(sid)
    f = freq(rows)
    per_year = 12 if f == "m" else 4
    g = (rows[-1][1] / rows[-1 - n][1]) ** (per_year / n) - 1
    return round(100 * g, nd), label(rows[-1][0], f)


def c_drawdown(sid, nd=1):
    rows = fetch(sid)
    d, v = rows[-1]
    peak = max(x for (dd, x) in rows if dd > d - dt.timedelta(days=365))
    return round(100 * (v / peak - 1), nd), d.isoformat()


def c_avg_last(sid, n, scale=1.0, nd=1):
    rows = fetch(sid)
    vals = [v for _, v in rows[-n:]]
    return round(sum(vals) / len(vals) / scale, nd), rows[-1][0].isoformat()


def c_real_rate():
    ff, ff_at = c_level("FEDFUNDS")
    cpi, _ = c_yoy("CPIAUCSL")
    return round(ff - cpi, 2), ff_at


def _mk(fn, sid, *a, **k):
    f = lambda: fn(sid, *a, **k)
    f.sids = [sid]
    return f


c_real_rate.sids = ["FEDFUNDS", "CPIAUCSL"]
L = lambda sid, **k: _mk(c_level, sid, **k)
Y = lambda sid, **k: _mk(c_yoy, sid, **k)
C1 = lambda sid, **k: _mk(c_chg1y, sid, **k)
CN = lambda sid, n, **k: _mk(c_chg_n, sid, n, **k)
A = lambda sid, n, **k: _mk(c_ann, sid, n, **k)
DD = lambda sid, **k: _mk(c_drawdown, sid, **k)

# (file, row key) -> calculation
SPECS = {
    # United States, detail (data/fred.json)
    ("fred.json", "m2_yoy"): Y("M2SL"),
    ("fred.json", "busloans_yoy"): Y("BUSLOANS"),
    ("fred.json", "bankcredit_yoy"): Y("TOTBKCR"),
    ("fred.json", "real_ffr"): c_real_rate,
    ("fred.json", "nfci"): L("NFCI", nd=3),
    ("fred.json", "pnfi_yoy"): Y("PNFI"),
    ("fred.json", "hpi_yoy"): Y("CSUSHPINSA"),
    ("fred.json", "busloans_3m"): A("BUSLOANS", 3),
    ("fred.json", "ff_6m"): CN("FEDFUNDS", 6),
    ("fred.json", "dgs10"): L("DGS10"),
    ("fred.json", "hy_spread"): L("BAMLH0A0HYM2"),
    ("fred.json", "sloos"): L("DRTSCILM", nd=1),
    ("fred.json", "curve"): L("T10Y3M"),
    ("fred.json", "sahm"): L("SAHMREALTIME"),
    ("fred.json", "claims"): _mk(c_avg_last, "ICSA", 4, scale=1000),
    ("fred.json", "unrate"): L("UNRATE", nd=1),
    ("fred.json", "gdp"): A("GDPC1", 1),
    ("fred.json", "payrolls_yoy"): Y("PAYEMS"),
    ("fred.json", "card_dq"): L("DRCCLACBS"),
    ("fred.json", "sentiment"): L("UMCSENT", nd=1),
    ("fred.json", "cpi_yoy"): Y("CPIAUCSL"),
    ("fred.json", "cpi_3m"): A("CPIAUCSL", 3),
    ("fred.json", "core_pce"): Y("PCEPILFE"),
    ("fred.json", "ppi_yoy"): Y("PPIACO"),
    # United States, shared indicators (data/detail.json)
    ("detail.json", "us_credit_chg"): C1("QUSPAM770A", nd=1),
    ("detail.json", "us_credit_level"): L("QUSPAM770A", nd=1),
    ("detail.json", "us_yield1y"): C1("DGS10"),
    ("detail.json", "us_drawdown"): DD("SP500"),
    ("detail.json", "us_unemp1y"): C1("UNRATE", nd=1),
    ("detail.json", "us_cli"): L("USALOLITOAASTSAM"),
    # United Kingdom
    ("detail.json", "uk_credit_chg"): C1("QGBPAM770A", nd=1),
    ("detail.json", "uk_credit_level"): L("QGBPAM770A", nd=1),
    ("detail.json", "uk_house"): Y("QGBN628BIS"),
    ("detail.json", "uk_gilt"): L("IRLTLT01GBM156N"),
    ("detail.json", "uk_yield1y"): C1("IRLTLT01GBM156N"),
    ("detail.json", "uk_gbp"): Y("DEXUSUK", nd=1),
    ("detail.json", "uk_unemp"): L("LRHUTTTTGBM156S", nd=1),
    ("detail.json", "uk_unemp1y"): C1("LRHUTTTTGBM156S", nd=1),
    ("detail.json", "uk_cli"): L("GBRLOLITOAASTSAM"),
    # Japan
    ("detail.json", "jp_credit_chg"): C1("QJPPAM770A", nd=1),
    ("detail.json", "jp_credit_level"): L("QJPPAM770A", nd=1),
    ("detail.json", "jp_house"): Y("QJPN628BIS"),
    ("detail.json", "jp_rate6m"): CN("IRSTCI01JPM156N", 6),
    ("detail.json", "jp_jgb"): L("IRLTLT01JPM156N", nd=3),
    ("detail.json", "jp_yield1y"): C1("IRLTLT01JPM156N"),
    ("detail.json", "jp_drawdown"): DD("NIKKEI225"),
    ("detail.json", "jp_yen"): L("DEXJPUS"),
    ("detail.json", "jp_gdp"): Y("JPNRGDPEXP"),
    ("detail.json", "jp_gdp_q"): A("JPNRGDPEXP", 1),
    ("detail.json", "jp_unemp"): L("LRHUTTTTJPM156S", nd=1),
    ("detail.json", "jp_unemp1y"): C1("LRHUTTTTJPM156S", nd=1),
    ("detail.json", "jp_cli"): L("JPNLOLITOAASTSAM"),
    # China
    ("detail.json", "cn_credit_chg"): C1("QCNPAM770A", nd=1),
    ("detail.json", "cn_credit_level"): L("QCNPAM770A", nd=1),
    ("detail.json", "cn_house"): Y("QCNN628BIS"),
    ("detail.json", "cn_cli"): L("CHNLOLITOAASTSAM"),
    # Shared comparison table (data/countries.json)
    ("countries.json", "us_credit_chg"): C1("QUSPAM770A", nd=1),
    ("countries.json", "us_credit_level"): L("QUSPAM770A", nd=1),
    ("countries.json", "us_house"): Y("QUSN628BIS"),
    ("countries.json", "us_policy6m"): CN("FEDFUNDS", 6),
    ("countries.json", "us_yield1y"): C1("DGS10"),
    ("countries.json", "us_drawdown"): DD("SP500"),
    ("countries.json", "us_gdp"): Y("GDPC1"),
    ("countries.json", "us_unemp1y"): C1("UNRATE", nd=1),
    ("countries.json", "us_cli"): L("USALOLITOAASTSAM"),
    ("countries.json", "us_cpi"): Y("CPIAUCSL"),
    ("countries.json", "us_ppi"): Y("PPIFIS"),
    ("countries.json", "uk_credit_chg"): C1("QGBPAM770A", nd=1),
    ("countries.json", "uk_credit_level"): L("QGBPAM770A", nd=1),
    ("countries.json", "uk_house"): Y("QGBN628BIS"),
    ("countries.json", "uk_yield1y"): C1("IRLTLT01GBM156N"),
    ("countries.json", "uk_unemp1y"): C1("LRHUTTTTGBM156S", nd=1),
    ("countries.json", "uk_cli"): L("GBRLOLITOAASTSAM"),
    ("countries.json", "jp_credit_chg"): C1("QJPPAM770A", nd=1),
    ("countries.json", "jp_credit_level"): L("QJPPAM770A", nd=1),
    ("countries.json", "jp_house"): Y("QJPN628BIS"),
    ("countries.json", "jp_policy6m"): CN("IRSTCI01JPM156N", 6),
    ("countries.json", "jp_yield1y"): C1("IRLTLT01JPM156N"),
    ("countries.json", "jp_drawdown"): DD("NIKKEI225"),
    ("countries.json", "jp_gdp"): Y("JPNRGDPEXP"),
    ("countries.json", "jp_unemp1y"): C1("LRHUTTTTJPM156S", nd=1),
    ("countries.json", "jp_cli"): L("JPNLOLITOAASTSAM"),
    ("countries.json", "cn_credit_chg"): C1("QCNPAM770A", nd=1),
    ("countries.json", "cn_credit_level"): L("QCNPAM770A", nd=1),
    ("countries.json", "cn_house"): Y("QCNN628BIS"),
    ("countries.json", "cn_cli"): L("CHNLOLITOAASTSAM"),
}


# ---------------------------------------------------------------- trend-chart history
# series name -> (FRED id, how): 'level' monthly/quarterly value, 'avg' monthly average
# of a daily/weekly series, 'yoy' year-on-year %, 'yoy_mlast' year-on-year of the last
# value in each month (weekly series).

HISTORY = {
    "history.json": {
        "m2_yoy": ("M2SL", "yoy"), "busloans_yoy": ("BUSLOANS", "yoy"),
        "bankcredit_yoy": ("TOTBKCR", "yoy_mlast"), "cpi_yoy": ("CPIAUCSL", "yoy"),
        "fedfunds": ("FEDFUNDS", "level"), "dgs10": ("DGS10", "avg"), "hy_spread": ("BAMLH0A0HYM2", "avg"),
        "t10y3m": ("T10Y3M", "avg"), "claims": ("ICSA", "avg_k"), "sahm": ("SAHMREALTIME", "level"),
        "unrate": ("UNRATE", "level"), "nfci": ("NFCI", "avg"), "sentiment": ("UMCSENT", "level"),
    },
    "intl_history.json": {
        "uk_gilt": ("IRLTLT01GBM156N", "level"), "uk_unemp": ("LRHUTTTTGBM156S", "level"),
        "uk_cli": ("GBRLOLITOAASTSAM", "level"), "uk_gbp": ("DEXUSUK", "avg"),
        "uk_house": ("QGBN628BIS", "yoy"), "uk_credit": ("QGBPAM770A", "level"),
        "jp_jgb": ("IRLTLT01JPM156N", "level"), "jp_call": ("IRSTCI01JPM156N", "level"),
        "jp_unemp": ("LRHUTTTTJPM156S", "level"), "jp_cli": ("JPNLOLITOAASTSAM", "level"),
        "jp_yen": ("DEXJPUS", "avg"), "jp_nikkei": ("NIKKEI225", "avg"),
        "jp_house": ("QJPN628BIS", "yoy"), "jp_credit": ("QJPPAM770A", "level"), "jp_gdp": ("JPNRGDPEXP", "yoy"),
        "cn_cli": ("CHNLOLITOAASTSAM", "level"), "cn_house": ("QCNN628BIS", "yoy"),
        "cn_credit": ("QCNPAM770A", "level"), "cn_cny": ("DEXCHUS", "avg"), "cn_ib3m": ("IR3TIB01CNM156N", "level"),
    },
}


def monthly(rows, how):
    """Collapse a series to {YYYY-MM: value}."""
    buckets = {}
    for d, v in rows:
        buckets.setdefault(d.strftime("%Y-%m"), []).append(v)
    if how in ("avg", "avg_k"):
        out = {k: sum(v) / len(v) for k, v in buckets.items()}
        if how == "avg_k":
            out = {k: v / 1000 for k, v in out.items()}
        return out
    return {k: v[-1] for k, v in buckets.items()}


def history_series(sid, how):
    rows = fetch(sid)
    if how in ("yoy", "yoy_mlast"):
        m = monthly(rows, "level")
        out = {}
        for k, v in m.items():
            y, mo = k.split("-")
            prev = m.get(f"{int(y) - 1}-{mo}")
            if prev:
                out[k] = round(100 * (v / prev - 1), 2)
        return out
    nd = 0 if how == "avg_k" else 3
    return {k: round(v, nd) for k, v in monthly(rows, how).items()}


def history_start():
    t = dt.date.today()
    y, m = t.year, t.month - HISTORY_MONTHS
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y}-{m:02d}"


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="show changes without writing files")
    args = ap.parse_args()

    sids = [s for calc in SPECS.values() for s in getattr(calc, "sids", [])]
    sids += [sid for spec in HISTORY.values() for sid, _ in spec.values()]
    print(f"Downloading {len(set(sids))} series from FRED ...", flush=True)
    prefetch(sids)

    files = {name: json.loads((DATA / name).read_text(encoding="utf-8")) for name in ("fred.json", "detail.json", "countries.json")}
    changes, failures, missing = [], [], []

    for (fname, key), calc in SPECS.items():
        row = next((r for r in files[fname] if r.get("key") == key), None)
        if row is None:
            missing.append(f"{fname}:{key}")
            continue
        try:
            value, as_of = calc()
        except Exception as e:
            failures.append(f"{fname}:{key}: {e}")
            continue
        if row.get("value") != value or row.get("asOf") != as_of:
            changes.append((fname, key, row.get("value"), row.get("asOf"), value, as_of, row.get("note", "")))
            row["value"], row["asOf"] = value, as_of

    start = history_start()
    hist_out, hist_fail = {}, []
    for fname, spec in HISTORY.items():
        rows = []
        for name, (sid, how) in spec.items():
            try:
                for k, v in sorted(history_series(sid, how).items()):
                    if k >= start:
                        rows.append({"series": name, "date": k, "value": v})
            except Exception as e:
                hist_fail.append(f"{fname}:{name}: {e}")
        hist_out[fname] = rows

    print(f"\n{len(changes)} value(s) changed:")
    for fname, key, ov, oa, nv, na, _ in changes:
        print(f"  {fname:15} {key:18} {str(ov):>10} ({oa})  ->  {str(nv):>10} ({na})")
    if failures or hist_fail:
        print("\nCould not update (left as they were):")
        for f in failures + hist_fail:
            print("  " + f)
    if missing:
        print("\nRows not found in the data files: " + ", ".join(missing))

    stale_notes = [(f, k, n) for f, k, *_, n in changes if re.search(r"\d", n or "")]
    if stale_notes:
        print("\nThese notes quote numbers and may need editing by hand:")
        for f, k, n in stale_notes:
            print(f"  {f}:{k}  “{n}”")

    print("\nNot from FRED, update by hand: ISM and other PMIs, CAPE, margin debt, AI capex,"
          " UK CPI/GDP/Bank Rate/FTSE, Japan CPI/CGPI/PMI, all China monthly data except the"
          " OECD leading indicator, and data/country_view.json.")

    if args.dry_run:
        print("\nDry run: nothing written.")
        return

    for name, rows in files.items():
        (DATA / name).write_text(json.dumps(rows, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    for name, rows in hist_out.items():
        if rows:
            (DATA / name).write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")

    sys.path.insert(0, str(ROOT / "scripts"))
    import build_data  # noqa: F401  (rebuilds assets/data.js on import)
    print("Done. Open index.html to check, then commit and push.")


if __name__ == "__main__":
    main()
