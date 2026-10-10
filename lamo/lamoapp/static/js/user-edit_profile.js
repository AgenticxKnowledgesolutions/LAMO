// Show toast for form submission success
document.addEventListener('DOMContentLoaded', function() {
    const form = document.querySelector('.ep-form');
    if (form) {
        form.addEventListener('submit', function(e) {
            // Let the form submit normally, toast will show after redirect
        });
    }

    const setLocationBtn = document.getElementById('setUserLocationBtn');
    const statusEl = document.getElementById('userLocationStatus');
    const latInput = document.querySelector('input[name="latitude"]');
    const lngInput = document.querySelector('input[name="longitude"]');

    if (!setLocationBtn || !latInput || !lngInput) return;

    setLocationBtn.addEventListener('click', function() {
        if (!navigator.geolocation) {
            if (statusEl) {
                statusEl.textContent = 'Geolocation is not supported by your browser.';
                statusEl.style.color = '#dc2626';
            }
            return;
        }

        if (statusEl) {
            statusEl.textContent = 'Getting your location...';
            statusEl.style.color = '#6b7280';
        }

        navigator.geolocation.getCurrentPosition(
            function(position) {
                const lat = position.coords.latitude;
                const lng = position.coords.longitude;

                latInput.value = lat.toFixed(6);
                lngInput.value = lng.toFixed(6);

                if (statusEl) {
                    statusEl.textContent = 'Location set successfully.';
                    statusEl.style.color = '#16a34a';
                }
            },
            function(error) {
                let msg = 'Unable to get location.';
                switch (error.code) {
                    case error.PERMISSION_DENIED:
                        msg = 'Location permission denied. Please enable location access.';
                        break;
                    case error.POSITION_UNAVAILABLE:
                        msg = 'Location information unavailable.';
                        break;
                    case error.TIMEOUT:
                        msg = 'Location request timed out. Please try again.';
                        break;
                }
                if (statusEl) {
                    statusEl.textContent = msg;
                    statusEl.style.color = '#dc2626';
                }
            },
            {
                enableHighAccuracy: true,
                timeout: 10000,
                maximumAge: 300000
            }
        );
    });
});
