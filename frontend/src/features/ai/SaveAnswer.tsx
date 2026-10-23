import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../api'
import type { components } from '../../api.generated'
import type { Notebook } from '../notes/types'
import type { Run } from './chat-stream'

export type SavedAnswer=components['schemas']['SavedAnswerOut']

export function SaveAnswer({owner,run,saved,savedReady,onOpenNote}:{owner:string;run:Run;saved?:SavedAnswer;savedReady:boolean;onOpenNote?:(noteId:string)=>void}) {
  const client=useQueryClient()
  const [open,setOpen]=useState(false)
  const [title,setTitle]=useState(()=>Array.from(run.prompt.trim().replace(/\s+/g,' ')).slice(0,200).join(''))
  const [notebook,setNotebook]=useState('')
  const notebooks=useQuery({queryKey:['notes',owner,'notebooks'],queryFn:()=>api<Notebook[]>('/notebooks'),enabled:open})
  const save=useMutation({mutationFn:()=>api<SavedAnswer>(`/ai/runs/${run.id}/save-note`,'POST',{title:title.trim(),notebook_id:notebook||null}),onSuccess:row=>{
    client.setQueryData<SavedAnswer[]>(['ai-saved-notes',owner,run.conversation_id],old=>[...(old||[]).filter(item=>item.run_id!==row.run_id),row])
    void client.invalidateQueries({queryKey:['ai-saved-notes',owner,run.conversation_id]})
    void client.invalidateQueries({queryKey:['notes',owner]})
    setOpen(false)
  }})
  function submit(event:FormEvent){event.preventDefault();if(!save.isPending&&savedReady&&title.trim())save.mutate()}
  if(saved)return <div className="saved-answer-actions" role="status"><span>{saved.deleted_at?'已保存的笔记在回收站':'已保存为笔记'}：{saved.note_title}</span>{!saved.deleted_at&&onOpenNote?<button type="button" className="text-button" onClick={()=>onOpenNote(saved.note_id)}>打开笔记</button>:null}{saved.deleted_at?<small className="muted">可到“我的笔记”的回收站恢复。</small>:null}</div>
  if(run.status!=='succeeded'||!run.answer.trim())return null
  return <div className="save-answer">
    {!open?<button type="button" className="secondary" disabled={!savedReady} onClick={()=>setOpen(true)}>{savedReady?'保存为笔记':'等待核对保存状态…'}</button>:<form className="save-answer-form" onSubmit={submit}>
      <h4>保存为可编辑笔记</h4>
      <label>保存笔记标题<input value={title} maxLength={200} required disabled={save.isPending} onChange={event=>setTitle(event.target.value)}/></label>
      <label>保存到笔记本<select aria-label="保存到笔记本" value={notebook} disabled={save.isPending||notebooks.isPending||!!notebooks.error} onChange={event=>setNotebook(event.target.value)}><option value="">未分类</option>{notebooks.data?.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      {notebooks.isPending?<p className="muted" role="status">正在读取笔记本…</p>:null}
      {notebooks.error?<div className="error" role="alert">{notebooks.error instanceof Error?notebooks.error.message:'笔记本读取失败'}<button type="button" className="text-button" onClick={()=>void notebooks.refetch()}>重新读取笔记本</button></div>:null}
      <p className="muted">将保存这次问题、回答和来源信息；保存后可继续编辑。每条回答只创建一篇笔记。</p>
      {save.error?<div className="error" role="alert"><p>{save.error instanceof Error?save.error.message:'保存失败，请重试。'}</p>{!(save.error instanceof ApiError)||save.error.status>=500?<p>保存结果尚未确认。重试会确认同一篇笔记，不会创建副本或重新生成回答。</p>:null}</div>:null}
      <div className="saved-answer-actions"><button className="primary" disabled={save.isPending||!title.trim()||!savedReady}>{save.isPending?'正在保存…':save.error?'重试保存':'确认保存笔记'}</button><button type="button" className="text-button" disabled={save.isPending} onClick={()=>{setOpen(false);save.reset()}}>取消</button></div>
    </form>}
  </div>
}
