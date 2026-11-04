"""Receive the XLSX produced by one verified export-history download click."""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from backend.services.temu_fee_refund_controls import click_control


MAX_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 512 * 1024

CAPTURE_START = r'''(() => {
  const key=__KEY__, limit=__LIMIT__;
  if (window[key]) return JSON.stringify({ok:false});
  const state={files:[],pending:0,error:'',urls:new Set(),blobs:new WeakSet(),
    originalOpen:window.open,originalCreate:URL.createObjectURL};
  const fail=message=>{state.error ||= message;};
  const collectBytes=bytes=>{
    if(bytes.length<4 || bytes[0]!==80 || bytes[1]!==75 || bytes[2]!==3 || bytes[3]!==4) {
      fail('下载内容不是有效 Excel 文件');return;
    }
    state.files.push({bytes});
  };
  const collectBlob=blob=>{
    if (!(blob instanceof Blob) || state.blobs.has(blob)) return;
    state.blobs.add(blob);
    if(blob.size>limit){fail('导出文件超过 128 MiB 接收限制');return;}
    state.pending++;
    blob.arrayBuffer().then(buffer=>collectBytes(new Uint8Array(buffer)))
      .catch(()=>fail('读取浏览器导出文件失败')).finally(()=>state.pending--);
  };
  const collectURL=value=>{
    let url;
    try {url=new URL(String(value),location.href);} catch (_) {fail('无法识别导出下载地址');return;}
    if(url.protocol!=='blob:' && (url.origin!==location.origin || !/^https?:$/.test(url.protocol))) {
      fail('导出下载地址不属于当前卖家页面');return;
    }
    if(state.urls.has(url.href)) return;
    state.urls.add(url.href);state.pending++;
    (async()=>{
      const response=await fetch(url.href,{credentials:'same-origin',redirect:'error'});
      if(!response.ok) throw Error('response');
      const length=Number(response.headers.get('content-length')||0);
      if(length>limit){fail('导出文件超过 128 MiB 接收限制');return;}
      if(!response.body){collectBlob(await response.blob());return;}
      const reader=response.body.getReader(),parts=[];
      let size=0;
      try {
        for(;;){const {done,value}=await reader.read();if(done)break;
          size+=value.length;
          if(size>limit){fail('导出文件超过 128 MiB 接收限制');await reader.cancel();return;}
          parts.push(value);
        }
      } finally {reader.releaseLock();}
      const bytes=new Uint8Array(size);let offset=0;
      for(const part of parts){bytes.set(part,offset);offset+=part.length;}
      collectBytes(bytes);
    })().catch(()=>fail('读取本次导出下载链接失败')).finally(()=>state.pending--);
  };
  state.openWrapper=function(url,...args){
    if(url && String(url)!=='about:blank') collectURL(url);
    return state.originalOpen.call(this,url,...args);
  };
  state.createWrapper=function(blob){
    const url=state.originalCreate.call(this,blob);state.urls.add(url);
    collectBlob(blob);return url;
  };
  state.listener=event=>{
    const a=event.target?.closest?.('a[href]');
    if(a && (a.hasAttribute('download') || /\.xlsx(?:$|[?#])/i.test(a.href))) collectURL(a.href);
  };
  window[key]=state;window.open=state.openWrapper;URL.createObjectURL=state.createWrapper;
  document.addEventListener('click',state.listener,true);
  return JSON.stringify({ok:true});
})()'''

CAPTURE_CLEAR = r'''(() => {
  const key=__KEY__,state=window[key];
  if(state){
    if(window.open===state.openWrapper) window.open=state.originalOpen;
    if(URL.createObjectURL===state.createWrapper) URL.createObjectURL=state.originalCreate;
    document.removeEventListener('click',state.listener,true);delete window[key];
  }
  return JSON.stringify(true);
})()'''


def _validate_xlsx(path: Path) -> None:
    from openpyxl import load_workbook

    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            if (len(names) > 10000 or sum(info.file_size for info in archive.infolist()) > 512 * 1024 * 1024
                    or not {'[Content_Types].xml', 'xl/workbook.xml'}.issubset(names)
                    or archive.testzip() is not None):
                raise ValueError('下载内容不是完整有效的 XLSX 文件')
        book = load_workbook(path, read_only=True, data_only=False)
        try:
            if not book.worksheets:
                raise ValueError('导出文件没有工作表')
        finally:
            book.close()
    except (BadZipFile, KeyError, OSError) as exc:
        raise ValueError('下载内容不是完整有效的 XLSX 文件') from exc


def download_xlsx(browser, store_id: str, selector: str, raw_dir: str | Path, *,
                  filename: str = '导出.xlsx', timeout: float = 180) -> Path:
    """Click once and save newly captured bytes, never reusing old downloads."""
    if Path(filename).name != filename or Path(filename).suffix.lower() != '.xlsx':
        raise ValueError('导出文件名必须是本目录内的 XLSX 文件名')
    directory = Path(raw_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / filename
    if destination.exists():
        raise ValueError('本次下载目标文件已存在，请使用新的尝试目录')
    key_js = json.dumps('__hqyl_fee_download_' + uuid4().hex)
    temporary = directory / ('.' + uuid4().hex + '.xlsx')
    installed = False
    try:
        installed = True  # Cleanup also runs if an install response is lost.
        result = browser.evaluate(store_id, CAPTURE_START.replace('__KEY__', key_js).replace('__LIMIT__', str(MAX_BYTES)))
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise ValueError('无法开始接收本次下载文件')
        click_control(browser, store_id, selector)
        deadline = time.monotonic() + timeout
        while True:
            info = browser.evaluate(store_id, f'''JSON.stringify((() => {{const s=window[{key_js}];return s ?
              {{files:s.files.map(f=>({{size:f.bytes.length}})),pending:s.pending,error:s.error}} : {{lost:true}};}})())''')
            if not isinstance(info, dict) or info.get('lost'):
                raise ValueError('下载期间页面已切换，无法确认本次文件')
            if info.get('error'):
                # Hook errors are fixed strings and never contain signed URLs.
                raise ValueError('本次下载失败：' + str(info['error']))
            files = info.get('files', [])
            if len(files) > 1:
                raise ValueError('一次下载产生多个文件，无法确认结果')
            if len(files) == 1 and not info.get('pending'):
                size = files[0].get('size')
                if type(size) is not int or not 4 <= size <= MAX_BYTES:
                    raise ValueError('下载文件长度无效或超过接收限制')
                break
            if time.monotonic() >= deadline:
                raise ValueError('等待本次导出下载文件超时')
            time.sleep(min(.5, max(0, deadline - time.monotonic())))
        with temporary.open('xb') as stream:
            for start in range(0, size, CHUNK_BYTES):
                end = min(size, start + CHUNK_BYTES)
                script = f'''JSON.stringify((() => {{const a=window[{key_js}].files[0].bytes.subarray({start},{end});
                  let s='';for(let i=0;i<a.length;i+=8192)s+=String.fromCharCode(...a.subarray(i,i+8192));return btoa(s);}})())'''
                try:
                    chunk = base64.b64decode(browser.evaluate(store_id, script), validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValueError('导出文件传输编码无效') from exc
                if len(chunk) != end - start:
                    raise ValueError('导出文件传输长度不一致')
                stream.write(chunk)
        if temporary.stat().st_size != size:
            raise ValueError('导出文件未完整保存')
        _validate_xlsx(temporary)
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)
        if installed:
            try:
                browser.evaluate(store_id, CAPTURE_CLEAR.replace('__KEY__', key_js))
            except Exception:
                # The browser may have navigated or closed; never hide the
                # original download error with cleanup failure.
                pass
