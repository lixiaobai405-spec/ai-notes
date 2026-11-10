import {useState} from 'react'
import {useQuery,useMutation,useQueryClient} from '@tanstack/react-query'
import {api} from '../../api'
import {NoteError} from '../notes/NoteError'
import type {Profile} from './social'
import './social.css'
function ProfileForm({initial,onSaved,locked,onSavingChange}:{initial:Profile;onSaved:()=>void;locked:boolean;onSavingChange:(busy:boolean)=>void}){
  const [draft,setDraft]=useState({...initial}),[notice,setNotice]=useState('')
  const save=useMutation({mutationFn:()=>api<Profile>('/social/profile','PATCH',{revision:draft.revision,display_name:draft.display_name,bio:draft.bio||'',subject:draft.subject||'',details:draft.details||'',visibility:draft.visibility}),onSuccess:data=>{setDraft(data);setNotice('个人资料已保存');onSaved()},onSettled:()=>onSavingChange(false)})
  return <form className="social-form" onSubmit={e=>{e.preventDefault();onSavingChange(true);save.mutate()}}><fieldset disabled={save.isPending||locked}><label>昵称<input required maxLength={64} value={draft.display_name} onChange={e=>setDraft({...draft,display_name:e.target.value})}/></label><label>简介<input maxLength={240} value={draft.bio||''} onChange={e=>setDraft({...draft,bio:e.target.value})}/></label><label>学习学科<input maxLength={80} value={draft.subject||''} onChange={e=>setDraft({...draft,subject:e.target.value})}/></label><label>详细资料<textarea maxLength={4000} rows={5} value={draft.details||''} onChange={e=>setDraft({...draft,details:e.target.value})}/></label><label>详细资料可见范围<select value={draft.visibility||'friends'} onChange={e=>setDraft({...draft,visibility:e.target.value as 'friends'|'private'})}><option value="friends">仅好友可见</option><option value="private">仅自己可见</option></select></label><p className="muted">账号和昵称对登录成员可见。笔记、AI 会话与学习数据始终保持各自权限。</p><button className="primary">{save.isPending?'正在保存…':'保存个人资料'}</button></fieldset><NoteError error={save.error}/>{save.isError?<p>输入已保留。如有版本冲突，可先复制输入，再点击下方重新读取。</p>:null}<p role="status">{notice}</p></form>
}
export default function ProfileSettings({owner}:{owner:string}){
  const client=useQueryClient(),[reload,setReload]=useState(0),[reloading,setReloading]=useState(false),[saving,setSaving]=useState(false)
  const query=useQuery({queryKey:['social',owner,'profile'],queryFn:()=>api<Profile>('/social/profile')})
  async function reloadProfile(){if(saving||reloading||query.isFetching||!window.confirm('重新读取将清除尚未保存的资料输入，是否继续？'))return;setReloading(true);try{const result=await query.refetch();if(result.isSuccess)setReload(v=>v+1)}finally{setReloading(false)}}
  return <section className="panel"><h2>个人资料与隐私</h2><NoteError error={query.error}/>{query.data?<ProfileForm key={owner+':'+reload} initial={query.data} locked={reloading} onSavingChange={setSaving} onSaved={()=>{void client.invalidateQueries({queryKey:['me']});void client.invalidateQueries({queryKey:['social',owner]})}}/>:<p role="status">正在读取资料…</p>}{reloading?<p role="status">正在重新读取资料，暂时暂停编辑…</p>:null}<button className="text-button" disabled={saving||query.isFetching||reloading} onClick={()=>void reloadProfile()}>重新读取服务器资料</button></section>
}
