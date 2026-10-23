import assert from 'node:assert/strict'
import {test} from 'node:test'
import {readPrivateFile} from '../src/features/files/download.ts'
import {SessionChangedError,readSession} from '../src/session.ts'
import {signIn,tick} from './tab-environment.mjs'

test('PDF originals and reading/thumbnail images use Bearer for tickets and bytes',async()=>{
  const original=globalThis.fetch
  try{for(const kind of ['original','reading','thumbnail']){
    signIn('owner-a','a');const requests=[]
    globalThis.fetch=async(url,options)=>{requests.push({url,options});return requests.length===1?new Response(JSON.stringify({url:'/api/v1/files/file-qa/content?ticket=opaque'})):new Response('private bytes')}
    const blob=await readPrivateFile('file-qa',kind,new AbortController().signal)
    assert.equal(await blob.text(),'private bytes')
    assert.deepEqual(requests.map(r=>r.url),['/api/v1/files/file-qa/ticket?kind='+kind,'/api/v1/files/file-qa/content?ticket=opaque'])
    for(const {options} of requests){assert.equal(options.credentials,'omit');assert.equal(options.redirect,'error');assert.equal(options.headers.get('Authorization'),'Bearer '+'a'.repeat(43));assert.equal(options.headers.get('X-CSRF-Token'),readSession().csrf_token)}
  }}finally{globalThis.fetch=original}
})
test('a ticket cannot redirect private download credentials to another file or host',async()=>{
  const original=globalThis.fetch
  try{for(const url of ['https://example.invalid/api/v1/files/file-qa/content','/api/v1/files/another-file/content']){
    signIn('owner-a','a');let calls=0;globalThis.fetch=async()=>{calls++;return new Response(JSON.stringify({url}))}
    await assert.rejects(readPrivateFile('file-qa','original',new AbortController().signal),/私有访问规则/);assert.equal(calls,1)
  }}finally{globalThis.fetch=original}
})
test('private bytes read under an old identity are discarded before object URL creation',async()=>{
  const original=globalThis.fetch;signIn('owner-a','a');let resolve,calls=0
  globalThis.fetch=async()=>++calls===1?new Response(JSON.stringify({url:'/api/v1/files/file-qa/content?ticket=opaque'})):{ok:true,status:200,blob:()=>new Promise(yes=>{resolve=yes})}
  try{
    const pending=readPrivateFile('file-qa','reading',new AbortController().signal),rejected=assert.rejects(pending,SessionChangedError)
    for(let i=0;i<20&&!resolve;i++)await tick()
    assert.ok(resolve);signIn('owner-b','b');resolve(new Blob(['old private image']));await rejected
    assert.equal(readSession().session_token,'b'.repeat(43))
  }finally{globalThis.fetch=original}
})