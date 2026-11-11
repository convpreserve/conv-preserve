import os
import sys
import json
import random
import gzip
import hashlib

def main():
    pack_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    v100_dir = os.path.join(pack_dir, "artifacts", "g4", "oracle_v100")
    
    sys.path.insert(0, v100_dir)
    sys.path.insert(0, os.path.join(v100_dir, "src"))
    import run_sets_heldout_v100
    import judge as frozen_judge
    
    records = []
    
    for s in [9, 10, 11, 12, 13, 14]:
        def patch_compare(src_body, src_dialect, dst_body, dst_dialect, side, *, context=None):
            findings = frozen_judge.compare(src_body, src_dialect, dst_body, dst_dialect, side, context=context)
            records.append({
                "set": s,
                "src_body": src_body,
                "src_dialect": src_dialect,
                "dst_body": dst_body,
                "dst_dialect": dst_dialect,
                "side": side,
                "context": context,
                "findings": [{"rule_id": f.rule_id, "tier": f.tier, "detail": f.detail} for f in findings]
            })
            return findings
            
        run_sets_heldout_v100.compare = patch_compare
        tmp_out = f"/tmp/dummy_runofrecord_{s}.json"
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
        sys.argv = ["run_sets_heldout_v100.py", "--set", str(s), "--out", tmp_out]
        try:
            run_sets_heldout_v100.main()
        except SystemExit:
            pass
            
    buckets = {}
    for r in records:
        key = (r["set"], r["side"], r["src_dialect"], r["dst_dialect"])
        buckets.setdefault(key, []).append(r)
        
    random.seed(42)
    sample = []
    
    for key, items in buckets.items():
        if items:
            sample.append(random.choice(items))
            
    needed = min(400, len(records)) - len(sample)
    target_findings = int(0.4 * min(400, len(records)))
    
    with_findings = [r for r in records if r["findings"]]
    without_findings = [r for r in records if not r["findings"]]
    
    current_findings = len([r for r in sample if r["findings"]])
    
    while needed > 0:
        if current_findings < target_findings and with_findings:
            r = random.choice(with_findings)
            if r not in sample:
                sample.append(r)
                current_findings += 1
                needed -= 1
        else:
            if without_findings:
                r = random.choice(without_findings)
                if r not in sample:
                    sample.append(r)
                    needed -= 1
            else:
                break
                
    encoded = json.dumps(sample, sort_keys=True, separators=(",",":")).encode("utf-8")
    
    out_path = os.path.join(os.path.dirname(__file__), "..", "src", "convpreserve", "data", "selftest_sample.json.gz")
    with gzip.open(out_path, "wb") as f:
        f.write(encoded)
        
    sha = hashlib.sha256()
    with open(out_path, "rb") as f:
        sha.update(f.read())
        
    print(sha.hexdigest())

if __name__ == "__main__":
    main()
