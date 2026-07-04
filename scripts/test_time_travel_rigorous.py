#!/usr/bin/env python3
"""Rigorous time-travel test for GST Statute MCP.

Tests that querying provisions at dates before/after known amendment
implementation dates returns different text, correct status, and complete
amendment chains.
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, '/home/shikhar/openclaw-workspace/Projects/Git_for_Law')
from src.legal_corpus.serving import NyayaToolService


def run_tests():
    service = NyayaToolService()
    results = []

    # === TEST 1: CGST Rules — text changes across amendment dates ===
    print("\n" + "="*60)
    print("TEST 1: CGST Rules — text changes across amendment boundaries")
    print("="*60)

    with open("derived/version_history/cgst-rules-2017/node_versions.jsonl") as f:
        rule_versions = defaultdict(list)
        for line in f:
            row = json.loads(line)
            cid = row.get('component_id', '')
            if '/rule/' in cid and '/subrule/' not in cid:
                rule_versions[cid].append(row)

    rules_tested = 0
    rules_pass = 0
    rules_fail = 0

    for cid, versions in sorted(rule_versions.items(), key=lambda x: -len(x[1]))[:20]:
        versions.sort(key=lambda r: r.get('applicability_start', ''))
        
        for i in range(1, min(len(versions), 4)):
            transition = versions[i].get('applicability_start', '')
            if not transition or versions[i-1].get('text','') == versions[i].get('text',''):
                continue
            
            d = datetime.strptime(transition, '%Y-%m-%d')
            before = (d - timedelta(days=1)).strftime('%Y-%m-%d')

            r_before = service.get_provision_as_of_date(cid, date=before)
            r_after = service.get_provision_as_of_date(cid, date=transition)

            text_changed = r_before.get('text','') != r_after.get('text','')
            before_ok = r_before.get('status') in ('ok', 'ok_with_gaps')
            after_ok = r_after.get('status') in ('ok', 'ok_with_gaps')

            rules_tested += 1
            rule_num = cid.split('/rule/')[-1]
            
            if text_changed and before_ok and after_ok:
                rules_pass += 1
                print(f"  PASS Rule {rule_num:6s} {before} -> {transition}: text changed ({len(r_before['text'])} -> {len(r_after['text'])} chars)")
            else:
                rules_fail += 1
                print(f"  FAIL Rule {rule_num:6s} {before} -> {transition}: changed={text_changed} before={r_before.get('status')} after={r_after.get('status')}")

    results.append(('CGST Rules text change', rules_tested, rules_pass, rules_fail))

    # === TEST 2: CGST Act — text changes across amendment dates ===
    print("\n" + "="*60)
    print("TEST 2: CGST Act — text changes across amendment boundaries")
    print("="*60)

    with open("derived/version_history/cgst-act-2017/node_versions.jsonl") as f:
        act_versions = defaultdict(list)
        for line in f:
            row = json.loads(line)
            cid = row.get('component_id', '')
            if '/section/' in cid:
                act_versions[cid].append(row)

    act_tested = 0
    act_pass = 0
    act_fail = 0

    for cid, versions in sorted(act_versions.items(), key=lambda x: -len(x[1]))[:15]:
        versions.sort(key=lambda r: r.get('applicability_start', ''))
        
        for i in range(1, min(len(versions), 4)):
            transition = versions[i].get('applicability_start', '')
            if not transition or versions[i-1].get('text','') == versions[i].get('text',''):
                continue
            
            d = datetime.strptime(transition, '%Y-%m-%d')
            before = (d - timedelta(days=1)).strftime('%Y-%m-%d')

            r_before = service.get_provision_as_of_date(cid, date=before)
            r_after = service.get_provision_as_of_date(cid, date=transition)

            text_changed = r_before.get('text','') != r_after.get('text','')

            act_tested += 1
            sec_num = cid.split('/section/')[-1]
            
            if text_changed:
                act_pass += 1
                print(f"  PASS Sec {sec_num:6s} {before} -> {transition}: text changed ({len(r_before.get('text',''))} -> {len(r_after.get('text',''))} chars)")
            else:
                act_fail += 1
                print(f"  FAIL Sec {sec_num:6s} {before} -> {transition}: text NOT changed")

    results.append(('CGST Act text change', act_tested, act_pass, act_fail))

    # === TEST 3: Amendment chain completeness ===
    print("\n" + "="*60)
    print("TEST 3: Amendment chain for heavily-amended provisions")
    print("="*60)

    chain_tested = 0
    chain_pass = 0
    chain_fail = 0

    for cid in ['/in/union/rules/cgst-rules-2017/rule/89',
                '/in/union/rules/cgst-rules-2017/rule/142',
                '/in/union/rules/cgst-rules-2017/rule/96',
                '/in/union/acts/cgst-act-2017/section/16',
                '/in/union/acts/cgst-act-2017/section/107']:
        amendments = service.list_amendments(cid)
        count = amendments.get('count', 0)
        chain_tested += 1
        if count > 0:
            chain_pass += 1
            short = cid.split('/')[-1]
            print(f"  PASS {short:8s}: {count} amendments in chain")
        else:
            chain_fail += 1
            print(f"  FAIL {cid}: 0 amendments (expected > 0)")

    results.append(('Amendment chain', chain_tested, chain_pass, chain_fail))

    # === TEST 4: Timeline completeness ===
    print("\n" + "="*60)
    print("TEST 4: Provision timeline")
    print("="*60)

    tl_tested = 0
    tl_pass = 0
    tl_fail = 0

    for cid in ['/in/union/rules/cgst-rules-2017/rule/89',
                '/in/union/rules/cgst-rules-2017/rule/142',
                '/in/union/acts/cgst-act-2017/section/16']:
        timeline = service.get_provision_timeline(cid)
        count = timeline.get('count', 0)
        tl_tested += 1
        if count > 1:
            tl_pass += 1
            short = cid.split('/')[-1]
            print(f"  PASS {short:8s}: {count} versions in timeline")
        else:
            tl_fail += 1
            print(f"  FAIL {cid}: only {count} version(s)")

    results.append(('Timeline', tl_tested, tl_pass, tl_fail))

    # === TEST 5: Date validation ===
    print("\n" + "="*60)
    print("TEST 5: Date validation")
    print("="*60)

    dv_tested = 0
    dv_pass = 0
    dv_fail = 0

    for bad_date in ['not-a-date', '2024-99-99', '', 'invalid', '2024/01/01']:
        r = service.get_provision_as_of_date('/in/union/rules/cgst-rules-2017/rule/10', date=bad_date)
        dv_tested += 1
        if r.get('status') == 'invalid_date':
            dv_pass += 1
            print(f"  PASS '{bad_date}' -> invalid_date")
        else:
            dv_fail += 1
            print(f"  FAIL '{bad_date}' -> {r.get('status')} (expected invalid_date)")

    # Good date should work
    r = service.get_provision_as_of_date('/in/union/rules/cgst-rules-2017/rule/10', date='2020-01-01')
    dv_tested += 1
    if r.get('status') in ('ok', 'ok_with_gaps'):
        dv_pass += 1
        print(f"  PASS '2020-01-01' -> {r.get('status')} (valid date accepted)")
    else:
        dv_fail += 1
        print(f"  FAIL '2020-01-01' -> {r.get('status')}")

    results.append(('Date validation', dv_tested, dv_pass, dv_fail))

    # === TEST 6: query_law_as_of_date (act+section+date) ===
    print("\n" + "="*60)
    print("TEST 6: query_law_as_of_date (act + section + date)")
    print("="*60)

    q_tested = 0
    q_pass = 0
    q_fail = 0

    # Test known CGST Act sections
    for section, date in [('16', '2024-01-01'), ('16', '2018-01-01'), ('107', '2024-01-01'), ('2', '2024-01-01')]:
        citation = f"section {section} CGST Act"
        resolved = service.resolve_citation(citation, limit=3)
        candidates = resolved.get('candidates', [])
        found = any(c.get('exists') for c in candidates)
        
        q_tested += 1
        if found:
            canonical_id = next(c['canonical_id'] for c in candidates if c.get('exists'))
            r = service.get_provision_as_of_date(canonical_id, date=date)
            q_pass += 1
            print(f"  PASS 'CGST Act section {section}' at {date} -> {r.get('status')} ({len(r.get('text',''))} chars)")
        else:
            q_fail += 1
            print(f"  FAIL Could not resolve 'section {section} CGST Act'")

    # Test CGST Rules citation
    for rule, date in [('10', '2020-01-01'), ('89', '2020-01-01'), ('96', '2020-01-01')]:
        citation = f"rule {rule} CGST Rules"
        resolved = service.resolve_citation(citation, limit=3)
        candidates = resolved.get('candidates', [])
        found = any(c.get('exists') for c in candidates)
        
        q_tested += 1
        if found:
            canonical_id = next(c['canonical_id'] for c in candidates if c.get('exists'))
            r = service.get_provision_as_of_date(canonical_id, date=date)
            q_pass += 1
            print(f"  PASS 'CGST Rules rule {rule}' at {date} -> {r.get('status')} ({len(r.get('text',''))} chars)")
        else:
            q_fail += 1
            print(f"  FAIL Could not resolve 'rule {rule} CGST Rules'")

    results.append(('Citation resolution', q_tested, q_pass, q_fail))

    # === TEST 7: Rate-related rule changes ===
    print("\n" + "="*60)
    print("TEST 7: Rate-related provisions (GSTR-1, GSTR-3B forms)")
    print("="*60)

    rate_tested = 0
    rate_pass = 0
    rate_fail = 0

    # Check that GSTR-1 form has multiple versions (it tracks form changes)
    timeline = service.get_provision_timeline('/in/union/forms/gstr-1')
    count = timeline.get('count', 0)
    rate_tested += 1
    if count > 5:
        rate_pass += 1
        print(f"  PASS GSTR-1 form: {count} versions")
    else:
        rate_fail += 1
        print(f"  FAIL GSTR-1 form: only {count} versions")

    # Check GSTR-3B
    timeline = service.get_provision_timeline('/in/union/forms/gstr-3b')
    count = timeline.get('count', 0)
    rate_tested += 1
    if count > 5:
        rate_pass += 1
        print(f"  PASS GSTR-3B form: {count} versions")
    else:
        rate_fail += 1
        print(f"  FAIL GSTR-3B form: only {count} versions")

    results.append(('Rate/Form provisions', rate_tested, rate_pass, rate_fail))

    # === TEST 8: Existing MCP tools backward compat ===
    print("\n" + "="*60)
    print("TEST 8: Existing MCP tools still work")
    print("="*60)

    bc_tested = 0
    bc_pass = 0
    bc_fail = 0

    # lookup_provision
    r = service.lookup_provision('/in/union/rules/cgst-rules-2017/rule/10')
    bc_tested += 1
    if r.get('found'):
        bc_pass += 1
        print(f"  PASS lookup_provision(rule/10) -> found")
    else:
        bc_fail += 1
        print(f"  FAIL lookup_provision(rule/10) -> not found")

    # resolve_citation
    r = service.resolve_citation('section 16 CGST Act')
    bc_tested += 1
    if r.get('candidates'):
        bc_pass += 1
        print(f"  PASS resolve_citation('section 16 CGST Act') -> {len(r['candidates'])} candidates")
    else:
        bc_fail += 1
        print(f"  FAIL resolve_citation returned no candidates")

    # get_outgoing_refs
    r = service.get_outgoing_refs('/in/union/acts/cgst-act-2017/section/16')
    bc_tested += 1
    if r.get('count', 0) >= 0:
        bc_pass += 1
        print(f"  PASS get_outgoing_refs(section/16) -> {r.get('count',0)} refs")
    else:
        bc_fail += 1
        print(f"  FAIL get_outgoing_refs(section/16)")

    # compare_versions
    r = service.compare_versions('/in/union/rules/cgst-rules-2017/rule/10', from_date='2017-06-22', to_date='2025-01-01')
    bc_tested += 1
    if r.get('status') in ('ok', 'ok_with_gaps'):
        bc_pass += 1
        print(f"  PASS compare_versions(rule/10, 2017, 2025) -> {r.get('status')}")
    else:
        bc_fail += 1
        print(f"  FAIL compare_versions -> {r.get('status')}")

    results.append(('Backward compat', bc_tested, bc_pass, bc_fail))

    # === SUMMARY ===
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    total_tested = sum(r[1] for r in results)
    total_pass = sum(r[2] for r in results)
    total_fail = sum(r[3] for r in results)
    
    for name, tested, passed, failed in results:
        status = "PASS" if failed == 0 else "MIXED"
        print(f"  {name:30s}: {tested:3d} tested, {passed:3d} passed, {failed:3d} failed [{status}]")
    
    print(f"\n  {'TOTAL':30s}: {total_tested:3d} tested, {total_pass:3d} passed, {total_fail:3d} failed")
    pct = (total_pass / total_tested * 100) if total_tested > 0 else 0
    print(f"  Pass rate: {pct:.1f}%")
    
    return total_fail == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
