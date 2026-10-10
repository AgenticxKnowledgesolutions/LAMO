function toggleLoginPassword() {

    const password =
      document.getElementById("password");

    const icon =
      document.getElementById(
        "loginPasswordIcon"
      );


    if (password.type === "password") {

      password.type = "text";

      icon.classList.remove(
        "bi-eye"
      );

      icon.classList.add(
        "bi-eye-slash"
      );

    }

    else {

      password.type = "password";

      icon.classList.remove(
        "bi-eye-slash"
      );

      icon.classList.add(
        "bi-eye"
      );

    }

  }


  /* =========================================================
     LOGIN BUTTON — Ripple + Loading Spinner
  ========================================================= */

  (function () {

    'use strict';

    const loginBtn =
      document.getElementById('loginBtn');

    if (!loginBtn) {
      return;
    }


    /* Ripple on click */
    loginBtn.addEventListener('mousedown', function (e) {

      const rect =
        loginBtn.getBoundingClientRect();

      const ripple =
        document.createElement('span');

      ripple.className = 'ripple';

      const size =
        Math.max(
          rect.width,
          rect.height
        );

      ripple.style.width =
        ripple.style.height =
          size + 'px';

      ripple.style.left =
        (e.clientX - rect.left - size / 2) + 'px';

      ripple.style.top =
        (e.clientY - rect.top - size / 2) + 'px';

      loginBtn.appendChild(ripple);

      ripple.addEventListener(
        'animationend',
        function () {
          ripple.remove();
        }
      );

    });


    /* Loading state on submit */
    const loginForm =
      document.querySelector(
        'form[action=""]'
      );

    if (loginForm) {

      loginForm.addEventListener('submit', function () {

        loginBtn.classList.add('loading');

      });

    }

  })();
