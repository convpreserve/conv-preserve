import shutil
import tempfile
import unittest
import os
import sys
import subprocess
import json

def strip_keys(d):
    if isinstance(d, dict):
        d.pop("generated_at", None)
        d.pop("wall_time_sec", None)
        d.pop("v16", None)
        for k, v in list(d.items()):
            strip_keys(v)
    elif isinstance(d, list):
        for v in d:
            strip_keys(v)

def compare_dicts(d1, d2, path=""):
    if isinstance(d1, dict) and isinstance(d2, dict):
        keys = set(d1.keys()) | set(d2.keys())
        for k in keys:
            if k not in d1: return f"{path}.{k} missing in d1"
            if k not in d2: return f"{path}.{k} missing in d2"
            res = compare_dicts(d1[k], d2[k], f"{path}.{k}")
            if res: return res
    elif isinstance(d1, list) and isinstance(d2, list):
        if len(d1) != len(d2): return f"{path} length mismatch {len(d1)} != {len(d2)}"
        for i, (v1, v2) in enumerate(zip(d1, d2)):
            res = compare_dicts(v1, v2, f"{path}[{i}]")
            if res: return res
    else:
        if d1 != d2: return f"{path} value mismatch: {d1} != {d2}"
    return None

class TestRunOfRecordV100(unittest.TestCase):
    def test_runofrecord(self):
        pack_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        v100_dir = os.path.join(pack_dir, "artifacts", "g4", "oracle_v100")
        if not os.path.exists(v100_dir):
            self.skipTest("artifacts/g4/oracle_v100 not found (running from wheel).")
            return
            
        driver = os.path.join(os.path.dirname(__file__), "_run_frozen_runner.py")
        out_dir = tempfile.mkdtemp(prefix="convpreserve_test_")
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        os.makedirs(out_dir, exist_ok=True)
        
        env = {"PATH": os.environ.get("PATH", "")}
        
        for s in [9, 10, 11, 12, 13, 14]:
            runner_out = os.path.join(out_dir, f"set{s}_v100.json")
            if os.path.exists(runner_out): os.remove(runner_out)
            
            # Use /dev/null for out_jsonl since we only care about runner-out
            subprocess.run([sys.executable, driver, str(s), "vendored", os.devnull, "--runner-out", runner_out], env=env, check=True)
            
            with open(runner_out, "r") as f:
                actual = json.load(f)
            
            ref_file = os.path.join(v100_dir, "out", f"set{s}_v100.json")
            with open(ref_file, "r") as f:
                expected = json.load(f)
                
            strip_keys(actual)
            strip_keys(expected)
            
            diff = compare_dicts(expected, actual, "root")
            if diff:
                print(f"Set {s}: identical: no ({diff})")
                self.fail(f"Set {s} mismatch: {diff}")
            else:
                print(f"Set {s}: identical: yes")

if __name__ == "__main__":
    unittest.main()
