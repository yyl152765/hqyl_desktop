import re
import shutil
import subprocess
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]


class FinancialFrontendTests(TestCase):
    def test_markup_ids_and_all_navigation_paths_are_connected(self):
        script = (ROOT / 'frontend/assets/pages/temu-financial-export.js').read_text(encoding='utf-8')
        html = (ROOT / 'frontend/pages/temu-financial-export.html').read_text(encoding='utf-8')
        self.assertFalse(set(re.findall(r'\$\("([\w-]+)"\)', script)) - set(re.findall(r'id="([^"]+)"', html)))
        for relative in ('frontend/assets/common.js', 'frontend/assets/pages/dashboard.js'):
            self.assertIn('temu-financial-export.html', (ROOT / relative).read_text(encoding='utf-8'))
        self.assertIn('每个工作表第一列为完整紫鸟店铺名', html)
        self.assertNotIn('operatorName', html)

    def test_payload_partial_results_and_account_change_race(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js unavailable')
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements=new Map();
function element(){return {value:'',textContent:'',disabled:false,hidden:false,listeners:{},childNodes:[],addEventListener(n,f){this.listeners[n]=f},appendChild(e){this.childNodes.push(e)},replaceChildren(){this.childNodes=[]}}}
const e=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id)};
global.document={createElement:element};
let page,account='a',sent,saved,opened,resolveProgress;
global.HQYL={$:e,activeAccount:()=>({id:account}),boot:p=>page=p,showToast(){},openAccountDialog(){throw Error('unexpected account dialog')},
api:()=>({get_temu_financial_export_info:async()=>({ok:true,output_dir:'D:/financial'}),
save_scoped_output_dirs:async p=>{saved=p;return {ok:true}},start_temu_financial_export:async p=>{sent=p;return {ok:true,context:{account_id:p.account_id}}},
open_path:async path=>{opened=path;return {ok:true}},get_temu_financial_export_progress:()=>new Promise(resolve=>{resolveProgress=resolve})}),
startTask:async fn=>{await fn()}};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
(async()=>{
await page.init();
assert.equal(e('outputDir').value,'D:/financial');
const set=(id,value)=>{e(id).value=value;e(id).listeners.input()};
set('storeNames','甲\n乙');set('startDate','2026-10-01');set('endDate','2026-10-31');
assert.equal(e('startBtn').disabled,false);
set('storeNames','甲\n\n乙');assert.equal(e('startBtn').disabled,true);set('storeNames','甲\n乙');
e('exportForm').listeners.submit({preventDefault(){}});await new Promise(r=>setTimeout(r,0));
assert.deepEqual(sent,{account_id:'a',store_names:'甲\n乙',start_date:'2026-10-01',end_date:'2026-10-31',output_dir:'D:/financial'});
assert.deepEqual(saved,{tool:'temu_financial_export',output_dir:'D:/financial'});
page.applyResult({run_id:'batch',merged:{status:'partial',file:'D:/partial.xlsx'},stores:[{store_name:'甲',status:'success',row_count:3,sheet_count:2,result_file:'D:/shop.xlsx'},{store_name:'乙',status:'failed',error:'下载失败'}]});
assert.equal(e('openMergedBtn').disabled,false);assert.match(e('openMergedBtn').textContent,/不完整/);assert.equal(e('mergedMessage').className,'temu-export-warning');
assert.equal(e('storeResults').childNodes[1].childNodes[6].textContent,'下载失败');
e('openMergedBtn').listeners.click();await new Promise(r=>setTimeout(r,0));assert.equal(opened,'D:/partial.xlsx');
page.onTaskUpdate({status:'running',context:{manifest_path:'D:/batch/manifest.json'}});
account='b';page.onAccountsChanged();resolveProgress({ok:true,result:{run_id:'OLD ACCOUNT',stores:[]}});await new Promise(r=>setTimeout(r,0));
assert.equal(e('batchLabel').textContent,'尚无批次');assert.equal(e('storeNames').value,'');assert.equal(e('openMergedBtn').disabled,true);
assert.equal(page.taskContextFilter({account_id:'a'}),false);
page.applyResult({merged:{status:'success',file:'D:/all.xlsx',store_count:2,sheet_count:3,row_count:9},stores:[]});
assert.match(e('mergedMessage').textContent,/2 个店铺，3 个工作表，共 9 行/);
page.resetResult();assert.equal(e('mergedMessage').hidden,true);assert.equal(e('openMergedBtn').disabled,true);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run([node, '-e', script, str(ROOT / 'frontend/assets/pages/temu-financial-export.js')],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
