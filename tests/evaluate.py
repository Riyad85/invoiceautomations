#!/usr/bin/env python3
"""Evaluation of the Document Intelligence system on the labeled dataset.

Uploads every file listed in labels.json to the LIVE API, waits for the final status,
compares the result with the ground truth and writes REAL measured numbers:
  - extraction accuracy (per field, per file type, whole-document)
  - hallucinations (a value was given where the truth is "not present")
  - line-item recall / precision
  - processing time (mean, median, p90, max), measured end-to-end by the client
  - success rate / failure rate / correct handling of corrupt files
  - validation-flag detection (planted errors + duplicate)

Usage (PowerShell):
    $env:DI_API_KEY = "your-upload-key"
    py evaluate.py --dataset "C:\\Users\\Riyad\\Downloads\\test_dataset\\dataset"

Outputs (in ./results): evaluation_report.md, evaluation_results.json, evaluation_per_file.csv
"""
import argparse
import csv
import datetime as dt
import json
import os
import pathlib
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

import requests

BASE = os.environ.get("DI_BASE_URL", "https://assignment02.app.n8n.cloud/webhook").rstrip("/")
KEY = os.environ.get("DI_API_KEY", "")
FIELDS = ["invoice_number", "invoice_date", "supplier_name", "customer_name",
          "subtotal", "tax", "total", "currency"]
NUM_FIELDS = {"subtotal", "tax", "total"}
FINAL = {"completed", "needs_review", "failed"}
TOL = 0.011
# Files that must be uploaded only after their original has finished (duplicate detection
# compares against documents that already exist).
SERIAL_AFTER = {"dup_resend.jpg"}
KNOWN_CODES = {"no_extraction", "missing_field", "no_line_items", "not_a_number", "negative_value",
               "zero_total", "total_mismatch", "line_total_mismatch", "line_sum_mismatch",
               "duplicate_invoice", "low_confidence"}
MIME = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "tif": "image/tiff", "tiff": "image/tiff", "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xls": "application/vnd.ms-excel"}
lock = threading.Lock()


# ----------------------------------------------------------------- scoring
def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower()) if s is not None else ""


def to_num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def field_correct(field, truth, got):
    if got == "":
        got = None
    if truth is None:
        return got is None            # truth says "not present": any value is invented
    if got is None:
        return False
    if field in NUM_FIELDS:
        a, b = to_num(truth), to_num(got)
        return a is not None and b is not None and abs(a - b) <= TOL
    if field == "invoice_date":
        return str(got)[:10] == str(truth)[:10]
    if field == "currency":
        return str(got).upper() == str(truth).upper()
    return norm(got) == norm(truth)


def match_items(truth_items, got_items):
    """Greedy one-to-one matching. A line matches when every number the truth gives is equal
    (within 0.01) and the descriptions are similar (>= 0.6)."""
    used, matched = set(), 0
    for t in truth_items:
        best, best_s = None, 0.0
        for i, g in enumerate(got_items):
            if i in used:
                continue
            ok = True
            for k in ("quantity", "unit_price", "line_total"):
                if t.get(k) is not None:
                    tv, gv = to_num(t[k]), to_num(g.get(k))
                    if gv is None or tv is None or abs(gv - tv) > TOL:
                        ok = False
                        break
            if not ok:
                continue
            s = SequenceMatcher(None, norm(t.get("description")), norm(g.get("description"))).ratio()
            if s >= 0.6 and s > best_s:
                best, best_s = i, s
        if best is not None:
            used.add(best)
            matched += 1
    return matched


def score_invoice(truth, inv):
    """truth: dict from labels.json. inv: first invoice of /api-result, or None."""
    out = {"fields": {}, "hallucinations": 0, "no_invoice": inv is None,
           "li_truth": 0, "li_got": 0, "li_matched": 0}
    for f in FIELDS:
        t = truth.get(f)
        if inv is None:
            ok, g = False, None            # nothing was extracted: every field counts as wrong
        else:
            g = inv.get(f)
            ok = field_correct(f, t, g)
            if t is None and g not in (None, ""):
                out["hallucinations"] += 1
        out["fields"][f] = {"ok": ok, "truth": t, "got": g}
    if "line_items" in truth:
        t_items = truth.get("line_items") or []
        g_items = (inv.get("line_items") if inv else None) or []
        out["li_truth"], out["li_got"] = len(t_items), len(g_items)
        out["li_matched"] = match_items(t_items, g_items)
    out["perfect"] = all(v["ok"] for v in out["fields"].values()) and \
        out["li_matched"] == out["li_truth"] == out["li_got"]
    return out


def toks(*parts):
    text = " ".join(str(p) for p in parts if p).lower().replace("_", " ")
    return set(re.findall(r"[a-z0-9]+", text))


def flag_matches(expected, actual):
    exp = expected.get("code") if isinstance(expected, dict) else str(expected)
    code = str(actual.get("code") or "").lower()
    e_norm = re.sub(r"[^a-z0-9]+", "_", str(exp).lower()).strip("_")
    if e_norm in KNOWN_CODES:
        return code == e_norm                       # exact rule name given: require that rule
    return toks(exp) <= toks(code, actual.get("field"))   # e.g. "missing_date" -> missing_field on invoice_date


def score_flags(expected, actual_errors):
    actual = [e for e in (actual_errors or []) if not e.get("resolved")]
    missing = [e for e in expected if not any(flag_matches(e, a) for a in actual)]
    found = [e for e in expected if e not in missing]
    unexpected = [a for a in actual if (a.get("severity") == "error")
                  and not any(flag_matches(e, a) for e in expected)]
    warnings = [a for a in actual if a.get("severity") != "error"]
    return {"found": found, "missing": missing, "unexpected_errors": unexpected, "warnings": warnings}


# --------------------------------------------------------------- API calls
def hdr():
    return {"apikey": KEY}


def api_get(path, **params):
    return requests.get(BASE + path, headers=hdr(), params=params, timeout=60)


def run_file(entry, dataset, timeout, poll=2.0):
    name = entry["file"]
    rec = {"file": name, "type": entry.get("type"), "expected_status": entry.get("expected_status"),
           "final_status": None, "seconds": None, "error": None, "doc_id": None, "result": None}
    path = dataset / name
    if not path.exists():
        rec["error"], rec["final_status"] = "file missing from dataset folder", "missing_file"
        return rec
    data = path.read_bytes()
    t0 = time.time()
    try:
        # The part MUST carry a Content-Type. Without it n8n's multipart parser treats the part as a
        # plain text field (body.file) instead of a binary file, and the API answers "No file received".
        ctype = MIME.get(path.suffix.lower().lstrip("."), "application/octet-stream")
        r = requests.post(BASE + "/invoice-upload", headers=hdr(), files={"file": (name, data, ctype)}, timeout=120)
    except Exception as e:
        rec["error"], rec["final_status"] = f"upload error: {e}", "upload_rejected"
        return rec
    if r.status_code != 202:
        rec["error"], rec["final_status"] = f"upload HTTP {r.status_code}: {r.text[:150]}", "upload_rejected"
        return rec
    rec["doc_id"] = r.json()["document_id"]
    while time.time() - t0 < timeout:
        try:
            s = api_get("/api-document", id=rec["doc_id"])
            if s.status_code == 200 and s.json()["status"] in FINAL:
                rec["final_status"] = s.json()["status"]
                rec["seconds"] = round(time.time() - t0, 1)
                rec["error"] = s.json().get("error_message")
                break
        except Exception:
            pass                                   # transient network problem: keep polling
        time.sleep(poll)
    else:
        rec["final_status"], rec["error"] = "timeout", f"not finished after {timeout}s"
        return rec
    for _ in range(3):
        try:
            res = api_get("/api-result", id=rec["doc_id"])
            if res.status_code == 200:
                rec["result"] = res.json()
                break
        except Exception:
            time.sleep(2)
    return rec


# ------------------------------------------------------------------ stats
def pct(a, b):
    return None if not b else round(100.0 * a / b, 1)


def fmt(v, unit="%"):
    return "n/a" if v is None else f"{v}{unit}"


def percentile(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return round(s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))], 1)


def summarise(entries, recs):
    by_file = {r["file"]: r for r in recs}
    per_file, field_tot, field_ok = [], {f: 0 for f in FIELDS}, {f: 0 for f in FIELDS}
    type_tot, type_ok, type_docs, type_perfect = {}, {}, {}, {}
    halluc = li_t = li_g = li_m = perfect = n_eval = 0
    times, times_ok = [], []
    flag_exp = flag_found = false_alarm_docs = clean_docs = 0

    for e in entries:
        r = by_file[e["file"]]
        exp_status = e.get("expected_status")
        row = {"file": e["file"], "type": e.get("type"), "expected_status": exp_status,
               "final_status": r["final_status"], "seconds": r["seconds"],
               "status_match": r["final_status"] == exp_status, "wrong_fields": "",
               "flags_expected": "", "flags_missing": "", "flags_unexpected_errors": "",
               "warnings": "", "error_message": r["error"] or ""}
        if r["seconds"] is not None:
            times.append(r["seconds"])
            if r["final_status"] != "failed":
                times_ok.append(r["seconds"])
        result = r["result"] or {}
        inv = (result.get("invoices") or [None])[0]
        truth = e.get("truth") or {}
        if exp_status != "failed" and truth:
            n_eval += 1
            sc = score_invoice(truth, inv)
            t = e.get("type") or "unknown"
            type_docs[t] = type_docs.get(t, 0) + 1
            for f, v in sc["fields"].items():
                field_tot[f] += 1
                type_tot[t] = type_tot.get(t, 0) + 1
                if v["ok"]:
                    field_ok[f] += 1
                    type_ok[t] = type_ok.get(t, 0) + 1
            wrong = [f"{f} (truth={v['truth']!r}, got={v['got']!r})" for f, v in sc["fields"].items() if not v["ok"]]
            row["wrong_fields"] = "; ".join(wrong)
            halluc += sc["hallucinations"]
            li_t += sc["li_truth"]; li_g += sc["li_got"]; li_m += sc["li_matched"]
            if sc["perfect"]:
                perfect += 1
                type_perfect[t] = type_perfect.get(t, 0) + 1
            row["li"] = f"{sc['li_matched']}/{sc['li_truth']} matched, {sc['li_got']} extracted"
        expected_flags = e.get("expected_flags") or []
        fl = score_flags(expected_flags, result.get("validation_errors"))
        row["flags_expected"] = ", ".join(map(str, expected_flags))
        row["flags_missing"] = ", ".join(map(str, fl["missing"]))
        row["flags_unexpected_errors"] = ", ".join(f"{a.get('code')}:{a.get('field')}" for a in fl["unexpected_errors"])
        row["warnings"] = ", ".join(f"{a.get('code')}:{a.get('field')}" for a in fl["warnings"])
        if expected_flags:
            flag_exp += len(expected_flags)
            flag_found += len(fl["found"])
        elif exp_status != "failed":
            clean_docs += 1
            if fl["unexpected_errors"]:
                false_alarm_docs += 1
        per_file.append(row)

    ok_docs = [e for e in entries if e.get("expected_status") != "failed"]
    bad_docs = [e for e in entries if e.get("expected_status") == "failed"]
    unexpected_fail = [e["file"] for e in ok_docs if by_file[e["file"]]["final_status"] in
                       ("failed", "timeout", "upload_rejected", "missing_file")]
    correctly_failed = [e["file"] for e in bad_docs if by_file[e["file"]]["final_status"] == "failed"]
    status_match = sum(1 for p in per_file if p["status_match"])
    return {
        "files_total": len(entries), "files_expected_to_process": len(ok_docs),
        "success_rate_pct": pct(len(ok_docs) - len(unexpected_fail), len(ok_docs)),
        "failure_rate_pct": pct(len(unexpected_fail), len(ok_docs)),
        "unexpected_failures": unexpected_fail,
        "corrupt_files_total": len(bad_docs), "corrupt_files_handled_correctly": len(correctly_failed),
        "status_match_pct": pct(status_match, len(entries)), "status_match": status_match,
        "field_accuracy_pct": pct(sum(field_ok.values()), sum(field_tot.values())),
        "field_correct": sum(field_ok.values()), "field_total": sum(field_tot.values()),
        "per_field_pct": {f: pct(field_ok[f], field_tot[f]) for f in FIELDS},
        "per_type": {t: {"docs": type_docs[t], "field_accuracy_pct": pct(type_ok.get(t, 0), type_tot[t]),
                         "perfect_docs": type_perfect.get(t, 0)} for t in type_docs},
        "docs_evaluated": n_eval, "docs_perfect": perfect, "docs_perfect_pct": pct(perfect, n_eval),
        "hallucinated_values": halluc,
        "line_item_recall_pct": pct(li_m, li_t), "line_item_precision_pct": pct(li_m, li_g),
        "line_items_truth": li_t, "line_items_extracted": li_g, "line_items_matched": li_m,
        "time_seconds": {"n": len(times), "mean": round(statistics.mean(times), 1) if times else None,
                         "median": round(statistics.median(times), 1) if times else None,
                         "p90": percentile(times, 90), "max": max(times) if times else None,
                         "min": min(times) if times else None,
                         "mean_excluding_failed": round(statistics.mean(times_ok), 1) if times_ok else None},
        "flags": {"expected": flag_exp, "found": flag_found, "recall_pct": pct(flag_found, flag_exp),
                  "clean_docs": clean_docs, "clean_docs_with_false_error_flags": false_alarm_docs,
                  "false_alarm_pct": pct(false_alarm_docs, clean_docs)},
    }, per_file


def write_report(path, s, per_file, meta):
    t = s["time_seconds"]
    L = []
    a = L.append
    a("# Evaluation report\n")
    a(f"Run: {meta['when']} | Files: {s['files_total']} | Concurrency: {meta['concurrency']} | "
      f"Database empty before run: {'yes' if meta['clean'] else 'NO (duplicate flags may be affected)'}\n")
    a("All numbers below were measured by `evaluate.py` against the live system. Nothing was edited by hand.\n")
    a("## Headline numbers\n")
    a("| Metric | Result |\n|---|---|")
    a(f"| Field-level extraction accuracy | {fmt(s['field_accuracy_pct'])} ({s['field_correct']}/{s['field_total']} fields) |")
    a(f"| Documents with every field and line item correct | {fmt(s['docs_perfect_pct'])} ({s['docs_perfect']}/{s['docs_evaluated']}) |")
    a(f"| Invented values (truth = not present, system gave a value) | {s['hallucinated_values']} |")
    a(f"| Line-item recall / precision | {fmt(s['line_item_recall_pct'])} / {fmt(s['line_item_precision_pct'])} ({s['line_items_matched']} of {s['line_items_truth']} true lines matched, {s['line_items_extracted']} extracted) |")
    a(f"| Processing success rate (files that should process) | {fmt(s['success_rate_pct'])} |")
    a(f"| Failure rate (files that should process but failed) | {fmt(s['failure_rate_pct'])} |")
    a(f"| Corrupt files handled correctly (marked failed, no invented data) | {s['corrupt_files_handled_correctly']}/{s['corrupt_files_total']} |")
    a(f"| Final status equals expected status | {fmt(s['status_match_pct'])} ({s['status_match']}/{s['files_total']}) |")
    a(f"| Planted-error / duplicate flags detected | {fmt(s['flags']['recall_pct'])} ({s['flags']['found']}/{s['flags']['expected']}) |")
    a(f"| Clean documents wrongly given an error flag | {fmt(s['flags']['false_alarm_pct'])} ({s['flags']['clean_docs_with_false_error_flags']}/{s['flags']['clean_docs']}) |")
    a(f"| Processing time, mean / median / p90 / max | {t['mean']} / {t['median']} / {t['p90']} / {t['max']} seconds |\n")
    a("## Accuracy per field\n")
    a("| Field | Accuracy |\n|---|---|")
    for f, v in s["per_field_pct"].items():
        a(f"| {f} | {fmt(v)} |")
    a("\n## Accuracy per file type\n")
    a("| Type | Documents | Field accuracy | Fully correct documents |\n|---|---|---|---|")
    for ty, v in sorted(s["per_type"].items()):
        a(f"| {ty} | {v['docs']} | {fmt(v['field_accuracy_pct'])} | {v['perfect_docs']} |")
    a("\n## Files that did not behave as expected\n")
    bad = [p for p in per_file if (not p["status_match"]) or p["wrong_fields"] or p["flags_missing"]
           or p["flags_unexpected_errors"]]
    if not bad:
        a("None.\n")
    for p in bad:
        a(f"- **{p['file']}** ({p['type']}): expected `{p['expected_status']}`, got `{p['final_status']}`")
        if p["wrong_fields"]:
            a(f"  - wrong fields: {p['wrong_fields']}")
        if p["flags_missing"]:
            a(f"  - expected flags NOT raised: {p['flags_missing']}")
        if p["flags_unexpected_errors"]:
            a(f"  - unexpected error flags: {p['flags_unexpected_errors']}")
        if p["error_message"]:
            a(f"  - message: {p['error_message']}")
    a("\n## How to read these numbers (limits)\n")
    a("- The dataset has only 32 files and was generated synthetically, so results will be better than on messy real-world documents. Treat them as a controlled baseline, not a production guarantee.")
    a("- A field counts as correct only when it equals the label (numbers within 0.01, dates exact, names compared ignoring case and punctuation). A field the label marks as absent counts as wrong if the system returned any value.")
    a("- Documents that failed unexpectedly count every field as wrong, so failures lower accuracy.")
    a("- Time is measured by the client from the start of the upload until the status becomes final, polling every 2 seconds (about +/- 2 s resolution). It includes AI calls but not the later embedding step.")
    a("- Files were processed with limited concurrency; timings on a quiet system or paid plan will differ.")
    pathlib.Path(path).write_text("\n".join(L), encoding="utf-8")


# ------------------------------------------------------------------- main
def load_labels(path):
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        if all(isinstance(v, dict) for v in data.values()) and not any(k in data for k in ("files", "documents")):
            data = [dict(v, file=v.get("file", k)) for k, v in data.items()]
        else:
            data = next(v for v in data.values() if isinstance(v, list))
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=os.environ.get("DI_DATASET_DIR", r"C:\Users\Riyad\Downloads\test_dataset\dataset"))
    ap.add_argument("--labels", default=None, help="path to labels.json (default: search the dataset folder and its parent)")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", default="results")
    ap.add_argument("--allow-dirty", action="store_true", help="run even if the database already contains documents")
    args = ap.parse_args()
    if not KEY:
        sys.exit("Set the DI_API_KEY environment variable first.")
    dataset = pathlib.Path(args.dataset)
    labels = pathlib.Path(args.labels) if args.labels else next(
        (p for p in (dataset / "labels.json", dataset.parent / "labels.json") if p.exists()), None)
    if not labels or not labels.exists():
        sys.exit(f"labels.json not found. Pass --labels <path>. Looked in {dataset} and its parent.")
    entries = load_labels(labels)
    print(f"{len(entries)} labeled files loaded from {labels}")

    # Duplicate detection compares with documents already in the database, so old test uploads
    # would corrupt the result (digital_01.pdf would be flagged as a duplicate of itself).
    clean = True
    try:
        pre = api_get("/api-documents", limit=100)
        if pre.status_code != 200:
            sys.exit(f"API check failed: HTTP {pre.status_code}: {pre.text[:200]}  -> wrong DI_API_KEY, wrong DI_BASE_URL, or the workflows are not published / out of executions.")
        existing = pre.json().get("count", 0)
    except Exception as e:
        sys.exit(f"Cannot reach the API ({e}). Check DI_API_KEY and that the workflows are active.")
    if existing:
        clean = False
        print(f"\nWARNING: the database already contains {existing}+ documents.")
        if not args.allow_dirty:
            sys.exit("Empty the tables first (Supabase > SQL editor: truncate table documents cascade;) "
                     "or re-run with --allow-dirty (the report will say the database was not empty).")

    first = [e for e in entries if e["file"] not in SERIAL_AFTER]
    second = [e for e in entries if e["file"] in SERIAL_AFTER]
    recs, done = [], [0]

    def work(e):
        r = run_file(e, dataset, args.timeout)
        with lock:
            done[0] += 1
            print(f"[{done[0]:>2}/{len(entries)}] {e['file']:<34} expected={e.get('expected_status'):<13} "
                  f"got={r['final_status']:<14} {r['seconds'] if r['seconds'] is not None else '-'}s"
                  + (f"  [{r['error']}]" if r['final_status'] == 'upload_rejected' else ""))
        return r

    start = time.time()
    for batch in (first, second):
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            for fut in as_completed([ex.submit(work, e) for e in batch]):
                recs.append(fut.result())
    wall = round(time.time() - start, 1)

    summary, per_file = summarise(entries, recs)
    meta = {"when": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "concurrency": args.concurrency,
            "clean": clean, "wall_clock_seconds": wall, "endpoint": BASE}
    out = pathlib.Path(args.out)
    out.mkdir(exist_ok=True)
    (out / "evaluation_results.json").write_text(json.dumps({"meta": meta, "summary": summary, "files": per_file}, indent=2, default=str), encoding="utf-8")
    with open(out / "evaluation_per_file.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(dict.fromkeys(k for p in per_file for k in p)))
        w.writeheader()
        w.writerows(per_file)
    write_report(out / "evaluation_report.md", summary, per_file, meta)

    print("\n================ SUMMARY ================")
    print(f"Field accuracy        : {fmt(summary['field_accuracy_pct'])}  ({summary['field_correct']}/{summary['field_total']})")
    print(f"Fully correct docs    : {summary['docs_perfect']}/{summary['docs_evaluated']}")
    print(f"Invented values       : {summary['hallucinated_values']}")
    print(f"Success / failure rate: {fmt(summary['success_rate_pct'])} / {fmt(summary['failure_rate_pct'])}")
    print(f"Corrupt files handled : {summary['corrupt_files_handled_correctly']}/{summary['corrupt_files_total']}")
    print(f"Flags detected        : {summary['flags']['found']}/{summary['flags']['expected']}")
    print(f"Time mean/median/p90  : {summary['time_seconds']['mean']} / {summary['time_seconds']['median']} / {summary['time_seconds']['p90']} s")
    print(f"Report written to     : {out / 'evaluation_report.md'}")


if __name__ == "__main__":
    main()
