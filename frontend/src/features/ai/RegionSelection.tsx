import {useEffect,useRef,useState,type PointerEvent} from 'react'
import {PrivateImage} from '../files/PrivateImage'
import {describeRegion,dragRegion,imagePoint,percentRegion,regionPercent,type Point,type Region} from './image-region'

export function RegionFields({region,onChange,disabled}:{region:Region|null;onChange:(region:Region)=>void;disabled:boolean}) {
  const [values,setValues]=useState(()=>region?regionPercent(region):['0','0','100','100']),[error,setError]=useState('')
  useEffect(()=>{setValues(region?regionPercent(region):['0','0','100','100']);setError('')},[region])
  return <details className="image-region-fields"><summary>用百分比精确调整选区（支持键盘）</summary><div>{['距左侧','距上侧','宽度','高度'].map((label,index)=><label key={label}>{label}（%）<input aria-label={'选区'+label+'百分比'} type="number" min="0" max="100" step="0.01" value={values[index]} disabled={disabled} onChange={event=>setValues(old=>old.map((value,i)=>i===index?event.target.value:value))}/></label>)}</div>{error?<p className="error" role="alert">{error}</p>:null}<button type="button" className="secondary" disabled={disabled} onClick={()=>{try{onChange(percentRegion(values));setError('')}catch(error){setError(error instanceof Error?error.message:'选区无效')}}}>应用百分比选区</button></details>
}

export function RegionCanvas({fileId,title,region,selecting,disabled,onChange,onReady,readyId=fileId}:{fileId:string;title:string;region:Region|null;selecting:boolean;disabled:boolean;onChange:(region:Region|null)=>void;onReady:(identity:string)=>void;readyId?:string}) {
  const [image,setImage]=useState<HTMLImageElement|null>(null),[geometry,setGeometry]=useState<{left:number;top:number;width:number;height:number}|null>(null)
  const [draft,setDraft]=useState<Region|null>(null),[error,setError]=useState('')
  const start=useRef<{pointer:number;point:Point}|null>(null)
  useEffect(()=>{
    onReady(image?readyId:'')
    if(!image){setGeometry(null);return}
    const measure=()=>setGeometry({left:image.offsetLeft,top:image.offsetTop,width:image.offsetWidth,height:image.offsetHeight})
    const observer=new ResizeObserver(measure);observer.observe(image);if(image.offsetParent)observer.observe(image.offsetParent);measure()
    return()=>{observer.disconnect();onReady('')}
  },[image,readyId,onReady])
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
