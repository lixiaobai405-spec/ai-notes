import { useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, date } from '../../api'
import type { components } from '../../api.generated'
import { SourceReader, type SourceRef } from './PdfSources'
import { pageRange } from './pdf-pages'
import { describeRegion } from './image-region'
import './ai.css'

type Provenance=components['schemas']['SavedAnswerProvenanceOut']
type Page=SourceRef

export function SavedAnswerSource({owner,noteId}:{owner:string;noteId:string}) {
  const provenance=useQuery({queryKey:['notes',owner,'ai-answer',noteId],queryFn:()=>api<Provenance|null>(`/notes/${noteId}/ai-answer`),retry:false})
  const [page,setPage]=useState<Page|null>(null)
  const trigger=useRef<HTMLElement|null>(null)
  function openSource(value:Page){trigger.current=document.activeElement as HTMLElement;setPage(value)}
  function closeSource(){setPage(null);trigger.current?.focus();trigger.current?.scrollIntoView({block:'center'})}
  if(provenance.error)return <div className="saved-answer-source"><p className="error" role="alert">{provenance.error instanceof Error?provenance.error.message:'无法读取 AI 来源信息。'}</p><button type="button" className="text-button" onClick={()=>void provenance.refetch()}>重新读取来源信息</button></div>
  const value=provenance.data
  if(!value)return null
  const source=value.source
  const image=source?.kind==='image'
  const region=source?.kind==='image'?source.pages[0]?.region:null
  const cited=source?.pages.filter(item=>value.cited_page_numbers.includes(item.number))||[]
  const model=value.model==='deepseek-flash'?'DeepSeek Flash':value.model==='deepseek-v4-pro'?'DeepSeek V4 Pro':value.model
  return <section className="saved-answer-source" aria-label="保存回答的来源">
    <h4>AI 回答来源 · {model}</h4><p className="muted">保存于 {date(value.saved_at)}。正文可自由编辑，原始来源记录保留。</p>
    {source?<><p><strong>{source.title}</strong> · 来源版本 {source.revision} · {image?(source.mode==='region'?'框选局部':'整张图片'):(source.mode==='all'?'整本 PDF':'指定页')} · {image?'图片':'原 PDF'} 第 {pageRange(source.pages.map(item=>item.number))} {image?'张':'页'}</p>
      {region?<p>{describeRegion(region)}</p>:null}
      {cited.length?<><p>回答引用的原页：</p><div className="pdf-page-links">{cited.map(item=><button type="button" className="citation-link" key={item.page_id} onClick={()=>openSource(item)}>{image?'查看来源图片第':'查看原 PDF 第'} {item.number} {image?'张':'页'}</button>)}</div></>:<p className="muted">原回答未标出具体引用页，可展开本次全部资料。</p>}
      <details><summary>查看本次全部资料（{source.pages.length} {image?'张':'页'}）</summary><div className="pdf-page-links">{source.pages.map(item=><button type="button" className="text-button" key={item.page_id} onClick={()=>openSource(item)}>{image?'查看来源图片第':'查看原 PDF 第'} {item.number} {image?'张':'页'}</button>)}</div></details>
      {page?<SourceReader owner={owner} runId={value.run_id} page={page} onClose={closeSource} closeLabel="返回笔记"/>:null}
    </>:<p className="muted">本次为纯文字对话，没有附加图像资料。</p>}
  </section>
}
