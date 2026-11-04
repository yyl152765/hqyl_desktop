"""Close observed Lazada Ads overlays without targeting promotion actions."""
from __future__ import annotations

import time

from selenium.common.exceptions import ElementClickInterceptedException, StaleElementReferenceException


class LazadaAdsOverlayError(ValueError):
    pass


ADS_OVERLAY_STATE = r"""
/* lazada_ads_overlay_state */
return (() => {
  const visible=element=>{
    if(!element || !element.isConnected || !element.getClientRects().length)return false;
    const style=getComputedStyle(element);
    return style.display!=='none' && !['hidden','collapse'].includes(style.visibility);
  };
  const hit=element=>{
    const rect=element.getBoundingClientRect();
    const x=rect.left+rect.width/2,y=rect.top+rect.height/2;
    if(rect.width<=0 || rect.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight)return false;
    const top=document.elementFromPoint(x,y);
    return top===element || element.contains(top);
  };
  const outsideViewport=element=>{
    const rect=element.getBoundingClientRect();
    const x=rect.left+rect.width/2,y=rect.top+rect.height/2;
    return rect.width>0 && rect.height>0 && (x<0 || y<0 || x>=innerWidth || y>=innerHeight);
  };
  const selectors=[
    ['modal','img[class*="styles_close"]'],
    ['modal','img[class*="msaCreate_close__"]'],
    ['modal','img[class*="popupShell_close__"]'],
    ['modal','svg[class*="cardKeyDialogContent_closeIcon"]'],
    ['modal','img[class*="ooba_close__"]'],
    ['guide','span[class*="abiGuideBubble_closeIcon"]']
  ];
  const layers=new Map();
  for(const [kind,selector] of selectors){
    for(const close of document.querySelectorAll(selector)){
      if(!visible(close))continue;
      let root=close.closest('[role="dialog"],.next-dialog');
      if(kind==='guide'){
        root=close.parentElement;
        while(root && !String(root.className).includes('abiGuideBubble_'))root=root.parentElement;
        root ||= close.parentElement;
      }else{
        root ||= close.closest('.next-overlay-inner') || close.parentElement;
      }
      if(!root || !visible(root))continue;
      if(selector==='img[class*="ooba_close__"]' &&
         (!close.parentElement?.matches('[class*="ooba_ooba__"]') ||
          !root.matches('[role="dialog"],.next-dialog')))continue;
      if(selector==='img[class*="styles_close"]'){
        // The generic close class alone is insufficient. These observed product
        // and shop promotion components work across languages; older DOM retains
        // its title guard. The shop component also offers a recharge coupon, so
        // only its close control is eligible, never its promotion actions.
        if(!root.matches('[role="dialog"],.next-dialog,.next-overlay-inner'))continue;
        const productComponent=[...(close.parentElement?.classList || [])].some(name=>
          name.startsWith('styles_sMaxAMPopup__') || name.startsWith('styles_sMaxAAPopup__')) &&
          Boolean(close.closest('[role="dialog"]'));
        const heading=(root.innerText || root.textContent || '').replace(/\s/g,'');
        const knownHeading=heading.includes('助推高潜力商品') && heading.includes('全站推广');
        if(!productComponent && !knownHeading)continue;
      }
      if(!layers.has(root))layers.set(root,{kind,root,closes:new Set()});
      layers.get(root).closes.add(close);
    }
  }
  const all=[...layers.values()];
  const modal=all.filter(layer=>layer.kind==='modal');
  const selected=modal.length ? modal : all;
  if(selected.some(layer=>layer.closes.size!==1))return {present:true,error:'Lazada 推广弹窗关闭按钮无法唯一确认'};
  const candidates=selected.map(layer=>({...layer,close:[...layer.closes][0]}));
  const ready=candidates.find(layer=>hit(layer.close));
  if(ready)return {present:true,root:ready.root,close:ready.close,kind:ready.kind};
  // An in-viewport control that fails the hit test may be covered by an unknown
  // layer. Only a unique, recognized offscreen control can request scrolling.
  if(candidates.length===1 && outsideViewport(candidates[0].close))
    return {present:true,scrollTarget:candidates[0].close};
  return {present:all.length>0};
})();
"""

ADS_SCROLL_CLOSE = r"""
/* lazada_ads_scroll_close */
const target=arguments[0];
if(!target || !target.isConnected || !target.getClientRects().length)return false;
const style=getComputedStyle(target);
if(style.display==='none' || ['hidden','collapse'].includes(style.visibility))return false;
const rect=target.getBoundingClientRect();
const x=rect.left+rect.width/2,y=rect.top+rect.height/2;
if(rect.width<=0 || rect.height<=0 || (x>=0 && y>=0 && x<innerWidth && y<innerHeight))return false;
target.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
return true;
"""

ADS_CLOSE_HIT = r"""
/* lazada_ads_close_hit */
const target=arguments[0];
const rect=target.getBoundingClientRect();
const x=rect.left+rect.width/2,y=rect.top+rect.height/2;
if(rect.width<=0 || rect.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight)return false;
const hit=document.elementFromPoint(x,y);
return hit===target || target.contains(hit);
"""

ADS_LAYER_GONE = r"""
/* lazada_ads_layer_gone */
const layer=arguments[0];
return !layer.isConnected || !layer.getClientRects().length ||
  getComputedStyle(layer).display==='none' || ['hidden','collapse'].includes(getComputedStyle(layer).visibility);
"""


def _layer_gone(driver, layer, close) -> bool:
    # Lazada reuses its outer next-dialog between promotions. The exact native
    # close control disappearing also proves that the clicked layer has ended;
    # being covered or losing pointer events does not count as disappearance.
    for element in (close, layer):
        try:
            if driver.execute_script(ADS_LAYER_GONE, element) is True:
                return True
        except StaleElementReferenceException:
            return True
    return False


def dismiss_lazada_ads_overlays(driver, *, timeout: float = 5) -> int:
    """Return the number of known layers confirmed hidden after native clicks."""
    deadline = time.monotonic() + max(0, timeout)
    quiet_until = min(deadline, time.monotonic() + .6)
    closed = 0
    scroll_attempts = 0
    while time.monotonic() < deadline:
        state = driver.execute_script(ADS_OVERLAY_STATE) or {}
        if state.get('error'):
            raise LazadaAdsOverlayError(state['error'])
        close = state.get('close')
        if close is None:
            scroll_target = state.get('scrollTarget')
            if scroll_target is not None and scroll_attempts < 3:
                scroll_attempts += 1
                try:
                    driver.execute_script(ADS_SCROLL_CLOSE, scroll_target)
                except StaleElementReferenceException:
                    pass
                # Scroll may move or replace the dialog. Re-discover its exact
                # close, then re-run the hit test before any native click.
                time.sleep(.1)
                continue
            if not state.get('present') and time.monotonic() >= quiet_until:
                return closed
            time.sleep(.1)
            continue
        # Observed promotion dialogs may precede three navigation guides.
        if closed >= 8:
            raise LazadaAdsOverlayError('Lazada 推广弹窗持续出现，已停止关闭')
        try:
            if not close.is_displayed() or not close.is_enabled() or driver.execute_script(ADS_CLOSE_HIT, close) is not True:
                time.sleep(.1)
                continue
            close.click()
        except (ElementClickInterceptedException, StaleElementReferenceException):
            # Re-read the DOM; never substitute an unchecked coordinate click.
            time.sleep(.1)
            continue
        while not _layer_gone(driver, state['root'], close):
            if time.monotonic() >= deadline:
                raise LazadaAdsOverlayError('Lazada 推广弹窗点击关闭后仍显示，已停止操作')
            time.sleep(.1)
        closed += 1
        scroll_attempts = 0
        quiet_until = min(deadline, time.monotonic() + 1.2)
    return closed
