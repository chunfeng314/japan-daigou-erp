/* 录单页：商品需求历史复用 + 截图上传 */
(function () {
  'use strict';

  function esc(value) {
    return String(value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  /* ---------------- 1) 商品需求：聚焦下拉本人最近用过的 ---------------- */
  var descInput = document.getElementById('product-desc');
  var box = document.getElementById('suggest-box');
  var dataEl = document.getElementById('history-data');
  var items = [];
  try { items = JSON.parse(dataEl ? dataEl.textContent : '[]') || []; } catch (err) { items = []; }
  var active = -1;

  function visibleItems() {
    if (!descInput) return [];
    var keyword = descInput.value.trim().toLowerCase();
    var list = keyword
      ? items.filter(function (it) { return String(it.desc).toLowerCase().indexOf(keyword) >= 0; })
      : items;
    return list.slice(0, 20);
  }

  function renderItems() {
    if (!box) return;
    var list = visibleItems();
    if (!list.length) { box.innerHTML = ''; box.hidden = true; return; }
    box.innerHTML = list.map(function (it, i) {
      return '<div class="suggest-item' + (i === active ? ' active' : '') + '" data-desc="' + esc(it.desc) + '">' +
        '<span class="suggest-text">' + esc(it.desc) + '</span>' +
        '<span class="suggest-count">用过 ' + (it.use_count || 1) + ' 次</span>' +
        '</div>';
    }).join('');
    box.hidden = false;
  }

  function closeBox() { active = -1; if (box) box.hidden = true; }

  function refreshHistory() {
    fetch('/api/product-history', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data && Array.isArray(data.items)) { items = data.items; renderItems(); }
      })
      .catch(function () { /* 拿不到就用服务端渲染的那份 */ });
  }

  if (descInput && box) {
    descInput.addEventListener('focus', function () { refreshHistory(); renderItems(); });
    descInput.addEventListener('input', function () { active = -1; renderItems(); });
    descInput.addEventListener('keydown', function (ev) {
      var list = visibleItems();
      if (!list.length || box.hidden) return;
      if (ev.key === 'ArrowDown') { ev.preventDefault(); active = Math.min(active + 1, list.length - 1); renderItems(); }
      else if (ev.key === 'ArrowUp') { ev.preventDefault(); active = Math.max(active - 1, 0); renderItems(); }
      else if (ev.key === 'Enter' && active >= 0) { ev.preventDefault(); descInput.value = list[active].desc; closeBox(); }
      else if (ev.key === 'Escape') { closeBox(); }
    });
    box.addEventListener('mousedown', function (ev) {
      var item = ev.target.closest ? ev.target.closest('.suggest-item') : null;
      if (!item) return;
      ev.preventDefault();
      descInput.value = item.getAttribute('data-desc');
      closeBox();
    });
    descInput.addEventListener('blur', function () { setTimeout(closeBox, 150); });
  }

  /* ---------------- 2) 截图上传 ---------------- */
  var fileInput = document.getElementById('shot-input');
  var listEl = document.getElementById('shot-list');
  var keysEl = document.getElementById('image-keys');
  var namesEl = document.getElementById('image-names');
  var hintEl = document.getElementById('upload-hint');
  var submitBtn = document.getElementById('submit-btn');
  var MAX_MB = 10;
  var uploaded = [];

  // 改单场景：先把已存在的截图装回来，否则保存时会被当成"全部移除"
  var existingEl = document.getElementById('existing-images');
  if (existingEl) {
    try { uploaded = JSON.parse(existingEl.textContent || '[]') || []; } catch (e) { uploaded = []; }
  }

  function syncHidden() {
    if (keysEl) keysEl.value = JSON.stringify(uploaded.map(function (f) { return f.key; }));
    if (namesEl) namesEl.value = JSON.stringify(uploaded.map(function (f) { return f.filename; }));
  }

  function renderShots() {
    if (!listEl) return;
    listEl.innerHTML = uploaded.map(function (f, i) {
      return '<div class="shot">' +
        '<img src="' + f.preview + '" alt="' + esc(f.filename) + '">' +
        '<button type="button" class="shot-del" data-i="' + i + '" title="移除">×</button>' +
        '<span class="shot-name">' + esc(f.filename) + '</span>' +
        '</div>';
    }).join('');
  }

  function signUpload(filename, contentType) {
    return fetch('/api/uploads/sign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: filename, content_type: contentType || null })
    }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          throw new Error(d.detail || '上传签名失败');
        });
      }
      return r.json();
    });
  }

  function uploadOne(file) {
    return signUpload(file.name, file.type).then(function (signed) {
      if (signed.url) {
        // cos 模式：浏览器直传，不经服务器中转
        var headers = file.type ? { 'Content-Type': file.type } : {};
        return fetch(signed.url, { method: 'PUT', body: file, headers: headers }).then(function (r) {
          if (!r.ok) throw new Error('直传失败（请检查存储桶 CORS 配置）');
          return signed.key;
        });
      }
      var fd = new FormData();
      fd.append('files', file);
      return fetch('/api/uploads', { method: 'POST', body: fd }).then(function (r) {
        if (!r.ok) {
          return r.json().catch(function () { return {}; }).then(function (d) {
            throw new Error(d.detail || '上传失败');
          });
        }
        return r.json().then(function (data) { return data.files[0].key; });
      });
    });
  }

  if (fileInput) {
    fileInput.addEventListener('change', function () {
      var files = Array.prototype.slice.call(fileInput.files || []);
      if (!files.length) return;
      fileInput.value = '';
      if (submitBtn) submitBtn.disabled = true;
      if (hintEl) hintEl.textContent = '上传中…';

      var failed = [];
      var chain = Promise.resolve();
      files.forEach(function (file) {
        chain = chain.then(function () {
          if (file.size > MAX_MB * 1024 * 1024) {
            failed.push(file.name + '（超过 ' + MAX_MB + 'MB）');
            return null;
          }
          return uploadOne(file).then(function (key) {
            uploaded.push({ key: key, filename: file.name, preview: URL.createObjectURL(file) });
            renderShots();
            syncHidden();
          }).catch(function (e) {
            failed.push(file.name + '（' + e.message + '）');
          });
        });
      });

      chain.then(function () {
        if (hintEl) {
          hintEl.textContent = failed.length
            ? '失败：' + failed.join('；')
            : '已上传 ' + uploaded.length + ' 张';
        }
        if (submitBtn) submitBtn.disabled = false;
      });
    });
  }

  if (listEl) {
    listEl.addEventListener('click', function (ev) {
      var btn = ev.target.closest ? ev.target.closest('.shot-del') : null;
      if (!btn) return;
      uploaded.splice(parseInt(btn.getAttribute('data-i'), 10), 1);
      renderShots();
      syncHidden();
      if (hintEl) {
        hintEl.textContent = uploaded.length ? '已上传 ' + uploaded.length + ' 张' : '可多选；单张不超过 10MB。';
      }
    });
  }

  /* ---------------- 3) 截图识别预填（P3） ---------------- */
  var extractInput = document.getElementById('extract-input');
  var extractStatus = document.getElementById('extract-status');

  var FIELD_LABELS = {
    platform: '平台', shop: '店铺', order_date: '下单日期', order_time: '下单时间',
    product_desc: '商品需求', quantity: '数量', sale_amount: '销售金额', sale_currency: '币种'
  };

  function applyFields(fields) {
    var done = [];
    Object.keys(fields || {}).forEach(function (name) {
      var el = document.querySelector('#order-form [name="' + name + '"]');
      if (!el) return;
      el.value = fields[name];
      el.classList.add('ai-filled');
      done.push(FIELD_LABELS[name] || name);
    });
    return done;
  }

  if (extractInput && extractStatus) {
    extractInput.addEventListener('change', function () {
      var file = extractInput.files && extractInput.files[0];
      if (!file) return;
      extractInput.value = '';

      if (file.size > MAX_MB * 1024 * 1024) {
        extractStatus.textContent = '图片超过 ' + MAX_MB + 'MB，请压缩后再传。';
        return;
      }

      extractStatus.textContent = '正在识别截图…（大约几秒）';
      var fd = new FormData();
      fd.append('file', file);

      fetch('/orders/extract', { method: 'POST', body: fd })
        .then(function (r) {
          return r.json()
            .catch(function () { return {}; })
            .then(function (body) { return { ok: r.ok, body: body }; });
        })
        .then(function (res) {
          if (!res.ok || !res.body || !res.body.ok) {
            extractStatus.textContent = '识别失败：' + ((res.body && res.body.error) || '未知错误') + '（可以手工填写）';
            return;
          }
          var filled = applyFields(res.body.fields);
          // 这张图同时就是订单截图，别让用户再选一次
          uploaded.push({
            key: res.body.key,
            filename: res.body.filename,
            preview: URL.createObjectURL(file)
          });
          renderShots();
          syncHidden();
          if (hintEl) hintEl.textContent = '已上传 ' + uploaded.length + ' 张';

          extractStatus.textContent = filled.length
            ? ('已按截图填好 ' + filled.length + ' 个字段：' + filled.join('、') + '。请核对后再提交。')
            : '这张截图没识别出可用字段，请手工填写。';
        })
        .catch(function (e) {
          extractStatus.textContent = '识别失败：' + e.message + '（可以手工填写）';
        });
    });
  }

  if (hintEl && uploaded.length) {
    hintEl.textContent = '已上传 ' + uploaded.length + ' 张';
  }
  renderShots();
  syncHidden();
})();
