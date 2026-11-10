import {useNotificationFeed} from './features/notifications/useNotificationFeed'
import {NotificationHistory} from './features/notifications/NotificationHistory'
import {useChatRealtime} from './features/social/useChatRealtime'
import {cancelPushForLogout} from './features/pwa/push'
import { lazy, Suspense, useEffect, useState, useSyncExternalStore, useCallback, useRef, type FormEvent, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { BookOpen, ArrowUpRight, LayoutDashboard, Ticket, Users, ShieldCheck, Activity, LogOut, Bell, RefreshCw, Check, ChevronRight, KeyRound, Copy, MessageSquare, Settings as SettingsIcon, Menu, X, Layers, ClipboardList, RotateCcw, BarChart3, Download, Compass } from 'lucide-react'
import { api, authenticate, logoutTab, date, ApiError, type User, type Invite, type CreatedInvite, type Job, type Audit, type LoginInput, type RegisterInput, type ResetInput } from './api'

import { clearTabNavigation,rememberSection,rememberNote,wasNotesOpen,wasChatOpen } from './features/notes/drafts'
import { authSession,clearSession,readSession,startSessionSync } from './session.ts'
import { createUuid } from './uuid'
import {LearningActivityProvider} from './features/learning/ActivityTracker'
import {usePersonalization} from './features/personalization/usePersonalization'
import {homeSection,shouldApplyHomeDefault} from './features/personalization/personalization'
import {createStartupDefaults} from './features/personalization/startup-default'
import type { AskTextSelection } from './features/notes/text-selection'
const NotesWorkspace=lazy(()=>import('./features/notes/NotesWorkspace'))
const AiChat=lazy(()=>import('./features/ai/AiChat'))
const AiSettings=lazy(()=>import('./features/ai/AiSettings'))
const CardWorkspace=lazy(()=>import('./features/learning/CardWorkspace'))
const QuizWorkspace=lazy(()=>import('./features/learning/QuizWorkspace'))
const ReviewCenter=lazy(()=>import('./features/learning/ReviewCenter'))
const LearningOverview=lazy(()=>import('./features/learning/LearningOverview'))
const LearningPreferences=lazy(()=>import('./features/learning/LearningPreferences'))
const SocialWorkspace=lazy(()=>import('./features/social/SocialWorkspace'))
const ChatWorkspace=lazy(()=>import('./features/social/ChatWorkspace'))
const PwaSettings=lazy(()=>import('./features/pwa/PwaSettings'))
const OfflineEntry=lazy(()=>import('./features/offline/OfflineEntry'))
const OfflineSettings=lazy(()=>import('./features/offline/OfflineSettings'))
const ProfileSettings=lazy(()=>import('./features/social/ProfileSettings'))
const Workbench=lazy(()=>import('./features/social/Workbench'))
const CommunityWorkspace=lazy(()=>import('./features/community/CommunityWorkspace'))
const ArchiveHistory=lazy(()=>import('./features/social/ArchiveHistory'))
type Section='home'|'notes'|'cards'|'quizzes'|'reviews'|'learning'|'preferences'|'social'|'messages'|'community'|'profile'|'chat'|'ai'|'invites'|'users'|'jobs'|'audit'
const statuses:Record<string,string>={queued:'等待执行',running:'正在执行',succeeded:'已完成',failed:'失败',active:'正常',disabled:'已停用'}
const startupDefaults=createStartupDefaults(()=>window.sessionStorage,()=>readSession()?.scope||'')
function ErrorMessage({error}:{error:unknown}) {
  if (!error) return null
  return <div className="error" role="alert">{error instanceof Error ? error.message : '操作失败，请重试'}{error instanceof ApiError ? <small>请求标识：{error.requestId}</small> : null}</div>
}
function Empty({children}:{children:ReactNode}) {return <div className="empty">{children}</div>}
function Status({value}:{value:string}) {return <span className={'badge '+value}>{statuses[value]||value}</span>}

function AuthScreen({notice,error,onOffline}:{notice?:string;error?:unknown;onOffline:()=>void}) {
  const client=useQueryClient()
  const [mode,setMode]=useState<'login'|'register'|'reset'>('login')
  const mutation=useMutation({mutationFn:async(form:HTMLFormElement)=>{
    const ticket=authSession.ticket(),submittedMode=mode
    const data=Object.fromEntries(new FormData(form))
    if(submittedMode==='reset'){const value=await api('/auth/tab/password/reset','POST',data as ResetInput);return {ticket,mode:submittedMode,value}}
    const result=await authenticate(submittedMode==='login'?'/auth/tab/login':'/auth/tab/register',data as LoginInput|RegisterInput)
    return {ticket:result.ticket,mode:submittedMode,value:result.user}
  },onSuccess:result=>{if(!authSession.current(result.ticket))return;if(result.mode==='reset'){setMode('login');mutation.reset()}else{const session=authSession.ticket();client.setQueryData(['me',session.generation],result.value)}}})
  function submit(e:FormEvent<HTMLFormElement>){e.preventDefault();mutation.mutate(e.currentTarget)}
  const title=mode==='login'?'欢迎回到知页':mode==='register'?'开始你的学习空间':'恢复账户访问'
  return <main className="auth-layout">
    <section className="auth-story"><div className="brand"><BookOpen size={28}/>知页 <span>AI NOTES</span></div><div><p className="eyebrow">你的知识，慢慢积累。</p><h1>让每一页笔记，<br/>成为下一次理解的起点。</h1><p>一个属于你和朋友的学习空间。<br/>记录思考，连接知识，一起往前走。</p></div><span className="story-footer">个人学习空间 · 邀请制</span></section>
    <section className="auth-form"><div className="mini-brand"><BookOpen/> 知页</div><p className="eyebrow">学习，从这里继续</p><h2>{title}</h2><p className="muted">{mode==='register'?'使用管理员提供的邀请码注册。':mode==='reset'?'输入管理员核实身份后提供的一次性凭证。':'登录后查看你的账户与学习空间。'}</p>
      {notice?<p className="error" role="alert">{notice}</p>:null}<ErrorMessage error={error}/>
      <form onSubmit={submit} key={mode}>
        {mode!=='reset'?<label>账号<input name="username" required minLength={3} maxLength={32} pattern={'[a-zA-Z0-9_\\-]+'} autoComplete="username" placeholder="3–32 位字母、数字或下划线"/></label>:<label>重置凭证<input name="token" required minLength={20} maxLength={128} autoComplete="off"/></label>}
        {mode==='register'?<label>昵称<input name="display_name" required maxLength={64} placeholder="朋友如何称呼你"/></label>:null}
        <label>{mode==='reset'?'新密码':'密码'}<input name="password" type="password" required minLength={mode==='login'?1:6} maxLength={128} autoComplete={mode==='login'?'current-password':'new-password'} placeholder={mode==='login'?'输入密码':'至少 6 位'}/></label>
        {mode==='register'?<label>邀请码<input name="invite_code" required minLength={20} maxLength={128} autoComplete="off" placeholder="粘贴管理员提供的邀请码"/></label>:null}
        <ErrorMessage error={mutation.error}/><button className="primary full" disabled={mutation.isPending}>{mutation.isPending?'正在处理…':mode==='login'?'登录学习空间':mode==='register'?'创建账户':'重置密码'}<ArrowUpRight size={18}/></button>
      </form>
      <div className="auth-links">{(['login','register','reset'] as const).filter(x=>x!==mode).map(x=><button className="text-button" key={x} onClick={()=>{setMode(x);mutation.reset()}}>{x==='login'?'返回登录':x==='register'?'使用邀请码注册':'忘记密码'}</button>)}</div>
      <button type="button" className="secondary" onClick={onOffline}>解锁本机离线资料</button><p className="auth-note">登录仅对当前标签页生效，刷新保留登录。新开标签页可登录其他账号。</p>
    </section>
  </main>
}

function InvitesPanel() {
  const client=useQueryClient()
  const [created,setCreated]=useState<CreatedInvite|null>(null)
  const [copied,setCopied]=useState(false)
  const [copyError,setCopyError]=useState<Error|null>(null)
  const query=useQuery({queryKey:['invites'],queryFn:()=>api<Invite[]>('/admin/invites')})
  const create=useMutation({mutationFn:(form:HTMLFormElement)=>{const d=new FormData(form);return api<CreatedInvite>('/admin/invites','POST',{label:d.get('label'),max_uses:Number(d.get('max_uses')),valid_days:Number(d.get('valid_days'))})},onSuccess:(d)=>{setCreated(d);setCopied(false);client.invalidateQueries({queryKey:['invites']})}})
  const toggle=useMutation({mutationFn:(i:Invite)=>api('/admin/invites/'+i.id,'PATCH',{disabled:!i.disabled}),onSuccess:()=>client.invalidateQueries({queryKey:['invites']})})
  return <><div className="panel"><h3>邀请朋友加入</h3><p className="muted">邀请码只在创建后显示一次，请及时复制保存。</p><form className="invite-form" onSubmit={e=>{e.preventDefault();create.mutate(e.currentTarget)}}><label>备注<input name="label" placeholder="例如：学习小组" maxLength={64}/></label><label>使用次数<input name="max_uses" type="number" min={1} max={100} defaultValue={1} required/></label><label>有效天数<input name="valid_days" type="number" min={1} max={90} defaultValue={7} required/></label><button className="primary" disabled={create.isPending}>创建邀请码</button></form><ErrorMessage error={create.error}/>
    {created?<div className="secret-box"><span>新邀请码</span><code>{created.code}</code><button className="secondary" onClick={async()=>{try{await navigator.clipboard.writeText(created.code);setCopied(true);setCopyError(null)}catch{setCopyError(new Error('无法自动复制，请手动选中保存'))}}}>{copied?<Check size={16}/>:<Copy size={16}/>} {copied?'已复制':'复制'}</button><button className="text-button" onClick={()=>setCreated(null)}>隐藏</button><ErrorMessage error={copyError}/></div>:null}</div>
    <div className="panel"><h3>最近的邀请码</h3><ErrorMessage error={query.error||toggle.error}/>{query.isLoading?<Empty>正在读取…</Empty>:query.data?.length?<div className="table-wrap"><table><thead><tr><th>备注</th><th>使用情况</th><th>到期时间</th><th>状态</th><th>操作</th></tr></thead><tbody>{query.data.map(i=><tr key={i.id}><td>{i.label||'未备注'}</td><td>{i.used_count} / {i.max_uses}</td><td>{date(i.expires_at)}</td><td><Status value={i.disabled?'disabled':new Date(i.expires_at)<new Date()||i.used_count>=i.max_uses?'失效':'active'}/></td><td><button className="text-button" disabled={toggle.isPending} onClick={()=>toggle.mutate(i)}>{i.disabled?'启用':'停用'}</button></td></tr>)}</tbody></table></div>:<Empty>还没有邀请码，创建一个邀请朋友加入。</Empty>}</div></>
}

function UsersPanel() {
  const client=useQueryClient()
  const [reset,setReset]=useState<{token:string;expires_at:string}|null>(null)
  const query=useQuery({queryKey:['users'],queryFn:()=>api<User[]>('/admin/users')})
  const change=useMutation({mutationFn:(u:User)=>api('/admin/users/'+u.id,'PATCH',{status:u.status==='active'?'disabled':'active'}),onSuccess:()=>client.invalidateQueries({queryKey:['users']})})
  const restore=useMutation({mutationFn:(u:User)=>api<{token:string;expires_at:string}>('/admin/users/'+u.id+'/reset-token','POST',{}),onSuccess:setReset})
  return <div className="panel"><h3>账户管理</h3><p className="muted">停用账户会立即撤销已有会话。核实身份后，可提供一次性密码重置凭证。</p><ErrorMessage error={query.error||change.error||restore.error}/>{reset?<div className="secret-box"><span>重置凭证 · {date(reset.expires_at)} 到期</span><code>{reset.token}</code><button className="text-button" onClick={()=>setReset(null)}>隐藏</button></div>:null}<div className="table-wrap"><table><thead><tr><th>用户</th><th>角色</th><th>状态</th><th>操作</th></tr></thead><tbody>{query.data?.map(u=><tr key={u.id}><td><strong>{u.display_name}</strong><small>@{u.username}</small></td><td>{u.role==='admin'?'管理员':'成员'}</td><td><Status value={u.status}/></td><td>{u.role==='user'?<div className="row-actions"><button className="text-button" disabled={change.isPending} onClick={()=>change.mutate(u)}>{u.status==='active'?'停用':'启用'}</button><button className="text-button" disabled={restore.isPending||u.status!=='active'} onClick={()=>restore.mutate(u)}>恢复密码</button></div>:<span className="muted">服务器管理</span>}</td></tr>)}</tbody></table></div>{query.isLoading?<Empty>正在读取…</Empty>:null}</div>
}

function JobsPanel({admin}:{admin:boolean}) {
  const client=useQueryClient()
  const query=useQuery({queryKey:['jobs',admin],queryFn:()=>api<Job[]>(admin?'/admin/jobs':'/jobs'),refetchInterval:3000})
  const create=useMutation({mutationFn:()=>api<Job>('/jobs/diagnostic','POST',{request_key:createUuid(),duration_seconds:3}),onSuccess:()=>client.invalidateQueries({queryKey:['jobs']})})
  const retry=useMutation({mutationFn:(id:string)=>api('/admin/jobs/'+id+'/retry','POST',{}),onSuccess:()=>client.invalidateQueries({queryKey:['jobs']})})
  return <div className="panel"><div className="panel-heading"><div><h3>后台任务</h3><p className="muted">自检任务用于验证后台处理。页面会自动更新任务状态。</p></div><button className="primary" disabled={create.isPending} onClick={()=>create.mutate()}>运行自检 <Activity size={16}/></button></div><ErrorMessage error={query.error||create.error||retry.error}/>{query.data?.length?<div className="table-wrap"><table><thead><tr><th>任务</th><th>状态</th><th>尝试次数</th><th>创建时间</th><th>结果</th></tr></thead><tbody>{query.data.map(j=><tr key={j.id}><td>{j.kind==='pdf.import'?'PDF 导入':j.kind==='image.process'?'图片处理':j.kind==='notification.test'?'浏览器提醒测试':j.kind==='notification.push'?'浏览器推送':'后台自检'}<small>{j.id.slice(0,8)}</small></td><td><Status value={j.status}/></td><td>{j.attempts} / {j.max_attempts}</td><td>{date(j.created_at)}</td><td>{j.status==='failed'&&admin?<button className="text-button" disabled={retry.isPending} onClick={()=>retry.mutate(j.id)}>重新执行</button>:j.status==='succeeded'?(j.kind==='notification.push'?(j.result?.outcome==='accepted'?'推送服务已接受，设备送达未确认':j.result?.outcome==='skipped'?'已跳过':'推送处理已结束'):j.kind==='notification.test'?(j.result?.outcome==='queued'?'站内测试已生成，等待推送':'已跳过'):j.kind==='pdf.import'?(j.result?.accepted?'已转换 '+j.result.pages+' 页':'未完整转换，请在笔记中查看原因'):'正常'):j.error_code||'—'}</td></tr>)}</tbody></table></div>:<Empty>暂无任务。运行一次自检，检查后台是否正常工作。</Empty>}</div>
}

function AuditPanel() {
  const query=useQuery({queryKey:['audit'],queryFn:()=>api<Audit[]>('/admin/audit')})
  return <div className="panel"><h3>最近的管理与账户操作</h3><p className="muted">记录操作与请求标识，不保存密码、邀请码或正文。</p><ErrorMessage error={query.error}/><div className="table-wrap"><table><thead><tr><th>操作</th><th>时间</th><th>请求标识</th></tr></thead><tbody>{query.data?.map(a=><tr key={a.id}><td>{a.action}</td><td>{date(a.created_at)}</td><td><code>{a.request_id}</code></td></tr>)}</tbody></table></div></div>
}

function Home({user,onGo}:{user:User;onGo:(s:Section)=>void}) {
  const ready=useQuery({queryKey:['health'],queryFn:()=>api<{status:string}>('/health/ready'),refetchInterval:30000})
  const notices=useNotificationFeed(user.id)
  const client=useQueryClient()
  const read=useMutation({mutationFn:(id:string)=>api('/notifications/'+id+'/read','POST',{}),onSuccess:()=>client.invalidateQueries({queryKey:['notices',user.id]})})
  return <>
    <section className="panel home-notices" aria-label="最近通知"><div className="panel-heading"><h3>最近通知</h3><Bell size={19} aria-hidden="true"/></div><ErrorMessage error={notices.error||read.error}/>{notices.data?.length?notices.data.map(n=><div className="notice" key={n.id}><span className={'notice-dot '+(n.read_at?'read':'')}/><div><strong>{n.message}</strong><small>{date(n.created_at)}</small>{n.target==='reviews'?<button className="text-button" onClick={()=>onGo('reviews')}>打开复习中心</button>:null}</div>{!n.read_at?<button className="text-button" disabled={read.isPending} onClick={()=>read.mutate(n.id)}>已读</button>:null}</div>):<div className="home-notice-empty"><span className="muted">这里会显示后台任务和学习提醒。</span><button className="text-button" onClick={()=>onGo('jobs')}>查看后台任务 <ChevronRight size={15}/></button></div>}{notices.catchingUp?<p role="status">正在继续补拉通知…<button className="text-button" onClick={()=>void notices.refetch()}>继续补拉通知</button></p>:null}<NotificationHistory owner={user.id} onRead={id=>read.mutate(id)} readPending={read.isPending}/></section>
    <details className="panel home-status">
      <summary>空间状态 <span className={ready.isError?'badge home-status-error':'badge'}>系统连接：{ready.isPending?'检查中':ready.isError?'暂不可用':'正常'}</span></summary>
      <div className="status-grid"><div className="status-card"><ShieldCheck aria-hidden="true"/><span>账户身份</span><strong>{user.role==='admin'?'管理员':'学习成员'}</strong><small>邀请制 · 私人空间</small></div><div className="status-card"><Activity aria-hidden="true"/><span>系统连接</span><strong>{ready.isPending?'检查中':ready.isError?'暂不可用':'正常'}</strong><small>账户与数据库服务</small></div><div className="status-card"><KeyRound aria-hidden="true"/><span>模型服务</span><strong>统一配置</strong><small>登录成员共用 · 模型设置查看状态</small></div></div>
    </details>
    <details className="panel settings-roadmap"><summary>功能进度</summary><p className="muted">已开放的功能和后续计划。</p>{[{n:'01',t:'账户与系统基础',s:'已完成',done:true},{n:'02',t:'笔记管理与 AI 对话',s:'笔记与 PDF 对话已开放',done:false},{n:'03',t:'搜索、卡片与复习',s:'搜索、学习卡片与复习已开放',done:false},{n:'04',t:'好友与学习社区',s:'好友、私聊与社区已开放',done:false}].map(x=><div className="roadmap-row" key={x.n}><span className={x.done?'step done':'step'}>{x.done?<Check size={16}/>:x.n}</span><strong>{x.t}</strong><span className="muted">{x.s}</span></div>)}</details>
  </>
}

export default function App() {
  const client=useQueryClient()
  const session=useSyncExternalStore(authSession.subscribe,authSession.snapshot)
  const explicitNavigation=useRef(false),startupApplied=useRef('')
  const [postToOpen,setPostToOpen]=useState('')
  const consumePost=useCallback(()=>setPostToOpen(''),[])
  const [section,setSection]=useState<Section>('notes')
  const [offlineOpen,setOfflineOpen]=useState(false)
  const [mobileNavOpen,setMobileNavOpen]=useState(false)
  const navTrigger=useRef<HTMLButtonElement>(null),navigation=useRef<HTMLElement>(null)
  useEffect(()=>{
    if(!mobileNavOpen)return
    const previousOverflow=document.body.style.overflow
    document.body.style.overflow='hidden'
    navigation.current?.querySelector<HTMLButtonElement>('button')?.focus()
    const keydown=(event:KeyboardEvent)=>{
      if(event.key==='Escape'){event.preventDefault();setMobileNavOpen(false);return}
      if(event.key!=='Tab')return
      const controls=Array.from(navigation.current?.querySelectorAll<HTMLElement>('button,a[href]')||[]).filter(element=>!element.hasAttribute('disabled')&&element.getClientRects().length>0)
      const first=controls[0],last=controls[controls.length-1]
      if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus()}
      else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus()}
    }
    const resize=()=>{if(window.innerWidth>760)setMobileNavOpen(false)}
    document.addEventListener('keydown',keydown);window.addEventListener('resize',resize)
    return()=>{document.body.style.overflow=previousOverflow;document.removeEventListener('keydown',keydown);window.removeEventListener('resize',resize);navTrigger.current?.focus()}
  },[mobileNavOpen])
  useChatRealtime(session.phase==='active'&&!offlineOpen?session.owner||'':'')
  const [socialInitialTab,setSocialInitialTab]=useState<'friends'|'shares'>('friends')
  const [peerToOpen,setPeerToOpen]=useState('')
  const consumePeer=useCallback(()=>setPeerToOpen(''),[])
  const [noteToOpen,setNoteToOpen]=useState('')
  const [reviewToOpen,setReviewToOpen]=useState('')
  const [quizToOpen,setQuizToOpen]=useState('')
  const [textSelection,setTextSelection]=useState<AskTextSelection|null>(null)
  const [storageNotice,setStorageNotice]=useState('')
  useEffect(()=>{
    let previous=authSession.ticket()
    const unsubscribe=authSession.subscribe(()=>{
      const next=authSession.ticket(),old=previous;previous=next
      if(next.generation===old.generation)return
      // Abort/remove old queries before any delayed response can repopulate a private screen.
      void client.cancelQueries();client.clear()
      if(old.owner&&(next.phase==='closing'||next.phase==='anonymous'||next.owner!==old.owner)&&!clearTabNavigation(old.owner))setStorageNotice('界面已锁定，但浏览器未允许清理当前标签页记录。')
      if(old.owner&&(next.phase==='closing'||next.phase==='anonymous'||next.owner!==old.owner))startupDefaults.clear(old.owner)
      explicitNavigation.current=false;startupApplied.current='';setPostToOpen('')
      setPeerToOpen('');setNoteToOpen('');setReviewToOpen('');setQuizToOpen('');setTextSelection(null);setMobileNavOpen(false);setSection('notes')
    })
    const stop=startSessionSync(()=>{void client.invalidateQueries({queryKey:['me']})})
    return()=>{stop();unsubscribe()}
  },[client])
  const me=useQuery({queryKey:['me',session.generation],queryFn:async()=>{const ticket=authSession.ticket();const user=await api<User>('/auth/me');authSession.verify(user.id,ticket);return user},enabled:!offlineOpen&&(session.phase==='checking'||session.phase==='active')})
  const homePreferences=usePersonalization(me.data?.id||'',session.id)
  useEffect(()=>{
    const owner=me.data?.id
    if(!owner||session.phase!=='active'||session.owner!==owner)return
    const identity=owner+':'+session.id
    if(startupApplied.current===identity)return
    const restoredNotes=wasNotesOpen(owner),restoredChat=wasChatOpen(owner)
    if(!shouldApplyHomeDefault({explicitNavigation:explicitNavigation.current,restoredNotes,restoredChat,alreadyApplied:startupDefaults.has(owner)})){
      startupApplied.current=identity
      startupDefaults.mark(owner)
      if(!explicitNavigation.current){if(restoredNotes)setSection('notes');else if(restoredChat)setSection('chat')}
      return
    }
    if(homePreferences.data){startupApplied.current=identity;startupDefaults.mark(owner);const next=homeSection(homePreferences.data.default_section);if(next)setSection(next)}
  },[me.data?.id,session.id,session.phase,session.owner,homePreferences.data])
  function go(next:Section){
    if(['invites','users','audit'].includes(next)&&me.data?.role!=='admin')return
    setMobileNavOpen(false)
    explicitNavigation.current=true;setPostToOpen('');if(next==='social')setSocialInitialTab('friends')
    if(me.data){startupDefaults.mark(me.data.id);if(next==='notes'||next==='chat')rememberSection(me.data.id,next)}
    setNoteToOpen('');setReviewToOpen('');setQuizToOpen('');setTextSelection(null);setSection(next)
  }
  const logout=useMutation({mutationFn:async()=>{const ticket=authSession.ticket();const {confirmOfflineLogout}=await import('./features/offline/logout-guard.ts');if(!await confirmOfflineLogout(user!.id))return;if(!authSession.current(ticket,user!.id))return;const closing=authSession.beginLogout(ticket);try{await cancelPushForLogout(user!.id);await logoutTab()}finally{if(authSession.current(closing))clearSession()}}})
  if(session.phase==='closing')return <div className="loading">正在退出登录，已暂停本机编辑…</div>
  if(offlineOpen)return <Suspense fallback={<main className="offline-entry"><p>正在打开本机离线空间…</p></main>}><OfflineEntry onClose={()=>setOfflineOpen(false)}/></Suspense>
  if(session.phase==='anonymous')return <AuthScreen notice={storageNotice} error={logout.error} onOffline={()=>setOfflineOpen(true)}/>
  if(me.isPending) return <div className="loading">正在连接你的学习空间…<button className="secondary" onClick={()=>setOfflineOpen(true)}>解锁本机离线资料</button></div>
  if(!me.data || (me.error instanceof ApiError&&me.error.status===401)) {
    if(me.error instanceof ApiError&&me.error.status===401) return <AuthScreen onOffline={()=>setOfflineOpen(true)}/>
    return <main className="connection-error"><BookOpen size={36}/><h2>暂时无法连接学习空间</h2><ErrorMessage error={me.error}/><button className="primary" onClick={()=>me.refetch()}>重新连接</button><button className="secondary" onClick={()=>setOfflineOpen(true)}>解锁本机离线资料</button></main>
  }
  if(session.phase!=='active'||session.owner!==me.data.id)return <div className="loading">正在核对登录状态…</div>
  const user=me.data
  const navGroups=[
    {label:'日常学习',items:[{id:'notes' as const,label:'我的笔记',icon:BookOpen},{id:'chat' as const,label:'AI 对话',icon:MessageSquare},{id:'cards' as const,label:'学习卡片',icon:Layers},{id:'quizzes' as const,label:'学习测验',icon:ClipboardList},{id:'reviews' as const,label:'复习中心',icon:RotateCcw}]},
    {label:'发现与协作',items:[{id:'social' as const,label:'好友与分享',icon:Users},{id:'messages' as const,label:'私聊消息',icon:MessageSquare},{id:'community' as const,label:'学习社区',icon:Compass}]},
    {label:'空间工具',items:[{id:'home' as const,label:'空间概览',icon:LayoutDashboard},{id:'learning' as const,label:'学习统计',icon:BarChart3}]},
  ]
  const settingsNav=[{id:'profile' as const,label:'个人设置',icon:Users},{id:'preferences' as const,label:'学习提醒',icon:Bell},{id:'ai' as const,label:'模型服务',icon:KeyRound},{id:'jobs' as const,label:'后台任务',icon:Activity}]
  const adminNav=[{id:'invites' as const,label:'邀请码',icon:Ticket},{id:'users' as const,label:'成员管理',icon:Users},{id:'audit' as const,label:'操作记录',icon:ShieldCheck}]
  const currentSection=user.role!=='admin'&&adminNav.some(item=>item.id===section)?'notes':section
  const inSettings=settingsNav.some(item=>item.id===currentSection)
  const inAdmin=user.role==='admin'&&adminNav.some(item=>item.id===currentSection)
  const titles:Record<Section,string>={home:'空间概览',social:'好友与分享',messages:'私聊消息',community:'学习社区',profile:'个人资料与隐私',notes:'我的笔记',cards:'学习卡片',quizzes:'学习测验',reviews:'复习中心',learning:'学习统计',preferences:'学习提醒',chat:'AI 对话',ai:'模型服务',invites:'邀请码管理',users:'成员管理',jobs:'后台任务',audit:'操作记录'}
  const pageTitle=inSettings?'设置':inAdmin?'空间管理':titles[currentSection]
  const descriptions:Record<Section,string>={notes:'集中管理你的学习笔记，支持搜索、筛选、导入与新建，快速回顾和复习重要内容。',chat:'围绕文字、笔记与 PDF 提问，把理解留在每一次对话里。',cards:'整理知识卡片，连接原始笔记与复习。',quizzes:'创建与完成测验，检验你的掌握情况。',reviews:'把笔记、卡片和测验安排进复习计划。',learning:'回顾学习记录，了解你的学习进展。',home:'查看工作台、通知和空间状态。',social:'找到学习伙伴，管理好友、关注与笔记分享。',messages:'与好友继续交流，消息记录只对参与者可见。',community:'发现学习分享，阅读与发布社区内容。',profile:'管理个人资料、隐私以及本机与离线设置。',preferences:'设置学习提醒，安排适合自己的节奏。',ai:'查看模型服务，管理可用模型与工具配置。',jobs:'查看任务进度、结果与失败重试。',invites:'创建邀请码，邀请朋友加入学习空间。',users:'管理成员状态与账户恢复。',audit:'查看空间管理操作记录。'}
  return <LearningActivityProvider key={user.id+session.id} owner={user.id} sessionId={session.id}><div className="app-shell workspace-refresh">
    <a className="workspace-skip-link" href="#workspace-main">跳到主要内容</a>
    <button type="button" className="navigation-backdrop" hidden={!mobileNavOpen} tabIndex={-1} aria-label="关闭导航遮罩" onClick={()=>setMobileNavOpen(false)}/>
    <aside ref={navigation} id="workspace-navigation" className={'sidebar workspace-sidebar'+(mobileNavOpen?' is-open':'')} role={mobileNavOpen?'dialog':undefined} aria-modal={mobileNavOpen?true:undefined} aria-label="工作空间导航">
      <div className="sidebar-brand-row"><div className="brand"><BookOpen/> 知页</div><button type="button" className="icon-button nav-close" aria-label="关闭导航" onClick={()=>setMobileNavOpen(false)}><X size={20}/></button></div>
      <p className="sidebar-caption">私人学习空间</p>
      <div className="sidebar-navs">{navGroups.map(group=><nav className="workspace-nav-group" key={group.label} aria-label={group.label}><p className="workspace-nav-label">{group.label}</p>{group.items.map(item=><button key={item.id} type="button" className={currentSection===item.id?'nav-item selected':'nav-item'} aria-current={currentSection===item.id?'page':undefined} onClick={()=>go(item.id)}><item.icon size={18} aria-hidden="true"/><span>{item.label}</span></button>)}{group.label==='空间工具'?<button type="button" className="nav-item" onClick={()=>{setMobileNavOpen(false);setOfflineOpen(true)}}><Download size={18} aria-hidden="true"/><span>本机离线空间</span></button>:null}</nav>)}</div>
      <div className="sidebar-footer">
        <nav className="sidebar-utilities" aria-label="设置与管理">
          <button type="button" className={inSettings?'nav-item selected':'nav-item'} aria-label="设置" title="设置" aria-current={inSettings?'page':undefined} onClick={()=>go('profile')}><SettingsIcon size={19} aria-hidden="true"/><span>设置</span></button>
          {user.role==='admin'?<button type="button" className={inAdmin?'nav-item selected':'nav-item'} aria-label="管理" title="管理" aria-current={inAdmin?'page':undefined} onClick={()=>go('invites')}><ShieldCheck size={19} aria-hidden="true"/><span>管理</span></button>:null}
        </nav>
        <div className="account"><span className="avatar">{user.display_name.slice(0,1)}</span><div><strong title={user.display_name}>{user.display_name}</strong><small>@{user.username}</small></div><button type="button" aria-label="退出登录" className="icon-button" disabled={logout.isPending} onClick={()=>logout.mutate()}><LogOut size={17}/></button></div>
      </div>
    </aside>
    <main id="workspace-main" className="main" tabIndex={-1} inert={mobileNavOpen}><header aria-label="页面操作"><div><button ref={navTrigger} type="button" className="icon-button mobile-menu-toggle" aria-label="打开导航" aria-expanded={mobileNavOpen} aria-controls="workspace-navigation" onClick={()=>setMobileNavOpen(true)}><Menu size={21}/></button></div><button type="button" className="icon-button" aria-label="刷新当前数据" title="刷新当前数据" onClick={()=>client.invalidateQueries()}><RefreshCw size={18}/></button></header>
      <div className="content"><div className="page-heading"><div><h1>{pageTitle}</h1><p className="page-description">{descriptions[currentSection]}</p></div></div><ErrorMessage error={logout.error}/>
        {inSettings?<nav className="section-nav" aria-label="设置分类">{settingsNav.map(item=><button key={item.id} type="button" className={currentSection===item.id?'section-link selected':'section-link'} aria-current={currentSection===item.id?'page':undefined} onClick={()=>go(item.id)}><item.icon size={16} aria-hidden="true"/>{item.label}</button>)}</nav>:null}
        {inAdmin?<nav className="section-nav" aria-label="管理分类">{adminNav.map(item=><button key={item.id} type="button" className={currentSection===item.id?'section-link selected':'section-link'} aria-current={currentSection===item.id?'page':undefined} onClick={()=>go(item.id)}><item.icon size={16} aria-hidden="true"/>{item.label}</button>)}</nav>:null}
        {currentSection==='home'?<div className="home-workspace"><Suspense fallback={<p>正在读取工作台…</p>}><Workbench key={user.id+':'+session.id} owner={user.id} onReadPost={id=>{go('community');setPostToOpen(id)}} onGo={next=>{if(next==='shares'){go('social');setSocialInitialTab('shares')}else go(next)}}/></Suspense><Home user={user} onGo={go}/></div>:currentSection==='social'?<Suspense fallback={<p>正在打开好友空间…</p>}><SocialWorkspace key={user.id+':'+socialInitialTab} owner={user.id} initialTab={socialInitialTab} onMessage={id=>{setPeerToOpen(id);go('messages')}}/></Suspense>:currentSection==='messages'?<Suspense fallback={<p>正在打开私聊…</p>}><ChatWorkspace owner={user.id} peerId={peerToOpen} onPeerConsumed={consumePeer} onFriends={()=>go('social')}/></Suspense>:currentSection==='profile'?<Suspense fallback={<p>正在打开个人设置…</p>}><ProfileSettings owner={user.id}/><PwaSettings owner={user.id}/><OfflineSettings owner={user.id} onOpen={()=>setOfflineOpen(true)}/><ArchiveHistory owner={user.id}/></Suspense>:currentSection==='community'?<Suspense fallback={<p>正在打开学习社区…</p>}><CommunityWorkspace key={user.id+':'+session.id} owner={user.id} admin={user.role==='admin'} initialPostId={postToOpen} onPostConsumed={consumePost}/></Suspense>:currentSection==='notes'?<Suspense fallback={<div className="empty">正在打开笔记空间…</div>}><NotesWorkspace owner={user.id} initialNoteId={noteToOpen} onGoReview={id=>{go('reviews');setReviewToOpen(id||'')}} onAskText={selection=>{go('chat');setTextSelection(selection)}}/></Suspense>:currentSection==='cards'?<Suspense fallback={<div className="empty">正在打开学习卡片…</div>}><CardWorkspace owner={user.id} onOpenNote={id=>{rememberNote(user.id,id);go('notes');setNoteToOpen(id)}}/></Suspense>:currentSection==='quizzes'?<Suspense fallback={<div className="empty">正在打开学习测验…</div>}><QuizWorkspace owner={user.id} initialQuizId={quizToOpen} onGoReview={id=>{go('reviews');setReviewToOpen(id||'')}} onOpenNote={id=>{rememberNote(user.id,id);go('notes');setNoteToOpen(id)}}/></Suspense>:currentSection==='reviews'?<Suspense fallback={<div className="empty">正在打开复习中心…</div>}><ReviewCenter owner={user.id} initialPlanId={reviewToOpen} onOpenQuiz={id=>{go('quizzes');setQuizToOpen(id)}} onOpenNote={id=>{rememberNote(user.id,id);go('notes');setNoteToOpen(id)}}/></Suspense>:currentSection==='learning'?<Suspense fallback={<div className="empty">正在读取学习统计…</div>}><LearningOverview owner={user.id}/></Suspense>:currentSection==='preferences'?<Suspense fallback={<div className="empty">正在读取学习提醒…</div>}><LearningPreferences owner={user.id}/></Suspense>:currentSection==='chat'?<Suspense fallback={<div className="empty">正在打开 AI 对话…</div>}><AiChat owner={user.id} initialTextSelection={textSelection||undefined} onTextSelectionConsumed={requestKey=>setTextSelection(current=>current?.requestKey===requestKey?null:current)} onOpenNote={id=>{rememberNote(user.id,id);go('notes');setNoteToOpen(id)}}/></Suspense>:currentSection==='ai'?<Suspense fallback={<div className="empty">正在读取模型设置…</div>}><AiSettings owner={user.id} admin={user.role==='admin'}/></Suspense>:currentSection==='invites'&&user.role==='admin'?<InvitesPanel/>:currentSection==='users'&&user.role==='admin'?<UsersPanel/>:currentSection==='audit'&&user.role==='admin'?<AuditPanel/>:<JobsPanel admin={user.role==='admin'}/>}<footer>知页 · 给知识一个可以生长的地方</footer>
      </div>
    </main>
  </div></LearningActivityProvider>
}
