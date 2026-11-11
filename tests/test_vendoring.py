import hashlib
import os
import unittest

def get_sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

class TestVendoring(unittest.TestCase):
    def test_vendoring(self):
        here = os.path.dirname(os.path.abspath(__file__))
        package_dir = os.path.join(here, "..")
        vendored_md_path = os.path.join(package_dir, "VENDORED.md")
        with open(vendored_md_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        for line in content.splitlines():
            if line.startswith("| `") and "artifacts" in line:
                parts = [p.strip().strip("`") for p in line.split("|")]
                orig = parts[1]
                vendored = parts[2]
                vendored_sha = parts[5]
                
                rel_vendored = vendored.replace("", "")
                abs_vendored = os.path.join(package_dir, rel_vendored)
                
                actual_sha = get_sha256(abs_vendored)
                self.assertEqual(actual_sha, vendored_sha, f"Hash mismatch for {vendored}")

if __name__ == "__main__":
    unittest.main()
