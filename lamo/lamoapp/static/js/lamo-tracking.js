/**
 * LAMO Live Route Map & Tracking System (Leaflet / OpenStreetMap)
 * Real-time telemetry, route calculation, and live GPS marker updates.
 */

let trackingMap = null;
let customerMarker = null;
let workerMarker = null;
let routePolyline = null;
let trackingPollTimer = null;
let activeTrackingBookingId = null;

/**
 * Open and initialize the Live Tracking Modal for a given booking.
 */
function openLiveTrackingModal(bookingId) {
  activeTrackingBookingId = bookingId;

  const modalEl = document.getElementById('lamoTrackingModal');
  if (!modalEl) {
    console.error('[LAMO Tracking] Modal element not found in DOM.');
    return;
  }

  // Show modal (Bootstrap with fallback)
  if (typeof bootstrap !== 'undefined' && bootstrap.Modal) {
    const bsModal = bootstrap.Modal.getOrCreateInstance(modalEl);
    bsModal.show();
  } else {
    modalEl.classList.add('show');
    modalEl.style.display = 'block';
    modalEl.removeAttribute('aria-hidden');
    document.body.classList.add('modal-open');
  }

  // Show loading indicator
  const loader = document.getElementById('trackingMapLoader');
  if (loader) loader.classList.remove('hidden');

  // Initialize or invalidate map after modal transition finishes
  setTimeout(() => {
    initTrackingLeafletMap();
    fetchBookingTelemetry(bookingId, true);
  }, 350);

  // Start polling every 8 seconds
  if (trackingPollTimer) clearInterval(trackingPollTimer);
  trackingPollTimer = setInterval(() => {
    if (activeTrackingBookingId) {
      fetchBookingTelemetry(activeTrackingBookingId, false);
    }
  }, 8000);
}

/**
 * Initialize Leaflet Map instance on the container.
 */
function initTrackingLeafletMap() {
  const container = document.getElementById('lamoTrackingMap');
  if (!container) return;

  if (!trackingMap) {
    // Default Kerala center (Kollam / Kochi corridor)
    trackingMap = L.map('lamoTrackingMap', {
      zoomControl: true,
      attributionControl: false
    }).setView([8.8932, 76.6141], 12);

    // Clean, high-resolution OpenStreetMap Humanitarian (HOT) street tiles
    L.tileLayer('https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> contributors'
    }).addTo(trackingMap);

    // Recenter button click
    const recenterBtn = document.getElementById('btnTrackingRecenter');
    if (recenterBtn) {
      recenterBtn.addEventListener('click', recenterTrackingMap);
    }
  } else {
    trackingMap.invalidateSize();
  }
}

/**
 * Fetch tracking telemetry from backend API.
 */
function fetchBookingTelemetry(bookingId, isInitialLoad) {
  fetch(`/api/booking/${bookingId}/tracking/`)
    .then(res => {
      if (!res.ok) throw new Error(`HTTP error ${res.status}`);
      return res.json();
    })
    .then(data => {
      if (!data.success) {
        console.warn('[LAMO Tracking] Telemetry warning:', data.error);
        return;
      }
      renderTrackingData(data, isInitialLoad);
    })
    .catch(err => {
      console.error('[LAMO Tracking] Fetch error:', err);
    });
}

/**
 * Render telemetry data, markers, polyline and UI metrics.
 */
function renderTrackingData(data, isInitialLoad) {
  // Hide loader
  const loader = document.getElementById('trackingMapLoader');
  if (loader) loader.classList.add('hidden');

  // Update Title & Subtitle for Worker vs Customer
  const modalTitle = document.getElementById('lamoTrackingModalLabel');
  if (modalTitle) {
    modalTitle.innerText = data.is_worker ? 'Route to Customer' : 'Live Pro Tracking';
  }

  // Update Subtitle
  const subTitle = document.getElementById('trackingSubTitle');
  if (subTitle) {
    subTitle.innerText = data.is_worker
      ? `Booking #${data.booking_id} • Directions to Customer Doorstep`
      : `Booking #${data.booking_id} • ${data.status_text}`;
  }

  const serviceName = document.getElementById('trackingServiceName');
  if (serviceName) serviceName.innerText = data.service_name;

  const statusBadge = document.getElementById('trackingStatusBadge');
  if (statusBadge) {
    statusBadge.innerText = data.status.replace(/_/g, ' ');
  }

  // Arrival OTP (if customer viewing and OTP exists)
  const otpContainer = document.getElementById('trackingOtpContainer');
  const otpCode = document.getElementById('trackingOtpCode');
  if (otpContainer && otpCode) {
    if (data.arrival_otp) {
      otpContainer.style.display = 'inline-flex';
      otpCode.innerText = data.arrival_otp;
    } else {
      otpContainer.style.display = 'none';
    }
  }

  // Person Details Card (Customer info for worker, Worker info for customer)
  const wName = document.getElementById('trackingWorkerName');
  const wPlace = document.getElementById('trackingWorkerPlace');
  const wRating = document.getElementById('trackingWorkerRating');
  const wAvatar = document.getElementById('trackingWorkerAvatar');
  const callBtn = document.getElementById('btnTrackingCall');

  if (data.is_worker) {
    // When worker views Route modal, show customer details & call customer button
    if (wName) wName.innerText = data.customer ? data.customer.name : 'Customer';
    if (wPlace) wPlace.innerText = data.customer ? (data.customer.address || 'Doorstep Destination') : 'Customer Location';
    if (wRating) wRating.innerHTML = `<i class="bi bi-person-check-fill text-success"></i> Customer`;
    if (wAvatar) wAvatar.src = '/static/img/avatar1.png';

    if (callBtn) {
      callBtn.title = "Call Customer (In-App)";
      callBtn.onclick = function() {
        if (typeof initiateLamoCall === 'function') {
          initiateLamoCall(data.booking_id, data.customer ? data.customer.name : 'Customer', 'Customer');
        } else {
          alert(`Connecting call to customer...`);
        }
      };
    }
  } else if (data.worker) {
    // When customer views, show assigned worker details
    if (wName) wName.innerText = data.worker.name;
    if (wPlace) wPlace.innerText = data.worker.current_place || 'Assigned Zone';
    if (wRating) wRating.innerHTML = `<i class="bi bi-star-fill text-warning"></i> ${data.worker.rating.toFixed(1)}`;
    if (wAvatar && data.worker.avatar) {
      wAvatar.src = data.worker.avatar;
    }

    if (callBtn) {
      callBtn.title = "Call Service Professional (In-App)";
      callBtn.onclick = function() {
        if (typeof initiateLamoCall === 'function') {
          initiateLamoCall(data.booking_id, data.worker.name, 'Service Professional');
        } else {
          alert(`Connecting call to ${data.worker.name}...`);
        }
      };
    }
  }

  // Customer destination address
  const custAddress = document.getElementById('trackingCustomerAddress');
  if (custAddress) {
    custAddress.innerText = data.customer.address || `${data.customer.name}'s Location`;
  }

  // Distance & ETA
  const distEl = document.getElementById('trackingMetricDistance');
  if (distEl) {
    distEl.innerText = (data.distance_km !== null) ? `${data.distance_km} km` : '-- km';
  }

  const etaEl = document.getElementById('trackingMetricEta');
  if (etaEl) {
    if (data.eta_minutes !== null) {
      etaEl.innerText = (data.eta_minutes <= 1) ? 'Arrived' : `~${data.eta_minutes} mins`;
    } else {
      etaEl.innerText = '-- mins';
    }
  }

  // Google Maps Directions link
  const gmapsBtn = document.getElementById('btnTrackingGmaps');
  if (gmapsBtn) {
    if (data.gmaps_url) {
      gmapsBtn.href = data.gmaps_url;
      gmapsBtn.style.display = 'inline-flex';
    } else {
      gmapsBtn.style.display = 'none';
    }
  }

  // Update Leaflet Markers & Route
  updateMapTelemetry(data, isInitialLoad);
}

/**
 * Update Leaflet Map with Customer & Worker Pins and Route Polyline.
 */
function updateMapTelemetry(data, isInitialLoad) {
  if (!trackingMap) return;

  const cLat = data.customer.latitude;
  const cLng = data.customer.longitude;

  // 1. Customer Marker
  const customerIcon = L.divIcon({
    className: 'leaflet-customer-pin-wrap',
    html: '<div class="leaflet-customer-pin"><i class="bi bi-house-door-fill"></i></div>',
    iconSize: [38, 38],
    iconAnchor: [19, 19]
  });

  if (!customerMarker) {
    customerMarker = L.marker([cLat, cLng], { icon: customerIcon })
      .bindPopup(`<b>Your Address</b><br>${data.customer.address}`)
      .addTo(trackingMap);
  } else {
    customerMarker.setLatLng([cLat, cLng]);
  }

  // 2. Worker Marker (if worker assigned)
  if (data.worker && data.worker.latitude && data.worker.longitude) {
    const wLat = data.worker.latitude;
    const wLng = data.worker.longitude;

    const workerIcon = L.divIcon({
      className: 'leaflet-worker-pin-wrap',
      html: '<div class="leaflet-worker-pin"><i class="bi bi-scooter"></i></div>',
      iconSize: [44, 44],
      iconAnchor: [22, 22]
    });

    if (!workerMarker) {
      workerMarker = L.marker([wLat, wLng], { icon: workerIcon })
        .bindPopup(`<b>${data.worker.name}</b><br><small class="text-success"><i class="bi bi-shield-check"></i> Verified Pro</small>`)
        .addTo(trackingMap);
    } else {
      // Smoothly move worker pin
      workerMarker.setLatLng([wLat, wLng]);
    }

    // 3. Route Polyline
    const routeCoords = [
      [wLat, wLng],
      [cLat, cLng]
    ];

    if (!routePolyline) {
      routePolyline = L.polyline(routeCoords, {
        color: '#0284c7',
        weight: 4,
        opacity: 0.85,
        dashArray: '8, 8',
        lineCap: 'round'
      }).addTo(trackingMap);
    } else {
      routePolyline.setLatLngs(routeCoords);
    }

    // Fit map bounds on initial load
    if (isInitialLoad) {
      const bounds = L.latLngBounds([
        [cLat, cLng],
        [wLat, wLng]
      ]);
      trackingMap.fitBounds(bounds, { padding: [60, 60], maxZoom: 15 });
    }
  } else {
    // Only customer location available
    if (isInitialLoad) {
      trackingMap.setView([cLat, cLng], 14);
    }
  }
}

/**
 * Recenter and fit both pins inside the viewport.
 */
function recenterTrackingMap() {
  if (!trackingMap) return;
  const points = [];
  if (customerMarker) points.push(customerMarker.getLatLng());
  if (workerMarker) points.push(workerMarker.getLatLng());

  if (points.length > 1) {
    trackingMap.fitBounds(L.latLngBounds(points), { padding: [60, 60], maxZoom: 15 });
  } else if (points.length === 1) {
    trackingMap.setView(points[0], 14);
  }
}

/**
 * Close modal and clear polling timer.
 */
function closeTrackingModal() {
  if (trackingPollTimer) {
    clearInterval(trackingPollTimer);
    trackingPollTimer = null;
  }
  activeTrackingBookingId = null;

  const modalEl = document.getElementById('lamoTrackingModal');
  if (modalEl) {
    if (typeof bootstrap !== 'undefined' && bootstrap.Modal) {
      const bsModal = bootstrap.Modal.getInstance(modalEl);
      if (bsModal) bsModal.hide();
    }
    modalEl.classList.remove('show');
    modalEl.style.display = 'none';
    modalEl.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('modal-open');
    const backdrop = document.querySelector('.modal-backdrop');
    if (backdrop) backdrop.remove();
  }
}

// Clean up when modal is hidden via backdrop or ESC key
document.addEventListener('DOMContentLoaded', () => {
  const modalEl = document.getElementById('lamoTrackingModal');
  if (modalEl) {
    modalEl.addEventListener('hidden.bs.modal', () => {
      closeTrackingModal();
    });
  }
});
