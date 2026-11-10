import {useId,useRef,useState,useSyncExternalStore} from 'react'
import {useQuery} from '@tanstack/react-query'
import {CalendarDays} from 'lucide-react'
import {api,ApiError} from '../../api'
import {authSession} from '../../session'
import {NoteError} from '../notes/NoteError'
import {useLearningSettings} from './LearningPreferences'
import {dayShift,duration,localDay,rangeQuery,readingCoverage,type Stats,type Calendar} from './learning'
import './learning.css'
export default function LearningOverview({owner}:{owner:string}){const session=useSyncExternalStore(authSession.subscribe,authSession.snapshot);return session.phase==='active'&&session.owner===owner?<SettingsScope key={owner+session.id} owner={owner} sessionId={session.id}/>:null}
function SettingsScope({owner,sessionId}:{owner:string;sessionId:string}){const settings=useLearningSettings(owner,sessionId);return settings.data?<Overview key={settings.data.timezone} owner={owner} sessionId={sessionId} timeZone={settings.data.timezone}/>:<section className="panel"><h3>学习统计与日历</h3><NoteError error={settings.error}/>{settings.isPending?<p>正在读取显示时区…</p>:<button type="button" className="secondary" onClick={()=>void settings.refetch()}>重试读取时区</button>}</section>}
function Overview({owner,sessionId,timeZone}:{owner:string;sessionId:string;timeZone:string}){
  const today=localDay(timeZone)
  const recent={from:dayShift(today,-29),to:today},upcoming={from:today,to:dayShift(today,29)}
  const [draft,setDraft]=useState(recent),[range,setRange]=useState(draft),[kind,setKind]=useState(''),[error,setError]=useState<unknown>(null),[offset,setOffset]=useState(0)
  const [rangeOpen,setRangeOpen]=useState(false),rangeId=useId(),rangeButton=useRef<HTMLButtonElement>(null)
  const params=rangeQuery(range.from,range.to).toString(),stats=useQuery({queryKey:['learning-stats',owner,sessionId,'stats',timeZone,params],queryFn:()=>api<Stats>('/learning/stats?'+params),retry:false}),calendar=useQuery({queryKey:['learning-stats',owner,sessionId,'calendar',timeZone,params,kind],queryFn:()=>api<Calendar>('/learning/calendar?'+params+(kind?'&kind='+kind:'')),retry:false})
  const days=stats.data?.days||[],sum=(key:'total_seconds'|'reading_seconds'|'review_seconds'|'practice_seconds'|'quiz_seconds'|'formal_reviews'|'practice_count')=>days.reduce((n,d)=>n+d[key],0),quizSum=(key:keyof Stats['days'][number]['quiz'])=>days.reduce((n,d)=>n+d.quiz[key],0)
  const progress=readingCoverage(stats.data?.reading_progress),queryError=stats.error||calendar.error,scanError=queryError instanceof ApiError&&queryError.status===422
  const rangeLabel=range.from===recent.from&&range.to===recent.to?'最近 30 天':range.from===upcoming.from&&range.to===upcoming.to?'未来 30 天':'自定义范围'
  function closeRange(){setRangeOpen(false);rangeButton.current?.focus({preventScroll:true})}
  function applyRange(next:typeof draft){
    try{rangeQuery(next.from,next.to);setDraft(next);setRange(next);setOffset(0);setError(null);closeRange()}
    catch(err){setError(err)}
  }
  return <div className="learning-workspace learning-overview">
    <section className="panel learning-summary" aria-label="统计概览">
      <div className="panel-heading">
        <div><h3>统计概览</h3><p className="muted learning-summary-range">{rangeLabel} · {range.from} 至 {range.to}（{timeZone}）</p></div>
        <button ref={rangeButton} type="button" className="secondary learning-range-toggle" aria-expanded={rangeOpen} aria-controls={rangeId} onClick={()=>setRangeOpen(open=>!open)}><CalendarDays size={16} aria-hidden="true"/>日期范围</button>
      </div>
      <div id={rangeId} className="learning-range-panel" role="group" aria-label="设置日期范围" hidden={!rangeOpen} onKeyDown={event=>{if(event.key==='Escape'){event.preventDefault();closeRange()}}}>
        <div className="learning-actions">
          <button type="button" className="secondary" onClick={()=>applyRange(recent)}>最近 30 天</button>
          <button type="button" className="text-button" onClick={()=>applyRange(upcoming)}>查看未来 30 天到期</button>
        </div>
        <form className="learning-form learning-range" onSubmit={e=>{e.preventDefault();applyRange(draft)}}>
          <label>当地开始日期<input type="date" value={draft.from} onChange={e=>setDraft({...draft,from:e.target.value})} required/></label>
          <label>当地结束日期<input type="date" value={draft.to} onChange={e=>setDraft({...draft,to:e.target.value})} required/></label>
          <button className="secondary">查询日期范围</button>
        </form>
        <p className="muted">每次含首尾最多 93 天。</p>
        <NoteError error={error}/>
      </div>
      {stats.isPending?<p role="status">正在读取统计概览…</p>:stats.data?<>
        <div className="learning-metrics"><Metric label="有效总时长" value={duration(sum('total_seconds'))}/><Metric label="正式复习次数" value={sum('formal_reviews')}/><Metric label="测验交卷" value={quizSum('submitted')}/></div>
        <p className="muted">活动时长记录真实可见且近期有操作的学习片段；阅读时长不代表掌握程度。</p>
      </>:null}
      <NoteError error={queryError}/>
      {scanError?<p className="review-uncertain">范围内记录过多或参数超出限制，请缩短日期范围再查询。数据不会被静默截断。</p>:null}
      {queryError?<button type="button" className="secondary" onClick={()=>{void stats.refetch();void calendar.refetch()}}>重试读取统计和日历</button>:null}
    </section>
    <section className="panel" aria-label="本期阅读覆盖"><h3>本期阅读覆盖</h3>{stats.isPending?<p role="status">正在读取阅读覆盖…</p>:progress?<><div className="learning-metrics"><Metric label="当前可读笔记覆盖" value={progress.coverage}/><Metric label="本期有阅读记录的笔记" value={progress.recorded}/></div><p className="muted">覆盖的分子、分母只包含当前未回收且可读的笔记；本期阅读记录按笔记去重，包含之后被回收的历史记录。同一笔记跨天、跨设备只算一次。</p></>:<p className="muted">未提供阅读进度。请重新读取统计；缺失数据不会当作零。</p>}<p className="muted">有有效阅读活动不代表已读完或掌握。仅复习活动不计入阅读覆盖；没有阅读记录也不代表学习未完成。</p></section>
    <section className="panel" aria-label="活动与学习事实"><h3>活动与学习事实</h3>{stats.isPending?<p role="status">正在读取统计…</p>:stats.data?<><div className="learning-metrics"><Metric label="有效总时长" value={duration(sum('total_seconds'))}/><Metric label="阅读" value={duration(sum('reading_seconds'))}/><Metric label="复习活动" value={duration(sum('review_seconds'))}/><Metric label="卡片练习活动" value={duration(sum('practice_seconds'))}/><Metric label="测验活动" value={duration(sum('quiz_seconds'))}/></div><p className="muted">总时长已合并同账号多设备或多页面重叠区间；各分类独立合并、可能重叠，不能相加得到总时长。</p><div className="learning-metrics"><Metric label="正式复习次数" value={sum('formal_reviews')}/><Metric label="临时练习次数" value={sum('practice_count')}/>{(['again','hard','good','easy'] as const).map((key,i)=><Metric key={key} label={['Again（重来）','Hard（困难）','Good（良好）','Easy（轻松）'][i]} value={days.reduce((n,d)=>n+d.ratings[key],0)}/>)}</div><h4>测验结果</h4><div className="learning-metrics">{(['submitted','graded','needs_review','actual_points','max_points','manually_corrected'] as const).map((key,i)=><Metric key={key} label={['交卷总数','已判分作答','待复核作答','实际得分','快照满分','人工修正答案数'][i]} value={quizSum(key)}/>)}</div><p className="muted">测验分数与主动回忆评价分开记录。待复核可能尚未获得最终分数；人工修正后可重新读取统计。</p>{!days.some(d=>d.total_seconds||d.formal_reviews||d.practice_count||d.quiz.submitted)?<p className="empty">这个日期范围暂无学习事实，可调整日期或开始阅读、练习和作答。</p>:null}<details><summary>逐日明细与每天去重对象数</summary><div className="learning-day-list">{days.slice(offset,offset+31).map(day=><article key={day.date}><strong>{day.date}（{timeZone}）</strong><p>有效 {duration(day.total_seconds)} · 阅读 {duration(day.reading_seconds)} · 复习 {duration(day.review_seconds)} · 练习 {duration(day.practice_seconds)} · 测验 {duration(day.quiz_seconds)}</p><p>{typeof day.reading_notes==='number'?`当天有阅读记录的笔记 ${day.reading_notes} 篇（当天去重）`:'未提供当天阅读进度'}</p><p>正式复习 {day.formal_reviews} 次 · 当天去重复习对象 {day.distinct_review_objects} 个 · 临时练习 {day.practice_count} 次</p><p>重来 {day.ratings.again} · 困难 {day.ratings.hard} · 良好 {day.ratings.good} · 轻松 {day.ratings.easy}</p><p>测验交卷 {day.quiz.submitted} · 已判分 {day.quiz.graded} · 待复核 {day.quiz.needs_review} · 得分 {day.quiz.actual_points}/{day.quiz.max_points} · 人工修正 {day.quiz.manually_corrected}</p></article>)}</div><div className="learning-pagination"><span>每天阅读笔记和复习对象分别去重，跨天不相加为期间唯一总数。</span><div><button type="button" className="secondary" disabled={!offset} onClick={()=>setOffset(v=>Math.max(0,v-31))}>上一页日期</button><button type="button" className="secondary" disabled={offset+31>=days.length} onClick={()=>setOffset(v=>v+31)}>下一页日期</button></div></div></details></>:null}</section>
    <section className="panel"><div className="panel-heading"><h3>当地学习日历</h3><label>日历对象类型<select value={kind} onChange={e=>setKind(e.target.value)}><option value="">全部类型</option><option value="note">笔记</option><option value="card">卡片</option><option value="quiz">测验</option></select></label></div><p className="muted">日期均为 {calendar.data?.timezone||timeZone} 当地日期。历史完成与临时练习分别显示；未来到期仅计当前有效计划。当天到期包含今天尚未到时的计划。</p>{calendar.isPending?<p role="status">正在读取日历…</p>:calendar.data?<div className="learning-calendar">{calendar.data.days.map(day=><article key={day.date} className={day.date===today?'today':''}><h4>{day.date}{day.date===today?' · 今天':''}</h4><p>正式完成：{counts(day.completed)}</p><p>临时练习：{counts(day.practice)}</p><p>测验交卷 {day.quiz.submitted} · 待复核 {day.quiz.needs_review}</p><p>未来到期：{counts(day.due)}</p></article>)}</div>:null}</section></div>
}
function counts(value:{note:number;card:number;quiz:number}){return `笔记 ${value.note} · 卡片 ${value.card} · 测验 ${value.quiz}`}
function Metric({label,value}:{label:string;value:string|number}){return <div className="learning-metric"><span>{label}</span><strong>{value}</strong></div>}
