import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readStream } from '../src/features/ai/chat-stream.ts'
import { authSession } from '../src/session.ts'
import {signIn,tick} from './tab-environment.mjs'
signIn('stream-owner','s')

test('SSE parsing preserves split UTF-8, split CRLF, comments and multiple events',async()=>{
  const original=globalThis.fetch, requests=[]
  const bytes=new TextEncoder().encode(': heartbeat\r\n\r\nevent: meta\r\ndata: {"answer":"首🙂"}\r\n\r\nevent: delta\r\ndata: {"offset":2,"text":"尾"}\r\n\r\nevent: done\r\ndata: {"status":"succeeded"}\r\n\r\n')
  globalThis.fetch=async(url,options)=>{
    requests.push({url,options})
    return new Response(new ReadableStream({start(controller){for(const byte of bytes)controller.enqueue(new Uint8Array([byte]));controller.close()}}))
  }
  try{
    const received=[]
    await readStream('existing-run',new AbortController().signal,(name,data)=>received.push({name,data}))
    assert.deepEqual(received,[{name:'meta',data:{answer:'首🙂'}},{name:'delta',data:{offset:2,text:'尾'}},{name:'done',data:{status:'succeeded'}}])
    assert.equal(requests.length,1)
    assert.equal(requests[0].url,'/api/v1/ai/runs/existing-run/events')
    assert.equal(requests[0].options.credentials,'omit');assert.equal(requests[0].options.headers.get('Authorization'),'Bearer '+'s'.repeat(43))
    assert.equal(requests[0].options.method,undefined) // Read-only GET; no generation on reconnect.
  }finally{globalThis.fetch=original}
})

test('unexpected EOF is explicit and does not silently retry or lose delivered text',async()=>{
  const original=globalThis.fetch, received=[];let calls=0
  globalThis.fetch=async()=>{calls++;return new Response('event: delta\ndata: {"offset":0,"text":"已保存"}\n\n')}
  try{
    await assert.rejects(()=>readStream('run',new AbortController().signal,(name,data)=>received.push(data)),/连接已断开/)
    assert.equal(calls,1);assert.equal(received[0].text,'已保存')
  }finally{globalThis.fetch=original}
})

test('multiple valid events can share a network chunk larger than the single-event limit',async()=>{
  const original=globalThis.fetch, answer='a'.repeat(600_000)
  const meta=`event: meta\ndata: ${JSON.stringify({answer})}\n\n`
  const done=`event: done\ndata: ${JSON.stringify({answer,status:'succeeded'})}\n\n`
  assert.ok(meta.length<1_000_000&&done.length<1_000_000&&meta.length+done.length>1_000_000)
  const bytes=new TextEncoder().encode(meta+done)
  globalThis.fetch=async()=>new Response(new ReadableStream({start(controller){controller.enqueue(bytes);controller.close()}}))
  try{
    const received=[]
    await readStream('run',new AbortController().signal,(name,data)=>received.push({name,data}))
    assert.deepEqual(received,[{name:'meta',data:{answer}},{name:'done',data:{answer,status:'succeeded'}}])
  }finally{globalThis.fetch=original}
})

test('a complete event larger than the single-event limit is rejected without delivering it',async()=>{
  const original=globalThis.fetch, received=[];let calls=0
  const block=`event: done\ndata: ${JSON.stringify({answer:'a'.repeat(1_000_000)})}\n\n`
  globalThis.fetch=async()=>{calls++;return new Response(block)}
  try{
    await assert.rejects(()=>readStream('run',new AbortController().signal,(name,data)=>received.push(data)),/回复格式异常/)
    assert.equal(calls,1);assert.deepEqual(received,[])
  }finally{globalThis.fetch=original}
})

test('an unfinished event larger than the limit is rejected after delivering preceding valid events',async()=>{
  const original=globalThis.fetch, received=[];let calls=0
  const prefix='event: delta\ndata: {"offset":0,"text":"已保存"}\n\n'
  const unfinished='event: done\ndata: '+ 'a'.repeat(1_000_001)
  globalThis.fetch=async()=>{calls++;return new Response(prefix+unfinished)}
  try{
    await assert.rejects(()=>readStream('run',new AbortController().signal,(name,data)=>received.push({name,data})),/回复格式异常/)
    assert.equal(calls,1);assert.deepEqual(received,[{name:'delta',data:{offset:0,text:'已保存'}}])
  }finally{globalThis.fetch=original}
})

test('late stream headers from a previous session are cancelled without delivering private events',async()=>{
  const original=globalThis.fetch,received=[];let resolve,cancelled=false
  signIn('stream-old-owner')
  globalThis.fetch=()=>new Promise(yes=>{resolve=yes})
  try{
    const pending=readStream('old-run',new AbortController().signal,(name,data)=>received.push({name,data}))
    const rejected=assert.rejects(pending,{name:'AbortError'})
    await tick()
    authSession.invalidate(authSession.ticket())
    signIn('stream-new-owner')
    resolve(new Response(new ReadableStream({cancel(){cancelled=true}})))
    await rejected
    assert.deepEqual(received,[]);assert.equal(cancelled,true)
  }finally{globalThis.fetch=original}
})

test('logout while reading a stream rejects buffered events and releases the reader',async()=>{
  const original=globalThis.fetch,received=[];let streamController,first,cancelled=false
  const firstEvent=new Promise(resolve=>{first=resolve})
  signIn('stream-reading-owner')
  globalThis.fetch=async()=>new Response(new ReadableStream({start(controller){streamController=controller},cancel(){cancelled=true}}))
  try{
    const pending=readStream('old-run',new AbortController().signal,(name,data)=>{received.push({name,data});first()})
    await tick()
    streamController.enqueue(new TextEncoder().encode('event: meta\ndata: {"answer":"已接收"}\n\n'))
    await firstEvent
    authSession.invalidate(authSession.ticket())
    streamController.enqueue(new TextEncoder().encode('event: done\ndata: {"answer":"退出后不能出现"}\n\n'))
    await assert.rejects(pending,{name:'AbortError'})
    assert.deepEqual(received,[{name:'meta',data:{answer:'已接收'}}]);assert.equal(cancelled,true)
  }finally{globalThis.fetch=original}
})
