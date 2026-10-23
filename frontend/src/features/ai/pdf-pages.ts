export function parsePages(value:string, max:number):number[] {
  if(!value.trim())throw new Error('请输入原 PDF 页码，例如 2、4-6')
  const selected=new Set<number>()
  for(const token of value.replace(/[，、]/g,',').split(',')) {
    const match=token.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/)
    if(!match)throw new Error('页码格式应为 2、4-6，使用逗号分隔')
    const start=Number(match[1]),end=Number(match[2]||match[1])
    if(start<1||end<start||end>max||end-start>199)throw new Error(`页码须在 1–${max} 内，起始页不能大于结束页`)
    for(let n=start;n<=end;n++)selected.add(n)
  }
  if(selected.size>200)throw new Error('一次最多选择 200 页')
  return [...selected].sort((a,b)=>a-b)
}

export function pageRange(numbers:number[]):string {
  const groups:string[]=[]
  for(let i=0;i<numbers.length;i++){
    const start=numbers[i];let end=start
    while(numbers[i+1]===end+1){end=numbers[++i]}
    groups.push(start===end?String(start):`${start}-${end}`)
  }
  return groups.join('、')
}
