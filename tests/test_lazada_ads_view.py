"""Ads evidence geometry is exercised in local Chromium, never seller accounts."""
from __future__ import annotations

import unittest

from backend.services import lazada_ads_view as view
from test_temu_balance_overlays import SeleniumFixtureDriver

try:
    from playwright.sync_api import Error, sync_playwright
except ImportError:
    sync_playwright = None


FIXTURE = '''<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;font:18px Arial;color:#111;background:white}header{height:70px;background:#ddd}
main{padding:30px;margin-left:180px}.card{width:500px;background:#e9f2ff;padding:20px}
.accountBalanceValue_fixture{font-size:38px;margin-top:18px;padding:8px;background:white}
[hidden]{display:none!important}
</style></head><body><header>Seller Center — synthetic fixture</header><main>
<div class="card"><div><span data-i18n-key="Ads_Available_Balance">可用余额</span></div>
<div class="accountBalanceValue_fixture"><span>THB</span> <strong>-3.52</strong></div></div>
</main><script>window.clicks=0;document.addEventListener('click',()=>window.clicks++);</script></body></html>'''


class LazadaAdsViewTests(unittest.TestCase):
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
        self.context = self.browser.new_context(viewport={'width':900,'height':700})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_content(FIXTURE)
        self.driver = SeleniumFixtureDriver(self.page)

    def metric(self):
        return self.driver.execute_script(view.ADS_COMMON + "return {label:adsText(ads.label),value:adsText(ads.value),error:ads.error};")

    def check(self):
        return self.driver.execute_script(view.ADS_CHECK)

    def test_single_label_and_value_are_visible_without_text_or_style_changes(self):
        before = self.page.content()
        with view.ads_evidence_view(self.driver, timeout=1):
            self.assertTrue(self.check())
            self.assertEqual(self.metric(), {'label':'可用余额','value':'THB -3.52','error':''})
        self.assertEqual(self.page.content(), before)
        self.assertEqual(self.page.evaluate('window.clicks'), 0)

    def test_hidden_responsive_labels_and_values_do_not_create_ambiguity(self):
        for hidden in ('display:none','visibility:hidden','visibility:collapse','opacity:0'):
            with self.subTest(hidden=hidden):
                self.page.set_content(FIXTURE)
                self.page.evaluate('''hidden=>{const duplicate=document.querySelector('.card').cloneNode(true);
                  const ancestor=document.createElement('section');ancestor.style=hidden;ancestor.append(duplicate);document.body.append(ancestor);}''', hidden)
                self.assertFalse(self.metric()['error'])
                with view.ads_evidence_view(self.driver, timeout=1):
                    self.assertTrue(self.check())

    def test_multiple_really_rendered_labels_or_values_are_rejected(self):
        for target in ('.card','.accountBalanceValue_fixture'):
            with self.subTest(target=target):
                self.page.set_content(FIXTURE)
                self.page.evaluate('selector=>{const item=document.querySelector(selector);item.parentElement.append(item.cloneNode(true));}', target)
                self.assertTrue(self.metric()['error'])
                self.assertFalse(self.check())
                with self.assertRaisesRegex(view.LazadaAdsViewError, '唯一确认'):
                    with view.ads_evidence_view(self.driver, timeout=0):
                        self.fail('ambiguous metric accepted')

    def test_wide_empty_value_box_does_not_reject_fully_visible_text(self):
        self.page.evaluate("document.querySelector('.accountBalanceValue_fixture').style.width='1500px'")
        self.assertTrue(self.check())
        self.assertGreater(self.page.locator('.accountBalanceValue_fixture').bounding_box()['width'], 900)
        with view.ads_evidence_view(self.driver, timeout=1):
            self.assertTrue(self.check())

    def test_inline_wrapped_text_and_display_contents_are_measured_by_text_ranges(self):
        self.page.evaluate("document.querySelector('.accountBalanceValue_fixture').style.display='contents'")
        self.assertFalse(self.metric()['error'])
        self.assertTrue(self.check())

    def test_pointer_transparent_inner_text_hits_only_its_balance_target(self):
        self.page.evaluate("document.querySelector('.accountBalanceValue_fixture strong').style.pointerEvents='none'")
        self.assertTrue(self.check())
        self.page.evaluate("document.querySelector('.accountBalanceValue_fixture').style.pointerEvents='none'")
        self.assertFalse(self.check())  # A hit on the outer card is not proof.

    def test_unknown_overlay_and_unrelated_descendant_overlay_remain_rejected(self):
        for inside in (False, True):
            with self.subTest(inside=inside):
                self.page.set_content(FIXTURE)
                self.page.evaluate('''inside=>{const cover=document.createElement('div');cover.id='cover';
                  cover.style='position:fixed;inset:0;background:#0009;z-index:10000';
                  (inside?document.querySelector('.accountBalanceValue_fixture'):document.body).append(cover);}''', inside)
                self.assertFalse(self.check())
                with self.assertRaisesRegex(view.LazadaAdsViewError, '遮挡'):
                    with view.ads_evidence_view(self.driver, timeout=0):
                        self.fail('unknown overlay accepted')
                self.assertEqual(self.page.locator('#cover').count(), 1)
                self.assertEqual(self.page.evaluate('window.clicks'), 0)

    def test_pointer_transparent_painted_overlay_rejected_without_mutation(self):
        for paint in ('background:rgba(0,0,0,.5)', 'background-image:linear-gradient(red,blue);opacity:.8'):
            with self.subTest(paint=paint):
                self.page.set_content(FIXTURE)
                self.page.evaluate('''paint=>{const cover=document.createElement('div');cover.id='cover';
                  cover.style='position:fixed;inset:0;z-index:10000;pointer-events:none;'+paint;document.body.append(cover);}''',paint)
                before=self.page.locator('#cover').get_attribute('style')
                self.assertFalse(self.check())
                with self.assertRaisesRegex(view.LazadaAdsViewError,'遮罩'):
                    with view.ads_evidence_view(self.driver,timeout=0):
                        self.fail('painted pointer-transparent overlay accepted')
                self.assertEqual(self.page.locator('#cover').get_attribute('style'),before)

    def test_pointer_transparent_painted_descendant_cannot_cover_amount(self):
        for position in ('absolute', 'fixed'):
            with self.subTest(position=position):
                self.page.set_content(FIXTURE)
                self.assertTrue(self.check())
                before=self.metric()['value']
                self.page.evaluate('''position=>{
                  const target=document.querySelector('.accountBalanceValue_fixture');target.style.position='relative';
                  const cover=document.createElement('div');cover.id='amount-cover';
                  cover.style='position:'+position+';inset:0;z-index:10000;pointer-events:none;background:black';
                  target.append(cover);
                }''', position)
                self.assertEqual(self.metric()['value'],before)
                self.assertFalse(self.check())
                with self.assertRaisesRegex(view.LazadaAdsViewError,'遮罩'):
                    with view.ads_evidence_view(self.driver,timeout=0):
                        self.fail('painted pointer-transparent amount descendant accepted')
                self.assertEqual(self.page.locator('#amount-cover').count(),1)

    def test_empty_transparent_wrapper_and_negative_z_paint_are_not_overlays(self):
        for style in ('position:fixed;inset:0;pointer-events:none',
                      'position:fixed;inset:0;pointer-events:none;z-index:-1;background:red'):
            with self.subTest(style=style):
                self.page.set_content(FIXTURE)
                self.page.evaluate('style=>{const e=document.createElement("div");e.style=style;document.body.append(e);}',style)
                self.assertTrue(self.check())

    def test_proven_lower_sibling_stacking_context_background_does_not_block(self):
        self.page.evaluate('''()=>{
          document.querySelector('main').style.cssText='position:relative;z-index:2';
          const decoration=document.createElement('div');decoration.id='decoration';
          decoration.style='position:fixed;inset:0;z-index:1;pointer-events:none;background:red';
          document.body.append(decoration);
        }''')
        self.assertTrue(self.check())
        self.page.locator('#decoration').evaluate("e=>e.style.zIndex='3'")
        self.assertFalse(self.check())

    def test_high_z_child_inside_proven_lower_sibling_context_stays_behind(self):
        self.page.evaluate('''()=>{
          document.querySelector('main').style.cssText='position:relative;z-index:2';
          const layer=document.createElement('div');layer.style='position:relative;z-index:1';
          const decoration=document.createElement('div');decoration.id='decoration';
          decoration.style='position:fixed;inset:0;z-index:999;pointer-events:none;background:red';
          layer.append(decoration);document.body.append(layer);
        }''')
        self.assertTrue(self.check())
        self.page.locator('#decoration').evaluate("e=>e.parentElement.style.zIndex='3'")
        self.assertFalse(self.check())

    def test_read_only_check_does_not_scroll_into_view(self):
        self.page.evaluate("document.querySelector('main').style.marginTop='1200px'")
        before = self.page.evaluate('[scrollX,scrollY]')
        self.assertFalse(self.check())
        self.assertEqual(self.page.evaluate('[scrollX,scrollY]'), before)

    def test_window_scroll_preparation_is_restored_when_capture_raises(self):
        self.page.evaluate("document.querySelector('main').style.cssText='margin-left:1200px;margin-top:1200px;width:1000px';window.scrollTo(45,80)")
        before = self.page.evaluate('[scrollX,scrollY]')
        text = self.page.locator('.card').inner_text()
        with self.assertRaisesRegex(RuntimeError,'capture fixture failed'):
            with view.ads_evidence_view(self.driver, timeout=1):
                self.assertTrue(self.check())
                self.assertNotEqual(self.page.evaluate('[scrollX,scrollY]'), before)
                raise RuntimeError('capture fixture failed')
        self.assertEqual(self.page.evaluate('[scrollX,scrollY]'), before)
        self.assertEqual(self.page.locator('.card').inner_text(), text)

    def test_nested_scroll_containers_are_prepared_and_restored(self):
        self.page.evaluate('''()=>{const card=document.querySelector('.card'), scroller=document.createElement('div');
          scroller.id='scroller';scroller.style='width:450px;height:190px;overflow:auto;border:8px solid black';
          const content=document.createElement('section');content.style='width:1100px;height:1200px;padding-top:400px;padding-left:500px';
          card.parentElement.append(scroller);content.append(card);scroller.append(content);scroller.scrollTop=50;scroller.scrollLeft=20;}''')
        self.assertFalse(self.check())
        before = self.page.locator('#scroller').evaluate('e=>[e.scrollLeft,e.scrollTop]')
        with view.ads_evidence_view(self.driver, timeout=1):
            self.assertTrue(self.check())
            self.assertNotEqual(self.page.locator('#scroller').evaluate('e=>[e.scrollLeft,e.scrollTop]'), before)
        self.assertEqual(self.page.locator('#scroller').evaluate('e=>[e.scrollLeft,e.scrollTop]'), before)

    def test_document_scrolling_element_uses_viewport_not_translated_root_box(self):
        self.page.evaluate("document.documentElement.style.overflow='auto';document.querySelector('main').style.marginTop='1200px';window.scrollTo(0,400)")
        before=self.page.evaluate('[scrollX,scrollY]')
        with view.ads_evidence_view(self.driver,timeout=1):
            self.assertTrue(self.check())
            self.assertGreater(self.page.evaluate('scrollY'),400)
        self.assertEqual(self.page.evaluate('[scrollX,scrollY]'),before)

    def test_new_scroll_container_created_between_samples_is_also_restored(self):
        key='__fixture_ads_state'
        self.driver.execute_script(view.ADS_PREPARE,key)
        self.page.evaluate('''()=>{const old=document.querySelector('.card'),card=old.cloneNode(true),scroller=document.createElement('div');
          scroller.id='replacement';scroller.style='width:450px;height:190px;overflow:auto';
          const content=document.createElement('section');content.style='width:1100px;height:1200px;padding-top:400px;padding-left:500px';
          old.replaceWith(scroller);content.append(card);scroller.append(content);scroller.scrollTop=77;scroller.scrollLeft=33;}''')
        self.assertTrue(self.driver.execute_script(view.ADS_PREPARE,key)['ok'])
        self.assertTrue(self.check())
        self.driver.execute_script(view.ADS_RESTORE,key)
        self.assertEqual(self.page.locator('#replacement').evaluate('e=>[e.scrollLeft,e.scrollTop]'),[33,77])

    def test_overflow_clip_and_too_small_viewport_do_not_accept_cropped_text(self):
        self.page.evaluate("document.querySelector('.accountBalanceValue_fixture').style.cssText='width:60px;overflow:clip;white-space:nowrap'")
        self.assertFalse(self.check())
        with self.assertRaisesRegex(view.LazadaAdsViewError, '裁剪'):
            with view.ads_evidence_view(self.driver, timeout=0):
                self.fail('clipped metric accepted')
        self.page.set_content(FIXTURE)
        self.page.set_viewport_size({'width':240,'height':160})
        self.page.evaluate("document.querySelector('.accountBalanceValue_fixture').style.marginTop='250px'")
        with self.assertRaisesRegex(view.LazadaAdsViewError, '同时完整显示'):
            with view.ads_evidence_view(self.driver, timeout=0):
                self.fail('oversized union accepted')

    def test_successful_view_remains_clear_in_small_but_sufficient_viewport(self):
        self.page.set_viewport_size({'width':480,'height':360})
        with view.ads_evidence_view(self.driver, timeout=1):
            self.assertTrue(self.check())
        self.assertEqual(self.page.evaluate('[scrollX,scrollY]'), [0,0])

    def test_resolver_is_safe_when_no_ads_field_exists(self):
        self.page.set_content('<html><body>Income</body></html>')
        self.assertEqual(self.metric()['value'], '')
        self.assertTrue(self.metric()['error'])
        self.assertFalse(self.check())


if __name__ == '__main__':
    unittest.main()
