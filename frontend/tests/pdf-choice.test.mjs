import assert from 'node:assert/strict'
import {test} from 'node:test'
import {buildPdfInput,changePdfBook,changePdfMode,changePdfPage,confirmPdfRevision,emptyChoice,pdfReady,restorePdfChoice} from '../src/features/ai/pdf-choice.ts'

const region={x:.1,y:.2,width:.3,height:.4}
const book={id:'book-a',revision:4,page_count:3,pages:[{page_id:'page-a',file_id:'file-a',number:1},{page_id:'page-b',file_id:'file-b',number:2},{page_id:'page-c',file_id:'file-c',number:3}]}
const selected={...emptyChoice,noteId:book.id,mode:'region',pageId:'page-b',region,sourceRevision:4}

test('PDF region submits one original page and keeps PDF semantics',()=>{
  assert.deepEqual(buildPdfInput(selected,book),{note_id:'book-a',revision:4,mode:'region',page_numbers:[2],region})
  assert.deepEqual(buildPdfInput({...selected,mode:'pages',range:'1、3'},book),{note_id:'book-a',revision:4,mode:'pages',page_numbers:[1,3],region:null})
  assert.deepEqual(buildPdfInput({...selected,mode:'all'},book),{note_id:'book-a',revision:4,mode:'all',page_numbers:[],region:null})
})
test('PDF selection rejects invalid ranges, missing pages and empty or invalid rectangles',()=>{
  for(const range of ['0','4','1-4','2.5',''])assert.throws(()=>buildPdfInput({...selected,mode:'pages',range},book))
  for(const value of [null,{...region,width:0},{...region,x:NaN},{...region,width:1},{...region,height:-1}])assert.throws(()=>buildPdfInput({...selected,region:value},book))
  assert.throws(()=>buildPdfInput({...selected,pageId:'deleted'},book),/已失效/)
  assert.throws(()=>buildPdfInput(selected,{...book,id:'old-book'}),/读取/)
  const largeBook={...book,page_count:201,pages:Array.from({length:201},(_,index)=>({page_id:'page-'+index,file_id:'file-'+index,number:index+1}))}
  assert.equal(buildPdfInput({...selected,mode:'pages',range:'1-200'},largeBook).page_numbers.length,200)
  assert.throws(()=>buildPdfInput({...selected,mode:'pages',range:'1-201'},largeBook))
  assert.deepEqual(buildPdfInput({...selected,mode:'pages',range:'201'},largeBook).page_numbers,[201])
})
test('switching book, page or mode clears region and revision confirmation',()=>{
  const confirmed={...selected,confirmedRevision:4}
  assert.deepEqual(changePdfBook(confirmed,'book-b'),{...emptyChoice,noteId:'book-b'})
  assert.equal(changePdfMode(confirmed,'all').region,null)
  assert.equal(changePdfMode(confirmed,'all').pageId,'')
  assert.equal(changePdfMode(confirmed,'all').confirmedRevision,undefined)
  assert.equal(changePdfPage(confirmed,'page-c',4).region,null)
  assert.equal(changePdfPage(confirmed,'page-c',4).pageId,'page-c')
  assert.equal(changePdfPage(confirmed,'page-c',4).confirmedRevision,undefined)
})
test('history restores stable page id and region, requiring current revision confirmation',()=>{
  const choice=restorePdfChoice({note_id:'book-a',revision:3,mode:'region',pages:[{page_id:'page-b',number:1,region}]})
  assert.equal(choice.pageId,'page-b');assert.deepEqual(choice.region,region);assert.equal(choice.sourceRevision,3)
  assert.throws(()=>buildPdfInput(choice,book),/版本/)
  const confirmed=confirmPdfRevision(choice,book)
  assert.deepEqual(buildPdfInput(confirmed,book).page_numbers,[2])
  assert.throws(()=>buildPdfInput(confirmed,{...book,revision:5}),/版本/)
  assert.throws(()=>buildPdfInput(confirmed,{...book,pages:book.pages.filter(page=>page.page_id!=='page-b')}),/已失效/)
  const legacy=restorePdfChoice({note_id:'book-a',revision:4,mode:'pages',pages:[{page_id:'page-a',number:1},{page_id:'page-c',number:3}]})
  assert.equal(legacy.range,'1、3');assert.equal(legacy.region,null)
})
test('ready requires matching current payload, loaded page identity, and completed preview',()=>{
  const input=buildPdfInput(selected,book)
  const preview={source:{note_id:book.id,revision:4,mode:'region',pages:[{page_id:'page-b',number:2,region}]}}
  const status={input,choice:selected,book,preview,fetching:false,error:null,loadedImage:'4:page-b:file-b'}
  assert.equal(pdfReady(status),true)
  for(const change of [{loadedImage:''},{loadedImage:'3:page-b:file-b'},{loadedImage:'4:page-a:file-a'},{fetching:true},{error:new Error('failed')},{preview:undefined},{input:null},{book:{...book,id:'other'}}])assert.equal(pdfReady({...status,...change}),false)
  assert.equal(pdfReady({...status,preview:{source:{...preview.source,revision:3}}}),false)
  assert.equal(pdfReady({...status,preview:{source:{...preview.source,pages:[{...preview.source.pages[0],region:{...region,x:.2}}]}}}),false)
  assert.equal(pdfReady({...status,preview:{source:{...preview.source,mode:'pages'}}}),false)
  assert.equal(pdfReady({...status,input:{...input,region:null}}),false)
  const pages=buildPdfInput({...selected,mode:'pages',range:'1'},book)
  assert.equal(pdfReady({...status,input:pages,choice:{...selected,mode:'pages',range:'1'},loadedImage:'',preview:{source:{...preview.source,mode:'pages',pages:[{page_id:'page-a',number:1}]}}}),true)
})
