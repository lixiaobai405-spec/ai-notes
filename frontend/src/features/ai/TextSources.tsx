import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api'
import type { components } from '../../api.generated'

export type TextSource=components['schemas']['TextSourceOut']
type TextRead=components['schemas']['TextSourceReadOut']

export function TextSelectionScope({source,onOpen}:{source:TextSource;onOpen?:()=>void}) {
  return <div className="pdf-run-scope text-selection-scope" aria-label="文字选段来源">
    <p><strong>{source.title}</strong> · 来源版本 {source.revision} · 文字选段（{Array.from(source.quote).length} 字）</p>
    <blockquote>{source.quote}</blockquote>
    {onOpen?<button type="button" className="citation-link" onClick={onOpen}>查看原文选段</button>:null}
  </div>
}

export function TextSourceReader({owner,runId,onClose,onOpenNote,closeLabel='返回对话'}:{owner:string;runId:string;onClose:()=>void;onOpenNote?:(id:string)=>void;closeLabel?:string}) {
  const highlight=useRef<HTMLElement>(null)
  const query=useQuery({queryKey:['ai-text-source',owner,runId],queryFn:()=>api<TextRead>(`/ai/runs/${runId}/source-text`),retry:false})
  useEffect(()=>{if(query.data)highlight.current?.scrollIntoView({block:'center',behavior:'smooth'})},[runId,query.data])
  const value=query.data
  const chars=value?Array.from(value.text):[]
  // Validate the response before highlighting; a stale or malformed selection
  // must never point at a different occurrence of the same quotation.
  const valid=value&&chars.slice(value.source.start,value.source.end).join('')===value.source.quote
  return <section className="pdf-source-reader text-source-reader" aria-label="来源原文">
    <div className="panel-heading"><h4>{value?.source.title||'来源原文'}{value?` · 原版本 ${value.source.revision}`:''}</h4><button type="button" className="secondary" onClick={onClose}>{closeLabel}</button></div>
    {query.isPending?<p>正在验证原文与来源权限…</p>:query.error?<><p className="error" role="alert">{query.error instanceof Error?query.error.message:'来源已不可用'}</p><button type="button" className="text-button" onClick={()=>void query.refetch()}>重新读取原文</button></>:value&&valid?<>
      {value.source_changed?<p className="chat-warning" role="status">来源笔记已更新。下方显示提问时的原版本 {value.source.revision}，高亮对应当时的选段。</p>:<p className="muted">原笔记的已保存版本，选段已高亮。</p>}
      <button type="button" className="secondary" onClick={()=>highlight.current?.scrollIntoView({block:'center',behavior:'smooth'})}>定位到原文选段</button>
      {onOpenNote?<button type="button" className="text-button" onClick={()=>onOpenNote(value.source.note_id)}>打开当前原笔记</button>:null}
      <div className="text-source-body" tabIndex={0} aria-label="原版本正文">{chars.slice(0,value.source.start).join('')}<mark ref={highlight}>{value.source.quote}</mark>{chars.slice(value.source.end).join('')}</div>
    </>:value?<p className="error" role="alert">来源记录与原文不一致，无法定位选段。</p>:null}
  </section>
}
