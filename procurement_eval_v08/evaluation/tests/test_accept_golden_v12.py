"""验收门禁的负例：证据篡改、失效引用和错误的必要召回不可被放过。"""
import copy
import json
from pathlib import Path
import unittest

from evaluation import accept_golden_v12 as acceptance


class AcceptanceGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=acceptance.RUN/'candidate_after_repairs.jsonl'
        cls.cases=[json.loads(x) for x in path.read_text(encoding='utf-8').splitlines()]
        cls.sources=acceptance.golden.load_sources()

    def rejected(self,edit):
        cases=copy.deepcopy(self.cases)
        edit(cases)
        with self.assertRaises((AssertionError,KeyError)):
            acceptance.validate(cases,self.sources,write_report=False)

    def test_final_data_passes(self):
        self.assertEqual(acceptance.validate(self.cases,self.sources,write_report=False)['passed'],46)

    def test_changed_quote_is_rejected(self):
        self.rejected(lambda c:c[0]['golden_evidence'][0].update(evidence_text='一万元由M01审批'))

    def test_changed_hash_is_rejected(self):
        self.rejected(lambda c:c[0]['golden_evidence'][0].update(source_hash='0'*64))

    def test_unresolved_point_reference_is_rejected(self):
        self.rejected(lambda c:c[0]['answer_key_points'][0].update(fact_ids=['NONEXISTENT']))

    def test_optional_fact_cannot_be_required_by_checker(self):
        def edit(cases):
            c=next(x for x in cases if x['case_id']=='RGV1-D08')
            c['checker_spec']['answer_correctness']['fact_ids'].append('F1')
        self.rejected(edit)

    def test_single_document_cannot_be_declared_cross_document(self):
        def edit(cases):
            next(x for x in cases if x['case_id']=='RGV1-C04')['scenario_type']='C'
        self.rejected(edit)


if __name__=='__main__':unittest.main()
