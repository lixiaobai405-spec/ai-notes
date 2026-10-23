import assert from 'node:assert/strict'
import {test} from 'node:test'
import {dragRegion,imagePoint,percentRegion,regionPercent} from '../src/features/ai/image-region.ts'

test('image coordinates exclude padded viewport and clamp dragging outside edges',()=>{
  const rect={left:120,top:80,width:400,height:800}
  assert.deepEqual(imagePoint({x:220,y:480},rect),{x:.25,y:.5})
  assert.deepEqual(imagePoint({x:0,y:1000},rect),{x:0,y:1})
  assert.throws(()=>imagePoint({x:0,y:0},{...rect,width:0}))
})
test('reverse drags retain image geometry and zero area is not a selection',()=>{
  assert.deepEqual(dragRegion({x:.8,y:.7},{x:.2,y:.1}),{x:.2,y:.1,width:.6,height:.6})
  assert.equal(dragRegion({x:.3,y:.4},{x:.3,y:.8}),null)
  assert.deepEqual(dragRegion({x:-1,y:2},{x:3,y:-4}),{x:0,y:0,width:1,height:1})
})
test('keyboard percentage selections round trip and reject invalid or out of image input',()=>{
  const region=percentRegion(['10','20','30','40'])
  assert.deepEqual(region,{x:.1,y:.2,width:.3,height:.4})
  assert.deepEqual(regionPercent(region),['10','20','30','40'])
  for(const values of [['','','',''],['NaN','0','20','20'],['0','0','0','20'],['90','0','20','20'],['-1','0','20','20'],['0','0','Infinity','20']])assert.throws(()=>percentRegion(values))
})
