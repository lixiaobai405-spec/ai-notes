import type { Region } from '../ai/image-region'

export type ReaderView={scale:number;x:number;y:number}
export type ImageGeometry={left:number;top:number;width:number;height:number}

// Image geometry is measured before the CSS transform, excluding viewport padding.
export function regionView(region:Region,image:ImageGeometry,viewport:{width:number;height:number}):ReaderView|null {
  const values=[region.x,region.y,region.width,region.height,image.left,image.top,image.width,image.height,viewport.width,viewport.height]
  if(values.some(value=>!Number.isFinite(value))||image.width<=0||image.height<=0||viewport.width<=0||viewport.height<=0||
     region.x<0||region.y<0||region.width<=0||region.height<=0||region.x+region.width>1+Number.EPSILON||region.y+region.height>1+Number.EPSILON)return null
  const scale=Math.min(6,Math.max(1,Math.min(viewport.width*.8/(region.width*image.width),viewport.height*.8/(region.height*image.height))))
  const cx=image.left+(region.x+region.width/2)*image.width,cy=image.top+(region.y+region.height/2)*image.height
  return {scale,x:scale*(viewport.width/2-cx),y:scale*(viewport.height/2-cy)}
}
