document.addEventListener("DOMContentLoaded", function () {
    // -------------------------------------------------------------
    // 1. Toast Notifications Auto-Dismiss & Hover Pause
    // -------------------------------------------------------------
    document.querySelectorAll(".lamo-toast").forEach(function (toast) {
        const closeButton = toast.querySelector(".lamo-toast-close");
        const progress = toast.querySelector(".lamo-toast-progress");
        let timer;

        function closeToast() {
            if (toast.classList.contains("is-closing")) return;

            clearTimeout(timer);
            toast.classList.add("is-closing");

            setTimeout(function () {
                toast.remove();
            }, 350);
        }

        if (closeButton) {
            closeButton.addEventListener("click", closeToast);
        }

        timer = setTimeout(closeToast, 4500);

        toast.addEventListener("mouseenter", function () {
            clearTimeout(timer);
            if (progress) {
                progress.style.animationPlayState = "paused";
            }
        });

        toast.addEventListener("mouseleave", function () {
            if (progress) {
                progress.style.animationPlayState = "running";
            }
            timer = setTimeout(closeToast, 1200);
        });
    });

    // -------------------------------------------------------------
    // 2. Strike Modal Trigger & Fallback Initializer
    // -------------------------------------------------------------
    document.querySelectorAll(".strike-btn").forEach(function (btn) {
        btn.addEventListener("click", function (e) {
            const targetSelector = this.getAttribute("data-bs-target");
            if (!targetSelector) return;

            const modalEl = document.querySelector(targetSelector);
            if (modalEl && window.bootstrap && window.bootstrap.Modal) {
                e.preventDefault();
                const modalInstance = window.bootstrap.Modal.getOrCreateInstance(modalEl);
                modalInstance.show();
            }
        });
    });

    // -------------------------------------------------------------
    // 3. Strike Modal Dynamic Action Switcher (Strike vs Warning vs Reset)
    // -------------------------------------------------------------
    document.querySelectorAll(".worker-strike-modal").forEach(function (modalEl) {
        const form = modalEl.querySelector(".strike-modal-form");
        if (!form) return;

        const radios = form.querySelectorAll(".action-type-radio");
        const submitBtn = form.querySelector(".strike-submit-btn");
        const suspendContainer = form.querySelector(".suspend-container");
        const suspendCheckbox = form.querySelector("input[name='suspend_now']");
        const reasonLabel = form.querySelector(".reason-label");
        const reasonInput = form.querySelector(".strike-reason-input");
        const optionCards = form.querySelectorAll(".strike-option-card");

        function updateActionUI(actionValue) {
            // Update active state class on option cards
            optionCards.forEach(function (card) {
                const radio = card.querySelector("input[type='radio']");
                if (radio && radio.value === actionValue) {
                    card.classList.add("active");
                } else {
                    card.classList.remove("active");
                }
            });

            if (actionValue === "strike") {
                if (submitBtn) {
                    submitBtn.className = "btn btn-sm btn-danger strike-submit-btn px-4 fw-bold";
                    submitBtn.innerHTML = '<i class="bi bi-shield-slash me-1"></i> Issue Official Strike (+1)';
                }
                if (suspendContainer) {
                    suspendContainer.style.display = "block";
                }
                if (reasonLabel) {
                    reasonLabel.innerHTML = 'Violation / Incident Details <span class="text-danger">*</span>';
                }
                if (reasonInput) {
                    reasonInput.placeholder = "Specify policy violation, unfulfilled booking, or incident details...";
                }
            } else if (actionValue === "warning") {
                if (submitBtn) {
                    submitBtn.className = "btn btn-sm btn-warning strike-submit-btn px-4 fw-bold";
                    submitBtn.innerHTML = '<i class="bi bi-chat-right-dots me-1"></i> Send Formal Warning';
                }
                if (suspendContainer) {
                    suspendContainer.style.display = "none";
                }
                if (suspendCheckbox) {
                    suspendCheckbox.checked = false;
                }
                if (reasonLabel) {
                    reasonLabel.innerHTML = 'Advisory / Warning Notes <span class="text-danger">*</span>';
                }
                if (reasonInput) {
                    reasonInput.placeholder = "Specify formal warning note and expected improvement...";
                }
            } else if (actionValue === "reset") {
                if (submitBtn) {
                    submitBtn.className = "btn btn-sm btn-success strike-submit-btn px-4 fw-bold";
                    submitBtn.innerHTML = '<i class="bi bi-arrow-counterclockwise me-1"></i> Reset Strikes to Zero';
                }
                if (suspendContainer) {
                    suspendContainer.style.display = "none";
                }
                if (suspendCheckbox) {
                    suspendCheckbox.checked = false;
                }
                if (reasonLabel) {
                    reasonLabel.innerHTML = 'Reason for Resetting Strikes <span class="text-danger">*</span>';
                }
                if (reasonInput) {
                    reasonInput.placeholder = "Document why strikes are being forgiven or cleared...";
                }
            }
        }

        radios.forEach(function (radio) {
            radio.addEventListener("change", function () {
                if (this.checked) {
                    updateActionUI(this.value);
                }
            });
        });

        // Initialize with default checked radio
        const initialChecked = form.querySelector(".action-type-radio:checked");
        if (initialChecked) {
            updateActionUI(initialChecked.value);
        }

        // Form submit spinner & prevent double clicks
        form.addEventListener("submit", function (e) {
            if (reasonInput && !reasonInput.value.trim()) {
                e.preventDefault();
                reasonInput.focus();
                return;
            }

            if (submitBtn) {
                submitBtn.disabled = true;
                const originalHtml = submitBtn.innerHTML;
                submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span> Processing...';
                // Fallback reset if page doesn't unload quickly
                setTimeout(function () {
                    submitBtn.disabled = false;
                    submitBtn.innerHTML = originalHtml;
                }, 8000);
            }
        });
    });
});