import assert from 'node:assert/strict'
import {test} from 'node:test'
import {Schema} from '@tiptap/pm/model'
import {canonicalText,projectText,selectionFromJson} from '../src/features/notes/text-selection.ts'

const text=value=>({type:'text',text:value})
const paragraph=(...content)=>({type:'paragraph',...(content.length?{content}:{})})
const doc=(...content)=>({type:'doc',content})

test('emoji editor positions are UTF-16 while source offsets count Unicode code points',()=>{
  const body=doc(paragraph(text('甲😀乙')))
  assert.equal(canonicalText(body),'甲😀乙')
  assert.deepEqual(selectionFromJson(body,2,4),{start:1,end:2,quote:'😀'})
  assert.deepEqual(selectionFromJson(body,4,5),{start:2,end:3,quote:'乙'})
  assert.throws(()=>selectionFromJson(body,2,3),/截断字符/)
  assert.throws(()=>selectionFromJson(body,3,5),/截断字符/)
})

test('repeated text is located by saved offsets rather than quote search',()=>{
  const body=doc(paragraph(text('重复 重复')))
  assert.deepEqual(selectionFromJson(body,4,6),{start:3,end:5,quote:'重复'})
})

test('cross-paragraph selection preserves canonical separators and hard breaks',()=>{
  const body=doc(paragraph(text('甲😀乙')),paragraph(text('丙'),{type:'hardBreak'},text('丁')))
  assert.equal(canonicalText(body),'甲😀乙\n丙\n丁')
  assert.deepEqual(selectionFromJson(body,2,10),{start:1,end:7,quote:'😀乙\n丙\n丁'})
  assert.deepEqual(selectionFromJson(body,7,10),{start:4,end:7,quote:'丙\n丁'})
})

test('empty paragraphs still occupy two editor positions and preserve both newlines',()=>{
  const body=doc(paragraph(text('A')),paragraph(),paragraph(text('B')))
  assert.equal(canonicalText(body),'A\n\nB')
  assert.deepEqual(selectionFromJson(body,6,7),{start:3,end:4,quote:'B'})
  assert.deepEqual(selectionFromJson(body,1,7),{start:0,end:4,quote:'A\n\nB'})
  assert.equal(selectionFromJson(body,2,6),null)
})

test('nested lists and blockquotes use the same JSON projection as saved plain text',()=>{
  const body=doc({type:'bulletList',content:[{type:'listItem',content:[paragraph(text('甲'))]},{type:'listItem',content:[paragraph(text('乙'))]}]},{type:'blockquote',content:[paragraph(text('丙'))]})
  const projection=projectText(body)
  assert.equal(projection.text,'甲\n乙\n丙')
  assert.deepEqual(selectionFromJson(body,3,15),{start:0,end:5,quote:'甲\n乙\n丙'})
  assert.deepEqual(selectionFromJson(body,8,9),{start:2,end:3,quote:'乙'})
})

test('formula atoms contribute their entire LaTeX and cannot be split',()=>{
  const body=doc(paragraph(text('甲'),{type:'inlineMath',attrs:{latex:'x😀+1'}},text('乙')),{type:'blockMath',attrs:{latex:'y^2'}})
  assert.equal(canonicalText(body),'甲x😀+1乙\ny^2')
  assert.deepEqual(selectionFromJson(body,2,3),{start:1,end:5,quote:'x😀+1'})
  assert.deepEqual(selectionFromJson(body,3,4),{start:5,end:6,quote:'乙'})
  assert.deepEqual(selectionFromJson(body,5,6),{start:7,end:10,quote:'y^2'})
})

test('adjacent block math separators are included only when selection crosses them',()=>{
  const body=doc({type:'blockMath',attrs:{latex:'x'}},{type:'blockMath',attrs:{latex:'y'}})
  assert.equal(canonicalText(body),'x\ny')
  assert.deepEqual(selectionFromJson(body,0,1),{start:0,end:1,quote:'x'})
  assert.deepEqual(selectionFromJson(body,1,2),{start:2,end:3,quote:'y'})
  assert.deepEqual(selectionFromJson(body,0,2),{start:0,end:3,quote:'x\ny'})
})

test('empty, whitespace, collapsed and invalid selections cannot enter a source',()=>{
  const body=doc(paragraph(text(' \n ')))
  assert.equal(selectionFromJson(body,1,4),null)
  for(const [from,to] of [[1,1],[2,1],[-1,2],[NaN,2],[1,Infinity],[1,100]])assert.equal(selectionFromJson(body,from,to),null)
  assert.equal(canonicalText({}),'')
})

test('8000 limit counts code points including emoji and formula text',()=>{
  const body=doc(paragraph(text('😀'.repeat(8000))))
  assert.equal(selectionFromJson(body,1,16001).end,8000)
  const tooLong=doc(paragraph(text('😀'.repeat(8001))))
  assert.throws(()=>selectionFromJson(tooLong,1,16003),/8000/)
  const formula=doc({type:'blockMath',attrs:{latex:'x'.repeat(8001)}})
  assert.throws(()=>selectionFromJson(formula,0,1),/8000/)
})

test('projection text positions match real ProseMirror nodes including empty blocks and nested containers',()=>{
  const schema=new Schema({nodes:{
    doc:{content:'block*'},text:{group:'inline'},paragraph:{content:'inline*',group:'block'},
    bulletList:{content:'listItem*',group:'block'},listItem:{content:'paragraph+'},blockquote:{content:'paragraph+',group:'block'},
    hardBreak:{inline:true,group:'inline'},inlineMath:{inline:true,group:'inline',atom:true,attrs:{latex:{default:''}}},blockMath:{group:'block',atom:true,attrs:{latex:{default:''}}},
  }})
  const body=doc(paragraph(),paragraph(text('e\u0301👩‍💻'),{type:'hardBreak'},{type:'inlineMath',attrs:{latex:'x😀'}}),{type:'bulletList',content:[{type:'listItem',content:[paragraph(text('重'))]},{type:'listItem',content:[paragraph(),paragraph(text('复'))]}]},{type:'blockquote',content:[paragraph(text('末'))]},{type:'blockMath',attrs:{latex:'y'}})
  const actual=schema.nodeFromJSON(body),projection=projectText(body)
  actual.descendants((node,position)=>{
    if(node.isText){
      let offset=position
      for(const character of node.text){assert.ok(projection.units.some(unit=>unit.value===character&&unit.from===offset&&unit.to===offset+character.length&&!unit.atomic));offset+=character.length}
    } else if(node.type.name==='inlineMath'||node.type.name==='blockMath'){
      assert.equal(node.nodeSize,1)
      assert.deepEqual(selectionFromJson(body,position,position+node.nodeSize)?.quote,node.attrs.latex)
    }
  })
  assert.equal(projection.text,'\ne\u0301👩‍💻\nx😀\n重\n\n复\n末\ny')
})
