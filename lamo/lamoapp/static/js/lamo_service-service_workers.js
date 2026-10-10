document.addEventListener(
    "DOMContentLoaded",
    function () {

        const cards =
            document.querySelectorAll(
                ".worker-card"
            );

        cards.forEach(
            function (card, index) {

                card.style.opacity = "0";

                card.style.transform =
                    "translateY(20px)";

                setTimeout(
                    function () {

                        card.style.transition =
                            "opacity .5s ease, transform .5s ease";

                        card.style.opacity = "1";

                        card.style.transform =
                            "translateY(0)";

                    },
                    80 + (index * 70)
                );

            }
        );

    }
);



/* =========================================================
   MAP INITIALIZATION FOR BOOKING MODAL
======================================================= */
document.addEventListener("DOMContentLoaded", function () {
    const mapContainer = document.getElementById("modalBookingMap");
    if (!mapContainer) return;

    const defaultLat = parseFloat(mapContainer.dataset.defaultLat) || 9.9312;
    const defaultLng = parseFloat(mapContainer.dataset.defaultLng) || 76.2673;
    // Tile provider is configured in Django settings (MAP_TILE_*) and passed to
    // this static file through data attributes on the map container.
    const tileUrl = mapContainer.dataset.tileUrl
        || "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}";
    const tileAttribution = mapContainer.dataset.tileAttribution
        || 'Tiles &copy; Esri &mdash; Source: Esri, HERE, Garmin, Intermap, GEBCO, USGS, FAO, NPS, NRCAN, GeoBase, IGN, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), &copy; OpenStreetMap contributors';
    const fullAttribution = tileAttribution
        .replace(/&copy;/gi, '©')
        .replace(/&mdash;/gi, '—');
    const compactAttribution = fullAttribution
        .split(/\s+—\s+Source:/i)[0]
        .replace(/^Tiles\s+/i, '')
        .trim() || fullAttribution;
    const escapeAttribution = value => String(value)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    const mapAttribution = `<span title="${escapeAttribution(fullAttribution)}">${escapeAttribution(compactAttribution)}</span>`;
    const tileSubdomains = mapContainer.dataset.tileSubdomains || '';
    const tileMaxZoom = parseInt(mapContainer.dataset.tileMaxZoom, 10) || 19;

    const map = L.map("modalBookingMap").setView([defaultLat, defaultLng], 13);

    L.tileLayer(tileUrl, {
        attribution: mapAttribution,
        subdomains: tileSubdomains,
        maxZoom: tileMaxZoom
    }).addTo(map);

    let marker = L.marker([defaultLat, defaultLng], { draggable: true }).addTo(map);

    const latInput = document.getElementById("modalLatitudeInput");
    const lngInput = document.getElementById("modalLongitudeInput");
    const locationInput = document.getElementById("modalLocationInput");
    const validationDiv = document.getElementById("modalDistrictValidation");

    function validateDistrict(lat, lng) {
        const district = mapContainer.dataset.district || '';
        if (!district || !validationDiv) return;
        fetch(`https://nominatim.openstreetmap.org/reverse?format=json&lat=${lat}&lon=${lng}`)
            .then(response => response.json())
            .then(data => {
                const address = data.address || {};
                const districtFields = ['state_district', 'county', 'district', 'city', 'town', 'village'];
                let matched = false;
                for (const field of districtFields) {
                    const value = address[field] || '';
                    // NOTE: this used to read `district.toLowerCase() in value.toLowerCase()`,
                    // which tests array indexes - never a substring - so the "wrong district"
                    // warning appeared even for a correct location.
                    if (value && value.toLowerCase().includes(district.toLowerCase())) {
                        matched = true;
                        break;
                    }
                }
                if (!matched && data.display_name) {
                    validationDiv.style.display = 'block';
                } else {
                    validationDiv.style.display = 'none';
                }
            })
            .catch(() => {
                validationDiv.style.display = 'none';
            });
    }

    marker.on("dragend", function (e) {
        const latlng = marker.getLatLng();
        latInput.value = latlng.lat.toFixed(6);
        lngInput.value = latlng.lng.toFixed(6);
        reverseGeocodeModal(latlng.lat, latlng.lng);
        validateDistrict(latlng.lat, latlng.lng);
    });

    map.on("click", function (e) {
        marker.setLatLng(e.latlng);
        latInput.value = e.latlng.lat.toFixed(6);
        lngInput.value = e.latlng.lng.toFixed(6);
        reverseGeocodeModal(e.latlng.lat, e.latlng.lng);
        validateDistrict(e.latlng.lat, e.latlng.lng);
    });

    function reverseGeocodeModal(lat, lng) {
        fetch(`https://nominatim.openstreetmap.org/reverse?format=json&lat=${lat}&lon=${lng}`)
            .then(response => response.json())
            .then(data => {
                if (data && data.display_name) {
                    locationInput.value = data.display_name;
                }
            })
            .catch(err => console.log("Geocoding error:", err));
    }

    const currentLocationBtn = document.getElementById("modalGetCurrentLocationBtn");
    if (currentLocationBtn) {
        currentLocationBtn.addEventListener("click", function () {
            if (navigator.geolocation) {
                navigator.geolocation.getCurrentPosition(
                    function (position) {
                        const lat = position.coords.latitude;
                        const lng = position.coords.longitude;
                        map.setView([lat, lng], 15);
                        marker.setLatLng([lat, lng]);
                        latInput.value = lat.toFixed(6);
                        lngInput.value = lng.toFixed(6);
                        reverseGeocodeModal(lat, lng);
                        validateDistrict(lat, lng);
                    },
                    function (error) {
                        alert("Unable to get your location. Please ensure location permission is granted.");
                    }
                );
            } else {
                alert("Geolocation is not supported by your browser.");
            }
        });

        currentLocationBtn.addEventListener("mouseenter", function() {
            this.style.background = "#087f5b";
            this.style.color = "#ffffff";
        });
        currentLocationBtn.addEventListener("mouseleave", function() {
            this.style.background = "#ffffff";
            this.style.color = "#087f5b";
        });
    }
});

/* =========================================================
   EMERGENCY SERVICE MODAL
======================================================= */

function openEmergencyModal() {
    const modal = document.getElementById('emergencyModal');
    if (!modal) return;
    modal.classList.add('open');
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('modal-open');
}

function closeEmergencyModal() {
    const modal = document.getElementById('emergencyModal');
    if (!modal) return;
    modal.classList.remove('open');
    modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('modal-open');
    const form = document.getElementById('emergencyForm');
    if (form) form.reset();
    const statusDiv = document.getElementById('emergencyStatus');
    if (statusDiv) {
        statusDiv.className = 'emergency-status';
        statusDiv.innerHTML = '';
    }
}

const emergencyModal = document.getElementById('emergencyModal');
if (emergencyModal) {
    emergencyModal.addEventListener('click', function(e) {
        if (e.target === this) {
            closeEmergencyModal();
        }
    });
}

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && emergencyModal && emergencyModal.classList.contains('open')) {
        closeEmergencyModal();
    }
});

const emergencyForm = document.getElementById('emergencyForm');
if (emergencyForm) {
    emergencyForm.addEventListener('submit', function(e) {
        e.preventDefault();

        var submitBtn = document.getElementById('emergencySubmitBtn');
        var statusDiv = document.getElementById('emergencyStatus');
        var problem = document.getElementById('emergencyProblem').value.trim();
        var location = document.getElementById('emergencyLocation').value.trim();
        var time = document.getElementById('emergencyTime').value;
        var phone = document.getElementById('emergencyPhone').value.trim();

        if (!problem || !location || !time || !phone) {
            statusDiv.className = 'emergency-status unavailable';
            statusDiv.innerHTML = '<i class="bi bi-exclamation-triangle"></i> Please fill in all required fields.';
            return;
        }

        if (!/^\d{10}$/.test(phone)) {
            statusDiv.className = 'emergency-status unavailable';
            statusDiv.innerHTML = '<i class="bi bi-exclamation-triangle"></i> Please enter a valid 10-digit phone number.';
            return;
        }

        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Finding Worker...';
        statusDiv.className = 'emergency-status finding';
        statusDiv.innerHTML = '🚨 Finding an available worker for you...';

        var formData = new FormData(this);

        fetch('/book-service/', {
            method: 'POST',
            body: formData,
            headers: {
                'X-Requested-With': 'XMLHttpRequest'
            }
        })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            submitBtn.disabled = false;
            submitBtn.innerHTML = '🚨 Find Available Worker';

            if (data.success) {
                if (data.emergency && data.waiting) {
                    statusDiv.className = 'emergency-status finding';
                    statusDiv.innerHTML = '🚨 Emergency Request Submitted! Redirecting to your bookings...';
                    setTimeout(function() {
                        window.location.href = data.redirect_url || '/my-bookings/';
                    }, 1500);
                } else {
                    statusDiv.className = 'emergency-status assigned';
                    statusDiv.innerHTML = '✅ Worker assigned: ' + (data.worker_name || 'Worker');
                    setTimeout(function() {
                        window.location.href = data.redirect_url || '/my-bookings/';
                    }, 2000);
                }
            } else {
                statusDiv.className = 'emergency-status unavailable';
                statusDiv.innerHTML = '<i class="bi bi-x-circle"></i> ' + (data.error_message || 'Unable to process request.');
            }
        })
        .catch(function(err) {
            submitBtn.disabled = false;
            submitBtn.innerHTML = '🚨 Find Available Worker';
            statusDiv.className = 'emergency-status unavailable';
            statusDiv.innerHTML = '<i class="bi bi-x-circle"></i> An error occurred. Please try again.';
        });
    });
}
