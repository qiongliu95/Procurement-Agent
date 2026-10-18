"""可信证据 Checker（v1.0 候选，尚未冻结）。

normalize_backend() 只能由评测 Harness 使用 evaluator-only 后端快照、实际 HTTP
捕获和真实 Dify Trace 调用。模型自行拼出的同形 JSON 不属于可信证据。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path


CASES = json.loads(
    Path(__file__).with_name('cases_v1.0_candidate.json').read_text(encoding='utf-8')
)['cases']

SOURCE_ORIGINS = {
    'db_request': 'backend_eval_adapter', 'db_po': 'backend_eval_adapter',
    'db_budget': 'backend_eval_adapter', 'db_vendor': 'backend_eval_adapter',
    'db_evidence': 'backend_eval_adapter', 'approval_queue': 'backend_eval_adapter',
    'supplier_review_queue': 'backend_eval_adapter', 'idempotency_log': 'backend_eval_adapter',
    'approval_history': 'backend_eval_adapter', 'audit_log': 'backend_eval_adapter',
    'identity_context': 'backend_eval_adapter', 'field_sources': 'backend_eval_adapter',
    'fault_injection_log': 'backend_eval_adapter', 'api_response': 'harness_http_capture',
    'tool_trace': 'dify_trace_export', 'agent_trace': 'dify_trace_export',
    'trace_annotations': 'harness_human_annotation',
}


def _digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def _backend_event(event):
    """把 SQLite 字段名映射为跨系统事件契约字段。"""
    return {
        'backend_sequence': event.get('seq'), 'trace_index': None,
        'event_type': event.get('event_type', event.get('type')),
        'run_id': event.get('run_id'), 'request_id': event.get('request_id'),
        'request_version': event.get('request_version', event.get('version')),
        'actor_id': event.get('actor_id'), 'actor_role': event.get('actor_role'),
        'tool_call_id': event.get('tool_call_id'), 'po_id': event.get('po_id'),
        'idempotency_key': event.get('idempotency_key', event.get('idem_key')),
        'occurred_at': event.get('occurred_at'), 'evidence_source': event.get('evidence_source'),
        'detail': event.get('detail', {}), 'evidence_channel': 'db_audit',
    }


def _agent_event(event):
    return {
        'backend_sequence': None, 'trace_index': event.get('trace_index'),
        'event_type': event.get('event_type', event.get('type')),
        'run_id': event.get('run_id'), 'request_id': event.get('request_id'),
        'request_version': event.get('request_version', event.get('version')),
        'actor_id': event.get('actor_id'), 'actor_role': event.get('actor_role'),
        'tool_call_id': event.get('tool_call_id'), 'po_id': event.get('po_id'),
        'idempotency_key': event.get('idempotency_key', event.get('idem_key')),
        'occurred_at': event.get('occurred_at'), 'evidence_source': event.get('evidence_source'),
        'detail': event.get('detail', {}), 'evidence_channel': 'agent_trace',
    }


def normalize_backend(case, raw, *, http_status=None, api_error=None, replies=None,
                      lookup=None, agent_events=None, trace_annotations=None):
    """从可信适配器输入生成观察值；不接受模型自报的最终状态。"""
    rid = raw['request']['id']
    request = raw['request']
    pos = raw['pos']
    backend_events = [_backend_event(event) for event in raw['events']]
    normalized_agent = [] if agent_events is None else [_agent_event(event) for event in agent_events]
    events = backend_events + normalized_agent
    backend_types = [event['event_type'] for event in backend_events]
    balance = raw['budget_balance']
    case_id = case['case_id']
    final_state = {'request_state': request['state'], 'po_count': len(pos)}
    budget = f'{balance / 100:.2f}'

    if case_id == 'C01':
        final_state.update(
            po_total=f"{pos[0]['amount'] / 100:.2f}" if pos else None,
            budget_balance=budget, approved_by=request['approved_by'],
            approval_matches_version=bool(
                pos and request['approved_version'] == pos[0]['version'] == request['version']),
        )
    elif case_id == 'C02':
        final_state['submitted_count'] = backend_types.count('REQUEST_SUBMITTED')
    elif case_id == 'C03':
        final_state.update(error_code=api_error, budget_balance=budget)
    elif case_id == 'C04':
        final_state['error_code'] = api_error
    elif case_id == 'C05':
        final_state.update(http_status=http_status,
                           approved_event_count=backend_types.count('HUMAN_APPROVED'))
    elif case_id == 'C06':
        routed = [event['detail'].get('approver') for event in backend_events
                  if event['event_type'] == 'REQUEST_ROUTED_M02']
        final_state['assigned_approver'] = routed[-1] if routed else None
    elif case_id == 'C07':
        final_state.update(supplier_review_count=len(raw['vendor_reviews']),
                           vendor_status=raw['vendor_status'])
    elif case_id == 'C08':
        reply_po_ids = [item.get('po_id') for item in (replies or []) if item]
        final_state.update(
            budget_balance=budget,
            same_po_id_in_replies=(len(reply_po_ids) >= 2 and len(set(reply_po_ids)) == 1
                                   and bool(pos) and reply_po_ids[0] == pos[0]['id']),
        )
    elif case_id == 'C09':
        final_state.update(
            current_approval_valid=bool(
                request['approved_version'] == request['version'] and request['approved_by']),
            historical_approval_preserved=any(
                item['version'] < request['version'] for item in raw['approvals']),
            budget_unchanged=(balance == 500000),
        )
    elif case_id == 'C10':
        lookup_ok = bool(
            lookup and pos and lookup.get('po_id') == pos[0]['id']
            and lookup.get('idem_key') == pos[0]['idem_key']
            and lookup.get('request_id') == rid and lookup.get('version') == pos[0]['version'])
        reports = [event for event in normalized_agent
                   if event['event_type'] == 'AGENT_REPORTED_CONFIRMED_PO']
        report_ok = bool(reports and lookup_ok and reports[-1].get('po_id') == pos[0]['id']
                         and reports[-1].get('idempotency_key') == pos[0]['idem_key'])
        final_state.update(lookup_po_matches_stored_po=lookup_ok,
                           budget_debit_count=backend_types.count('BUDGET_DEBITED'),
                           user_report_matches_db=report_ok)

    available = {
        'db_request': 'request' in raw, 'db_po': 'pos' in raw,
        'db_budget': 'budget_balance' in raw, 'db_vendor': 'vendor_status' in raw,
        'db_evidence': 'evidence_verified' in request,
        'approval_queue': any(event['event_type'] == 'REQUEST_ROUTED_M02' for event in backend_events),
        'supplier_review_queue': 'vendor_reviews' in raw,
        'idempotency_log': 'idempotency_records' in raw,
        'approval_history': 'approvals' in raw, 'audit_log': 'events' in raw,
        'identity_context': any(event['actor_id'] for event in backend_events),
        'field_sources': any(event['event_type'] == 'FIELD_SOURCE_RESOLVED' for event in backend_events),
        'api_response': http_status is not None or bool(replies) or bool(lookup),
        'fault_injection_log': any(event['event_type'] == 'HTTP_RESPONSE_DROPPED'
                                   for event in backend_events),
        'tool_trace': any(event['event_type'] == 'TOOL_CALL_COMPLETED'
                          for event in normalized_agent),
        'agent_trace': bool(normalized_agent),
        'trace_annotations': bool(trace_annotations),
    }
    proofs = {name: {'origin': SOURCE_ORIGINS[name], 'reference_id': f'{rid}/{name}'}
              for name, is_available in available.items() if is_available}
    return {
        'request_id': rid, 'observed_final_state': final_state,
        'observed_events': events, 'evidence_sources': proofs,
        'db_snapshot': raw, 'db_snapshot_sha256': _digest(raw),
        'trace_annotations': trace_annotations or [],
        'trace_annotations_sha256': _digest(trace_annotations or []),
        'evaluation_layer': case['evaluation_layer'],
        'agent_trace_status': (
            'NOT_REQUIRED' if not case['evaluation_layer'].startswith('agent_')
            else 'NOT_RUN' if agent_events is None
            else 'AVAILABLE' if normalized_agent else 'EVIDENCE_MISSING'),
    }


def _in_order(events, required):
    types = [event['event_type'] for event in events]
    index = -1
    missing = []
    for event_type in required:
        try:
            index = types.index(event_type, index + 1)
        except ValueError:
            missing.append(event_type)
    return missing


def _valid_time(value):
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return True
    except ValueError:
        return False


def check(case, observation):
    if observation is None:
        return {'status': 'NOT_RUN', 'errors': []}
    if not isinstance(observation, dict):
        return {'status': 'INVALID_OBSERVATION', 'errors': ['observation not an object']}
    if case['evaluation_layer'].startswith('agent_'):
        trace_status = observation.get('agent_trace_status')
        if trace_status == 'NOT_RUN':
            return {'status': 'NOT_RUN', 'errors': ['real Agent trace was not run']}
        if trace_status != 'AVAILABLE':
            return {'status': 'EVIDENCE_MISSING', 'errors': ['real Agent trace is missing']}

    final_state = observation.get('observed_final_state')
    events = observation.get('observed_events')
    sources = observation.get('evidence_sources')
    snapshot = observation.get('db_snapshot')
    if not isinstance(snapshot, dict):
        return {'status': 'EVIDENCE_MISSING', 'errors': ['trusted database snapshot is missing']}
    if not isinstance(final_state, dict) or not isinstance(events, list) or not isinstance(sources, dict):
        return {'status': 'INVALID_OBSERVATION', 'errors': ['final state, events or evidence sources missing']}

    errors = []
    missing_evidence = []
    request = snapshot.get('request', {})
    request_id = observation.get('request_id')
    if not request_id or request.get('id') != request_id:
        return {'status': 'INVALID_OBSERVATION', 'errors': ['observation request_id mismatch']}
    if observation.get('db_snapshot_sha256') != _digest(snapshot):
        errors.append('database snapshot changed after trusted normalization')
    annotations = observation.get('trace_annotations', [])
    if (not isinstance(annotations,list)
            or observation.get('trace_annotations_sha256') != _digest(annotations)):
        errors.append('trace annotations changed after trusted normalization')

    assertions = case['checker_assertions']
    for name in assertions['required_evidence_sources']:
        proof = sources.get(name)
        if proof is None:
            missing_evidence.append(f'missing trusted evidence source: {name}')
        elif (not isinstance(proof, dict) or proof.get('origin') != SOURCE_ORIGINS[name]
              or proof.get('reference_id') != f'{request_id}/{name}'):
            errors.append(f'invalid or cross-request evidence source: {name}')

    backend_events = [event for event in events if event.get('evidence_channel') == 'db_audit']
    agent_events = [event for event in events if event.get('evidence_channel') == 'agent_trace']
    if len(backend_events) + len(agent_events) != len(events):
        errors.append('unknown evidence event channel')
    if backend_events != [_backend_event(event) for event in snapshot.get('events', [])]:
        errors.append('observed backend events do not match evaluator DB audit snapshot')

    backend_sequences = [event.get('backend_sequence') for event in backend_events]
    if (any(not isinstance(value, int) for value in backend_sequences)
            or backend_sequences != sorted(backend_sequences)
            or len(backend_sequences) != len(set(backend_sequences))):
        errors.append('backend audit sequence is invalid')
    agent_indexes = [event.get('trace_index') for event in agent_events]
    if agent_events and (any(not isinstance(value, int) for value in agent_indexes)
                         or agent_indexes != sorted(agent_indexes)
                         or len(agent_indexes) != len(set(agent_indexes))):
        errors.append('Agent trace index is invalid')

    required_fields = ('event_type', 'run_id', 'request_id', 'request_version', 'actor_id',
                       'tool_call_id', 'po_id', 'idempotency_key', 'occurred_at', 'evidence_source')
    for event in events:
        absent = [name for name in required_fields if name not in event]
        if absent:
            errors.append(f'event contract fields absent: {absent}')
            continue
        if event['request_id'] != request_id:
            errors.append(f"{event['event_type']}: request_id mismatch")
        if not _valid_time(event['occurred_at']):
            errors.append(f"{event['event_type']}: occurred_at invalid")
        expected_source = ('backend_audit_db' if event['evidence_channel'] == 'db_audit'
                           else 'dify_trace_export')
        if event['evidence_source'] != expected_source:
            errors.append(f"{event['event_type']}: untrusted evidence_source")
    if len({event['run_id'] for event in agent_events if event.get('run_id')}) > 1:
        errors.append('Agent trace contains multiple run_id values')
    agent_run_ids={event['run_id'] for event in agent_events if event.get('run_id')}
    agent_trace_indexes={event['trace_index'] for event in agent_events}
    for annotation in annotations:
        if (not isinstance(annotation,dict)
                or annotation.get('evidence_source')!='human_trace_annotation'
                or not annotation.get('annotator_id')):
            errors.append('untrusted or incomplete human trace annotation')
            continue
        if annotation.get('request_id')!=request_id or annotation.get('run_id') not in agent_run_ids:
            errors.append('human trace annotation is not linked to Agent run/request')
        indexes=annotation.get('source_trace_indices')
        if (not isinstance(indexes,list) or not indexes
                or any(not isinstance(index,int) or index not in agent_trace_indexes for index in indexes)):
            errors.append('human trace annotation lacks source trace linkage')

    for key, expected in assertions['final_state_equals'].items():
        actual = final_state.get(key)
        if (actual not in expected if isinstance(expected, list) else actual != expected):
            errors.append(f'final_state.{key}: expected={expected!r} got={actual!r}')
    errors.extend(f'backend event missing/out of order: {name}' for name in
                  _in_order(backend_events, assertions['required_backend_events_in_order']))
    errors.extend(f'Agent event missing/out of order: {name}' for name in
                  _in_order(agent_events, assertions['required_agent_events_in_order']))
    all_types = [event['event_type'] for event in events]
    errors.extend(f'forbidden event present: {name}' for name in
                  assertions['forbidden_event_types_absent'] if name in all_types)

    pos = snapshot.get('pos', [])
    if final_state.get('request_state') != request.get('state') or final_state.get('po_count') != len(pos):
        errors.append('final state and database snapshot disagree')
    if ('budget_balance' in final_state
            and final_state['budget_balance'] != f"{snapshot['budget_balance'] / 100:.2f}"):
        errors.append('budget balance disagrees with database snapshot')
    if ('po_total' in final_state
            and (not pos or final_state['po_total'] != f"{pos[0]['amount'] / 100:.2f}")):
        errors.append('PO total disagrees with database snapshot')

    def backend(event_type):
        return [event for event in backend_events if event['event_type'] == event_type]

    approvals = snapshot.get('approvals', [])
    for event in backend('HUMAN_APPROVED'):
        if not any(row.get('request_id') == request_id
                   and row.get('version') == event['request_version']
                   and row.get('actor') == event['actor_id']
                   and row.get('decision') == 'APPROVED' for row in approvals):
            errors.append('approval audit event does not match approval table')
        expected_actor = 'M02' if request.get('amount', 0) >= 1000000 else 'M01'
        expected_role = 'senior_approver' if expected_actor == 'M02' else 'approver'
        if event['actor_id'] != expected_actor or event.get('actor_role') != expected_role:
            errors.append('approval actor or role is not authorized for request amount')

    idempotency = snapshot.get('idempotency_records', [])
    for po in pos:
        if (po.get('request_id') != request_id or po.get('version') != request.get('version')
                or po.get('amount') != request.get('amount')):
            errors.append('PO does not match current request/version/amount')
        if request.get('approved_version') != po.get('version') or request.get('approved_by') is None:
            errors.append('PO is not authorized by approval for the same version')
        if not any(row.get('po_id') == po.get('id') and row.get('request_id') == request_id
                   and row.get('version') == po.get('version')
                   and row.get('idem_key') == po.get('idem_key') for row in idempotency):
            errors.append('PO and idempotency record are not linked')

    created = backend('PO_CREATED')
    committed = backend('PO_COMMITTED')
    debited = backend('BUDGET_DEBITED')
    for event in created + committed + debited:
        if not any(event.get('po_id') == po.get('id')
                   and event.get('request_version') == po.get('version')
                   and event.get('idempotency_key') == po.get('idem_key') for po in pos):
            errors.append(f"{event['event_type']} does not match any stored PO")
    for po in pos:
        linked_events = [event for event in created + committed + debited
                         if event.get('po_id') == po.get('id')]
        if not linked_events or any(event.get('request_version') != po.get('version')
                                    or event.get('idempotency_key') != po.get('idem_key')
                                    for event in linked_events):
            errors.append('PO creation/debit events are not linked to the stored PO')
        matching_approval = [event for event in backend('HUMAN_APPROVED')
                             if event['request_version'] == po.get('version')]
        po_sequences = [event['backend_sequence'] for event in created + committed]
        if not matching_approval or not po_sequences or any(
                event['backend_sequence'] >= min(po_sequences) for event in matching_approval):
            errors.append('valid approval must occur before PO creation')
    if len(debited) != len(pos):
        errors.append('budget debit count is not equal to successful PO count')

    for event in backend('IDEMPOTENT_REPLAY'):
        if not any(row.get('po_id') == event.get('po_id')
                   and row.get('idem_key') == event.get('idempotency_key')
                   and row.get('request_id') == request_id
                   and row.get('version') == event.get('request_version') for row in idempotency):
            errors.append('idempotent replay does not match stored idempotency record')

    # 使用三元关联而不是同名事件：run_id + tool_call_id + backend_event_sequence。
    completed_calls = [event for event in agent_events if event['event_type'] == 'TOOL_CALL_COMPLETED']
    for event in completed_calls:
        sequence = event.get('detail', {}).get('backend_event_sequence')
        matches = [item for item in backend_events if item['backend_sequence'] == sequence]
        if len(matches) != 1:
            errors.append('Agent tool result lacks unique backend event reference')
            continue
        match = matches[0]
        if (event.get('run_id') != match.get('run_id')
                or event.get('tool_call_id') != match.get('tool_call_id')
                or event.get('request_version') != match.get('request_version')):
            errors.append('Agent tool result and backend audit linkage mismatch')

    if case['case_id'] == 'C01':
        resolved=backend('SUPPLIER_SOURCE_RESOLVED')
        if (not resolved or resolved[-1].get('detail',{}).get('source')!='trusted_supplier_master'
                or resolved[-1].get('detail',{}).get('supplier_id')!=request.get('supplier_id')
                or not resolved[-1].get('detail',{}).get('supplier_name')):
            errors.append('C01 supplier name is not linked to trusted master-data resolution')
    elif case['case_id'] == 'C02':
        asks = [event for event in agent_events if event['event_type'] == 'AGENT_ASKED_MISSING_FIELDS']
        required={'quantity','unit_price','supplier_name','purpose'}
        identified=set()
        if asks:
            detail=asks[-1].get('detail', {})
            identified.update(detail.get('identified_missing_fields', []))
            identified.update(detail.get('requested_slots', []))
            if 'supplier_id' in identified:
                identified.add('supplier_name')
            for annotation in annotations:
                if (annotation.get('annotation_type')=='missing_fields'
                        and asks[-1]['trace_index'] in annotation.get('source_trace_indices', [])):
                    identified.update(annotation.get('fields', []))
        if not asks or not required <= identified:
            errors.append('C02 trace/annotation lacks required missing fields')
    elif case['case_id'] == 'C05':
        denied = backend('AUTH_REJECTED')
        if not denied or denied[-1].get('actor_id') != 'U01' or denied[-1].get('actor_role') != 'requester':
            errors.append('C05 rejection is not linked to unauthorized requester identity')
    elif case['case_id'] == 'C06':
        routed = backend('REQUEST_ROUTED_M02')
        if not routed or routed[-1].get('detail', {}).get('approver') != 'M02' or not request.get('evidence_verified'):
            errors.append('C06 M02 route and trusted evidence are not established')
        if backend('HUMAN_APPROVED'):
            errors.append('C06 must await human approval')
    elif case['case_id'] == 'C07':
        if not snapshot.get('vendor_reviews') or any(
                row.get('request_id') != request_id or row.get('status') != 'PENDING'
                for row in snapshot['vendor_reviews']):
            errors.append('C07 supplier review is not linked to request')
    elif case['case_id'] == 'C09':
        version = request.get('version')
        if (not approvals or any(row['version'] >= version for row in approvals)
                or request.get('approved_by') is not None):
            errors.append('C09 historical approval is not isolated from current version')
    elif case['case_id'] == 'C10':
        reports = [event for event in agent_events
                   if event['event_type'] == 'AGENT_REPORTED_CONFIRMED_PO']
        lookups = backend('PO_LOOKUP_BY_KEY')
        if reports and lookups:
            report, lookup_event = reports[-1], lookups[-1]
            cause = report.get('detail', {}).get('caused_by_tool_call_id')
            linked_call = [event for event in completed_calls if event.get('tool_call_id') == cause]
            if (cause != lookup_event.get('tool_call_id') or not linked_call
                    or report.get('run_id') != lookup_event.get('run_id')
                    or report.get('po_id') != lookup_event.get('po_id')
                    or report.get('idempotency_key') != lookup_event.get('idempotency_key')):
                errors.append('C10 Agent report is not causally linked to trusted PO lookup')

    if errors:
        return {'status': 'FAIL', 'errors': errors}
    if missing_evidence:
        return {'status': 'EVIDENCE_MISSING', 'errors': missing_evidence}
    return {'status': 'PASS', 'errors': []}
