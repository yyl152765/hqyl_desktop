import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

from PIL import Image
from backend.core.ziniao_cli import ZiniaoCliError
from backend.services.lazada_ads_monthly_cli import ZiniaoMonthlyCli
from backend.services.lazada_ads_monthly_page import LazadaMonthlyPageAdapter


class CliContractTests(unittest.TestCase):
    def cli(self):
        cli=ZiniaoMonthlyCli.__new__(ZiniaoMonthlyCli)
        cli.auth_failed=False
        cli.targets={'store':'target'}
        cli.owned_windows=set()
        cli.assert_profile=lambda:None
        return cli

    def test_native_input_and_close_plain_ack(self):
        cli=self.cli()
        for args, ack in ((['page','input'],'✓ 已输入'),(['store','close'],'✓ 店铺已关闭')):
            with self.subTest(args=args), patch.object(cli,'_process',return_value=subprocess.CompletedProcess([],0,ack,'')):
                self.assertTrue(cli.run(args)['sent'])

    def test_nested_error_not_success(self):
        cli=self.cli()
        result={'ok':True,'data':{'ok':False,'error':{'type':'zclaw','message':'failed'}}}
        with patch.object(cli,'_process',return_value=subprocess.CompletedProcess([],0,json.dumps(result),'')):
            with self.assertRaises(ZiniaoCliError):cli.run(['page','exec'])

    def test_click_not_replayed_on_stale_target(self):
        cli=self.cli()
        with patch.object(cli,'run',side_effect=ZiniaoCliError('stale')) as call:
            with self.assertRaises(ZiniaoCliError):cli.click('store','#download')
        self.assertEqual(call.call_count,1)
        self.assertEqual(cli.targets['store'],'target')

    def test_runtime_logout_login_then_exact_command_retry(self):
        cli=self.cli()
        responses=[subprocess.CompletedProcess([],1,'','Ziniao user is not logged in'),
                   subprocess.CompletedProcess([],0,'登录成功',''),
                   subprocess.CompletedProcess([],0,json.dumps({'ok':True,'data':{'items':[]}}),'')]
        args=['store','list','--all','--format','json']
        with patch.object(cli,'_process',side_effect=responses) as call:
            cli.run(args)
        self.assertEqual([c.args[0] for c in call.call_args_list],[args,['zclaw','login'],args])

    def test_failed_login_does_not_retry_business(self):
        cli=self.cli()
        responses=[subprocess.CompletedProcess([],1,'','Ziniao user is not logged in'),subprocess.CompletedProcess([],1,'失败','')]
        with patch.object(cli,'_process',side_effect=responses) as call:
            with self.assertRaises(ZiniaoCliError):cli.run(['store','list'])
        self.assertEqual(call.call_count,2)
        self.assertTrue(cli.auth_failed)

    def test_screenshot_copies_real_bridge_file(self):
        cli=self.cli()
        with tempfile.TemporaryDirectory() as tmp:
            source,target=Path(tmp)/'bridge.png',Path(tmp)/'report.png'
            Image.new('RGB',(45,30),'white').save(source)
            with patch.object(cli,'_execute_page_command',return_value={'data':{'filePath':str(source)}}):
                cli.screenshot('store',target)
            self.assertEqual(source.read_bytes(),target.read_bytes())

    def test_read_retries_redirect_context_only(self):
        cli=self.cli()
        error=ZiniaoCliError('redirect');error.code='CDP_ERROR'
        with patch.object(cli,'evaluate',side_effect=[error,'complete']) as call,patch('backend.services.lazada_ads_monthly_page.time.sleep'):
            self.assertEqual(LazadaMonthlyPageAdapter(cli,'store','TH').read('document.readyState'),'complete')
        self.assertEqual(call.call_count,2)

    def test_dom_write_is_not_replayed(self):
        cli=self.cli()
        error=ZiniaoCliError('redirect');error.code='CDP_ERROR'
        with patch.object(cli,'evaluate',side_effect=error) as call:
            with self.assertRaises(ZiniaoCliError):LazadaMonthlyPageAdapter(cli,'store','TH').read('hook()',retry=False)
        self.assertEqual(call.call_count,1)

    def test_registration_login_opens_and_selects_new_tab(self):
        cli=self.cli();cli.expected_urls={'store':'https://sellercenter.lazada.co.th/sponsor/solutions/ads/report/'}
        page=LazadaMonthlyPageAdapter(cli,'store','TH')
        login='https://sellercenter.lazada.co.th/apps/seller/login?login=1'
        states=[{'path':'/apps/register/index','host':'sellercenter.lazada.co.th'},'sellercenter.lazada.co.th',login,
                {'path':'/apps/seller/login','host':'sellercenter.lazada.co.th'},
                {'path':'/apps/seller/login','challenge':False},'/sponsor/solutions/ads/report/','sellercenter.lazada.co.th']
        content={'data':{'content':{'forms':[{'name':'account','value':'masked'},{'type':'password','value':'***'}]}}}
        with patch.object(page,'read',side_effect=states),patch.object(page,'click') as click,patch.object(page,'wait_for_page_ready'),patch.object(cli,'rebind_store_page',return_value='new-target') as bind,patch.object(cli,'_execute_page_command',return_value=content):
            page.ensure_logged_in()
        self.assertEqual([c.args[0] for c in click.call_args_list],['a.to-login-link','button.login-button'])
        bind.assert_called_once_with('store',require_path=True)
        self.assertEqual(cli.expected_urls['store'],login)

    def test_changed_config_stops_even_with_cached_profile(self):
        cli=self.cli();del cli.assert_profile
        cli._identity='old';cli._profile_verified=True
        with patch.object(cli,'identity_fingerprint',return_value='changed'):
            with self.assertRaises(ZiniaoCliError):cli.assert_profile()
        self.assertTrue(cli.auth_failed)


class FrontendContractTests(unittest.TestCase):
    def test_choose_open_preview_and_retry(self):
        node=shutil.which('node')
        if not node:self.skipTest('Node required')
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes={},logs=[],toasts=[],calls=[];
function node(id){return nodes[id] ||= {value:'',textContent:'',style:{},events:{},children:[],addEventListener(n,f){this.events[n]=f},replaceChildren(){this.children=[]},appendChild(e){this.children.push(e)}};}
const api={get_lazada_ads_monthly_info:async()=>({ok:true,default_month:'2026-10',default_output_dir:'C:/out',cli_status:{available:true,profile:'test'}}),
choose_output_dir:async()=>({ok:true,path:'D:/new'}),open_path:async p=>{calls.push(['open',p]);return {ok:true}},
preview_lazada_ads_monthly:async()=>({ok:false,matched_count:1,issues:[{requested_name:'missing',error:'not found'}]}),
retry_lazada_ads_monthly:async p=>{calls.push(['retry',p.manifest_path]);return {ok:true}},start_lazada_ads_monthly:async()=>({ok:true})};
let page;const HQYL={$:node,api:()=>api,appendLog:v=>logs.push(v),showToast:v=>toasts.push(v),boot:p=>page=p,
startTask:async f=>{page.resetResult();return f()}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{HQYL,document:{createElement:()=>node(Math.random())}});
(async()=>{await page.init();
await node('chooseOutputBtn').events.click();assert.equal(node('outputDirInput').value,'D:/new');
node('storesInput').value='one\nmissing';await node('previewBtn').events.click();assert(logs.some(s=>s.includes('not found')));
page.onTaskUpdate({result:{workbook_path:'C:/out/book.xlsx',batch_dir:'C:/out',manifest_path:'C:/out/manifest.json',stores:[{status:'failed',store_name:'<img>',note:'bad'}]}});
await node('openOutputBtn').events.click();assert.deepEqual(calls[0],['open','C:/out']);
assert(node('resultBody').children[0].innerHTML.includes('&lt;img&gt;'));
await node('retryBtn').events.click();assert.deepEqual(calls[1],['retry','C:/out/manifest.json']);
assert.equal(node('retryBtn').style.display,'none');assert.equal(node('openOutputBtn').disabled,true);
console.log('frontend contracts passed');})().catch(e=>{console.error(e);process.exitCode=1});
'''
        path=Path(__file__).resolve().parents[1]/'frontend/assets/pages/lazada-ads-monthly.js'
        result=subprocess.run([node,'-e',script,str(path)],capture_output=True,text=True,encoding='utf8')
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
