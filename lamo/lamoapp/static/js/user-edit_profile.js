// Interactive Profile Settings Controller (Ideas 1, 2 & 3)

function triggerProfileImageUpload() {
    var fileInput = document.getElementById('id_profile_image');
    if (fileInput) {
        fileInput.click();
    }
}

document.addEventListener('DOMContentLoaded', function() {
    // ---------------------------------------------------------
    // 1. LIVE CLIENT-SIDE AVATAR PREVIEW (IDEA 2)
    // ---------------------------------------------------------
    var fileInput = document.getElementById('id_profile_image');
    var avatarWrapper = document.querySelector('.avatar-circle-wrapper');

    if (fileInput) {
        fileInput.addEventListener('change', function(e) {
            var file = e.target.files && e.target.files[0];
            if (!file) return;

            // Simple validation
            if (!file.type.match('image.*')) {
                alert('Please select an image file (JPG, PNG or WEBP).');
                return;
            }

            var reader = new FileReader();
            reader.onload = function(evt) {
                var previewImg = document.getElementById('profileAvatarPreview');
                var fallbackBox = document.getElementById('profileAvatarFallback');

                if (previewImg) {
                    previewImg.src = evt.target.result;
                } else if (fallbackBox && avatarWrapper) {
                    // Replace fallback initial with fresh image element
                    fallbackBox.style.display = 'none';
                    var newImg = document.createElement('img');
                    newImg.id = 'profileAvatarPreview';
                    newImg.className = 'profile-avatar-img';
                    newImg.src = evt.target.result;
                    newImg.alt = 'Uploaded avatar';
                    avatarWrapper.insertBefore(newImg, avatarWrapper.firstChild);
                }

                // 2. LIVE NAVBAR AVATAR UPDATE
                var navAvatarWrap = document.getElementById('userNavbarAvatarWrap');
                if (navAvatarWrap) {
                    navAvatarWrap.style.width = '34px';
                    navAvatarWrap.style.height = '34px';
                    navAvatarWrap.style.minWidth = '34px';
                    navAvatarWrap.style.minHeight = '34px';
                    navAvatarWrap.style.maxWidth = '34px';
                    navAvatarWrap.style.maxHeight = '34px';
                    navAvatarWrap.style.borderRadius = '50%';
                    navAvatarWrap.style.overflow = 'hidden';
                    navAvatarWrap.style.display = 'inline-flex';
                    navAvatarWrap.innerHTML = '<img id="userNavbarAvatar" src="' + evt.target.result + '" class="user-nav-avatar-img" style="width:34px !important;height:34px !important;min-width:34px !important;min-height:34px !important;max-width:34px !important;max-height:34px !important;border-radius:50% !important;object-fit:cover !important;display:block !important;" alt="Avatar">';
                }
            };
            reader.readAsDataURL(file);

            // 3. INSTANT AUTO-SAVE TO BACKEND VIA AJAX
            var csrfToken = document.querySelector('[name=csrfmiddlewaretoken]') ? document.querySelector('[name=csrfmiddlewaretoken]').value : '';
            if (csrfToken) {
                var formData = new FormData();
                formData.append('csrfmiddlewaretoken', csrfToken);
                formData.append('profile_image', file);
                formData.append('ajax_avatar', 'true');

                var statusPill = document.getElementById('avatarStatusPill');
                if (statusPill) {
                    statusPill.innerHTML = '<i class="bi bi-arrow-repeat spin"></i> Saving photo...';
                    statusPill.className = 'avatar-status-pill uploading';
                    statusPill.style.display = 'inline-flex';
                }

                fetch(window.location.href, {
                    method: 'POST',
                    body: formData,
                    headers: {
                        'X-Requested-With': 'XMLHttpRequest'
                    }
                })
                .then(function(res) { return res.json(); })
                .then(function(data) {
                    if (data && data.status === 'success') {
                        if (statusPill) {
                            statusPill.innerHTML = '<i class="bi bi-check-circle-fill"></i> Photo Saved!';
                            statusPill.className = 'avatar-status-pill success';
                            setTimeout(function() {
                                statusPill.style.display = 'none';
                            }, 4500);
                        }
                        if (data.image_url) {
                            var navAvatarWrap = document.getElementById('userNavbarAvatarWrap');
                            if (navAvatarWrap) {
                                navAvatarWrap.style.width = '34px';
                                navAvatarWrap.style.height = '34px';
                                navAvatarWrap.style.minWidth = '34px';
                                navAvatarWrap.style.minHeight = '34px';
                                navAvatarWrap.style.maxWidth = '34px';
                                navAvatarWrap.style.maxHeight = '34px';
                                navAvatarWrap.style.borderRadius = '50%';
                                navAvatarWrap.style.overflow = 'hidden';
                                navAvatarWrap.style.display = 'inline-flex';
                                navAvatarWrap.innerHTML = '<img id="userNavbarAvatar" src="' + data.image_url + '" class="user-nav-avatar-img" style="width:34px !important;height:34px !important;min-width:34px !important;min-height:34px !important;max-width:34px !important;max-height:34px !important;border-radius:50% !important;object-fit:cover !important;display:block !important;" alt="Avatar">';
                            }
                        }
                    } else {
                        if (statusPill) {
                            statusPill.innerHTML = '<i class="bi bi-exclamation-circle"></i> Save failed, click Save below';
                            statusPill.className = 'avatar-status-pill error';
                        }
                    }
                })
                .catch(function(err) {
                    console.log('Background upload error:', err);
                    if (statusPill) {
                        statusPill.innerHTML = '<i class="bi bi-info-circle"></i> Click Save Changes below to save';
                        statusPill.className = 'avatar-status-pill error';
                    }
                });
            }
        });
    }

    // ---------------------------------------------------------
    // 2. LIVE DISPLAY NAME SYNC IN SIDEBAR
    // ---------------------------------------------------------
    var firstNameInput = document.getElementById('id_first_name');
    var lastNameInput = document.getElementById('id_last_name');
    var sidebarNameEl = document.getElementById('sidebarDisplayName');

    function updateSidebarName() {
        var fn = firstNameInput ? firstNameInput.value.trim() : '';
        var ln = lastNameInput ? lastNameInput.value.trim() : '';
        var fullName = (fn + ' ' + ln).trim();
        if (sidebarNameEl && fullName) {
            sidebarNameEl.textContent = fullName;
        }
        var navbarNameEl = document.getElementById('userNavbarName');
        if (navbarNameEl && (fn || fullName)) {
            navbarNameEl.textContent = fn || fullName;
        }
    }

    if (firstNameInput) firstNameInput.addEventListener('input', updateSidebarName);
    if (lastNameInput) lastNameInput.addEventListener('input', updateSidebarName);

    // ---------------------------------------------------------
    // 3. SMOOTH SIDEBAR ANCHOR SCROLLING & ACTIVE STATE
    // ---------------------------------------------------------
    var navLinks = document.querySelectorAll('.sidebar-nav-link');
    navLinks.forEach(function(link) {
        link.addEventListener('click', function(e) {
            var href = link.getAttribute('href');
            if (href && href.startsWith('#')) {
                e.preventDefault();
                var target = document.querySelector(href);
                if (target) {
                    navLinks.forEach(function(l) { l.classList.remove('active'); });
                    link.classList.add('active');
                    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
            }
        });
    });

    // ---------------------------------------------------------
    // 4. GPS LOCATION PINNING
    // ---------------------------------------------------------
    var setLocationBtn = document.getElementById('setUserLocationBtn');
    var statusEl = document.getElementById('userLocationStatus');
    var latInput = document.querySelector('input[name="latitude"]');
    var lngInput = document.querySelector('input[name="longitude"]');

    if (setLocationBtn && latInput && lngInput) {
        setLocationBtn.addEventListener('click', function() {
            if (!navigator.geolocation) {
                if (statusEl) {
                    statusEl.innerHTML = '<i class="bi bi-exclamation-triangle-fill text-danger"></i> Geolocation is not supported by your browser.';
                    statusEl.className = 'gps-pin-status';
                }
                return;
            }

            if (statusEl) {
                statusEl.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Detecting your coordinates...';
                statusEl.className = 'gps-pin-status';
            }

            navigator.geolocation.getCurrentPosition(
                function(position) {
                    var lat = position.coords.latitude;
                    var lng = position.coords.longitude;

                    latInput.value = lat.toFixed(6);
                    lngInput.value = lng.toFixed(6);

                    if (statusEl) {
                        statusEl.innerHTML = '<i class="bi bi-check2-circle"></i> Exact coordinates pinned (' + lat.toFixed(4) + ', ' + lng.toFixed(4) + ')';
                        statusEl.className = 'gps-pin-status status-pinned';
                    }
                },
                function(error) {
                    var msg = 'Unable to get location.';
                    switch (error.code) {
                        case error.PERMISSION_DENIED:
                            msg = 'Location permission denied. Please enable location access in your browser.';
                            break;
                        case error.POSITION_UNAVAILABLE:
                            msg = 'Location information unavailable.';
                            break;
                        case error.TIMEOUT:
                            msg = 'Location request timed out. Please try again.';
                            break;
                    }
                    if (statusEl) {
                        statusEl.innerHTML = '<i class="bi bi-exclamation-circle text-danger"></i> ' + msg;
                        statusEl.className = 'gps-pin-status';
                    }
                },
                {
                    enableHighAccuracy: true,
                    timeout: 10000,
                    maximumAge: 300000
                }
            );
        });
    }
});
