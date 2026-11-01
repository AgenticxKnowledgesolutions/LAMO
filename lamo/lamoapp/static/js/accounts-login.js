/**
 * accounts-login.js
 * Standalone Login Page - Mobile Number / Email + 6-digit OTP
 */

document.addEventListener('DOMContentLoaded', function () {
  'use strict';

  // CSRF token
  function getCsrfToken() {
    const input = document.querySelector('#standaloneLoginForm input[name="csrfmiddlewaretoken"]');
    return input ? input.value : '';
  }

  // -------------------------------------------------------------
  // DYNAMIC IDENTIFIER ICON (PHONE vs EMAIL)
  // -------------------------------------------------------------
  const otpIdInput = document.getElementById('otpIdentifierInput');
  const otpIdIcon = document.getElementById('otpIdentifierIcon');
  if (otpIdInput && otpIdIcon) {
    otpIdInput.addEventListener('input', function () {
      const val = this.value.trim();
      if (val.includes('@')) {
        otpIdIcon.className = 'bi bi-envelope login-input-icon';
      } else if (/^[0-9+() -]+$/.test(val) && val.length >= 3) {
        otpIdIcon.className = 'bi bi-telephone login-input-icon';
      } else {
        otpIdIcon.className = 'bi bi-person login-input-icon';
      }
    });
  }

  // -------------------------------------------------------------
  // OTP COUNTDOWN & RESEND
  // -------------------------------------------------------------
  let otpCountdownInterval = null;
  const otpTimerEl = document.getElementById('otpCountdownTimer');
  const btnResend = document.getElementById('btnResendOtp');

  function startOtpCountdown(seconds) {
    if (otpCountdownInterval) clearInterval(otpCountdownInterval);
    let rem = seconds || 30;
    if (otpTimerEl) {
      otpTimerEl.textContent = 'Resend code in ' + rem + 's';
      otpTimerEl.style.display = 'inline';
    }
    if (btnResend) btnResend.style.display = 'none';

    otpCountdownInterval = setInterval(function () {
      rem--;
      if (rem <= 0) {
        clearInterval(otpCountdownInterval);
        if (otpTimerEl) otpTimerEl.style.display = 'none';
        if (btnResend) btnResend.style.display = 'inline';
      } else {
        if (otpTimerEl) otpTimerEl.textContent = 'Resend code in ' + rem + 's';
      }
    }, 1000);
  }

  // -------------------------------------------------------------
  // STEP 1: SEND OTP
  // -------------------------------------------------------------
  const btnSendOtp = document.getElementById('btnSendOtp');
  const otpStep1 = document.getElementById('otpStep1');
  const otpStep2 = document.getElementById('otpStep2');
  const otpSendErr = document.getElementById('otpSendError');
  const otpMaskedTarget = document.getElementById('otpMaskedTarget');
  const otpCodeInput = document.getElementById('otpCodeInput');

  if (btnSendOtp) {
    btnSendOtp.addEventListener('click', function () {
      const val = otpIdInput ? otpIdInput.value.trim() : '';
      if (otpSendErr) {
        otpSendErr.textContent = '';
        otpSendErr.style.display = 'none';
      }

      if (!val) {
        if (otpSendErr) {
          otpSendErr.textContent = 'Please enter your mobile number or email.';
          otpSendErr.style.display = 'block';
        }
        if (otpIdInput) otpIdInput.focus();
        return;
      }

      btnSendOtp.disabled = true;
      btnSendOtp.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span> Sending OTP...';

      const fd = new FormData();
      fd.append('action', 'send_login_otp');
      fd.append('identifier', val);
      fd.append('csrfmiddlewaretoken', getCsrfToken());

      fetch(window.location.pathname, {
        method: 'POST',
        body: fd,
        headers: { 'X-Requested-With': 'XMLHttpRequest' }
      })
      .then(r => r.json())
      .then(data => {
        btnSendOtp.disabled = false;
        btnSendOtp.innerHTML = '<span class="btn-login-text">Send OTP Code <i class="bi bi-arrow-right"></i></span>';
        if (data.success) {
          if (otpMaskedTarget) otpMaskedTarget.textContent = data.masked_target || val;
          if (otpStep1) otpStep1.style.display = 'none';
          if (otpStep2) otpStep2.style.display = 'block';
          if (otpCodeInput) {
            otpCodeInput.value = '';
            otpCodeInput.focus();
          }
          startOtpCountdown(30);
        } else {
          if (otpSendErr) {
            otpSendErr.textContent = data.error || 'Unable to send OTP. Please try again.';
            otpSendErr.style.display = 'block';
          }
        }
      })
      .catch(() => {
        btnSendOtp.disabled = false;
        btnSendOtp.innerHTML = '<span class="btn-login-text">Send OTP Code <i class="bi bi-arrow-right"></i></span>';
        if (otpSendErr) {
          otpSendErr.textContent = 'Network error. Please try again.';
          otpSendErr.style.display = 'block';
        }
      });
    });
  }

  // Change target
  const btnChangeTarget = document.getElementById('btnChangeTarget');
  if (btnChangeTarget) {
    btnChangeTarget.addEventListener('click', function () {
      if (otpStep2) otpStep2.style.display = 'none';
      if (otpStep1) otpStep1.style.display = 'block';
      if (otpIdInput) otpIdInput.focus();
    });
  }

  if (btnResend) {
    btnResend.addEventListener('click', function () {
      if (btnSendOtp) btnSendOtp.click();
    });
  }

  // -------------------------------------------------------------
  // STEP 2: VERIFY OTP
  // -------------------------------------------------------------
  const btnVerifyOtp = document.getElementById('btnVerifyOtp');
  const otpVerifyErr = document.getElementById('otpVerifyError');

  if (btnVerifyOtp) {
    btnVerifyOtp.addEventListener('click', function () {
      const idVal = otpIdInput ? otpIdInput.value.trim() : '';
      const otpVal = otpCodeInput ? otpCodeInput.value.trim() : '';
      const nextVal = document.getElementById('standaloneLoginNext')?.value || '';

      if (otpVerifyErr) {
        otpVerifyErr.textContent = '';
        otpVerifyErr.style.display = 'none';
      }

      if (!otpVal || otpVal.length < 6) {
        if (otpVerifyErr) {
          otpVerifyErr.textContent = 'Please enter the complete 6-digit verification code.';
          otpVerifyErr.style.display = 'block';
        }
        if (otpCodeInput) otpCodeInput.focus();
        return;
      }

      btnVerifyOtp.disabled = true;
      btnVerifyOtp.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span> Verifying...';

      const fd = new FormData();
      fd.append('action', 'verify_login_otp');
      fd.append('identifier', idVal);
      fd.append('otp', otpVal);
      if (nextVal) fd.append('next', nextVal);
      fd.append('csrfmiddlewaretoken', getCsrfToken());

      fetch(window.location.pathname, {
        method: 'POST',
        body: fd,
        headers: { 'X-Requested-With': 'XMLHttpRequest' }
      })
      .then(r => r.json())
      .then(data => {
        btnVerifyOtp.disabled = false;
        btnVerifyOtp.innerHTML = '<span class="btn-login-text">Verify &amp; Sign In <i class="bi bi-arrow-right"></i></span>';
        if (data.success) {
          window.location.href = data.redirect_url || '/';
        } else {
          if (otpVerifyErr) {
            otpVerifyErr.textContent = data.error || 'Verification failed. Please check the code.';
            otpVerifyErr.style.display = 'block';
          }
        }
      })
      .catch(() => {
        btnVerifyOtp.disabled = false;
        btnVerifyOtp.innerHTML = '<span class="btn-login-text">Verify &amp; Sign In <i class="bi bi-arrow-right"></i></span>';
        if (otpVerifyErr) {
          otpVerifyErr.textContent = 'Network error. Please try again.';
          otpVerifyErr.style.display = 'block';
        }
      });
    });
  }

  // -------------------------------------------------------------
  // KEYBOARD ENTER HANDLERS
  // -------------------------------------------------------------
  if (otpIdInput) {
    otpIdInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        if (btnSendOtp) btnSendOtp.click();
      }
    });
  }

  if (otpCodeInput) {
    otpCodeInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        if (btnVerifyOtp) btnVerifyOtp.click();
      }
    });
  }

  // Auto-focus first input on load
  if (otpIdInput) setTimeout(() => otpIdInput.focus(), 120);
});
