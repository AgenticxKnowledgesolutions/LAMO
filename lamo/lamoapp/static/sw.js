// LAMO Service Worker for Background Web Push Notifications
self.addEventListener('push', function (event) {
    if (!event.data) return;

    let data = {};
    try {
        data = event.data.json();
    } catch (e) {
        data = {
            title: 'LAMO Alert',
            body: event.data.text(),
            url: '/worker-dashboard/'
        };
    }

    const title = data.title || 'New Job Request | LAMO';
    const options = {
        body: data.body || 'You have received a new service booking request.',
        icon: data.icon || '/static/images/logo.png',
        badge: data.badge || '/static/images/logo.png',
        data: {
            url: data.url || '/worker-dashboard/'
        },
        vibrate: [200, 100, 200, 100, 200],
        requireInteraction: true,
        tag: data.tag || 'lamo-job-notification',
        renotify: true,
        actions: [
            { action: 'open', title: 'View Job' }
        ]
    };

    event.waitUntil(
        self.registration.showNotification(title, options)
    );
});

self.addEventListener('notificationclick', function (event) {
    event.notification.close();

    const targetUrl = (event.notification.data && event.notification.data.url)
        ? event.notification.data.url
        : '/worker-dashboard/';

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (clientList) {
            for (let i = 0; i < clientList.length; i++) {
                const client = clientList[i];
                if (client.url.includes('/worker-dashboard') && 'focus' in client) {
                    client.navigate(targetUrl);
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow(targetUrl);
            }
        })
    );
});
