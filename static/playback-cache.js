'use strict';
// Store complete compatible audio by content, independently of the visible row.
// IndexedDB also works on the HTTP addresses used by this workbench.
(() => {
  const VERSION = 'mp3-1', MAX_FILE_BYTES = 512 * 1024 * 1024;
  const MEMORY_BYTES = 192 * 1024 * 1024, MEMORY_FILES = 8;
  const validKey = key => /^[a-f0-9]{64}\.mp3-1$/.test(key);
  const pause = (milliseconds, signal) => new Promise((resolve, reject) => {
    const timer = setTimeout(done, milliseconds);
    function done() { signal.removeEventListener('abort', abort); resolve(); }
    function abort() { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); }
    signal.addEventListener('abort', abort, {once:true});
    if (signal.aborted) abort();
  });
  const sizeText = size => (size / 1024 / 1024).toFixed(1) + ' MB';
  const deadline = async (promise, cancel) => {
    let timer;
    try {
      return await Promise.race([promise, new Promise((_, reject) => {
        timer = setTimeout(() => { reject(new Error('回听连接长时间没有响应，请重试。')); cancel(); }, 60000);
      })]);
    } finally { clearTimeout(timer); }
  };
  const validMP3 = async blob => {
    const bytes = new Uint8Array(await blob.slice(0, 3).arrayBuffer());
    return bytes.length === 3 && ((bytes[0] === 73 && bytes[1] === 68 && bytes[2] === 51)
      || (bytes[0] === 255 && (bytes[1] & 224) === 224));
  };

  class PlaybackFiles {
    constructor() {
      this.memory = new Map(); this.jobs = new Map(); this.downloads = [];
      this.downloading = false; this.active = null; this.database = null;
      this.invalidKeys = new Set();
    }
    keyFor(item) {
      const key = item.sha256 + '.' + VERSION;
      if (!validKey(key)) throw new Error('音频标识无效，请刷新后重试。');
      return key;
    }
    setActive(key) {
      this.active = key;
      this.downloads.sort((a, b) => Number(b.key === key) - Number(a.key === key));
      this.prune();
    }
    prune() {
      let bytes = [...this.memory.values()].reduce((total, entry) => total + entry.blob.size, 0);
      for (const [key, entry] of this.memory) {
        if (this.memory.size <= MEMORY_FILES && bytes <= MEMORY_BYTES) break;
        if (key === this.active || this.jobs.has(key)) continue;
        URL.revokeObjectURL(entry.url); this.memory.delete(key); bytes -= entry.blob.size;
      }
    }
    remember(key, blob, saved) {
      const previous = this.memory.get(key);
      if (previous) { previous.saved = saved; this.memory.delete(key); this.memory.set(key, previous); return previous; }
      const entry = {blob, url:URL.createObjectURL(blob), saved};
      this.memory.set(key, entry); this.prune(); return entry;
    }
    async db() {
      if (!this.database) this.database = new Promise(resolve => {
        let settled = false, request;
        const finish = value => { if (!settled) { settled = true; clearTimeout(timer); resolve(value); } };
        const timer = setTimeout(() => finish(null), 5000);
        try { request = indexedDB.open('audio-review-playback', 1); } catch (_) { finish(null); return; }
        request.onupgradeneeded = () => {
          if (!request.result.objectStoreNames.contains('files')) request.result.createObjectStore('files', {keyPath:'key'});
        };
        request.onsuccess = () => {
          if (settled) { request.result.close(); return; }
          request.result.onversionchange = () => { request.result.close(); this.database = null; };
          finish(request.result);
        };
        request.onerror = request.onblocked = () => finish(null);
      });
      const pending = this.database, result = await pending;
      if (!result && this.database === pending) this.database = null;
      return result;
    }
    async stored(key, operation, value) {
      const database = await this.db();
      if (!database) return null;
      return new Promise(resolve => {
        let transaction, answer = null, settled = false;
        const finish = result => { if (!settled) { settled = true; clearTimeout(timer); resolve(result); } };
        const timer = setTimeout(() => { try { transaction?.abort(); } catch (_) {} finish(null); }, 30000);
        try {
          transaction = database.transaction('files', operation === 'get' ? 'readonly' : 'readwrite');
          const store = transaction.objectStore('files');
          const request = operation === 'get' ? store.get(key) : operation === 'put' ? store.put(value) : store.delete(key);
          request.onsuccess = () => { answer = operation === 'get' ? request.result : true; };
          transaction.oncomplete = () => finish(answer);
          transaction.onerror = transaction.onabort = () => finish(null);
        } catch (_) { finish(null); }
      });
    }
    emit(job, state) {
      job.state = state;
      for (const listener of job.listeners) { try { listener(state); } catch (_) {} }
    }
    ready(job, entry, saving = false) {
      this.emit(job, {state:'ready', progress:100, url:entry.url, saved:entry.saved,
        message:saving ? '回听已可播放 · 正在保存，切换不会重新下载'
          : entry.saved ? '回听已保存 · 切换、刷新直接使用'
          : '回听已加载 · 浏览器未能保存，刷新后可能需要重新加载'});
    }
    watch(item, listener) {
      const key = this.keyFor(item);
      let job = this.jobs.get(key);
      if (job) { job.listeners.add(listener); listener(job.state); return () => job.listeners.delete(listener); }
      const cached = this.memory.get(key);
      if (cached) {
        this.memory.delete(key); this.memory.set(key, cached);
        listener({state:'ready', progress:100, url:cached.url, saved:cached.saved,
          message:cached.saved ? '回听已保存 · 切换、刷新直接使用' : '回听已加载 · 浏览器未能保存，刷新后可能需要重新加载'});
        return () => {};
      }
      if (this.jobs.size >= 32) { listener({state:'error', message:'回听加载队列已满，请等当前加载完成后重试。'}); return () => {}; }
      job = {key, item, listeners:new Set([listener]), controller:new AbortController(),
        state:{state:'loading', message:'正在读取已保存的回听…', progress:null}};
      job.done = new Promise(resolve => { job.finish = resolve; });
      this.jobs.set(key, job); listener(job.state); this.run(job);
      return () => job.listeners.delete(listener);
    }
    async json(job, action, method = 'GET', retry = true) {
      const requestedId = job.item.id, path = '/api/playback/' + requestedId + '/' + action;
      const controller = new AbortController(), abort = () => controller.abort();
      job.controller.signal.addEventListener('abort', abort, {once:true});
      if (job.controller.signal.aborted) abort();
      let response, result;
      try {
        response = await deadline(fetch(path, {method, signal:controller.signal, cache:'no-store'}), abort);
        result = await deadline(response.json(), abort);
      } finally { job.controller.signal.removeEventListener('abort', abort); }
      if (!response.ok) {
        if (response.status === 404 && retry && requestedId !== job.item.id) return this.json(job, action, method, false);
        throw new Error(result.message || result.detail || '回听准备失败，请重试。');
      }
      if (!result || result.cache_key !== job.key) throw new Error('回听版本已更新，请刷新网页。');
      if (!['idle','queued','waiting','converting','ready','error'].includes(result.state)) throw new Error('回听状态异常，请重试。');
      if (result.state === 'ready') {
        const url = new URL(result.stream_url, location.href);
        if (!Number.isSafeInteger(result.bytes) || result.bytes <= 0 || result.bytes > MAX_FILE_BYTES
            || url.origin !== location.origin || url.pathname !== '/api/playback/' + requestedId + '/audio'
            || url.searchParams.get('key') !== job.key) throw new Error('回听文件信息无效，请刷新后重试。');
        result.stream_url = '/api/playback/' + job.item.id + '/audio?key=' + encodeURIComponent(job.key);
      }
      return result;
    }
    async run(job) {
      try {
        const stored = this.invalidKeys.has(job.key) ? null : await this.stored(job.key, 'get');
        if (job.controller.signal.aborted) throw new DOMException('Aborted', 'AbortError');
        if (stored && stored.key === job.key && stored.blob instanceof Blob && stored.bytes === stored.blob.size
            && stored.bytes > 0 && stored.bytes <= MAX_FILE_BYTES && stored.blob.type === 'audio/mpeg'
            && await validMP3(stored.blob)) {
          this.ready(job, this.remember(job.key, stored.blob, true)); return;
        }
        if (stored) await this.stored(job.key, 'delete');
        let status = await this.json(job, 'prepare', 'POST');
        while (status.state !== 'ready') {
          if (status.state === 'error') throw new Error(status.message || '回听准备失败，请重试。');
          this.emit(job, status);
          await pause(1000, job.controller.signal);
          status = status.state === 'idle' ? await this.json(job, 'prepare', 'POST')
            : await this.json(job, 'status');
        }
        job.serverStatus = status;
        this.emit(job, {state:'loading', progress:null, message:'回听已准备 · 正在等待首次加载…'});
        await new Promise((resolve, reject) => {
          const abort = () => {
            this.downloads = this.downloads.filter(entry => entry !== job);
            reject(new DOMException('Aborted', 'AbortError'));
          };
          job.queueAbort = abort;
          const finish = callback => value => { job.controller.signal.removeEventListener('abort', abort); callback(value); };
          job.downloadResolve = finish(resolve); job.downloadReject = finish(reject);
          job.controller.signal.addEventListener('abort', abort, {once:true});
          if (job.controller.signal.aborted) { abort(); return; }
          this.downloads.push(job); this.setActive(this.active); this.pump();
        });
      } catch (error) {
        if (error.name !== 'AbortError') this.emit(job, {state:'error', message:error.message || '回听加载失败，请重试。'});
      } finally {
        if (this.jobs.get(job.key) === job) this.jobs.delete(job.key);
        job.listeners.clear(); job.finish(); this.prune();
      }
    }
    async pump() {
      if (this.downloading) return;
      this.downloading = true;
      try {
        while (this.downloads.length) {
          const job = this.downloads.shift();
          job.controller.signal.removeEventListener('abort', job.queueAbort);
          try {
            if (job.controller.signal.aborted) throw new DOMException('Aborted', 'AbortError');
            await this.download(job); job.downloadResolve();
          } catch (error) { job.downloadReject(error); }
        }
      } finally { this.downloading = false; }
    }
    async download(job) {
      const chunks = [], expected = job.serverStatus.bytes;
      // A whole-file GET avoids the media Range failures seen on some networks.
      const requestedId = job.item.id, streamURL = () => '/api/playback/' + job.item.id + '/audio?key=' + encodeURIComponent(job.key);
      const request = () => deadline(fetch(streamURL(),
        {signal:job.controller.signal, cache:'no-store'}), () => job.controller.abort());
      let response = await request();
      if (response.status === 404 && requestedId !== job.item.id) response = await request();
      if (!response.ok || response.status !== 200) throw new Error('回听文件读取失败，请重试。');
      if (response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'audio/mpeg') throw new Error('收到的回听文件格式异常，请重试。');
      const total = Number(response.headers.get('Content-Length'));
      if (total > MAX_FILE_BYTES || expected > MAX_FILE_BYTES) throw new Error('回听文件超过 512 MB，请先拆分原音频。');
      if (expected && total && expected !== total) throw new Error('回听文件长度不一致，请重试。');
      let loaded = 0;
      const update = () => this.emit(job, {state:'loading', progress:total > 0 ? Math.min(99, loaded / total * 100) : null,
        message:`首次加载回听 · ${sizeText(loaded)}${total > 0 ? ' / ' + sizeText(total) + ' · ' + Math.min(99, Math.floor(loaded / total * 100)) + '%' : ''}`});
      if (response.body) {
        const reader = response.body.getReader();
        try {
          while (true) {
            const {done, value} = await deadline(reader.read(), () => job.controller.abort()); if (done) break;
            loaded += value.byteLength;
            if (loaded > MAX_FILE_BYTES) throw new Error('回听文件超过 512 MB，请先拆分原音频。');
            chunks.push(value); update();
          }
        } catch (error) { await reader.cancel().catch(() => {}); throw error; }
      } else {
        const value = await deadline(response.arrayBuffer(), () => job.controller.abort()); loaded = value.byteLength;
        if (loaded > MAX_FILE_BYTES) throw new Error('回听文件超过 512 MB，请先拆分原音频。');
        chunks.push(value);
      }
      if (!loaded || (total > 0 && loaded !== total) || (expected > 0 && loaded !== expected)) throw new Error('回听加载中断，请重试。');
      const blob = new Blob(chunks, {type:'audio/mpeg'}); chunks.length = 0;
      if (!await validMP3(blob)) throw new Error('回听文件不完整或格式异常，请重试。');
      if (job.controller.signal.aborted) throw new DOMException('Aborted', 'AbortError');
      const entry = this.remember(job.key, blob, false); this.ready(job, entry, true);
      const saved = await this.stored(job.key, 'put', {key:job.key, blob, bytes:blob.size, saved_at:Date.now()});
      if (job.controller.signal.aborted) throw new DOMException('Aborted', 'AbortError');
      entry.saved = saved === true;
      if (entry.saved) this.invalidKeys.delete(job.key);
      this.ready(job, entry);
    }
    async invalidate(item) {
      return this.forgetKey(this.keyFor(item));
    }
    async forgetKey(key) {
      if (!validKey(key)) throw new Error('回听标识无效，请刷新后重试。');
      const job = this.jobs.get(key);
      this.invalidKeys.add(key);
      if (job) { job.controller.abort(); await job.done; }
      const entry = this.memory.get(key);
      if (entry) { URL.revokeObjectURL(entry.url); this.memory.delete(key); }
      const removed = await this.stored(key, 'delete');
      return removed === true;
    }
    async removeRecords(remaining, deletedIds, removedKeys) {
      const alternatives = new Map(remaining.map(item => [this.keyFor(item), item]));
      const keys = new Set(removedKeys);
      for (const job of this.jobs.values()) {
        if (!deletedIds.has(job.item.id)) continue;
        const replacement = alternatives.get(job.key);
        if (replacement) job.item = replacement;
        else keys.add(job.key);
      }
      let failed = false;
      for (const key of keys) {
        if (alternatives.has(key)) continue;
        if (!await this.forgetKey(key)) failed = true;
      }
      if (failed) throw new Error('服务器记录已删除，但浏览器未能清理保存的回听，请清理此网站的浏览器数据。');
    }
  }
  window.audioPlaybackCache = new PlaybackFiles();
})();
