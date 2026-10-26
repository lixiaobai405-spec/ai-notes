import type { components } from '../../api.generated'
import { RegionCanvas } from './RegionSelection'
import type { Region } from './image-region'

export type ImageInput=components['schemas']['ImageInput']
export type ImageBook=components['schemas']['ImageNotebookOut']
export type ImageDetail=components['schemas']['ImageNotebookDetail']
export type ImagePreview=components['schemas']['ImagePreviewOut']
export type ImageChoice={noteId:string;pageId:string;mode:'page'|'region';region:Region|null;sourceRevision?:number}
export const emptyImageChoice:ImageChoice={noteId:'',pageId:'',mode:'page',region:null}

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
