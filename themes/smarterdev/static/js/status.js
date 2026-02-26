/**
 * SSE Status Indicator — Maps Skrift notification events to Online/Offline/Linking.
 *
 * Listens to the sk:notification-status CustomEvent dispatched by Skrift's
 * notifications.js and updates the .sk-connection-status widget.
 */
(function () {
  'use strict';

  // Status mapping: Skrift event → { label, cssClass }
  var STATUS_MAP = {
    connected:    { label: 'Online',  css: 'sk-status--online' },
    disconnected: { label: 'Offline', css: 'sk-status--offline' },
    suspended:    { label: 'Offline', css: 'sk-status--offline' },
    connecting:   { label: 'Linking', css: 'sk-status--linking' },
    reconnecting: { label: 'Linking', css: 'sk-status--linking' }
  };

  var ALL_CLASSES = ['sk-status--online', 'sk-status--offline', 'sk-status--linking'];

  function updateStatus(eventName) {
    var mapping = STATUS_MAP[eventName];
    if (!mapping) return;

    var elements = document.querySelectorAll('[data-sk-status]');
    elements.forEach(function (el) {
      // Remove all status classes
      ALL_CLASSES.forEach(function (cls) { el.classList.remove(cls); });

      // Add the new one
      el.classList.add(mapping.css);

      // Update label text
      var label = el.querySelector('.sk-status-label');
      if (label) label.textContent = mapping.label;
    });
  }

  // Listen for Skrift notification status events
  document.addEventListener('sk:notification-status', function (e) {
    var status = e.detail && e.detail.status;
    if (status) updateStatus(status);
  });
})();
