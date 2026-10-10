/* 助手对话界面：多轮上下文 + 输入体验 */
(function () {
  'use strict';

  var form = document.getElementById('chat-form');
  var input = document.getElementById('chat-question');
  var historyField = document.getElementById('chat-history');
  var messages = document.getElementById('messages');
  var sendBtn = document.getElementById('chat-send');
  if (!form || !input || !historyField || !messages) return;

  var MAX_TURNS = 8;   // 最多带 8 轮历史，和服务端的清洗上限对得上
  var pendingQuestion = '';   // 本轮提问；不要从 DOM 里读，实测会读到空

  function readHistory() {
    try {
      var parsed = JSON.parse(historyField.value || '[]');
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
      return [];
    }
  }

  function writeHistory(list) {
    // 只保留最近 MAX_TURNS 轮，避免越聊越慢、越烧额度
    historyField.value = JSON.stringify(list.slice(-MAX_TURNS * 2));
  }

  function scrollToBottom() {
    messages.scrollTop = messages.scrollHeight;
  }

  function removeEmptyState() {
    var empty = document.getElementById('chat-empty');
    if (empty) empty.remove();
  }

  function showPending(question) {
    removeEmptyState();
    var pending = document.createElement('div');
    pending.className = 'turn turn-pending';
    pending.id = 'chat-pending';
    pending.innerHTML = '<div class="bubble bubble-user"></div>' +
      '<div class="bubble bubble-ai"><div class="bubble-text muted">正在查…</div></div>';
    pending.querySelector('.bubble-user').textContent = question;
    messages.appendChild(pending);
    scrollToBottom();
    return pending;
  }

  function busy(on) {
    if (sendBtn) {
      sendBtn.disabled = on;
      sendBtn.textContent = on ? '查询中…' : '发送';
    }
    input.disabled = on;
  }

  // ---- Enter 发送 / Shift+Enter 换行 + 输入框随内容长高 ----
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event('submit'));
    }
  });
  input.addEventListener('input', function () {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  });

  // ---- 点示例问句直接发 ----
  messages.addEventListener('click', function (e) {
    var chip = e.target.closest('.chip-btn');
    if (!chip) return;
    input.value = chip.getAttribute('data-sample') || '';
    form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event('submit'));
  });

  // ---- 请求生命周期：这些事件挂在**发起请求的 form** 上，必须绑给 form。
  //      ⚠️ 绑到 #messages（form 的兄弟节点）收不到 —— 曾因此导致"正在查"提示
  //         从不显示、用户提完问界面毫无反馈。
  form.addEventListener('htmx:beforeRequest', function (e) {
    pendingQuestion = input.value.trim();
    if (!pendingQuestion) {
      e.preventDefault();
      return;
    }
    busy(true);
    showPending(pendingQuestion);
    input.value = '';
    input.style.height = 'auto';
  });

  form.addEventListener('htmx:afterRequest', function () {
    busy(false);
  });

  form.addEventListener('htmx:responseError', function () {
    busy(false);
    var pending = document.getElementById('chat-pending');
    if (pending) {
      pending.querySelector('.bubble-text').textContent = '查询失败，请稍后重试。';
    }
  });

  // ---- 每次请求都带上历史（configRequest 能改请求参数）----
  form.addEventListener('htmx:configRequest', function () {
    if (!historyField.value) historyField.value = '[]';
  });

  // ---- 片段追加进来之后：记历史、去掉"正在查"、滚到底 ----
  messages.addEventListener('htmx:afterSwap', function (e) {
    var turn = e.detail.target.lastElementChild;
    if (!turn || !turn.classList.contains('turn') || turn.classList.contains('turn-pending')) return;

    var pending = document.getElementById('chat-pending');
    if (pending) pending.remove();

    var answerNode = turn.querySelector('.bubble-ai .bubble-text');
    var history = readHistory();
    if (pendingQuestion) history.push({ role: 'user', content: pendingQuestion });
    if (answerNode) history.push({ role: 'assistant', content: answerNode.textContent.trim() });
    writeHistory(history);
    pendingQuestion = '';

    scrollToBottom();
    input.focus();
  });

  // 初始就有历史（通过 ?q= 进来的）时，把它填进 hidden，
  // 这样紧接着的第一次追问就是有上下文的。
  var firstQuestion = document.querySelector('#messages .bubble-user');
  var firstAnswer = document.querySelector('#messages .bubble-ai .bubble-text');
  if (firstQuestion && firstAnswer) {
    writeHistory([
      { role: 'user', content: firstQuestion.textContent.trim() },
      { role: 'assistant', content: firstAnswer.textContent.trim() }
    ]);
  }
})();
