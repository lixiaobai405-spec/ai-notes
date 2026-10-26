export interface TextSelectionInput { note_id:string; revision:number; start:number; end:number; quote:string }
export interface AskTextSelection { requestKey:string; input:TextSelectionInput; title:string }
export interface TextRange { start:number; end:number; quote:string }
type JsonNode={type?:string;text?:string;attrs?:Record<string,unknown>;content?:JsonNode[]}
interface TextUnit { value:string; from:number; to:number; atomic:boolean }
interface Projection { text:string; units:TextUnit[] }
const separated=new Set(['doc','bulletList','orderedList','listItem','blockquote'])
const containers=new Set(['doc','paragraph','heading','codeBlock','bulletList','orderedList','listItem','blockquote'])
const hasContent=(node:JsonNode)=>containers.has(node.type||'')||Array.isArray(node.content)

function size(node:JsonNode):number {
  if(node.type==='text')return (node.text||'').length
  if(node.type==='doc')return (node.content||[]).reduce((total,child)=>total+size(child),0)
  return hasContent(node)?((node.content||[]).reduce((total,child)=>total+size(child),0)+2):1
}

/** The saved JSON projection is shared with the server; editor positions use UTF-16. */
export function projectText(body:Record<string,unknown>):Projection {
  const units:TextUnit[]=[]
  function append(value:string,from:number,to:number,atomic:boolean) {
    let offset=from
    for(const character of value){units.push({value:character,from:atomic?from:offset,to:atomic?to:offset+character.length,atomic});offset+=character.length}
  }
  function walk(node:JsonNode,position:number,root=false):void {
    if(node.type==='inlineMath'||node.type==='blockMath'){append(String(node.attrs?.latex||''),position,position+size(node),true);return}
    if(node.type==='hardBreak'){append('\n',position,position+1,true);return}
    if(node.text){append(node.text,position,position+node.text.length,false);return}
    const children=node.content||[]
    let cursor=root?position:position+1
    let previousEnd=cursor
    children.forEach((child,index)=>{
      const childSize=size(child)
      const childContentStart=hasContent(child)?cursor+1:cursor
      if(index&&separated.has(node.type||''))units.push({value:'\n',from:previousEnd,to:childContentStart,atomic:false})
      walk(child,cursor)
      previousEnd=hasContent(child)?cursor+childSize-1:cursor+childSize
      cursor+=childSize
    })
  }
  walk(body as JsonNode,0,true)
  return {text:units.map(unit=>unit.value).join(''),units}
}

export function canonicalText(body:Record<string,unknown>):string {
  const node=body as JsonNode
  if(node.type==='inlineMath'||node.type==='blockMath')return String(node.attrs?.latex||'')
  if(node.type==='hardBreak')return '\n'
  if(node.text)return node.text
  return (node.content||[]).map(child=>canonicalText(child as Record<string,unknown>)).join(separated.has(node.type||'')?'\n':'')
}

export function selectionFromJson(body:Record<string,unknown>,from:number,to:number,maxLength=8000):TextRange|null {
  if(!Number.isSafeInteger(from)||!Number.isSafeInteger(to)||from<0||to<=from)return null
  if(to>size(body as JsonNode))return null
  const {units}=projectText(body)
  const selected:number[]=[]
  for(let index=0;index<units.length;index++){
    const unit=units[index]
    // A surrogate pair and a formula atom must be included in their entirety.
    if((unit.atomic||unit.value.length>1)&&((from>unit.from&&from<unit.to)||(to>unit.from&&to<unit.to)))throw new Error('请完整选择文字或公式，不能截断字符。')
    if(unit.from===unit.to?from<unit.from&&unit.to<to:from<=unit.from&&unit.to<=to)selected.push(index)
  }
  if(!selected.length)return null
  const start=selected[0],end=selected[selected.length-1]+1
  if(selected.length!==end-start)throw new Error('请选择一个连续的文字片段。')
  const quote=units.slice(start,end).map(unit=>unit.value).join('')
  if(!quote.trim())return null
  if(end-start>maxLength)throw new Error(`每次最多选择 ${maxLength} 个字符，请缩短选段。`)
  return {start,end,quote}
}
