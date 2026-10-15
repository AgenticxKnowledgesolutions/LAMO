document.addEventListener(
    'DOMContentLoaded',
    function () {


        const form =
            document.getElementById(
                'registerForm'
            );


        const roleCards =
            document.querySelectorAll(
                '.role-card'
            );


        const roleSelect =
            document.getElementById(
                'roleSelect'
            );


        const stepAccount =
            document.getElementById(
                'stepAccount'
            );


        const stepWorkerOne =
            document.getElementById(
                'stepWorkerOne'
            );


        const stepWorkerTwo =
            document.getElementById(
                'stepWorkerTwo'
            );

        const continueButton =
            document.getElementById(
                'continueButton'
            );


        const workerNext =
            document.getElementById(
                'workerNext'
            );


        const workerBackOne =
            document.getElementById(
                'workerBackOne'
            );


        const workerBackTwo =
            document.getElementById(
                'workerBackTwo'
            );

        const workerNextTwo =
            document.getElementById(
                'workerNextTwo'
            );

        const heading =
            document.getElementById(
                'registerHeading'
            );


        const subtitle =
            document.getElementById(
                'registerSubtitle'
            );


        /* Initial entrance animation */
        if (stepAccount && stepAccount.classList.contains('active')) {

            stepAccount.classList.add(
                'step-entering'
            );

            setTimeout(function () {

                stepAccount.classList.remove(
                    'step-entering'
                );

            }, 450);

        }


        /* ==========================================
           SHOW STEP
        ========================================== */

        function showStep(step) {

            const current =
                document.querySelector(
                    '.register-step.active'
                );


            if (current && current !== step) {

                current.classList.add(
                    'step-exiting'
                );


                setTimeout(function () {

                    current.classList.remove(
                        'active',
                        'step-exiting'
                    );

                    step.classList.add(
                        'active',
                        'step-entering'
                    );


                    setTimeout(function () {

                        step.classList.remove(
                            'step-entering'
                        );

                    }, 450);

                }, 250);

            } else if (!current) {

                step.classList.add(
                    'active',
                    'step-entering'
                );


                setTimeout(function () {

                    step.classList.remove(
                        'step-entering'
                    );

                }, 450);

            }

        }


        /* ==========================================
           ROLE
        ========================================== */

        roleCards.forEach(
            function (card) {

                card.addEventListener(
                    'click',
                    function () {

                        roleCards.forEach(
                            function (item) {

                                item.classList.remove(
                                    'active'
                                );

                            }
                        );


                        this.classList.add(
                            'active'
                        );


                        roleSelect.value =
                            this.dataset.role;

                    }
                );


                card.addEventListener(
                    'keydown',
                    function (event) {

                        if (
                            event.key === 'Enter' ||
                            event.key === ' '
                        ) {

                            event.preventDefault();

                            this.click();

                        }

                    }
                );

            }
        );


        /* ==========================================
           CONTINUE
        ========================================== */

        continueButton.addEventListener(
            'click',
            function () {


                const role =
                    roleSelect.value;


                if (!role) {

                    alert(
                        'Please select User or Worker.'
                    );

                    return;

                }


                /*
                 USER
                 */

                if (role === 'USER') {

                    form.submit();

                    return;

                }


                /*
                 WORKER
                 */

                heading.textContent =
                    'Complete your worker profile';

                subtitle.textContent =
                    'A few professional details first.';


                showStep(
                    stepWorkerOne
                );

            }
        );


        /* ==========================================
           WORKER NEXT
        ========================================== */

        workerNext.addEventListener(
            'click',
            function () {


                const phone =
                    document
                        .getElementById('phone')
                        .value
                        .trim();


                const service =
                    document
                        .getElementById('service')
                        .value;


                const district =
                    document.getElementById('district').value;


                const address =
                    document
                        .getElementById('address')
                        .value
                        .trim();


                const experience =
                    document
                        .getElementById(
                            'experience_years'
                        )
                        .value;


                const availability =
                    document.getElementById('availability').value;


                if (
                    !/^[6-9][0-9]{9}$/.test(phone)
                ) {

                    alert(
                        'Please enter a valid 10-digit Indian mobile number.'
                    );

                    document
                        .getElementById('phone')
                        .focus();

                    return;

                }


                if (!service) {

                    alert(
                        'Please select your service.'
                    );

                    document
                        .getElementById('service')
                        .focus();

                    return;

                }


                if (!district) {
                    alert('Please select your district.');
                    document.getElementById('district').focus();
                    return;
                }


                if (!address) {
                    alert('Please enter your address.');
                    document.getElementById('address').focus();
                    return;
                }


                if (
                    experience === '' ||
                    Number(experience) < 0
                ) {

                    alert(
                        'Please enter valid experience.'
                    );

                    document
                        .getElementById(
                            'experience_years'
                        )
                        .focus();

                    return;

                }


                if (!availability) {
                    alert('Please select your availability.');
                    document.getElementById('availability').focus();
                    return;
                }


                heading.textContent =
                    'Pricing & Verification';

                subtitle.textContent =
                    'Set your pricing and upload verification details.';

                showStep(
                    stepWorkerTwo
                );

            }
        );


        /* ==========================================
           WORKER NEXT — STEP 3 TO STEP 4
        ========================================== */

        workerNextTwo.addEventListener(
            'click',
            function () {

                const pricingType =
                    document.getElementById('pricing_type').value;

                const pricingAmount =
                    document.getElementById('pricing_amount')
                        .value.trim();

                if (!pricingType) {
                    alert('Please select a pricing type.');
                    document.getElementById('pricing_type').focus();
                    return;
                }

                if (
                    !pricingAmount ||
                    !Number.isFinite(Number(pricingAmount)) ||
                    Number(pricingAmount) <= 0
                ) {
                    alert('Please enter a pricing amount greater than ₹0.');
                    document.getElementById('pricing_amount').focus();
                    return;
                }

                const proofDocument =
                    document.getElementById('proof_document');

                if (!proofDocument.files.length) {
                    alert('Please upload your proof document.');
                    proofDocument.focus();
                    return;
                }

                const proofFile = proofDocument.files[0];
                const allowedProofTypes = [
                    'application/pdf',
                    'image/jpeg',
                    'image/png'
                ];

                if (
                    !allowedProofTypes.includes(proofFile.type) ||
                    proofFile.size > 5 * 1024 * 1024
                ) {
                    alert(
                        'Proof must be a PDF, JPG or PNG file smaller than 5 MB.'
                    );
                    proofDocument.focus();
                    return;
                }

                const profileImage =
                    document.getElementById('profile_image');

                if (profileImage.files.length) {
                    const imageFile = profileImage.files[0];
                    const allowedImageTypes = [
                        'image/jpeg',
                        'image/png',
                        'image/webp'
                    ];

                    if (
                        !allowedImageTypes.includes(imageFile.type) ||
                        imageFile.size > 5 * 1024 * 1024
                    ) {
                        alert(
                            'Profile image must be JPG, PNG or WEBP and smaller than 5 MB.'
                        );
                        profileImage.focus();
                        return;
                    }
                }

                form.submit();
            }
        );


        /* ==========================================
           BACK STEP 2
        ========================================== */

        workerBackOne.addEventListener(
            'click',
            function () {

                heading.textContent =
                    'Create your account';

                subtitle.textContent =
                    'Get started with LAMO in just a few steps.';

                showStep(
                    stepAccount
                );

            }
        );


        /* ==========================================
           BACK STEP 4
        ========================================== */

        workerBackTwo.addEventListener(
            'click',
            function () {

                heading.textContent =
                    'Work & Pricing';

                subtitle.textContent =
                    'Set your location, availability and pricing.';

                showStep(
                    stepWorkerTwo
                );

            }
        );


        /* ==========================================
           BACK STEP 4
        ========================================== */

        /* ==========================================
           FINAL SUBMIT
        ========================================== */

        form.addEventListener(
            'submit',
            function (event) {


                const role =
                    roleSelect.value;


                const password =
                    document
                        .getElementById(
                            'password'
                        )
                        .value;


                const confirmPassword =
                    document
                        .getElementById(
                            'confirm_password'
                        )
                        .value;


                /* ROLE */

                if (!role) {

                    event.preventDefault();

                    alert(
                        'Please select User or Worker.'
                    );

                    showStep(
                        stepAccount
                    );

                    return;

                }


                /* PASSWORD */

                if (
                    password !==
                    confirmPassword
                ) {

                    event.preventDefault();

                    alert(
                        'Passwords do not match.'
                    );

                    showStep(
                        stepAccount
                    );

                    return;

                }


                if (
                    password.length < 8
                ) {

                    event.preventDefault();

                    alert(
                        'Password must contain at least 8 characters.'
                    );

                    showStep(
                        stepAccount
                    );

                    return;

                }


                /* WORKER */

                if (
                    role === 'WORKER'
                ) {

                    const proofDocument =
                        document.getElementById(
                            'proof_document'
                        );

                    const address =
                        document
                            .getElementById(
                                'address'
                            )
                            .value
                            .trim();


                    if (!proofDocument.files.length) {

                        event.preventDefault();

                        alert(
                            'Please upload your proof document.'
                        );

                        showStep(
                            stepWorkerTwo
                        );

                        proofDocument.focus();

                        return;

                    }

                    const proofFile =
                        proofDocument.files[0];

                    const allowedProofTypes = [
                        'application/pdf',
                        'image/jpeg',
                        'image/png'
                    ];

                    if (
                        !allowedProofTypes.includes(proofFile.type) ||
                        proofFile.size > 5 * 1024 * 1024
                    ) {
                        event.preventDefault();
                        alert(
                            'Proof must be a PDF, JPG or PNG file smaller than 5 MB.'
                        );
                        proofDocument.focus();
                        return;
                    }

                    const profileImage =
                        document.getElementById('profile_image');

                    if (profileImage.files.length) {
                        const imageFile =
                            profileImage.files[0];

                        const allowedImageTypes = [
                            'image/jpeg',
                            'image/png',
                            'image/webp'
                        ];

                        if (
                            !allowedImageTypes.includes(imageFile.type) ||
                            imageFile.size > 5 * 1024 * 1024
                        ) {
                            event.preventDefault();
                            alert(
                                'Profile image must be JPG, PNG or WEBP and smaller than 5 MB.'
                            );
                            profileImage.focus();
                            return;
                        }
                    }


                    if (!address) {

                        event.preventDefault();

                        alert(
                            'Please enter your address.'
                        );

                        showStep(
                            stepWorkerOne
                        );

                        document
                            .getElementById(
                                'address'
                            )
                            .focus();

                        return;

                    }

                }

            }
        );


        /* ==========================================
           LIVE VALIDATION
           Messages appear while the user types (and
           on blur), instead of only after Create
           Account is clicked. Nothing runs on page
           load, so an untouched form stays silent.
           Wording mirrors the server-side messages,
           and these checks never block submission -
           the view stays the final authority.
        ========================================== */

        const liveFieldIds = [
            'username',
            'email',
            'password',
            'confirm_password',
            'phone',
            'service',
            'custom_service',
            'district',
            'address',
            'experience_years',
            'availability',
            'pricing_amount',
            'proof_type',
            'proof_document'
        ];


        function showFieldError(field, message) {

            const slot = document.getElementById(field.id + 'Error');
            const group = field.parentElement;

            if (message) {

                if (slot) {
                    slot.textContent = message;
                    slot.classList.add('show');
                }

                if (
                    group &&
                    group.classList &&
                    group.classList.contains('input-group')
                ) {
                    group.classList.add('has-error');
                }

                field.classList.add('invalid');
                field.setAttribute('aria-invalid', 'true');

            } else {

                if (slot) {
                    slot.textContent = '';
                    slot.classList.remove('show');
                }

                if (group && group.classList) {
                    group.classList.remove('has-error');
                }

                field.classList.remove('invalid');
                field.removeAttribute('aria-invalid');

            }
        }


        function refreshLiveField(field) {

            if (!field) return;

            showFieldError(
                field,
                liveValidationMessage(field)
            );
        }


        liveFieldIds.forEach(function (fieldId) {

            const field = document.getElementById(fieldId);

            if (!field) return;


            function refresh() {

                field.dataset.touched = 'true';

                refreshLiveField(field);


                /* The confirmation slot depends on the password, and the
                   custom service only matters while "Other" is selected,
                   so those two are refreshed together with them. */
                if (fieldId === 'password') {
                    refreshLiveField(
                        document.getElementById('confirm_password')
                    );
                }

                if (fieldId === 'service') {
                    refreshLiveField(
                        document.getElementById('custom_service')
                    );
                }
            }

            field.addEventListener('input', refresh);
            field.addEventListener('change', refresh);
            field.addEventListener('blur', refresh);

        });


    }
);


/* ==========================================
   TOGGLE OTHER SERVICE FIELD
========================================== */

function toggleOtherService(value) {
    var group = document.getElementById('otherServiceGroup');
    var customInput = document.getElementById('custom_service');
    if (value === 'other') {
        if (group) group.style.display = 'block';
        if (customInput) customInput.required = true;
    } else {
        if (group) group.style.display = 'none';
        if (customInput) customInput.required = false;
    }
}


/* ==========================================
   PASSWORD SHOW / HIDE
========================================== */

function togglePassword(
    inputId,
    iconId
) {

    const input =
        document.getElementById(
            inputId
        );

    const icon =
        document.getElementById(
            iconId
        );


    if (
        input.type === 'password'
    ) {

        input.type = 'text';

        icon.classList.remove(
            'bi-eye'
        );

        icon.classList.add(
            'bi-eye-slash'
        );

    } else {

        input.type = 'password';

        icon.classList.remove(
            'bi-eye-slash'
        );

        icon.classList.add(
            'bi-eye'
        );

    }

}


/* ==========================================
   EXPOSE INLINE HANDLERS
   This whole file lives inside a
   document.addEventListener('DOMContentLoaded', ...)
   callback, so every function above is scoped to
   that callback and is NOT reachable from an
   inline onclick="..." attribute (those resolve
   against window). register.html wires its password
   show/hide and the "Other service" toggle that way,
   so without these two assignments the browser threw
   "togglePassword is not defined" and both controls
   were dead. Nothing about the behaviour changes.
========================================== */
window.togglePassword = togglePassword;
window.toggleOtherService = toggleOtherService;


/* ==========================================
   LIVE VALIDATION CHECKS
   Pure helpers used by the LIVE VALIDATION
   block inside the DOMContentLoaded callback
   above. They are declared at the top level
   of the file so they can be unit-tested
   outside the browser (function declarations
   hoist, so the callback can already call
   them). Every message matches the wording
   register_view and the step buttons already
   use, so the user sees one consistent
   message whether the problem is caught
   here, at the button, or on the server.
========================================== */

const LIVE_EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const LIVE_PHONE_PATTERN = /^[6-9][0-9]{9}$/;


function liveValidationMessage(field) {

    const id = field.id;
    const raw = field.value || '';
    const value = raw.trim();


    /* PASSWORD - length first, then "all digits", exactly
       like the server (len < 8, then validate_password). */
    if (id === 'password') {

        if (!raw) return 'Password is required.';

        if (raw.length < 8) {
            return 'Password must contain at least 8 characters.';
        }

        if (/^\d+$/.test(raw)) return 'This password is entirely numeric.';

        return '';
    }


    /* CONFIRM PASSWORD - a mismatch shows as soon as the user
       types something that differs; an untouched empty field
       stays silent until it has been blurred once. */
    if (id === 'confirm_password') {

        const passwordField = document.getElementById('password');
        const password = passwordField ? passwordField.value : '';

        if (!raw) {
            return field.dataset.touched && password
                ? 'Passwords do not match.'
                : '';
        }

        return raw !== password
            ? 'Passwords do not match.'
            : '';
    }


    if (id === 'email') {

        if (!value) return 'Email is required.';

        return LIVE_EMAIL_PATTERN.test(value)
            ? ''
            : 'Please enter a valid email address.';
    }


    if (id === 'username') {
        return value ? '' : 'Username is required.';
    }


    /* PHONE - same rule as register_view: [6-9] followed by
       nine digits (an empty value fails it too). */
    if (id === 'phone') {

        return LIVE_PHONE_PATTERN.test(value)
            ? ''
            : 'Please enter a valid 10-digit Indian mobile number.';
    }


    if (id === 'service') {
        return value ? '' : 'Please select your service.';
    }


    if (id === 'custom_service') {

        const serviceField = document.getElementById('service');

        if (!serviceField || serviceField.value !== 'other') return '';

        return value.length >= 2
            ? ''
            : 'Please enter your custom service name.';
    }


    if (id === 'district') {
        return value ? '' : 'Please select your district.';
    }


    if (id === 'availability') {
        return value ? '' : 'Please select your availability.';
    }


    if (id === 'proof_type') {
        return value
            ? ''
            : 'Please select a valid proof type (Aadhaar, PAN, or Voter ID).';
    }


    if (id === 'address') {

        if (!value) return 'Please enter your address.';

        return value.length >= 10
            ? ''
            : 'Please enter a complete address.';
    }


    if (id === 'experience_years') {

        if (value === '') return 'Please enter your experience.';

        const experience = Number(value);

        if (!Number.isInteger(experience)) {
            return 'Please enter a valid experience.';
        }

        if (experience < 0) return 'Experience cannot be negative.';

        if (experience > 60) return 'Experience cannot exceed 60 years.';

        return '';
    }


    if (id === 'pricing_amount') {

        if (value === '') return 'Please enter your pricing amount.';

        const amount = Number(value);

        if (!Number.isFinite(amount)) {
            return 'Please enter a valid pricing amount.';
        }

        if (amount <= 0) {
            return 'Please enter a pricing amount greater than ₹0.';
        }

        if (amount > 100000) {
            return 'Pricing amount seems too high. Please enter a valid amount.';
        }

        return '';
    }


    if (id === 'proof_document') {
        return field.files && field.files.length
            ? ''
            : 'Please upload your proof document.';
    }


    return '';
}
