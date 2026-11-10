import {useCallback,useEffect,useRef,useState,useSyncExternalStore} from 'react'
import {useQuery} from '@tanstack/react-query'
import {api} from '../../api'
import {authSession} from '../../session'
import {createUuid} from '../../uuid'
import {NoteError} from '../notes/NoteError'
import type {Note} from '../notes/types'
import {CardPreview,CardSource} from './CardPreview'
import {sourceVisual,type Card,type SourceRead} from './cards'
import {ReviewPlanEditor} from './ReviewPlanEditor'
import {ReviewPractice} from './ReviewPractice'
import {useReviewMutation} from './ReviewMutation'
import {actions,deviceZone,ratings,reviewKinds,reviewListPath,reviewReason,reviewStatuses,reviewTime,snapshotDescription,type ReviewFilters,type ReviewLogs,type ReviewPlan,type ReviewPlans} from './reviews'
import './cards.css'
import './reviews.css'
import {useLearningActivity} from './ActivityTracker'
import {useLearningSettings} from './LearningPreferences'
type Props={owner:string;onOpenNote:(id:string)=>void;onOpenQuiz?:(id:string)=>void;initialPlanId?:string;timeZone?:string}
export default function ReviewCenter(props:Props){const session=useSyncExternalStore(authSession.subscribe,authSession.snapshot);if(session.phase!=='active'||session.owner!==props.owner)return null;return <Center key={props.owner+':'+session.id} {...props} sessionId={session.id}/>}
function Center({owner,sessionId,onOpenNote,onOpenQuiz,initialPlanId,timeZone:explicitTimeZone}:Props&{sessionId:string}){
  const settings=useLearningSettings(owner,sessionId),timeZone=explicitTimeZone||settings.data?.timezone||'Asia/Shanghai'
  const [filters,setFilters]=useState<ReviewFilters>({mode:'due',kind:'',status:'',eligible:false,from:'',to:'',offset:0}),[selected,setSelected]=useState(initialPlanId||''),[snapshot,setSnapshot]=useState<ReviewPlan|null>(null),[formEpoch,setFormEpoch]=useState(0)
  const detailRef=useRef<HTMLElement>(null),selectedPlanButton=useRef<HTMLButtonElement>(null)
  const ready=!!(explicitTimeZone||settings.data)
  const selection=useRef(selected);selection.current=selected
  const mutation=useReviewMutation(owner,sessionId,receipt=>{if(receipt.plan?.id===selection.current){setSnapshot(receipt.plan);setFormEpoch(value=>value+1)}})
  let path='',filterError:unknown;try{path=reviewListPath(filters)}catch(e){filterError=e}
  const plans=useQuery({queryKey:['reviews',owner,sessionId,'list',path],queryFn:()=>api<ReviewPlans>(path),enabled:!!path,retry:false})
  const current=useQuery({queryKey:['reviews',owner,sessionId,'plan',selected],queryFn:()=>api<ReviewPlan>('/learning/review-plans/'+selected),enabled:!!selected,retry:false})
  // Freeze the editable version. Background reads must never silently rebase unsaved input.
  useEffect(()=>{if(current.data?.id===selected&&!snapshot)setSnapshot(current.data)},[current.data,selected,snapshot])
  useEffect(()=>{
    if(!selected||!ready)return
    detailRef.current?.focus({preventScroll:true})
    detailRef.current?.scrollIntoView({block:'start'})
  },[selected,ready,snapshot?.id])
  const readablePlan=snapshot&&current.data?.id===snapshot.id?{...snapshot,item:{...snapshot.item,eligible:current.data.item.eligible}}:snapshot
  function choose(id:string){setSnapshot(null);setSelected(id);setFormEpoch(value=>value+1)}
  function filter(change:Partial<ReviewFilters>){setFilters(old=>({...old,...change,offset:change.offset??0}));choose('')}
  function reload(){if(current.data){setSnapshot(current.data);setFormEpoch(value=>value+1)}}
  function changePlanStatus(operation:'pause'|'resume'|'archive'){
    if(!snapshot)return
    void mutation.send({path:'/learning/review-plans/'+snapshot.id,method:'PATCH',planId:snapshot.id,body:{operation,revision:snapshot.revision,request_key:createUuid()}})
  }
  if(!ready)return <section className="panel"><h3>复习计划</h3><NoteError error={settings.error}/>{settings.isPending?<p>正在读取用户显示时区…</p>:<button type="button" className="secondary" onClick={()=>void settings.refetch()}>重试读取时区</button>}</section>

  const activeFilterCount=[filters.kind,filters.from,filters.to,filters.mode==='all'&&filters.status,filters.mode==='all'&&filters.eligible].filter(Boolean).length
  const versionChanged=snapshot&&current.data&&current.data.revision!==snapshot.revision
  const readVersion=<button type="button" className="text-button" disabled={current.isFetching} onClick={()=>void current.refetch()}>读取当前计划（保留输入）</button>
  const adoptVersion=<button type="button" className="secondary" disabled={mutation.blocked||current.isFetching||!current.data} onClick={reload}>采用已读取版本并重置输入</button>
  const versionActions=<div className="card-actions review-version-actions">
    {readVersion}{adoptVersion}
  </div>
  const feedback=<section className="review-feedback" aria-label="复习操作结果">{mutation.feedback}</section>
  return <div className="review-center card-workspace">
    <section className="panel">
      <div className="panel-heading">
        <h3>{filters.mode==='due'?'待复习':'复习计划'}</h3>
        <div className="card-actions" role="group" aria-label="计划范围">
          <button type="button" className={filters.mode==='due'?'primary':'secondary'} aria-pressed={filters.mode==='due'} onClick={()=>{if(filters.mode!=='due')filter({mode:'due'})}}>当前到期</button>
          <button type="button" className={filters.mode==='all'?'primary':'secondary'} aria-pressed={filters.mode==='all'} onClick={()=>{if(filters.mode!=='all')filter({mode:'all'})}}>全部计划</button>
        </div>
      </div>
      <details className="review-filters">
        <summary>筛选{activeFilterCount?`（${activeFilterCount} 项已选）`:''}</summary>
        <div className="card-filters">
          <label>复习对象类型<select value={filters.kind} onChange={e=>filter({kind:e.target.value})}><option value="">全部类型</option>{Object.entries(reviewKinds).map(([v,n])=><option value={v} key={v}>{n}</option>)}</select></label>
          {filters.mode==='all'?<>
            <label>计划状态<select value={filters.status} onChange={e=>filter({status:e.target.value})}><option value="">全部状态</option>{Object.entries(reviewStatuses).map(([v,n])=><option value={v} key={v}>{n}</option>)}</select></label>
            <label className="card-check"><input type="checkbox" checked={filters.eligible} onChange={e=>filter({eligible:e.target.checked})}/>仅可读取的有效对象</label>
          </>:null}
          <label>到期开始（设备时区：{deviceZone()}）<input type="datetime-local" value={filters.from} onChange={e=>filter({from:e.target.value})}/></label>
          <label>到期结束（设备时区：{deviceZone()}）<input type="datetime-local" value={filters.to} onChange={e=>filter({to:e.target.value})}/></label>
        </div>
      </details>
      <NoteError error={filterError||plans.error}/>
      {plans.isPending&&path?<p role="status">正在读取计划…</p>:plans.data&&path?<>
        {plans.data.items.length?<div className="review-list">{plans.data.items.map(plan=>
          <button type="button" ref={selected===plan.id?selectedPlanButton:undefined} className={'review-list-item '+(selected===plan.id?'selected':'')} aria-pressed={selected===plan.id} key={plan.id} onClick={()=>choose(plan.id)}>
            <strong>{plan.item.title}</strong>
            <span>{reviewKinds[plan.item.kind]} · {reviewStatuses[plan.status]} · {plan.strategy==='manual'?'手动排期':'FSRS 自动排期'}</span>
            <small>下次：{reviewTime(plan.next_due_at,timeZone)}{!plan.item.eligible?' · 当前对象不可复习':''}</small>
          </button>
        )}</div>:<p className="empty">{activeFilterCount?'当前筛选没有复习计划。':filters.mode==='due'?'当前没有到期的复习计划。':'还没有复习计划。可从笔记加入复习，或确认学习卡片。'}</p>}
        <div className="card-pagination">
          <span>共 {plans.data.total} 个计划 · {timeZone}</span>
          {plans.data.total>50||filters.offset>0?<div>
            <button type="button" className="secondary" disabled={!filters.offset||plans.isFetching} onClick={()=>filter({offset:Math.max(0,filters.offset-50)})}>上一页计划</button>
            <button type="button" className="secondary" disabled={filters.offset+50>=plans.data.total||plans.isFetching} onClick={()=>filter({offset:filters.offset+50})}>下一页计划</button>
          </div>:null}
        </div>
      </>:null}
    </section>
    {selected?<section ref={detailRef} tabIndex={-1} className="panel review-current" aria-label="当前复习计划">
      <div className="panel-heading"><h3>{snapshot?.item.title||'当前复习计划'}</h3><button type="button" className="text-button" onClick={()=>{selectedPlanButton.current?.focus();choose('')}}>关闭计划</button></div>
      <NoteError error={current.error}/>
      {current.isError?readVersion:null}
      {feedback}
      {current.isFetching?<p role="status">正在核对当前计划…</p>:null}
      {snapshot&&!current.isError?<>
        <p className="muted">{reviewKinds[snapshot.item.kind]} · {reviewStatuses[snapshot.status]} · {snapshot.strategy==='manual'?'手动排期':'FSRS 自动排期'} · 下次 {reviewTime(snapshot.next_due_at,timeZone)}</p>
        {!readablePlan?.item.eligible?<p className="error">对象当前不可复习，可能已移入回收站、归档或尚未确认。</p>:null}
        {versionChanged?<div className="review-uncertain"><p role="status">当前计划已有较新版本，已保留你的输入和原版本。读取后再选择采用新版本。</p>{versionActions}</div>:null}
        <ReviewObject key={snapshot.item.id} plan={readablePlan||snapshot} owner={owner} sessionId={sessionId} onOpenNote={onOpenNote} onOpenQuiz={onOpenQuiz}/>
        <ReviewPractice key={'rating:'+formEpoch} plan={readablePlan||snapshot} blocked={mutation.blocked||current.isFetching} onSubmit={attempt=>void mutation.send(attempt)}/>
        {snapshot.status==='paused'?<div className="review-status-actions"><button type="button" className="secondary" disabled={mutation.blocked} onClick={()=>changePlanStatus('resume')}>恢复计划</button></div>:null}
        {!versionChanged?<div className="review-version-actions">{readVersion}</div>:null}
        <ReviewPlanEditor key={'edit:'+formEpoch} plan={readablePlan||snapshot} blocked={mutation.blocked||current.isFetching} onSubmit={attempt=>void mutation.send(attempt)}/>
        {!versionChanged||snapshot.status!=='archived'?<details className="review-disclosure" key={'management:'+snapshot.id}>
          <summary>版本与计划状态</summary>
          {!versionChanged?<div className="review-version-actions">{adoptVersion}</div>:null}
          {snapshot.status!=='archived'?<div className="card-actions review-status-actions">
            {snapshot.status==='active'?<button type="button" className="secondary" disabled={mutation.blocked} onClick={()=>changePlanStatus('pause')}>暂停计划</button>:null}
            <button type="button" className="text-button" disabled={mutation.blocked} onClick={()=>{if(window.confirm('归档此计划？历史保留；后续可从原对象重新加入复习。'))changePlanStatus('archive')}}>归档计划</button>
          </div>:null}
        </details>:null}
        <details className="review-disclosure" key={'history:'+snapshot.id}>
          <summary>复习与策略变更历史</summary>
          <ReviewHistory owner={owner} sessionId={sessionId} plan={snapshot} timeZone={timeZone}/>
        </details>
      </>:null}
    </section>:feedback}
  </div>
}
function ReviewObject({plan,owner,sessionId,onOpenNote,onOpenQuiz}:{plan:ReviewPlan;owner:string;sessionId:string;onOpenNote:(id:string)=>void;onOpenQuiz?:Props['onOpenQuiz']}){
  if(!plan.item.eligible)return null
  if(plan.item.kind==='note'&&plan.item.note_id)return <ReviewNote key={plan.item.note_id} id={plan.item.note_id} owner={owner} sessionId={sessionId} onOpen={onOpenNote}/>
  if(plan.item.kind==='card'&&plan.item.card_id)return <ReviewCard key={plan.item.card_id} id={plan.item.card_id} owner={owner} sessionId={sessionId} onOpenNote={onOpenNote}/>
  return <section className="review-object"><h4>测验 · {plan.item.title}</h4><p className="muted">先独立完成测验，再根据自己的回忆感受选择评价。测验结果与复习排期分别保存。</p>{onOpenQuiz&&plan.item.quiz_id?<button type="button" className="secondary" onClick={()=>onOpenQuiz(plan.item.quiz_id!)}>打开测验作答</button>:null}</section>
}
function ReviewNote({id,owner,sessionId,onOpen}:{id:string;owner:string;sessionId:string;onOpen:(id:string)=>void}){const note=useQuery({queryKey:['reviews',owner,sessionId,'note',id],queryFn:()=>api<Note>('/notes/'+id),retry:false,gcTime:0});useLearningActivity('review',note.data&&!note.data.deleted_at&&!note.isError?{note_id:id}:null);return <section className="review-object" aria-label="复习笔记正文"><NoteError error={note.error}/>{note.isPending?<p>正在读取当前笔记…</p>:note.data&&!note.isError?<><h4>{note.data.title}</h4><p className="review-note-body">{note.data.plain_text||'该笔记没有文字正文，请打开原笔记查看图片或 PDF。'}</p><button type="button" className="secondary" onClick={()=>onOpen(id)}>打开原笔记</button></>:null}</section>}
function ReviewCard({id,owner,sessionId,onOpenNote}:{id:string;owner:string;sessionId:string;onOpenNote:(id:string)=>void}){const card=useQuery({queryKey:['reviews',owner,sessionId,'card',id],queryFn:()=>api<Card>('/learning/cards/'+id),retry:false,gcTime:0}),[read,setRead]=useState<SourceRead|null>(null);const onRead=useCallback((value:SourceRead|null)=>setRead(value),[]);useLearningActivity('review',card.data?.status==='confirmed'&&!card.isError?{card_id:id}:null);return <section className="review-object" aria-label="复习卡片内容"><NoteError error={card.error}/>{card.isPending?<p>正在读取卡片…</p>:card.data&&!card.isError?<><CardPreview kind={card.data.kind} front={card.data.front} back={card.data.back} masks={card.data.payload?.masks} visual={read?sourceVisual(read):null}/><CardSource owner={owner} sessionId={sessionId} card={card.data} onOpenNote={onOpenNote} onRead={onRead}/></>:null}</section>}
function ReviewHistory({owner,sessionId,plan,timeZone}:{owner:string;sessionId:string;plan:ReviewPlan;timeZone:string}){const [offset,setOffset]=useState(0),[action,setAction]=useState('');const query=useQuery({queryKey:['reviews',owner,sessionId,'logs',plan.id,offset,action],queryFn:()=>api<ReviewLogs>('/learning/review-logs?'+new URLSearchParams({plan_id:plan.id,offset:String(offset),limit:'50',...(action?{action}:{})})),retry:false});return <section className="review-history"><div className="panel-heading"><h4>复习与策略变更历史</h4><label>历史操作<select aria-label="历史操作" value={action} onChange={e=>{setAction(e.target.value);setOffset(0)}}><option value="">全部操作</option>{Object.entries(actions).map(([v,n])=><option key={v} value={v}>{n}</option>)}</select></label></div><NoteError error={query.error}/>{query.isPending?<p>正在读取历史…</p>:query.data?.items.length?<ol>{query.data.items.map(log=><li key={log.id}><strong>{actions[log.action]||'计划操作'}{log.rating?' · '+ratings[log.rating-1]:''}</strong><small>{reviewTime(log.reviewed_at,timeZone)} · {reviewKinds[plan.item.kind]}：{plan.item.title}</small><p>原排期：{snapshotDescription(log.old_snapshot,timeZone)}</p><p>新排期：{snapshotDescription(log.new_snapshot,timeZone)}</p>{log.reason?<p>原因：{reviewReason(log.reason)}</p>:null}{log.action==='practice'?<p>仅记录练习，排期保持不变。</p>:null}{log.action==='switch'?<p>策略变更与历史算法状态已保留。</p>:null}</li>)}</ol>:<p className="muted">暂无符合筛选的历史。</p>}<div className="card-pagination"><span>共 {query.data?.total||0} 条</span><div><button type="button" className="text-button" disabled={!offset||query.isFetching} onClick={()=>setOffset(v=>Math.max(0,v-50))}>上一页历史</button><button type="button" className="text-button" disabled={offset+50>=(query.data?.total||0)||query.isFetching} onClick={()=>setOffset(v=>v+50)}>下一页历史</button></div></div></section>}
