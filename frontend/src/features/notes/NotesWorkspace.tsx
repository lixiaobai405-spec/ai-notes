import { useDeferredValue,useEffect,useLayoutEffect,useRef,useState } from 'react'
import { useQuery,useQueryClient } from '@tanstack/react-query'
import { ChevronDown,Download,FileText,FileUp,FolderTree,Image as ImageIcon,MoreHorizontal,Plus,Search,SlidersHorizontal,Trash2,X } from 'lucide-react'
import { api,ApiError,date } from '../../api'
import { rememberNote,selectedNote } from './drafts'
import { NoteDetail } from './NoteDetail'
import { NoteError } from './NoteError'
import { OrganizationManager } from './OrganizationManager'
import type { Folder,Notebook,Tag,Note,NoteList,NoteSummary } from './types'
import { PdfImportForm } from '../files/PdfImport'
import { ExportPanel } from '../exports/ExportPanel'
import { SearchResults } from '../search/SearchResults'
import { SemanticResults } from '../search/SemanticResults'
import type { AskTextSelection } from './text-selection'
import {ReviewJoin} from '../learning/ReviewJoin'
import {useLearningActivity} from '../learning/ActivityTracker'
import './notes.css'
const foldersEmpty:Folder[]=[],notebooksEmpty:Notebook[]=[],tagsEmpty:Tag[]=[]
type WorkspacePanel='create'|'pdf'|'organization'|'export'|null
export default function NotesWorkspace({owner,initialNoteId,onAskText,onGoReview}:{owner:string;initialNoteId?:string;onGoReview?:(planId?:string)=>void;onAskText?:(selection:AskTextSelection)=>void}) {
  const client=useQueryClient()
  const mounted=useRef(true)
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[])
  const folders=useQuery({queryKey:['notes',owner,'folders'],queryFn:()=>api<Folder[]>('/folders')})
  const notebooks=useQuery({queryKey:['notes',owner,'notebooks'],queryFn:()=>api<Notebook[]>('/notebooks')})
  const tags=useQuery({queryKey:['notes',owner,'tags'],queryFn:()=>api<Tag[]>('/tags')})
  const [filters,setFilters]=useState({q:'',kind:'',folder_id:'',notebook_id:'',tag_id:'',deleted:false})
  const [cursors,setCursors]=useState<string[]>(['']),[selected,setSelected]=useState(()=>initialNoteId||selectedNote(owner)),[newTitle,setNewTitle]=useState(''),[newKind,setNewKind]=useState<'text'|'image'>('text'),[busy,setBusy]=useState(false),[error,setError]=useState<unknown>(null)
  const [searchMode,setSearchMode]=useState<'keyword'|'semantic'>('keyword')
  const [pdfBusy,setPdfBusy]=useState(false),[filtersOpen,setFiltersOpen]=useState(false)
  const [menu,setMenu]=useState<'new'|'more'|null>(null),[panel,setPanel]=useState<WorkspacePanel>(null)
  const newEntry=useRef<HTMLDivElement>(null),moreEntry=useRef<HTMLDivElement>(null)
  const newTrigger=useRef<HTMLButtonElement>(null),moreTrigger=useRef<HTMLButtonElement>(null),filterTrigger=useRef<HTMLButtonElement>(null)
  const panelTrigger=useRef<HTMLButtonElement|null>(null)
  const titleInput=useRef<HTMLInputElement>(null)
  const workspace=useRef<HTMLDivElement>(null)
  const deferredQ=useDeferredValue(filters.q)
  const globalSearch=!filters.deleted&&(searchMode==='semantic'||!!filters.q.trim())
  const params=new URLSearchParams({limit:'20',deleted:String(filters.deleted)})
  for(const [key,value] of Object.entries({...filters,q:deferredQ}))if(key!=='deleted'&&typeof value==='string'&&value)params.set(key,value)
  const cursor=cursors[cursors.length-1];if(cursor)params.set('cursor',cursor)
  const notes=useQuery({queryKey:['notes',owner,'list',params.toString()],queryFn:()=>api<NoteList>('/notes?'+params),enabled:!globalSearch})
  const detail=useQuery({queryKey:['notes',owner,'detail',selected],queryFn:()=>api<Note>('/notes/'+selected),enabled:!!selected})
  const imageDetail=detail.data?.kind==='image'
  useLayoutEffect(()=>{
    const entry=workspace.current
    if(imageDetail&&entry&&entry.getBoundingClientRect().top<0)entry.scrollIntoView({block:'start'})
  },[selected,imageDetail])
  useLearningActivity('reading',selected&&detail.data?.id===selected&&!detail.data.deleted_at&&!detail.isError?{note_id:selected}:null)
  useEffect(()=>rememberNote(owner,selected),[owner,selected])
  useEffect(()=>{if(selected&&((detail.error instanceof ApiError&&detail.error.status===404)||(detail.data?.deleted_at&&!filters.deleted))){setSelected('');setError(new Error('上次打开的笔记已删除或不存在，已返回列表。'))}},[selected,detail.error,detail.data?.deleted_at,filters.deleted])
  useEffect(()=>{
    if(!menu)return
    const outside=(event:PointerEvent)=>{const entry=menu==='new'?newEntry:moreEntry;if(!entry.current?.contains(event.target as Node))setMenu(null)}
    document.addEventListener('pointerdown',outside)
    return()=>document.removeEventListener('pointerdown',outside)
  },[menu])
  useEffect(()=>{
    if(panel==='create')titleInput.current?.focus()
    else if(panel)workspace.current?.querySelector<HTMLElement>(`[data-workspace-panel="${panel}"] input:not(:disabled),[data-workspace-panel="${panel}"] select:not(:disabled),[data-workspace-panel="${panel}"] button:not(:disabled):not(.workspace-panel-close)`)?.focus()
  },[panel,newKind])
  function filter(patch:Partial<typeof filters>){setFilters(old=>({...old,...patch}));setCursors(['']);setSelected('')}
  function refresh(){void client.invalidateQueries({queryKey:['notes',owner]})}
  function saved(note:Note){if(!mounted.current)return;client.setQueryData(['notes',owner,'detail',note.id],note);void client.invalidateQueries({queryKey:['notes',owner,'list']});void client.invalidateQueries({queryKey:['notes',owner,note.id,'versions']})}
  function openNote(id:string){setMenu(null);setPanel(null);setSelected(id)}
  function openPanel(next:WorkspacePanel){panelTrigger.current=menu==='new'?newTrigger.current:menu==='more'?moreTrigger.current:document.activeElement instanceof HTMLButtonElement?document.activeElement:null;setMenu(null);setPanel(next)}
  function closePanel(){if(pdfBusy||busy)return;const trigger=panelTrigger.current||(panel==='create'||panel==='pdf'?newTrigger.current:moreTrigger.current);setPanel(null);trigger?.focus()}
  function newNote(kind:'text'|'image'){setNewKind(kind);openPanel('create')}
  async function create(){setBusy(true);setError(null);try{const note=await api<Note>('/notes','POST',{title:newTitle.trim(),kind:newKind,notebook_id:filters.notebook_id||null,tag_ids:filters.tag_id?[filters.tag_id]:[]});saved(note);openNote(note.id);setNewTitle('');if(filters.deleted)filter({deleted:false})}catch(e){setError(e)}finally{setBusy(false)}}
  async function removeOrRestore(note:NoteSummary){if(!note.deleted_at&&!window.confirm('将此笔记移入回收站？正文和历史版本会保留，可在回收站恢复。'))return;setBusy(true);setError(null);try{await api('/notes/'+note.id+(note.deleted_at?'/restore':''),note.deleted_at?'POST':'DELETE',{revision:note.revision});refresh()}catch(e){setError(e);refresh()}finally{setBusy(false)}}
  const fs=folders.data||foldersEmpty,ns=notebooks.data||notebooksEmpty,ts=tags.data||tagsEmpty
  const filterCount=[filters.kind,filters.folder_id,filters.notebook_id,filters.tag_id].filter(Boolean).length
  const hasFilters=!!filters.q||filterCount>0
  function folderPath(folder:Folder):string {const parts=[folder.name],seen=new Set([folder.id]);let id=folder.parent_id;while(id&&!seen.has(id)){seen.add(id);const parent=fs.find(f=>f.id===id);if(!parent)break;parts.unshift(parent.name);id=parent.parent_id}return parts.join(' / ')}
  const reviewJoin=detail.data&&!detail.data.deleted_at?<ReviewJoin key={'review:'+detail.data.id} owner={owner} kind="note" id={detail.data.id} onGoReview={onGoReview}/>:null
  return <div ref={workspace} className="notes-workspace" onKeyDown={event=>{
    if(event.key!=='Escape')return
    if(menu){event.preventDefault();const trigger=menu==='new'?newTrigger:moreTrigger;setMenu(null);trigger.current?.focus()}
    else if(panel&&!pdfBusy&&!busy){event.preventDefault();closePanel()}
    else if(filtersOpen&&!selected){event.preventDefault();setFiltersOpen(false);filterTrigger.current?.focus()}
  }}>
    <div className={'notes-toolbar'+(selected?' notes-toolbar-detail':'')}>
      {!selected?<label className="notes-search"><Search size={18} aria-hidden="true"/><span className="notes-sr-only">{filters.deleted?'搜索回收站标题或正文':'搜索标题、标签、正文或 OCR'}</span><input value={filters.q} disabled={pdfBusy} onChange={event=>filter({q:event.target.value})} placeholder={filters.deleted?'搜索回收站笔记':'搜索标题、标签、正文或 OCR'}/></label>:null}
      <div className="notes-toolbar-actions">
        {!selected?<button ref={filterTrigger} type="button" className={'secondary'+(filtersOpen?' is-active':'')} disabled={pdfBusy} aria-expanded={filtersOpen} aria-controls="notes-filter-panel" onClick={()=>{setMenu(null);setFiltersOpen(value=>!value)}}><SlidersHorizontal size={16} aria-hidden="true"/>筛选{filterCount?<span className="notes-filter-count">{filterCount}</span>:null}</button>:null}
        {!selected&&!filters.deleted?<button type="button" className="secondary notes-import-entry" disabled={busy||pdfBusy} aria-expanded={panel==='pdf'} aria-controls="notes-pdf-import-panel" onClick={()=>openPanel('pdf')}><FileUp size={17} aria-hidden="true"/>导入 PDF</button>:null}
        {!selected&&!filters.deleted?<div ref={newEntry} className="notes-menu-anchor notes-new-entry"><button ref={newTrigger} type="button" className="primary" disabled={busy||pdfBusy} aria-expanded={menu==='new'} aria-controls="notes-new-menu" onClick={()=>setMenu(value=>value==='new'?null:'new')}><Plus size={17} aria-hidden="true"/>新建笔记<ChevronDown size={14} aria-hidden="true"/></button>
          {menu==='new'?<div id="notes-new-menu" className="notes-popover" role="group" aria-label="新建笔记方式"><button type="button" onClick={()=>newNote('text')}><FileText size={18} aria-hidden="true"/><span><strong>文字笔记</strong><small>写下想法与学习内容</small></span></button><button type="button" onClick={()=>newNote('image')}><ImageIcon size={18} aria-hidden="true"/><span><strong>图片笔记</strong><small>整理图片与手写笔记</small></span></button></div>:null}
        </div>:null}
        <div ref={moreEntry} className="notes-menu-anchor"><button ref={moreTrigger} type="button" className="secondary" disabled={busy||pdfBusy} aria-label="更多笔记操作" aria-expanded={menu==='more'} aria-controls="notes-more-menu" onClick={()=>setMenu(value=>value==='more'?null:'more')}><MoreHorizontal size={18} aria-hidden="true"/><span>更多</span></button>
          {menu==='more'?<div id="notes-more-menu" className="notes-popover" role="group" aria-label="更多笔记操作"><button type="button" onClick={()=>openPanel('organization')}><FolderTree size={18} aria-hidden="true"/><span><strong>整理分类</strong><small>管理文件夹、笔记本与标签</small></span></button><button type="button" onClick={()=>openPanel('export')}><Download size={18} aria-hidden="true"/><span><strong>完整导出</strong><small>下载当前账号的完整归档</small></span></button>{!selected?<button type="button" onClick={()=>{setMenu(null);setPanel(null);filter({deleted:!filters.deleted})}}><Trash2 size={18} aria-hidden="true"/><span><strong>{filters.deleted?'返回全部笔记':'回收站'}</strong><small>{filters.deleted?'继续整理与阅读':'查看和恢复已删除的笔记'}</small></span></button>:null}</div>:null}
        </div>
      </div>
    </div>

    {!selected?<section id="notes-filter-panel" className="panel notes-filters-panel" hidden={!filtersOpen} aria-label="筛选笔记"><div className="panel-heading"><h3>筛选笔记</h3><button type="button" className="icon-button" aria-label="收起筛选" onClick={()=>{setFiltersOpen(false);filterTrigger.current?.focus()}}><X size={17} aria-hidden="true"/></button></div><div className="note-filters">{!filters.deleted?<label>搜索方式<select value={searchMode} disabled={pdfBusy} onChange={event=>setSearchMode(event.target.value as 'keyword'|'semantic')}><option value="keyword">关键词（不调用模型）</option><option value="semantic">语义检索（需确认外发）</option></select></label>:null}<label>笔记类型<select value={filters.kind} disabled={pdfBusy} onChange={event=>filter({kind:event.target.value})}><option value="">全部类型</option><option value="text">文字笔记</option><option value="image">图片笔记</option></select></label><label>筛选文件夹<select value={filters.folder_id} disabled={pdfBusy} onChange={event=>filter({folder_id:event.target.value,notebook_id:''})}><option value="">全部文件夹</option>{fs.map(folder=><option value={folder.id} key={folder.id}>{folderPath(folder)}</option>)}</select></label><label>筛选笔记本<select value={filters.notebook_id} disabled={pdfBusy} onChange={event=>filter({notebook_id:event.target.value})}><option value="">全部笔记本</option>{ns.filter(notebook=>!filters.folder_id||notebook.folder_id===filters.folder_id).map(notebook=><option value={notebook.id} key={notebook.id}>{notebook.name}</option>)}</select></label><label>筛选标签<select value={filters.tag_id} disabled={pdfBusy} onChange={event=>filter({tag_id:event.target.value})}><option value="">全部标签</option>{ts.map(tag=><option value={tag.id} key={tag.id}>{tag.name}</option>)}</select></label></div>{filters.folder_id?<p className="muted">显示该文件夹直属笔记本的笔记。</p>:null}</section>:null}

    {!selected&&(hasFilters||(!filters.deleted&&searchMode==='semantic'))?<div className="notes-active-filters" aria-label="当前搜索与筛选">{!filters.deleted&&searchMode==='semantic'?<span className="notes-filter-chip">语义检索 · 需确认外发</span>:null}{filters.q?<span className="notes-filter-chip">搜索：{filters.q}</span>:null}{filters.kind?<span className="notes-filter-chip">{filters.kind==='text'?'文字笔记':'图片笔记'}</span>:null}{filters.folder_id?<span className="notes-filter-chip">文件夹：{fs.find(folder=>folder.id===filters.folder_id)?.name||'已选择'}</span>:null}{filters.notebook_id?<span className="notes-filter-chip">笔记本：{ns.find(notebook=>notebook.id===filters.notebook_id)?.name||'已选择'}</span>:null}{filters.tag_id?<span className="notes-filter-chip">标签：{ts.find(tag=>tag.id===filters.tag_id)?.name||'已选择'}</span>:null}<button type="button" className="text-button" disabled={pdfBusy} onClick={()=>filter({q:'',kind:'',folder_id:'',notebook_id:'',tag_id:''})}>清除筛选</button></div>:null}

    {!selected&&!filters.deleted?<>
      <section className="panel notes-create-panel" hidden={panel!=='create'} aria-label="新建笔记表单"><div className="panel-heading"><h3>新建{newKind==='text'?'文字':'图片'}笔记</h3><button type="button" className="icon-button" disabled={busy} aria-label="收起新建笔记" onClick={closePanel}><X size={17} aria-hidden="true"/></button></div><form className="new-note-form" onSubmit={event=>{event.preventDefault();void create()}}><label>新笔记标题<input ref={titleInput} value={newTitle} disabled={busy} onChange={event=>setNewTitle(event.target.value)} maxLength={200} required placeholder="为这篇笔记起个名字"/></label><label>新笔记类型<select value={newKind} disabled={busy} onChange={event=>setNewKind(event.target.value as 'text'|'image')}><option value="text">文字笔记</option><option value="image">图片笔记</option></select></label><button className="primary" disabled={busy||!newTitle.trim()}>{busy?'正在创建…':'创建笔记'}</button></form></section>
      <div id="notes-pdf-import-panel" className="workspace-inline-panel notes-pdf-panel" data-workspace-panel="pdf" hidden={panel!=='pdf'}><button type="button" className="icon-button workspace-panel-close" disabled={pdfBusy} aria-label="收起 PDF 导入" onClick={closePanel}><X size={17} aria-hidden="true"/></button><PdfImportForm notebookId={filters.notebook_id} tagIds={filters.tag_id?[filters.tag_id]:[]} onBusyChange={setPdfBusy} onOpen={id=>{refresh();openNote(id)}}/></div>
    </>:null}

    <div className="workspace-inline-panel" data-workspace-panel="organization" hidden={panel!=='organization'}><button type="button" className="icon-button workspace-panel-close" aria-label="收起分类管理" onClick={closePanel}><X size={17} aria-hidden="true"/></button><OrganizationManager folders={fs} notebooks={ns} tags={ts} onChange={refresh}/></div>
    <div className="workspace-inline-panel" data-workspace-panel="export" hidden={panel!=='export'}><button type="button" className="icon-button workspace-panel-close" aria-label="收起完整导出" onClick={closePanel}><X size={17} aria-hidden="true"/></button><ExportPanel owner={owner}/></div>
    <NoteError error={folders.error||notebooks.error||tags.error}/>

    {selected?<>{imageDetail?null:reviewJoin}<NoteError error={detail.error}/>{detail.isPending?<div className="panel empty">正在读取笔记…</div>:detail.data?<NoteDetail key={detail.data.id} note={detail.data} notebooks={ns} tags={ts} onSaved={saved} onClose={()=>{setSelected('');setPanel(null)}} onAskText={onAskText} onOpenNote={openNote}/>:<button className="secondary" onClick={()=>setSelected('')}>返回笔记列表</button>}{imageDetail?reviewJoin:null}</>:<section className="panel notes-library"><div className="notes-list-heading"><div><h3>{filters.deleted?'回收站':globalSearch||hasFilters?'筛选结果':'全部笔记'}</h3><span className="muted">{globalSearch?'搜索全部笔记':notes.isPending?'正在读取…':`共 ${notes.data?.total||0} 篇`}</span></div>{filters.deleted?<button type="button" className="text-button" onClick={()=>filter({deleted:false})}>返回全部笔记</button>:null}</div><NoteError error={error}/>{globalSearch?<div className="notes-search-results" inert={pdfBusy}>{searchMode==='semantic'?<SemanticResults owner={owner} filters={filters} onOpenNote={id=>{if(!pdfBusy)openNote(id)}} onKeyword={()=>setSearchMode('keyword')}/>:<SearchResults owner={owner} filters={filters} onOpenNote={id=>{if(!pdfBusy)openNote(id)}}/>}</div>:<><NoteError error={notes.error}/>{notes.isPending?<div className="empty">正在读取笔记…</div>:notes.data?.items.length?<div className="note-list">{notes.data.items.map(note=><article className="note-card" key={note.id}><button className="note-open" disabled={pdfBusy} onClick={()=>openNote(note.id)}><span className="note-file-icon" aria-hidden="true">{note.kind==='text'?<FileText size={22}/>:<ImageIcon size={22}/>}</span><span className="notes-sr-only">{note.kind==='text'?'文字笔记':'图片笔记'}</span><span className="note-row-copy"><span className="note-row-title"><strong>{note.title}</strong><span className="note-tags">{note.tag_ids.map(id=><span key={id}>{ts.find(tag=>tag.id===id)?.name||'标签'}</span>)}</span></span><small>{ns.find(notebook=>notebook.id===note.notebook_id)?.name||'未分类'} · {date(note.updated_at)}</small></span></button><button className="text-button" disabled={busy||pdfBusy} onClick={()=>void removeOrRestore(note)}>{note.deleted_at?'恢复笔记':'移入回收站'}</button></article>)}</div>:<div className="empty notes-empty"><FileText size={28} strokeWidth={1.4} aria-hidden="true"/><strong>{filters.deleted?'回收站为空':hasFilters?'没有找到匹配的笔记':'从第一篇笔记开始'}</strong><p>{filters.deleted?'移入回收站的笔记会保留在这里。':hasFilters?'换个关键词，或清除筛选再看看。':'新建一篇笔记，或导入 PDF，开始整理你的学习内容。'}</p>{hasFilters?<button type="button" className="text-button" disabled={pdfBusy} onClick={()=>filter({q:'',kind:'',folder_id:'',notebook_id:'',tag_id:''})}>清除所有条件</button>:null}</div>}<div className="note-pagination"><span className="muted">共 {notes.data?.total||0} 篇 · 第 {cursors.length} 页</span><div className="note-actions"><button className="secondary" disabled={cursors.length===1||notes.isFetching||pdfBusy} onClick={()=>setCursors(old=>old.slice(0,-1))}>上一页</button><button className="secondary" disabled={!notes.data?.next_cursor||notes.isFetching||pdfBusy} onClick={()=>{if(notes.data?.next_cursor)setCursors(old=>[...old,notes.data!.next_cursor!])}}>下一页</button></div></div></>}</section>}
  </div>
}
