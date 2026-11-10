import {ModelPicker} from './ModelPicker'
import { ChevronDown, MessageSquare, Paperclip, Plus, Send } from 'lucide-react'
import {defaultModel,modelReady,modelLabel,type ModelChoice} from './model-catalog'
import {ToolScopePicker,emptyToolDraft,type ToolDraft} from './ToolScopePicker'
import {ToolHistory} from './ToolHistory'
import { createUuid } from '../../uuid'
import { useEffect, useLayoutEffect, useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, date, ApiError, type AiCatalog } from '../../api'
import { active, readStream, type Conversation, type Detail, type Run } from './chat-stream'
import { CitedAnswer, PdfPicker, PdfRunScope, SourceReader, emptyChoice, type PdfChoice, type PdfBook, type PdfDetail, type PdfInput, type PdfPreview } from './PdfSources'
import { buildPdfInput, pdfReady as pdfSelectionReady, restorePdfChoice, type PdfPayload } from './pdf-choice'
import { pageRange } from './pdf-pages'
import { SaveAnswer, type SavedAnswer } from './SaveAnswer'
import { ImagePicker, emptyImageChoice, type ImageChoice, type ImageBook, type ImageDetail, type ImageInput, type ImagePreview } from './ImageSources'
import { TextSelectionScope, TextSourceReader, type TextSource } from './TextSources'
import type { AskTextSelection, TextSelectionInput } from '../notes/text-selection'
import type { SourceRef } from './PdfSources'
import { authSession } from '../../session'
import './ai.css'

const labels:Record<Run['status'],string>={queued:'等待生成',running:'正在回复',succeeded:'已完成',cancelled:'已取消',interrupted:'回复中断',failed:'生成失败',truncated:'达到输出上限'}
const errors:Record<string,string>={history_tool_source_unavailable:'历史工具来源已变化或不可用。请核对已授权资料，并新建对话后重新预览范围；本次不会自动发送。',tool_preview_changed:'授权资料或学习统计已变化，尚未创建模型运行；请重新预览并确认工具范围。',visual_tools_unsupported:'图片 / PDF 暂不支持工具；请关闭工具后主动重新发送。',tool_source_changed:'授权资料或学习统计已变化，已停止后续调用，请重新预览范围。',tool_call_rejected:'模型请求的工具参数被拒绝，未自动重发。',tool_scope_rejected:'模型请求超出授权范围，已拒绝。',tool_budget_exceeded:'已达到本次工具调用上限。',provider_changed:'服务商配置已变化，本次停止；请重新核对后手动发送。',credential_rejected:'共享模型凭据不可用，请联系管理员。',provider_balance:'共享模型账户余额不足，请联系管理员。',provider_rate_limited:'模型服务繁忙，请稍后主动发送。',provider_unavailable:'模型服务暂时不可用。',provider_unreachable:'未能连接模型服务。',provider_timeout:'模型响应超时。',provider_invalid_response:'模型返回了无法处理的响应。',provider_incomplete:'模型未完整返回结果。',worker_interrupted:'服务曾中断，本次不会自动重新生成。',output_limit:'回复达到长度上限，以下内容不完整。',account_disabled:'账户已停用。',ai_not_configured:'共享模型服务尚未配置。'}
function message(error:unknown) {return error instanceof ApiError&&errors[error.code]?errors[error.code]:error instanceof Error ? error.message : '操作失败，请重新读取'}
const pdfErrors:Record<string,string>={pdf_source_unavailable:'来源 PDF 已删除、页面已失效或权限不可用，已停止后续调用。',pdf_storage_unavailable:'读取页图时存储暂不可用，未自动重发模型请求。',pdf_context_too_large:'资料超过本次处理大小上限，请选择更少页面或缩小选区。',pdf_region_too_small:'选区至少需要 8 × 8 像素，请扩大选区并核对预览。',pdf_summary_too_large:'阅读记录超过汇总预算，本次未完成全部处理。',pdf_summary_truncated:'部分阅读记录被模型截断，本次未完成全部处理。',pdf_plan_invalid:'处理计划异常，本次已停止。'}
const imageErrors:Record<string,string>={image_source_unavailable:'来源图片已删除、发生变化或权限不可用，已停止本次调用。',image_storage_unavailable:'读取图片时存储暂不可用，未自动重发模型请求。',image_context_too_large:'本次图片超过处理上限，请缩小选区或使用较小图片。',image_region_too_small:'选区至少需要 8 × 8 像素，请扩大选区并核对预览。'}

export default function AiChat({owner,onOpenNote,initialTextSelection,onTextSelectionConsumed}:{owner:string;onOpenNote?:(noteId:string)=>void;initialTextSelection?:AskTextSelection|null;onTextSelectionConsumed?:(requestKey:string)=>void}) {
  const client=useQueryClient()
  const mounted=useRef(false),selectionRequest=useRef(initialTextSelection)
  const [sessionTicket]=useState(()=>authSession.ticket())
  selectionRequest.current=initialTextSelection
  useLayoutEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[])
  const storageKey=`ai-notes:chat:${owner}`
  const [selected,setSelected]=useState(()=>{try{return sessionStorage.getItem(storageKey)||''}catch{return ''}})
  const [prompt,setPrompt]=useState('')
  const [historyOpen,setHistoryOpen]=useState(()=>typeof window==='undefined'||typeof window.matchMedia!=='function'?true:window.matchMedia('(min-width:1001px)').matches)
  useEffect(()=>{
    if(typeof window==='undefined'||typeof window.matchMedia!=='function')return
    const viewport=window.matchMedia('(min-width:1001px)')
    const update=(event:MediaQueryListEvent)=>setHistoryOpen(event.matches)
    viewport.addEventListener('change',update)
    return()=>viewport.removeEventListener('change',update)
  },[])
  const [choice,setChoice]=useState<ModelChoice>(defaultModel)
  const {provider,model}=choice
  const [toolDraft,setToolDraft]=useState<ToolDraft>(emptyToolDraft)
  const [streamError,setStreamError]=useState('')
  const [uncertain,setUncertain]=useState(false)
  const [readVersion,setReadVersion]=useState(0)
  const [pdfChoice,setPdfChoice]=useState<PdfChoice>(emptyChoice)
  const [material,setMaterial]=useState<'text'|'pdf'|'image'|'selection'>('text')
  const [materialOpen,setMaterialOpen]=useState(false)
  const [textChoice,setTextChoice]=useState<AskTextSelection|null>(null)
  const [textSourceRun,setTextSourceRun]=useState('')
  const initialRequested=useRef('')
  const [imageChoice,setImageChoice]=useState<ImageChoice>(emptyImageChoice)
  const [loadedImage,setLoadedImage]=useState('')
  const [loadedPdfImage,setLoadedPdfImage]=useState('')
  const [sourceView,setSourceView]=useState<{run:Run;page:SourceRef}|null>(null)
  const sourceTrigger=useRef<HTMLElement|null>(null),restoredSource=useRef('')
  const pending=useRef<{conversation:string;key:string;prompt:string;model:string;provider:string;tools:string;previous:string|null;pdf:PdfInput|null;image:ImageInput|null;text:TextSelectionInput|null}|null>(null)
  const newKey=useRef(createUuid())
  const sending=useRef(false)
  const conversations=useQuery({queryKey:['ai-conversations',owner],queryFn:()=>api<Conversation[]>('/ai/conversations')})
  const catalog=useQuery({queryKey:['ai-catalog',owner],queryFn:()=>api<AiCatalog>('/ai/providers')})
  const books=useQuery({queryKey:['ai-pdf-books',owner],queryFn:()=>api<PdfBook[]>('/ai/pdf-notebooks'),enabled:material==='pdf'})
  const book=useQuery({queryKey:['ai-pdf-book',owner,pdfChoice.noteId],queryFn:()=>api<PdfDetail>('/ai/pdf-notebooks/'+pdfChoice.noteId),enabled:material==='pdf'&&!!pdfChoice.noteId,retry:false})
  const imageBooks=useQuery({queryKey:['ai-image-books',owner],queryFn:()=>api<ImageBook[]>('/ai/image-notebooks'),enabled:material==='image'})
  const imageBook=useQuery({queryKey:['ai-image-book',owner,imageChoice.noteId],queryFn:()=>api<ImageDetail>('/ai/image-notebooks/'+imageChoice.noteId),enabled:material==='image'&&!!imageChoice.noteId,retry:false})
  let pdf:PdfPayload|null=null,pdfInputError=''
  if(material==='pdf'&&pdfChoice.noteId && book.data?.id===pdfChoice.noteId) {
    try{pdf=buildPdfInput(pdfChoice,book.data)}catch(error){pdfInputError=message(error)}
  }
  const preview=useQuery({queryKey:['ai-pdf-preview',owner,pdf],queryFn:()=>api<PdfPreview>('/ai/pdf-preview','POST',pdf),enabled:!!pdf&&!book.isFetching,retry:false,staleTime:30000})
  const pdfIsReady=pdfSelectionReady({input:pdf,choice:pdfChoice,book:book.data,preview:preview.data,fetching:book.isFetching||preview.isFetching,error:book.error||preview.error,loadedImage:loadedPdfImage})
  const pdfReady=material!=='pdf'||pdfIsReady
  const imagePage=imageBook.data?.id===imageChoice.noteId?imageBook.data.pages.find(page=>page.page_id===imageChoice.pageId):undefined
  let image:ImageInput|null=null
  if(material==='image'&&imageChoice.noteId&&imagePage&&imageBook.data&&(imageChoice.mode==='page'||imageChoice.region))image={note_id:imageChoice.noteId,revision:imageBook.data.revision,page_id:imagePage.page_id,region:imageChoice.mode==='region'?imageChoice.region:null}
  const imagePreview=useQuery({queryKey:['ai-image-preview',owner,image],queryFn:()=>api<ImagePreview>('/ai/image-preview','POST',image),enabled:!!image,retry:false,staleTime:30000})
  const imageReady=material!=='image'||!!(image&&imagePage&&loadedImage===imagePage.file_id&&imagePreview.data&&!imagePreview.isFetching&&!imagePreview.error)
  const text=material==='selection'?textChoice?.input||null:null
  const textReady=material!=='selection'||!!text
  const textSource:TextSource|null=text&&textChoice?{kind:'text',...text,title:textChoice.title}:null
  const pdfError=pdfInputError||((book.error||preview.error)?message(book.error||preview.error):'')
  const imageError=imageBook.error||imagePreview.error?message(imageBook.error||imagePreview.error):''
  const pdfPreview=pdfIsReady?preview.data:undefined
  const pdfTitle=books.data?.find(item=>item.id===pdfChoice.noteId)?.title||book.data?.title
  const imageTitle=imageBooks.data?.find(item=>item.id===imageChoice.noteId)?.title||imageBook.data?.title
  const detailKey=['ai-conversation',owner,selected]
  const detail=useQuery({queryKey:detailKey,queryFn:()=>api<Detail>('/ai/conversations/'+selected),enabled:!!selected})
  const savedNotes=useQuery({queryKey:['ai-saved-notes',owner,selected],queryFn:()=>api<SavedAnswer[]>(`/ai/conversations/${selected}/saved-notes`),enabled:!!selected,retry:false})
  const savedByRun=new Map(savedNotes.data?.map(row=>[row.run_id,row]))
  const latest=detail.data?.runs.at(-1)
  const busy=active(latest)
  const toolContext=JSON.stringify({provider,model,material,pdf,image,text})
  const toolsReady=!toolDraft.selection.tools.length||toolDraft.confirmed&&toolDraft.contextKey===toolContext&&!!toolDraft.preview
  const capable=modelReady(catalog.data,choice,{vision:material==='pdf'||material==='image',tools:!!toolDraft.selection.tools.length})
  const toolSignature=JSON.stringify({selection:toolDraft.selection,context:toolContext,preview:toolDraft.preview?.preview_hash})
  const previousToolContext=useRef(toolContext)
  useEffect(()=>{
    if(previousToolContext.current!==toolContext){previousToolContext.current=toolContext;setToolDraft(old=>({...old,preview:null,confirmed:false,contextKey:toolContext}))}
  },[toolContext])
  useEffect(()=>{pending.current=null},[toolSignature])

  function choose(id:string) {
    if(id===selected)return
    restoredSource.current=''
    setToolDraft(emptyToolDraft)
    setSelected(id);setPrompt('');pending.current=null;setStreamError('')
    setPdfChoice(emptyChoice);setImageChoice(emptyImageChoice);setTextChoice(null);setTextSourceRun('');setMaterial('text');setMaterialOpen(false);setLoadedImage('');setLoadedPdfImage('');setSourceView(null)
    try{sessionStorage.setItem(storageKey,id)}catch{/* Browser storage is optional. */}
  }
  useEffect(()=>{
    if (!selected && !initialTextSelection && conversations.data?.length) choose(conversations.data[0].id)
  },[conversations.data,selected])
  useEffect(()=>{
    if(!selected||!detail.data||detail.data.id!==selected||restoredSource.current===selected)return
    restoredSource.current=selected
    const source=detail.data.runs.at(-1)?.source
    if(source?.kind==='image'){const page=source.pages[0];setMaterial('image');setImageChoice({noteId:source.note_id,pageId:page.page_id,mode:source.mode,region:page.region||null,sourceRevision:source.revision})}
    else if(source?.kind==='text'){const {note_id,revision,start,end,quote}=source;setMaterial('selection');setTextChoice({requestKey:'',input:{note_id,revision,start,end,quote},title:source.title})}
    else if(source){setMaterial('pdf');setLoadedPdfImage('');setPdfChoice(restorePdfChoice(source))}
  },[selected,detail.data])

  useEffect(()=>{
    if (!latest || !active(latest)) return
    const controller=new AbortController()
    const id=latest.id
    setStreamError('')
    readStream(id,controller.signal,(name,data)=>{
      client.setQueryData<Detail>(['ai-conversation',owner,selected],old=>{
        if(!old)return old
        return {...old,runs:old.runs.map(run=>{
          if(run.id!==id)return run
          if(name==='meta'||name==='done')return data as Run
          if(name==='state')return {...run,...data as Partial<Run>}
          if(name==='delta') {
            const delta=data as {offset:number;text:string}
            // Python offsets count Unicode code points, including emoji as one.
            if(Array.from(run.answer).length!==delta.offset)throw new Error('回复同步中断，请重新读取')
            return {...run,answer:run.answer+delta.text}
          }
          return run
        })}
      })
    }).catch(error=>{if(!controller.signal.aborted)setStreamError(message(error))})
    return ()=>controller.abort()
  },[latest?.id,busy,owner,selected,readVersion,client])

  const create=useMutation({mutationFn:(selection?:AskTextSelection)=>api<Conversation>('/ai/conversations','POST',{request_key:selection?.requestKey||newKey.current}),onSuccess:(row,selection)=>{
    if(!mounted.current||!authSession.current(sessionTicket,owner)||selection&&selectionRequest.current?.requestKey!==selection.requestKey)return
    newKey.current=createUuid();choose(row.id)
    // The source belongs to this freshly created conversation, never the one
    // that happened to be selected before leaving the note editor.
    restoredSource.current=row.id
    if(selection){setTextChoice(selection);setMaterial('selection');onTextSelectionConsumed?.(selection.requestKey)}
    client.setQueryData<Detail>(['ai-conversation',owner,row.id],{...row,runs:[]})
    client.invalidateQueries({queryKey:['ai-conversations',owner]})
  }})
  useEffect(()=>{
    let cancelled=false
    // StrictMode replays mount effects and unsubscribes mutation observers.
    // Start only after that replay, so the observer receives the terminal state.
    queueMicrotask(()=>{if(!cancelled&&initialTextSelection&&initialRequested.current!==initialTextSelection.requestKey){initialRequested.current=initialTextSelection.requestKey;create.mutate(initialTextSelection)}})
    return()=>{cancelled=true}
  },[initialTextSelection])
  const send=useMutation({mutationFn:async()=>{
    if(!toolsReady||!capable)throw new Error('请重新核对模型能力及工具范围。')
    const previous=latest?.id||null
    if (!pending.current || pending.current.conversation!==selected || pending.current.prompt!==prompt || pending.current.model!==model || pending.current.provider!==provider || pending.current.tools!==toolSignature || JSON.stringify(pending.current.pdf)!==JSON.stringify(pdf) || JSON.stringify(pending.current.image)!==JSON.stringify(image) || JSON.stringify(pending.current.text)!==JSON.stringify(text)) {
      pending.current={conversation:selected,key:createUuid(),prompt,model,provider,tools:toolSignature,previous,pdf,image,text}
    }
    const p=pending.current
    return api<Run>(`/ai/conversations/${p.conversation}/runs`,'POST',{request_key:p.key,prompt:p.prompt,model:p.model,provider:p.provider,...toolDraft.selection,...(toolDraft.selection.tools.length?{tool_preview_hash:toolDraft.preview!.preview_hash}:{}),previous_run_id:p.previous,pdf:p.pdf,image:p.image,text:p.text})
  },onSuccess:run=>{
    if(!mounted.current||!authSession.current(sessionTicket,owner))return
    pending.current=null;setUncertain(false);setPrompt('')
    client.setQueryData<Detail>(['ai-conversation',owner,run.conversation_id],old=>old?{...old,title:old.runs.length?old.title:run.prompt.trim().slice(0,64),runs:old.runs.some(r=>r.id===run.id)?old.runs.map(r=>r.id===run.id?run:r):[...old.runs,run]}:old)
    client.invalidateQueries({queryKey:['ai-conversations',owner]})
  },onError:error=>{
    if(!mounted.current||!authSession.current(sessionTicket,owner))return
    setUncertain(!(error instanceof ApiError)||error.status>=500||error.status===408)
    if(error instanceof ApiError&&error.code==='tool_preview_changed')setToolDraft(old=>({...old,preview:null,confirmed:false}))
    if(error instanceof ApiError && error.status<500 && error.status!==408){
      pending.current=null
      if(error.status===409){client.invalidateQueries({queryKey:detailKey});client.invalidateQueries({queryKey:['ai-pdf-book',owner]});client.invalidateQueries({queryKey:['ai-pdf-preview',owner]});client.invalidateQueries({queryKey:['ai-image-book',owner]});client.invalidateQueries({queryKey:['ai-image-preview',owner]})}
    }
  },onSettled:()=>{sending.current=false}})
  const cancel=useMutation({mutationFn:(id:string)=>api<Run>(`/ai/runs/${id}/cancel`,'POST',{}),onSuccess:run=>{
    client.setQueryData<Detail>(['ai-conversation',owner,run.conversation_id],old=>old?{...old,runs:old.runs.map(r=>r.id===run.id?run:r)}:old)
  }})
  function submit(event:FormEvent) {
    event.preventDefault()
    if(sending.current||busy||create.isPending||!!initialTextSelection||!prompt.trim()||!pdfReady||!imageReady||!textReady||!toolsReady||!capable)return
    sending.current=true;send.mutate()
  }
  function openSource(run:Run,page:SourceRef){sourceTrigger.current=document.activeElement as HTMLElement;setTextSourceRun('');setSourceView({run,page})}
  function openTextSource(runId:string){sourceTrigger.current=document.activeElement as HTMLElement;setSourceView(null);setTextSourceRun(runId)}
  function closeTextSource(){setTextSourceRun('');sourceTrigger.current?.focus();sourceTrigger.current?.scrollIntoView({block:'center'})}
  function closeSource(){setSourceView(null);sourceTrigger.current?.focus();sourceTrigger.current?.scrollIntoView({block:'center'})}
  function changeMaterial(value:'text'|'pdf'|'image'|'selection'){setMaterial(value);setPdfChoice(emptyChoice);setImageChoice(emptyImageChoice);setLoadedImage('');setLoadedPdfImage('');pending.current=null;}
  const error=detail.error||conversations.error||catalog.error||create.error||send.error||cancel.error||(material==='pdf'?books.error:material==='image'?imageBooks.error:null)
  return <div className="chat-layout">
    <aside className="chat-history" aria-label="会话历史"><button type="button" className="secondary full chat-new-conversation" disabled={create.isPending||send.isPending} onClick={()=>create.mutate(undefined)}><Plus size={17} aria-hidden="true"/>新建对话</button>
      <details className="chat-history-list" open={historyOpen} onToggle={event=>setHistoryOpen(event.currentTarget.open)}><summary><span>我的对话</span><span className="chat-history-count">{conversations.data?.length||0}</span><ChevronDown size={16} aria-hidden="true"/></summary><nav aria-label="AI 对话历史">
      {conversations.isPending?<p className="muted">正在读取…</p>:null}
      {conversations.data?.map(row=><button type="button" className={'chat-history-item '+(row.id===selected?'selected':'')} aria-current={row.id===selected?'page':undefined} key={row.id} disabled={send.isPending||create.isPending} onClick={()=>choose(row.id)}><strong>{row.title}</strong><small>{date(row.created_at)}</small></button>)}
      {!conversations.data?.length?<p className="muted">新建一段对话，开始提问。</p>:null}
      </nav></details>
    </aside>
    <section className="panel chat-main"><div className="panel-heading chat-thread-heading"><div><h3>{detail.data?.title||'和 AI 一起理解'}</h3><p className="muted">共享模型 · 历史仅自己可见 · 文字、PDF 与图片局部提问</p></div></div>
      {error?<div className="error" role="alert">{message(error)}</div>:null}
      {initialTextSelection&&create.isError?<button type="button" className="secondary" onClick={()=>create.mutate(initialTextSelection)}>重试创建选段对话</button>:null}
      {initialTextSelection&&create.isPending?<p role="status">正在为所选原文创建对话…</p>:null}
      {catalog.data&&!catalog.data.platform_enabled?<p className="error">共享模型服务尚未配置，请联系管理员。</p>:null}
      {selected&&detail.isPending?<p>正在打开对话…</p>:null}
      {savedNotes.error?<div className="error" role="alert">无法核对已保存的笔记：{message(savedNotes.error)}<button type="button" className="text-button" onClick={()=>void savedNotes.refetch()}>重新核对保存状态</button></div>:null}
      {!selected?<div className="chat-start"><MessageSquare size={28} strokeWidth={1.5} aria-hidden="true"/><strong>从一个问题开始</strong><p>点击“新建对话”，再输入问题或选择要引用的资料。</p></div>:null}
      <div className="chat-messages" aria-label="对话记录">{detail.data?.runs.map(run=><article className="chat-turn" key={run.id}>
        <div className="chat-question"><small>你</small><p>{run.prompt}</p></div>
        {run.source?.kind==='text'?<TextSelectionScope source={run.source} onOpen={()=>openTextSource(run.id)}/>:<PdfRunScope run={run} onOpen={openSource}/>}
        <div className="chat-answer"><small>{modelLabel(run.provider,run.model)} · <span role="status">{labels[run.status]}</span></small>
          <div className="chat-answer-text">{run.answer?<CitedAnswer run={run} onOpen={openSource}/>:(active(run)?(run.source?'正在读取所选资料，完成后显示回答…':'正在准备回复…'):'本次没有生成完整正文。')}</div>
          {run.error_code?<p className="chat-warning">{errors[run.error_code]||pdfErrors[run.error_code]||imageErrors[run.error_code]||(run.error_code==='text_source_unavailable'?'原文已删除、历史版本缺失或来源不可用，已停止本次调用。':'本次回复未完成。')}</p>:null}
          {run.usage?.total_tokens!==undefined?<small>{run.source&&run.source.kind!=='text'?'已知用量':'用量'}：{run.usage.total_tokens} tokens{run.source&&run.source.kind!=='text'&&!run.progress?.usage_complete?' · 尚有调用未返回完整用量':''}</small>:!active(run)&&(run.status!=='succeeded'||!!run.tools?.length)?<small>本次用量未完整返回；已产生的用量仍可能计费。</small>:null}
          <ToolHistory owner={owner} runId={run.id} active={active(run)} onOpenNote={onOpenNote}/>
          <SaveAnswer owner={owner} run={run} saved={savedByRun.get(run.id)} savedReady={savedNotes.data!==undefined&&!savedNotes.error} onOpenNote={onOpenNote}/>
          {sourceView?.run.id===run.id?<SourceReader owner={owner} runId={run.id} page={sourceView.page} onClose={closeSource}/>:null}
          {textSourceRun===run.id?<TextSourceReader owner={owner} runId={run.id} onClose={closeTextSource} onOpenNote={onOpenNote}/>:null}
        </div></article>)}</div>
      {streamError?<div className="error" role="alert">{streamError}<button className="text-button" onClick={()=>{detail.refetch();setReadVersion(v=>v+1)}}>重新读取</button></div>:null}
      {selected?<form className="chat-composer" onSubmit={submit}>
        <label className="chat-prompt">提问<textarea value={prompt} onChange={e=>setPrompt(e.target.value)} maxLength={8000} rows={3} placeholder="输入你的问题" disabled={create.isPending||busy||send.isPending||detail.isError}/></label>
        <details className="chat-materials" open={materialOpen} onToggle={event=>setMaterialOpen(event.currentTarget.open)}>
          <summary><Paperclip size={16} aria-hidden="true"/>{material==='text'?'添加资料':'更换资料'}<ChevronDown size={14} aria-hidden="true"/></summary>
          <div className="chat-material-options">
            <label className="material-choice">资料类型<select aria-label="资料类型" value={material} disabled={create.isPending||busy||send.isPending} onChange={event=>changeMaterial(event.target.value as typeof material)}>
              <option value="text">仅文字</option>
              {textChoice?<option value="selection">笔记原文选段</option>:null}
              <option value="pdf">PDF 选页 / 整本 / 单页框选</option>
              <option value="image">普通图片 / 框选局部</option>
            </select></label>
            {textSource?<TextSelectionScope source={textSource}/>:null}
            <p className="muted">引用新选段时，在「我的笔记」打开已保存的文字笔记，选中文字后点击「就选段提问」。</p>
            {textSource&&onOpenNote?<button type="button" className="text-button" onClick={()=>onOpenNote(textSource.note_id)}>打开原笔记重新选段</button>:null}
            {material==='pdf'?<PdfPicker choice={pdfChoice} onChange={setPdfChoice} books={books.data||[]} book={book.data} preview={pdfPreview} loading={!!pdfChoice.noteId&&(book.isFetching||!!pdf&&preview.isFetching)} error={pdfError} disabled={create.isPending||busy||send.isPending} onReady={setLoadedPdfImage} loadedImage={loadedPdfImage}/>:null}
            {material==='image'?<ImagePicker choice={imageChoice} onChange={setImageChoice} books={imageBooks.data||[]} book={imageBook.data} preview={imagePreview.data} loading={!!imageChoice.noteId&&(imageBook.isPending||!!image&&imagePreview.isFetching)} error={imageError} disabled={create.isPending||busy||send.isPending} onReady={setLoadedImage}/>:null}
          </div>
        </details>
        {textSource?<div className="chat-material-status">
          {!materialOpen?<strong>笔记选段 · {textSource.title} · 版本 {textSource.revision} · {Array.from(textSource.quote).length} 字</strong>:null}
          <p>本次仅向所选模型发送所选原文和本对话历史。已引用的选段可继续按原版本提问。</p>
        </div>:null}
        {!materialOpen&&material==='pdf'?<div className="chat-material-status">
          <strong>PDF{pdfTitle?' · '+pdfTitle:''}</strong>
          {pdfPreview?<p role="status">{pdfChoice.mode==='all'?'整本 · ':''}原 PDF 第 {pageRange(pdfPreview.source.pages.map(page=>page.number))} 页 · 将{pdfChoice.mode==='region'?'框选区域':'所选页图'}和文字对话发送给所选模型，计划调用 {pdfPreview.planned_calls} 次。</p>:<p role="status">{pdfError||'展开资料选项核对 PDF 和提问范围。'}</p>}
        </div>:null}
        {!materialOpen&&material==='image'?<div className="chat-material-status">
          <strong>图片{imageTitle?' · '+imageTitle:''}</strong>
          {imageReady&&imagePage&&imagePreview.data?<p role="status">第 {imagePage.number} 张 · 将{imageChoice.mode==='region'?'框选区域':'整张图片'}和文字对话发送给所选模型，计划调用 {imagePreview.data.planned_calls} 次。</p>:<p role="status">{imageError||'展开资料选项核对图片和提问范围。'}</p>}
          {imageChoice.sourceRevision!==undefined&&imageBook.data&&imageChoice.sourceRevision!==imageBook.data.revision?<p className="chat-warning" role="status">来源笔记已更新，请展开资料选项核对当前图片和选区。</p>:null}
        </div>:null}
        {uncertain?<p className="chat-warning" role="alert">原提交结果暂时未知，可能已计费。先重新读取本对话核对；不会自动重发。参数保持相同时，主动再次发送沿用原请求编号；更改参数后属于新请求。</p>:null}
        <div className="chat-controls"><ModelPicker catalog={catalog.data} value={choice} onChange={setChoice} vision={material==='pdf'||material==='image'} tools={!!toolDraft.selection.tools.length} disabled={create.isPending||busy||send.isPending} label="对话模型"/>
          {busy&&latest?<button type="button" className="secondary" disabled={cancel.isPending} onClick={()=>cancel.mutate(latest.id)}>停止生成</button>:<button className="primary" disabled={create.isPending||!!initialTextSelection||send.isPending||!prompt.trim()||!detail.data||!catalog.data?.platform_enabled||!pdfReady||!imageReady||!textReady||!toolsReady||!capable}><Send size={16} aria-hidden="true"/>{send.isPending?'正在提交…':'发送'}</button>}</div>
        {!toolsReady?<p className="muted chat-tool-status" role="status">请展开只读工具，预览并确认本次授权范围后发送。</p>:null}
        <ToolScopePicker owner={owner} contextKey={toolContext} value={toolDraft} onChange={setToolDraft} disabled={create.isPending||busy||send.isPending}/>
        <div className="chat-composer-footer"><p className="muted chat-hint">每次最多 8000 字 · 每小时 30 次</p><details className="chat-usage-help"><summary>使用说明</summary><p className="muted">刷新或切换页面后可查看原回复；取消会保留已保存的部分内容。AI 回答请自行核对。</p></details></div>
      </form>:null}
    </section>
  </div>
}
