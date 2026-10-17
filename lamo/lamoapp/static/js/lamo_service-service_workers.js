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

/* =========================================================
   WORKER PROFILE POPUP MODAL & REAL-TIME FILTERS
========================================================= */

let activeProfileData = null;

function openWorkerProfileModal(btn) {
    const modal = document.getElementById('workerProfileModal');
    if (!modal) return;

    activeProfileData = {
        workerId: btn.dataset.workerId || '',
        workerName: btn.dataset.workerName || 'Worker',
        serviceName: btn.dataset.workerService || '',
        serviceId: btn.dataset.serviceId || '',
        district: btn.dataset.workerDistrict || '',
        exp: btn.dataset.workerExp || '0',
        hours: btn.dataset.workerHours || 'Full Time',
        rate: btn.dataset.workerRate || '₹0.00 / Hour',
        rating: btn.dataset.workerRating || '5.0',
        reviews: btn.dataset.workerReviews || '1',
        image: btn.dataset.workerImage || '',
        verified: btn.dataset.workerVerified === '1',
        bookable: btn.dataset.workerBookable === '1',
        bio: btn.dataset.workerBio || ''
    };

    const nameEl = document.getElementById('profileModalName');
    const serviceEl = document.getElementById('profileModalService');
    const ratingEl = document.getElementById('profileModalRating');
    const reviewsEl = document.getElementById('profileModalReviews');
    const statusPill = document.getElementById('profileModalStatusPill');
    const expEl = document.getElementById('profileModalExp');
    const hoursEl = document.getElementById('profileModalHours');
    const districtEl = document.getElementById('profileModalDistrict');
    const rateEl = document.getElementById('profileModalRate');
    const bioEl = document.getElementById('profileModalBio');
    const coverageEl = document.getElementById('profileModalCoverage');
    const verifiedBadge = document.getElementById('profileModalVerified');
    const imgEl = document.getElementById('profileModalImg');
    const fallbackEl = document.getElementById('profileModalFallback');
    const bookBtn = document.getElementById('profileModalBookBtn');

    if (nameEl) nameEl.textContent = activeProfileData.workerName;
    if (serviceEl) serviceEl.textContent = activeProfileData.serviceName;
    if (ratingEl) ratingEl.textContent = activeProfileData.rating;
    if (reviewsEl) reviewsEl.textContent = `(${activeProfileData.reviews} review${activeProfileData.reviews === '1' ? '' : 's'})`;

    if (statusPill) {
        if (activeProfileData.bookable) {
            statusPill.textContent = 'Available';
            statusPill.style.background = 'rgba(5, 150, 105, 0.3)';
            statusPill.style.color = '#ffffff';
        } else {
            statusPill.textContent = 'Unavailable';
            statusPill.style.background = 'rgba(217, 119, 6, 0.3)';
            statusPill.style.color = '#ffffff';
        }
    }

    if (expEl) expEl.textContent = `${activeProfileData.exp} year${activeProfileData.exp === '1' ? '' : 's'}`;
    if (hoursEl) hoursEl.textContent = activeProfileData.hours;
    if (districtEl) districtEl.textContent = activeProfileData.district;
    if (coverageEl) coverageEl.textContent = activeProfileData.district;
    if (rateEl) rateEl.textContent = activeProfileData.rate;
    if (bioEl) {
        bioEl.textContent = activeProfileData.bio || `Experienced ${activeProfileData.serviceName.toLowerCase()} providing reliable, verified, and safe technical solutions for homes and commercial establishments across ${activeProfileData.district}.`;
    }

    if (verifiedBadge) {
        verifiedBadge.style.display = activeProfileData.verified ? 'block' : 'none';
    }

    if (imgEl && fallbackEl) {
        if (activeProfileData.image) {
            imgEl.src = activeProfileData.image;
            imgEl.style.display = 'block';
            fallbackEl.style.display = 'none';
        } else {
            imgEl.style.display = 'none';
            fallbackEl.style.display = 'flex';
        }
    }

    if (bookBtn) {
        if (activeProfileData.bookable) {
            bookBtn.disabled = false;
            bookBtn.style.opacity = '1';
            bookBtn.style.cursor = 'pointer';
            bookBtn.innerHTML = 'Book Now <i class="bi bi-arrow-right"></i>';
        } else {
            bookBtn.disabled = true;
            bookBtn.style.opacity = '0.5';
            bookBtn.style.cursor = 'not-allowed';
            bookBtn.innerHTML = 'Worker Unavailable';
        }
    }

    modal.classList.add('open');
    modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('modal-open');
}

function closeWorkerProfileModal() {
    const modal = document.getElementById('workerProfileModal');
    if (!modal) return;
    modal.classList.remove('open');
    modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('modal-open');
}

function bookFromProfileModal() {
    if (!activeProfileData || !activeProfileData.bookable) return;
    closeWorkerProfileModal();

    const dummyBtn = document.createElement('button');
    dummyBtn.dataset.workerId = activeProfileData.workerId;
    dummyBtn.dataset.workerName = activeProfileData.workerName;
    dummyBtn.dataset.workerDistrict = activeProfileData.district;
    dummyBtn.dataset.serviceId = activeProfileData.serviceId;
    dummyBtn.dataset.serviceName = activeProfileData.serviceName;
    if (activeProfileData.image) {
        dummyBtn.dataset.workerImage = activeProfileData.image;
    }

    if (typeof openBookingModal === 'function') {
        openBookingModal(dummyBtn);
    }
}

document.addEventListener('DOMContentLoaded', function() {
    const pModal = document.getElementById('workerProfileModal');
    if (pModal) {
        pModal.addEventListener('click', function(e) {
            if (e.target === this) {
                closeWorkerProfileModal();
            }
        });
    }

    initWorkerFilters();
});

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        const pModal = document.getElementById('workerProfileModal');
        if (pModal && pModal.classList.contains('open')) {
            closeWorkerProfileModal();
        }
    }
});

function initWorkerFilters() {
    const filterAvailableOnly = document.getElementById('filterAvailableOnly');
    const expCheckboxes = document.querySelectorAll('.filter-exp');
    const ratingCheckboxes = document.querySelectorAll('.filter-rating');
    const hoursCheckboxes = document.querySelectorAll('.filter-hours');
    const clearBtn = document.getElementById('clearAllFilters');
    const cards = Array.from(document.querySelectorAll('.worker-row-card'));
    const container = document.getElementById('workersListContainer');
    const noResultsMsg = document.getElementById('noFilterResultsMsg');

    if (!container || cards.length === 0) return;

    function applyFilters() {
        const onlyAvailable = filterAvailableOnly ? filterAvailableOnly.checked : false;

        const selectedExp = Array.from(expCheckboxes)
            .filter(cb => cb.checked)
            .map(cb => cb.value);

        const selectedRatings = Array.from(ratingCheckboxes)
            .filter(cb => cb.checked)
            .map(cb => parseFloat(cb.value));

        const selectedHours = Array.from(hoursCheckboxes)
            .filter(cb => cb.checked)
            .map(cb => cb.value.toLowerCase());

        let visibleCount = 0;

        cards.forEach(card => {
            const isBookable = card.dataset.bookable === '1';
            const exp = parseFloat(card.dataset.exp || '0');
            const rating = parseFloat(card.dataset.rating || '0');
            const hours = (card.dataset.hours || '').toLowerCase();

            let match = true;

            if (onlyAvailable && !isBookable) {
                match = false;
            }

            if (match && selectedExp.length > 0) {
                const expMatch = selectedExp.some(range => {
                    if (range === '0-2') return exp <= 2;
                    if (range === '3-5') return exp >= 3 && exp <= 5;
                    if (range === '5+') return exp >= 5;
                    return true;
                });
                if (!expMatch) match = false;
            }

            if (match && selectedRatings.length > 0) {
                const minRating = Math.min(...selectedRatings);
                if (rating < minRating) match = false;
            }

            if (match && selectedHours.length > 0) {
                const hoursMatch = selectedHours.some(h => hours.includes(h));
                if (!hoursMatch) match = false;
            }

            if (match) {
                card.style.display = 'flex';
                visibleCount++;
            } else {
                card.style.display = 'none';
            }
        });

        if (noResultsMsg) {
            noResultsMsg.style.display = visibleCount === 0 ? 'block' : 'none';
        }
    }

    if (filterAvailableOnly) filterAvailableOnly.addEventListener('change', applyFilters);
    expCheckboxes.forEach(cb => cb.addEventListener('change', applyFilters));
    ratingCheckboxes.forEach(cb => cb.addEventListener('change', applyFilters));
    hoursCheckboxes.forEach(cb => cb.addEventListener('change', applyFilters));

    if (clearBtn) {
        clearBtn.addEventListener('click', function() {
            if (filterAvailableOnly) filterAvailableOnly.checked = false;
            expCheckboxes.forEach(cb => cb.checked = false);
            ratingCheckboxes.forEach(cb => cb.checked = false);
            hoursCheckboxes.forEach(cb => cb.checked = false);
            applyFilters();
        });
    }

    applyFilters();
}

function applyWorkerSort(sortBy) {
    const container = document.getElementById('workersListContainer');
    if (!container) return;
    const cards = Array.from(container.querySelectorAll('.worker-row-card'));

    cards.sort((a, b) => {
        if (sortBy === 'rating-high') {
            return parseFloat(b.dataset.rating || '0') - parseFloat(a.dataset.rating || '0');
        } else if (sortBy === 'exp-high') {
            return parseFloat(b.dataset.exp || '0') - parseFloat(a.dataset.exp || '0');
        } else if (sortBy === 'price-low') {
            return parseFloat(a.dataset.price || '0') - parseFloat(b.dataset.price || '0');
        } else if (sortBy === 'price-high') {
            return parseFloat(b.dataset.price || '0') - parseFloat(a.dataset.price || '0');
        } else {
            return parseInt(a.dataset.workerId || '0') - parseInt(b.dataset.workerId || '0');
        }
    });

    cards.forEach(card => container.appendChild(card));
}

