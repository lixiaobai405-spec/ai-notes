import {pageRange,parsePages} from './pdf-pages.ts'
import type {Region} from './image-region.ts'

export type PdfChoice={noteId:string;mode:'pages'|'all'|'region';range:string;pageId:string;region:Region|null;sourceRevision?:number;confirmedRevision?:number}
export const emptyChoice:PdfChoice={noteId:'',mode:'pages',range:'1',pageId:'',region:null}
type PdfPage={page_id:string;file_id:string;number:number}
type PdfBook={id:string;revision:number;page_count:number;pages:PdfPage[]}
type PdfSource={note_id:string;revision:number;mode:PdfChoice['mode'];pages:{page_id:string;number:number;region?:Region|null}[]}
export type PdfPayload={note_id:string;revision:number;mode:PdfChoice['mode'];page_numbers:number[];region:Region|null}

export function changePdfBook(_choice:PdfChoice,noteId:string):PdfChoice{return {...emptyChoice,noteId}}
export function changePdfMode(choice:PdfChoice,mode:PdfChoice['mode']):PdfChoice{return {...choice,mode,pageId:'',region:null,confirmedRevision:undefined}}
export function changePdfPage(choice:PdfChoice,pageId:string,revision:number):PdfChoice{return {...choice,pageId,region:null,sourceRevision:choice.sourceRevision??revision,confirmedRevision:undefined}}
export function confirmPdfRevision(choice:PdfChoice,book:PdfBook):PdfChoice {
  if(book.id!==choice.noteId)throw new Error('正在读取所选 PDF，请稍候')
  return {...choice,confirmedRevision:book.revision}
}
export function restorePdfChoice(source:PdfSource):PdfChoice {
  const page=source.pages[0]
  return {...emptyChoice,noteId:source.note_id,mode:source.mode,range:pageRange(source.pages.map(page=>page.number)),pageId:source.mode==='region'?page?.page_id||'':'',region:source.mode==='region'?page?.region||null:null,sourceRevision:source.revision}
}
export function pdfImageIdentity(choice:PdfChoice,book?:PdfBook):string {
  const page=book?.id===choice.noteId?book.pages.find(page=>page.page_id===choice.pageId):undefined
  return page?`${book!.revision}:${page.page_id}:${page.file_id}`:''
}
function validRegion(region:Region|null):region is Region {
  return !!region&&[region.x,region.y,region.width,region.height].every(Number.isFinite)&&region.x>=0&&region.y>=0&&region.width>0&&region.height>0&&region.x+region.width<=1.000001&&region.y+region.height<=1.000001
}
export function buildPdfInput(choice:PdfChoice,book?:PdfBook):PdfPayload {
  if(!choice.noteId||book?.id!==choice.noteId)throw new Error('正在读取所选 PDF，请稍候')
  // A historical stable page id must never silently fall back to its old page number.
  const page=choice.mode==='region'?book.pages.find(page=>page.page_id===choice.pageId):undefined
  if(choice.mode==='region'&&!page)throw new Error(choice.pageId?'原 PDF 页面已失效，请重新选择页面':'请选择一个原 PDF 页面')
  if(choice.sourceRevision!==undefined&&choice.sourceRevision!==book.revision&&choice.confirmedRevision!==book.revision)throw new Error('来源版本已变化，请核对当前原页和选区后确认使用当前版本')
  if(choice.mode==='region'){
    if(!validRegion(choice.region))throw new Error('请先在原 PDF 页图内画框或填写有效百分比选区')
    return {note_id:choice.noteId,revision:book.revision,mode:'region',page_numbers:[page!.number],region:choice.region}
  }
  return {note_id:choice.noteId,revision:book.revision,mode:choice.mode,page_numbers:choice.mode==='all'?[]:parsePages(choice.range,book.page_count),region:null}
}
const sameRegion=(left:Region|null|undefined,right:Region|null|undefined)=>left&&right?left.x===right.x&&left.y===right.y&&left.width===right.width&&left.height===right.height:!left&&!right
export function pdfReady({input,choice,book,preview,fetching,error,loadedImage}:{input:PdfPayload|null;choice:PdfChoice;book?:PdfBook;preview?:{source:PdfSource};fetching:boolean;error:unknown;loadedImage:string}):boolean {
  if(!input||!book||!preview||fetching||error)return false
  let current:PdfPayload
  try{current=buildPdfInput(choice,book)}catch{return false}
  if(JSON.stringify(current)!==JSON.stringify(input))return false
  const source=preview.source
  if(source.note_id!==input.note_id||source.revision!==input.revision||source.mode!==input.mode)return false
  const numbers=input.mode==='all'?book.pages.map(page=>page.number):input.page_numbers
  if(source.pages.length!==numbers.length)return false
  if(!source.pages.every(page=>numbers.includes(page.number)&&book.pages.some(current=>current.number===page.number&&current.page_id===page.page_id)&&sameRegion(page.region,input.region)))return false
  if(new Set(source.pages.map(page=>page.page_id)).size!==source.pages.length)return false
  return input.mode!=='region'||loadedImage===pdfImageIdentity(choice,book)
}
