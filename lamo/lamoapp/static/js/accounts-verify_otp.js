(function () {

    'use strict';

    const digits = document.querySelectorAll('.otp-digit');
    const hidden = document.getElementById('otpHidden');
    const form = document.getElementById('otpForm');
    const group = document.querySelector('.otp-input-group');
    const verifyBtn = document.getElementById('verifyOtpBtn');

    if (!digits.length || !hidden || !form || !group) {
        return;
    }


    /* =====================================================
       UPDATE HIDDEN OTP
    ===================================================== */

    function updateHidden() {

        hidden.value = Array.from(digits)
            .map(input => input.value)
            .join('');

    }


    /* =====================================================
       CHECK OTP COMPLETION
    ===================================================== */

    function checkComplete() {

        const otp = Array.from(digits)
            .map(input => input.value)
            .join('');

        if (otp.length === 6) {

            group.classList.add('complete');
            group.classList.add('active-flow');

            return true;

        }

        group.classList.remove('complete');
        group.classList.remove('active-flow');

        return false;
    }


    /* =====================================================
       INPUT HANDLING
    ===================================================== */

    digits.forEach((input, index) => {

        input.addEventListener('input', function () {

            this.value = this.value
                .replace(/[^0-9]/g, '')
                .slice(0, 1);


            if (this.value.length === 1) {

                this.classList.add('filled');

                /*
                 * Small delay makes the box animation
                 * feel smoother before moving forward.
                 */
                if (index < digits.length - 1) {

                    setTimeout(() => {
                        digits[index + 1].focus();
                    }, 70);

                }

            } else {

                this.classList.remove('filled');

            }


            updateHidden();
            checkComplete();

        });


        /* =================================================
           BACKSPACE / ARROW NAVIGATION
           ================================================= */

        input.addEventListener('keydown', function (e) {

            if (
                e.key === 'Backspace' &&
                !this.value &&
                index > 0
            ) {

                e.preventDefault();

                digits[index - 1].value = '';

                digits[index - 1]
                    .classList
                    .remove('filled');

                digits[index - 1].focus();

                updateHidden();
                checkComplete();
            }


            if (
                e.key === 'ArrowLeft' &&
                index > 0
            ) {

                e.preventDefault();

                digits[index - 1].focus();

            }


            if (
                e.key === 'ArrowRight' &&
                index < digits.length - 1
            ) {

                e.preventDefault();

                digits[index + 1].focus();

            }

        });


        /* =================================================
           PASTE OTP
           ================================================= */

        input.addEventListener('paste', function (e) {

            e.preventDefault();

            const paste = (
                e.clipboardData ||
                window.clipboardData
            )
                .getData('text')
                .replace(/[^0-9]/g, '')
                .slice(0, 6);


            paste.split('').forEach((char, i) => {

                if (digits[i]) {

                    digits[i].value = char;

                    digits[i]
                        .classList
                        .add('filled');

                }

            });


            if (paste.length < 6) {

                digits[paste.length].focus();

            } else {

                digits[5].focus();

            }


            updateHidden();
            checkComplete();

        });

    });


    /* =====================================================
       FORM SUBMIT
    ===================================================== */

    form.addEventListener('submit', function (e) {

        updateHidden();

        const otp = hidden.value;


        /* Not complete */
        if (otp.length !== 6) {

            e.preventDefault();

            group.classList.remove('complete');
            group.classList.remove('active-flow');

            alert('Please enter all 6 digits.');

            const firstEmpty = Array.from(digits)
                .find(input => !input.value);

            if (firstEmpty) {
                firstEmpty.focus();
            }

            return;
        }


        /*
         * Let the user actually see the verification
         * animation before the browser navigates.
         */

        e.preventDefault();


        group.classList.add('complete');
        group.classList.add('active-flow');


        setTimeout(() => {

            group.classList.remove('active-flow');
            group.classList.add('verifying');

            if (verifyBtn) {
                verifyBtn.classList.add('verifying');
                verifyBtn.disabled = true;
            }

        }, 350);


        /*
         * Submit normally after the animation starts.
         * requestSubmit is NOT used here because it would
         * trigger this same submit handler again.
         */

        setTimeout(() => {

            HTMLFormElement.prototype.submit.call(form);

        }, 850);

    });


    /* =====================================================
       INITIAL FOCUS
    ===================================================== */

    if (digits.length > 0) {

        setTimeout(() => {
            digits[0].focus();
        }, 250);

    }


})();
