import { useEffect, useRef, useState, type PointerEvent } from 'react'
import type { components } from '../../api.generated'
import { PrivateImage } from '../files/PrivateImage'
import { describeRegion, dragRegion, imagePoint, percentRegion, regionPercent, type Point, type Region } from './image-region'

export type ImageInput=components['schemas']['ImageInput']
export type ImageBook=components['schemas']['ImageNotebookOut']
export type ImageDetail=components['schemas']['ImageNotebookDetail']
export type ImagePreview=components['schemas']['ImagePreviewOut']
export type ImageChoice={noteId:string;pageId:string;mode:'page'|'region';region:Region|null;sourceRevision?:number}
export const emptyImageChoice:ImageChoice={noteId:'',pageId:'',mode:'page',region:null}

function RegionFields({region,onChange,disabled}:{region:Region|null;onChange:(region:Region)=>void;disabled:boolean}) {
  const [values,setValues]=useState(()=>region?regionPercent(region):['0','0','100','100']),[error,setError]=useState('')
  useEffect(()=>{setValues(region?regionPercent(region):['0','0','100','100']);setError('')},[region])
  return <details className="image-region-fields"><summary>用百分比精确调整选区（支持键盘）</summary><div>{['距左侧','距上侧','宽度','高度'].map((label,index)=><label key={label}>{label}（%）<input aria-label={'选区'+label+'百分比'} type="number" min="0" max="100" step="0.01" value={values[index]} disabled={disabled} onChange={event=>setValues(old=>old.map((value,i)=>i===index?event.target.value:value))}/></label>)}</div>{error?<p className="error" role="alert">{error}</p>:null}<button type="button" className="secondary" disabled={disabled} onClick={()=>{try{onChange(percentRegion(values));setError('')}catch(error){setError(error instanceof Error?error.message:'选区无效')}}}>应用百分比选区</button></details>
}

function RegionCanvas({fileId,title,region,selecting,disabled,onChange,onReady}:{fileId:string;title:string;region:Region|null;selecting:boolean;disabled:boolean;onChange:(region:Region|null)=>void;onReady:(fileId:string)=>void}) {
  const [image,setImage]=useState<HTMLImageElement|null>(null),[geometry,setGeometry]=useState<{left:number;top:number;width:number;height:number}|null>(null)
  const [draft,setDraft]=useState<Region|null>(null),[error,setError]=useState('')
  const start=useRef<{pointer:number;point:Point}|null>(null)
  useEffect(()=>{
    onReady(image?fileId:'')
    if(!image){setGeometry(null);return}
    const measure=()=>setGeometry({left:image.offsetLeft,top:image.offsetTop,width:image.offsetWidth,height:image.offsetHeight})
    const observer=new ResizeObserver(measure);observer.observe(image);if(image.offsetParent)observer.observe(image.offsetParent);measure()
    return()=>{observer.disconnect();onReady('')}
  },[image,fileId,onReady])
  function point(event:PointerEvent<HTMLDivElement>){return imagePoint({x:event.clientX,y:event.clientY},image!.getBoundingClientRect())}
  function down(event:PointerEvent<HTMLDivElement>){
    if(disabled||!selecting||!image||!event.isPrimary||event.button!==0||start.current||(event.target as HTMLElement).closest('button'))return
    const rect=image.getBoundingClientRect()
    if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)return
    start.current={pointer:event.pointerId,point:point(event)};setError('');setDraft(null);onChange(null)
    event.currentTarget.setPointerCapture(event.pointerId);event.preventDefault()
  }
  function move(event:PointerEvent<HTMLDivElement>){if(start.current?.pointer===event.pointerId&&image)setDraft(dragRegion(start.current.point,point(event)))}
  function up(event:PointerEvent<HTMLDivElement>){
    if(start.current?.pointer!==event.pointerId||!image)return
    const next=dragRegion(start.current.point,point(event));start.current=null;setDraft(null)
    if(next){onChange(next);setError('')}else setError('选区宽高必须大于 0，请重新拖动画框。')
    if(event.currentTarget.hasPointerCapture(event.pointerId))event.currentTarget.releasePointerCapture(event.pointerId)
  }
  function cancel(event:PointerEvent<HTMLDivElement>){if(start.current?.pointer===event.pointerId){start.current=null;setDraft(null)}}
  const visible=draft||region
  return <div className="image-region-preview"><div className={'image-selection-stage '+(selecting?'selecting':'')} aria-label={selecting?'拖动框选提问区域':'本次图片预览'} onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={cancel} onLostPointerCapture={cancel}>
    <PrivateImage fileId={fileId} kind="reading" alt={title} onImageReady={setImage}/>
    {selecting&&visible&&geometry?<span className="image-source-region" aria-label="当前提问选区" style={{left:geometry.left+visible.x*geometry.width,top:geometry.top+visible.y*geometry.height,width:visible.width*geometry.width,height:visible.height*geometry.height}}/>:null}
  </div>{error?<p className="error" role="alert">{error}</p>:null}
    {selecting?<><p className="muted">在图片内按住并拖动画框，可反向拖动；手机用单指画框。框选时不缩放图片，发送后可在来源中缩放回看。</p>{region?<p role="status">{describeRegion(region)}</p>:<p className="muted" role="status">尚未选择区域，请画框或填写下方百分比。</p>}<button type="button" className="text-button" disabled={disabled||!region} onClick={()=>{onChange(null);setError('')}}>清除选区并重画</button><RegionFields region={region} onChange={onChange} disabled={disabled||!image}/></>:null}
  </div>
}

export function ImagePicker({choice,onChange,books,book,preview,error,loading,disabled,onReady}:{choice:ImageChoice;onChange:(choice:ImageChoice)=>void;books:ImageBook[];book?:ImageDetail;preview?:ImagePreview;error:string;loading:boolean;disabled:boolean;onReady:(fileId:string)=>void}) {
  const page=book?.id===choice.noteId?book.pages.find(page=>page.page_id===choice.pageId):undefined
  return <fieldset className="pdf-picker image-picker" disabled={disabled}><legend>本次图片资料</legend>
    <label>图片笔记<select aria-label="图片笔记" value={choice.noteId} onChange={event=>{onReady('');onChange({...emptyImageChoice,noteId:event.target.value})}}><option value="">选择普通图片笔记</option>{books.map(book=><option key={book.id} value={book.id}>{book.title}（{book.page_count} 张图片）</option>)}</select></label>
    {!books.length?<p className="muted">先到“我的笔记”上传照片或截图。此入口只显示普通图片，PDF 请使用 PDF 资料。</p>:null}
    {choice.noteId?<><label>图片页面<select aria-label="图片页面" value={choice.pageId} disabled={!book||book.id!==choice.noteId||disabled} onChange={event=>{onReady('');onChange({...choice,pageId:event.target.value,region:null,sourceRevision:undefined})}}><option value="">选择一张图片</option>{book?.id===choice.noteId?book.pages.map(page=><option key={page.page_id} value={page.page_id}>第 {page.number} 张图片</option>):null}</select></label>
      {page?<><div className="pdf-mode"><label><input type="radio" name="image-mode" checked={choice.mode==='page'} onChange={()=>onChange({...choice,mode:'page'})}/>整张图片</label><label><input type="radio" name="image-mode" checked={choice.mode==='region'} onChange={()=>onChange({...choice,mode:'region'})}/>框选局部</label></div>
        <RegionCanvas key={page.file_id} fileId={page.file_id} title={book!.title+' · 第 '+page.number+' 张图片'} region={choice.region} selecting={choice.mode==='region'} disabled={disabled} onChange={region=>onChange({...choice,region})} onReady={onReady}/>
      </>:null}
      {choice.sourceRevision!==undefined&&book&&choice.sourceRevision!==book.revision?<p className="chat-warning">来源笔记已更新。本次预览使用当前版本 {book.revision}，历史回答仍保留版本 {choice.sourceRevision} 的来源记录，请核对图片和选区。</p>:null}
      {loading?<p className="muted" role="status">正在核对图片与提问范围…</p>:null}
      {error?<p className="error" role="alert">{error}</p>:null}
      {preview&&!loading&&!error&&page?<div className="pdf-preview" role="status"><strong>第 {page.number} 张图片 · {choice.mode==='region'?'仅发送框选区域':'发送整张图片'}</strong><p>本次图像 {preview.crop_width} × {preview.crop_height} 像素，计划 {preview.planned_calls} 次模型调用。</p><small>{choice.mode==='region'?'选区以原图比例记录，来源回看会标出绿色边框。':'整页图像与文字对话将发送给 DeepSeek Flash。'} 图片页序以提问时记录为准。</small></div>:null}
    </>:null}
  </fieldset>
}
