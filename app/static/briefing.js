/* 日报页：把纯文本版复制到剪贴板 */
(function () {
  'use strict';

  var button = document.getElementById('copy-briefing');
  var area = document.getElementById('briefing-text');
  if (!button || !area) return;

  var original = button.textContent;

  function flash(text) {
    button.textContent = text;
    setTimeout(function () { button.textContent = original; }, 2000);
  }

  button.addEventListener('click', function () {
    // navigator.clipboard 只在 https / localhost 下可用，所以保留 execCommand 兜底
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(area.value)
        .then(function () { flash('已复制 ✓'); })
        .catch(function () { fallback(); });
      return;
    }
    fallback();

    function fallback() {
      area.removeAttribute('readonly');
      area.select();
      area.setSelectionRange(0, area.value.length);
      try {
        var ok = document.execCommand('copy');
        flash(ok ? '已复制 ✓' : '请手动复制');
      } catch (err) {
        flash('请手动复制');
      }
      area.setAttribute('readonly', 'readonly');
      window.getSelection().removeAllRanges();
    }
  });
})();
