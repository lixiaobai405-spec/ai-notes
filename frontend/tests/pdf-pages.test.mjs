import assert from 'node:assert/strict'
import {test} from 'node:test'
import {parsePages,pageRange} from '../src/features/ai/pdf-pages.ts'
test('PDF page selection parses Chinese separators, ranges, duplicates and original order',()=>{
  assert.deepEqual(parsePages('3，1、4-6, 5',10),[1,3,4,5,6])
  assert.equal(pageRange([1,3,4,5,6,9]),'1、3-6、9')
  assert.deepEqual(parsePages(pageRange([1,3,4,5,6,9]),10),[1,3,4,5,6,9])
})
test('invalid or unbounded PDF ranges are rejected before submitting',()=>{
  for(const value of ['', '0', '4-2', '1-999999999', '1,', 'abc', '2.5', '11'])assert.throws(()=>parsePages(value,10))
})
