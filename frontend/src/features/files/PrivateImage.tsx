import { useEffect,useRef,useState } from 'react'
import { readPrivateFile } from './download'

// Tickets are issued only for visible, ready images. Failed image loads
// require a deliberate reload, preventing an automatic retry loop.
export function PrivateImage({fileId,kind,alt,className,onSelect,selected,onImageReady}:{fileId:string;kind:'reading'|'thumbnail';alt:string;className?:string;onSelect?:()=>void;selected?:boolean;onImageReady?:(image:HTMLImageElement|null)=>void}) {
  const [url,setUrl]=useState(''),[error,setError]=useState(''),[reload,setReload]=useState(0)
  const host=useRef<HTMLSpanElement>(null),[visible,setVisible]=useState(kind==='reading')
  useEffect(()=>{if(kind==='reading'){setVisible(true);return}const observer=new IntersectionObserver(entries=>{if(entries.some(entry=>entry.isIntersecting)){setVisible(true);observer.disconnect()}},{rootMargin:'100px'});if(host.current)observer.observe(host.current);return()=>observer.disconnect()},[kind])
  useEffect(()=>{
    if(!visible)return
    let alive=true,objectUrl='';const controller=new AbortController();setUrl('');setError('');onImageReady?.(null)
    async function ticket(){
      try{const blob=await readPrivateFile(fileId,kind,controller.signal);if(!alive)return;objectUrl=URL.createObjectURL(blob);setUrl(objectUrl)}catch(e){if(alive)setError(e instanceof Error?e.message:'图片读取失败')}
    }
    void ticket();return()=>{alive=false;controller.abort();if(objectUrl)URL.revokeObjectURL(objectUrl)}
  },[fileId,kind,reload,visible,onImageReady])
  const image=url?<img className={className} src={url} alt={alt} referrerPolicy="no-referrer" draggable={false} onLoad={event=>onImageReady?.(event.currentTarget)} onError={()=>{onImageReady?.(null);setError('图片读取失败或授权已过期，请重新加载')}}/>:<span className="muted" role="status">读取图片…</span>
  return <span className="private-image" ref={host}>{error?<span className="private-image-error"><span role="status">{error}</span><button type="button" className="text-button" aria-label={'重新加载'+alt} onClick={()=>setReload(n=>n+1)}>重新加载</button></span>:onSelect?<button type="button" className="image-thumbnail-button" aria-label={'阅读'+alt} aria-pressed={selected} onClick={onSelect}>{image}</button>:image}</span>
}
