/* =========================================================
   GENERIC "MARK AS READ" HELPER
   -----------------------------------------------------
   Shared by the worker and admin notification pages so both
   reuse the SAME existing endpoint (/notifications/<id>/mark-read/),
   the same CSRF handling and the same recipient ownership check
   that notifications.js already uses for customers. There is no
   second notification store, model or endpoint here.

   The pages have different card markup, so instead of hard-coding
   this project's class names the button declares what to toggle via
   data attributes:
     data-notification-id  the Notification id to POST
     data-unread-class     class removed once read
     data-unread-selector  selector for the unread marker to remove
     data-read-class       class added once read (optional)
   ========================================================= */

(function () {
    'use strict';

    function getCsrfToken() {
        // Read from the DOM first: with CSRF_COOKIE_HTTPONLY=True
        // (production) the csrftoken cookie is unreadable from JS.
        var meta = document.querySelector('meta[name="csrf-token"]');
        if (meta && meta.content) {
            return meta.content;
        }
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) {
            return input.value;
        }
        return '';
    }

    function markRead(button) {
        var id = button.getAttribute('data-notification-id');
        if (!id) {
            return;
        }

        // Guard against a double click firing two POSTs.
        if (button.dataset.lamoBusy === '1') {
            return;
        }
        button.dataset.lamoBusy = '1';
        button.disabled = true;

        fetch('/notifications/' + encodeURIComponent(id) + '/mark-read/', {
            method: 'POST',
            headers: {
                'X-CSRFToken': getCsrfToken(),
                'X-Requested-With': 'XMLHttpRequest'
            },
            credentials: 'same-origin'
        })
            .then(function (response) {
                if (!response.ok) {
                    throw new Error('HTTP ' + response.status);
                }
                return response.json();
            })
            .then(function (data) {
                if (!data || data.success !== true) {
                    throw new Error('unexpected response');
                }

                var card = button.closest('.notification-card, .admin-notification-card');
                var unreadClass = button.getAttribute('data-unread-class');
                var readClass = button.getAttribute('data-read-class');
                var unreadSelector = button.getAttribute('data-unread-selector');

                if (card) {
                    if (unreadClass) {
                        card.classList.remove(unreadClass);
                    }
                    if (readClass) {
                        card.classList.add(readClass);
                    }
                }

                if (unreadSelector) {
                    var dots = document.querySelectorAll(unreadSelector);
                    for (var i = 0; i < dots.length; i += 1) {
                        dots[i].remove();
                    }
                }

                // The control has done its job - remove it rather than leaving a
                // dead "Mark as read" on an already-read notification.
                if (button.parentNode) {
                    button.parentNode.removeChild(button);
                }

                // Keep the header counter honest when the page shows one.
                var counter = document.querySelector('[data-unread-counter]');
                if (counter && typeof data.unread_count !== 'undefined') {
                    if (data.unread_count <= 0) {
                        counter.textContent = '0';
                    } else {
                        counter.textContent = String(data.unread_count);
                    }
                }
            })
            .catch(function () {
                // Graceful failure: restore the control so the user can retry,
                // and say nothing alarming on screen.
                button.disabled = false;
                button.dataset.lamoBusy = '0';
                button.setAttribute('title', 'Could not mark as read. Please try again.');
            });
    }

    document.addEventListener('DOMContentLoaded', function () {
        var buttons = document.querySelectorAll('[data-notification-id][data-lamo-mark-read]');
        Array.prototype.forEach.call(buttons, function (button) {
            button.addEventListener('click', function () {
                markRead(button);
            });
        });
    });
})();

/* =========================================================
   CUSTOMER NOTIFICATIONS (existing behaviour, unchanged)
   - Updates card state immediately (unread -> read)
   - Updates unread count in header
   - No full page reload
   ========================================================= */

(function () {
    function getCookie(name) {
        var cookieValue = null;
        if (document.cookie && document.cookie !== '') {
            var cookies = document.cookie.split(';');
            for (var i = 0; i < cookies.length; i++) {
                var cookie = cookies[i].trim();
                if (cookie.substring(0, name.length + 1) === (name + '=')) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }
        return cookieValue;
    }

    /* The token is read from the DOM first. With CSRF_COOKIE_HTTPONLY=True
       (production) JavaScript cannot read the csrftoken cookie at all, so a
       cookie-only lookup returned null and the request was rejected with 403. */
    function getCsrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        if (meta && meta.content) {
            return meta.content;
        }

        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) {
            return input.value;
        }

        // Fallback for deployments that still expose the cookie.
        return getCookie('csrftoken') || '';
    }

    function updateHeaderUnreadCount(newCount) {
        var statusContainer = document.querySelector('.notifications-status');
        if (!statusContainer) return;

        if (newCount > 0) {
            statusContainer.innerHTML =
                '<i class="bi bi-circle-fill"></i>' +
                '<span class="count-pill">' + newCount + '</span>' +
                'new ' + (newCount === 1 ? 'notification' : 'notifications');
        } else {
            statusContainer.innerHTML =
                '<i class="bi bi-check-circle-fill"></i>' +
                "You're all caught up";
        }
    }

    function markRead(notificationId, button) {
        fetch('/notifications/' + notificationId + '/mark-read/', {
            method: 'POST',
            headers: {
                'X-CSRFToken': getCsrfToken(),
                'X-Requested-With': 'XMLHttpRequest'
            },
            credentials: 'same-origin'
        })
        .then(function(response) { return response.json(); })
        .then(function(data) {
            if (data.success) {
                var card = document.getElementById('notification-' + notificationId);
                if (card) {
                    card.classList.remove('is-unread');
                    card.classList.add('is-read');
                    card.setAttribute('data-is-read', 'true');

                    // Remove NEW badge
                    var newBadge = card.querySelector('.notification-new-badge');
                    if (newBadge) newBadge.remove();

                    // Remove mark-as-read button
                    if (button && button.parentNode) {
                        button.parentNode.removeChild(button);
                    }
                }

                // Update header unread count
                if (typeof data.unread_count !== 'undefined') {
                    updateHeaderUnreadCount(data.unread_count);
                }
            }
        })
        .catch(function(err) { console.error('Error:', err); });
    }

    // Attach event listeners to all mark-read buttons
    document.addEventListener('DOMContentLoaded', function() {
        var buttons = document.querySelectorAll('.mark-read-btn');
        buttons.forEach(function(btn) {
            btn.addEventListener('click', function() {
                var id = this.getAttribute('data-id');
                markRead(id, this);
            });
        });
    });
})();


(function() {
    function getCookie(name) {
        var cookieValue = null;
        if (document.cookie && document.cookie !== '') {
            var cookies = document.cookie.split(';');
            for (var i = 0; i < cookies.length; i++) {
                var cookie = cookies[i].trim();
                if (cookie.substring(0, name.length + 1) === (name + '=')) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }
        return cookieValue;
    }

    /* The token is read from the DOM first. With CSRF_COOKIE_HTTPONLY=True
       (production) JavaScript cannot read the csrftoken cookie at all, so a
       cookie-only lookup returned null and the request was rejected with 403. */
    function getCsrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        if (meta && meta.content) {
            return meta.content;
        }

        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) {
            return input.value;
        }

        // Fallback for deployments that still expose the cookie.
        return getCookie('csrftoken') || '';
    }

    function updateHeaderUnreadCount(newCount) {
        var statusContainer = document.querySelector('.notifications-status');
        if (!statusContainer) return;

        if (newCount > 0) {
            statusContainer.innerHTML =
                '<i class="bi bi-circle-fill"></i>' +
                '<span class="count-pill">' + newCount + '</span>' +
                'new ' + (newCount === 1 ? 'notification' : 'notifications');
        } else {
            statusContainer.innerHTML =
                '<i class="bi bi-check-circle-fill"></i>' +
                "You're all caught up";
        }
    }

    function markRead(notificationId, button) {
        fetch('/notifications/' + notificationId + '/mark-read/', {
            method: 'POST',
            headers: {
                'X-CSRFToken': getCsrfToken(),
                'X-Requested-With': 'XMLHttpRequest'
            },
            credentials: 'same-origin'
        })
        .then(function(response) { return response.json(); })
        .then(function(data) {
            if (data.success) {
                var card = document.getElementById('notification-' + notificationId);
                if (card) {
                    card.classList.remove('is-unread');
                    card.classList.add('is-read');
                    card.setAttribute('data-is-read', 'true');

                    // Remove NEW badge
                    var newBadge = card.querySelector('.notification-new-badge');
                    if (newBadge) newBadge.remove();

                    // Remove mark-as-read button
                    if (button && button.parentNode) {
                        button.parentNode.removeChild(button);
                    }
                }

                // Update header unread count
                if (typeof data.unread_count !== 'undefined') {
                    updateHeaderUnreadCount(data.unread_count);
                }
            }
        })
        .catch(function(err) { console.error('Error:', err); });
    }

    // Attach event listeners to all mark-read buttons
    document.addEventListener('DOMContentLoaded', function() {
        var buttons = document.querySelectorAll('.mark-read-btn');
        buttons.forEach(function(btn) {
            btn.addEventListener('click', function() {
                var id = this.getAttribute('data-id');
                markRead(id, this);
            });
        });
    });
})();