"""Observed Lazada close controls exercised with local DOM and native clicks."""
from __future__ import annotations

import unittest

from selenium.common.exceptions import StaleElementReferenceException
from backend.services import lazada_balance_overlays as helper
from test_temu_balance_overlays import SeleniumFixtureDriver as BaseDriver

try:
    from playwright.sync_api import Error, sync_playwright
except ImportError:
    sync_playwright = None


FIXTURE = r"""
<!doctype html><html><head><style>
body{font:16px Arial}[hidden]{display:none!important}
.next-dialog{position:fixed;left:300px;top:100px;width:900px;height:420px;background:#fff;z-index:100;box-shadow:0 0 0 1500px #6668}
[data-close]{position:absolute;right:12px;top:12px;width:24px;height:24px;cursor:pointer}
.abiGuideBubble_wrap{position:fixed;left:36px;width:170px;height:50px;background:#eee;z-index:10}
.abiGuideBubble_closeIcon{display:block;width:24px;height:24px;cursor:pointer}
[data-promote]{position:fixed;left:36px;top:250px;width:200px;height:30px;z-index:101}
#unknown-cover{position:fixed;inset:0;background:#6666;z-index:1000}
</style></head><body><button id="recharge">充值</button>
<script>
window.counts={closed:[],promotion:0,recharge:0,checkbox:0,trusted:[]};
document.querySelector('#recharge').onclick=()=>window.counts.recharge++;
window.addGuide=(index=0)=>{
  const guide=document.createElement('div');guide.className='abiGuideBubble_wrap';guide.style.top=(250+index*70)+'px';
  guide.innerHTML='<span class="abiGuideBubble_closeIcon__fixture">×</span><span>引导'+index+'</span>';
  guide.querySelector('.abiGuideBubble_closeIcon__fixture').onclick=event=>{
    window.counts.closed.push('guide'+index);window.counts.trusted.push(event.isTrusted);guide.remove();
  };document.body.append(guide);
};
window.addModal=(options={})=>{
  const {name='promo',closeClass='styles_close__87NOH',heading='助推高潜力商品 使用 全站推广 - 商品！',tag='img',duplicate=false,keep=false,next=false}=options;
  const modal=document.createElement('div');modal.className='next-dialog next-dialog-v2';modal.role='dialog';modal.dataset.fixtureModal=name;
  modal.innerHTML='<h2>'+heading+'</h2><button data-promote>为所选的5个商品提升流量</button><input type="checkbox" checked><button data-apply>应用</button>';
  const close=tag==='svg'?document.createElementNS('http://www.w3.org/2000/svg','svg'):document.createElement(tag);
  close.setAttribute('class',closeClass);close.dataset.close='';
  if(tag==='svg')close.innerHTML='<rect width="24" height="24" fill="black"/>';
  else close.src='data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><path d="M3 3L21 21M21 3L3 21" stroke="black"/></svg>';
  close.onclick=event=>{
    window.counts.closed.push(name);window.counts.trusted.push(event.isTrusted);
    if(!keep)modal.remove();
    if(next)setTimeout(()=>window.addModal({name:'delayed',closeClass:'popupShell_close__G3xsj',heading:'降低全站推广目标ROI'}),350);
  };
  modal.append(close);
  if(duplicate){const second=close.cloneNode(true);second.style.right='48px';modal.append(second)}
  modal.querySelector('[data-promote]').onclick=()=>window.counts.promotion++;
  modal.querySelector('[data-apply]').onclick=()=>window.counts.promotion++;
  modal.querySelector('input').onchange=()=>window.counts.checkbox++;
  if(closeClass.includes('ooba_close__')){
    modal.querySelector('[data-promote]').textContent='立即手动充值';
    modal.querySelector('[data-promote]').onclick=()=>window.counts.recharge++;
    const content=document.createElement('div');content.className='aplus-auto-exp ooba_ooba__FNZ3D';
    content.append(...modal.childNodes);modal.append(content);
  }
  document.body.append(modal);
};
</script></body></html>
"""


class Driver(BaseDriver):
    def execute_script(self, script, *args):
        for value in args:
            if hasattr(value, 'locator') and value.locator.count() == 0:
                raise StaleElementReferenceException('Fixture layer removed from DOM')
        return super().execute_script(script, *args)


class LazadaBalanceOverlaysTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest('Playwright unavailable')
        cls.runtime = sync_playwright().start()
        cls.addClassCleanup(cls.runtime.stop)
        try:
            cls.browser = cls.runtime.chromium.launch(headless=True, channel='msedge')
        except Error:
            cls.browser = cls.runtime.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 1800, 'height': 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(1500)
        self.page.set_content(FIXTURE)
        self.driver = Driver(self.page)

    def add_modal(self, **options):
        self.page.evaluate('window.addModal', options)

    def counts(self):
        return self.page.evaluate('window.counts')

    def assert_business_untouched(self):
        state = self.counts()
        self.assertEqual([state[key] for key in ('promotion', 'recharge', 'checkbox')], [0, 0, 0])
        self.assertTrue(all(state['trusted']))

    def test_modal_over_guide_coordinates_closes_first_without_business_clicks(self):
        for index in range(3):
            self.page.evaluate('window.addGuide', index)
        self.add_modal()
        self.assertTrue(self.page.evaluate("document.elementFromPoint(48,262).matches('[data-promote]')"))
        self.assertEqual(helper.dismiss_lazada_ads_overlays(self.driver, timeout=3), 4)
        self.assertEqual(self.counts()['closed'], ['promo', 'guide0', 'guide1', 'guide2'])
        self.assert_business_untouched()

    def test_observed_specific_close_classes_do_not_depend_on_page_language(self):
        for close_class, tag, heading in (
            ('msaCreate_close__test', 'img', '立即推广'),
            ('popupShell_close__G3xsj', 'img', '降低全站推广 - 店铺的目标ROI，建议目标ROAS'),
            ('cardKeyDialogContent_closeIcon__test', 'svg', 'Ưu đãi quảng cáo'),
        ):
            with self.subTest(close_class=close_class):
                self.page.set_content(FIXTURE)
                self.add_modal(closeClass=close_class, tag=tag, heading=heading)
                self.assertEqual(helper.dismiss_lazada_ads_overlays(self.driver, timeout=1), 1)
                self.assertEqual(self.counts()['closed'], ['promo'])
                self.assert_business_untouched()

    def test_delayed_next_layer_is_observed_and_closed(self):
        self.add_modal(next=True)
        self.assertEqual(helper.dismiss_lazada_ads_overlays(self.driver, timeout=2.5), 2)
        self.assertEqual(self.counts()['closed'], ['promo', 'delayed'])
        self.assert_business_untouched()

    def test_shared_dialog_replaces_close_and_content_for_four_layers(self):
        for index in range(3):
            self.page.evaluate('window.addGuide', index)
        self.page.evaluate("""()=>{
          const shared=document.createElement('div');
          shared.className='next-dialog next-dialog-v2';shared.role='dialog';
          document.body.append(shared);window.sharedDialog=shared;
          const stages=[
            {closeClass:'popupShell_close__G3xsj',heading:'降低全站推广 - 店铺的目标ROI'},
            {closeClass:'styles_close__87NOH',heading:'助推高潜力商品 使用 全站推广 - 商品！'},
            {closeClass:'cardKeyDialogContent_closeIcon__UlIyw',tag:'svg',heading:'限时激励礼包'},
            {closeClass:'ooba_close__xXNLp',heading:'余额不足以支撑2天 / 立即充值保持活动顺利进行'}
          ];
          function render(index){
            window.addModal({...stages[index],name:'temporary'});
            const temporary=document.querySelector('[data-fixture-modal=temporary]');
            shared.replaceChildren(...temporary.childNodes);temporary.remove();
            shared.querySelector('[data-close]').onclick=event=>{
              window.counts.closed.push('layer'+index);window.counts.trusted.push(event.isTrusted);
              if(index<stages.length-1)setTimeout(()=>render(index+1),50);else shared.hidden=true;
            };
          }
          render(0);
        }""")
        self.assertEqual(helper.dismiss_lazada_ads_overlays(self.driver, timeout=4), 7)
        self.assertEqual(self.counts()['closed'], ['layer0', 'layer1', 'layer2', 'layer3', 'guide0', 'guide1', 'guide2'])
        self.assertTrue(self.page.evaluate('window.sharedDialog.isConnected && window.sharedDialog.hidden'))
        self.assert_business_untouched()

    def test_ooba_close_requires_its_observed_parent_content_layer(self):
        self.add_modal(closeClass='ooba_close__xXNLp', heading='余额不足以支撑2天')
        self.page.evaluate("""()=>{
          const content=document.querySelector('[class*=ooba_ooba__]');
          content.replaceWith(...content.childNodes);
        }""")
        self.assertEqual(helper.dismiss_lazada_ads_overlays(self.driver, timeout=.3), 0)
        self.assertEqual(self.counts()['closed'], [])
        self.assert_business_untouched()

    def test_ninth_continuous_known_layer_is_not_clicked(self):
        self.page.evaluate("""()=>{
          function render(index){
            window.addModal({name:'layer'+index,closeClass:'popupShell_close__G3xsj',heading:'降低目标ROI'});
            const modal=document.querySelector('[data-fixture-modal="layer'+index+'"]');
            modal.querySelector('[data-close]').onclick=event=>{
              window.counts.closed.push('layer'+index);window.counts.trusted.push(event.isTrusted);
              modal.remove();render(index+1);
            };
          }
          render(0);
        }""")
        with self.assertRaisesRegex(helper.LazadaAdsOverlayError, '持续出现'):
            helper.dismiss_lazada_ads_overlays(self.driver, timeout=5)
        self.assertEqual(self.counts()['closed'], ['layer'+str(index) for index in range(8)])
        self.assertEqual(self.page.locator('[data-fixture-modal=layer8]').count(), 1)
        self.assert_business_untouched()

    def test_specific_component_close_without_generic_dialog_wrapper_is_preserved(self):
        self.add_modal(closeClass='msaCreate_close__test', heading='Promotional offer')
        self.page.evaluate("""()=>{
          const modal=document.querySelector('[role=dialog]');
          modal.removeAttribute('role');
          modal.className='msaCreate_content__fixture';
          modal.style.cssText='position:fixed;left:300px;top:100px;width:900px;height:420px;background:white;z-index:100';
        }""")
        self.assertEqual(helper.dismiss_lazada_ads_overlays(self.driver, timeout=1), 1)
        self.assertEqual(self.counts()['closed'], ['promo'])
        self.assert_business_untouched()

    def test_duplicate_close_controls_are_rejected_without_clicking(self):
        self.add_modal(duplicate=True)
        with self.assertRaisesRegex(helper.LazadaAdsOverlayError, '无法唯一确认'):
            helper.dismiss_lazada_ads_overlays(self.driver, timeout=.3)
        self.assertEqual(self.counts()['closed'], [])
        self.assert_business_untouched()

    def test_generic_close_with_unrecognized_dialog_title_is_not_clicked(self):
        for heading in ('请确认广告充值', 'Nạp tiền vào tài khoản quảng cáo'):
            with self.subTest(heading=heading):
                self.page.set_content(FIXTURE)
                self.add_modal(heading=heading)
                self.assertEqual(helper.dismiss_lazada_ads_overlays(self.driver, timeout=.3), 0)
                self.assertEqual(self.counts()['closed'], [])
                self.assertEqual(self.page.locator('[role=dialog]').count(), 1)
                self.assert_business_untouched()

    def test_vietnamese_product_popup_requires_observed_component_and_dialog(self):
        for dialog in (True, False):
            with self.subTest(dialog=dialog):
                self.page.set_content(FIXTURE)
                self.add_modal(heading='Thúc đẩy sản phẩm tiềm năng với Tài Trợ Max - Sản Phẩm')
                self.page.evaluate("""dialog=>{
                  const modal=document.querySelector('[role=dialog]');
                  const component=document.createElement('div');
                  component.className='styles_sMaxAMPopup__FOwBa';
                  component.append(...modal.childNodes);modal.append(component);
                  if(!dialog)modal.removeAttribute('role');
                }""", dialog)
                self.assertEqual(helper.dismiss_lazada_ads_overlays(self.driver, timeout=1), 1 if dialog else 0)
                self.assertEqual(self.counts()['closed'], ['promo'] if dialog else [])
                self.assert_business_untouched()

    def test_unknown_cover_prevents_all_close_clicks(self):
        self.add_modal()
        self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div id=unknown-cover><button>关闭</button></div>')")
        self.assertEqual(helper.dismiss_lazada_ads_overlays(self.driver, timeout=.3), 0)
        self.assertEqual(self.counts()['closed'], [])
        self.assert_business_untouched()

    def test_ancestor_hit_is_not_mistaken_for_close_hit(self):
        self.add_modal()
        self.page.evaluate("document.querySelector('[data-close]').style.pointerEvents='none'")
        self.assertEqual(helper.dismiss_lazada_ads_overlays(self.driver, timeout=.3), 0)
        self.assertEqual(self.counts()['closed'], [])
        self.assert_business_untouched()

    def test_late_cover_between_discovery_and_native_click_is_not_clicked(self):
        self.add_modal()

        def after_execute(script):
            if script == helper.ADS_OVERLAY_STATE:
                self.driver.after_execute = None
                self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<div id=unknown-cover><button>充值</button></div>')")

        self.driver.after_execute = after_execute
        self.assertEqual(helper.dismiss_lazada_ads_overlays(self.driver, timeout=.3), 0)
        self.assertEqual(self.counts()['closed'], [])
        self.assert_business_untouched()

    def test_native_click_without_layer_disappearance_is_not_counted_or_repeated(self):
        self.add_modal(keep=True)
        with self.assertRaisesRegex(helper.LazadaAdsOverlayError, '仍显示'):
            helper.dismiss_lazada_ads_overlays(self.driver, timeout=.35)
        self.assertEqual(self.counts()['closed'], ['promo'])
        self.assertEqual(self.page.locator('[role=dialog]').count(), 1)
        self.assert_business_untouched()

    def test_close_becoming_covered_after_click_does_not_prove_layer_disappeared(self):
        self.add_modal(keep=True)
        self.page.evaluate("""()=>{
          document.querySelector('[data-close]').addEventListener('click',()=>{
            document.body.insertAdjacentHTML('beforeend','<div id=unknown-cover><button>应用</button></div>');
          });
        }""")
        with self.assertRaisesRegex(helper.LazadaAdsOverlayError, '仍显示'):
            helper.dismiss_lazada_ads_overlays(self.driver, timeout=.35)
        self.assertEqual(self.counts()['closed'], ['promo'])
        self.assertEqual(self.page.locator('[data-close]').count(), 1)
        self.assert_business_untouched()


if __name__ == '__main__':
    unittest.main()
