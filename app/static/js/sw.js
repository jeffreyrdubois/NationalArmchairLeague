// NAL Service Worker — handles incoming push notifications

self.addEventListener("push", function (event) {
  let data = { title: "NAL", body: "You have a new notification.", url: "/" };
  if (event.data) {
    try { data = event.data.json(); } catch (_) {}
  }
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: "/static/images/nal-logo.png",
      badge: "/static/images/nal-logo.png",
      data: { url: data.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  const target = event.notification.data && event.notification.data.url
    ? event.notification.data.url
    : "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
      for (const client of list) {
        if (client.url === target && "focus" in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(target);
    })
  );
});
