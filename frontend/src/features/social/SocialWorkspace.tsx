import {useState} from 'react'
import {useMutation,useQuery,useQueryClient} from '@tanstack/react-query'
import {ChevronDown} from 'lucide-react'
import {api} from '../../api'
import {NoteError} from '../notes/NoteError'
import ShareManager from './ShareManager'
import {queryString,type FriendRequest,type Member,type Page,type Profile} from './social'
import './social.css'

const tabs=[
  {id:'friends',label:'我的好友',heading:'我的好友'},
  {id:'members',label:'查找成员',heading:'查找学习伙伴'},
  {id:'incoming',label:'收到的申请',heading:'收到的好友申请'},
  {id:'outgoing',label:'发出的申请',heading:'发出的好友申请'},
  {id:'following',label:'我的关注',heading:'我的关注'},
  {id:'shares',label:'笔记分享',heading:'笔记分享'},
] as const
type Tab=typeof tabs[number]['id']

function MemberProfile({owner,memberId,onClose,onMessage}:{owner:string;memberId:string;onClose:()=>void;onMessage:(id:string)=>void}){
  const q=useQuery({queryKey:['social',owner,'member',memberId],queryFn:()=>api<Profile>('/social/members/'+memberId)})
  return <section className="panel"><button className="text-button" onClick={onClose}>返回成员列表</button><NoteError error={q.error}/>{q.data?<><h2>{q.data.display_name}</h2><p className="muted">@{q.data.username}</p>{q.data.details_visible?<><p>{q.data.bio||'尚未填写简介'}</p><p>学习学科：{q.data.subject||'尚未填写'}</p><p className="social-prewrap">{q.data.details||'尚未填写详细资料'}</p></>:<p>详细资料仅按对方隐私设置向好友展示。</p>}{q.data.relationship==='friend'?<button className="primary" onClick={()=>onMessage(memberId)}>发私聊消息</button>:null}</>:<p role="status">正在读取成员资料…</p>}</section>
}

export default function SocialWorkspace({owner,onMessage,initialTab='friends'}:{owner:string;onMessage:(id:string)=>void;initialTab?:'friends'|'shares'}){
  const client=useQueryClient(),[tab,setTab]=useState<Tab>(initialTab),[search,setSearch]=useState(''),[q,setQ]=useState(''),[cursor,setCursor]=useState(''),[memberId,setMemberId]=useState(''),[notice,setNotice]=useState('')
  const path=tab==='friends'?'/social/friends':tab==='incoming'||tab==='outgoing'?'/social/friend-requests':tab==='following'?'/social/following':'/social/members'
  const query=useQuery({queryKey:['social',owner,'list',tab,q,cursor],queryFn:()=>api<Page<Member|FriendRequest>>(path+queryString({cursor,q:tab==='members'?q:'',direction:tab==='incoming'||tab==='outgoing'?tab:undefined})),enabled:tab!=='shares'})
  const action=useMutation({mutationFn:({path,method='POST',body,message}:{path:string;method?:string;body?:unknown;message:string})=>api(path,method,body).then(()=>message),onSuccess:message=>{setNotice(message);void client.invalidateQueries({queryKey:['social',owner]})}})
  const run=(path:string,message:string,method='POST',body?:unknown)=>action.mutate({path,method,body,message})
  if(memberId)return <MemberProfile owner={owner} memberId={memberId} onClose={()=>setMemberId('')} onMessage={onMessage}/>
  return <div className="social-stack friend-workspace">
    <nav className="social-tabs" aria-label="好友空间分类">
      {tabs.map(item=><button key={item.id} type="button" className={tab===item.id?'primary':'secondary'} aria-current={tab===item.id?'page':undefined} onClick={()=>{setTab(item.id);setCursor('');setNotice('');action.reset()}}>{item.label}</button>)}
    </nav>
    {tab==='shares'?<ShareManager owner={owner}/>:<section className="panel friend-list">
      <h2>{tabs.find(item=>item.id===tab)?.heading}</h2>
      {tab==='members'?<form className="social-search" onSubmit={e=>{e.preventDefault();setQ(search);setCursor('')}}>
        <label>按账号查找<input value={search} maxLength={32} onChange={e=>setSearch(e.target.value)} placeholder="输入成员账号"/></label>
        <button className="primary">查找</button>
      </form>:null}
      <NoteError error={query.error||action.error}/>
      <p className="friend-notice" role="status">{notice}</p>
      {query.isPending?<p>正在读取成员…</p>:query.data?.items.length?query.data.items.map(item=>{
        const relation='member' in item?item:null
        const member=relation?relation.member:item as Member
        const isFriend=member.relationship==='friend'
        const showMore=isFriend||member.following
        const follow=()=>run('/social/follows/'+member.id,member.following?'已取消关注':'已关注，关注不代表好友',member.following?'DELETE':'PUT')
        return <article key={tab+':'+(relation?.id||member.id)} className="social-row friend-row">
          <div>
            <button type="button" className="text-button social-name" onClick={()=>setMemberId(member.id)}>{member.display_name}</button>
            <p className="muted">@{member.username}{isFriend?' · 好友':''}</p>
          </div>
          <div className="social-actions friend-row-actions">
            {tab==='incoming'&&relation?<>
              <button type="button" className="primary" disabled={action.isPending} onClick={()=>run('/social/friend-requests/'+relation.id+'/respond','已接受好友申请','POST',{decision:'accept',revision:relation.revision})}>接受</button>
              <button type="button" className="secondary" disabled={action.isPending} onClick={()=>run('/social/friend-requests/'+relation.id+'/respond','已拒绝好友申请','POST',{decision:'reject',revision:relation.revision})}>拒绝</button>
            </>:null}
            {member.relationship==='none'?<button type="button" className="secondary" disabled={action.isPending} onClick={()=>run('/social/friend-requests','好友申请已发送','POST',{user_id:member.id})}>加为好友</button>:null}
            {member.relationship==='outgoing'?<span className="badge">等待对方确认</span>:null}
            {isFriend?<button type="button" className="primary" onClick={()=>onMessage(member.id)}>私聊</button>:null}
            {showMore?<details className="friend-more" name="friend-more">
              <summary className="secondary" aria-label={member.display_name+'的更多操作'}>更多 <ChevronDown size={14} aria-hidden="true"/></summary>
              <div className="friend-more-actions">
                <button type="button" className="text-button" disabled={action.isPending} onClick={follow}>{member.following?'取消关注':'关注'}</button>
                {isFriend?<button type="button" className="text-button" disabled={action.isPending} onClick={()=>{if(window.confirm('解除好友会立即停止双方已有笔记分享的新读取，私聊历史保留。继续？'))run('/social/friends/'+member.id,'已解除好友关系','DELETE')}}>解除好友</button>:null}
              </div>
            </details>:<button type="button" className="secondary" disabled={action.isPending} onClick={follow}>关注</button>}
          </div>
        </article>
      }):<p className="empty">此列表暂无成员。</p>}
      {cursor||query.data?.next_cursor?<div className="social-actions friend-pagination">
        <button type="button" className="secondary" disabled={!cursor||query.isFetching} onClick={()=>setCursor('')}>返回首页</button>
        <button type="button" className="secondary" disabled={!query.data?.next_cursor||query.isFetching} onClick={()=>setCursor(query.data?.next_cursor||'')}>下一页成员</button>
      </div>:null}
    </section>}
  </div>
}
