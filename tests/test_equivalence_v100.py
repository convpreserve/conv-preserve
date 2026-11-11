import shutil
import tempfile
import unittest
import os
import sys
import subprocess
import json
import time

class TestEquivalenceV100(unittest.TestCase):
    def test_equivalence(self):
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
            frozen_jsonl = os.path.join(out_dir, f"set{s}_frozen.jsonl")
            vendored_jsonl = os.path.join(out_dir, f"set{s}_vendored.jsonl")
            
            subprocess.run([sys.executable, driver, str(s), "frozen", frozen_jsonl], env=env, check=True)
            subprocess.run([sys.executable, driver, str(s), "vendored", vendored_jsonl], env=env, check=True)
            
            with open(frozen_jsonl, "r", encoding="utf-8") as f:
                frozen_lines = f.readlines()
            with open(vendored_jsonl, "r", encoding="utf-8") as f:
                vendored_lines = f.readlines()
            
            self.assertEqual(len(frozen_lines), len(vendored_lines), f"Set {s} call count mismatch")
            
            identical = 0
            for i, (f_line, v_line) in enumerate(zip(frozen_lines, vendored_lines)):
                if f_line == v_line:
                    identical += 1
                else:
                    f_rec = json.loads(f_line)
                    v_rec = json.loads(v_line)
                    self.assertEqual(f_rec, v_rec, f"Set {s} call {i} mismatch")
                    
            print(f"Set {s}: {identical} identical / {len(frozen_lines)} total")
            
if __name__ == "__main__":
    unittest.main()
