from collections import Counter
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_runtime import ModelToolCall, ModelTurn
from evaluation.case_loader import CaseLoader
from evaluation.unified import UnifiedEvaluator, catalog, object_hash, resumable
from evaluation.unified_adapters import NativeAdapters, artifacts, digest, read, save
from evaluation.unified_reporting import summarize


class UnifiedTests(unittest.TestCase):
    def test_current_catalog_is_120_unique_module_cases(self):
        entries = catalog(CaseLoader())
        self.assertEqual(Counter(e['module'] for e in entries), {'agent': 33, 'backend': 20, 'ir': 21, 'rag': 46})
        self.assertEqual(len({e['key'] for e in entries}), 120)
        self.assertTrue(all(e['version'] == 'rag-golden-1.2.1-accepted-candidate' for e in entries if e['module'] == 'rag'))

    def test_fingerprint_set_order_independent(self):
        self.assertEqual(object_hash({'x': {'A', 'B'}}), object_hash({'x': {'B', 'A'}}))

    def state(self, root, modules=('agent', 'backend')):
        reports = root / 'reports'
        reports.mkdir()
        return dict(batch_id='test', started_at='test', fingerprint='frozen', scope='PARTIAL_VALIDATION', model_config={},
                    run_dir=str(root), report_dir=str(reports), state_path=str(root / 'state.json'),
                    records={m+':TEST': dict(key=m+':TEST', module=m, case_id='TEST', case_fingerprint='test',
                                            final_result='NOT_RUN', execution_status='NOT_RUN') for m in modules})

    def result(self, root, module='backend', status='PASS'):
        path = root / (module + '.json')
        save(path, {'actual': 'input/output'})
        return dict(score={'suite': module, 'case_id': 'TEST', 'run_id': module+'-run', 'final_result': status, 'criteria': []},
                    execution_status='COMPLETED_AWAITING_SCORING', evidence_path=str(path), artifacts=artifacts([path]))

    def test_case_exception_does_not_stop_other_module_and_resume_retries_only_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self.state(root)
            evaluator = UnifiedEvaluator(state, {'protected_files': {}}, CaseLoader())
            evaluator.native.execute = Mock(side_effect=[RuntimeError('isolated fault'), self.result(root)])
            evaluator.execute()
            self.assertEqual(state['records']['agent:TEST']['final_result'], 'EVIDENCE_MISSING')
            self.assertEqual(state['records']['backend:TEST']['final_result'], 'PASS')
            first_error = state['records']['agent:TEST']['result_path']
            evaluator.native.execute = Mock(return_value=self.result(root, 'agent'))
            evaluator.execute()
            evaluator.native.execute.assert_called_once_with('agent', 'TEST')
            self.assertTrue(Path(first_error).exists())
            self.assertEqual(state['records']['agent:TEST']['previous_attempts'], [first_error])

    def test_fail_and_review_are_terminal_valid_evidence_not_retried_to_raise_score(self):
        for status in ('FAIL', 'REVIEW_REQUIRED'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                state = self.state(root, ('backend',))
                evaluator = UnifiedEvaluator(state, {'protected_files': {}}, CaseLoader())
                evaluator.native.execute = Mock(return_value=self.result(root, status=status))
                evaluator.execute()
                evaluator.execute()
                self.assertEqual(evaluator.native.execute.call_count, 1)

    def test_tampered_evidence_is_not_resumable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = self.result(root)
            record.update(fingerprint='frozen', final_result='PASS')
            path = root / 'result.json'
            save(path, record)
            record.update(result_path=str(path), result_sha256=digest(path))
            self.assertTrue(resumable(record, 'frozen'))
            save(Path(record['evidence_path']), {'tampered': True})
            self.assertFalse(resumable(record, 'frozen'))

    def test_changed_frozen_input_stops_execution_truthfully(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self.state(root)
            path = root / 'golden.json'
            save(path, {'old': True})
            frozen = digest(path)
            save(path, {'changed': True})
            evaluator = UnifiedEvaluator(state, {'protected_files': {str(path): frozen}}, CaseLoader())
            evaluator.native.execute = Mock()
            evaluator.execute()
            evaluator.native.execute.assert_not_called()
            self.assertFalse(state['integrity']['unchanged'])
            self.assertEqual({r['final_result'] for r in state['records'].values()}, {'NOT_RUN'})

    def test_metrics_do_not_average_modules_or_hide_review_and_missing(self):
        state = {'records': {
            'a': dict(module='agent', final_result='PASS'),
            'b': dict(module='agent', final_result='FAIL'),
            'c': dict(module='agent', final_result='REVIEW_REQUIRED'),
            'd': dict(module='agent', final_result='EVIDENCE_MISSING'),
            'e': dict(module='backend', final_result='PASS', provenance='HISTORICAL_VALIDATED'),
        }}
        result = summarize(state)
        self.assertIsNone(result['overall_score'])
        self.assertEqual(result['modules']['agent']['pass_over_decisive']['value'], 0.5)
        self.assertEqual(result['modules']['agent']['pass_over_planned']['value'], 0.25)
        self.assertEqual(result['pending_count'], 2)
        self.assertFalse(result['is_full_selection'])

    def test_ir_uses_original_checks_with_raw_model_trace_and_isolated_database(self):
        model = Mock()
        model.complete.return_value = ModelTurn(None, [ModelToolCall('ir-test', 'propose_task_state',
            {'intent': 'create', 'item': '电脑', 'quantity': 2, 'question': '请补充单价、供应商、用途。'})])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('evaluation.unified_adapters.default_real_model_factory', return_value=(model, {'model_name': 'scripted-test'})), \
                    patch('evaluation.unified_adapters.build_default_index', return_value=(Mock(), {})):
                result = NativeAdapters(CaseLoader(), root, root / 'reports').execute('ir', 'IR07')
            self.assertEqual(result['score']['final_result'], 'PASS')
            original = result['score']['original_ir_result']
            self.assertTrue(all(original['checks'].values()))
            self.assertEqual(original['request_count_before'], original['request_count_after'])
            model.client.close.assert_called_once()
            path = Path(result['evidence_path'])
            self.assertTrue((path.parent / 'model_calls/01.json').exists())
            self.assertTrue((path.parent / 'fixture.sqlite').exists())
            self.assertTrue(read(path)['trace']['events'])

    def test_historical_cache_is_counted_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self.state(root, ('rag',))
            evaluator = UnifiedEvaluator(state, {'protected_files': {}}, CaseLoader(), reuse_evidence=True)
            evaluator.rag.cached = Mock(return_value=self.result(root, 'rag', 'REVIEW_REQUIRED'))
            evaluator.rag.execute = Mock()
            result = evaluator.execute()
            evaluator.rag.execute.assert_not_called()
            self.assertEqual(result['modules']['rag']['provenance_counts'], {'HISTORICAL_VALIDATED': 1})


if __name__ == '__main__':
    unittest.main()
