(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var backdrop = document.getElementById('lamoChatBackdrop');
    var modal = document.getElementById('lamoChatModal');
    var titleEl = document.getElementById('lamoChatTitle');
    var subtitleEl = document.getElementById('lamoChatSubtitle');
    var bodyEl = document.getElementById('lamoChatBody');
    var formEl = document.getElementById('lamoChatForm');
    var inputEl = document.getElementById('lamoChatInput');
    var errorEl = document.getElementById('lamoChatError');
    var closeBtn = document.getElementById('lamoChatClose');
    if (!backdrop || !modal || !bodyEl || !formEl) { return; }

    var currentChatUrl = null;
    var currentBookingId = null;
    var lastFocused = null;

    function csrfToken() {
      var meta = document.querySelector('meta[name="csrf-token"]');
      if (meta && meta.getAttribute('content')) { return meta.getAttribute('content'); }
      var hidden = formEl.querySelector('input[name="csrfmiddlewaretoken"]');
      return hidden ? hidden.value : '';
    }

    function showError(text) {
      if (!errorEl) { return; }
      errorEl.textContent = text || '';
      errorEl.hidden = !text;
    }

    function bubbleNode(mine, text, meta) {
      var row = document.createElement('div');
      row.className = 'chat-msg-row' + (mine ? ' me' : '');
      var bubble = document.createElement('div');
      bubble.className = 'chat-msg-bubble';
      var p = document.createElement('div');
      p.className = 'chat-msg-text';
      p.textContent = text;
      bubble.appendChild(p);
      if (meta) {
        var m = document.createElement('div');
        m.className = 'chat-msg-meta';
        m.textContent = meta;
        bubble.appendChild(m);
      }
      row.appendChild(bubble);
      return row;
    }

    function emptyNode() {
      var d = document.createElement('div');
      d.className = 'chat-modal-empty';
      var s = document.createElement('strong');
      s.textContent = 'Start a conversation';
      var p = document.createElement('p');
      p.textContent = 'Send a message to your customer.';
      d.appendChild(s);
      d.appendChild(p);
      return d;
    }

    function setLoading() {
      bodyEl.innerHTML = '';
      var d = document.createElement('div');
      d.className = 'chat-modal-loading';
      d.textContent = 'Loading conversation...';
      bodyEl.appendChild(d);
    }

    function scrollBottom() {
      bodyEl.scrollTop = bodyEl.scrollHeight;
    }

    // Parse the server-rendered booking_messages page (same endpoint, same
    // authorization) into safe DOM nodes. textContent guarantees escaping.
    function renderThreadFromHtml(html) {
      bodyEl.innerHTML = '';
      var doc = new DOMParser().parseFromString(html, 'text/html');
      var rows = doc.querySelectorAll('.chat-body .msg-row');
      if (!rows.length) {
        bodyEl.appendChild(emptyNode());
        return;
      }
      rows.forEach(function (row) {
        var bubble = row.querySelector('.msg-bubble');
        if (!bubble) { return; }
        var metaEl = bubble.querySelector('.msg-meta');
        var meta = metaEl ? metaEl.textContent.trim().replace(/\s+/g, ' ') : '';
        if (metaEl) { metaEl.remove(); }
        var text = bubble.textContent.trim();
        bodyEl.appendChild(bubbleNode(row.classList.contains('me'), text, meta));
      });
      scrollBottom();
    }

    function openChat(btn) {
      currentBookingId = btn.getAttribute('data-booking-id');
      currentChatUrl = btn.getAttribute('data-chat-url');
      var name = btn.getAttribute('data-customer-name') || 'Customer';
      var service = btn.getAttribute('data-service-name') || '';
      titleEl.textContent = name;
      subtitleEl.textContent = 'Booking #' + currentBookingId + (service ? ' - ' + service : '');
      formEl.setAttribute('action', currentChatUrl);
      showError('');
      setLoading();
      lastFocused = document.activeElement;
      backdrop.hidden = false;
      modal.hidden = false;
      document.body.style.overflow = 'hidden';
      requestAnimationFrame(function () {
        backdrop.classList.add('show');
        modal.classList.add('show');
      });
      fetch(currentChatUrl, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' })
        .then(function (resp) {
          if (!resp.ok) {
            throw new Error(resp.status === 403
              ? 'You are not authorized to view this conversation.'
              : 'Could not load this conversation (HTTP ' + resp.status + ').');
          }
          return resp.text();
        })
        .then(renderThreadFromHtml)
        .catch(function (err) {
          bodyEl.innerHTML = '';
          showError(err.message || 'Could not load this conversation.');
        })
        .then(function () { inputEl.focus(); });
    }

    function closeChat() {
      backdrop.classList.remove('show');
      modal.classList.remove('show');
      document.body.style.overflow = '';
      window.setTimeout(function () {
        backdrop.hidden = true;
        modal.hidden = true;
      }, 180);
      currentBookingId = null;
      currentChatUrl = null;
      showError('');
      if (lastFocused && lastFocused.focus) { lastFocused.focus(); }
    }

    document.querySelectorAll('.chat-open-btn').forEach(function (btn) {
      btn.addEventListener('click', function () { openChat(btn); });
    });
    if (closeBtn) { closeBtn.addEventListener('click', closeChat); }
    backdrop.addEventListener('click', closeChat);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !modal.hidden) { closeChat(); }
    });

    inputEl.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        formEl.requestSubmit();
      }
    });

    formEl.addEventListener('submit', function (e) {
      e.preventDefault();
      if (!currentChatUrl) { return; }
      var text = inputEl.value.trim();
      if (!text) { return; }
      showError('');
      var fd = new FormData();
      fd.append('message', text);
      fetch(currentChatUrl, {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
        body: fd,
        credentials: 'same-origin'
      })
        .then(function (resp) {
          if (!resp.ok) {
            throw new Error(resp.status === 403
              ? 'You are not authorized to send messages here.'
              : 'Message could not be sent (HTTP ' + resp.status + ').');
          }
          return resp.text();
        })
        .then(function () {
          return fetch(currentChatUrl, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' });
        })
        .then(function (resp) {
          if (!resp.ok) { throw new Error('Message sent. Reopen the chat to refresh.'); }
          return resp.text();
        })
        .then(function (html) {
          renderThreadFromHtml(html);
          inputEl.value = '';
          inputEl.focus();
        })
        .catch(function (err) {
          showError(err.message || 'Message could not be sent.');
        });
    });
  });
})();
