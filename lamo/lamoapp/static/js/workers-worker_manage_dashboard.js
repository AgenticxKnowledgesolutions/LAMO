document.addEventListener('DOMContentLoaded', function() {
    const updateBtn = document.getElementById('workerUpdateLocationBtn');
    const statusEl = document.getElementById('workerLocationStatus');
    const latInput = document.getElementById('workerLatitudeInput');
    const lngInput = document.getElementById('workerLongitudeInput');
    const form = document.getElementById('workerLocationForm');

    if (!updateBtn || !latInput || !lngInput || !form) return;

    updateBtn.addEventListener('click', function() {
        if (!navigator.geolocation) {
            if (statusEl) {
                statusEl.textContent = 'Geolocation is not supported by your browser.';
                statusEl.style.color = '#c62828';
                statusEl.style.display = 'block';
            }
            return;
        }

        if (statusEl) {
            statusEl.textContent = 'Getting your current location...';
            statusEl.style.color = '#718078';
            statusEl.style.display = 'block';
        }

        updateBtn.disabled = true;
        updateBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span> Updating...';

        navigator.geolocation.getCurrentPosition(
            function(position) {
                const lat = position.coords.latitude;
                const lng = position.coords.longitude;

                latInput.value = lat.toFixed(6);
                lngInput.value = lng.toFixed(6);

                if (statusEl) {
                    statusEl.textContent = 'Location captured. Saving...';
                    statusEl.style.color = '#087f5b';
                }

                form.submit();
            },
            function(error) {
                updateBtn.disabled = false;
                updateBtn.innerHTML = '<i class="bi bi-geo-alt-fill"></i> Update Current Location';

                let msg = 'Unable to get your location.';
                switch (error.code) {
                    case error.PERMISSION_DENIED:
                        msg = 'Location permission denied. Please enable location access in your browser settings.';
                        break;
                    case error.POSITION_UNAVAILABLE:
                        msg = 'Location information unavailable. Please try again.';
                        break;
                    case error.TIMEOUT:
                        msg = 'Location request timed out. Please try again.';
                        break;
                }

                if (statusEl) {
                    statusEl.textContent = msg;
                    statusEl.style.color = '#c62828';
                    statusEl.style.display = 'block';
                }
            },
            {
                enableHighAccuracy: true,
                timeout: 15000,
                maximumAge: 60000
            }
        );
    });
});
