"""Locate the one rendered Ads balance and prepare its text for window evidence.

Only scrolling changes. Financial text, styles, overlays and browser zoom are
never modified, and the original scroll positions are restored on every exit.
"""
from __future__ import annotations

from contextlib import contextmanager
import time
from uuid import uuid4


class LazadaAdsViewError(ValueError):
    pass


ADS_COMMON = r'''
/* lazada_ads_common */
const adsRendered = element => {
  if (!element || !element.isConnected) return false;
  for (let e=element;e;e=e.parentElement) {
    const style=getComputedStyle(e);
    if (style.display==='none' || ['hidden','collapse'].includes(style.visibility) || Number(style.opacity)===0) return false;
  }
  return true;
};
const adsTextParts = element => {
  if (!adsRendered(element)) return [];
  const walker=document.createTreeWalker(element,NodeFilter.SHOW_TEXT),parts=[];
  for(let node=walker.nextNode();node;node=walker.nextNode()) {
    if (!String(node.nodeValue||'').trim() || !adsRendered(node.parentElement)) continue;
    const range=document.createRange();range.selectNodeContents(node);
    const rects=[...range.getClientRects()].filter(r=>r.width>0 && r.height>0);
    if (rects.length) parts.push({node,parent:node.parentElement,rects});
  }
  return parts;
};
const adsText = element => adsTextParts(element).map(part=>part.node.nodeValue.trim()).join(' ').trim();
const adsResolve = () => {
  const labels=[...document.querySelectorAll('[data-i18n-key*="Available_Balance"]')].filter(e=>adsTextParts(e).length);
  if (labels.length!==1) return {label:null,value:null,error:'无法唯一确认 Ads 可用余额标签'};
  const label=labels[0],scope=label.parentElement?.parentElement;
  const values=[...scope?.querySelectorAll('[class*="accountBalanceValue"]')||[]].filter(e=>adsTextParts(e).length);
  if (values.length!==1) return {label,value:null,error:'无法唯一确认 Ads 可用余额金额'};
  return {label,value:values[0],scope,error:''};
};
const ads=adsResolve();
'''

_GEOMETRY = r'''
const adsParts = () => [ads.label,ads.value].flatMap(target=>adsTextParts(target).map(part=>({...part,target})));
const adsUnion = parts => {
  const rects=parts.flatMap(part=>part.rects);
  if (!rects.length) return null;
  return {left:Math.min(...rects.map(r=>r.left)),top:Math.min(...rects.map(r=>r.top)),
          right:Math.max(...rects.map(r=>r.right)),bottom:Math.max(...rects.map(r=>r.bottom))};
};
const adsContentBox = e => {
  const r=e.getBoundingClientRect(),sx=e.offsetWidth?r.width/e.offsetWidth:1,sy=e.offsetHeight?r.height/e.offsetHeight:1;
  return {left:r.left+e.clientLeft*sx,top:r.top+e.clientTop*sy,
          right:r.left+(e.clientLeft+e.clientWidth)*sx,bottom:r.top+(e.clientTop+e.clientHeight)*sy};
};
const adsProvenBehind = (element,textParent) => {
  // Compare only two explicit, positioned sibling stacking contexts. Numeric
  // z-index inside either context cannot escape its branch. Other stacking
  // relationships remain conservative; top-layer dialogs/popovers are exempt
  // from ordinary z-index ordering and must never use this shortcut.
  if(element.closest('dialog[open],[popover]') || textParent.closest('dialog[open],[popover]') ||
      document.fullscreenElement) return false;
  const overlayAncestors=new Set();
  for(let e=element;e;e=e.parentElement) overlayAncestors.add(e);
  let textBranch=textParent;
  while(textBranch?.parentElement && !overlayAncestors.has(textBranch.parentElement)) textBranch=textBranch.parentElement;
  const common=textBranch?.parentElement;
  if(!common) return false;
  let overlayBranch=element;
  while(overlayBranch && overlayBranch.parentElement!==common) overlayBranch=overlayBranch.parentElement;
  if(!overlayBranch || overlayBranch===textBranch) return false;
  const layer=e=>{
    const style=getComputedStyle(e);
    return ['relative','absolute','fixed','sticky'].includes(style.position) && /^-?\d+$/.test(style.zIndex)
      ? Number(style.zIndex) : null;
  };
  const overlayZ=layer(overlayBranch),textZ=layer(textBranch);
  return overlayZ!==null && textZ!==null && overlayZ<textZ;
};
const adsPaintedOverlays = () => {
  // Pointer-transparent overlays are omitted by elementFromPoint even though
  // their pixels can hide the financial text. Recognize their own paint,
  // rather than treating every large transparent navigation wrapper as paint.
  const opaqueColor = color => {
    if (!color || color==='transparent') return false;
    const rgba=color.match(/^rgba?\(([^)]+)\)$/i);
    if(rgba){const channels=rgba[1].split(',');return channels.length!==4 || Number(channels[3])>0;}
    const slash=color.match(/\/\s*([\d.]+)(%)?\s*\)$/);
    return !slash || Number(slash[1])>0;
  };
  const textParents=adsParts().map(part=>part.parent);
  return [...document.querySelectorAll('body *')].flatMap(element=>{
    const style=getComputedStyle(element);
    if (style.pointerEvents!=='none' || !['absolute','fixed'].includes(style.position) ||
        (style.zIndex!=='auto' && Number(style.zIndex)<0) || !adsRendered(element)) return [];
    // Exempt only the actual text and its ancestors. A painted, text-free
    // descendant inside the balance target can still cover the amount while
    // pointer-events:none makes hit testing pass through to the text below.
    if (textParents.some(parent=>element===parent || element.contains(parent))) return [];
    const painted=opaqueColor(style.backgroundColor) || style.backgroundImage!=='none' ||
      ['IMG','SVG','CANVAS','VIDEO'].includes(element.tagName.toUpperCase());
    if (!painted) return [];
    return [...element.getClientRects()].filter(rect=>rect.width>0&&rect.height>0).map(rect=>({element,rect}));
  });
};
const adsCheck = () => {
  if (ads.error) return {ok:false,error:ads.error};
  const parts=adsParts();
  if (!parts.length) return {ok:false,error:'Ads 余额文本尚未显示'};
  const width=document.documentElement.clientWidth || innerWidth,height=document.documentElement.clientHeight || innerHeight;
  const paintedOverlays=adsPaintedOverlays();
  for (const part of parts) for(const r of part.rects) {
    if (r.left < -0.5 || r.top < -0.5 || r.right > width+0.5 || r.bottom > height+0.5)
      return {ok:false,error:'Ads 标签或金额超出当前窗口'};
    for (let e=part.parent;e;e=e.parentElement) {
      // The scrolling document's client box is the viewport, not its translated
      // document bounding box. Its clipping is already checked above.
      if(e===document.scrollingElement) continue;
      const style=getComputedStyle(e),box=adsContentBox(e);
      if (/(auto|scroll|hidden|clip)/.test(style.overflowX) && e.clientWidth>0 &&
          (r.left<box.left-0.5 || r.right>box.right+0.5)) return {ok:false,error:'Ads 标签或金额被横向滚动区域裁剪'};
      if (/(auto|scroll|hidden|clip)/.test(style.overflowY) && e.clientHeight>0 &&
          (r.top<box.top-0.5 || r.bottom>box.bottom+0.5)) return {ok:false,error:'Ads 标签或金额被纵向滚动区域裁剪'};
    }
    if(paintedOverlays.some(({element,rect:cover})=>cover.left<r.right && cover.right>r.left && cover.top<r.bottom && cover.bottom>r.top &&
        !adsProvenBehind(element,part.parent)))
      return {ok:false,error:'Ads 标签或金额被透明指针遮罩覆盖'};
    for(const [x,y] of [[.5,.5],[.15,.15],[.85,.15],[.15,.85],[.85,.85]]) {
      const hit=document.elementFromPoint(r.left+r.width*x,r.top+r.height*y);
      // A pointer-transparent inner span may hit its own balance target. A
      // generic ancestor (body/card) or an unrelated overlay never proves it.
      if (!hit || !(hit===part.parent || hit===part.target ||
          (part.target.contains(hit) && hit.contains(part.parent))))
        return {ok:false,error:'Ads 标签或金额被其他内容遮挡'};
    }
  }
  return {ok:true,error:''};
};
'''

ADS_CHECK = '/* lazada_ads_view_check */\n' + ADS_COMMON + _GEOMETRY + '\nreturn adsCheck().ok;'
ADS_STATUS = '/* lazada_ads_view_status */\n' + ADS_COMMON + _GEOMETRY + '\nreturn adsCheck();'

ADS_PREPARE = '/* lazada_ads_view_prepare */\n' + ADS_COMMON + _GEOMETRY + r'''
const key=arguments[0];
if (ads.error) return {ok:false,error:ads.error};
if (!window[key]) window[key]={x:scrollX,y:scrollY,containers:[]};
const state=window[key],saved=new Set(state.containers.map(item=>item.e));
// A rerender can replace a scroll container between preparation samples. Save
// every newly encountered ancestor before changing its position as well.
for(const target of [ads.label,ads.value]) for(let e=target;e;e=e.parentElement) {
  if(!saved.has(e)){state.containers.push({e,left:e.scrollLeft,top:e.scrollTop});saved.add(e);}
}
const depth=e=>{let result=0;for(;e;e=e.parentElement) result++;return result;};
const parents=new Set();
for(const target of [ads.label,ads.value]) for(let e=target;e&&e!==document.body&&e!==document.documentElement;e=e.parentElement) parents.add(e);
for(const e of [...parents].sort((a,b)=>depth(b)-depth(a))) {
  const style=getComputedStyle(e),parts=adsParts().filter(part=>e.contains(part.parent));
  const box=adsContentBox(e),bounds=adsUnion(parts);
  if (!bounds) continue;
  if (e.scrollWidth>e.clientWidth && /(auto|scroll|hidden)/.test(style.overflowX))
    e.scrollLeft += (bounds.left+bounds.right-box.left-box.right)/2;
  if (e.scrollHeight>e.clientHeight && /(auto|scroll|hidden)/.test(style.overflowY))
    e.scrollTop += (bounds.top+bounds.bottom-box.top-box.bottom)/2;
}
const union=adsUnion(adsParts());
if (union) {
  const width=document.documentElement.clientWidth || innerWidth,height=document.documentElement.clientHeight || innerHeight;
  if (union.right-union.left>width || union.bottom-union.top>height)
    return {ok:false,error:'Ads 标签与金额无法在当前窗口同时完整显示，请增大窗口或屏幕分辨率'};
  window.scrollBy({left:(union.left+union.right-width)/2,top:(union.top+union.bottom-height)/2,behavior:'instant'});
}
return adsCheck();
'''

ADS_RESTORE = r'''/* lazada_ads_view_restore */
const key=arguments[0],state=window[key];if(!state)return;
for(const {e,left,top} of [...state.containers].reverse()) {
  if(e.isConnected){e.scrollLeft=left;e.scrollTop=top;}
}
window.scrollTo({left:state.x,top:state.y,behavior:'instant'});delete window[key];
'''


@contextmanager
def ads_evidence_view(driver, *, timeout=6):
    """Scroll only rendered Ads text into view, then restore prior positions."""
    key = '__hqyl_ads_view_' + uuid4().hex
    deadline = time.monotonic() + max(0, timeout)
    ready = 0
    error = 'Ads 标签或金额尚未完整显示'
    try:
        while True:
            state = driver.execute_script(ADS_PREPARE, key)
            if isinstance(state, dict) and state.get('ok') is True:
                ready += 1
            else:
                ready = 0
                if isinstance(state, dict) and isinstance(state.get('error'), str):
                    error = state['error']
            if ready >= 2:
                yield
                return
            if time.monotonic() >= deadline:
                raise LazadaAdsViewError(error)
            time.sleep(.2)
    finally:
        driver.execute_script(ADS_RESTORE, key)
