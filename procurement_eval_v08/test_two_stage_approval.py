"""两级审批定向回归：真实临时HTTP/SQLite，不调用真实模型或演示库。"""
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import test_simulator_v1 as helpers
from scripts.repair_approval_routes import repair_routes


class TwoStageApprovalTests(unittest.TestCase):
    setUp = helpers.SimulatorV1Tests.setUp
    tearDown = helpers.SimulatorV1Tests.tearDown
    call = helpers.SimulatorV1Tests.call
    create = helpers.SimulatorV1Tests.create

    def high(self, rid='HIGH', supplier='启明办公', price='10998.00'):
        self.create(rid, supplier=supplier, quantity=1, price=price)
        self.assertEqual(self.call('POST', '/__eval__/seed_budget', 'demo-eval',
                                  {'request_id': rid, 'budget_id': 'B_HIGH'})[0], 200)
        return rid

    def raw(self, rid):
        with self.app.store.connect() as db:
            return {'request': dict(db.execute('SELECT * FROM requests WHERE id=?', (rid,)).fetchone()),
                    'approvals': [dict(r) for r in db.execute('SELECT * FROM approvals WHERE request_id=? ORDER BY id', (rid,))],
                    'pos': [dict(r) for r in db.execute('SELECT * FROM pos WHERE request_id=?', (rid,))],
                    'budgets': [tuple(r) for r in db.execute('SELECT * FROM budgets ORDER BY id')],
                    'debits': db.execute("SELECT count(*) FROM events WHERE request_id=? AND type='BUDGET_DEBITED'", (rid,)).fetchone()[0]}

    def approve(self, rid, actor, version=1):
        return self.call('POST', f'/requests/{rid}/approve', 'demo-' + actor.lower(), {'expected_version': version})

    def test_01_low_and_exact_threshold_use_m01_only(self):
        for index, price in enumerate(('9999.99', '10000.00')):
            rid = self.high('LOW-' + str(index), price=price)
            self.assertEqual(self.call('POST', f'/requests/{rid}/submit', data={})[1],
                             {'state': 'SUBMITTED', 'assigned_approver': 'M01'})
            before = self.raw(rid)
            self.assertEqual(self.approve(rid, 'M02')[0], 403)
            self.assertEqual(self.raw(rid), before)
            self.assertEqual(self.approve(rid, 'M01')[1]['state'], 'APPROVED')
            row = self.raw(rid)
            self.assertEqual([x['actor'] for x in row['approvals']], ['M01'])
            self.assertEqual(row['budgets'], before['budgets'])

    def test_02_high_sequential_state_visibility_and_final_approval(self):
        rid = self.high(price='10000.01')
        self.assertEqual(self.call('POST', f'/requests/{rid}/submit', data={})[1]['state'], 'SUBMITTED')
        listing = self.call('GET', '/requests', 'demo-m01')[1]['requests']
        detail = next(x for x in listing if x['request_id'] == rid)
        self.assertEqual((detail['approval']['current_approver'], detail['approval']['required_approver']), ('M01', 'M02'))
        before = self.raw(rid)
        self.assertEqual(self.approve(rid, 'M02')[0], 403)
        self.assertEqual(self.raw(rid), before)
        first = self.approve(rid, 'M01')
        self.assertEqual((first[0], first[1]['state']), (200, 'PENDING_HIGH_APPROVAL'))
        after = self.raw(rid)
        self.assertIsNone(after['request']['approved_by'])
        self.assertIsNone(after['request']['approved_version'])
        self.assertEqual(after['budgets'], before['budgets'])
        self.assertEqual([x['actor'] for x in after['approvals']], ['M01'])
        detail = self.call('GET', f'/requests/{rid}', 'demo-m02')[1]
        self.assertEqual(detail['approval']['current_approver'], 'M02')
        self.assertFalse(detail['approval']['approval_valid'])
        self.assertEqual(self.approve(rid, 'M01')[0], 403)
        self.assertEqual(self.approve(rid, 'M02')[1]['error_code'], 'EVIDENCE_REQUIRED')
        self.call('POST', f'/requests/{rid}/evidence', 'demo-m02', {})
        self.assertEqual(self.approve(rid, 'M02')[1]['state'], 'APPROVED')
        final = self.call('GET', f'/requests/{rid}', 'demo-m02')[1]
        self.assertTrue(final['approval']['approval_valid'])
        self.assertIsNone(final['approval']['current_approver'])
        self.assertEqual([x['actor'] for x in self.raw(rid)['approvals']], ['M01', 'M02'])

    def test_03_legacy_high_state_cannot_bypass_m01_even_with_evidence(self):
        rid = self.high()
        with self.app.store.connect() as db:
            db.execute("UPDATE requests SET state='PENDING_HIGH_APPROVAL',evidence_verified=1 WHERE id=?", (rid,))
        before = self.raw(rid)
        self.assertEqual(self.approve(rid, 'M02')[1]['error_code'], 'M01_APPROVAL_REQUIRED')
        self.assertEqual(self.raw(rid), before)

    def test_04_version_change_invalidates_both_levels_and_evidence(self):
        rid = self.high()
        self.call('POST', f'/requests/{rid}/submit', data={})
        self.approve(rid, 'M01')
        self.call('POST', f'/requests/{rid}/evidence', 'demo-m02', {})
        self.approve(rid, 'M02')
        self.assertEqual(self.call('PATCH', f'/requests/{rid}/update', 'demo-agent',
                                  {'purpose': '改用途', 'expected_version': 1})[1]['version'], 2)
        row = self.raw(rid)
        self.assertEqual(row['request']['state'], 'REAPPROVAL_REQUIRED')
        self.assertIsNone(row['request']['approved_version'])
        self.assertEqual(row['request']['evidence_verified'], 0)
        self.assertEqual(len(row['approvals']), 2)
        self.assertEqual(self.call('POST', f'/requests/{rid}/po', 'demo-agent',
                                  {'idempotency_key': 'STALE'})[1]['error_code'], 'STALE_APPROVAL')
        self.call('POST', f'/requests/{rid}/submit', data={})
        self.assertEqual(self.approve(rid, 'M01', 1)[1]['error_code'], 'VERSION_CONFLICT')
        self.assertEqual(self.approve(rid, 'M02', 2)[0], 403)
        self.assertEqual(self.approve(rid, 'M01', 2)[1]['state'], 'PENDING_HIGH_APPROVAL')
        self.assertEqual(self.approve(rid, 'M02', 2)[1]['error_code'], 'EVIDENCE_REQUIRED')
        self.call('POST', f'/requests/{rid}/evidence', 'demo-m02', {})
        self.assertEqual(self.approve(rid, 'M02', 2)[1]['state'], 'APPROVED')
        self.assertEqual([(x['version'], x['actor']) for x in self.raw(rid)['approvals']],
                         [(1, 'M01'), (1, 'M02'), (2, 'M01'), (2, 'M02')])

    def test_05_po_requires_complete_chain_and_debits_once(self):
        rid = self.high()
        self.call('POST', f'/requests/{rid}/submit', data={})
        before = self.raw(rid)
        self.approve(rid, 'M01')
        self.assertEqual(self.call('POST', f'/requests/{rid}/po', 'demo-agent', {'idempotency_key': 'EARLY'})[0], 409)
        self.assertEqual(self.raw(rid)['budgets'], before['budgets'])
        self.call('POST', f'/requests/{rid}/evidence', 'demo-m02', {})
        self.approve(rid, 'M02')
        self.assertEqual(self.call('POST', f'/requests/{rid}/po', 'demo-u01', {'idempotency_key': 'ONCE'})[0], 200)
        self.assertTrue(self.call('POST', f'/requests/{rid}/po', 'demo-u01', {'idempotency_key': 'ONCE'})[1]['replayed'])
        row = self.raw(rid)
        self.assertEqual((len(row['pos']), row['debits']), (1, 1))
        balances = {r[0]: r[1] for r in row['budgets']}
        self.assertEqual(balances['B_HIGH'], 2000000 - 1099800)

    def test_06_po_rejects_forged_final_state_missing_or_unordered_steps(self):
        for index, actors in enumerate(([], ['M02'], ['M02', 'M01'])):
            rid = self.high('FORGED-' + str(index))
            with self.app.store.connect() as db:
                db.execute("UPDATE requests SET state='APPROVED',approved_by='M02',approved_version=1,evidence_verified=1 WHERE id=?", (rid,))
                for actor in actors:
                    db.execute("INSERT INTO approvals(request_id,version,actor,decision) VALUES (?,1,?,'APPROVED')", (rid, actor))
            before = self.raw(rid)
            self.assertEqual(self.call('POST', f'/requests/{rid}/po', 'demo-agent',
                                      {'idempotency_key': rid})[1]['error_code'], 'APPROVAL_CHAIN_INCOMPLETE')
            self.assertEqual(self.raw(rid), before)

    def test_07_vendor_admission_enters_same_first_stage(self):
        rid = self.high(supplier='新锐科技')
        self.assertEqual(self.call('POST', f'/requests/{rid}/submit', data={})[1]['state'], 'PENDING_VENDOR_REVIEW')
        reviews = self.call('GET', '/admin/overview', 'demo-m02')[1]['vendor_reviews']
        review = next(r for r in reviews if r['request_id'] == rid)
        response = self.call('POST', f"/admin/vendor-reviews/{review['id']}/decision", 'demo-m02', {'decision': 'APPROVED'})
        self.assertEqual(response[1]['state'], 'SUBMITTED')
        self.assertEqual(self.call('GET', f'/requests/{rid}', 'demo-m01')[1]['approval']['current_approver'], 'M01')
        self.assertEqual(self.approve(rid, 'M02')[0], 403)
        self.assertEqual(self.approve(rid, 'M01')[1]['state'], 'PENDING_HIGH_APPROVAL')

    def test_08_agent_and_requester_cannot_approve_or_forge_actor(self):
        rid = self.high()
        self.call('POST', f'/requests/{rid}/submit', data={})
        before = self.raw(rid)
        for token in ('demo-agent', 'demo-u01'):
            self.assertEqual(self.call('POST', f'/requests/{rid}/approve', token, {'actor': 'M01'})[1]['error_code'], 'APPROVAL_FORBIDDEN')
        self.assertEqual(self.raw(rid), before)

    def test_09_safe_migration_preview_apply_and_repeat_preserve_data(self):
        rid = self.high()
        with self.app.store.connect() as db:
            db.execute("UPDATE requests SET state='PENDING_HIGH_APPROVAL' WHERE id=?", (rid,))
        before = self.raw(rid)
        with self.app.store.connect() as db:
            preview = repair_routes(db)
        self.assertEqual(preview['eligible'], [rid])
        self.assertEqual(self.raw(rid), before)
        with self.app.store.connect() as db:
            self.assertEqual(repair_routes(db, apply=True)['repaired'], [rid])
        after = self.raw(rid)
        expected = dict(before['request'], state='SUBMITTED')
        self.assertEqual(after, {**before, 'request': expected})
        with self.app.store.connect() as db:
            self.assertEqual(repair_routes(db, apply=True)['repaired'], [])
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE type='APPROVAL_ROUTE_REPAIRED'").fetchone()[0], 1)

    def test_10_migration_never_changes_existing_approval_records(self):
        rid = self.high()
        self.call('POST', f'/requests/{rid}/submit', data={})
        self.approve(rid, 'M01')
        before = self.raw(rid)
        with self.app.store.connect() as db:
            self.assertEqual(repair_routes(db, apply=True)['skipped'], [rid])
        self.assertEqual(self.raw(rid), before)

    def test_11_pages_use_current_and_final_nodes(self):
        page = Path(__file__).with_name('simulator.html').read_text(encoding='utf-8')
        self.assertIn("r.approval.current_approver==='M01'", page)
        self.assertIn("r.approval.current_approver==='M02'", page)
        self.assertIn('当前审批人 / 最终级别', page)
        self.assertIn('expected_version:Number(b.dataset.version)', page)
        chat = Path(__file__).with_name('agent.html').read_text(encoding='utf-8')
        self.assertIn("['当前审批人',result.approval?.current_approver", chat)
        self.assertIn("['最终审批级别',result.approval?.required_approver", chat)

    def test_12_old_version_m01_cannot_authorize_current_m02(self):
        rid = self.high()
        with self.app.store.connect() as db:
            db.execute("INSERT INTO approvals(request_id,version,actor,decision) VALUES (?,1,'M01','APPROVED')", (rid,))
            db.execute("UPDATE requests SET version=2,state='PENDING_HIGH_APPROVAL',evidence_verified=1 WHERE id=?", (rid,))
        before = self.raw(rid)
        self.assertEqual(self.approve(rid, 'M02', 2)[1]['error_code'], 'M01_APPROVAL_REQUIRED')
        self.assertEqual(self.raw(rid), before)

    def test_13_concurrent_first_stage_is_recorded_once(self):
        rid = self.high()
        self.call('POST', f'/requests/{rid}/submit', data={})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.approve(rid, 'M01'), range(2)))
        self.assertEqual(sorted(r[0] for r in results), [200, 403])
        row = self.raw(rid)
        self.assertEqual([x['actor'] for x in row['approvals']], ['M01'])
        self.assertEqual(row['request']['state'], 'PENDING_HIGH_APPROVAL')


if __name__ == '__main__':
    unittest.main()
