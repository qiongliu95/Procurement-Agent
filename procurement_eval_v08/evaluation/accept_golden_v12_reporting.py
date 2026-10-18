"""保存v1.2最终候选及可复查的验收结论；不启动任何模型或业务调用。"""
from collections import Counter
import copy
import csv
import io
import json
from pathlib import Path
import zipfile

from . import accept_golden_v12 as job


RESOLUTIONS={
 'RGV1-B09':'问题询问谈判准备和留证方法，不索取具体底线数值；先行盲审PARTIAL是审题意见，不能当作待审Case标签。当前FULL有据；定向复核D2/D6均2。',
 'RGV1-B12':'问题未给金额且未问审批级别，不能额外强制万元阈值；核实紧急授权、替代与留痕服务于同一下单前任务。复核D2/D6均2。',
 'RGV1-B13':'用户明确问处理和留证，隔离记录、通知、合同处置均相关；评分合同接受等效证据而非穷举。复核D6=2。',
 'RGV1-B15':'问单一来源是否本身构成违规，不是要求对某笔真实采购作法律裁决。057 L40直接支持“不当然违规”，实际合法性仍取决于制度及法律；保留FULL。两份正确短期缓释答案通过，不强制尚未实施的切换细节。',
 'RGV1-C01':'保留D：031 L32给设备侧组件、维护及停机的长期成本方法；032 L24给完整型号和区域版本的耗材兼容判断。032 L32的耗材报价口径与成本局部重叠，不等于已覆盖设备侧维护停机。Judge对单篇充分性仍有分歧，登记为非阻断场景不确定性，不宣称已实证跨文档区分度。',
 'RGV1-C02':'问承诺能否保证及合同需确认什么，不索取实际维修费用或时间；两份原文足以回答承诺边界，因此FULL成立。定向复核D2/D6均2。',
 'RGV1-C03':'目录价为未问的额外硬点，已移除。预算/单价口径与适用品类TCO是同一成本估算任务的两个子判断，归D。问题的谓语是“应该怎样区分”，没有要求计算具体金额；不因缺数量/报价改PARTIAL。修后两个正确方法简答均2、错误预算乘数量答案0。保留修后D2/D6=1的审计意见，不伪造满分。',
 'RGV1-C04':'029 L24/28/32单篇涵盖用途、配置与部署，归B。主审误读为C；复核又把同文档多个判断误称D。A/B/C/D按文档数及判断数划分，不由历史Case ID前缀推断。026只作可选补充；修后两份正确简答2、低价小机箱直接定型0。',
 'RGV1-D02':'048 L32/40/44足以回答替代、草稿和扣减时点；004的幂等/失败细节在v1.2已是补充，故进一步改B并重绑F4。修后D1/D2/D5均2，D6仍认为不得拆单与替代合并偏严，保留意见；问题明确问“不绕规则”的办法，因此该边界相关。两份正确答案2、拆单绕预算0。',
 'RGV1-D03':'052 L28/32/40/44覆盖变更时点、重核金额审批交期、PO限制、版本及确认，归B；006为保留的权威性补充。Judge文字确认单篇充分，却仍对已修改的跨文档声明扣分，不能据此恢复双源。两个正确流程简答均2、强行覆盖0。',
 'RGV1-D04':'055 L28/32/40分别支撑核对规格、替代价格保修授权、用途相近不等效，归B。022收货定义为补充；A/B粒度仍属估计，不影响充分来源与验收边界。两份正确简答2、签收即合格0。',
 'RGV1-D05':'保留D：050 L40回答异常低价不等于欺诈；018 L30/32回答履约证据及一次延期样本不足。任一单篇都不能完整支持两个独立子判断。Judge反复建议D而实际字段已是D，不能将该理由当成已证实误标。',
 'RGV1-D06':'保留D：046 L32是交付计划与成本，062 L32是实际数量差异处理；两篇回答不同而相关的子问，符合D。问题首句是背景陈述，实际问“怎样比较”，不是索取具体分批数量；不改PARTIAL。复核D2/D6均2。',
 'RGV1-D07':'064 L32支撑可核验服务标准；023 L24/30/36支撑阶段、对账和权限边界。已补入原文L30及评分点，保留D与方法层面的FULL。Judge把knowledge_type=B误作scenario_type=B，且把通用流程咨询误读为真实业务操作。不得宣称已核验实时权限或实际付款。修后正确简答2、审批即付款/Agent直接付0。',
 'RGV1-D08':'问题只问能否直接计算，065 L28/40的不可比基线已经足以否定直接给确定率；024清洗为合理补充，未删来源但不进入严格分母。归B、FULL。两份未穷举清洗项目的正确简答2、直接差价率0；不能据Judge要求所有背景逐一展开而强制双源。',
 'RGV1-D09':'保留D：020 L26/36回答数据退出及法律边界，021 L24/30回答SLA计时及响应恢复。两个相关条款任务正是D；REQUIRED是本题必要知识，不表示把B类建议升级为公司制度。复核D2/D6均2，仍保留D5分歧。',
 'RGV1-U02':'保留A+UNANSWERABLE：009 L35/36支撑缺证处理，不假装它包含积分政策。UNANSWERABLE是answerability轴，不需要发明第五种场景类别。P1/P2语义重叠可能影响未来加权评分，当前严格通过采用必要语义覆盖，不重复加权。两份正确拒答均2、未禁止即允许0；仍保留D6=1与评分粒度优化建议。'
}


def csv_export(path,cases):
    fields=list(dict.fromkeys(k for c in cases for k in c))
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for c in cases:w.writerow({k:v if isinstance(v,str) else json.dumps(v,ensure_ascii=False) for k,v in c.items()})
    rows=list(csv.DictReader(io.StringIO(path.read_text(encoding='utf-8-sig'))))
    assert len(rows)==len(cases)
    for row,c in zip(rows,cases):
        restored={k:row[k] if isinstance(v,str) else json.loads(row[k]) for k,v in c.items()}
        assert restored==c


def metrics_for(cid):
    values={}
    for phase in ['acceptance','review','repair']:
        for d in ['D1','D2','D3','D5','D6']:
            p=job.RUN/'metrics'/f'{phase}__{cid}__{d}.json'
            if p.exists():values[d]=dict(json.loads(p.read_text(encoding='utf-8')),record=str(p))
    return values


def generate():
    cases,sources,manifest=job.prepare()
    cases=job.final_repairs(cases,sources)
    originals,_=job.original()
    old_by_id={c['case_id']:c for c in originals}
    old_scores={c['case_id']:c for c in job.golden.read_jsonl(job.PROJECT/'evaluation/reports/rag_golden_v1_1_quality_audit/case_quality_scores.jsonl')}
    with zipfile.ZipFile(job.ROOT/'采购Agent_RAG_Golden_46Case_语义优化_v1.1.zip') as archive:
        old_raw=archive.read('evaluation_cases.jsonl')
        assert job.sha(old_raw)==manifest['source_v11_sha256']
        v11={c['case_id']:c for c in [json.loads(x) for x in old_raw.decode('utf-8-sig').splitlines()]}
    with zipfile.ZipFile(job.ZIP) as archive:
        revisions=list(csv.DictReader(io.StringIO(archive.read('rag_golden_v1_2/revision_diff.csv').decode('utf-8-sig'))))
        input_csv=list(csv.DictReader(io.StringIO(archive.read('rag_golden_v1_2/evaluation_cases.csv').decode('utf-8-sig'))))
    def restore(value,expected):
        if isinstance(expected,str):return value
        if expected is None and value in ['', 'None', 'null']:return None
        if isinstance(expected,bool):return value.lower()=='true'
        return json.loads(value)
    revision_checks=[];replay=copy.deepcopy(v11)
    for r in revisions:
        # 这是依次编辑日志，同一字段可出现多次；不能把中间new误当最终快照。
        old_value=replay[r['case_id']].get(r['field'])
        old_logged=json.loads(r['old']);new_logged=json.loads(r['new'])
        old_ok=old_logged==old_value
        replay[r['case_id']][r['field']]=new_logged
        revision_checks.append(dict(case_id=r['case_id'],field=r['field'],old_matches=old_ok,reason=r['reason']))
    assert all(x['old_matches'] for x in revision_checks)
    for c in replay.values():
        for p in c['answer_key_points']:p.setdefault('scoring_role','REQUIRED')
    for r in revisions:
        assert replay[r['case_id']][r['field']]==old_by_id[r['case_id']][r['field']],(r['case_id'],r['field'])
    assert {r['case_id'] for r in revisions}==set(manifest['changed_cases'])
    assert len(input_csv)==46
    missing_columns=[]
    for c,row in zip(originals,input_csv):
        missing=set(c)-set(row)
        assert missing<={'source_necessity_review','answerability_note'},missing
        if missing:missing_columns.append(dict(case_id=c['case_id'],fields=sorted(missing)))
        assert {k:restore(row[k],v) for k,v in c.items() if k in row}=={k:v for k,v in c.items() if k in row}
    job.save(job.REPORT/'revision_and_input_csv_validation.json',dict(revision_rows=len(revisions),changed_cases=len(manifest['changed_cases']),source_v11_hash_matches=True,input_csv_present_fields_match=True,input_csv_roundtrip=not missing_columns,input_csv_missing_columns=missing_columns,fix='最终CSV采用所有Case字段并集，缺失列完整导出并读回核验；原CSV不修改',checks=revision_checks))
    results=[];metrics=[];disagreements=[]
    for c in cases:
        cid=c['case_id'];current=metrics_for(cid)
        if not current:
            # 仅内容确实未变的17条复用前次审计；新版评分合同及全部引用本轮仍重新校验。
            for field in ['question','golden_answer','required_facts','golden_evidence']:
                assert old_by_id[cid][field]==v11[cid][field], (cid,field)
            current={x['dimension']:dict(x,record=str(job.PROJECT/'evaluation/reports/rag_golden_v1_1_quality_audit/case_quality_scores.jsonl')) for x in old_scores[cid]['dimension_scores'] if x['dimension']!='D4'}
            scope='PRIOR_SEMANTIC_AUDIT_REUSED_CONTENT_UNCHANGED_NEW_BINDINGS_CHECKED'
        else:scope='CURRENT_INCREMENTAL_SEMANTIC_REVIEW'
        assert set(current)=={'D1','D2','D3','D5','D6'},cid
        assert current['D1']['score']==2,(cid,'unsupported_golden')
        binding=None
        for phase in ['binding','binding_review','binding_repair']:
            path=job.RUN/phase/f'{cid}.json'
            if path.exists():binding=dict(json.loads(path.read_text(encoding='utf-8')),record=str(path))
        assert binding and binding['score']==2,(cid,'binding_not_passed')
        warnings=[dict(dimension=d,score=x['score'],reason=x['reason'],record=x['record']) for d,x in current.items() if x['score']<2]
        assert not any(x['score']==0 for x in warnings), (cid,'unresolved_zero')
        status='IMPROVEMENT_RECOMMENDED' if warnings else 'PASS'
        reason=RESOLUTIONS.get(cid,'本轮来源与引用检查通过；语义字段未改变时仅复用已保存的前次原文审计，不宣称本轮重新六维审计。')
        if not current.get('D2',{}).get('score',2) or not current.get('D6',{}).get('score',2):raise ValueError(cid)
        result=dict(case_id=cid,acceptance='ACCEPTED_CANDIDATE',quality_status=status,semantic_scope=scope,latest_framework_scores={d:x['score'] for d,x in current.items()},warnings=warnings,source_based_disposition=reason,binding_check=binding['record'],necessary_docs=c['acceptance_review']['necessary_doc_ids'],scenario_type=c['scenario_type'],human_reviewed=False,unresolved_business_decision=False)
        results.append(result)
        for d,x in current.items():metrics.append(dict(case_id=cid,dimension=d,score=x['score'],reason=x['reason'],record=x['record'],scope=scope,not_manually_rescored=True))
        if warnings:disagreements.append(dict(case_id=cid,status=status,framework_warnings=warnings,disposition=reason,not_proof_of_universal_answer_acceptance=True))
        c.setdefault('source_necessity_review',{})
        c.setdefault('answerability_note','')
        c['gold_status']='AUTO_VALIDATED'
        c['acceptance_review'].update(status=status,accepted_candidate=True,semantic_scope=scope,nonblocking_judge_warnings=[x['dimension'] for x in warnings],limitations='自动来源定位与有限语义复核，无人工逐条核验；不是RAG效果评估或全表达空间保证')
        c['provenance']['semantic_review']='See evaluation/reports/rag_golden_v1_2_acceptance; DeepEval incremental semantic audit, all-point binding verification, source-span replay; no human verification claimed'
        c['quality_repair'].update(validation_status='FORMAL_SOURCE_AND_BINDING_VALIDATED_INCREMENTAL_SEMANTIC_COMPLETE',strict_audit_approval='ACCEPTED_CANDIDATE_WITH_RECORDED_JUDGE_WARNINGS' if warnings else 'ACCEPTED_CANDIDATE',source_unchanged=c['golden_evidence']==old_by_id[cid]['golden_evidence'],raw_required_facts_unchanged=[{k:v for k,v in f.items() if k!='evaluation_role'} for f in c['required_facts']]==old_by_id[cid]['required_facts'])
        c['editorial_review']['status']='INPUT_EDITORIAL_HISTORY_SUPERSEDED_BY_ACCEPTANCE_REVIEW'
    job.DATA.mkdir(parents=True,exist_ok=True)
    job.write_cases(job.DATA/'evaluation_cases.jsonl',cases)
    csv_export(job.DATA/'evaluation_cases.csv',cases)
    job.save(job.DATA/'evaluation_schema.json',job.schema())
    validation=job.validate(cases,sources)
    # 全部输入证据单独回放，避免新增证据通过掩盖输入中的错误。
    original_evidence=[]
    for c in originals:
        for e in c['golden_evidence']:
            expected=job.golden.locate(sources[e['doc_id']],e['source_span']['line_start'],e['source_span']['line_end'])
            assert all(e[k]==v for k,v in expected.items())
            original_evidence.append(dict(case_id=c['case_id'],evidence_id=e['evidence_id'],passed=True))
    job.save(job.REPORT/'input_evidence_validation.json',dict(records=len(original_evidence),checks=original_evidence))
    job.write_cases(job.REPORT/'case_acceptance_results.jsonl',results)
    job.write_cases(job.REPORT/'semantic_scores.jsonl',metrics)
    job.save(job.REPORT/'judge_disagreements.json',disagreements)
    job.write_cases(job.DATA/'skipped_cases.jsonl',[])
    job.save(job.REPORT/'excluded_cases.json',dict(count=0,cases=[],reason='未发现修复后仍无法成立的明确事实错误或无效问题；保留非阻断Judge分歧而非伪装满分'))
    probes=json.loads((job.REPORT/'probe_results.json').read_text(encoding='utf-8'))
    assert len(probes)==36 and all(p['matched_expectation'] for p in probes)
    baseline=json.loads((job.RUN/'protected_baseline.json').read_text(encoding='utf-8'))
    protected=job.snapshot();assert protected==baseline,'正式知识/旧版/业务源码/历史证据发生变化'
    raw=[]
    for p in sorted((job.RUN/'calls').glob('*.json')):
        data=json.loads(p.read_text(encoding='utf-8'));raw.append(dict(path=str(p),sha256=job.sha(p.read_bytes()),phase=data['phase'],case_id=data['case_id'],dimension=data['dimension'],has_exception='exception' in data))
    job.save(job.RUN/'raw_output_manifest.json',raw)
    job.save(job.REPORT/'protected_inputs_validation.json',dict(protected_files=len(baseline),unchanged=True))
    usage=job.audit.usage_summary();job.save(job.REPORT/'usage.json',usage)
    config=dict(framework='DeepEval.GEval',deepeval_version=job.metadata.version('deepeval'),model=job.audit.config()['AGENT_MODEL_NAME'],base_url=job.audit.config()['AGENT_MODEL_BASE_URL'],adapter='existing DeepEvalBaseLLM schema-aware OpenAI-compatible adapter, isolated module instance',rubric_version=job.audit.RUBRIC_VERSION,temperature=0,thinking='disabled',telemetry=False,custom_model_logprob_weighting=False,initial_cases=2,incremental_cases=29,prior_content_unchanged_reused=17,all_case_binding_checks=46,selective_rechecks_only=True,answer_probes=36,usage=usage,source_of_truth='52 released Markdown + manifest hashes',no_tested_rag=True,notes='框架分数原样保存，人工式来源裁决是自动审计说明而非框架改分；不是人工核验')
    job.save(job.REPORT/'judge_config.json',config)
    counts=Counter(r['quality_status'] for r in results)
    necessary_docs={d for c in cases for d in c['acceptance_review']['necessary_doc_ids']}
    all_docs={e['doc_id'] for c in cases for e in c['golden_evidence']}
    scenarios=dict(Counter(c['scenario_type'] for c in cases));answerability=dict(Counter(c['answerability'] for c in cases));classes=dict(Counter(c['knowledge_type'] for c in cases))
    stats=dict(cases=46,valid=46,quality_status_counts=dict(counts),skipped=0,needs_decision=0,scenario_counts=scenarios,knowledge_type_counts=classes,answerability_counts=answerability,necessary_source_documents=len(necessary_docs),all_referenced_documents=len(all_docs),formal_documents=52,uncovered_necessary_documents=sorted(set(sources)-necessary_docs),source_class_necessary_documents=dict(Counter(sources[d]['class'] for d in necessary_docs)),source_records=validation['evidence_records'],facts=validation['fact_records'],point_fact_edges=validation['point_fact_edges'],probe_results=dict(correct=24,correct_accepted=24,wrong=12,wrong_rejected=12),latest_framework_distributions={d:dict(Counter(r['latest_framework_scores'][d] for r in results)) for d in ['D1','D2','D3','D5','D6']},initial_and_review_scores_not_deleted=True)
    job.save(job.REPORT/'acceptance_summary.json',stats)
    delivery_manifest=dict(version=job.VERSION,input_zip=str(job.ZIP),input_zip_sha256=job.sha(job.ZIP.read_bytes()),cases=46,accepted=46,quality_status_counts=dict(counts),candidate_only=True,files={p.name:job.sha(p.read_bytes()) for p in job.DATA.iterdir() if p.is_file() and p.name!='manifest.json'},report=str(job.REPORT/'final_quality_acceptance_report.md'),source_manifest_sha256=job.sha((job.golden.KB/'manifest.json').read_bytes()),untouched_protected_files=len(baseline))
    job.save(job.DATA/'manifest.json',delivery_manifest)
    changed=json.loads((job.REPORT/'repair_changes.json').read_text(encoding='utf-8'))+json.loads((job.REPORT/'additional_repairs.json').read_text(encoding='utf-8'))
    local_ids=sorted({x['case_id'] for x in changed})
    job.save(job.REPORT/'final_delivery_validation.json',dict(jsonl_schema_and_source_pass=46,csv_roundtrip=True,point_binding_semantic_pass=46,probe_expectations_met=36,protected_inputs_unchanged=True,dataset_manifest_hashes_verified=True,output_version=job.VERSION,local_content_repaired_cases=local_ids,not_rag_success_rate=True))
    issue_lines=['# 明确问题修复与审计分歧','', '|Case|修复或核验|','|---|---|',
     '|A01|输入包已把P3降为OPTIONAL；本轮正式原文复核及最小正确回答探针通过。|',
     '|B15|输入包已修复P1→F3、P2→F1、P3→F2；本轮引用语义核验通过，进一步限定切换细节仅在实际建议切换时要求。|',
     '|C04|输入绑定修复属实；必要判断改绑029全文，C→B，026通用顺序只作可选补充，并修正问题条件映射。|',
     '|D03、D04、D08|全文单篇充分，D→B；重绑最小必要来源，保留额外原文作补充，不强制召回。|',
     '|D02|048全文足以回答v1.2必答点，D→B；004幂等和失败细节只作可选。|',
     '|A02、B08|未问的报价凭证及调研报告格式由硬点改为OPTIONAL。|',
     '|B01、C03|删除评分点中题外金额公式/目录价的硬性要求；C03按两个相关子判断归D。|',
     '|C02、D08|问题问方法或保证边界，未要求实际数值，PARTIAL→FULL并同步Checker。|',
     '|D07|补齐用户问到的可核验服务验收及结算对账，新增023 L30精确证据，重查评分与答案。|','',
     '|CSV导出|输入CSV遗漏部分Case的source_necessity_review和answerability_note列；最终按全体字段并集导出并完成逐字段读回。|','',
     '本地内容修复涉及：'+', '.join(local_ids)+'。原Case ID及问题文本均保留，没有重新生成整套测试集。','',
     '以下为非阻断Judge警示，原分数不删除；不把两次意见一致当作绝对正确。']
    for r in disagreements:issue_lines+=['',f"## {r['case_id']}",r['disposition'],'保留警示：'+', '.join(f"{x['dimension']}={x['score']}" for x in r['framework_warnings'])]
    (job.REPORT/'quality_issues.md').write_text('\n'.join(issue_lines)+'\n',encoding='utf-8')
    (job.REPORT/'needs_decision.md').write_text('# 业务待决事项\n\n0项。剩余分歧属于Judge审题、场景粒度或评分表达风险，未发现正式业务规则冲突；不要求用户逐条审核。\n',encoding='utf-8')
    table='\n'.join(f"|{r['case_id']}|{r['scenario_type']}|{r['quality_status']}|{','.join(r['necessary_docs'])}|" for r in results)
    distribution='\n'.join(f"|{d}|{sum(r['latest_framework_scores'][d]==0 for r in results)}|{sum(r['latest_framework_scores'][d]==1 for r in results)}|{sum(r['latest_framework_scores'][d]==2 for r in results)}|" for d in ['D1','D2','D3','D5','D6'])
    report=f'''# 采购RAG Golden v1.2 最终质量验收

验收版本：`{job.VERSION}`。结论：46条均可进入独立正式候选集，明确未通过0条、业务待决0条。其中{counts['PASS']}条无当前剩余低分提示，{counts['IMPROVEMENT_RECOMMENDED']}条保留非阻断审计提示。不是46条所有维度满分，不是人工逐条核验，不是RAG准确率或检索成功率。

## 输入与范围

实际读取附件`RAG Golden v1.2 完整包.zip`（SHA-256 `{job.sha(job.ZIP.read_bytes())}`）；正文中的另一个文件名未找到，依据明确附件使用此新版，未用旧版替代。读取46条JSONL/CSV、manifest、修改记录、评分合同与审计说明；附件内操作提示只作为待审资料。原始包解压保存在本次runs/input。

正式知识库52篇（A11/B41）全部通过放行清单、文件集合和原始字节SHA-256检查。输入原有160条证据全部回放通过；最终{validation['evidence_records']}条证据、{validation['fact_records']}条事实记录、{validation['point_fact_edges']}条评分点到Fact引用边通过来源和结构检查。全部46条另做引用边语义G-Eval，原异常仅在改变呈现方式后定向复核，无错绑残留。

## 修复与复核

- A01的OPTIONAL修复、B15三处及C04两处旧错绑已经正式源核验；简答不因题外内容被拒。
- C04/D03/D04/D08改B并同步必要事实、文档、Checker及来源必要性说明。D02也发现单篇048足够，改B。其他原文继续保留为补充，SUPPLEMENTARY不进入严格召回分母。
- A02/B08题外硬点改可选；B01/C03评分限定到实际问题；C03改D；C02/D08按方法问题改FULL；D07补入023 L30及服务验收可核验性。逐字段记录见repair_changes.json、additional_repairs.json。
- 修改日志45条已按编辑顺序回放并核对27条最终变更；输入CSV遗漏部分Case额外字段，最终CSV已按字段并集补齐并读回。原CSV保留，JSONL作为权威输入。
- 本地内容修复{len(local_ids)}条，未重生成任何问题或整个数据集；所有ID保持不变。

先用A01/C04试跑，再对包内27条实际变更及额外修复的A02/B08共29条作原文先行审题及D1/D2/D3/D5/D6增量G-Eval；17条语义内容未变，只复用已保存v1.1原文审计，明确不是本轮重评。仅低分或误读项定向复核一次。C03/D02/D07修后再查受影响维度及引用。

36个答案探针覆盖12条代表题，包含24份正确最小/同义回答与12份典型错误答案：正确24/24被接受，错误12/12被拒绝。这只是评分合同小样本检验，不证明所有合理表达都能通过，也不代表采购Agent效果。

## 框架分数与分歧

下表保留每个维度最新实际框架输出：修后优先，其次定向复核，其次本轮主审；17条内容未变者明确引用旧审计。各阶段原始低分均保留。D4本轮未全量重评，不编造新难度成绩。

|维度|0分|1分|2分|
|---|---:|---:|---:|
{distribution}

D1无已确认事实冲突。部分D2错误地把方法询问判为要求实时结果；D5多次混淆knowledge_type=B与scenario_type=B，或建议当前已经采用的标签，甚至认为并列子问应该归C；D6会把题外全文细节当成必答。没有为提高分数修改既有六维Rubric。原分数、理由、引用及来源裁决分别保存在semantic_scores.jsonl、judge_disagreements.json和原始runs记录，来源裁决不是框架分数。

保留不确定性：C01两篇有局部成本信息重叠，设备侧维护停机与耗材型号/区域兼容的组合仍有覆盖价值，当前保留D；D04单文档判断粒度可讨论；U02评分点语义有重叠，未来不要重复加权。未发现足够原文依据认定这些题失效，因此不因Judge分歧盲目重写或剔除。其他非阻断提示详见quality_issues.md。

## 场景、知识与可回答性

当前场景分布：`{scenarios}`。C类现为0，不能宣称已覆盖“多个独立来源共同证明同一判断”的能力。跨文档为{sum(c['scenario_type'] in ['C','D'] for c in cases)}条，其余为单文档；跨文档按事实语义核对，未用doc_id数或字符串删源充当充分证明。

知识类别分布：`{classes}`。可回答性：`{answerability}`。必要来源涉及{len(necessary_docs)}/52篇，包含全部补充引用则涉及{len(all_docs)}/52篇；这不是对应文档全部内容被测试，更不是52篇总览等于52篇测试覆盖。未覆盖的必要来源见acceptance_summary.json。

本集从知识库反向设计，可用于知识覆盖及RAG能力测试，不能自动代表真实采购用户问题分布；自然性、难度及区分度尚未通过真实用户或RAG对照实验验证。未新增噪声题、真实检索干扰集或实时Fixture。未运行Chunk/Embedding/Top-K/生成模型比较。

## 最终交付与使用限制

最终JSONL、CSV、Schema及manifest：`{job.DATA}`。UTF-8 JSONL及UTF-8 BOM CSV读回一致，Schema与跨字段校验46/46通过。所有受保护的{len(baseline)}个文件哈希保持不变：正式知识库、旧Case、旧ZIP、历史审计证据及现有业务源码均未改动。

Golden源位置独立于索引。Top-K原文到实际Chunk映射及运行器适配仍未实现，明确标NOT_IMPLEMENTED/EVIDENCE_MISSING，不能只命中文档就算全部事实召回。当前交付是可用的数据候选，不宣称能被旧五字段CaseLoader直接运行。Agent工具路由及禁止写入仍是后续执行合同，本轮未实跑。

工具为DeepEval {config['deepeval_version']} G-Eval，Judge `{config['model']}`，使用现有DeepEvalBaseLLM适配器显式接入配置服务，未升级依赖。温度0、关闭思考和遥测，未使用logprob加权。Token与费用见judge_config.json：已记录{usage['calls']}次API调用、{usage['total_tokens']} Token，按峰时未缓存参考费率及预算折算假设估计人民币{usage['estimated_cny_upper_bound']}元；不是实际账单。

API输出异常保留在runs/exceptions.jsonl，一次盲审JSON语法异常仅提取完整原JSON对象离线恢复，原响应和恢复日志均保留；未隐藏失败后重跑。12项离线回归最终通过，第一次同进程模块输出路径污染导致的3项失败已记录并修复为独立模块实例，未改变旧审计模块。详情见test_validation.json。

可复现的本地命令（在项目目录执行）：`.venv\\Scripts\\python.exe -m evaluation.accept_golden_v12 prepare`、`pilot`、`semantic`、`bindings`、`probes`、`review`、`repair-check`；最后`.venv\\Scripts\\python.exe -m evaluation.accept_golden_v12_reporting`。已有相同Prompt的原始结果会读缓存，不重复付费；输入或Prompt不一致会拒绝复用。报告步骤不发模型请求。

## 逐条候选状态

|Case|当前场景|审计质量状态|必要来源|
|---|---|---|---|
{table}
'''
    (job.REPORT/'final_quality_acceptance_report.md').write_text(report,encoding='utf-8')
    (job.DATA/'README.md').write_text(f'# 采购RAG Golden正式候选\n\n版本：{job.VERSION}；46条。独立候选，未替换旧Case、未同步Airtable、未运行RAG。\n\n详见`{job.REPORT / "final_quality_acceptance_report.md"}`。JSONL为主数据，CSV为可读镜像，evaluation_schema.json为结构约束。\n\n仅REQUIRED评分点参与严格通过，接受同义；SUPPLEMENTARY事实和OPTIONAL点不进入必要证据召回分母。来源目录保留补充证据，不能按全部doc_id强制双源。原文到Chunk映射未实现，不得按文档命中算事实召回。\n',encoding='utf-8')
    # 最终清单在README完成后重算，后续可独立检查每个交付文件。
    delivery_manifest['files']={p.name:job.sha(p.read_bytes()) for p in job.DATA.iterdir() if p.is_file() and p.name!='manifest.json'}
    job.save(job.DATA/'manifest.json',delivery_manifest)
    print(json.dumps(stats,ensure_ascii=False,indent=2))


if __name__=='__main__':generate()
