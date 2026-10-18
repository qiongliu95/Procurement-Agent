"""验证Baseline适配器不会把文档命中、无效引文或错误引用算作通过。"""
import json
from pathlib import Path
import tempfile
import unittest

from agent_tools import ProcurementTransportError
from evaluation import rag_baseline as run
from evaluation import rag_baseline_scoring as scoring


class BaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run.load_env_file(run.PROJECT/'.env')
        cls.sources=run.golden.load_sources()
        cls.index=run.FrozenIndex()
        cls.mapping=run.source_mapping(cls.index,cls.sources)
        cls.case=next(c for c in run.cases() if c['case_id']=='RGV1-A01')

    def test_all_existing_chunks_map_exactly(self):
        self.assertEqual(len(self.mapping),364)
        self.assertTrue(all(m['char_end']>m['char_start'] for m in self.mapping.values()))

    def test_document_hit_is_not_fact_hit(self):
        hit=next(c.public(.9) for c in self.index._chunks if c.doc_id=='KB-003' and c.heading=='适用范围') if any(c.doc_id=='KB-003' and c.heading=='适用范围' for c in self.index._chunks) else next(c.public(.9) for c in self.index._chunks if c.doc_id=='KB-003' and c.heading=='概要')
        self.assertEqual(run.canonical_recall(self.case,[hit],self.mapping,self.sources)['covered'],0)

    def test_correct_source_interval_is_recalled(self):
        hit=next(c.public(.9) for c in self.index._chunks if c.doc_id=='KB-003' and c.heading=='审批金额分界')
        self.assertEqual(run.canonical_recall(self.case,[hit],self.mapping,self.sources)['covered'],1)

    def test_whitespace_gaps_do_not_hide_missing_negation(self):
        text='不得\n修改';span={'char_start':0,'char_end':len(text)}
        self.assertFalse(run.span_covered(text,span,[(1,2),(3,5)]))
        self.assertTrue(run.span_covered(text,span,[(0,2),(3,5)]))

    def test_index_rebuild_is_forbidden(self):
        with self.assertRaises(AssertionError):self.index.ensure_index(force=True)

    def test_business_write_api_is_blocked(self):
        guard=run.BusinessApiGuard()
        with self.assertRaises(ProcurementTransportError):guard.create_request({},run_id='test',tool_call_id='test')
        self.assertEqual(guard.attempts,[{'method':'POST','path':'/requests','blocked':True}])

    def test_optional_point_is_not_required(self):
        self.assertEqual({p['point_id'] for p in scoring.required_payload(self.case)['required_points']},{'P1','P2'})

    def test_quote_normalization_keeps_business_words(self):
        self.assertTrue(scoring.quote_in('M02审批','**M02** 审批'))
        self.assertFalse(scoring.quote_in('10000元不需要凭证','10000元需要凭证'))

    def test_score_two_with_missing_point_is_flagged(self):
        payload=dict(score=2,confidence=.9,point_results=[dict(point_id='P1',status='SATISFIED',answer_quote='M02审批'),dict(point_id='P2',status='MISSING')],violated_claims=[])
        warnings=scoring.validate_payload('correctness',payload,self.case,{'response':{'message':'M02审批'}},[])
        self.assertIn('CORRECTNESS_SCORE_CONTRADICTS_DETAILS',warnings)

    def test_source_title_cannot_replace_supporting_quote(self):
        payload=dict(score=2,confidence=.9,fact_results=[dict(fact_id='F1',status='SUPPORTED',evidence=[dict(chunk_id='KB-003#002',quote='审批金额与凭证规则')])])
        warnings=scoring.validate_payload('retrieval',payload,self.case,{'response':{'message':''}},[dict(chunk_id='KB-003#002',text='金额达到10000元需要M02及报价凭证')])
        self.assertTrue(any('RETRIEVAL_SUPPORT_QUOTE_INVALID' in x for x in warnings))

    def test_class_metadata_can_support_faithfulness_only(self):
        payload=dict(score=2,confidence=.9,claim_results=[dict(claim='B类建议',status='SUPPORTED',evidence=[dict(chunk_id='x',quote='"class": "B"')])])
        warnings=scoring.validate_payload('faithfulness',payload,self.case,{'response':{'message':'B类建议'}},[dict(chunk_id='x',text='建议正文',**{'class':'B'})])
        self.assertEqual(warnings,[])

    def test_direct_router_answer_uses_visible_tool_chunks(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp);(path/'model_calls').mkdir()
            hit=dict(chunk_id='x',text='真实已检索证据')
            call=dict(messages=[dict(role='tool',content=json.dumps(dict(hits=[hit])))],model_turn=dict(content='根据证据作答'))
            (path/'model_calls/01.json').write_text(json.dumps(call),encoding='utf-8')
            e=dict(evidence_path=str(path/'evidence.json'),response=dict(message='根据证据作答'),retrievals=[dict(hits=[hit])],hits=[hit])
            self.assertEqual(scoring.effective_hits(e,dict(trace=dict(events=[]))),[hit])


if __name__=='__main__':unittest.main()
