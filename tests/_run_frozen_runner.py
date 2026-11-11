import argparse
import sys
import os
import json
import hashlib
import importlib.util

def _canonical_hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("set", type=int)
    parser.add_argument("judge", choices=["frozen", "vendored"])
    parser.add_argument("out_jsonl")
    parser.add_argument("--runner-out")
    args = parser.parse_args()

    pack_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    v100_dir = os.path.join(pack_dir, "artifacts", "g4", "oracle_v100")
    
    sys.path.insert(0, v100_dir)
    sys.path.insert(0, os.path.join(v100_dir, "src"))

    if args.judge == "frozen":
        import judge as judge_module
        actual_compare = judge_module.compare
    else:
        pkg_init = os.path.join(pack_dir, "tooling", "convpreserve", "src", "convpreserve", "__init__.py")
        spec = importlib.util.spec_from_file_location("convpreserve_vendored", pkg_init, submodule_search_locations=[os.path.dirname(pkg_init)])
        convpreserve_vendored = importlib.util.module_from_spec(spec)
        sys.modules["convpreserve_vendored"] = convpreserve_vendored
        spec.loader.exec_module(convpreserve_vendored)
        import convpreserve_vendored.judge.judge
        actual_compare = convpreserve_vendored.judge.judge.compare

    import run_sets_heldout_v100
    import reference_v100
    
    out_f = open(args.out_jsonl, "w", encoding="utf-8")
    call_idx = 0

    def make_recording_compare(phase):
        def recording_compare(src_body, src_dialect, dst_body, dst_dialect, side, *, context=None):
            nonlocal call_idx
            findings = actual_compare(src_body, src_dialect, dst_body, dst_dialect, side, context=context)
            rec = {
                "i": call_idx,
                "phase": phase,
                "sd": src_dialect,
                "dd": dst_dialect,
                "side": side,
                "ctx": context,
                "h_src": _canonical_hash(src_body),
                "h_dst": _canonical_hash(dst_body),
                "f": [[f.rule_id, f.tier, f.detail] for f in findings]
            }
            out_f.write(json.dumps(rec) + "\n")
            call_idx += 1
            return findings
        return recording_compare

    run_sets_heldout_v100.compare = make_recording_compare("loop")
    reference_v100.compare = make_recording_compare("setup")

    sys.argv = ["run_sets_heldout_v100.py", "--set", str(args.set)]
    if args.runner_out:
        sys.argv.extend(["--out", args.runner_out])
    else:
        tmp_out = f"/tmp/dummy_runner_out_{args.set}_{args.judge}.json"
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
        sys.argv.extend(["--out", tmp_out])

    try:
        run_sets_heldout_v100.main()
    except SystemExit:
        pass
        
    out_f.close()

if __name__ == "__main__":
    main()
