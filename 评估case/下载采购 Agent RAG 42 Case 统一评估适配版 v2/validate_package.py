from pathlib import Path
import json,collections
root=Path(__file__).parent
cases=[json.loads(s) for s in (root/'cases_v2.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]
assert len(cases)==42 and len(set(x['case_id'] for x in cases))==42
assert collections.Counter(x['scenario_type'] for x in cases)=={'single_condition':10,'multi_condition':10,'cross_document':10,'insufficient_evidence':6,'routing':6}
for c in cases:
    assert c['run_state']=='NOT_RUN' and c['score_state']=='NOT_SCORED'
    assert c['gold_validation']['status'] in ('needs_formal_kb_confirmation','needs_atomic_gold_review')
    if c['case_id']!='RAG-042': assert c['process']['allow_business_write'] is False
    if c['scenario_type']=='cross_document': assert len(c['retrieval']['required_distinct_doc_ids'])>=2
    if c['expected_route']=='BUSINESS_API_READ': assert c['retrieval']['evaluate']=='not_applicable'
print('42 cases validated; no evaluation results implied')
