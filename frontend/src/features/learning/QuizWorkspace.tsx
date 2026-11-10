import {useCallback,useEffect,useRef,useState,useSyncExternalStore} from 'react'
import {useQuery,useQueryClient} from '@tanstack/react-query'
import {api} from '../../api'
import {authSession} from '../../session'
import {createUuid} from '../../uuid'
import {NoteError} from '../notes/NoteError'
import {ReviewJoin} from './ReviewJoin'
import {QuizEditor} from './QuizEditor'
import {QuizAttempt} from './QuizAttempt'
import {QuizGenerations} from './QuizAI'
import {useQuizMutation} from './QuizMutation'
import {attemptStatuses,statuses,type Quiz,type QuizList,type Attempt,type AttemptList} from './quizzes'
import './cards.css'
import './quizzes.css'
type Props={owner:string;initialQuizId?:string;initialAttemptId?:string;onOpenNote:(id:string)=>void;onGoReview?:(planId?:string)=>void}
export default function QuizWorkspace(props:Props){const session=useSyncExternalStore(authSession.subscribe,authSession.snapshot);if(session.phase!=='active'||session.owner!==props.owner)return null;return <Workspace key={props.owner+':'+session.id} {...props} sessionId={session.id}/>}
function Workspace({owner,sessionId,initialQuizId,initialAttemptId,onOpenNote,onGoReview}:Props&{sessionId:string}){
  const client=useQueryClient(),[status,setStatus]=useState(''),[offset,setOffset]=useState(0),[id,setId]=useState(initialQuizId||''),[attemptId,setAttemptId]=useState(initialAttemptId||''),[editor,setEditor]=useState<Quiz|'new'|null>(null),[generation,setGeneration]=useState(false),[attemptOffset,setAttemptOffset]=useState(0),[snapshot,setSnapshot]=useState<Quiz|null>(null),[error,setError]=useState<unknown>(null),[patchBusy,setPatchBusy]=useState(false),[ticket]=useState(()=>authSession.ticket()),viewEpoch=useRef(0),startEpoch=useRef(-1),currentView=useRef({id,editor,attemptId})
  const detailRef=useRef<HTMLElement>(null),attemptRef=useRef<HTMLDivElement>(null)
  currentView.current={id,editor,attemptId}
  const quizzes=useQuery({queryKey:['quizzes',owner,sessionId,status,offset],queryFn:()=>api<QuizList>('/learning/quizzes?limit=50&offset='+offset+(status?'&status='+status:'')),retry:false})
  const detail=useQuery({queryKey:['quiz',owner,sessionId,id],queryFn:()=>api<Quiz>('/learning/quizzes/'+id),enabled:!!id,retry:false,refetchOnWindowFocus:false,refetchOnMount:'always'})
  const latestAttempts=useQuery({queryKey:['quiz-attempts',owner,sessionId,id,0],queryFn:()=>api<AttemptList>('/learning/quiz-attempts?limit=50&offset=0&quiz_id='+id),enabled:!!id,retry:false})
  const olderAttempts=useQuery({queryKey:['quiz-attempts',owner,sessionId,id,attemptOffset],queryFn:()=>api<AttemptList>('/learning/quiz-attempts?limit=50&offset='+attemptOffset+'&quiz_id='+id),enabled:!!id&&attemptOffset>0,retry:false})
  const attempts=attemptOffset===0?latestAttempts:olderAttempts
  const resumable=latestAttempts.data?.items.find(attempt=>attempt.status==='in_progress')||attempts.data?.items.find(attempt=>attempt.status==='in_progress')
  const versionChanged=snapshot&&detail.data&&detail.data.revision>snapshot.revision
  const refresh=useCallback(()=>{void client.invalidateQueries({queryKey:['quizzes',owner,sessionId]});void client.invalidateQueries({queryKey:['quiz-attempts',owner,sessionId]})},[client,owner,sessionId])
  const start=useQuizMutation<Attempt>(owner,sessionId,'start',(data,_pending,retry)=>{const view=currentView.current;if(view.id===data.quiz_id&&!view.editor&&(startEpoch.current===viewEpoch.current||retry&&!view.attemptId))setAttemptId(data.id);refresh()})
  useEffect(()=>{if(detail.data?.id===id&&!detail.isFetching&&detail.isFetchedAfterMount&&!detail.isError&&!snapshot)setSnapshot(detail.data)},[detail.data,detail.isFetching,detail.isFetchedAfterMount,detail.isError,id,snapshot])
  useEffect(()=>{
    if(!id||currentView.current.attemptId)return
    detailRef.current?.focus({preventScroll:true})
    detailRef.current?.scrollIntoView({block:'start'})
  },[id,snapshot?.id])
  useEffect(()=>{
    if(!attemptId)return
    attemptRef.current?.focus({preventScroll:true})
    attemptRef.current?.scrollIntoView({block:'start'})
  },[attemptId])
  function choose(next:string){viewEpoch.current++;if(next===id)void detail.refetch();setId(next);setSnapshot(null);setEditor(null);setAttemptId('');setAttemptOffset(0);setError(null)}
  function openAttempt(next:string){viewEpoch.current++;setEditor(null);setAttemptId(next)}
  function adoptSnapshot(data:Quiz){setSnapshot(current=>current?.id===data.id&&current.revision>data.revision?current:data)}
  async function readLatest(){const quizId=id,epoch=viewEpoch.current;const result=await detail.refetch();if(start.active()&&viewEpoch.current===epoch&&currentView.current.id===quizId&&result.data?.id===quizId)adoptSnapshot(result.data)}
  async function patch(value:'confirmed'|'archived'|'draft'){if(!snapshot||patchBusy)return;const quizId=snapshot.id,epoch=viewEpoch.current;setPatchBusy(true);setError(null);const sameView=()=>authSession.current(ticket,owner)&&viewEpoch.current===epoch&&currentView.current.id===quizId;try{const data=await api<Quiz>('/learning/quizzes/'+quizId,'PATCH',{revision:snapshot.revision,status:value});if(authSession.current(ticket,owner)){if(sameView())adoptSnapshot(data);refresh()}}catch(e){if(sameView())setError(e)}finally{if(authSession.current(ticket,owner))setPatchBusy(false)}}
  const readVersion=<button type="button" className="secondary" disabled={detail.isFetching} onClick={()=>void readLatest()}>读取最新测验版本</button>
  const confirmQuiz=<button type="button" className={resumable||snapshot?.status==='archived'?'secondary':'primary'} disabled={patchBusy||!snapshot?.question_count} onClick={()=>void patch('confirmed')}>人工审核后确认测验</button>
  return <div className="quiz-workspace">
    <section className="panel">
      <div className="panel-heading">
        <h3>我的测验</h3>
        <div className="card-actions">
          <button className="secondary" type="button" onClick={()=>setGeneration(v=>!v)}>{generation?'收起 AI 草稿':'AI 生成测验'}</button>
          <button className={id?'secondary':'primary'} type="button" onClick={()=>{viewEpoch.current++;setId('');setSnapshot(null);setEditor('new');setAttemptId('')}}>手工新建测验</button>
        </div>
      </div>
      <div className="quiz-list-tools">
        <label>测验状态<select value={status} onChange={e=>{setStatus(e.target.value);setOffset(0)}}><option value="">全部状态</option>{Object.entries(statuses).map(([k,n])=><option key={k} value={k}>{n}</option>)}</select></label>
        <button type="button" className="text-button" onClick={()=>void quizzes.refetch()}>刷新测验列表</button>
      </div>
      <NoteError error={quizzes.error}/>
      {quizzes.isPending?<p role="status">正在读取测验…</p>:quizzes.data?.items.length?<ul className="quiz-list">
        {quizzes.data.items.map(q=><li key={q.id}><button className={'quiz-list-open '+(q.id===id?'selected':'')} type="button" aria-pressed={q.id===id} onClick={()=>choose(q.id)}><strong>{q.title}</strong><span>{statuses[q.status]} · {q.question_count} 题 · 版本 {q.revision}</span></button></li>)}
      </ul>:<p className="empty">还没有符合条件的测验，可以手工创建或生成 AI 草稿。</p>}
      <div className="card-pagination">
        <span>共 {quizzes.data?.total||0} 份</span>
        {offset>0||(quizzes.data?.total||0)>50?<div>
          <button type="button" className="secondary" disabled={!offset||quizzes.isFetching} onClick={()=>setOffset(v=>v-50)}>上一页测验</button>
          <button type="button" className="secondary" disabled={offset+50>=(quizzes.data?.total||0)||quizzes.isFetching} onClick={()=>setOffset(v=>v+50)}>下一页测验</button>
        </div>:null}
      </div>
    </section>
    {start.feedback}{start.pending?<button className="secondary" type="button" disabled={start.busy} onClick={()=>void start.reconcile(async()=>{const quizId=start.pending!.path.split('/')[3];const list=await api<AttemptList>('/learning/quiz-attempts?quiz_id='+quizId+'&limit=50&offset=0');if(!start.active())return;if(!list.items.length)throw new Error('未找到作答记录，可明确重试原编号与原参数。');refresh()} )}>核对原开始请求（只读）</button>:null}
    {id?<section ref={detailRef} tabIndex={-1} className="panel quiz-detail" aria-label="当前测验">
      <NoteError error={detail.error||error}/>
      {detail.isError&&!snapshot?<button type="button" className="secondary" disabled={detail.isFetching} onClick={()=>void detail.refetch()}>重试读取测验</button>:null}
      {detail.isPending?<p role="status">正在读取测验…</p>:snapshot?<>
        <div className="panel-heading">
          <h3>{snapshot.title}</h3>
          <span className="muted">{statuses[snapshot.status]} · {snapshot.question_count} 题 · 版本 {snapshot.revision}</span>
        </div>
        {versionChanged?<div className="quiz-warning"><p role="status">服务器存在更新版本，当前原版本仍保留。</p>{readVersion}</div>:null}
        <NoteError error={latestAttempts.error||attempts.error}/>
        <div className="card-actions quiz-detail-actions">
          {resumable?<button type="button" className="primary" onClick={()=>openAttempt(resumable.id)}>继续作答</button>:null}
          {snapshot.status==='confirmed'?<button type="button" className={resumable?'secondary':'primary'} disabled={start.blocked||latestAttempts.isPending} onClick={()=>{
            startEpoch.current=viewEpoch.current
            void start.send({path:'/learning/quizzes/'+snapshot.id+'/attempts',method:'POST',body:{revision:snapshot.revision,request_key:createUuid()}})
          }}>开始新的作答</button>:null}
          {snapshot.status==='draft'?confirmQuiz:null}
          {snapshot.status==='archived'?<button type="button" className={resumable?'secondary':'primary'} disabled={patchBusy} onClick={()=>void patch('draft')}>恢复为草稿</button>:null}
        </div>
        {snapshot.status==='confirmed'?<ReviewJoin owner={owner} kind="quiz" id={snapshot.id} onGoReview={onGoReview}/>:<p className="muted">请先审核并确认题目，才可开始作答或加入复习。</p>}
        <details className="quiz-disclosure" key={'history:'+id}>
          <summary>作答记录{attempts.data?`（${attempts.data.total} 次）`:''}</summary>
          <button className="text-button" type="button" onClick={()=>{void attempts.refetch();if(attemptOffset>0)void latestAttempts.refetch()}}>刷新作答记录</button>
          {attempts.isPending?<p role="status">正在读取作答记录…</p>:attempts.data?.items.length?attempts.data.items.map(attempt=><div className="quiz-record" key={attempt.id}>
            <span>{attemptStatuses[attempt.status]} · 快照版本 {attempt.quiz_revision} · {attempt.status==='in_progress'?'未交卷':attempt.total_points+' / '+attempt.max_points+' 分'} · {attempt.created_at}</span>
            <button className="secondary" type="button" onClick={()=>openAttempt(attempt.id)}>{attempt.status==='in_progress'?'继续作答':'查看评分与复核'}</button>
          </div>):attempts.data?<p className="muted">还没有作答记录。</p>:null}
          {attemptOffset>0||(attempts.data?.total||0)>50?<div className="card-pagination">
            <span>共 {attempts.data?.total||0} 次</span>
            <div><button className="text-button" type="button" disabled={!attemptOffset||attempts.isFetching} onClick={()=>setAttemptOffset(v=>v-50)}>上一页作答</button><button className="text-button" type="button" disabled={attemptOffset+50>=(attempts.data?.total||0)||attempts.isFetching} onClick={()=>setAttemptOffset(v=>v+50)}>下一页作答</button></div>
          </div>:null}
        </details>
        <details className="quiz-disclosure" key={'management:'+id}>
          <summary>更多测验操作</summary>
          <div className="card-actions">
            {!versionChanged?readVersion:null}
            <button type="button" className="secondary" disabled={patchBusy} onClick={()=>{viewEpoch.current++;setEditor(snapshot);setAttemptId('')}}>编辑题目与答案</button>
            {snapshot.status==='archived'?confirmQuiz:<button type="button" className="text-button" disabled={patchBusy} onClick={()=>{if(window.confirm('归档测验？既有作答和复核历史保留。'))void patch('archived')}}>归档测验</button>}
          </div>
        </details>
      </>:null}
    </section>:null}
    {editor?<QuizEditor key={editor==='new'?'new':editor.id+':'+editor.revision} owner={owner} sessionId={sessionId} quiz={editor==='new'?undefined:editor} onOpenNote={onOpenNote} onClose={()=>setEditor(null)} onSaved={q=>{choose(q.id);setSnapshot(q);refresh()}}/>:null}
    {attemptId?<div ref={attemptRef} tabIndex={-1} className="quiz-attempt-target"><QuizAttempt key={attemptId} owner={owner} sessionId={sessionId} id={attemptId} onOpenNote={onOpenNote} onClose={()=>{setAttemptId('');refresh();detailRef.current?.focus()}}/></div>:null}
    {generation?<QuizGenerations owner={owner} sessionId={sessionId} onOpenQuiz={choose} onDrafts={refresh}/>:null}
  </div>
}
