document.addEventListener("DOMContentLoaded", function () {
    // -------------------------------------------------------------
    // TOAST NOTIFICATIONS
    // -------------------------------------------------------------
    const toasts = document.querySelectorAll(".lamo-toast");

    toasts.forEach(function (toast) {
        const closeButton = toast.querySelector(".lamo-toast-close");
        let timer;

        function closeToast() {
            if (toast.classList.contains("is-closing")) return;

            clearTimeout(timer);
            toast.classList.add("is-closing");

            setTimeout(function () {
                toast.remove();
            }, 350);
        }

        if (closeButton) {
            closeButton.addEventListener("click", closeToast);
        }

        timer = setTimeout(closeToast, 4500);

        /* Pause auto-close while the user is reading it */
        toast.addEventListener("mouseenter", function () {
            clearTimeout(timer);
            const progress = toast.querySelector(".lamo-toast-progress");
            if (progress) progress.style.animationPlayState = "paused";
        });

        toast.addEventListener("mouseleave", function () {
            const progress = toast.querySelector(".lamo-toast-progress");
            if (progress) progress.style.animationPlayState = "running";
            timer = setTimeout(closeToast, 1200);
        });
    });

    // -------------------------------------------------------------
    // BROADCAST ANNOUNCEMENT PRESET SELECTOR
    // -------------------------------------------------------------
    const presetSelect = document.getElementById("broadcastPresetSelect");
    const broadcastTitle = document.getElementById("broadcastTitle");
    const broadcastMessage = document.getElementById("broadcastMessage");

    const PRESET_TEMPLATES = {
        monsoon: {
            title: "Kerala Monsoon Weather & Safety Advisory",
            message: "Due to heavy rains in several districts, we urge all service professionals and customers to take necessary weather precautions. Please ensure indoor safety during outdoor work."
        },
        holiday: {
            title: "Holiday Operations & Festive Schedule Notice",
            message: "Happy festive season! Please note that standard bookings might have slight delays during festival days. Emergency services continue to operate 24/7."
        },
        safety: {
            title: "Worker Safety & Identity Verification Notice",
            message: "All customers and service partners are reminded to verify job completion codes and identity before initiating and completing jobs on the platform."
        },
        maintenance: {
            title: "Scheduled Platform Maintenance Alert",
            message: "LAMO platform will undergo brief scheduled maintenance tonight between 1:00 AM - 2:00 AM IST. All ongoing jobs will remain unaffected."
        }
    };

    if (presetSelect && broadcastTitle && broadcastMessage) {
        presetSelect.addEventListener("change", function () {
            const val = this.value;
            if (val && PRESET_TEMPLATES[val]) {
                broadcastTitle.value = PRESET_TEMPLATES[val].title;
                broadcastMessage.value = PRESET_TEMPLATES[val].message;
            }
        });
    }

    // Ensure broadcast announcement modal opens reliably on click
    const broadcastButtons = document.querySelectorAll(".btn-broadcast, [data-bs-target='#broadcastModal']");
    broadcastButtons.forEach(function (btn) {
        btn.addEventListener("click", function (e) {
            const modalEl = document.getElementById("broadcastModal");
            if (modalEl && window.bootstrap && window.bootstrap.Modal) {
                const modalInstance = bootstrap.Modal.getOrCreateInstance(modalEl);
                modalInstance.show();
            }
        });
    });

    // -------------------------------------------------------------
    // LIVE DISTRICT OPERATIONS LEAFLET MAP
    // -------------------------------------------------------------
    const mapContainer = document.getElementById("adminKeralaMap");
    const mapDataEl = document.getElementById("liveMapData");

    if (mapContainer && mapDataEl && typeof L !== "undefined") {
        try {
            const mapData = JSON.parse(mapDataEl.textContent || "{}");
            const bookings = mapData.bookings || [];
            const workers = mapData.workers || [];
            const districts = mapData.districts || [];

            // Initialize map centered on Kerala [10.3, 76.4]
            const map = L.map("adminKeralaMap", {
                center: [10.35, 76.4],
                zoom: 7,
                scrollWheelZoom: false
            });

            // Tile provider from dataset or Esri World Street Map fallback
            const defaultTileUrl = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}";
            const defaultAttribution = 'Tiles &copy; Esri &mdash; Source: Esri, HERE, Garmin, Intermap, GEBCO, USGS, FAO, NPS, NRCAN, GeoBase, IGN, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), &copy; OpenStreetMap contributors';

            const tileUrl = (mapContainer && mapContainer.dataset.tileUrl) || defaultTileUrl;
            const tileAttribution = (mapContainer && mapContainer.dataset.tileAttribution) || defaultAttribution;
            const tileSubdomains = (mapContainer && mapContainer.dataset.tileSubdomains) || '';
            const tileMaxZoom = parseInt(mapContainer && mapContainer.dataset.tileMaxZoom, 10) || 19;

            const tileOptions = {
                attribution: tileAttribution,
                maxZoom: tileMaxZoom
            };
            if (tileSubdomains) {
                tileOptions.subdomains = tileSubdomains;
            }

            L.tileLayer(tileUrl, tileOptions).addTo(map);

            const bookingLayer = L.layerGroup().addTo(map);
            const workerLayer = L.layerGroup().addTo(map);

            // Custom icon creators
            function createBookingIcon(isEmergency) {
                const color = isEmergency ? "#dc2626" : "#2563eb";
                const bg = isEmergency ? "#fee2e2" : "#dbeafe";
                const pulseClass = isEmergency ? "leaflet-emergency-marker" : "";
                return L.divIcon({
                    className: "custom-map-icon",
                    html: `<div class="${pulseClass}" style="width:28px; height:28px; border-radius:50%; background:${color}; color:#fff; display:flex; align-items:center; justify-content:center; box-shadow:0 3px 8px rgba(0,0,0,0.3); font-size:12px; border:2px solid #fff;"><i class="bi bi-${isEmergency ? 'exclamation-circle-fill' : 'calendar2-check-fill'}"></i></div>`,
                    iconSize: [28, 28],
                    iconAnchor: [14, 14],
                    popupAnchor: [0, -14]
                });
            }

            function createWorkerIcon() {
                return L.divIcon({
                    className: "custom-worker-icon",
                    html: `<div style="width:28px; height:28px; border-radius:50%; background:#059669; color:#fff; display:flex; align-items:center; justify-content:center; box-shadow:0 3px 8px rgba(0,0,0,0.3); font-size:12px; border:2px solid #fff;"><i class="bi bi-person-fill"></i></div>`,
                    iconSize: [28, 28],
                    iconAnchor: [14, 14],
                    popupAnchor: [0, -14]
                });
            }

            // Plot bookings
            bookings.forEach(function (b) {
                if (b.lat && b.lng) {
                    const marker = L.marker([b.lat, b.lng], {
                        icon: createBookingIcon(b.is_emergency)
                    });

                    const popupContent = `
                        <div style="font-family:inherit; min-width:180px; font-size:12px;">
                            <div style="font-weight:700; color:#1e293b; font-size:13px; margin-bottom:4px;">
                                ${b.is_emergency ? '<span style="color:#dc2626;">🚨 Emergency</span>' : '📅 Booking'} #${b.id}
                            </div>
                            <div style="color:#059669; font-weight:600; margin-bottom:6px;">${b.service}</div>
                            <div style="color:#64748b; line-height:1.4;">
                                <div><strong>Customer:</strong> ${b.customer}</div>
                                <div><strong>Worker:</strong> ${b.worker}</div>
                                <div><strong>District:</strong> ${b.district || 'Kerala'}</div>
                                <div><strong>Status:</strong> <span style="font-weight:600; color:#2563eb;">${b.status}</span></div>
                            </div>
                        </div>
                    `;
                    marker.bindPopup(popupContent);
                    bookingLayer.addLayer(marker);
                }
            });

            // Plot workers
            workers.forEach(function (w) {
                if (w.lat && w.lng) {
                    const marker = L.marker([w.lat, w.lng], {
                        icon: createWorkerIcon()
                    });

                    const popupContent = `
                        <div style="font-family:inherit; min-width:180px; font-size:12px;">
                            <div style="font-weight:700; color:#059669; font-size:13px; margin-bottom:4px;">
                                <i class="bi bi-person-badge me-1"></i> ${w.name}
                            </div>
                            <div style="color:#334155; font-weight:600; margin-bottom:6px;">${w.service}</div>
                            <div style="color:#64748b; line-height:1.4;">
                                <div><strong>District:</strong> ${w.district || 'Kerala'}</div>
                                <div><strong>Status:</strong> <span style="font-weight:600; color:#059669;">${w.status}</span></div>
                                <div><strong>Strikes:</strong> ${w.strikes > 0 ? '<span style="color:#dc2626; font-weight:700;">' + w.strikes + '/3</span>' : '<span style="color:#10b981;">Clean</span>'}</div>
                            </div>
                        </div>
                    `;
                    marker.bindPopup(popupContent);
                    workerLayer.addLayer(marker);
                }
            });

            // Map Filter Pills
            const filterBtns = document.querySelectorAll(".map-pill-btn");
            filterBtns.forEach(function (btn) {
                btn.addEventListener("click", function () {
                    filterBtns.forEach(b => b.classList.remove("active"));
                    this.classList.add("active");

                    const mode = this.getAttribute("data-filter");
                    if (mode === "all") {
                        map.addLayer(bookingLayer);
                        map.addLayer(workerLayer);
                    } else if (mode === "bookings") {
                        map.addLayer(bookingLayer);
                        map.removeLayer(workerLayer);
                    } else if (mode === "workers") {
                        map.removeLayer(bookingLayer);
                        map.addLayer(workerLayer);
                    }
                });
            });

            // District quick jump chips
            const districtChips = document.querySelectorAll(".district-chip");
            districtChips.forEach(function (chip) {
                chip.addEventListener("click", function () {
                    const lat = parseFloat(this.getAttribute("data-lat"));
                    const lng = parseFloat(this.getAttribute("data-lng"));
                    if (!isNaN(lat) && !isNaN(lng)) {
                        map.flyTo([lat, lng], 10, { duration: 1 });
                    }
                });
            });

            // Invalidate size once visible
            setTimeout(function () {
                map.invalidateSize();
            }, 300);

        } catch (e) {
            console.error("Failed to initialize Kerala Operations Map:", e);
        }
    }
});