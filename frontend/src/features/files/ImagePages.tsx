import { useEffect,useId,useRef,useState,type ReactNode } from 'react'
import { ChevronDown } from 'lucide-react'
import { api,ApiError } from '../../api'
import { createUuid } from '../../uuid'
import { NoteError } from '../notes/NoteError'
import type { Note } from '../notes/types'
import { ImageReader } from './ImageReader'
import { PrivateImage } from './PrivateImage'
import { OcrPanel } from '../search/OcrPanel'
import { sendContent } from './upload'
import type { CompleteOut,FileOut,PageOut,UploadOut } from './types'
import './files.css'

type Stage='selected'|'starting'|'sending'|'completing'|'done'|'failed'
type Pick={id:string;file:File;preview:string;requestKey:string;stage:Stage;progress:number;session?:UploadOut;received?:boolean;error?:string;invalid?:boolean}
const stages:Record<Stage,string>={selected:'待上传',starting:'申请上传',sending:'传输中',completing:'创建页面',done:'上传完成',failed:'上传失败'}
const statuses:Record<string,string>={uploading:'等待上传',processing:'生成预览中',ready:'可阅读',rejected:'图片校验失败',expired:'上传已过期',failed:'处理失败'}
type Props={noteId:string;ownerId:string;revision:number;pdfImportStatus:ReactNode;canMutate:boolean;beforeMutation:()=>boolean;onBusyChange:(busy:boolean)=>void;onNoteChanged:(note:Note)=>void}
export function ImagePages({noteId,ownerId,revision,pdfImportStatus,canMutate,beforeMutation,onBusyChange,onNoteChanged}:Props){
  const [picks,setPicks]=useState<Pick[]>([]),[pages,setPages]=useState<PageOut[]>([]),[selected,setSelected]=useState(''),[loading,setLoading]=useState(true),[busy,setBusy]=useState(false),[error,setError]=useState<unknown>(null),[notice,setNotice]=useState('')
  const [pollEpoch,setPollEpoch]=useState(0)
  const [pickerMode,setPickerMode]=useState<'pages'|'order'|null>(null)
  const pickerId=useId()
  const pickerButton=useRef<HTMLButtonElement>(null),pickerPanel=useRef<HTMLDivElement>(null)
  const pickerOpen=pages.length>1&&pickerMode!==null
  const sorting=pickerOpen&&pickerMode==='order'
  const alive=useRef(false),generation=useRef(0),items=useRef(picks),urls=useRef(new Set<string>()),running=useRef(false),controller=useRef<AbortController|null>(null),callbacks=useRef({beforeMutation,onBusyChange,onNoteChanged})
  const pageRequest=useRef(0)
  callbacks.current={beforeMutation,onBusyChange,onNoteChanged}
  function changeItems(next:Pick[]){items.current=next;setPicks(next)}
  function patch(id:string,value:Partial<Pick>){if(alive.current)changeItems(items.current.map(item=>item.id===id?{...item,...value}:item))}
  function current(){const token=generation.current;return()=>alive.current&&generation.current===token}
  async function readPages(isCurrent=current()){const request=++pageRequest.current,next=await api<PageOut[]>('/notes/'+noteId+'/pages');if(isCurrent()&&request===pageRequest.current){setPages(next);setSelected(old=>next.some(p=>p.id===old)?old:next[0]?.id||'')}return next}
  useEffect(()=>{
    alive.current=true;return()=>{alive.current=false;generation.current++;controller.current?.abort();for(const url of urls.current)URL.revokeObjectURL(url);urls.current.clear();callbacks.current.onBusyChange(false)}
  },[noteId,ownerId])
  useEffect(()=>{const isCurrent=current();let relevant=true;void readPages(()=>isCurrent()&&relevant).then(()=>{if(isCurrent()&&relevant){setError(null);setLoading(false)}}).catch(e=>{if(isCurrent()&&relevant){setError(e);setLoading(false)}});return()=>{relevant=false}},[noteId,revision])
  useEffect(()=>{
    if(!pickerOpen)return
    function closeOutside(event:Event){
      const target=event.target as Node
      if(!pickerButton.current?.contains(target)&&!pickerPanel.current?.contains(target))setPickerMode(null)
    }
    function closeWithEscape(event:KeyboardEvent){
      if(event.key!=='Escape')return
      event.preventDefault()
      event.stopPropagation()
      closePicker()
    }
    document.addEventListener('pointerdown',closeOutside)
    document.addEventListener('focusin',closeOutside)
    document.addEventListener('keydown',closeWithEscape,true)
    return()=>{
      document.removeEventListener('pointerdown',closeOutside)
      document.removeEventListener('focusin',closeOutside)
      document.removeEventListener('keydown',closeWithEscape,true)
    }
  },[pickerOpen])
  // A new page may become pending after an upload without changing the incoming
  // revision immediately. Poll from its status until processing has finished.
  const pending=pages.some(page=>['uploading','processing'].includes(page.file.status))
  useEffect(()=>{if(!pending)return;const isAlive=current();let relevant=true;const isCurrent=()=>isAlive()&&relevant;let timer:ReturnType<typeof setTimeout>;async function tick(){try{const next=await readPages(isCurrent);if(isCurrent()&&next.some(p=>['uploading','processing'].includes(p.file.status)))timer=setTimeout(()=>void tick(),2500)}catch(e){if(isCurrent())setError(e)}}timer=setTimeout(()=>void tick(),2500);return()=>{relevant=false;clearTimeout(timer)}},[pending,noteId,pollEpoch])
  function choose(files:FileList|null){if(!files)return;const problems:string[]=[],next=[...items.current];for(const file of files){if(next.length>=50){problems.push('每批最多选择 50 张图片');break}if(!['image/jpeg','image/png','image/webp'].includes(file.type)){problems.push(file.name+'：仅支持 JPEG、PNG、WebP');continue}if(!file.size||file.size>20*1024*1024){problems.push(file.name+'：图片须大于 0 且不超过 20 MiB');continue}if(file.name.length>200){problems.push(file.name+'：文件名不能超过 200 个字符');continue}try{const id=createUuid(),requestKey=createUuid(),preview=URL.createObjectURL(file);urls.current.add(preview);next.push({id,requestKey,file,preview,stage:'selected',progress:0})}catch{problems.push(file.name+'：无法创建图片预览，请重新选择图片或升级浏览器后重试')}}changeItems(next);setError(problems.length?new Error(problems.join('；')):null)}
  function remove(id:string){const item=items.current.find(p=>p.id===id);if(item){URL.revokeObjectURL(item.preview);urls.current.delete(item.preview)}changeItems(items.current.filter(p=>p.id!==id))}
  function movePick(index:number,step:number){const next=[...items.current];[next[index],next[index+step]]=[next[index+step],next[index]];changeItems(next)}
  function begin(){if(running.current||!callbacks.current.beforeMutation()){setError(new Error('请先保存标题、笔记本和标签，再操作图片'));return false}running.current=true;setBusy(true);setError(null);callbacks.current.onBusyChange(true);controller.current=new AbortController();return true}
  function finish(isCurrent:()=>boolean){if(!isCurrent())return;running.current=false;setBusy(false);callbacks.current.onBusyChange(false)}
  async function freshNote(isCurrent:()=>boolean){const note=await api<Note>('/notes/'+noteId);if(!isCurrent())throw new DOMException('已离开笔记','AbortError');return note}
  async function refreshPages(){const isCurrent=current();setLoading(true);try{const [latest]=await Promise.all([freshNote(isCurrent),readPages(isCurrent)]);if(isCurrent()){callbacks.current.onNoteChanged(latest);setError(null);setPollEpoch(n=>n+1)}}catch(e){if(isCurrent())setError(e)}finally{if(isCurrent())setLoading(false)}}
  async function upload(selectedIds?:string[]){if(!begin())return;const isCurrent=current(),signal=controller.current!.signal;setNotice('')
    try{const batch=items.current.filter(p=>(!selectedIds||selectedIds.includes(p.id))&&['selected','failed'].includes(p.stage)&&!p.invalid);for(const item of batch){if(!isCurrent())break;let completed=false;try{
      let session=item.session,received=item.received
      if(session&&new Date(session.expires_at).getTime()<=Date.now()){session=undefined;received=false;patch(item.id,{requestKey:createUuid(),session:undefined,received:false})}
      if(!session){patch(item.id,{stage:'starting',error:undefined,progress:0});session=await api<UploadOut>('/notes/'+noteId+'/uploads','POST',{filename:item.file.name,mime:item.file.type,size:item.file.size,request_key:items.current.find(p=>p.id===item.id)!.requestKey});if(!isCurrent())break;patch(item.id,{session})}
      if(!received){patch(item.id,{stage:'sending',error:undefined});session=await sendContent(session,item.file,percent=>patch(item.id,{progress:percent}),signal);if(!isCurrent())break;patch(item.id,{session,received:true,progress:100})}
      patch(item.id,{stage:'completing'});let latest=await freshNote(isCurrent)
      let result:CompleteOut
      try{result=await api<CompleteOut>('/uploads/'+session.id+'/complete','POST',{revision:latest.revision})}catch(e){if(!(e instanceof ApiError&&e.status===409&&e.code!=='upload_incomplete'))throw e;latest=await freshNote(isCurrent);if(isCurrent())setNotice('服务器版本已更新，正在将图片追加到最新页面顺序');result=await api<CompleteOut>('/uploads/'+session.id+'/complete','POST',{revision:latest.revision})}
      if(!isCurrent())break;completed=true;patch(item.id,{stage:'done',session:{...session,status:'completed'},error:undefined});pageRequest.current++;setPages(old=>old.some(p=>p.id===result.page.id)?old:[...old,result.page]);setSelected(old=>old||result.page.id);const updated=await freshNote(isCurrent);callbacks.current.onNoteChanged(updated);await readPages(isCurrent)
    }catch(e){if(!isCurrent())break;if(completed){setError(new Error('图片已上传成功，刷新笔记信息失败，请刷新页面后继续'));continue}const expired=e instanceof ApiError&&e.status===410;patch(item.id,{stage:'failed',error:e instanceof Error?e.message:'上传失败',invalid:e instanceof ApiError&&['upload_mismatch','filename_invalid','file_too_large'].includes(e.code),...(expired?{session:undefined,received:false,requestKey:createUuid()}: {})})}}
    }finally{finish(isCurrent)}
  }
  async function movePage(index:number,step:number){if(!begin())return;const isCurrent=current();try{const ids=pages.map(p=>p.id);[ids[index],ids[index+step]]=[ids[index+step],ids[index]];await api('/notes/'+noteId+'/pages/order','PATCH',{revision,page_ids:ids});if(!isCurrent())return;callbacks.current.onNoteChanged(await freshNote(isCurrent));await readPages(isCurrent)}catch(e){if(isCurrent()){setError(e instanceof ApiError&&e.status===409?new Error('页面或笔记已更新，请检查最新顺序后再次移动'):e);try{const latest=await freshNote(isCurrent);callbacks.current.onNoteChanged(latest);await readPages(isCurrent)}catch(fetchError){if(isCurrent())setError(fetchError)}}}finally{finish(isCurrent)}}
  async function retryFile(file:FileOut){if(!begin())return;const isCurrent=current();try{await api<FileOut>('/files/'+file.id+'/retry','POST');if(isCurrent())await readPages(isCurrent)}catch(e){if(isCurrent())setError(e)}finally{finish(isCurrent)}}
  function closePicker(){setPickerMode(null);pickerButton.current?.focus({preventScroll:true})}
  function selectPage(id:string){setSelected(id);closePicker()}
  const selectedIndex=pages.findIndex(page=>page.id===selected)
  const active=pages[selectedIndex],waiting=picks.filter(p=>p.stage!=='done'&&!p.invalid)
  return <section className="image-pages" aria-label="图片笔记页面">
    <div className="panel-heading image-pages-heading">
      <h3>图片页面 {pages.length===1?<span className="muted">共 1 页</span>:null}</h3>
      <div className="image-pages-heading-actions">
        <button type="button" className="text-button" disabled={busy||loading} onClick={()=>void refreshPages()}>刷新页面</button>
        {pages.length>1?<button type="button" ref={pickerButton} className="secondary image-page-picker-trigger" aria-expanded={pickerOpen} aria-controls={pickerId} onClick={()=>setPickerMode(pickerOpen?null:'pages')}>
          选页 · {selectedIndex+1} / {pages.length}<ChevronDown size={14} aria-hidden="true"/>
        </button>:null}
      </div>
      {pages.length>1?<div ref={pickerPanel} id={pickerId} className="image-page-picker" role="group" aria-label="选择页面" hidden={!pickerOpen}>
        <div className="image-page-picker-heading">
          <strong>选择页面</strong>
          <button type="button" className="text-button" aria-pressed={sorting} onClick={()=>setPickerMode(sorting?'pages':'order')}>调整页序</button>
        </div>
        {sorting?<NoteError error={error}/>:null}
        <ol className="image-page-strip">
          {pages.map((page,index)=>{
            const current=index===selectedIndex
            return <li key={page.id} className={current?'selected':''}>
              <div className="image-page-select">
                {page.file.status==='ready'?<PrivateImage fileId={page.file.id} kind="thumbnail" alt={'第'+(index+1)+'页缩略图'} onSelect={()=>selectPage(page.id)} selected={current}/>:<span className="image-state">{statuses[page.file.status]||page.file.status}</span>}
                <button type="button" className="image-page-open" aria-label={'阅读第'+(index+1)+'页：'+page.file.filename} aria-pressed={current} onClick={()=>selectPage(page.id)}>
                  <span>第 {index+1} 页{current?' · 当前':''}</span><small>{page.file.filename}</small>
                </button>
              </div>
              {sorting?<div className="image-page-actions">
                <button type="button" className="text-button" aria-label={'第'+(index+1)+'页上移'} disabled={busy||!canMutate||index===0} onClick={()=>void movePage(index,-1)}>上移</button>
                <button type="button" className="text-button" aria-label={'第'+(index+1)+'页下移'} disabled={busy||!canMutate||index===pages.length-1} onClick={()=>void movePage(index,1)}>下移</button>
              </div>:null}
            </li>
          })}
        </ol>
      </div>:null}
    </div>
    {loading?<p role="status">读取页面…</p>:null}
    {!loading&&!pages.length?<div className="empty">尚无图片页面，请选择图片上传。</div>:null}
    {active?.file.status==='ready'?
      <ImageReader imageFirst key={active.file.id} fileId={active.file.id} filename={active.file.filename}/>:active?
      <div className="image-processing" role="status">
        <strong>{statuses[active.file.status]||active.file.status}</strong>
        {active.file.error_code?<p className="muted">错误：{active.file.error_code}</p>:null}
        {active.file.status==='failed'?<button className="secondary" disabled={busy||!canMutate} onClick={()=>void retryFile(active.file)}>重试图片处理</button>:active.file.status==='rejected'?<p className="muted">原图未通过校验，请重新选择有效图片上传。</p>:<p className="muted">处理完成后将显示阅读图片。</p>}
      </div>:null}
    {active?.file.status==='ready'?<OcrPanel key={[ownerId,noteId,active.id,active.file.id].join(':')} noteId={noteId} ownerId={ownerId} revision={revision} page={active} canMutate={canMutate&&!busy} beforeMutation={beforeMutation} onBusyChange={onBusyChange}/>:null}
    {pdfImportStatus}
    <div className="image-upload-tools">
      <p className="muted">选择图片后可预览、移除和调整顺序；每批最多 50 张，单张最多 20 MiB。上传成功的页面会追加到笔记。</p>
      <label className="image-file-label">选择 JPEG / PNG / WebP 图片<input type="file" accept="image/jpeg,image/png,image/webp" multiple disabled={busy||!canMutate} onChange={e=>{choose(e.target.files);e.target.value=''}}/></label>
      {!canMutate&&!busy?<p className="muted">请先保存笔记信息，再选择图片或调整页序。</p>:null}
      {sorting?null:<NoteError error={error}/>}
      {notice?<p className="muted" role="status">{notice}</p>:null}
      {picks.length?<><ol className="upload-picks">{picks.map((item,index)=><li key={item.id}><img src={item.preview} alt={item.file.name+' 上传前预览'}/><div className="upload-info"><strong>{item.file.name}</strong><span role="status">{stages[item.stage]}{item.stage==='sending'?' · '+item.progress+'%':''}</span>{item.stage==='sending'?<progress max={100} value={item.progress} aria-label={item.file.name+'上传进度'}/>:null}{item.error?<span className="upload-error">{item.error}</span>:null}{item.invalid?<small>请移除此项并选择符合要求的图片。</small>:null}</div><div className="image-page-actions"><button className="secondary" aria-label={'将'+item.file.name+'提前'} disabled={busy||index===0} onClick={()=>movePick(index,-1)}>上移</button><button className="secondary" aria-label={'将'+item.file.name+'后移'} disabled={busy||index===picks.length-1} onClick={()=>movePick(index,1)}>下移</button>{item.stage==='failed'&&!item.invalid?<button className="secondary" disabled={busy||!canMutate} onClick={()=>void upload([item.id])}>重试</button>:null}<button className="text-button" aria-label={'移除'+item.file.name+'预览'} disabled={busy} onClick={()=>remove(item.id)}>移除</button></div></li>)}</ol><div className="note-actions"><button className="primary" disabled={busy||!canMutate||!waiting.length} onClick={()=>void upload()}>{busy?'正在处理…':'按当前顺序上传 / 重试'}</button><button className="text-button" disabled={busy||!picks.some(p=>p.stage==='done')} onClick={()=>picks.filter(p=>p.stage==='done').forEach(p=>remove(p.id))}>清理已上传预览</button></div></>:null}
    </div>
  </section>
}
