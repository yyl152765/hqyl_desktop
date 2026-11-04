"""Prepare the verified balance page for a clear, full-page screenshot.

Only presentation (zoom, scroll and scroll-container size) changes. The native
window is saved separately; no pixels or business data are composed.
"""
from __future__ import annotations

from contextlib import contextmanager
import time
from uuid import uuid4


class LazadaBalanceViewError(ValueError):
    pass


_COMMON = r'''
const visible=e=>Boolean(e&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');
const cards=[...document.querySelectorAll('[data-spm="total_balance_card"]')].filter(visible);
const tables=[...document.querySelectorAll('table')].filter(visible);
const candidates=tables.filter(t=>[...t.querySelectorAll('tbody tr')].some(r=>r.querySelectorAll('td').length>=6));
const table=candidates.length===1?candidates[0]:(tables.length===1?tables[0]:null);
const pagination=[...document.querySelectorAll('.next-pagination')].filter(visible);
const filters=[...document.querySelectorAll('.cap-type')].filter(visible);
const targets=cards.length===1&&table?[cards[0],...filters,table,...pagination]:[];
'''

VIEW_BEGIN = '/* lazada_balance_view_begin */\n' + _COMMON + r'''
const key=arguments[0],root=document.documentElement;
if(!targets.length)return {error:'无法唯一确认余额总额及提现列表'};
const state={root,zoom:root.style.getPropertyValue('zoom'),priority:root.style.getPropertyPriority('zoom'),
  x:scrollX,y:scrollY,containers:[]};
window[key]=state;
root.style.setProperty('zoom','1','important');window.scrollTo(0,0);
const parents=new Set();
for(const target of targets)for(let e=target.parentElement;e&&e!==document.body&&e!==root;e=e.parentElement)parents.add(e);
const depth=e=>{let n=0;for(;e;e=e.parentElement)n++;return n;};
for(const e of [...parents].sort((a,b)=>depth(b)-depth(a))){
  const style=getComputedStyle(e);
  const expandY=e.scrollHeight>e.clientHeight+2&&/(auto|scroll|hidden|clip)/.test(style.overflowY);
  const expandX=e.scrollWidth>e.clientWidth+2&&/(auto|scroll|hidden|clip)/.test(style.overflowX);
  if(expandX||expandY){
    const properties=['height','max-height','overflow-y','width','max-width','overflow-x','box-sizing'];
    state.containers.push({e,top:e.scrollTop,left:e.scrollLeft,styles:properties.map(p=>[p,e.style.getPropertyValue(p),e.style.getPropertyPriority(p)])});
    const height=Math.ceil(e.scrollHeight+e.offsetHeight-e.clientHeight),width=Math.ceil(e.scrollWidth+e.offsetWidth-e.clientWidth);
    e.style.setProperty('box-sizing','border-box','important');
    if(expandY){e.style.setProperty('height',height+'px','important');
      e.style.setProperty('max-height','none','important');e.style.setProperty('overflow-y','visible','important');e.scrollTop=0;}
    if(expandX){e.style.setProperty('width',width+'px','important');
      e.style.setProperty('max-width','none','important');e.style.setProperty('overflow-x','visible','important');e.scrollLeft=0;}
  }
}
return {ok:true};
'''

VIEW_CHECK = '/* lazada_balance_view_check */\n' + _COMMON + r'''
if(!targets.length||scrollX!==0||scrollY!==0)return false;
const root=document.documentElement;
const width=Math.max(root.scrollWidth,root.clientWidth,document.body.scrollWidth);
const height=Math.max(root.scrollHeight,root.clientHeight,document.body.scrollHeight);
return targets.every(e=>{const r=e.getBoundingClientRect();
  if(r.width<1||r.height<1||r.left<0||r.top<0||r.right>width||r.bottom>height)return false;
  // Fixed document boxes with hidden/clip overflow also constrain CDP's
  // content size; their scrollHeight alone does not prove a full-page image.
  for(const parent of [document.body,root]){
    const p=parent.getBoundingClientRect(),s=getComputedStyle(parent);
    if(/(hidden|clip)/.test(s.overflowX)&&(r.left<p.left-1||r.right>p.right+1))return false;
    if(/(hidden|clip)/.test(s.overflowY)&&(r.top<p.top-1||r.bottom>p.bottom+1))return false;
  }
  for(let parent=e.parentElement;parent&&parent!==root&&parent!==document.body;parent=parent.parentElement){
    const p=parent.getBoundingClientRect(),s=getComputedStyle(parent);
    if(/(auto|scroll|hidden|clip)/.test(s.overflowX)&&(r.left<p.left+parent.clientLeft-1||r.right>p.left+parent.clientLeft+parent.clientWidth+1))return false;
    if(/(auto|scroll|hidden|clip)/.test(s.overflowY)&&(r.top<p.top+parent.clientTop-1||r.bottom>p.top+parent.clientTop+parent.clientHeight+1))return false;
  }
  const left=Math.max(0,r.left),right=Math.min(innerWidth,r.right),top=Math.max(0,r.top),bottom=Math.min(innerHeight,r.bottom);
  if(right<=left||bottom<=top)return true;
  return [[.5,.5],[.1,.1],[.9,.9]].every(([x,y])=>{const hit=document.elementFromPoint(left+(right-left)*x,top+(bottom-top)*y);return !!hit&&(hit===e||e.contains(hit));});});
'''

VIEW_RESTORE = r'''/* lazada_balance_view_restore */
const key=arguments[0],state=window[key];if(!state)return;
for(const {e,styles,top,left} of state.containers.reverse()){
  for(const [p,value,priority] of styles){if(value)e.style.setProperty(p,value,priority);else e.style.removeProperty(p);}
  e.scrollTop=top;e.scrollLeft=left;
}
if(state.zoom)state.root.style.setProperty('zoom',state.zoom,state.priority);else state.root.style.removeProperty('zoom');
window.scrollTo(state.x,state.y);delete window[key];
'''


@contextmanager
def balance_page_view(driver, *, timeout=6):
    """Expose the current withdrawal page at 100%, then restore presentation."""
    key = '__hqyl_balance_view_' + uuid4().hex
    try:
        state = driver.execute_script(VIEW_BEGIN, key)
        if not isinstance(state, dict) or not state.get('ok'):
            raise LazadaBalanceViewError(state.get('error', '无法准备完整余额截图') if isinstance(state, dict) else '无法准备完整余额截图')
        deadline = time.monotonic() + timeout
        ready = 0
        while True:
            ready = ready + 1 if driver.execute_script(VIEW_CHECK) is True else 0
            if ready >= 2:
                yield
                return
            if time.monotonic() >= deadline:
                raise LazadaBalanceViewError('余额总额或当前页提现列表被遮挡、裁剪，无法保存完整页面')
            time.sleep(.2)
    finally:
        driver.execute_script(VIEW_RESTORE, key)
