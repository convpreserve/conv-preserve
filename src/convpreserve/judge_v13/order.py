"""Causal partial order checker (`E1`–`E5` + `R07`) for Oracle v1.0 (`CAUSAL_ORDER.md` §2, `PREREG_V100.md` §3).
Zero vendor wire field names live here; operates exclusively on unified IR events.
"""
import re


def _ws(s):
    return re.sub(r"\s+", "", str(s or ""))


def _coarse_role(src):
    if str(src).startswith("tool:"):
        return "tool"
    return str(src)


def _canonicalize_refs(events, phase="request"):
    """Canonicalize tool_call and tool_result refs by appearance order of tool_calls (#0, #1, ...)."""
    ev = [dict(e) for e in events]
    idmap = {}
    next_idx = 0
    for e in ev:
        if e["kind"] == "tool_call":
            r = e["ref"]
            e["orig_ref"] = r
            if phase in ("response", "stream"):
                e["ref"] = f"#{next_idx}"
                if r is not None and r != "" and r not in idmap:
                    idmap[r] = f"#{next_idx}"
                next_idx += 1
            elif r is not None and r != "":
                if r not in idmap:
                    idmap[r] = f"#{next_idx}"
                    next_idx += 1
                e["ref"] = idmap[r]
            else:
                e["ref"] = f"#{next_idx}"
                next_idx += 1
    for e in ev:
        if e["kind"] == "tool_result":
            e["orig_ref"] = e["ref"]
            e["ref"] = idmap.get(e["ref"], f"?{e['ref']}")
            e["src"] = "tool"
    return ev


def _group_coarse_turns(events):
    """Group non-empty events into coarse turns by collapsing consecutive events of the same coarse role."""
    turns = []
    for e in events:
        if e["kind"] == "text" and e["payload"] == "":
            continue
        cr = _coarse_role(e["src"])
        if not turns or turns[-1]["role"] != cr:
            turns.append({"role": cr, "events": [dict(e)]})
        else:
            turns[-1]["events"].append(dict(e))
    return turns


def _coalesce_adjacent_texts(events):
    """Coalesce adjacent text events of the same src within a list, tracking coalesced block counts."""
    out = []
    coalesced_count = 0
    for e in events:
        if out and e["kind"] == "text" and out[-1]["kind"] == "text" and out[-1]["src"] == e["src"]:
            out[-1]["payload"] += e["payload"]
            out[-1]["n"] = out[-1].get("n", 1) + 1
            coalesced_count += 1
        else:
            d = dict(e)
            d["n"] = 1
            out.append(d)
    return out, coalesced_count


def _l07_hoist_turn(turn_events):
    """Hoist text events following tool_call before the earliest tool_call in an assistant turn (L07)."""
    calls = [idx for idx, e in enumerate(turn_events) if e["kind"] == "tool_call"]
    if not calls:
        return turn_events, False
    c0 = calls[0]
    late_text = [e for e in turn_events[c0:] if e["kind"] == "text"]
    if not late_text:
        return turn_events, False
    rest = [e for e in turn_events[c0:] if e["kind"] != "text"]
    return turn_events[:c0] + late_text + rest, True


def _turn_has_text_after_call(turn_events):
    calls = [idx for idx, e in enumerate(turn_events) if e["kind"] == "tool_call"]
    if not calls:
        return False
    c0 = calls[0]
    return any(e["kind"] == "text" and e["payload"] != "" for e in turn_events[c0:])


def check_order(ir_src, ir_dst, *, disabled=frozenset()):
    """Check causal partial order (E1-E5) and sibling tool_result order (R07).
    Returns list of (rule_id, tier, detail) tuples.
    """
    findings = []
    dst_dialect = ir_dst["dialect"]
    p2_disabled = "P2" in disabled

    ev_a = _canonicalize_refs(ir_src["events"], ir_src["phase"])
    ev_b = _canonicalize_refs(ir_dst["events"], ir_dst["phase"])

    # Filter out empty text blocks (accounted separately by R05)
    ev_a = [e for e in ev_a if not (e["kind"] == "text" and e["payload"] == "")]
    ev_b = [e for e in ev_b if not (e["kind"] == "text" and e["payload"] == "")]

    if p2_disabled:
        # Ablation -P2: revert to v0.16 _l07_openai forced input rewrite + total sequence equality
        if dst_dialect == "openai_chat":
            turns_a = _group_coarse_turns(ev_a)
            rewritten_a = []
            for t in turns_a:
                if t["role"] == "assistant":
                    hoisted, _ = _l07_hoist_turn(t["events"])
                    rewritten_a.extend(hoisted)
                else:
                    rewritten_a.extend(t["events"])
            ev_a = rewritten_a

        coal_a, _ = _coalesce_adjacent_texts(ev_a)
        coal_b, _ = _coalesce_adjacent_texts(ev_b)
        return _compare_sequence_strict(coal_a, coal_b, findings, allow_r07=False)

    # v1.0 P2 + L09 causal partial order (E1-E5 + R07)
    turns_a = _group_coarse_turns(ev_a)
    turns_b = _group_coarse_turns(ev_b)

    # Match assistant turns to apply L07 u L09 on openai_chat
    asst_turns_a = [t for t in turns_a if t["role"] == "assistant"]
    asst_turns_b = [t for t in turns_b if t["role"] == "assistant"]

    for idx_t, ta in enumerate(asst_turns_a):
        tb = asst_turns_b[idx_t] if idx_t < len(asst_turns_b) else None
        if dst_dialect == "openai_chat" and _turn_has_text_after_call(ta["events"]):
            if tb is not None and not _turn_has_text_after_call(tb["events"]):
                # Target used L07 (merged text before tool_calls)
                ta["events"], did_hoist = _l07_hoist_turn(ta["events"])
                if did_hoist:
                    findings.append(("R03", "T3", f"L07 assistant text-after-call coalesced before tool_calls in assistant turn {idx_t}"))
            elif tb is not None and _turn_has_text_after_call(tb["events"]):
                # Target used L09 (turn split preserving causal order)
                wire_turns_b = len({e["turn_index"] for e in tb["events"]})
                wire_turns_a = len({e["turn_index"] for e in ta["events"]})
                if wire_turns_b > wire_turns_a:
                    findings.append(("R03", "T3", f"L09 assistant turn split into {wire_turns_b} consecutive assistant messages preserving causal order"))

    # Check R07 (sibling tool_result order within one parallel tool group)
    tool_turns_a = [t for t in turns_a if t["role"] == "tool"]
    tool_turns_b = [t for t in turns_b if t["role"] == "tool"]
    for idx_t, (ta, tb) in enumerate(zip(tool_turns_a, tool_turns_b)):
        refs_a = [e["ref"] for e in ta["events"] if e["kind"] == "tool_result"]
        refs_b = [e["ref"] for e in tb["events"] if e["kind"] == "tool_result"]
        common = [r for r in refs_a if r in set(refs_b)]
        common_b = [r for r in refs_b if r in set(refs_a)]
        if len(common) >= 2 and common != common_b:
            findings.append(("R07", "T3", f"sibling tool_result order within parallel group {idx_t} changed: {common} -> {common_b}"))
            # Sort tool_results within this parallel group by ref so sibling order does not trigger S07
            ta["events"] = _sort_tool_results_in_group(ta["events"])
            tb["events"] = _sort_tool_results_in_group(tb["events"])

    flat_a = [e for t in turns_a for e in t["events"]]
    flat_b = [e for t in turns_b for e in t["events"]]

    coal_a, ca_cnt = _coalesce_adjacent_texts(flat_a)
    coal_b, cb_cnt = _coalesce_adjacent_texts(flat_b)
    if ca_cnt != cb_cnt and not any(f[0] == "R03" for f in findings):
        findings.append(("R03", "T3", f"adjacent same-role text blocks coalesced/split ({len(flat_a)} -> {len(flat_b)} blocks)"))

    return _compare_sequence_strict(coal_a, coal_b, findings, allow_r07=True)


def _sort_tool_results_in_group(events):
    res = sorted([e for e in events if e["kind"] == "tool_result"], key=lambda x: str(x["ref"]))
    other = [e for e in events if e["kind"] != "tool_result"]
    return res + other


def _compare_sequence_strict(coal_a, coal_b, findings, allow_r07=True):
    """Compare event sequences after legal equivalence reductions (L02/L04, L07 u L09, R07).
    Detects E1-E5 violations (including non-adjacent text hoisted across tool_call and identity-paired reordering).
    """
    # 1. Detect non-adjacent texts merged/hoisted across a non-text event (e.g. Rosetta E5 on Responses, or -P2)
    def _tkey(e):
        return (e["src"], e["kind"], e["ref"], _ws(e["payload"]))

    ka = [_tkey(e) for e in coal_a]
    kb = [_tkey(e) for e in coal_b]

    matched_a = [False] * len(coal_a)
    matched_b = [False] * len(coal_b)
    pair_map = []  # (idx_a, idx_b)

    # Initial pass: exact (src, kind, ref, ws(payload)) match in order
    for i, k in enumerate(ka):
        for j, bk in enumerate(kb):
            if not matched_b[j] and bk == k:
                matched_a[i] = True
                matched_b[j] = True
                pair_map.append((i, j))
                break

    # Check if an unmatched text in B is the concatenation of >=2 unmatched texts in A (hoisted across non-text)
    unmatched_a_texts = [(i, coal_a[i]) for i in range(len(coal_a)) if not matched_a[i] and coal_a[i]["kind"] == "text" and _ws(coal_a[i]["payload"])]
    unmatched_b_texts = [(j, coal_b[j]) for j in range(len(coal_b)) if not matched_b[j] and coal_b[j]["kind"] == "text" and _ws(coal_b[j]["payload"])]

    for j, eb in list(unmatched_b_texts):
        if matched_b[j]:
            continue
        wb = _ws(eb["payload"])
        parts = [(i, ea) for (i, ea) in unmatched_a_texts if not matched_a[i] and ea["src"] == eb["src"] and _ws(ea["payload"]) in wb]
        parts.sort(key=lambda item: wb.find(_ws(item[1]["payload"])))
        if len(parts) >= 2 and "".join(_ws(p[1]["payload"]) for p in parts) == wb:
            # Non-adjacent same-source texts were separated by another event in A and merged/hoisted in B
            findings.append(("S07", "T1", f"{len(parts)} non-adjacent {eb['src']} texts merged/hoisted across intervening event: {eb['payload'][:60]!r}"))
            matched_b[j] = True
            for i, _ in parts:
                matched_a[i] = True
            pair_map.append((parts[0][0], j))

    # Converse: an unmatched text in A split into >=2 non-adjacent texts in B around an intervening event
    for i, ea in list(unmatched_a_texts):
        if matched_a[i]:
            continue
        wa = _ws(ea["payload"])
        parts = [(j, eb) for (j, eb) in unmatched_b_texts if not matched_b[j] and eb["src"] == ea["src"] and _ws(eb["payload"]) in wa]
        parts.sort(key=lambda item: wa.find(_ws(item[1]["payload"])))
        if len(parts) >= 2 and "".join(_ws(p[1]["payload"]) for p in parts) == wa:
            findings.append(("S07", "T1", f"text run split around an intervening non-text event: {ea['payload'][:60]!r}"))
            matched_a[i] = True
            for j, _ in parts:
                matched_b[j] = True
            pair_map.append((i, parts[0][0]))

    # Second pass: identity pairing on remaining unmatched events (tool_call/tool_result by (kind, ref), others by (turn_index, src, kind, occ))
    # This ensures S03 (is_error loss) or S04/S09 never masks S07 (e.g., litellm #12404).
    def _identities(ev_list):
        ids = []
        counts = {}
        for e in ev_list:
            if e["kind"] in ("tool_call", "tool_result") and e["ref"] is not None:
                ids.append(("ref", e["kind"], e["ref"]))
            else:
                grp = (_coarse_role(e["src"]), e["kind"])
                occ = counts.get(grp, 0)
                counts[grp] = occ + 1
                ids.append(("pos", grp[0], grp[1], occ))
        return ids

    id_a = _identities(coal_a)
    id_b = _identities(coal_b)
    for i, ida in enumerate(id_a):
        if matched_a[i]:
            continue
        for j, idb in enumerate(id_b):
            if not matched_b[j] and ida == idb:
                matched_a[i] = True
                matched_b[j] = True
                pair_map.append((i, j))
                break

    # Check relative order of all paired events
    pair_map.sort(key=lambda p: p[0])
    b_indices_in_a_order = [p[1] for p in pair_map]
    if b_indices_in_a_order != sorted(b_indices_in_a_order):
        if not any(f[0] == "S07" for f in findings):
            # Describe the earliest inversion
            for k_idx in range(len(pair_map) - 1):
                i1, j1 = pair_map[k_idx]
                i2, j2 = pair_map[k_idx + 1]
                if j1 > j2:
                    e1, e2 = coal_a[i1], coal_a[i2]
                    findings.append((
                        "S07", "T1",
                        f"causal order violated: ({e1['src']},{e1['kind']},{e1['ref'] or e1['payload'][:20]!r}) "
                        f"and ({e2['src']},{e2['kind']},{e2['ref'] or e2['payload'][:20]!r}) reordered"
                    ))
                    break

    return findings
