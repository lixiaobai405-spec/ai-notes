import { useEffect,useRef,useState,type PointerEvent } from 'react'
import { PrivateImage } from './PrivateImage'
import type { Region } from '../ai/image-region'

type View={scale:number;x:number;y:number}
const initial:View={scale:1,x:0,y:0}
export function ImageReader({fileId,filename,region}:{fileId:string;filename:string;region?:Region|null}){
  const [view,setView]=useState(initial),points=useRef(new Map<number,{x:number;y:number}>()),previous=useRef<{x:number;y:number;distance:number}|null>(null)
  const [image,setImage]=useState<HTMLImageElement|null>(null),[geometry,setGeometry]=useState<{left:number;top:number;width:number;height:number}|null>(null)
  useEffect(()=>{
    if(!image){setGeometry(null);return}
    const measure=()=>setGeometry({left:image.offsetLeft,top:image.offsetTop,width:image.offsetWidth,height:image.offsetHeight})
    const observer=new ResizeObserver(measure);observer.observe(image);if(image.offsetParent)observer.observe(image.offsetParent);measure()
    return()=>observer.disconnect()
  },[image])
  function gesture(){const values=[...points.current.values()];if(!values.length)return null;const a=values[0],b=values[1]||a;return{x:(a.x+b.x)/2,y:(a.y+b.y)/2,distance:Math.hypot(a.x-b.x,a.y-b.y)}}
  function down(e:PointerEvent<HTMLDivElement>){if((e.target as HTMLElement).closest('button'))return;e.currentTarget.setPointerCapture(e.pointerId);points.current.set(e.pointerId,{x:e.clientX,y:e.clientY});previous.current=gesture()}
  function move(e:PointerEvent<HTMLDivElement>){if(!points.current.has(e.pointerId))return;points.current.set(e.pointerId,{x:e.clientX,y:e.clientY});const next=gesture(),old=previous.current;if(next&&old)setView(v=>({...v,x:v.x+next.x-old.x,y:v.y+next.y-old.y,scale:Math.min(6,Math.max(.5,old.distance>0&&next.distance>0?v.scale*next.distance/old.distance:v.scale))}));previous.current=next}
  function up(e:PointerEvent<HTMLDivElement>){points.current.delete(e.pointerId);previous.current=gesture()}
  return <div className="image-reader"><div className="image-reader-tools"><strong>{filename}</strong><div><button type="button" className="secondary" aria-label="缩小图片" disabled={view.scale<=.5} onClick={()=>setView(v=>({...v,scale:Math.max(.5,v.scale/1.25)}))}>−</button><output aria-label="图片缩放比例">{Math.round(view.scale*100)}%</output><button type="button" className="secondary" aria-label="放大图片" disabled={view.scale>=6} onClick={()=>setView(v=>({...v,scale:Math.min(6,v.scale*1.25)}))}>+</button><button type="button" className="text-button" onClick={()=>setView(initial)}>重置视图</button></div></div><p className="muted">拖动查看图片；手机可用双指缩放。缩放后可拖动阅读边缘内容。{region?'绿色边框标出了本次提问的原图选区。':''}</p><div className="image-reader-viewport" aria-label="图片阅读区域" onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up} onLostPointerCapture={up}><div className="image-reader-transform" style={{transform:`translate(${view.x}px, ${view.y}px) scale(${view.scale})`}}><PrivateImage fileId={fileId} kind="reading" alt={filename} onImageReady={setImage}/>{region&&geometry?<span className="image-source-region" aria-label="提问选区" style={{left:geometry.left+region.x*geometry.width,top:geometry.top+region.y*geometry.height,width:region.width*geometry.width,height:region.height*geometry.height}}/>:null}</div></div></div>
}
