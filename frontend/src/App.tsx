import { lazy, Suspense, useEffect, useState, useSyncExternalStore, type FormEvent, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { BookOpen, ArrowUpRight, LayoutDashboard, Ticket, Users, ShieldCheck, Activity, LogOut, Bell, RefreshCw, Check, ChevronRight, KeyRound, Copy, MessageSquare } from 'lucide-react'
import { api, authenticate, logoutTab, date, ApiError, type User, type Invite, type CreatedInvite, type Job, type Notice, type Audit, type LoginInput, type RegisterInput, type ResetInput } from './api'

import { clearTabNavigation,rememberSection,rememberNote,wasNotesOpen,wasChatOpen } from './features/notes/drafts'
import { authSession,clearSession,startSessionSync } from './session.ts'
import { createUuid } from './uuid'
const NotesWorkspace=lazy(()=>import('./features/notes/NotesWorkspace'))
const AiChat=lazy(()=>import('./features/ai/AiChat'))
const AiSettings=lazy(()=>import('./features/ai/AiSettings'))
type Section='home'|'notes'|'chat'|'ai'|'invites'|'users'|'jobs'|'audit'
const statuses:Record<string,string>={queued:'等待执行',running:'正在执行',succeeded:'已完成',failed:'失败',active:'正常',disabled:'已停用'}
function ErrorMessage({error}:{error:unknown}) {
  if (!error) return null
  return <div className="error" role="alert">{error instanceof Error ? error.message : '操作失败，请重试'}{error instanceof ApiError ? <small>请求标识：{error.requestId}</small> : null}</div>
}
function Empty({children}:{children:ReactNode}) {return <div className="empty">{children}</div>}
function Status({value}:{value:string}) {return <span className={'badge '+value}>{statuses[value]||value}</span>}

function AuthScreen({notice,error}:{notice?:string;error?:unknown}) {
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
        {mode!=='reset'?<label>用户名<input name="username" required minLength={3} maxLength={32} pattern={'[a-zA-Z0-9_\\-]+'} autoComplete="username" placeholder="3–32 位字母、数字或下划线"/></label>:<label>重置凭证<input name="token" required minLength={20} maxLength={128} autoComplete="off"/></label>}
        {mode==='register'?<label>显示名称<input name="display_name" required maxLength={64} placeholder="朋友如何称呼你"/></label>:null}
        <label>{mode==='reset'?'新密码':'密码'}<input name="password" type="password" required minLength={mode==='login'?1:10} maxLength={128} autoComplete={mode==='login'?'current-password':'new-password'} placeholder={mode==='login'?'输入密码':'至少 10 位'}/></label>
        {mode==='register'?<label>邀请码<input name="invite_code" required minLength={20} maxLength={128} autoComplete="off" placeholder="粘贴管理员提供的邀请码"/></label>:null}
        <ErrorMessage error={mutation.error}/><button className="primary full" disabled={mutation.isPending}>{mutation.isPending?'正在处理…':mode==='login'?'登录学习空间':mode==='register'?'创建账户':'重置密码'}<ArrowUpRight size={18}/></button>
      </form>
      <div className="auth-links">{(['login','register','reset'] as const).filter(x=>x!==mode).map(x=><button className="text-button" key={x} onClick={()=>{setMode(x);mutation.reset()}}>{x==='login'?'返回登录':x==='register'?'使用邀请码注册':'忘记密码'}</button>)}</div>
      <p className="auth-note">登录仅对当前标签页生效，刷新保留登录。新开标签页可登录其他账号。</p>
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
  return <div className="panel"><div className="panel-heading"><div><h3>后台任务</h3><p className="muted">自检任务用于验证后台处理。页面会自动更新任务状态。</p></div><button className="primary" disabled={create.isPending} onClick={()=>create.mutate()}>运行自检 <Activity size={16}/></button></div><ErrorMessage error={query.error||create.error||retry.error}/>{query.data?.length?<div className="table-wrap"><table><thead><tr><th>任务</th><th>状态</th><th>尝试次数</th><th>创建时间</th><th>结果</th></tr></thead><tbody>{query.data.map(j=><tr key={j.id}><td>{j.kind==='pdf.import'?'PDF 导入':j.kind==='image.process'?'图片处理':'后台自检'}<small>{j.id.slice(0,8)}</small></td><td><Status value={j.status}/></td><td>{j.attempts} / {j.max_attempts}</td><td>{date(j.created_at)}</td><td>{j.status==='failed'&&admin?<button className="text-button" disabled={retry.isPending} onClick={()=>retry.mutate(j.id)}>重新执行</button>:j.status==='succeeded'?(j.kind==='pdf.import'?(j.result?.accepted?'已转换 '+j.result.pages+' 页':'未完整转换，请在笔记中查看原因'):'正常'):j.error_code||'—'}</td></tr>)}</tbody></table></div>:<Empty>暂无任务。运行一次自检，检查后台是否正常工作。</Empty>}</div>
}

function AuditPanel() {
  const query=useQuery({queryKey:['audit'],queryFn:()=>api<Audit[]>('/admin/audit')})
  return <div className="panel"><h3>最近的管理与账户操作</h3><p className="muted">记录操作与请求标识，不保存密码、邀请码或正文。</p><ErrorMessage error={query.error}/><div className="table-wrap"><table><thead><tr><th>操作</th><th>时间</th><th>请求标识</th></tr></thead><tbody>{query.data?.map(a=><tr key={a.id}><td>{a.action}</td><td>{date(a.created_at)}</td><td><code>{a.request_id}</code></td></tr>)}</tbody></table></div></div>
}

function Home({user,onGo}:{user:User;onGo:(s:Section)=>void}) {
  const ready=useQuery({queryKey:['health'],queryFn:()=>api<{status:string}>('/health/ready'),refetchInterval:30000})
  const notices=useQuery({queryKey:['notices'],queryFn:()=>api<Notice[]>('/notifications'),refetchInterval:5000})
  const client=useQueryClient()
  const read=useMutation({mutationFn:(id:string)=>api('/notifications/'+id+'/read','POST',{}),onSuccess:()=>client.invalidateQueries({queryKey:['notices']})})
  return <><section className="welcome"><div><p className="eyebrow">一起积累，持续理解</p><h2>{user.display_name}，欢迎来到知页。</h2><p>你的笔记空间已经准备好。新建一页，记录正在学习的想法。</p><button className="primary" onClick={()=>onGo('notes')}>打开我的笔记<ArrowUpRight size={17}/></button></div><div className="welcome-mark" aria-hidden="true"><BookOpen size={100} strokeWidth={1}/><span>每一页，都算数。</span></div></section>
    <div className="status-grid"><div className="status-card"><ShieldCheck/><span>账户身份</span><strong>{user.role==='admin'?'管理员':'学习成员'}</strong><small>邀请制 · 私人空间</small></div><div className="status-card"><Activity/><span>系统连接</span><strong>{ready.isPending?'检查中':ready.isError?'暂不可用':'正常'}</strong><small>账户与数据库服务</small></div><div className="status-card"><KeyRound/><span>模型服务</span><strong>统一配置</strong><small>登录成员共用 · 模型设置查看状态</small></div></div>
    <div className="home-grid"><div className="panel"><div className="panel-heading"><h3>空间建设进度</h3><span className="badge active">基础已就绪</span></div><p className="muted">按阶段开放，让每个功能都能真正使用。</p>{[{n:'01',t:'账户与系统基础',s:'已完成',done:true},{n:'02',t:'笔记管理与 AI 对话',s:'笔记与 PDF 对话已开放',done:false},{n:'03',t:'搜索、卡片与复习',s:'规划中',done:false},{n:'04',t:'好友与学习社区',s:'规划中',done:false}].map(x=><div className="roadmap-row" key={x.n}><span className={x.done?'step done':'step'}>{x.done?<Check size={16}/>:x.n}</span><strong>{x.t}</strong><span className="muted">{x.s}</span></div>)}</div>
    <div className="panel"><div className="panel-heading"><h3>最近通知</h3><Bell size={19}/></div><ErrorMessage error={notices.error||read.error}/>{notices.data?.length?notices.data.map(n=><div className="notice" key={n.id}><span className={'notice-dot '+(n.read_at?'read':'')}/><div><strong>{n.message}</strong><small>{date(n.created_at)}</small></div>{!n.read_at?<button className="text-button" disabled={read.isPending} onClick={()=>read.mutate(n.id)}>已读</button>:null}</div>):<Empty>这里会显示后台任务和学习提醒。<button className="text-button" onClick={()=>onGo('jobs')}>运行一次后台自检 <ChevronRight size={15}/></button></Empty>}</div></div>
  </>
}

export default function App() {
  const client=useQueryClient()
  const session=useSyncExternalStore(authSession.subscribe,authSession.snapshot)
  const [section,setSection]=useState<Section>('home')
  const [noteToOpen,setNoteToOpen]=useState('')
  const [storageNotice,setStorageNotice]=useState('')
  useEffect(()=>{
    let previous=authSession.ticket()
    const unsubscribe=authSession.subscribe(()=>{
      const next=authSession.ticket(),old=previous;previous=next
      if(next.generation===old.generation)return
      // Abort/remove old queries before any delayed response can repopulate a private screen.
      void client.cancelQueries();client.clear()
      if(old.owner&&(next.phase==='closing'||next.phase==='anonymous'||next.owner!==old.owner)&&!clearTabNavigation(old.owner))setStorageNotice('界面已锁定，但浏览器未允许清理当前标签页记录。')
      setNoteToOpen('');setSection('home')
    })
    const stop=startSessionSync(()=>{void client.invalidateQueries({queryKey:['me']})})
    return()=>{stop();unsubscribe()}
  },[client])
  const me=useQuery({queryKey:['me',session.generation],queryFn:async()=>{const ticket=authSession.ticket();const user=await api<User>('/auth/me');authSession.verify(user.id,ticket);return user},enabled:session.phase==='checking'||session.phase==='active'})
  useEffect(()=>{if(me.data?.id){if(wasNotesOpen(me.data.id))setSection('notes');else if(wasChatOpen(me.data.id))setSection('chat')}},[me.data?.id])
  function go(next:Section){if(me.data)rememberSection(me.data.id,next);setNoteToOpen('');setSection(next)}
  const logout=useMutation({mutationFn:async()=>{const closing=authSession.beginLogout(authSession.ticket());try{await logoutTab()}finally{if(authSession.current(closing))clearSession()}}})
  if(session.phase==='closing')return <div className="loading">正在退出登录，已暂停本机编辑…</div>
  if(session.phase==='anonymous')return <AuthScreen notice={storageNotice} error={logout.error}/>
  if(me.isPending) return <div className="loading">正在连接你的学习空间…</div>
  if(!me.data || (me.error instanceof ApiError&&me.error.status===401)) {
    if(me.error instanceof ApiError&&me.error.status===401) return <AuthScreen/>
    return <main className="connection-error"><BookOpen size={36}/><h2>暂时无法连接学习空间</h2><ErrorMessage error={me.error}/><button className="primary" onClick={()=>me.refetch()}>重新连接</button></main>
  }
  if(session.phase!=='active'||session.owner!==me.data.id)return <div className="loading">正在核对登录状态…</div>
  const user=me.data
  const nav=[{id:'home' as const,label:'空间概览',icon:LayoutDashboard},{id:'notes' as const,label:'我的笔记',icon:BookOpen},{id:'chat' as const,label:'AI 对话',icon:MessageSquare},{id:'ai' as const,label:'模型设置',icon:KeyRound},{id:'jobs' as const,label:'后台任务',icon:Activity},...(user.role==='admin'?[{id:'invites' as const,label:'邀请码',icon:Ticket},{id:'users' as const,label:'成员管理',icon:Users},{id:'audit' as const,label:'操作记录',icon:ShieldCheck}]:[])]
  const titles:Record<Section,string>={home:'空间概览',notes:'我的笔记',chat:'AI 对话',ai:'模型设置',invites:'邀请码管理',users:'成员管理',jobs:'后台任务',audit:'操作记录'}
  return <div className="app-shell"><aside className="sidebar"><div className="brand"><BookOpen/> 知页 <span>AI NOTES</span></div><div className="workspace-chip"><span className="workspace-avatar">知</span><div><strong>我们的学习空间</strong><small>个人与朋友 · 邀请制</small></div></div><p className="nav-label">工作空间</p><nav>{nav.map(n=><button key={n.id} className={section===n.id?'nav-item selected':'nav-item'} onClick={()=>go(n.id)}><n.icon size={19}/>{n.label}</button>)}</nav><div className="sidebar-note"><span className="badge">正在建设</span><p>从一页笔记开始，<br/>积累自己的知识世界。</p></div><div className="account"><span className="avatar">{user.display_name.slice(0,1)}</span><div><strong>{user.display_name}</strong><small>@{user.username}</small></div><button aria-label="退出登录" className="icon-button" disabled={logout.isPending} onClick={()=>logout.mutate()}><LogOut size={17}/></button></div></aside><main className="main"><header><div><span className="muted">工作空间</span><ChevronRight size={13}/><strong>{titles[section]}</strong></div><button className="icon-button" aria-label="刷新当前数据" onClick={()=>client.invalidateQueries()}><RefreshCw size={17}/></button></header><div className="content"><div className="page-heading"><div><p className="eyebrow">YOUR LEARNING SPACE</p><h1>{titles[section]}</h1></div><span className="version">M1 · 笔记空间</span></div><ErrorMessage error={logout.error}/>{section==='home'?<Home user={user} onGo={go}/>:section==='notes'?<Suspense fallback={<div className="empty">正在打开笔记空间…</div>}><NotesWorkspace owner={user.id} initialNoteId={noteToOpen}/></Suspense>:section==='chat'?<Suspense fallback={<div className="empty">正在打开 AI 对话…</div>}><AiChat owner={user.id} onOpenNote={id=>{rememberNote(user.id,id);go('notes');setNoteToOpen(id)}}/></Suspense>:section==='ai'?<Suspense fallback={<div className="empty">正在读取模型设置…</div>}><AiSettings owner={user.id} admin={user.role==='admin'}/></Suspense>:section==='invites'&&user.role==='admin'?<InvitesPanel/>:section==='users'&&user.role==='admin'?<UsersPanel/>:section==='audit'&&user.role==='admin'?<AuditPanel/>:<JobsPanel admin={user.role==='admin'}/>}<footer>知页 · 给知识一个可以生长的地方</footer></div></main></div>
}
