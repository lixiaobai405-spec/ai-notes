export type Region={x:number;y:number;width:number;height:number}
export type Point={x:number;y:number}
const clamp=(value:number)=>Math.min(1,Math.max(0,value))
const rounded=(value:number)=>Math.round(value*1_000_000)/1_000_000

// Pointer coordinates are measured against the displayed image, never its padded viewport.
export function imagePoint(client:Point,rect:{left:number;top:number;width:number;height:number}):Point {
  if(rect.width<=0||rect.height<=0)throw new Error('图片尚未加载完成')
  return {x:clamp((client.x-rect.left)/rect.width),y:clamp((client.y-rect.top)/rect.height)}
}
export function dragRegion(start:Point,end:Point):Region|null {
  const x=rounded(Math.min(clamp(start.x),clamp(end.x))),y=rounded(Math.min(clamp(start.y),clamp(end.y)))
  const width=rounded(Math.max(clamp(start.x),clamp(end.x))-x),height=rounded(Math.max(clamp(start.y),clamp(end.y))-y)
  return width>0&&height>0?{x,y,width,height}:null
}
export function percentRegion(values:string[]):Region {
  if(values.length!==4||values.some(value=>!value.trim()||!Number.isFinite(Number(value))))throw new Error('请填写四个有效的百分比')
  const [x,y,width,height]=values.map(value=>rounded(Number(value)/100))
  if(x<0||y<0||width<=0||height<=0||x>1||y>1||width>1||height>1||x+width>1.000001||y+height>1.000001)throw new Error('选区必须在图片内，宽高大于 0，位置加宽高不超过 100%')
  return {x,y,width:rounded(Math.min(width,1-x)),height:rounded(Math.min(height,1-y))}
}
export function regionPercent(region:Region):string[] {
  return [region.x,region.y,region.width,region.height].map(value=>String(Math.round(value*10_000)/100))
}
export function describeRegion(region:Region):string {
  const [x,y,width,height]=regionPercent(region)
  return `选区：左 ${x}% · 上 ${y}% · 宽 ${width}% · 高 ${height}%`
}
