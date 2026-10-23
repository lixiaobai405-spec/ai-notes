import { Fragment, memo, type ReactNode } from 'react'
import katex from 'katex'
import 'katex/dist/katex.min.css'

type JsonObject=Record<string,unknown>
type Props={body:JsonObject;citation?:(number:number,key:string)=>ReactNode}
const object=(value:unknown):JsonObject=>value!==null&&typeof value==='object'&&!Array.isArray(value)?value as JsonObject:{}

function safeLink(value:unknown):string|null {
  if(typeof value!=='string'||!/^https?:\/\//i.test(value))return null
  try{const url=new URL(value);return url.protocol==='https:'||url.protocol==='http:'?url.href:null}catch{return null}
}

const Formula=memo(function Formula({latex,block}:{latex:string;block:boolean}) {
  let html:string
  try {
    if(new TextEncoder().encode(latex).byteLength>8192)throw new Error('formula too large')
    html=katex.renderToString(latex,{displayMode:block,throwOnError:true,trust:false,strict:'ignore',maxExpand:1000,maxSize:10})
  } catch {
    const fallback=<><span className="answer-math-error-label">公式未能排版，原 LaTeX：</span><code>{latex}</code></>
    return block?<div className="answer-math-error" role="note">{fallback}</div>:<span className="answer-math-error" role="note">{fallback}</span>
  }
  // Only KaTeX output is HTML; model text and node attributes never enter this sink.
  return block?<div className="answer-math answer-math-block" dangerouslySetInnerHTML={{__html:html}}/>:<span className="answer-math answer-math-inline" dangerouslySetInnerHTML={{__html:html}}/>
})

export function FormattedAnswer({body,citation}:Props) {
  function textNode(node:JsonObject,key:string,inCode:boolean):ReactNode {
    const text=typeof node.text==='string'?node.text:''
    const marks=Array.isArray(node.marks)?node.marks.map(object):[]
    let value:ReactNode=text
    if(citation&&!inCode&&!marks.some(mark=>mark.type==='code'||mark.type==='link')) {
      const pieces:ReactNode[]=[]
      let offset=0
      for(const match of text.matchAll(/\[\[p:([0-9]{1,3})\]\]/g)) {
        pieces.push(text.slice(offset,match.index!))
        pieces.push(citation(Number(match[1]),key+'-'+match.index)??match[0])
        offset=match.index!+match[0].length
      }
      pieces.push(text.slice(offset))
      value=pieces
    }
    if(!inCode)for(const mark of marks) {
      switch(mark.type) {
        case 'bold':value=<strong>{value}</strong>;break
        case 'italic':value=<em>{value}</em>;break
        case 'strike':value=<s>{value}</s>;break
        case 'code':value=<code>{value}</code>;break
        case 'link':{
          const href=safeLink(object(mark.attrs).href)
          if(href)value=<a href={href} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">{value}</a>
          break
        }
      }
    }
    return <Fragment key={key}>{value}</Fragment>
  }

  function render(value:unknown,key:string,depth=0,inCode=false):ReactNode {
    if(depth>64)return null
    const node=object(value),attrs=object(node.attrs)
    if(node.type==='text')return textNode(node,key,inCode)
    const children=()=>Array.isArray(node.content)?node.content.map((child,index)=>render(child,key+'-'+index,depth+1,inCode||node.type==='codeBlock')):null
    switch(node.type) {
      case 'doc':return <Fragment key={key}>{children()}</Fragment>
      case 'paragraph':return <p key={key}>{children()}</p>
      case 'heading':{
        const level=attrs.level
        const content=children()
        if(level===1)return <h1 key={key}>{content}</h1>
        if(level===3)return <h3 key={key}>{content}</h3>
        if(level===4)return <h4 key={key}>{content}</h4>
        if(level===5)return <h5 key={key}>{content}</h5>
        if(level===6)return <h6 key={key}>{content}</h6>
        return <h2 key={key}>{content}</h2>
      }
      case 'bulletList':return <ul key={key}>{children()}</ul>
      case 'orderedList':return <ol key={key} start={typeof attrs.start==='number'&&Number.isSafeInteger(attrs.start)&&attrs.start>0?attrs.start:1}>{children()}</ol>
      case 'listItem':return <li key={key}>{children()}</li>
      case 'blockquote':return <blockquote key={key}>{children()}</blockquote>
      case 'codeBlock':return <div className="answer-code" key={key}>{typeof attrs.language==='string'&&attrs.language?<small>{attrs.language}</small>:null}<pre><code>{children()}</code></pre></div>
      case 'hardBreak':return <br key={key}/>
      case 'horizontalRule':return <hr key={key}/>
      case 'inlineMath':case 'blockMath':return <Formula key={key} latex={typeof attrs.latex==='string'?attrs.latex:''} block={node.type==='blockMath'}/>
      default:return null
    }
  }
  return <div className="chat-formatted-answer">{render(body,'answer')}</div>
}
