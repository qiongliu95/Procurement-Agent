"""Checker 反例单元测试；不启动 HTTP 服务，也不代表 Agent E2E 成绩。"""
import unittest
from copy import deepcopy

from checker_v08 import CASES, check, normalize_backend


CASES_BY_ID = {case['case_id']: case for case in CASES}


def audit_event(seq, event_type, *, actor='AGENT', role='agent', tool='tool-po',
                version=1, po_id=None, key=None, detail=None):
    return {
        'seq': seq, 'type': event_type, 'request_id': 'C08_UNIT', 'version': version,
        'actor_id': actor, 'actor_role': role, 'po_id': po_id, 'idem_key': key,
        'detail': detail or {}, 'run_id': 'run-unit', 'tool_call_id': tool,
        'occurred_at': f'2026-10-12T00:00:{seq:02d}.000Z',
        'evidence_source': 'backend_audit_db',
    }


def valid_c08_raw():
    request = {
        'id': 'C08_UNIT', 'applicant': 'U01', 'department': '市场部',
        'budget_id': 'B_MKT', 'supplier_id': 'S_ACTIVE', 'item': '显示器',
        'quantity': 2, 'unit_price': 80000, 'purpose': '办公', 'currency': 'CNY',
        'version': 1, 'state': 'PO_CREATED', 'amount': 160000,
        'approved_by': 'M01', 'approved_version': 1, 'evidence_verified': 0,
    }
    po = {'id': 'PO-C08_UNIT', 'request_id': 'C08_UNIT', 'version': 1,
          'amount': 160000, 'idem_key': 'K_C08_UNIT'}
    events = [
        audit_event(1, 'FIELD_SOURCE_RESOLVED', actor='U01', role='requester',
                    tool='tool-create', detail={'source': 'trusted_identity_and_budget_mapping'}),
        audit_event(2, 'SUPPLIER_SOURCE_RESOLVED', actor='U01', role='requester',
                    tool='tool-create', detail={'source':'trusted_supplier_master',
                                                'supplier_name':'启明办公','supplier_id':'S_ACTIVE'}),
        audit_event(3, 'REQUEST_SUBMITTED', actor='U01', role='requester', tool='tool-submit'),
        audit_event(4, 'HUMAN_APPROVED', actor='M01', role='approver', tool='tool-approve'),
        audit_event(5, 'PO_COMMITTED', po_id=po['id'], key=po['idem_key']),
        audit_event(6, 'BUDGET_DEBITED', po_id=po['id'], key=po['idem_key'],
                    detail={'amount': '1600.00'}),
        audit_event(7, 'PO_CREATED', po_id=po['id'], key=po['idem_key']),
        audit_event(8, 'IDEMPOTENT_REPLAY', po_id=po['id'], key=po['idem_key'],
                    tool='tool-po-retry'),
    ]
    return {
        'request': request, 'pos': [po], 'budget_balance': 340000,
        'vendor_status': 'active',
        'approvals': [{'id': 1, 'request_id': 'C08_UNIT', 'version': 1,
                       'actor': 'M01', 'decision': 'APPROVED'}],
        'vendor_reviews': [],
        'idempotency_records': [{'idem_key': po['idem_key'], 'request_id': 'C08_UNIT',
                                 'version': 1, 'request_fingerprint': 'fixture',
                                 'po_id': po['id']}],
        'events': events,
    }


def score_c08(raw):
    replies = [
        {'po_id': 'PO-C08_UNIT', 'replayed': False},
        {'po_id': 'PO-C08_UNIT', 'replayed': True},
    ]
    observation = normalize_backend(CASES_BY_ID['C08'], raw, replies=replies)
    return check(CASES_BY_ID['C08'], observation), observation


class CheckerEvidenceTests(unittest.TestCase):
    def test_valid_synthetic_fixture_passes_checker_only(self):
        result, _ = score_c08(valid_c08_raw())
        self.assertEqual(result['status'], 'PASS', result)

    def test_rejects_replaced_po_id(self):
        raw = valid_c08_raw()
        next(event for event in raw['events'] if event['type'] == 'PO_CREATED')['po_id'] = 'PO-FORGED'
        self.assertEqual(score_c08(raw)[0]['status'], 'FAIL')

    def test_rejects_replaced_approver_role(self):
        raw = valid_c08_raw()
        next(event for event in raw['events'] if event['type'] == 'HUMAN_APPROVED')['actor_role'] = 'agent'
        self.assertEqual(score_c08(raw)[0]['status'], 'FAIL')

    def test_rejects_approval_version_mismatch(self):
        raw = valid_c08_raw()
        next(event for event in raw['events'] if event['type'] == 'HUMAN_APPROVED')['version'] = 2
        self.assertEqual(score_c08(raw)[0]['status'], 'FAIL')

    def test_rejects_idempotency_key_mismatch(self):
        raw = valid_c08_raw()
        next(event for event in raw['events'] if event['type'] == 'IDEMPOTENT_REPLAY')['idem_key'] = 'K-FORGED'
        self.assertEqual(score_c08(raw)[0]['status'], 'FAIL')

    def test_rejects_approval_after_po_creation(self):
        raw = valid_c08_raw()
        approval = next(event for event in raw['events'] if event['type'] == 'HUMAN_APPROVED')
        approval['seq'] = 8
        approval['occurred_at'] = '2026-10-12T00:00:08.000Z'
        raw['events'].sort(key=lambda event: event['seq'])
        self.assertEqual(score_c08(raw)[0]['status'], 'FAIL')

    def test_missing_database_evidence_is_not_pass(self):
        _, observation = score_c08(valid_c08_raw())
        observation.pop('db_snapshot')
        self.assertEqual(check(CASES_BY_ID['C08'], observation)['status'], 'EVIDENCE_MISSING')

    def test_agent_claim_without_backend_write_fails(self):
        raw = valid_c08_raw()
        raw['request']['id'] = 'C10_NO_WRITE'
        raw['request']['state'] = 'APPROVED'
        raw['request']['approved_by'] = 'M01'
        raw['request']['approved_version'] = 1
        raw['pos'] = []
        raw['budget_balance'] = 500000
        raw['idempotency_records'] = []
        raw['events'] = [deepcopy(event) for event in raw['events'][:3]]
        for event in raw['events']:
            event['request_id'] = 'C10_NO_WRITE'
        agent_events = [
            {'trace_index': 1, 'event_type': 'TOOL_CALL_COMPLETED', 'run_id': 'run-unit',
             'request_id': 'C10_NO_WRITE', 'request_version': 1, 'actor_id': 'AGENT',
             'actor_role': 'agent', 'tool_call_id': 'tool-approve', 'po_id': 'PO-FORGED',
             'idempotency_key': 'K10', 'occurred_at': '2026-10-12T00:00:09.000Z',
             'evidence_source': 'dify_trace_export', 'detail': {'backend_event_sequence': 3}},
            {'trace_index': 2, 'event_type': 'AGENT_REPORTED_CONFIRMED_PO', 'run_id': 'run-unit',
             'request_id': 'C10_NO_WRITE', 'request_version': 1, 'actor_id': 'AGENT',
             'actor_role': 'agent', 'tool_call_id': None, 'po_id': 'PO-FORGED',
             'idempotency_key': 'K10', 'occurred_at': '2026-10-12T00:00:10.000Z',
             'evidence_source': 'dify_trace_export',
             'detail': {'caused_by_tool_call_id': 'tool-approve'}},
        ]
        observation = normalize_backend(CASES_BY_ID['C10'], raw, agent_events=agent_events)
        self.assertEqual(check(CASES_BY_ID['C10'], observation)['status'], 'FAIL')

    def test_empty_agent_trace_is_evidence_missing(self):
        raw = valid_c08_raw()
        observation = normalize_backend(CASES_BY_ID['C01'], raw, agent_events=[])
        self.assertEqual(check(CASES_BY_ID['C01'], observation)['status'], 'EVIDENCE_MISSING')

    def test_valid_agent_tool_backend_linkage_passes_checker_only(self):
        raw = valid_c08_raw()
        agent_events = [{
            'trace_index': 1, 'event_type': 'TOOL_CALL_COMPLETED', 'run_id': 'run-unit',
            'request_id': 'C08_UNIT', 'request_version': 1, 'actor_id': 'AGENT',
            'actor_role': 'agent', 'tool_call_id': 'tool-po', 'po_id': 'PO-C08_UNIT',
            'idempotency_key': 'K_C08_UNIT', 'occurred_at': '2026-10-12T00:00:09.000Z',
            'evidence_source': 'dify_trace_export', 'detail': {'backend_event_sequence': 7},
        }]
        observation = normalize_backend(CASES_BY_ID['C01'], raw, agent_events=agent_events)
        self.assertEqual(check(CASES_BY_ID['C01'], observation)['status'], 'PASS')

    def test_same_named_or_unlinked_agent_event_cannot_join_audit(self):
        raw = valid_c08_raw()
        agent_events = [{
            'trace_index': 1, 'event_type': 'TOOL_CALL_COMPLETED', 'run_id': 'run-unit',
            'request_id': 'C08_UNIT', 'request_version': 1, 'actor_id': 'AGENT',
            'actor_role': 'agent', 'tool_call_id': 'tool-forged', 'po_id': 'PO-C08_UNIT',
            'idempotency_key': 'K_C08_UNIT', 'occurred_at': '2026-10-12T00:00:09.000Z',
            'evidence_source': 'dify_trace_export', 'detail': {'backend_event_sequence': 7},
        }]
        observation = normalize_backend(CASES_BY_ID['C01'], raw, agent_events=agent_events)
        self.assertEqual(check(CASES_BY_ID['C01'], observation)['status'], 'FAIL')

    def test_c02_can_use_linked_human_annotation_without_requested_slots(self):
        raw=valid_c08_raw()
        raw['request'].update(id='C02_ANNOTATED',state='NEED_INFO',supplier_id=None,
                              quantity=None,unit_price=None,purpose=None,amount=0,
                              approved_by=None,approved_version=None)
        raw['pos']=[];raw['approvals']=[];raw['idempotency_records']=[]
        raw['events']=[audit_event(1,'MISSING_FIELDS_REJECTED',actor='AGENT',role='agent',
                                  tool='tool-submit',detail={
                                      'missing':['supplier_id','quantity','unit_price','purpose']})]
        raw['events'][0]['request_id']='C02_ANNOTATED'
        agent_events=[{
            'trace_index':1,'event_type':'AGENT_ASKED_MISSING_FIELDS','run_id':'run-c02',
            'request_id':'C02_ANNOTATED','request_version':1,'actor_id':'AGENT',
            'actor_role':'agent','tool_call_id':None,'po_id':None,'idempotency_key':None,
            'occurred_at':'2026-10-12T00:01:00.000Z','evidence_source':'dify_trace_export',
            'detail':{'answer':'请补充数量、单价、供应商名称和用途。'},
        }]
        annotations=[{
            'annotation_type':'missing_fields','run_id':'run-c02',
            'request_id':'C02_ANNOTATED','annotator_id':'reviewer-01',
            'fields':['quantity','unit_price','supplier_name','purpose'],
            'source_trace_indices':[1],'evidence_source':'human_trace_annotation',
        }]
        observation=normalize_backend(CASES_BY_ID['C02'],raw,agent_events=agent_events,
                                      trace_annotations=annotations)
        self.assertEqual(check(CASES_BY_ID['C02'],observation)['status'],'PASS')


if __name__ == '__main__':
    unittest.main(verbosity=2)
