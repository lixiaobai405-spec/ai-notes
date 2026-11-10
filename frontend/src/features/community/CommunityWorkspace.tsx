import {lazy,Suspense,useEffect,useId,useRef,useState} from 'react'
import {useQuery} from '@tanstack/react-query'
import {SlidersHorizontal} from 'lucide-react'
import {api,date} from '../../api'
import {NoteError} from '../notes/NoteError'
import {queryString} from '../social/social'
import {postTypes,statuses,type Draft,type Page,type PostType,type Summary} from './types'
import './community.css'

const PostEditor=lazy(()=>import('./PostEditor'))
const PostReader=lazy(()=>import('./PostReader'))
const Moderation=lazy(()=>import('./Moderation'))
type Tab='feed'|'mine'|'bookmarks'|'moderation'
type Mode='latest'|'following'|'recommended'
interface Filters {subject:string;post_type:PostType|'';tag:string}
const emptyFilters:Filters={subject:'',post_type:'',tag:''}
const feedModes=[{id:'latest',label:'最新发布'},{id:'following',label:'我的关注'},{id:'recommended',label:'规则推荐'}] as const
const communityTabs:ReadonlyArray<{id:Tab;label:string}>=[{id:'feed',label:'社区发现'},{id:'mine',label:'我的帖子'},{id:'bookmarks',label:'我的收藏'}]

function PostCard({post,recommended,onRead}:{post:Summary;recommended:boolean;onRead:()=>void}){
  return <article className="community-card"><button className="text-button community-title" onClick={onRead}>{post.title}</button><p className="muted">{post.author.display_name} · @{post.author.username} · {date(post.published_at)}</p><div className="community-tags"><span>{postTypes[post.post_type]}</span>{post.subject?<span>{post.subject}</span>:null}{post.tags.map(tag=><span key={tag}>#{tag}</span>)}</div>{post.excerpt?<p className="community-excerpt">{post.excerpt}</p>:<p className="muted">打开帖子阅读完整内容与图片。</p>}{recommended?<p className="community-reason">推荐依据：{post.recommendation_reason}</p>:null}<div className="community-actions"><button className="secondary" onClick={onRead}>阅读与讨论</button><span className="muted">{post.like_count} 个赞 · {post.comment_count} 条讨论{post.bookmarked?' · 已收藏':''}</span></div></article>
}

export default function CommunityWorkspace({owner,admin,initialPostId='',onPostConsumed}:{owner:string;admin:boolean;initialPostId?:string;onPostConsumed?:()=>void}){
  const [tab,setTab]=useState<Tab>('feed'),[mode,setMode]=useState<Mode>('latest'),[cursor,setCursor]=useState(''),[selected,setSelected]=useState(initialPostId),[editing,setEditing]=useState('')
  useEffect(()=>{if(initialPostId){setSelected(initialPostId);setEditing('');onPostConsumed?.()}},[initialPostId,onPostConsumed])
  const [input,setInput]=useState(emptyFilters),[filters,setFilters]=useState(emptyFilters),[filtersOpen,setFiltersOpen]=useState(false)
  const filterId=useId(),filterTrigger=useRef<HTMLButtonElement>(null)
  const feed=useQuery({queryKey:['community',owner,'feed',tab,mode,filters,cursor],queryFn:()=>api<Page<Summary>>((tab==='bookmarks'?'/community/bookmarks':'/community/posts')+queryString({cursor,mode:tab==='feed'?mode:undefined,subject:tab==='feed'?filters.subject:undefined,post_type:tab==='feed'?filters.post_type:undefined,tag:tab==='feed'?filters.tag:undefined})),enabled:tab==='feed'||tab==='bookmarks'})
  const mine=useQuery({queryKey:['community',owner,'mine',cursor],queryFn:()=>api<Page<Draft>>('/community/posts/mine'+queryString({cursor})),enabled:tab==='mine',refetchInterval:tab==='mine'?5000:false})
  const navigation=(next:Tab)=>{setTab(next);setCursor('');setSelected('');setEditing('');setFiltersOpen(false)}
  const clearFilters=()=>{setInput(emptyFilters);setFilters(emptyFilters);setCursor('')}
  if(editing)return <Suspense fallback={<p role="status">正在打开社区编辑器…</p>}><PostEditor owner={owner} postId={editing} onClose={()=>{setEditing('');setTab('mine');setCursor('')}} onCreated={setEditing} onRead={id=>{setEditing('');setSelected(id)}}/></Suspense>
  if(selected)return <Suspense fallback={<p role="status">正在打开公开帖子…</p>}><PostReader key={owner+':'+selected} owner={owner} admin={admin} postId={selected} onClose={()=>setSelected('')} onEdit={id=>{setSelected('');setEditing(id)}}/></Suspense>
  const page=tab==='mine'?mine.data:feed.data
  const pending=tab==='mine'?mine.isPending:feed.isPending
  const fetching=tab==='mine'?mine.isFetching:feed.isFetching
  const appliedFilters=[
    {id:'subject',label:filters.subject&&`学科 ${filters.subject}`},
    {id:'post_type',label:filters.post_type&&`类型 ${postTypes[filters.post_type]}`},
    {id:'tag',label:filters.tag&&`标签 ${filters.tag}`},
  ].filter(item=>item.label)
  const heading=tab==='mine'?'我的帖子与草稿':tab==='bookmarks'?'我的社区收藏':'社区帖子'
  return <div className="community-stack">
    <div className="community-workspace-toolbar">
      <nav className="community-tabs" aria-label="社区分类">
        {communityTabs.map(item=><button key={item.id} type="button" className="secondary" aria-current={tab===item.id?'page':undefined} onClick={()=>navigation(item.id)}>{item.label}</button>)}
        {admin?<button type="button" className="secondary" aria-current={tab==='moderation'?'page':undefined} onClick={()=>navigation('moderation')}>社区审核</button>:null}
      </nav>
      <button type="button" className="primary" onClick={()=>setEditing('new')}>写帖子 / 准备笔记副本</button>
    </div>
    {tab==='moderation'&&admin?<Suspense fallback={<p>正在打开审核工作台…</p>}><Moderation owner={owner} onRead={setSelected}/></Suspense>:<section className="panel community-feed-panel" aria-label={heading}>
      {tab!=='feed'?<h2>{heading}</h2>:<>
        <div className="community-feed-toolbar">
          <div className="community-actions" role="group" aria-label="信息流排序">
            {feedModes.map(item=><button key={item.id} type="button" className="secondary" aria-pressed={mode===item.id} onClick={()=>{setMode(item.id);setCursor('')}}>{item.label}</button>)}
          </div>
          <button type="button" className="secondary" ref={filterTrigger} aria-controls={filterId} aria-expanded={filtersOpen} onClick={()=>setFiltersOpen(value=>!value)}>
            <SlidersHorizontal size={16} aria-hidden="true"/>筛选{appliedFilters.length?<span className="badge">{appliedFilters.length}</span>:null}
          </button>
        </div>
        {mode==='recommended'?<p className="muted community-feed-hint">推荐按内容完整度排序，独立图片和足量正文计分，同分按发布时间。每帖展示具体依据。</p>:mode==='following'?<p className="muted community-feed-hint">展示你关注的成员发布的帖子。</p>:null}
        <form className="community-filter" id={filterId} hidden={!filtersOpen} onSubmit={event=>{
          event.preventDefault()
          setFilters({...input,subject:input.subject.trim(),tag:input.tag.trim()})
          setCursor('')
          setFiltersOpen(false)
          filterTrigger.current?.focus()
        }}>
          <label>学科<input maxLength={80} value={input.subject} onChange={event=>setInput({...input,subject:event.target.value})} placeholder="全部学科"/></label>
          <label>帖子类型<select value={input.post_type} onChange={event=>setInput({...input,post_type:event.target.value as PostType|''})}><option value="">全部类型</option>{Object.entries(postTypes).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label>
          <label>标签<input maxLength={32} value={input.tag} onChange={event=>setInput({...input,tag:event.target.value})} placeholder="输入完整标签"/></label>
          <div className="community-filter-actions"><button type="submit" className="primary">应用筛选</button><button type="button" className="text-button" onClick={clearFilters}>清除筛选</button></div>
        </form>
        <div className="community-applied-filters" aria-live="polite">
          {appliedFilters.length?<>
            <span className="muted">已应用筛选</span>
            {appliedFilters.map(item=><span key={item.id} className="badge">{item.label}</span>)}
            {!filtersOpen?<button type="button" className="text-button" onClick={()=>{clearFilters();filterTrigger.current?.focus()}}>清除筛选</button>:null}
          </>:null}
        </div>
      </>}
      <NoteError error={tab==='mine'?mine.error:feed.error}/>
      {pending?<p role="status">正在读取社区内容…</p>:page?.items.length?<div className="community-post-list">
        {tab==='mine'?mine.data?.items.map(post=><article className="community-card" key={post.id}>
          <h3 className="community-wrap">{post.title}</h3>
          <p className="muted">{statuses[post.status]} · 草稿版本 {post.revision} · {date(post.updated_at)}</p>
          {post.publish_status==='preparing'?<p role="status">正在准备独立副本，完成后自动发布。</p>:post.publish_status==='failed'?<p className="error">副本准备失败，可打开草稿查看详情并重试。</p>:null}
          {post.moderation_reason?<p className="error">下架原因：{post.moderation_reason}</p>:null}
          <div className="community-actions"><button type="button" className="secondary" onClick={()=>setEditing(post.id)}>编辑草稿 / 发布管理</button>{post.status==='published'?<button type="button" className="text-button" onClick={()=>setSelected(post.id)}>阅读公开版本</button>:null}</div>
        </article>):feed.data?.items.map(post=><PostCard key={post.id} post={post} recommended={tab==='feed'&&mode==='recommended'} onRead={()=>setSelected(post.id)}/>)}
      </div>:<p className="empty">{tab==='mine'?'还没有帖子，先写一篇原创或从自己的笔记准备副本。':tab==='bookmarks'?'还没有有效收藏。已撤回或下架的帖子不会显示。':appliedFilters.length?'没有符合当前筛选的帖子。':mode==='following'?'关注列表暂无公开帖子，可以先到好友空间关注学习伙伴。':'暂无公开帖子。'}</p>}
      <div className="community-actions community-feed-pagination">
        {cursor||page?.next_cursor?<>
          <button type="button" className="secondary" disabled={!cursor||fetching} onClick={()=>setCursor('')}>列表首页</button>
          <button type="button" className="secondary" disabled={!page?.next_cursor||fetching} onClick={()=>setCursor(page?.next_cursor||'')}>下一页帖子</button>
        </>:null}
        <button type="button" className="text-button" disabled={fetching} onClick={()=>void(tab==='mine'?mine.refetch():feed.refetch())}>刷新当前页</button>
      </div>
      <details className="community-feed-help">
        <summary>社区内容与权限说明</summary>
        <p className="muted">社区对已登录成员开放，展示作者主动发布的独立副本。私人笔记、AI 对话和学习数据保持各自权限。关注用于查看成员发布的公开内容，好友和私人资源权限单独管理。</p>
      </details>
    </section>}
  </div>
}
