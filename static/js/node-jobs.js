(() => {
  const toggle = document.getElementById('nj-auto');
  const status = document.getElementById('nj-refresh-status');
  if (!toggle || toggle.disabled) return;
  let busy = false;
  async function refresh() {
    if (!toggle.checked || document.hidden || busy || document.activeElement?.closest('#nj-live')) return;
    busy = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(window.location.href, {credentials:'same-origin', cache:'no-store', signal:controller.signal});
      if (!response.ok || response.redirected) throw new Error('unavailable');
      const next = new DOMParser().parseFromString(await response.text(), 'text/html').getElementById('nj-live');
      if (!next) throw new Error('unavailable');
      if (!toggle.checked || document.activeElement?.closest('#nj-live')) return;
      const opened = [...document.querySelectorAll('#nj-live details')].map(d => d.open);
      [...next.querySelectorAll('details')].forEach((d, i) => { d.open = !!opened[i]; });
      document.getElementById('nj-live').replaceWith(next);
      status.textContent = `已更新 ${new Date().toLocaleTimeString('zh-TW')} · 每 10 秒更新`;
    } catch (_) { status.textContent = '暫時無法更新，保留目前畫面；稍後重試或手動重新整理。'; }
    finally { clearTimeout(timeout); busy = false; }
  }
  toggle.addEventListener('change', () => { status.textContent = toggle.checked ? '自動更新已開啟' : '自動更新已暫停'; });
  setInterval(refresh, 10000);
})();
