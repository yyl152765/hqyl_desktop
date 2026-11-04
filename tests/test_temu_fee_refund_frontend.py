from __future__ import annotations
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class TemuFeeRefundFrontendTests(unittest.TestCase):
    def test_request_contains_shop_and_dates_without_manual_operator(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js unavailable')
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const elements = new Map();
function element(){ return {value:'', textContent:'', disabled:false, hidden:false, listeners:{}, childNodes:[],
  addEventListener(n,fn){this.listeners[n]=fn}, appendChild(e){this.childNodes.push(e)}, replaceChildren(){this.childNodes=[]}}; }
global.document = {getElementById(id){if(!elements.has(id)) elements.set(id,element());return elements.get(id)}, createElement:element};
let page, sent, opened, saved, picked;
global.HQYL = {$: id=>document.getElementById(id), activeAccount:()=>({id:'a'}), boot:p=>page=p,
  api:()=>({get_temu_fee_refund_info:async()=>({ok:true,output_dir:'D:/results'}),
    save_scoped_output_dirs:async p=>{saved=p;return {ok:true}},
    choose_scoped_output_dir:async p=>{picked=p;return {ok:true,path:'D:/chosen-temu'}},
    open_path:async path=>{opened=path;return {ok:true}}, start_temu_fee_refund_export:async p=>{sent=p;return {ok:true,id:'test',context:{account_id:'a'}}}}),
  startTask:async fn=>{await fn()}, showToast(){}, openAccountDialog(){throw Error('unexpected account prompt')}};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
(async()=>{
  await page.init({settings:{output_dir:'D:/unrelated-global'}});
  const e=id=>elements.get(id), set=(id,value)=>{e(id).value=value;e(id).listeners.input()};
  assert.equal(e('outputDir').value,'D:/results','uses scoped info rather than global preference');
  assert.equal(e('openConsolidatedBtn').disabled,true,'no merged file before a run');
  assert.equal(e('consolidatedMessage').hidden,true);
  set('storeNames','Store A\nStore B');set('startDate','2026-09-01');set('endDate','2026-09-30');
  assert.equal(e('startBtn').disabled,false);
  set('endDate','2026-08-31'); assert.equal(e('startBtn').disabled,true,'reversed dates rejected');
  set('endDate','2026-09-30'); set('storeNames','Store A\n\nStore B'); assert.equal(e('startBtn').disabled,true,'blank row rejected');
  set('storeNames','Store A\nStore B'); e('exportForm').listeners.submit({preventDefault(){}});
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.deepStrictEqual(sent,{account_id:'a',store_names:'Store A\nStore B',start_date:'2026-09-01',end_date:'2026-09-30',output_dir:'D:/results'});
  assert.deepStrictEqual(saved,{tool:'temu_fee_refund_export',output_dir:'D:/results'});
  await e('chooseOutputBtn').listeners.click();
  assert.deepStrictEqual(picked,{tool:'temu_fee_refund_export',kind:'output',current_dir:'D:/results'});
  assert.equal(e('outputDir').value,'D:/chosen-temu');
  page.applyResult({output_dir:'D:/results',stores:[{store_name:'Store A',operator_name:'Alice',status:'partial',exports:{shipping:{status:'success',file:'D:/results/shipping.xlsx'},return_label:{status:'failed',error:'failed to download'},refund:{status:'no_data'}}}]});
  const cells=e('storeResults').childNodes[0].childNodes;
  assert.equal(cells[3].childNodes[0].textContent,'成功');
  assert.equal(cells[4].childNodes[1].textContent,'failed to download');
  cells[3].childNodes[1].listeners.click();await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(opened,'D:/results/shipping.xlsx');
  assert.equal(e('openConsolidatedBtn').disabled,true,'old batches without a merged file remain readable');
  page.applyResult({output_dir:'D:/results',consolidated:{},stores:[]});
  assert.equal(e('openConsolidatedBtn').disabled,true);
  assert.equal(e('consolidatedMessage').hidden,true,'empty merge metadata does not imply generation is running');
  assert.equal(e('consolidatedMessage').textContent,'');
  page.applyResult({output_dir:'D:/results',consolidated:{status:'partial',file:'D:/results/partial.xlsx',row_counts:{shipping:100,return_label:0,refund:2}},stores:[]});
  assert.equal(e('openConsolidatedBtn').disabled,false);
  assert.match(e('openConsolidatedBtn').textContent,/不完整/);
  assert.match(e('consolidatedMessage').textContent,/仅包含成功导出的数据/);
  assert.equal(e('consolidatedMessage').className,'temu-export-warning');
  e('openConsolidatedBtn').listeners.click();await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(opened,'D:/results/partial.xlsx','merged button opens the consolidated workbook');
  page.applyResult({output_dir:'D:/results',consolidated:{status:'success',file:'D:/results/merged.xlsx',row_counts:{shipping:319,return_label:1,refund:2}},stores:[]});
  assert.equal(e('openConsolidatedBtn').textContent,'打开合并总表');
  assert.match(e('consolidatedMessage').textContent,/发货面单费 319 条/);
  assert.match(e('consolidatedMessage').textContent,/退货面单费 1 条/);
  assert.match(e('consolidatedMessage').textContent,/退货退款 2 条/);
  assert.equal(e('consolidatedMessage').className,'temu-export-note');
  page.applyResult({output_dir:'D:/results',consolidated:{status:'failed',error:'表头不一致'},stores:[]});
  assert.equal(e('openConsolidatedBtn').disabled,true);
  assert.match(e('consolidatedMessage').textContent,/生成失败：表头不一致/);
  page.resetResult();
  assert.equal(e('openConsolidatedBtn').disabled,true);
  assert.equal(e('consolidatedMessage').hidden,true,'starting a new run clears old merge feedback');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        result = subprocess.run([node, '-e', script, str(ROOT / 'frontend/assets/pages/temu-fee-refund-export.js')], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

if __name__ == '__main__':
    unittest.main()
