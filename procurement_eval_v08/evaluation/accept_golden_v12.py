"""v1.2 独立收口：复用原文定位器及已有 DeepEval 适配器，不接触业务运行器。"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import copy
import csv
import importlib.metadata as metadata
import importlib.util
import io
import json
from pathlib import Path
import sys
import zipfile

from . import golden_dataset as golden

# 独立模块实例复用适配器，避免修改原模块全局变量而污染既有审计和同进程测试。
_adapter_spec=importlib.util.spec_from_file_location('evaluation._acceptance_v12_judge',Path(__file__).with_name('audit_golden_v11.py'))
audit=importlib.util.module_from_spec(_adapter_spec)
sys.modules[_adapter_spec.name]=audit
_adapter_spec.loader.exec_module(audit)

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
ZIP = ROOT / 'RAG Golden v1.2 完整包.zip'
RUN = PROJECT / 'evaluation/runs/rag_golden_v1_2_acceptance'
REPORT = PROJECT / 'evaluation/reports/rag_golden_v1_2_acceptance'
DATA = ROOT / '评估case/rag_golden_v1_2_accepted_candidate'
VERSION = 'rag-golden-1.2.1-accepted-candidate'
# 仅替换本进程内适配器的输出位置，旧审计脚本、原始记录和Rubric均不修改。
audit.RUN, audit.REPORT = RUN, REPORT
save, dump, sha = audit.save, audit.dump, audit.sha


def snapshot():
    result = golden.snapshot_protected()
    for path in [ZIP, ROOT / '采购Agent_RAG_Golden_46Case_语义优化_v1.1.zip']:
        if path.exists(): result[str(path.relative_to(ROOT))] = sha(path.read_bytes())
    for folder in [ROOT / '评估case/rag_golden_v1', PROJECT / 'evaluation/reports/rag_golden_v1_1_quality_audit', PROJECT / 'evaluation/runs/rag_golden_v1_1_quality_audit']:
        for path in folder.rglob('*'):
            if path.is_file(): result[str(path.relative_to(ROOT))] = sha(path.read_bytes())
    return result


def original():
    if not ZIP.is_file(): raise FileNotFoundError(str(ZIP))
    with zipfile.ZipFile(ZIP) as z:
        raw = z.read('rag_golden_v1_2/evaluation_cases.jsonl')
        manifest = json.loads(z.read('rag_golden_v1_2/manifest.json'))
        assert sha(raw) == manifest['new_cases_sha256']
        for name in z.namelist():
            target = (RUN / 'input' / name).resolve()
            assert target.is_relative_to((RUN / 'input').resolve()), '不安全压缩路径'
            if name.endswith('/'): continue
            content = z.read(name)
            if target.exists(): assert target.read_bytes() == content
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        cases = [json.loads(x) for x in raw.decode('utf-8-sig').splitlines() if x.strip()]
    assert len(cases) == len({c['case_id'] for c in cases}) == 46
    return cases, manifest


def write_cases(path, cases):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(c, ensure_ascii=False) + '\n' for c in cases), encoding='utf-8')


def build_cases(cases, sources):
    result = copy.deepcopy(cases)
    changes = []
    by_id = {c['case_id']: c for c in result}

    def change(cid, field, value, reason):
        c = by_id['RGV1-' + cid]
        if c.get(field) == value: return
        changes.append(dict(case_id=c['case_id'], field=field, before=copy.deepcopy(c.get(field)), after=copy.deepcopy(value), reason=reason))
        c[field] = value

    def add_fact(c, fid, eid, doc, line, judgment):
        e = golden.locate(sources[doc], line, line)
        e.update(evidence_id=eid, fact_id=fid, role='fact')
        c['golden_evidence'].append(e)
        c['required_facts'].append(dict(fact_id=fid, text=e['evidence_text'], evidence_ids=[eid], required_judgment=judgment, source_class=e['source_class'], authority='按正式原文适用范围，不把B类建议升级为公司强制规则'))

    def point(pid, text, fids, role='REQUIRED'):
        return dict(point_id=pid, criterion=text, fact_ids=fids, scoring_role=role)

    # 全文已能支撑当前问题时，以用户真正所问的最小判断替代题外通用优先级。
    c = by_id['RGV1-C04']
    add_fact(c, 'F3', 'E3', 'PKB-V3-029', 24, '按用途确认必要性能')
    add_fact(c, 'F4', 'E4', 'PKB-V3-029', 28, '配置及扩容匹配')
    change('C04', 'answer_key_points', [
        point('P1', '先明确实际办公用途及必要性能、兼容或扩容需求，不能只凭低价定型号；不要求穷举所有配置参数', ['F3', 'F4']),
        point('P2', '核对工位外设、网络电源及空间迁移等实际部署约束，并注意小机箱可能限制扩展；示例不必全部列举', ['F1']),
        point('P3', '通用的兼容、维护、交付和质保到价格比较顺序，仅作补充', ['F2'], 'OPTIONAL')
    ], '029全文L24/28/32足以回答用途和部署；026的完整选型顺序不是本题必要要求')
    change('C04', 'golden_answer', '先明确电脑的实际用途和必须满足的性能，例如普通办公、多屏分析或设计任务，再核对相应配置、接口及扩容需求。部署时按工位实际情况核对显示器和输入设备、网络与电源、空间及旧设备迁移成本。小机箱便于部署，但不一定适合后续扩展，不能只凭体积和低价定型号。', '仅保留问题所需、029全文可支持的用途及部署判断')
    change('C04', 'question_conditions', [dict(question_text='最便宜的小机箱电脑',fact_ids=['F1','F4'],treatment='source_supported_judgment'),dict(question_text='用途和部署条件',fact_ids=['F3','F1'],treatment='source_supported_judgment')], '用途与部署应分别绑定029的场景分级和部署段，不能用026的优先级段单独支持部署')
    # 052全文包含状态、版本、PO限制及重新确认，006作为权威性补充保留，但不强制召回。
    c = by_id['RGV1-D03']
    for fid,eid,line,j in [('F3','E3',28,'先核对变更所处状态'),('F4','E4',40,'PO后禁止Agent修改及冲突刷新'),('F5','E5',44,'READ确认写入及状态版本校验')]:
        add_fact(c, fid, eid, 'PKB-V4-052', line, j)
    change('D03', 'answer_key_points', [point('P1', '先读取状态与版本，写入前重新确认并遵守后端校验；已生成PO的申请不能通过Agent修改', ['F3','F4','F5']), point('P2','保留原需求及变更理由，重新评估金额、审批权限与交期', ['F2'])], '052 L28/32/40/44已足够；006的公司规则作为补充核对，不强制双源')
    change('D03', 'golden_answer', '先读取目标申请当前的状态和版本，明确变更内容，并遵守后端状态与版本校验；已生成PO后不能通过Agent继续修改该申请，版本冲突需刷新后重评估。如状态允许修改，应保留原需求和变更理由，重新核对金额、审批权限及交期，并取得新一轮确认，不能沿用旧确认强行覆盖。', '052全文支持全部必要步骤；避免扩展未被问到的权限实现细节')
    change('D03', 'question_conditions', [dict(question_text='正式修改之前',fact_ids=['F3','F4','F5'],treatment='source_supported_judgment'),dict(question_text='金额、审批和交期',fact_ids=['F2'],treatment='source_supported_judgment')], '必要条件与052全文的最小充分来源保持一致')
    c = by_id['RGV1-D04']
    add_fact(c, 'F4', 'E4', 'PKB-V4-055', 28, '核对原订单与替代约定')
    change('D04', 'answer_key_points', [point('P1', '不能因已签收就默认替代型号验收合格，应核对订单规格和替代约定', ['F2','F4']), point('P2','愿接受替代时，记录差异并明确价格调整、保修及正式授权', ['F2']), point('P3','用途接近不代表合同履约等效', ['F3'])], '055 L28/32/40足以回答，不强制022收货定义及题外验收清单')
    change('D04', 'golden_answer', '不能直接认定合格。应核对原订单、规格确认和替代约定，记录到货型号与约定的差异。若业务愿意接受替代，需明确价格调整、保修及正式授权，确认前不能默认验收通过。用途相近不代表合同履约等效。', '聚焦替代型号是否可验收；055独立支撑全部判断')
    c = by_id['RGV1-D08']
    pts = copy.deepcopy(c['answer_key_points'])
    pts[0].update(criterion='数据清理是与题设相关的补充建议；可核对重复和退款等，不要求列举所有清洗维度', scoring_role='OPTIONAL')
    pts[1]['criterion'] = '应先建立真实可采购且规格、数量及费用口径可比的基线；不要求穷举所有费用示例'
    change('D08','answer_key_points',pts,'用户问能否直接计算；065的可比基线边界单独足以判断，024是合理补充而非必要来源')
    change('D08','answerability','FULL','所问是方法可行性而非索取实际节约数值，现有原文足以完整回答不能直接计算')
    change('D08','missing_information',[],'FULL方法判断无需虚构缺失的用户请求')
    change('C02','answerability','FULL','问题询问承诺能否保证和应确认什么，不要求未知的实际维修时间或费用')
    change('C02','missing_information',[],'现有原文足以回答保证边界及澄清方法')
    # 与A01同类的题外硬点；不要求只问审批级别的回答同时补述凭证规则。
    pts = copy.deepcopy(by_id['RGV1-A02']['answer_key_points']); pts[2]['scoring_role']='OPTIONAL'
    change('A02','answer_key_points',pts,'问题只比较审批级别，报价材料是有据的可选补充')
    pts = copy.deepcopy(by_id['RGV1-B08']['answer_key_points']); pts[1]['scoring_role']='OPTIONAL'
    change('B08','answer_key_points',pts,'用户只问论坛能否当报价及缺资料能否推断信用，不要求调研报告格式')
    pts = copy.deepcopy(by_id['RGV1-B01']['answer_key_points']); pts[1]['criterion']='预算上限不等于商品单价；单价须有可靠来源，不能直接用总预算填入'
    change('B01','answer_key_points',pts,'不强制用户未询问的金额计算公式，与本题Golden保持一致')
    pts = copy.deepcopy(by_id['RGV1-B15']['answer_key_points'])
    pts[2]['criterion']='短期按风险选择合理备货、替代验证、第二来源开发或长期供货等适用缓释方案，不要求穷举；兼容、认证和测试成本仅在建议实际切换时需要考虑'
    change('B15','answer_key_points',pts,'问题明确短期换不了供应商，不应强制简答讨论尚未发生的切换细节；实际建议切换时仍需保留原文条件')
    for cid, doc, reason in [
        ('C04','PKB-V3-029','029 L24/28/32涵盖用途、配置及部署；026顺序仅补充'),
        ('D03','PKB-V4-052','052 L28/32/40/44涵盖状态、版本、PO限制、重新核算与确认'),
        ('D04','PKB-V4-055','055 L28/32/40已足以拒绝默认验收及给出替代条件'),
        ('D08','PKB-V4-065','065 L28/40单独足以判断缺乏可比基线不可直接计算；024清洗仅补充')]:
        change(cid,'scenario_type','B',reason)
        change(cid,'cross_document_reason','',reason)
        change(cid,'source_necessity_review',dict(status='FORMAL_FULLTEXT_CHECKED', minimal_doc_sets=[[doc]], reason=reason, supplemental_sources_not_required=True),reason)
    for c in result:
        c['case_version'] = VERSION
        required = list(dict.fromkeys(fid for p in c['answer_key_points'] if p['scoring_role']=='REQUIRED' for fid in p['fact_ids']))
        for fact in c['required_facts']:
            fact['evaluation_role'] = 'REQUIRED' if fact['fact_id'] in required else 'SUPPLEMENTARY'
        by_e = {e['evidence_id']:e for e in c['golden_evidence']}
        docs = list(dict.fromkeys(by_e[e]['doc_id'] for f in c['required_facts'] if f['fact_id'] in required for e in f['evidence_ids']))
        classes = {by_e[e]['source_class'] for f in c['required_facts'] if f['fact_id'] in required for e in f['evidence_ids']}
        c['knowledge_type'] = next(iter(classes)) if len(classes)==1 else 'MIXED'
        spec=c['checker_spec']
        for key in ['source_evidence_coverage','answer_correctness']:
            spec[key]['fact_ids']=required
        spec['top_k_necessary_evidence_recall'].update(required_fact_ids=required, necessary_doc_ids=docs, denominator='required_fact_ids', mapping_status='NOT_IMPLEMENTED', unmapped_status='EVIDENCE_MISSING')
        spec['cross_document_fact_coverage'].update(required=len(docs)>1, necessary_doc_ids=docs, fact_ids=required)
        spec['insufficient_evidence'].update(required=c['answerability']!='FULL', missing_information=c['missing_information'])
        spec['integration_status']='DATA_ACCEPTANCE_ONLY_RUNTIME_ADAPTER_NOT_IMPLEMENTED'
        c['acceptance_review']=dict(version=VERSION, status='PENDING', original_input_sha256=sha(ZIP.read_bytes()), necessary_fact_ids=required, necessary_doc_ids=docs, supplementary_fact_policy='来源目录保留补充事实；SUPPLEMENTARY不参与严格回答和Top-K必要事实分母，不因缺少补充来源判FAIL')
    return result, changes


def schema():
    s=golden.schema()
    p=s['properties']
    p['case_version']={'const':VERSION}
    p['golden_evidence']['minItems']=2
    p['required_facts']['items']['properties']['evaluation_role']={'enum':['REQUIRED','SUPPLEMENTARY']}
    p['required_facts']['items']['required'].append('evaluation_role')
    p['checker_spec']={'type':'object','required':['source_evidence_coverage','answer_correctness','top_k_necessary_evidence_recall','cross_document_fact_coverage']}
    p['answer_key_points']=golden.arr(golden.obj(dict(point_id={'type':'string','minLength':1}, criterion={'type':'string','minLength':1},fact_ids=golden.arr({'type':'string'},1),scoring_role={'enum':['REQUIRED','OPTIONAL']})),1)
    for key in ['editorial_review','quality_repair','source_necessity_review','acceptance_review']:
        p[key]={'type':'object'}
    p['answerability_note']={'type':'string'}
    s['required']+=['answer_key_points','acceptance_review']
    s.update(title='Procurement RAG Golden v1.2.1 independent accepted candidate', **{'$comment':'原文证据独立于索引；来源目录的SUPPLEMENTARY事实不参与必要召回。跨字段引用、哈希及角色一致性由accept_golden_v12验证。'})
    return s


def validate(cases,sources,write_report=True):
    checks=[]
    for c in cases:
        errors=golden.check_schema(c,schema())
        es={e['evidence_id']:e for e in c['golden_evidence']}; fs={f['fact_id']:f for f in c['required_facts']}
        if len(es)!=len(c['golden_evidence']) or len(fs)!=len(c['required_facts']): errors.append('duplicate_reference_id')
        records=[]
        for e in es.values():
            expected=golden.locate(sources[e['doc_id']],e['source_span']['line_start'],e['source_span']['line_end'])
            ok=all(e[k]==v for k,v in expected.items()) and sources[e['doc_id']]['text'][e['source_span']['char_start']:e['source_span']['char_end']]==e['evidence_text']
            if not ok:errors.append('evidence_mismatch:'+e['evidence_id'])
            records.append(dict(evidence_id=e['evidence_id'],doc_id=e['doc_id'],passed=ok))
        for f in fs.values():
            if len(f['evidence_ids'])!=1 or f['text']!=es[f['evidence_ids'][0]]['evidence_text']: errors.append('fact_text_mismatch:'+f['fact_id'])
            if any(es[e]['fact_id']!=f['fact_id'] for e in f['evidence_ids']): errors.append('fact_reverse_link_mismatch')
        needed=set()
        for p in c['answer_key_points']:
            if not set(p['fact_ids'])<=fs.keys():errors.append('point_unknown_fact:'+p['point_id'])
            if p['scoring_role']=='REQUIRED':needed.update(p['fact_ids'])
        if needed!={f['fact_id'] for f in fs.values() if f['evaluation_role']=='REQUIRED'}:errors.append('necessary_fact_roles_mismatch')
        for key in ['source_evidence_coverage','answer_correctness']:
            if set(c['checker_spec'][key]['fact_ids'])!=needed:errors.append('checker_fact_mismatch:'+key)
        for q in c['question_conditions']:
            if q['question_text'] not in c['question'] or not set(q['fact_ids'])<=fs.keys():errors.append('question_anchor_mismatch')
        docs={es[e]['doc_id'] for f in fs.values() if f['fact_id'] in needed for e in f['evidence_ids']}
        if docs!=set(c['acceptance_review']['necessary_doc_ids']):errors.append('necessary_doc_mismatch')
        if (c['scenario_type'] in ['C','D']) != (len(docs)>1):errors.append('scenario_source_count_mismatch')
        if c['real_time_dependency'] and c['fixture'] is None:errors.append('missing_fixture')
        checks.append(dict(case_id=c['case_id'],status='PASS' if not errors else 'FAIL',errors=errors,evidence=records,semantic_support_check='SEPARATE_DEEPEVAL_AND_SOURCE_INSPECTION'))
    result=dict(cases=len(cases),formal_documents=len(sources),official_class_counts=dict(Counter(d['class'] for d in sources.values())),passed=sum(x['status']=='PASS' for x in checks),evidence_records=sum(len(c['golden_evidence']) for c in cases),fact_records=sum(len(c['required_facts']) for c in cases),point_fact_edges=sum(len(p['fact_ids']) for c in cases for p in c['answer_key_points']),checks=checks)
    if write_report:save(REPORT/'deterministic_validation.json',result)
    assert result['passed']==46, [x for x in checks if x['status']!='PASS']
    return result


def final_repairs(cases,sources):
    """只修复源文与题面可直接裁决的漏项；不按Judge低分批量扩充答案。"""
    result=copy.deepcopy(cases); before={c['case_id']:copy.deepcopy(c) for c in result}
    for c in result:
        if c['case_id']=='RGV1-C03':
            c['answer_key_points'][0]['criterion']='总预算是支出上限，不等于商品成交单价，不能直接拿预算当单价；本题不强制解释未询问的目录参考价'
            c['scenario_type']='D'
            c['cross_document_reason']='同一成本估算任务中的两个独立子判断：KB-002解释预算与单价，PKB-V2-015解释后续生命周期成本；不称多源共同证明同一规则。'
        if c['case_id']=='RGV1-D07':
            e=golden.locate(sources['PKB-V2-023'],30,30)
            e.update(evidence_id='E4',fact_id='F4',role='fact')
            c['golden_evidence'].append(e)
            c['required_facts'].append(dict(fact_id='F4',text=e['evidence_text'],evidence_ids=['E4'],required_judgment='结算前对账及差异核实',source_class='B',authority='通用核对思路，非完整企业付款制度',evaluation_role='REQUIRED'))
            c['answer_key_points'][0]['criterion']='针对服务范围、响应时限或阶段成果等形成可核验的服务验收条件，不能只套实物到货数量标准；不要求穷举全部服务指标'
            c['answer_key_points'][1].update(criterion='申请、订单、服务确认、发票及付款是不同阶段，申请获批不等于已付款；结算前核对订单、实际交付与发票费用等并按财务流程核实差异',fact_ids=['F2','F4'])
            c['answer_key_points'][2]['criterion']='Agent不能自动建PO、审核发票或付款；具体付款要求按适用制度法规办理'
            c['golden_answer']='维护服务应按服务范围、响应时限、阶段成果、人员要求及计费方式形成可核验的验收标准，不能简单套用实物到货入库标准。采购申请、采购订单、服务确认、发票和付款是不同阶段，申请获批不表示已付款。结算前应核对供应商、订单、实际交付、发票金额及税费，差异按组织财务流程核实。Agent没有自动创建PO、审核发票或付款权限；具体付款要求须按适用制度及法规处理。'
    changes=[]
    for c in result:
        old=before[c['case_id']]
        for field in c:
            if c[field]!=old.get(field):changes.append(dict(case_id=c['case_id'],field=field,before=old.get(field),after=c[field],reason='C03移除题外目录价硬点并按两个子判断归D；D07补齐用户明确询问的可验证验收标准及结算核对（023 L30）'))
        if c['case_id']=='RGV1-D02':
            e=golden.locate(sources['PKB-V4-048'],44,44);e.update(evidence_id='E4',fact_id='F4',role='fact')
            c['golden_evidence'].append(e)
            c['required_facts'].append(dict(fact_id='F4',text=e['evidence_text'],evidence_ids=['E4'],required_judgment='仅实际生成PO才扣一次',source_class='B',authority='原文明确标出的当前系统规则',evaluation_role='REQUIRED'))
            c['answer_key_points'][0].update(criterion='预算仅在实际生成PO时扣减一次，保留草稿或提交审批不等于已扣预算',fact_ids=['F4'])
            c['answer_key_points'].append(dict(point_id='P4',criterion='PO创建失败不扣、幂等重放不重复扣，仅作本题可选的额外解释',fact_ids=['F1'],scoring_role='OPTIONAL'))
            c['scenario_type']='B';c['cross_document_reason']=''
            c['source_necessity_review']=dict(status='FORMAL_FULLTEXT_CHECKED',minimal_doc_sets=[['PKB-V4-048']],reason='048 L32/40/44已覆盖全部必答：替代、草稿及扣减时点；004仅提供本题不强制的幂等与失败精确补充',supplemental_sources_not_required=True)
            for field in ['golden_evidence','required_facts','answer_key_points','scenario_type','cross_document_reason','source_necessity_review']:
                changes.append(dict(case_id=c['case_id'],field=field,before=old.get(field),after=copy.deepcopy(c[field]),reason='v1.2已把幂等失败降为可选，因此048全文成为单篇充分来源，不能强制召回004'))
        required=list(dict.fromkeys(f for p in c['answer_key_points'] if p['scoring_role']=='REQUIRED' for f in p['fact_ids']))
        by_e={e['evidence_id']:e for e in c['golden_evidence']}
        for f in c['required_facts']:f['evaluation_role']='REQUIRED' if f['fact_id'] in required else 'SUPPLEMENTARY'
        docs=list(dict.fromkeys(by_e[e]['doc_id'] for f in c['required_facts'] if f['fact_id'] in required for e in f['evidence_ids']))
        classes={by_e[e]['source_class'] for f in c['required_facts'] if f['fact_id'] in required for e in f['evidence_ids']}
        c['knowledge_type']=next(iter(classes)) if len(classes)==1 else 'MIXED'
        c['acceptance_review']['necessary_fact_ids']=required
        c['acceptance_review']['necessary_doc_ids']=docs
        for key in ['source_evidence_coverage','answer_correctness','cross_document_fact_coverage']:c['checker_spec'][key]['fact_ids']=required
        c['checker_spec']['top_k_necessary_evidence_recall']['required_fact_ids']=required
        c['checker_spec']['top_k_necessary_evidence_recall']['necessary_doc_ids']=docs
        c['checker_spec']['cross_document_fact_coverage'].update(required=len(docs)>1,necessary_doc_ids=docs)
    save(REPORT/'additional_repairs.json',changes)
    write_cases(RUN/'candidate_after_repairs.jsonl',result)
    return result


def prepare():
    RUN.mkdir(parents=True,exist_ok=True); REPORT.mkdir(parents=True,exist_ok=True)
    baseline=RUN/'protected_baseline.json'; current=snapshot()
    if baseline.exists():assert json.loads(baseline.read_text(encoding='utf-8'))==current, '保护输入变化'
    else:save(baseline,current)
    original_cases,manifest=original(); sources=golden.load_sources()
    cases,changes=build_cases(original_cases,sources)
    write_cases(RUN/'candidate_before_judge.jsonl',cases)
    save(REPORT/'repair_changes.json',changes)
    save(REPORT/'input_manifest.json',dict(actual_zip=str(ZIP),zip_sha256=sha(ZIP.read_bytes()),requested_alternate_filename='采购Agent_RAG_Golden_46Case_定向修复_v1.2.zip',attachment_used=True,archive_manifest=manifest))
    validate(cases,sources)
    audit.frozen_config()
    # 新评分合同显式处理OPTIONAL，不改变旧六维质量Rubric。
    save(REPORT/'incremental_plan.json',dict(changed_in_package=manifest['changed_cases'],additionally_changed=sorted({c['case_id'] for c in changes}-set(manifest['changed_cases'])),semantic_dimensions=['D1','D2','D3','D5','D6'],binding_scope='all_46_cases',pilot=['RGV1-A01','RGV1-C04'],no_tested_rag=True))
    return cases,sources,manifest


async def semantic(cases,sources,manifest,pilot=False):
    ids=set(manifest['changed_cases'])|{x['case_id'] for x in json.loads((REPORT/'repair_changes.json').read_text(encoding='utf-8'))}
    if pilot:ids={'RGV1-A01','RGV1-C04'}
    sem=asyncio.Semaphore(3)
    async def one(c):
        if c['case_id'] not in ids:return
        async with sem:
            blind=await asyncio.to_thread(audit.blind_analysis,c,sources,'acceptance')
            scores={}
            for d in ['D1','D2','D3','D5','D6']:
                r=await asyncio.to_thread(audit.metric,c,sources,d,blind,'acceptance')
                scores[d]=r['score']
            print('SEMANTIC',c['case_id'],scores,flush=True)
    outcomes=await asyncio.gather(*(one(c) for c in cases),return_exceptions=True)
    save(REPORT/'usage.json',audit.usage_summary())
    errors=[repr(x) for x in outcomes if isinstance(x,Exception)]
    if errors:raise RuntimeError(errors)


def binding(c,sources,phase='binding'):
    GEval,_,Rubric,_,TestCase,Params=audit.framework()
    fs={f['fact_id']:f for f in c['required_facts']}
    pairs=[dict(point_id=p['point_id'],criterion=p['criterion'],scoring_role=p['scoring_role'],declared_fact_ids=p['fact_ids'],referenced_facts=[fs[f] for f in p['fact_ids']]) for p in c['answer_key_points']]
    steps=['逐一检查每个point的criterion是否由其referenced_facts实际支持，不准用其他未引用事实补救错绑；context正式全文只用于核对身份和范围。', 'OPTIONAL也须准确绑源，但不影响严格回答通过。criterion中的评审范围说明（如不要求穷举、仅供补充）不是需要原文证明的采购事实。', '全部准确给2；支持不明确给1；存在实质错绑、权限升级或禁止结论与证据冲突给0，reason须指明具体P/F与原文。']
    model=audit.make_adapter(phase,c['case_id'],'all_points')
    m=GEval(name='PointFactAcceptance',evaluation_steps=steps,evaluation_params=[Params.INPUT,Params.ACTUAL_OUTPUT,Params.CONTEXT],model=model,rubric=[Rubric(score_range=(0,0),expected_outcome='实质错绑'),Rubric(score_range=(1,1),expected_outcome='不明确'),Rubric(score_range=(2,2),expected_outcome='每条引用准确支持')],threshold=None,async_mode=False)
    output=dict(question=c['question'],forbidden_claims=c['forbidden_claims'])
    if phase!='binding': output.update(evaluand='point到referenced_facts的引用边；不是对用户作答，不按答案完整性打分',point_fact_pairs=pairs)
    tc=TestCase(input=dump(pairs),actual_output=dump(output),context=audit.source_context(c,sources))
    m.measure(tc,_show_indicator=False)
    result=dict(case_id=c['case_id'],score=round(m.score*2),reason=m.reason,pairs=pairs,framework='DeepEval.GEval',rubric_version='point-fact-acceptance-1.0')
    save(RUN/phase/f"{c['case_id']}.json",result)
    print('BINDING',c['case_id'],result['score'],flush=True)
    return result


async def bindings(cases,sources):
    sem=asyncio.Semaphore(3)
    async def one(c):
        async with sem:return await asyncio.to_thread(binding,c,sources)
    results=await asyncio.gather(*(one(c) for c in cases),return_exceptions=True)
    save(REPORT/'binding_results.json',[r if not isinstance(r,Exception) else dict(error=repr(r)) for r in results])
    save(REPORT/'usage.json',audit.usage_summary())
    if any(isinstance(r,Exception) for r in results):raise RuntimeError('绑定检查异常，保留原始输出后可单次故障续跑')


PROBES = {
 'RGV1-C03': [
  ('总预算是支出上限，不是设备单价。先用有据的单价乘数量估初始费用，再按品类纳入运行维护、耗材或退出等适用成本；未知费用标明，不能当确定报价。',True),
  ('不能把最多能花多少钱当作每件的成交价。使用周期成本应在有据的初始购置费上，加适用的能耗保养耗材和退役费用，未核实项保留为未知。',True),
  ('把全部预算当作每台单价乘台数就行，后续维护运行成本都不用算。',False)],
 'RGV1-D07': [
  ('维护服务应按范围、响应和阶段成果设可核验标准，不套实物入库。申请、订单、服务确认、发票和付款各不相同，获批不等于付款；结算前核对订单交付和发票税费，差异按财务流程处理。Agent不能自动建PO、审票或付款，具体按制度法规办。',True),
  ('用能核验的服务目标及阶段产出来验收，不只数到货。审批通过不是结算完成，要分清PR、订单、服务交付确认、发票及资金支付，并核对交付与票款差异走财务流程。Agent无权自动生成PO、审核发票和付钱，付款条件看有效制度法规。',True),
  ('维护服务只要按实物入库就算验收，申请批了等于付过钱，Agent可直接审票付款。',False)],
 'RGV1-D02': [
  ('可留草稿补信息，但不等于提交成功，预算不足提交会被阻断。可以合理降配或申请追加预算，不能违规拆单规避。提交审批不扣款，实际生成PO才扣减一次。',True),
  ('能先保留待完善的草稿，不能说已经提交；额度不够不能绕阻断。可考虑共享设备或补预算而非拆小单避审批；预算只在PO实际生成时扣一次。',True),
  ('拆成小单就能绕预算，草稿视为已提交，提交审批时就要先扣预算。',False)],
 'RGV1-U02': [
  ('当前资料不足以确认积分抵扣政策，不能凭未提及认定允许、禁止或制度不存在，应查正式采购政策再判断。',True),
  ('缺少可靠依据，现阶段无法下允许或不允许的结论，也不能说绝无这项规定；先找制度负责人核实有效政策。',True),
  ('资料没有说禁止就一定允许，个人积分可以直接抵采购款。',False)],
 'RGV1-A01': [
  ('按演示系统规则，一万元整走M02，还要提供可信报价材料。', True),
  ('应交M02审批，同时附可信的报价凭证。', True),
  ('刚好一万元仍走M01，不用报价材料。', False)],
 'RGV1-B15': [
  ('只有一家供货未必违规，要看组织制度和适用法律。先核查依赖原因及断供影响，短期可合理备货或约定长期供货。',True),
  ('不能直接认定违规，须核对本组织规则和适用法律。查清为何依赖它、停供有何后果；当前可适当增加安全库存并谈供货保障。',True),
  ('一家供货就一定违法，立刻换供应商，不必考虑认证和兼容。',False)],
 'RGV1-C04': [
  ('先看实际办公用途和必要性能、接口或扩容要求；再查工位外设、网络电源、空间迁移等部署约束。小机箱可能不利于扩展，不能只看便宜。',True),
  ('得先弄清电脑做什么、配置够不够及能否加配，还要核对桌面空间、接线供电和旧机迁移。小机箱省地方但升级余地可能不足。',True),
  ('买最便宜的小机箱就行，办公用途和工位条件都不用核对，小机箱扩展一定最好。',False)],
 'RGV1-D03': [
  ('先读申请状态版本并走后端校验，PO已生成就不能用Agent改；允许变更时保留原需求和理由，重新核对金额、审批权限和交期，再取得确认。',True),
  ('查最新状态及版本，不能绕状态版本检查，已经出PO的申请不能再由Agent修改。可改则留存变更前需求和原因，重算金额与审批交付影响并重新确认。',True),
  ('沿用旧确认直接覆盖型号，PO生成后也能改，不用重算金额审批或交期。',False)],
 'RGV1-D04': [
  ('不能直接验收合格。核对订单规格与替代约定、记录差异；接受替代需明确价格调整、保修和正式授权，功能相近不等于合同等效。',True),
  ('签收不表示换型号就被认可。先对照订货规格记下不同点，替代方案的价差、质保和授权要确认；用途差不多也不能自行认定履约等效。',True),
  ('已经签收而且功能差不多，直接验收通过即可，不必授权或核对价格保修。',False)],
 'RGV1-D08': [
  ('不能直接报确定节约率。先确认基准价真实可采购，规格数量及费用口径可比；缺少可比基线就不能给确定结果。',True),
  ('现在不能拿差价当确定节约。旧新采购须在可实际采购的价格、配置数量及税运费等口径上可比较，没有这样的基准不能报确定比例。',True),
  ('规格不同、退款重复都无所谓，直接用旧价减新价除旧价就是确定节约率。',False)],
 'RGV1-A02': [
  ('按演示规则，9999.99元走M01，10000元走M02。',True),
  ('前一笔不足万元由M01审批，后一笔刚到万元应由M02审批。',True),
  ('两笔都没超过一万元，因此都由M01审批。',False)],
 'RGV1-B08': [
  ('论坛只能作线索，应核实正式报价及来源时效，不能直接当报价；资料少只说明证据不足，不能断言信用差。',True),
  ('帖子不是现行报价依据，要核对可靠出处、适用地区和时间。没搜到资料不代表这家公司信用不好。',True),
  ('论坛推荐就是正式报价，资料很少说明供应商信用差。',False)]
}


async def probes(cases,sources):
    GEval,_,Rubric,_,TestCase,Params=audit.framework()
    steps=[
        '这是既定评分合同的答案探针，不是采购Agent评估。只检验问题实际所需的语义，不要求复述Golden。',
        '仅REQUIRED点参与通过；OPTIONAL缺失不得扣分。接受同义及逻辑蕴含，示例列表不要求穷举；条件性要求仅在答案主张触发条件时核对。',
        '关键错误或违反有据禁止结论给0；无错误但遗漏REQUIRED必要语义给1；全部必要判断成立给2。理由明确指出P及原文。不准以预期标签评分。'
    ]
    save(REPORT/'probe_rubric.json',dict(version='semantic-required-optional-probe-1.0',steps=steps,score_scale=[0,1,2],expected_labels_hidden=True))
    sem=asyncio.Semaphore(3)
    async def one(c,index,text,valid):
        async with sem:
            def measure():
                model=audit.make_adapter('answer_probe',c['case_id'],f'V{index}')
                tc=TestCase(input=c['question'],actual_output=text,expected_output=dump(dict(points=c['answer_key_points'],forbidden_claims=c['forbidden_claims'])),context=audit.source_context(c,sources))
                m=GEval(name='RequiredOptionalRuleProbe',evaluation_steps=steps,evaluation_params=[Params.INPUT,Params.ACTUAL_OUTPUT,Params.EXPECTED_OUTPUT,Params.CONTEXT],model=model,rubric=[Rubric(score_range=(0,0),expected_outcome='关键错误'),Rubric(score_range=(1,1),expected_outcome='遗漏必要语义'),Rubric(score_range=(2,2),expected_outcome='必要语义完整，接受同义简答')],threshold=None,async_mode=False)
                m.measure(tc,_show_indicator=False)
                score=round(m.score*2)
                r=dict(case_id=c['case_id'],variant=f'V{index}',answer=text,expected_valid=valid,score=score,reason=m.reason,framework='DeepEval.GEval',false_rejection=valid and score!=2,false_acceptance=not valid and score==2,matched_expectation=(score==2 if valid else score==0))
                save(RUN/'probes'/f"{c['case_id']}__V{index}.json",r)
                print('PROBE',c['case_id'],index,score,flush=True)
                return r
            return await asyncio.to_thread(measure)
    tasks=[one(c,i,text,valid) for c in cases for i,(text,valid) in enumerate(PROBES.get(c['case_id'],[]),1)]
    results=await asyncio.gather(*tasks,return_exceptions=True)
    save(REPORT/'probe_results.json',[r if not isinstance(r,Exception) else dict(error=repr(r)) for r in results])
    save(REPORT/'usage.json',audit.usage_summary())
    if any(isinstance(r,Exception) for r in results):raise RuntimeError('探针异常，保留原始记录')


async def review(cases,sources):
    selected=[]
    for c in cases:
        for d in ['D1','D2','D3','D5','D6']:
            p=RUN/'metrics'/f"acceptance__{c['case_id']}__{d}.json"
            if p.exists() and json.loads(p.read_text(encoding='utf-8'))['score']<2:selected.append((c,d))
    save(REPORT/'selective_review_plan.json',dict(method='same_model_different_presentation_no_blind_memo_or_prior_scores_explicit_metadata',selected=[dict(case_id=c['case_id'],dimension=d) for c,d in selected]))
    previous=audit.candidate_payload
    def payload(c):
        p=previous(c)
        p.update(actual_scenario_type=c['scenario_type'],immutable_id_note='case_id沿用历史ID，A/B/C/D前缀不是当前scenario_type；判断必须读取当前scenario_type字段',source_necessity_contract=c['acceptance_review'],scoring_role_contract='只有REQUIRED点为必答，OPTIONAL是补充；B类可作方法问题的必要知识，REQUIRED并不表示公司强制制度',question_scope_note='根据实际问句判断是否请求具体数值/真实操作，不能把审题者想补充的业务状态误当用户已要求的事实')
        return p
    audit.candidate_payload=payload
    sem=asyncio.Semaphore(3)
    async def one(c,d):
        async with sem:
            r=await asyncio.to_thread(audit.metric,c,sources,d,None,'review')
            print('REVIEW',c['case_id'],d,r['score'],flush=True)
            return r
    results=await asyncio.gather(*(one(c,d) for c,d in selected),return_exceptions=True)
    for c in cases:
        p=RUN/'binding'/f"{c['case_id']}.json"
        if p.exists() and json.loads(p.read_text(encoding='utf-8'))['score']<2:
            results.append(await asyncio.to_thread(binding,c,sources,'binding_review'))
    save(REPORT/'selective_review_results.json',[r if not isinstance(r,Exception) else dict(error=repr(r)) for r in results])
    save(REPORT/'usage.json',audit.usage_summary())
    if any(isinstance(r,Exception) for r in results):raise RuntimeError('复核存在异常')


async def repair_check(cases,sources):
    cases=final_repairs(cases,sources)
    validate(cases,sources)
    for c in cases:
        if c['case_id'] not in ['RGV1-C03','RGV1-D07','RGV1-D02']:continue
        blind=await asyncio.to_thread(audit.blind_analysis,c,sources,'repair')
        for d in ['D1','D2','D5','D6']:
            r=await asyncio.to_thread(audit.metric,c,sources,d,blind,'repair')
            print('REPAIR',c['case_id'],d,r['score'],flush=True)
        await asyncio.to_thread(binding,c,sources,'binding_repair')
    await probes(cases,sources)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['prepare','pilot','semantic','bindings','probes','review','repair-check']);args=parser.parse_args()
    cases,sources,manifest=prepare()
    if args.phase in ['pilot','semantic']:asyncio.run(semantic(cases,sources,manifest,args.phase=='pilot'))
    if args.phase=='bindings':asyncio.run(bindings(cases,sources))
    if args.phase=='probes':asyncio.run(probes(cases,sources))
    if args.phase=='review':asyncio.run(review(cases,sources))
    if args.phase=='repair-check':asyncio.run(repair_check(cases,sources))
    print(args.phase,'COMPLETE',flush=True)


if __name__=='__main__':main()
