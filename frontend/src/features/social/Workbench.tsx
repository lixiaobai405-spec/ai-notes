import {useQuery} from '@tanstack/react-query'
import {api} from '../../api'
import {NoteError} from '../notes/NoteError'
import './social.css'
import PersonalizationHub from '../personalization/PersonalizationHub'
import {usePersonalization} from '../personalization/usePersonalization'
export default function Workbench({owner,onGo,onReadPost}:{owner:string;onReadPost:(id:string)=>void;onGo:(section:'notes'|'chat'|'reviews'|'social'|'messages'|'community'|'profile'|'shares')=>void}){
  const preferences=usePersonalization(owner)
  const counts=useQuery({queryKey:['social',owner,'counts'],queryFn:()=>api<{friends:number;incoming_requests:number;following:number;received_shares:number}>('/social/counts'),refetchInterval:30000})
  const threads=useQuery({queryKey:['social',owner,'chat','summary'],queryFn:()=>api<{threads:number;unread_messages:number;unread_threads:number}>('/social/chat/summary'),refetchInterval:10000})
  const due=useQuery({queryKey:['workbench',owner,'due'],queryFn:()=>api<{total:number}>('/learning/review-due?limit=1'),refetchInterval:30000})
  const notes=useQuery({queryKey:['workbench',owner,'notes'],queryFn:()=>api<{total:number}>('/notes?limit=1')})
  const tiles=[{label:'我的笔记',value:notes.data?.total,target:'notes' as const,detail:'继续记录与阅读'},{label:'待复习',value:due.data?.total,target:'reviews' as const,detail:'笔记、卡片、测验独立进度'},{label:'好友',value:counts.data?.friends,target:'social' as const,detail:`${counts.data?.incoming_requests??'—'} 个待处理申请`},{label:'未读私聊',value:threads.data?.unread_messages,target:'messages' as const,detail:`${threads.data?.unread_threads??'—'} 个会话有新消息`},{label:'收到的分享',value:counts.data?.received_shares,target:'shares' as const,detail:'好友的只读笔记入口'}]
  const visible=(preferences.data?.tile_order||tiles.map(t=>t.target)).filter(id=>!preferences.data?.hidden_tiles.includes(id)).flatMap(id=>tiles.filter(tile=>tile.target===id))
  return <section className="home-workbench" aria-label="综合学习工作台">
    <div className="panel home-quick-panel">
      <h3>继续学习</h3>
      {visible.length?<div className="social-count-grid">{visible.map(tile=><button key={tile.target+tile.label} onClick={()=>onGo(tile.target)}><span>{tile.label}</span><strong>{tile.value??'—'}</strong><small>{tile.detail}</small></button>)}</div>:null}
      <NoteError error={counts.error||threads.error||due.error||notes.error}/>
      <div className="social-actions"><button className="secondary" onClick={()=>onGo('chat')}>继续 AI 对话</button><button className="secondary" onClick={()=>onGo('community')}>浏览学习社区</button><button className="text-button" onClick={()=>onGo('profile')}>个人资料与隐私</button></div>
    </div>
    <PersonalizationHub owner={owner} onRead={onReadPost} onLatest={()=>onGo('community')}/>
  </section>
}
