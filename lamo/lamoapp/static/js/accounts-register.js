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
