"""基线结果汇总：保留分母、Checker不确定性和证据驱动的失败归因。"""
from __future__ import annotations

from collections import Counter
import copy
import csv
import json
from pathlib import Path

from . import rag_baseline as run
from . import rag_baseline_scoring as scoring
from .checker.context import EvidenceContext, resolve_pointer
from .checker.models import CriterionResult, CriterionStatus, EvidenceRef


PHASES=['baseline_final','baseline_context_repair','insufficiency_format_repair','runtime_metadata_review','retrieval_consistency_review']


def current_metric(cid,dimension):
    selected=None
    for phase in PHASES:
        path=run.RUN/'score_validation'/f'{phase}__{cid}__{dimension}.json'
        if not path.exists():path=run.RUN/'scores'/f'{phase}__{cid}__{dimension}.json'
        if path.exists():selected=dict(json.loads(path.read_text(encoding='utf-8')),record=str(path))
    return selected


def aligned_metric(metric,case,execution,evidence,alignments):
    if not metric or 'score' not in metric:return metric
    metric=copy.deepcopy(metric);payload=dict(score=metric['score'],reason=metric['reason'],confidence=metric.get('confidence'),**metric['details'])
    targets=[a for a in alignments if a['case_id']==case['case_id'] and a['dimension']==metric['dimension']]
    applied=[]
    for a in targets:
        origin=Path(a['metric_path']);assert run.sha(origin.read_bytes())==a['metric_sha256']
        assert resolve_pointer(json.loads(origin.read_text(encoding='utf-8')),a['pointer'])==a['original_quote']
        corpus=(execution['response'] or {}).get('message','') if a['verification_corpus']=='actual_answer' else next(h['text'] for h in execution['hits'] if h['chunk_id']==a['verification_corpus'])
        assert corpus[a['char_start']:a['char_end']]==a['verified_literal']
        pointer=a['pointer'].replace('/details/','/',1);parent,key=pointer.rsplit('/',1)
        container=resolve_pointer(payload,parent)
        assert container[key]==a['original_quote']
        container[key]=a['verified_literal'];applied.append(a)
    hits=scoring.effective_hits(execution,evidence) if metric['dimension']=='faithfulness' else execution['hits']
    warnings=scoring.validate_payload(metric['dimension'],payload,case,execution,hits)
    metric['checker_warnings']=warnings
    metric['status']='REVIEW_REQUIRED' if warnings else 'PASS' if metric['score']==2 else 'FAIL'
    metric['literal_alignment_evidence']=applied
    metric['framework_score_unchanged']=True
    # 原Judge输出不修改；引用定位补丁另存，不能伪装为新的框架输出。
    run.save(run.RUN/'adjudicated_scores'/f"{case['case_id']}__{metric['dimension']}.json",metric)
    return metric


def rate(n,d):return dict(numerator=n,denominator=d,value=n/d if d else None)
def shown(m):return f"{m['numerator']}/{m['denominator']}（{m['value']*100:.2f}%）" if m['value'] is not None else 'N/A'


def failure_attribution(case,e,metrics,det,consistency):
    tags=[];evidence=[]
    correctness=metrics['correctness'];retrieval=metrics['retrieval'];faithfulness=metrics['faithfulness']
    if det['environment_errors'] or e['status']!='COMPLETED_AWAITING_SCORING':
        tags.append('ENVIRONMENT');evidence.append('执行异常或KNOWLEDGE_ANSWER_FAILED，不能当作纯回答能力错误')
    if not e['retrievals']:
        tags.append('ROUTING');evidence.append('实际未调用search_knowledge；'+(e.get('response') or {}).get('route_reason_code',''))
    elif det['route']['status']=='FAIL':
        tags.append('ROUTING')
        evidence.append('额外业务读取被评测边界阻断' if e['api_attempts'] else '检索后直接返回正文，未遵循Runtime的propose_task_state/READ_VERIFIED终结路径')
    if correctness.get('status')=='FAIL' and e['retrievals']:
        if case['answerability']=='UNANSWERABLE':
            tags.append('GENERATION_INSUFFICIENCY');evidence.append('本题不应检索出不存在的政策结论；实际拒答欠缺必要核实路径，不归咎未召回具体政策')
        elif retrieval.get('score',0)<2:
            tags.append('RETRIEVAL');evidence.append('必要事实召回不全且实际答案漏项；参考具体Fact/Point结果，不以文档命中归因')
        elif any(p.get('status')=='UNCERTAIN' for p in correctness.get('details',{}).get('point_results',[])):
            tags.append('GENERATION_AGGREGATION');evidence.append('整轮已召回必要事实，但最终回答对同一判断自相矛盾；需结合多次检索/生成Trace确认')
        else:
            tags.append('GENERATION');evidence.append('必要事实已召回，最终答案仍未满足必要判断')
    if faithfulness.get('status')=='FAIL':tags.append('GENERATION_FAITHFULNESS');evidence.append('实际答案存在无据判断；以claim及真实输入证据复查')
    insuff=metrics.get('insufficiency')
    if insuff and insuff['status']=='FAIL' and correctness.get('status')=='FAIL' and 'ROUTING' not in tags:
        tags.append('GENERATION_INSUFFICIENCY');evidence.append('缺证说明或后续核实路径不完整，和召回不足分别记录')
    if any(m and m.get('status')=='REVIEW_REQUIRED' for m in metrics.values()):tags.append('CHECKER_PENDING');evidence.append('引文或逐项结果与框架总分不一致，不能强制视为RAG失败')
    if consistency:tags.append('CHECKER_RETRIEVAL_SCOPE_PENDING');evidence.append('答案正确且忠实，但按Fact的召回判定不足；一次盲化复核仍有矛盾，保留待核实，不修改Golden')
    return dict(primary=tags[0] if tags else 'NONE',tags=list(dict.fromkeys(tags)),evidence=evidence,causal_intervention_performed=False)


def generate():
    run.load_env_file(run.PROJECT/'.env');cases=run.cases();sources=run.golden.load_sources()
    frozen=json.loads((run.RUN/'frozen_config.json').read_text(encoding='utf-8'));mapping=json.loads((run.RUN/'chunk_source_mapping.json').read_text(encoding='utf-8'))
    assert run.protected()==json.loads((run.RUN/'protected_inputs.json').read_text(encoding='utf-8'))
    alignments=json.loads((run.REPORT/'checker_evidence_alignments.json').read_text(encoding='utf-8'))['alignments']
    results=[];raw_all=[];usage=Counter();first_ks=Counter();all_ks=Counter();retrieval_calls=0;generation_calls=0;repeated=0;source_first=[];source_union=[];semantic_facts=[];semantic_primary=[]
    for c in cases:
        cid=c['case_id'];e=json.loads((run.RUN/'cases'/cid/'execution.json').read_text(encoding='utf-8'));evidence=json.loads(Path(e['evidence_path']).read_text(encoding='utf-8'))
        det=scoring.deterministic(e)
        run.save(run.RUN/'deterministic'/f'{cid}.json',det)
        metrics={d:aligned_metric(current_metric(cid,d),c,e,evidence,alignments) for d in scoring.RUBRICS}
        assert all(metrics[d] for d in ['retrieval','correctness','faithfulness']),cid
        consistency=c['answerability']!='UNANSWERABLE' and metrics['retrieval'].get('score')!=2 and metrics['correctness'].get('status')=='PASS' and metrics['faithfulness'].get('status')=='PASS'
        # 确定失败优先；其余有证据矛盾时待核实，不用平均分或乐观多数票。
        required=[metrics[d] for d in ['correctness','faithfulness','insufficiency'] if metrics[d] and metrics[d]['status']!='NOT_APPLICABLE']
        fail=det['route']['status']=='FAIL' or det['no_business_write']['status']=='FAIL' or any(m['status']=='FAIL' for m in required)
        uncertain=consistency or any(m['status']=='REVIEW_REQUIRED' for m in required)
        final='EVIDENCE_MISSING' if e['status']!='COMPLETED_AWAITING_SCORING' or det['environment_errors'] else 'FAIL' if fail else 'REVIEW_REQUIRED' if uncertain else 'PASS'
        first_hits=e['retrievals'][0]['hits'] if e['retrievals'] else []
        first=run.canonical_recall(c,first_hits,mapping,sources)
        union=run.canonical_recall(c,e['hits'],mapping,sources)
        if c['answerability']!='UNANSWERABLE':
            source_first.append(first);source_union.append(union)
            primary_path=run.RUN/'scores'/f'baseline_final__{cid}__retrieval.json'
            semantic_primary+=json.loads(primary_path.read_text(encoding='utf-8')).get('details',{}).get('fact_results',[])
            semantic_facts+=metrics['retrieval'].get('details',{}).get('fact_results',[])
        if e['retrievals']:first_ks[len(e['retrievals'][0]['hits'])]+=1
        all_ks.update(len(r['hits']) for r in e['retrievals']);retrieval_calls+=len(e['retrievals']);repeated+=len(e['retrievals'])>1
        generation_calls+=sum(v.get('event_type')=='KNOWLEDGE_ANSWER_GENERATED' for v in evidence['trace']['events'])
        for path in (run.RUN/'cases'/cid/'model_calls').glob('*.json'):
            raw=json.loads(path.read_text(encoding='utf-8'));u=raw.get('model_turn',{}).get('usage',{})
            usage.update({k:u.get(k,0) or 0 for k in ['prompt_tokens','completion_tokens','total_tokens']});raw_all.append(path)
        criteria=[]
        for dim,m in metrics.items():
            if not m or m['status']=='NOT_APPLICABLE':continue
            criteria.append(CriterionResult(dim,scoring.RUBRICS[dim]['title'],'rag-baseline-v121',CriterionStatus(m['status']),'semantic_review',dict(score=m.get('score'),details=m.get('details'),checker_warnings=m.get('checker_warnings',[])),[EvidenceRef('/steps/0/agent_output/message','实际最终回答'),EvidenceRef('/trace/events','实际检索和生成Trace')],reason=m.get('reason')).to_dict())
        attribution=failure_attribution(c,e,metrics,det,consistency)
        result=dict(schema_version='scored-result-1.0',checker_version='rag-baseline-1.0',case_id=cid,suite='rag_baseline',category=c['scenario_type'],answerability=c['answerability'],run_id=e['run_id'],model=frozen['generation'],evidence=dict(path=e['evidence_path'],sha256=run.sha(Path(e['evidence_path']).read_bytes())),criteria=criteria,final_result=final,strict_pass=final=='PASS',metrics=metrics,deterministic=det,retrieval_consistency_pending=consistency,canonical_first=first,canonical_union=union,first_k=len(first_hits) if first_hits else None,effective_unique_chunks=len(e['hits']),retrieval_calls=len(e['retrievals']),answer=e['response']['message'] if e['response'] else None,attribution=attribution)
        results.append(result);run.save(run.RUN/'cases'/cid/'scored_result.json',result)
    summary=dict(actual_unique_cases=len(results),completed=sum(r['final_result']!='EVIDENCE_MISSING' for r in results),status_counts=dict(Counter(r['final_result'] for r in results)),answer_correctness=rate(sum(r['metrics']['correctness']['status']=='PASS' for r in results),46),correctness_statuses=dict(Counter(r['metrics']['correctness']['status'] for r in results)),faithfulness=rate(sum(r['metrics']['faithfulness']['status']=='PASS' for r in results),sum(r['metrics']['faithfulness']['status']!='NOT_APPLICABLE' for r in results)),faithfulness_statuses=dict(Counter(r['metrics']['faithfulness']['status'] for r in results)),strict_chain_pass=rate(sum(r['strict_pass'] for r in results),46),knowledge_retrieval_executed=rate(sum(r['retrieval_calls']>0 for r in results),46),route_contract=rate(sum(r['deterministic']['route']['status']=='PASS' for r in results),46),no_business_write=rate(sum(r['deterministic']['no_business_write']['status']=='PASS' for r in results),46),canonical_first_fact_recall=rate(sum(x['covered'] for x in source_first),sum(x['total'] for x in source_first)),canonical_union_fact_recall=rate(sum(x['covered'] for x in source_union),sum(x['total'] for x in source_union)),canonical_first_all_facts=rate(sum(x['covered']==x['total'] for x in source_first),len(source_first)),canonical_union_all_facts=rate(sum(x['covered']==x['total'] for x in source_union),len(source_union)),semantic_fact_recall_estimate=rate(sum(x['status']=='SUPPORTED' for x in semantic_facts),len(semantic_facts)),semantic_primary_fact_recall_estimate=rate(sum(x['status']=='SUPPORTED' for x in semantic_primary),len(semantic_primary)),semantic_fact_statuses=dict(Counter(x['status'] for x in semantic_facts)),semantic_all_facts_answerable=rate(sum(r['answerability']!='UNANSWERABLE' and r['metrics']['retrieval']['status']=='PASS' for r in results),44),preset_insufficiency=rate(sum(r['answerability']!='FULL' and r['metrics']['insufficiency']['status']=='PASS' for r in results),sum(r['answerability']!='FULL' for r in results)),preset_partial=rate(sum(r['answerability']=='PARTIAL' and r['metrics']['insufficiency']['status']=='PASS' for r in results),sum(r['answerability']=='PARTIAL' for r in results)),preset_unanswerable=rate(sum(r['answerability']=='UNANSWERABLE' and r['metrics']['insufficiency']['status']=='PASS' for r in results),2),retrieval_scope_conflicts=[r['case_id'] for r in results if r['retrieval_consistency_pending']],primary_attribution=dict(Counter(r['attribution']['primary'] for r in results if r['final_result']!='PASS')),attribution_tags=dict(Counter(t for r in results if r['final_result']!='PASS' for t in r['attribution']['tags'])),retrieval_calls=retrieval_calls,first_k_distribution=dict(first_ks),all_search_k_distribution=dict(all_ks),repeated_retrieval_cases=repeated,knowledge_generation_calls=generation_calls,agent_model_calls=len(raw_all),agent_token_usage=dict(usage),no_rag_reruns=True,scenario_distribution=dict(Counter(r['category'] for r in results)),run_scope='original_agent_router_and_knowledge_path; business_API_blocked')
    judge_usage=json.loads((run.REPORT/'judge_usage.json').read_text(encoding='utf-8'))
    agent_usd=(usage['prompt_tokens']*.30+usage['completion_tokens']*1.20)/1e6
    summary['cost_estimate']=dict(agent_cny_upper_bound=round(agent_usd*8,4),judge_cny_upper_bound=judge_usage['estimated_cny_upper_bound'],total_cny_upper_bound=round(agent_usd*8+judge_usage['estimated_cny_upper_bound'],4),not_actual_bill=True,assumed_usd_per_million_input=.30,assumed_usd_per_million_output=1.20,cny_per_usd_budget_assumption=8)
    run.save(run.REPORT/'metrics.json',summary)
    (run.REPORT/'case_results.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in results),encoding='utf-8')
    with (run.REPORT/'case_results.csv').open('w',encoding='utf-8-sig',newline='') as f:
        fields=['case_id','scenario','answerability','final_result','retrieval_status','correctness_status','faithfulness_status','insufficiency_status','canonical_first','canonical_union','first_k','retrieval_calls','primary_attribution','attribution_tags','evidence_path','answer']
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for r in results:w.writerow(dict(case_id=r['case_id'],scenario=r['category'],answerability=r['answerability'],final_result=r['final_result'],**{d+'_status':r['metrics'][d]['status'] if r['metrics'][d] else 'NOT_APPLICABLE' for d in scoring.RUBRICS},canonical_first=f"{r['canonical_first']['covered']}/{r['canonical_first']['total']}",canonical_union=f"{r['canonical_union']['covered']}/{r['canonical_union']['total']}",first_k=r['first_k'],retrieval_calls=r['retrieval_calls'],primary_attribution=r['attribution']['primary'],attribution_tags=','.join(r['attribution']['tags']),evidence_path=r['evidence']['path'],answer=r['answer']))
    bad=['# Bad Case与待核实项','', '归因是根据已保存证据作出的诊断，未进行改配置、补召回或换模型等因果干预。REVIEW_REQUIRED不算成功，也不强制算模型失败。','']
    by_id={c['case_id']:c for c in cases}
    for r in results:
        if r['final_result']=='PASS':continue
        c=by_id[r['case_id']]
        bad += [f"## {r['case_id']} · {r['final_result']}",'',f"问题：{c['question']}",f"归因：{', '.join(r['attribution']['tags'])}",f"证据：`{r['evidence']['path']}`",f"首次K={r['first_k']}；检索{r['retrieval_calls']}次；规范事实覆盖首次{r['canonical_first']['covered']}/{r['canonical_first']['total']}、整轮{r['canonical_union']['covered']}/{r['canonical_union']['total']}。",'']
        for note in r['attribution']['evidence']:bad.append('- '+note)
        for d,m in r['metrics'].items():
            if not m or m['status'] in ['PASS','NOT_APPLICABLE']:continue
            bad += [f"- {d} [{m['status']}]：{m.get('reason','')}；原始评分 `{m.get('record','')}`"]
            for row in m.get('details',{}).get('point_results',[]):
                if row['status']!='SATISFIED':bad.append(f"  - {row['point_id']} {row['status']}：{row['reason']}")
        bad += ['', '**实际回答节选**','',r['answer'][:1800] if r['answer'] else '无实际回答','']
    (run.REPORT/'bad_cases.md').write_text('\n'.join(bad),encoding='utf-8')
    recommendations='''# 下一步优化建议（本轮未实施）

1. 优先复现A03/D01的路由偏离：知识或假设咨询为何进入申请字段补问；同时检查A05/B18为什么对方法问题额外读取业务申请，B17/U01为什么直接结束为MODEL_REQUESTED_CLARIFICATION。不要用强制插入检索来掩盖真实路由问题。
2. 检查D02的重复检索与逐次回答拼接：同一原问题的不同K先后召回不同证据，最终同时输出“不知道扣减时点”和“生成PO才扣”。下一轮可单独测试去重检索、汇总证据后一次生成，保留本轮Baseline作对照。
3. 针对B05/B06/B15/C02/C03/D05/D07等证据漏召回建立诊断表：区分命中“典型业务问题/来源限制”与命中实际建议路径/判断边界。再用单因素实验比较相邻章节补全、父文档扩展、检索排序或模型方案；本轮未变Chunk、Embedding或Top-K。
4. 六条Fact召回与答案判定矛盾项先修清评判口径：不能因整段原文的题外细节未覆盖就否定题内事实，也不能因语义相近就忽略条件。可用独立模型盲审或人工抽样验证同义事实支持；不得据此自动改Golden。
5. Checker继续使用逐条事实、答案短引及真实可见上下文。分类元数据、路由模型直接回答所见工具结果也属于来源；禁止只按文档ID命中算召回。D02的原始框架总分与逐项不确定仍不一致，需保留待核实。
6. 后续模型/检索比较先确定受控口径：当前Agent允许模型选择K并重复搜索，不是统一Recall@4。另设固定K的检索组件实验时要明确这是新配置，不能覆盖本轮全链路Baseline。真实用户问题分布、延迟稳定性、重复采样方差和更广安全能力均需另行验证。
'''
    (run.REPORT/'next_steps.md').write_text(recommendations,encoding='utf-8')
    table='\n'.join(f"|{r['case_id']}|{r['final_result']}|{r['metrics']['correctness']['status']}|{r['metrics']['faithfulness']['status']}|{r['attribution']['primary']}|" for r in results)
    report=f'''# 采购Agent RAG首次基线评测

## 实际运行与结论

v1.2.1已验收46条全部通过真实`AgentRuntime.handle_message`执行并保存Evidence/Trace/模型调用；预检A01、D06、U02已复用，没有重新调用被测RAG。执行完成{summary['completed']}/46。严格全链路结果：`{summary['status_counts']}`，通过率{shown(summary['strict_chain_pass'])}。待核实不算成功，也不等同已确认模型失败。

主要现象是回答漏项和路由偏离，已有证据下的回答通常忠实；不能把高忠实性解释为答案完整正确。D02证明“整轮最终召回足够”也可能因多次回答拼接而自相矛盾。没有运行补证/调参对照实验，因此归因是证据指向，不是因果改善保证。

## 配置冻结与适配边界

正式知识库52篇，索引364个Chunk；Embedding `{frozen['index']['model_id']}`，512维；切块700字符、重叠80；相似度门槛{frozen['min_score']}。52篇原文字节、Golden全部文件、索引、业务代码及.env哈希前后不变。现有模型缓存离线加载并记录权重/分词文件哈希与revision，未下载替代模型。

生成模型 `{frozen['generation']['model_name']}`，温度0，生成Prompt与系统Prompt未修改。max_tokens、思考模式和SDK重试保持原有默认，记录原始请求接口及Token使用；服务端模型别名的实际权重版本不能由本次冻结保证。

**Top-K不是固定4**：现有工具默认4，允许模型指定1～8。本轮保持该真实策略，首次实际K分布`{dict(first_ks)}`，全部检索K分布`{dict(all_ks)}`。因此下列指标是实际K策略的Baseline，不宣称Recall@4。

旧Runner未注入知识索引且旧CaseLoader只接受五字段。本轮只新增薄适配，复用原`KnowledgeIndex.search`、真实编码器、AgentRuntime、OpenAICompatibleModel、TraceWriter、EvidenceCollector、Case/Scenario类型、EvidenceContext、路由/供应商检查及scored-result-1.0结构；没有新建业务评测平台。旧业务TSR报告器按旧套件统计，不能把46条Golden硬塞进去伪报业务TSR，故新增RAG专项汇总。

所有业务API方法均在评测侧阻断，不连接真实Backend；独立SQLite复制快照只用于无写入观察，历史事件保留并排除于本次新事件。A05/B18曾尝试GET申请，均被阻断，属于本轮只读知识边界下的路由偏离，不假装Backend不可用。新写入事件、业务表变化和确认写卡均为0，禁止业务写入检查{shown(summary['no_business_write'])}。

## 主要指标及分母

|指标|结果|口径|
|---|---|---|
|真实执行覆盖|46/46|含路由失败与待核实，未隐去失败|
|实际触发知识检索|{shown(summary['knowledge_retrieval_executed'])}|没有调用的两条是路由失败，不能归咎索引|
|规范Golden原文事实召回·首次|{shown(summary['canonical_first_fact_recall'])}|44条可回答题；逐Fact原文区间，忽略空白但不忽略否定/数字|
|规范Golden原文事实召回·整轮并集|{shown(summary['canonical_union_fact_recall'])}|合并同一轮全部真实检索；不伪装成一次Top-K|
|规范全部事实齐全·整轮|{shown(summary['canonical_union_all_facts'])}|整题全部必要原文区间齐全|
|语义必要事实召回估计|{shown(summary['semantic_fact_recall_estimate'])}|同义证据可支持，Judge估计；六题范围矛盾待核实|
|语义全部事实齐全|{shown(summary['semantic_all_facts_answerable'])}|44条可回答题；不是文档召回率|
|答案正确性|{shown(summary['answer_correctness'])}|只要求REQUIRED，OPTIONAL缺失不扣；状态`{summary['correctness_statuses']}`|
|证据忠实性|{shown(summary['faithfulness'])}|仅43条实质回答；3条无实质知识生成不计忠实性成功；状态`{summary['faithfulness_statuses']}`|
|预设证据不足处理|{shown(summary['preset_insufficiency'])}|4条PARTIAL+2条UNANSWERABLE；其他FULL漏召回为附加诊断|
|PARTIAL处理|{shown(summary['preset_partial'])}|有据部分+具体缺证边界及核实路径|
|UNANSWERABLE处理|{shown(summary['preset_unanswerable'])}|U01完整说明及核实路径，U02模板拒答缺下一步来源|
|知识只读路由合同|{shown(summary['route_contract'])}|STOP/READ_VERIFIED+实际知识检索+无额外业务API尝试|
|严格全链路通过|{shown(summary['strict_chain_pass'])}|答案、忠实性、缺证处理及路由/无写入通过；指标矛盾待核实|

52篇与364个Chunk均可精确回溯；`chunk_source_mapping.json`存原文字节哈希与Unicode位置，证据覆盖按区间核对。文档命中不会自动算Fact召回。规范来源召回是保守诊断：其他放行原文等义支持不会命中同一规范区间，因此另列语义召回。UNANSWERABLE两题的元处理原则不混入知识事实召回主分母。

六条Fact召回与答案判定矛盾：`{summary['retrieval_scope_conflicts']}`。已经做一次不展示实际答案/前次分数、反序来源和按题内评分范围的复核，仍未消除矛盾，保持REVIEW_REQUIRED；不更改Golden、不强迫算检索失败或成功。

## 失败归因

非PASS主归因（互斥）`{summary['primary_attribution']}`。诊断标签（可重叠）`{summary['attribution_tags']}`，不可把标签数量相加当作Case数。

- A03/D01：未进入知识检索，输出采购字段补问，属路由/意图错误。
- B05/B06/B15/C02/C03/D05/D07等：实际片段缺关键知识，回答往往诚实但不完整；主要指向检索证据缺口，并分别记录缺证后的生成处理问题。
- D02：{next(r['retrieval_calls'] for r in results if r['case_id']=='RGV1-D02')}次检索后整轮规范必要事实齐全，最终仍拼接“未说明预算扣减时点”和“仅生成PO扣一次”，属生成聚合问题；不能只看最后一段判正确。
- B17/U01：路由模型看到工具结果后直接作答，绕过专用生成并终结为MODEL_REQUESTED_CLARIFICATION；内容正确性与路由结果分别计分。
- Checker：引用概括、Markdown强调与class元数据曾造成误报；已通过保存原始输出、逐字定位和明确可见上下文处理，未改Judge原分数。U01的工具证据不足标记可由真实输入确认，定向补全上下文后复核。D02的忠实性总分与逐项UNCERTAIN仍不一致，保留待核实。

全部Bad Case的问题、实际回答、缺失Point、原始理由、证据路径见bad_cases.md及逐Case JSONL/CSV。环境异常单列；本轮未发现执行/模型传输导致的Case缺失。

## 运行量、费用及质量限制

实际检索{retrieval_calls}次，其中{repeated}条重复检索；相较44次首次检索，额外22次只让规范来源事实覆盖从62/100增至63/100，且D02出现拼接矛盾；这不是语义收益的因果估计。专用知识生成{generation_calls}次；真实Agent模型调用{len(raw_all)}次，Token `{dict(usage)}`。Judge调用{judge_usage['calls']}次（含预检、格式修正及6条选择性复核），Token {judge_usage['total_tokens']}。基于参考费率和8元/美元预算折算假设估算总成本约{summary['cost_estimate']['total_cny_upper_bound']}元；不是实际账单，缓存优惠未扣。

G-Eval沿用本机DeepEval 4.2.8的schema-aware接入，按独立固定Rubric判断检索、正确性、忠实性与不足处理，非一次自由评论冒充框架成绩。主审结果、原始Judge提示/响应、引用定位修正及选择性复核均保留。Judge与生成器使用同一已配置模型，存在同族偏差；单次0/1/2分和引文定位不能保证所有推理正确或所有幻觉被检出。

这46条从知识库反向设计，不代表真实用户问题分布；场景A14/B24/C0/D8，C类共同支持判断未覆盖。本轮没有验证写入能力、真实业务API可用性、固定K组件优劣、不同Embedding/模型/Prompt性能、重复运行方差或真实用户总体成功率。后续优化建议见next_steps.md，本轮未自动调参。

## 交付、代码及复现

- 报告：`{run.REPORT}`，包括metrics.json、case_results.jsonl/.csv、bad_cases.md、next_steps.md。
- 原始证据：`{run.RUN}`，每Case含evidence.json、trace.jsonl、原始模型输入/ModelTurn、retrievals及scored_result.json；配置与原文映射在该目录根。
- 新增代码：evaluation/rag_baseline.py、rag_baseline_scoring.py、rag_baseline_reporting.py、evaluation/tests/test_rag_baseline.py；仅在evaluation/README.md补充运行说明。未修改Agent/Backend、知识库、Golden、索引或生成Prompt；未同步Airtable。
- 本轮离线回归17项通过，见test_validation.json；46条Evidence关联/完整性、Embedding与受保护输入不变、1342份原始文件哈希及CSV读回校验见final_validation.json。
- 在项目目录执行`.venv\\Scripts\\python.exe -m evaluation.rag_baseline freeze`、`pilot`、`run`；评分入口`-m evaluation.rag_baseline_scoring pilot`、`all`、`format-repairs`；汇总`-m evaluation.rag_baseline_reporting`。汇总不调用模型。已有Case执行结果只读缓存；已有Judge相同Prompt只读缓存，输入/Prompt变动拒绝复用。

## 逐Case结果

|Case|严格结果|正确性|忠实性|主归因|
|---|---|---|---|---|
{table}
'''
    (run.REPORT/'baseline_report.md').write_text(report,encoding='utf-8')
    fingerprints=[]
    for p in sorted(run.RUN.rglob('*')):
        if p.is_file() and p.name!='artifact_manifest.json':fingerprints.append(dict(path=str(p.relative_to(run.RUN)),sha256=run.sha(p.read_bytes()),bytes=p.stat().st_size))
    run.save(run.RUN/'artifact_manifest.json',dict(files=fingerprints,protected_inputs_unchanged=True,raw_rag_cases=46,source_chunk_mappings=364))
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':generate()
