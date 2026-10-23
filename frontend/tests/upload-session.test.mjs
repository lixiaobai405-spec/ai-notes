import assert from 'node:assert/strict'
import {test} from 'node:test'
import {authSession,readSession} from '../src/session.ts'
import {sendContent} from '../src/features/files/upload.ts'
import {signIn,tick} from './tab-environment.mjs'

const upload={id:'synthetic-upload',upload_url:'/api/v1/uploads/synthetic-upload/content',upload_method:'PUT'}
const file=new File(['synthetic'],'image.png',{type:'image/png'})
function environment(){
  const original=globalThis.XMLHttpRequest,requests=[]
  class XHR {
    constructor(){this.upload={};this.aborted=false;this.headers={};requests.push(this)}
    open(){}setRequestHeader(name,value){this.headers[name]=value}send(){this.sent=true}
    abort(){this.aborted=true;this.onabort?.()}
  }
  globalThis.XMLHttpRequest=XHR;signIn('synthetic-upload-owner','a')
  return{requests,restore(){globalThis.XMLHttpRequest=original}}
}
test('upload waiting for its tab context never sends under replacement credentials',async()=>{
  const env=environment()
  try{const pending=sendContent(upload,file,()=>assert.fail('no progress expected'),new AbortController().signal);signIn('new-upload-owner','b');await assert.rejects(pending,{name:'AbortError'});assert.equal(env.requests.length,0)}finally{env.restore()}
})
test('upload uses one Bearer and CSRF pair and keeps progress support',async()=>{
  const env=environment(),progress=[]
  try{
    const pending=sendContent(upload,file,value=>progress.push(value),new AbortController().signal);await tick()
    const request=env.requests[0];assert.equal(request.headers.Authorization,'Bearer '+'a'.repeat(43));assert.equal(request.headers['X-CSRF-Token'],readSession().csrf_token);assert.equal(request.withCredentials,false)
    request.upload.onprogress({lengthComputable:true,loaded:5,total:10});request.status=200;request.responseText=JSON.stringify(upload);request.onload()
    assert.deepEqual(await pending,upload);assert.deepEqual(progress,[50])
  }finally{env.restore()}
})
test('session invalidation aborts in-flight upload and ignores late progress or result',async()=>{
  const env=environment(),progress=[]
  try{
    const pending=sendContent(upload,file,value=>progress.push(value),new AbortController().signal),rejected=assert.rejects(pending,{name:'AbortError'});await tick()
    assert.equal(env.requests.length,1);const request=env.requests[0];assert.equal(request.sent,true)
    signIn('new-upload-owner','b');await rejected;assert.equal(request.aborted,true)
    request.upload.onprogress({lengthComputable:true,loaded:10,total:10});request.status=200;request.responseText=JSON.stringify(upload);request.onload()
    assert.deepEqual(progress,[])
  }finally{env.restore()}
})
test('an unauthorized upload clears only the current tab credentials',async()=>{
  const env=environment()
  try{const pending=sendContent(upload,file,()=>{},new AbortController().signal),rejected=assert.rejects(pending,{name:'AbortError'});await tick();const request=env.requests[0];request.status=401;request.responseText='{"code":"login_required"}';request.onload();await rejected;assert.equal(readSession(),null);assert.equal(authSession.snapshot().phase,'anonymous')}finally{env.restore()}
})