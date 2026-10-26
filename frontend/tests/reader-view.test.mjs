import assert from 'node:assert/strict'
import {test} from 'node:test'
import {regionView} from '../src/features/files/reader-view.ts'

const close=(actual,expected)=>assert.ok(Math.abs(actual-expected)<1e-8,`${actual} != ${expected}`)

test('source selection is centered using actual image geometry and viewport padding',()=>{
  const image={left:100,top:0,width:400,height:600},viewport={width:600,height:600}
  const region={x:.5,y:.5,width:.5,height:.5}
  const view=regionView(region,image,viewport)
  assert.ok(view)
  const x=image.left+(region.x+region.width/2)*image.width
  const y=image.top+(region.y+region.height/2)*image.height
  close(view.x+viewport.width/2+view.scale*(x-viewport.width/2),viewport.width/2)
  close(view.y+viewport.height/2+view.scale*(y-viewport.height/2),viewport.height/2)
  close(view.scale,1.6)
})

test('tiny corner selections are capped at 600 percent and full pages stay readable',()=>{
  const image={left:0,top:80,width:390,height:500},viewport={width:390,height:660}
  const tiny=regionView({x:.97,y:.98,width:.02,height:.01},image,viewport)
  assert.equal(tiny.scale,6)
  assert.ok(tiny.x<0&&tiny.y<0)
  const full=regionView({x:0,y:0,width:1,height:1},image,viewport)
  assert.equal(full.scale,1)
  close(full.x,0);close(full.y,0)
})

test('unloaded geometry and invalid rectangles never create a NaN transform',()=>{
  const image={left:0,top:0,width:800,height:600},viewport={width:800,height:600}
  for(const bad of [{x:0,y:0,width:0,height:.5},{x:.9,y:0,width:.2,height:.5},
                     {x:NaN,y:0,width:.5,height:.5},{x:0,y:0,width:Infinity,height:.5}]){
    assert.equal(regionView(bad,image,viewport),null)
  }
  assert.equal(regionView({x:0,y:0,width:.5,height:.5},{...image,width:0},viewport),null)
  assert.equal(regionView({x:0,y:0,width:.5,height:.5},image,{...viewport,height:0}),null)
})

test('portrait and landscape viewport changes retain the same normalized source center',()=>{
  const region={x:.1,y:.75,width:.3,height:.2}
  for(const [image,viewport] of [
    [{left:0,top:0,width:390,height:520},{width:390,height:520}],
    [{left:200,top:0,width:400,height:600},{width:800,height:600}],
  ]){
    const view=regionView(region,image,viewport)
    const cx=image.left+(region.x+region.width/2)*image.width
    const cy=image.top+(region.y+region.height/2)*image.height
    close(view.x+view.scale*(cx-viewport.width/2),0)
    close(view.y+view.scale*(cy-viewport.height/2),0)
  }
})
