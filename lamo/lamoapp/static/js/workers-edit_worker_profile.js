document.addEventListener("DOMContentLoaded", function () {

    const setLocationBtn =
        document.getElementById("setWorkerLocationBtn");

    const statusEl =
        document.getElementById("workerLocationStatus");

    const latInput =
        document.querySelector('input[name="latitude"]');

    const lngInput =
        document.querySelector('input[name="longitude"]');


    if (!setLocationBtn || !latInput || !lngInput) {
        return;
    }


    setLocationBtn.addEventListener("click", function () {

        /* ==========================================
           Browser Support
           ========================================== */

        if (!navigator.geolocation) {

            showStatus(
                "Geolocation is not supported by your browser.",
                "error"
            );

            return;
        }


        /* ==========================================
           Loading State
           ========================================== */

        setLocationBtn.disabled = true;

        setLocationBtn.classList.add("loading");

        setLocationBtn.innerHTML = `
            <span class="location-spinner"></span>
            <span>Getting Location...</span>
        `;

        showStatus(
            "Please allow location access...",
            "loading"
        );


        /* ==========================================
           Get Location
           ========================================== */

        navigator.geolocation.getCurrentPosition(

            function (position) {

                const latitude =
                    position.coords.latitude;

                const longitude =
                    position.coords.longitude;


                /* Set form values */

                latInput.value =
                    latitude.toFixed(6);

                lngInput.value =
                    longitude.toFixed(6);


                /* Trigger input event */

                latInput.dispatchEvent(
                    new Event("input", {
                        bubbles: true
                    })
                );

                lngInput.dispatchEvent(
                    new Event("input", {
                        bubbles: true
                    })
                );


                /* Success */

                showStatus(
                    "✓ Location set successfully.",
                    "success"
                );


                setLocationBtn.innerHTML = `
                    <span>✓</span>
                    <span>Location Updated</span>
                `;

                setLocationBtn.classList.remove("loading");

                setLocationBtn.classList.add("success");


                /* Reset button after 3 seconds */

                setTimeout(function () {

                    setLocationBtn.disabled = false;

                    setLocationBtn.classList.remove("success");

                    setLocationBtn.innerHTML = `
                        <span>📍</span>
                        <span>Use My Current Location</span>
                    `;

                }, 3000);

            },


            function (error) {

                let message =
                    "Unable to get your location.";


                switch (error.code) {

                    case error.PERMISSION_DENIED:

                        message =
                            "Location permission denied. Please allow location access.";

                        break;


                    case error.POSITION_UNAVAILABLE:

                        message =
                            "Location information is unavailable.";

                        break;


                    case error.TIMEOUT:

                        message =
                            "Location request timed out. Please try again.";

                        break;
                }


                showStatus(message, "error");


                setLocationBtn.disabled = false;

                setLocationBtn.classList.remove("loading");

                setLocationBtn.innerHTML = `
                    <span>📍</span>
                    <span>Try Again</span>
                `;

            },


            {
                enableHighAccuracy: true,

                timeout: 10000,

                maximumAge: 300000
            }
        );
    });


    /* ==========================================
       STATUS FUNCTION
       ========================================== */

    function showStatus(message, type) {

        if (!statusEl) {
            return;
        }

        statusEl.textContent = message;

        statusEl.className =
            "ep-location-status " + type;
    }

});