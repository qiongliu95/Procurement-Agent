"""v1.2.1 Golden到真实只读Runtime的薄适配；不修改检索、Prompt和业务模块。"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import sqlite3
import time

import numpy as np

from agent_runtime import AgentRuntime, OpenAICompatibleModel, TraceWriter, SYSTEM_PROMPT
from agent_tools import ProcurementApiClient, ProcurementTransportError
from knowledge_rag import KnowledgeIndex, KnowledgeChunk, default_knowledge_dir, default_index_dir, MODEL_ID
from .case_loader import AgentCase, ExecutionScenario, FixtureSpec, LoadedCase
from .evidence import EvidenceCollector, redact_secrets
from .fixture import snapshot_database
from .runner import load_env_file, runtime_state, sanitized_base_url
from . import golden_dataset as golden

PROJECT=Path(__file__).resolve().parents[1]
ROOT=PROJECT.parent
DATA=ROOT/'评估case/rag_golden_v1_2_accepted_candidate'
RUN=PROJECT/'evaluation/runs/rag_baseline_v1_2_1_20261014'
REPORT=PROJECT/'evaluation/reports/rag_baseline_v1_2_1_20261014'
PILOT=['RGV1-A01','RGV1-D06','RGV1-U02']


def sha(raw):return hashlib.sha256(raw).hexdigest()
def dump(v):return json.dumps(v,ensure_ascii=False,sort_keys=True)
def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def cases():
    values=golden.read_jsonl(DATA/'evaluation_cases.jsonl')
    assert len(values)==len({c['case_id'] for c in values})==46
    assert all(c['case_version']=='rag-golden-1.2.1-accepted-candidate' for c in values)
    manifest=json.loads((DATA/'manifest.json').read_text(encoding='utf-8'))
    for name,h in manifest['files'].items():assert sha((DATA/name).read_bytes())==h
    return values


def protected():
    files=[PROJECT/f for f in ['agent_runtime.py','agent_tools.py','agent_server.py','backend.py','knowledge_rag.py','procurement_rules_v1.json','.env']]
    files+=list(DATA.glob('*'))
    files+=list(default_index_dir().glob('*'))
    files+=[default_knowledge_dir()/'manifest.json',*default_knowledge_dir().rglob('*.md')]
    return {str(p.resolve()):sha(p.read_bytes()) for p in files if p.is_file()}


class FrozenIndex(KnowledgeIndex):
    """仅禁止ensure_index写入；检索排序与真实编码器仍完全调用原实现。"""
    def __init__(self):
        super().__init__(default_knowledge_dir(),default_index_dir(),model_id=os.getenv('AGENT_EMBEDDING_MODEL',MODEL_ID),max_chars=int(os.getenv('AGENT_RAG_CHUNK_CHARS','700')),overlap_chars=int(os.getenv('AGENT_RAG_CHUNK_OVERLAP','80')),min_score=float(os.getenv('AGENT_RAG_MIN_SCORE','0.62')))
        self.file_hashes={str(self.index_dir/n):sha((self.index_dir/n).read_bytes()) for n in ['index_meta.json','chunks.json','vectors.npy']}
        self._meta=json.loads((self.index_dir/'index_meta.json').read_text(encoding='utf-8'))
        _,documents,fingerprint=self._manifest()
        assert fingerprint==self._meta['corpus_fingerprint'],'缓存指纹不匹配；本轮禁止重建'
        self._chunks=[KnowledgeChunk(**v) for v in json.loads((self.index_dir/'chunks.json').read_text(encoding='utf-8'))]
        self._vectors=np.load(self.index_dir/'vectors.npy',allow_pickle=False)
        self._validate_index(self._vectors,self._chunks)
        assert self._chunks==self._load_chunks(documents),'当前原文/切块器与冻结缓存不一致'

    def ensure_index(self,*,force=False):
        assert not force,'Baseline禁止重建索引'
        for p,h in self.file_hashes.items():assert sha(Path(p).read_bytes())==h,'执行中索引发生变化'
        return {**self._meta,'rebuilt':False}


def source_mapping(index,sources):
    """把当前Chunk准确映射回原文；不以doc_id命中替代区间覆盖。"""
    mapped={}
    for chunk in index._chunks:
        doc=sources[chunk.doc_id]
        assert chunk.document_sha256==doc['hash']
        raw=doc['text'];normalized=[];positions=[];i=0
        while i<len(raw):
            positions.append(i)
            if raw[i:i+2]=='\r\n':normalized.append('\n');i+=2
            else:normalized.append(raw[i]);i+=1
        text=''.join(normalized);needle=chunk.text.replace('\r\n','\n')
        starts=[];cursor=0
        while True:
            found=text.find(needle,cursor)
            if found<0:break
            starts.append(found);cursor=found+1
        assert len(starts)==1,(chunk.chunk_id,'无法唯一精确映射')
        start=starts[0];end=start+len(needle)
        a=positions[start];b=positions[end] if end<len(positions) else len(raw)
        assert raw[a:b].replace('\r\n','\n')==needle
        mapped[chunk.chunk_id]=dict(chunk_id=chunk.chunk_id,doc_id=chunk.doc_id,source_hash=doc['hash'],char_start=a,char_end=b,line_start=raw.count('\n',0,a)+1,line_end=raw.count('\n',0,b)+1,text_sha256=sha(chunk.text.encode('utf-8')))
    return mapped


def span_covered(source_text,span,intervals):
    # 只忽略换行/空格缺口，不忽略任何业务字符、数字或否定词。
    return all(ch.isspace() or any(a<=i<b for a,b in intervals) for i,ch in enumerate(source_text[span['char_start']:span['char_end']],span['char_start']))


def canonical_recall(case,hits,mapping,sources):
    required=set(case['acceptance_review']['necessary_fact_ids']);by_e={e['evidence_id']:e for e in case['golden_evidence']}
    records=[]
    for fact in case['required_facts']:
        if fact['fact_id'] not in required:continue
        evs=[by_e[e] for e in fact['evidence_ids']];checks=[]
        for e in evs:
            matching=[mapping[h['chunk_id']] for h in hits if mapping[h['chunk_id']]['doc_id']==e['doc_id'] and mapping[h['chunk_id']]['source_hash']==e['source_hash']]
            ok=span_covered(sources[e['doc_id']]['text'],e['source_span'],[(m['char_start'],m['char_end']) for m in matching])
            checks.append(dict(evidence_id=e['evidence_id'],covered=ok,overlapping_chunk_ids=[m['chunk_id'] for m in matching if m['char_start']<e['source_span']['char_end'] and m['char_end']>e['source_span']['char_start']]))
        records.append(dict(fact_id=fact['fact_id'],covered=all(c['covered'] for c in checks),evidence_checks=checks))
    return dict(status='MEASURED' if hits else 'EVIDENCE_MISSING',covered=sum(f['covered'] for f in records),total=len(records),facts=records,interpretation='规范来源区间覆盖，保守诊断；不是所有可接受替代来源的语义召回，不把整段题外细节作为答案必答')


def freeze():
    load_env_file(PROJECT/'.env')
    RUN.mkdir(parents=True,exist_ok=True);REPORT.mkdir(parents=True,exist_ok=True)
    values=cases();sources=golden.load_sources();index=FrozenIndex()
    config=dict(dataset=str(DATA/'evaluation_cases.jsonl'),dataset_sha256=sha((DATA/'evaluation_cases.jsonl').read_bytes()),case_version=values[0]['case_version'],cases=46,index=index._meta,index_hashes=index.file_hashes,min_score=index.min_score,query_instruction=__import__('knowledge_rag').QUERY_INSTRUCTION,top_k=dict(default=4,tool_parameter_policy='existing_model_selected_integer_1_to_8',forced_override=False),generation=dict(model_name=os.getenv('AGENT_MODEL_NAME'),base_url=sanitized_base_url(os.getenv('AGENT_MODEL_BASE_URL','')),temperature=0,sdk_default_max_retries=2,max_tokens='existing_service_default',thinking='existing_service_default',prompt_source_sha256=sha((PROJECT/'agent_runtime.py').read_bytes()),system_prompt_sha256=sha(SYSTEM_PROMPT.encode('utf-8'))),runtime={k:int(os.getenv(k,d)) for k,d in [('AGENT_CONFIRM_TTL_SECONDS','600'),('AGENT_MAX_MODEL_STEPS','6'),('AGENT_WRITE_RETRY_LIMIT','1'),('AGENT_CONVERSATION_TURNS','12'),('AGENT_MODEL_CONTEXT_MESSAGES','6'),('AGENT_SESSION_HISTORY_MESSAGES','200')]},versions={k:metadata.version(k) for k in ['sentence-transformers','torch','transformers','numpy','openai']},read_only_guard='all_business_api_methods_blocked_no_network_delegate',pilot=PILOT,execution='unchanged AgentRuntime.handle_message -> actual tool selection -> KnowledgeIndex.search -> unchanged _generate_knowledge_answer',judge_gold_is_never_sent_to_tested_model=True)
    for name,value in [('frozen_config.json',config),('protected_inputs.json',protected())]:
        path=RUN/name
        if path.exists():assert json.loads(path.read_text(encoding='utf-8'))==value,'冻结输入/配置发生变化'
        else:save(path,value)
    model_cache=Path.home()/'.cache/huggingface/hub'/('models--'+index.model_id.replace('/','--'))
    reference=model_cache/'refs/main'
    if reference.exists():
        revision=reference.read_text(encoding='utf-8').strip()
        snapshot=model_cache/'snapshots'/revision
        artifacts={str(p.relative_to(snapshot)):sha(p.read_bytes()) for p in snapshot.rglob('*') if p.is_file()}
        frozen_model=dict(model_id=index.model_id,revision=revision,artifacts=artifacts,offline_loading=True)
        target=RUN/'embedding_artifacts.json'
        if target.exists():assert json.loads(target.read_text(encoding='utf-8'))==frozen_model,'Embedding缓存版本发生变化'
        else:save(target,frozen_model)
    mapping=source_mapping(index,sources);save(RUN/'chunk_source_mapping.json',mapping)
    # 只复制既有SQLite快照供无写入观察；不启动Backend、不清理或伪造历史审计事件。
    dbpath=RUN/'observation_fixture.sqlite'
    if not dbpath.exists():
        src=sqlite3.connect((PROJECT/'demo_test.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
        dst=sqlite3.connect(dbpath)
        try:src.backup(dst)
        finally:dst.close();src.close()
    save(RUN/'observation_fixture_setup.json',dict(path=str(dbpath),sha256=sha(dbpath.read_bytes()),role='仅观测无写入；不向Agent提供任何业务事实',original_history_retained=True))
    return values,sources,index,mapping,config


class BusinessApiGuard(ProcurementApiClient):
    def __init__(self):
        # 不绑定真实HTTP客户端；即使错误路由到业务方法也不能访问业务服务。
        self.attempts=[]
    def _request(self,method,path,**kwargs):
        self.attempts.append(dict(method=method,path=path,blocked=True))
        raise ProcurementTransportError('BASELINE_KNOWLEDGE_ONLY_BUSINESS_API_BLOCKED',uncertain=False)
    def get_procurement_rules(self,*args,**kwargs):
        self.attempts.append(dict(method='LOCAL_RULES',path='get_procurement_rules',blocked=True))
        raise ProcurementTransportError('BASELINE_KNOWLEDGE_ONLY_NON_RAG_TOOL_BLOCKED',uncertain=False)


class RecordingModel:
    def __init__(self,model,path):self.model=model;self.path=path;self.number=0
    def complete(self,messages,tools):
        self.number+=1;started=time.monotonic()
        record=dict(call_index=self.number,started_at=__import__('agent_runtime').utc_now(),messages=messages,tools=tools,temperature=0)
        try:
            result=self.model.complete(messages,tools)
            record['model_turn']=asdict(result)
            return result
        except Exception as e:
            record['exception']=dict(type=type(e).__name__,message=redact_secrets(str(e)))
            raise
        finally:
            record['elapsed_seconds']=round(time.monotonic()-started,3)
            save(self.path/f'{self.number:02d}.json',record)


def run_one(case,index,mapping,sources,config):
    cid=case['case_id'];path=RUN/'cases'/cid
    if (path/'execution.json').exists():
        result=json.loads((path/'execution.json').read_text(encoding='utf-8'))
        assert result['question']==case['question']
        print('CACHED',cid,flush=True);return result
    assert not path.exists(),'已有未完成运行需保留，禁止自动覆盖或重跑'
    run_id=cid+'-rag-baseline-v121';loaded=LoadedCase(AgentCase(cid,'rag_golden_v1_2_1',(case['question'],),'readonly-observation',tuple(p['criterion'] for p in case['answer_key_points'] if p['scoring_role']=='REQUIRED')),ExecutionScenario(cid,'agent','real_rag_single_turn',({'type':'user_message','text':case['question']},),'SOURCE_READY',()),FixtureSpec('readonly-observation',cid,'READY',{'business_api':'BLOCK_ALL','db_observation_only':True}))
    collector=EvidenceCollector(path,run_id,loaded,config['generation'],config['runtime'])
    before=snapshot_database(RUN/'observation_fixture.sqlite');api=BusinessApiGuard();model_base=OpenAICompatibleModel.from_env();model=RecordingModel(model_base,path/'model_calls')
    r=config['runtime'];runtime=AgentRuntime(model,api,TraceWriter(path/'traces'),model_name=config['generation']['model_name'],knowledge_index=index,confirm_ttl_seconds=r['AGENT_CONFIRM_TTL_SECONDS'],max_model_steps=r['AGENT_MAX_MODEL_STEPS'],write_retry_limit=r['AGENT_WRITE_RETRY_LIMIT'],conversation_turns=r['AGENT_CONVERSATION_TURNS'],model_context_messages=r['AGENT_MODEL_CONTEXT_MESSAGES'],session_history_messages=r['AGENT_SESSION_HISTORY_MESSAGES'])
    state=runtime.new_session('eval-'+run_id);initial=runtime_state(state);response=None;status='COMPLETED_AWAITING_SCORING';reason=None;started=time.monotonic()
    try:response=runtime.handle_message(state,case['question'])
    except Exception as e:
        status='RUN_ERROR';reason=redact_secrets(f'{type(e).__name__}: {e}');collector.add_exception(e,phase='execution')
    finally:model_base.client.close()
    collector.add_step(dict(step_index=1,step_type='user_message',user_input=case['question'],agent_output=response,runtime_state_before=initial,runtime_state_after=runtime_state(state)))
    after=snapshot_database(RUN/'observation_fixture.sqlite')
    evidence=collector.finish(status=status,status_reason=reason,session_id=state.session_id,initial_state=before,final_state=after)
    retrievals=[e['sanitized_result'] for e in evidence['trace']['events'] if e.get('event_type')=='TOOL_CALL_COMPLETED' and e.get('tool_name')=='search_knowledge']
    hits=list({h['chunk_id']:h for retrieval in retrievals for h in retrieval.get('hits',[])}.values())
    record=dict(case_id=cid,run_id=run_id,question=case['question'],status=evidence['run']['status'],response=response,retrievals=retrievals,hits=hits,canonical_recall=canonical_recall(case,hits,mapping,sources),api_attempts=api.attempts,elapsed_seconds=round(time.monotonic()-started,3),evidence_path=str(path/'evidence.json'),source_mapping_path=str(RUN/'chunk_source_mapping.json'),business_state_equal=before['tables']==after['tables'])
    save(path/'execution.json',record)
    print('RUN',cid,record['status'],'hits',len(hits),'span',record['canonical_recall']['covered'],'/',record['canonical_recall']['total'],flush=True)
    return record


def execute(pilot=False):
    values,sources,index,mapping,config=freeze()
    # 已有模型缓存离线加载，禁止隐式下载不同版本的权重；模型和编码参数仍由原实现决定。
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    _=index.encoder
    selected=[c for c in values if not pilot or c['case_id'] in PILOT]
    if not pilot:
        assert (RUN/'pilot_validation.json').exists(),'正式执行必须先完成预检'
    results=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(run_one,c,index,mapping,sources,config) for c in selected]
        for f in as_completed(futures):results.append(f.result())
    assert protected()==json.loads((RUN/'protected_inputs.json').read_text(encoding='utf-8'))
    if pilot:
        assert all(r['status']=='COMPLETED_AWAITING_SCORING' and r['hits'] and r['business_state_equal'] and not r['api_attempts'] for r in results)
        save(RUN/'pilot_validation.json',dict(cases=PILOT,executions_usable=True,index_mapping_records=len(mapping),no_writes=True,reused_in_formal_run=True))
    save(RUN/'execution_progress.json',dict(unique_cases=len(list((RUN/'cases').glob('*/execution.json'))),protected_inputs_unchanged=True))


def main():
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['freeze','pilot','run']);a=p.parse_args()
    if a.phase=='freeze':
        values,_,index,mapping,_=freeze();print('FROZEN',len(values),'cases',len(index._chunks),'chunks',len(mapping),'exact source mappings')
    else:execute(a.phase=='pilot')


if __name__=='__main__':main()
